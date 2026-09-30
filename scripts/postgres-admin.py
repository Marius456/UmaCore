#!/usr/bin/env python3
"""Ubuntu-only database migration and backup operations. Never prints credentials."""
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import time
from urllib.parse import parse_qs, unquote, urlsplit

ROOT = Path.home() / '.local/share/umacore-postgres'
REPO = Path.home() / 'UmaCore'
CONTAINER = 'umacore-postgres'
NETWORK = 'umacore-db'
VOLUME = 'umacore-postgres-data'
BOT = 'para-bot-container'
IMAGE = 'postgres:17-bookworm'


def run(args, data=None):
    result = subprocess.run(args, input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        # Tool stderr can include credentials/row contents. Keep it on the protected host.
        private('last-error.log', result.stderr)
        raise RuntimeError(f'{args[0]} failed; see {ROOT}/last-error.log on the server')
    return result.stdout


def private(name, data):
    path = ROOT / name
    with path.open('wb') as stream:
        stream.write(data.encode() if isinstance(data, str) else data)
    path.chmod(0o600)
    return path


def exists(kind, name):
    return subprocess.run(['docker', kind, 'inspect', name], capture_output=True).returncode == 0


def client(source=False):
    if source:
        return ['docker', 'run', '--rm', '-i', '--env-file', str(ROOT / 'source.env'), IMAGE]
    return ['docker', 'exec', '-i', CONTAINER]


def sql(query, database='umacore', source=False):
    args = client(source) + ['psql', '-X', '-A', '-t', '-v', 'ON_ERROR_STOP=1']
    if not source:
        args += ['-U', 'postgres', '-d', database]
    return run(args, query.encode()).decode().strip()


def identifier(value):
    return '"' + value.replace('"', '""') + '"'


def source_environment(url):
    parsed = urlsplit(url)
    options = parse_qs(parsed.query)
    values = {
        'PGHOST': parsed.hostname or '', 'PGPORT': str(parsed.port or 5432),
        'PGUSER': unquote(parsed.username or ''), 'PGPASSWORD': unquote(parsed.password or ''),
        'PGDATABASE': unquote(parsed.path.lstrip('/')), 'PGCONNECT_TIMEOUT': '15',
        'PGSSLMODE': options.get('sslmode', ['require'])[0],
        'PGCHANNELBINDING': options.get('channel_binding', ['prefer'])[0],
    }
    if any('\n' in value or '\r' in value for value in values.values()):
        raise RuntimeError('Invalid source connection parameter')
    return ''.join(f'{key}={value}\n' for key, value in values.items())


def manifest(database='umacore', source=False):
    def query(q):
        return sql(q, database, source)
    tables = json.loads(query("SELECT coalesce(json_agg(tablename ORDER BY tablename),'[]') "
                              "FROM pg_tables WHERE schemaname='public'"))
    rows = {}
    for table in tables:
        # Full-row fingerprints detect data changes, not just matching counts.
        result = query(f'SELECT row_to_json(t)::text FROM public.{identifier(table)} t '
                       'ORDER BY row_to_json(t)::text COLLATE "C"')
        rows[table] = {'count': int(query(f'SELECT count(*) FROM public.{identifier(table)}')),
                       'sha256': hashlib.sha256(result.encode()).hexdigest()}
    seqs = json.loads(query("SELECT coalesce(json_agg(sequencename ORDER BY sequencename),'[]') "
                            "FROM pg_sequences WHERE schemaname='public'"))
    return {
        'tables': rows,
        'sequences': {s: query(f'SELECT last_value,is_called FROM public.{identifier(s)}')
                      for s in seqs},
        'constraints': query("SELECT c.relname,k.conname,pg_get_constraintdef(k.oid) "
                             "FROM pg_constraint k JOIN pg_class c ON c.oid=k.conrelid "
                             "JOIN pg_namespace n ON n.oid=c.relnamespace "
                             "WHERE n.nspname='public' ORDER BY 1,2"),
        'extensions': query('SELECT extname,extversion FROM pg_extension ORDER BY 1'),
    }


def dump(name, source=False):
    args = client(source) + ['pg_dump', '-Fc', '--no-owner', '--no-acl']
    if not source:
        args += ['-U', 'postgres', '-d', 'umacore']
    path = private(name + '.partial', run(args))
    final = ROOT / name
    path.replace(final)
    return final


def restore(path, database):
    if database not in ('umacore', 'umacore_trial', 'umacore_verify'):
        raise RuntimeError('Unexpected restore target')
    # Refuse to replace an existing database; recovery requires an explicit operator decision.
    sql(f'CREATE DATABASE {identifier(database)} OWNER umacore;', 'postgres')
    run(client() + ['pg_restore', '-U', 'postgres', '-d', database, '--role=umacore',
                    '--no-owner', '--no-acl', '--exit-on-error', '--single-transaction'],
        path.read_bytes())


def start_database():
    run(['docker', 'run', '-d', '--name', CONTAINER, '--network', NETWORK,
         '--restart', 'unless-stopped', '--env-file', str(ROOT / 'postgres.env'),
         '--mount', f'type=volume,src={VOLUME},dst=/var/lib/postgresql/data',
         '--health-cmd', 'pg_isready -h 127.0.0.1 -U postgres', '--health-interval', '10s',
         '--health-timeout', '5s', '--health-retries', '6', IMAGE,
         '-c', 'shared_buffers=128MB', '-c', 'work_mem=4MB', '-c', 'max_connections=30',
         '-c', 'maintenance_work_mem=32MB'])
    for _ in range(60):
        if subprocess.run(['docker', 'exec', CONTAINER, 'pg_isready', '-h', '127.0.0.1',
                           '-U', 'postgres'],
                          capture_output=True).returncode == 0:
            return
        time.sleep(1)
    raise RuntimeError('PostgreSQL did not become ready')


def prepare():
    if exists('container', CONTAINER) or exists('volume', VOLUME):
        raise RuntimeError('Database resources already exist; inspect before retrying')
    info = json.loads(run(['docker', 'inspect', BOT]))[0]
    env = dict(item.split('=', 1) for item in info['Config']['Env'])
    source_url = env['DATABASE_URL']
    if '\n' in source_url or '\r' in source_url:
        raise RuntimeError('Invalid source URL')
    private('source.env', source_environment(source_url))
    private('original-bot.json', json.dumps(info))
    private('original.env', (REPO / '.env').read_bytes())
    run(['docker', 'pull', IMAGE])
    version = int(sql('SHOW server_version_num', source=True))
    if version // 10000 != 17:
        raise RuntimeError('Source is not PostgreSQL 17; match image major before proceeding')
    print('Source PostgreSQL 17 accessible; size:', sql(
        'SELECT pg_database_size(current_database())', source=True), flush=True)
    if not exists('network', NETWORK):
        run(['docker', 'network', 'create', NETWORK])
    run(['docker', 'volume', 'create', VOLUME])
    private('postgres.env', f'POSTGRES_PASSWORD={secrets.token_hex(32)}\n')
    password = secrets.token_hex(32)
    private('app-url', f'postgresql://umacore:{password}@{CONTAINER}:5432/umacore')
    start_database()
    sql(f"CREATE ROLE umacore LOGIN PASSWORD '{password}' NOSUPERUSER NOCREATEDB NOCREATEROLE;",
        'postgres')
    trial = dump('trial.dump', source=True)
    restore(trial, 'umacore_trial')
    before = manifest(source=True)
    after = manifest('umacore_trial')
    private('trial-source.json', json.dumps(before, indent=2))
    if before != after:
        raise RuntimeError('Trial differs from live source; investigate changes before cutover')
    sql('BEGIN; SET LOCAL ROLE umacore; CREATE TABLE public.migration_probe (id int); '
        'INSERT INTO public.migration_probe VALUES (1); SELECT * FROM public.migration_probe; '
        'ROLLBACK;', 'umacore_trial')
    private('prepared', 'ok\n')
    print('Trial restore matches all rows, sequences, constraints, and extensions.', flush=True)


def cutover():
    if not (ROOT / 'prepared').exists() or (ROOT / 'cutover-started').exists():
        raise RuntimeError('Not prepared or cutover already attempted; inspect state')
    info = json.loads((ROOT / 'original-bot.json').read_text())
    if info['Mounts'] or info['HostConfig']['PortBindings']:
        raise RuntimeError('Bot has mounts/ports requiring explicit preservation')
    run(['docker', 'stop', BOT])
    try:
        # Check repeatedly after stopping the known writer; retain source before/after fingerprints.
        for _ in range(3):
            sessions = int(sql("SELECT count(*) FROM pg_stat_activity WHERE "
                               "datname=current_database() AND pid<>pg_backend_pid() "
                               "AND backend_type='client backend'", source=True))
            if sessions:
                raise RuntimeError('Other source clients remain; coordinate their pause')
            time.sleep(3)
        before = manifest(source=True)
        final = dump('final-source.dump', source=True)
        restore(final, 'umacore')
        if before != manifest() or before != manifest(source=True):
            raise RuntimeError('Final data validation failed or source changed')
        private('final-manifest.json', json.dumps(before, indent=2))
        # The source is still authoritative until this marker is written.
    except Exception:
        run(['docker', 'start', BOT])
        raise
    private('cutover-started', dt.datetime.now(dt.timezone.utc).isoformat())
    env_path = REPO / '.env'
    original = env_path.read_text()
    lines = [line for line in original.splitlines()
             if not line.strip().startswith(('DATABASE_URL=', 'export DATABASE_URL='))]
    lines.append('DATABASE_URL=' + (ROOT / 'app-url').read_text())
    temp = env_path.with_name('.env.postgres-tmp')
    temp.write_text('\n'.join(lines) + '\n')
    temp.chmod(0o600)
    temp.replace(env_path)
    run(['docker', 'rename', BOT, BOT + '-neon-retired'])
    # Prevent the old source writer from returning after a server reboot.
    run(['docker', 'update', '--restart=no', BOT + '-neon-retired'])
    run(['docker', 'run', '-d', '--name', BOT, '--network', NETWORK,
         '--env-file', str(env_path), '--restart', 'always', info['Image']])
    print('Bot switched. Do not restart the retired Neon container after new writes.', flush=True)


def backup():
    stamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    path = dump(f'backup-{stamp}.dump')
    # Verify archive structure before expiring any previous successful backup.
    run(client() + ['pg_restore', '--list'], path.read_bytes())
    cutoff = time.time() - 7 * 86400
    for old in ROOT.glob('backup-*.dump'):
        if old.stat().st_mtime < cutoff:
            old.unlink()
    print(f'Backup complete: {path.name}', flush=True)


def verify_backup():
    path = sorted(ROOT.glob('backup-*.dump'))[-1]
    restore(path, 'umacore_verify')
    restored = manifest('umacore_verify')
    private('backup-restore-manifest.json', json.dumps(restored, indent=2))
    print('Backup restored successfully. Table counts:',
          json.dumps({k: v['count'] for k, v in restored['tables'].items()}), flush=True)
    sql('DROP DATABASE umacore_verify;', 'postgres')


def recreate():
    running = run(['docker', 'inspect', '-f', '{{.State.Running}}', BOT]).decode().strip()
    if running == 'true':
        raise RuntimeError('Stop the bot before the persistence check')
    before = manifest()
    run(['docker', 'stop', CONTAINER])
    run(['docker', 'rm', CONTAINER])
    start_database()
    if before != manifest():
        raise RuntimeError('Persistence validation failed')
    print('Database container recreated; all data preserved.', flush=True)


def main():
    import fcntl  # Operations run on Ubuntu; helpers remain importable in Windows tests.
    os.umask(0o077)
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['prepare', 'cutover', 'backup', 'verify-backup', 'recreate'])
    action = parser.parse_args().action
    with (ROOT / 'admin.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with (REPO / '.git/umacore-deploy.lock').open('w') as deploy_lock:
            fcntl.flock(deploy_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            globals()[action.replace('-', '_')]()


if __name__ == '__main__':
    main()
