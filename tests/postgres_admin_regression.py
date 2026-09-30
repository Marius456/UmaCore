"""Migration failure boundaries; no Docker, network, or production data access."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    'postgres_admin', Path(__file__).parents[1] / 'scripts/postgres-admin.py')
admin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(admin)


class MigrationSafetyTests(unittest.TestCase):
    def test_source_uri_becomes_libpq_environment(self):
        env = admin.source_environment('postgresql://user:p%40ss@db.example/test?sslmode=require')
        self.assertIn('PGDATABASE=test\n', env)
        self.assertIn('PGPASSWORD=p@ss\n', env)
        self.assertIn('PGHOST=db.example\n', env)

    def test_source_rejects_decoded_newlines(self):
        with self.assertRaisesRegex(RuntimeError, 'Invalid source'):
            admin.source_environment('postgresql://user:bad%0Avalue@host/db')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.root_patch = patch.object(admin, 'ROOT', self.root)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)

    def prepare_cutover(self):
        (self.root / 'prepared').write_text('ok')
        (self.root / 'original-bot.json').write_text(json.dumps({
            'Mounts': [], 'HostConfig': {'PortBindings': {}}, 'Image': 'sha256:old'}))

    def test_other_source_clients_abort_and_restart_original_bot(self):
        self.prepare_cutover()
        with patch.object(admin, 'run') as run, patch.object(admin, 'sql', return_value='1'):
            with self.assertRaisesRegex(RuntimeError, 'Other source clients'):
                admin.cutover()
        self.assertEqual(run.call_args_list[-1].args[0], ['docker', 'start', admin.BOT])
        self.assertFalse((self.root / 'cutover-started').exists())

    def test_changed_source_aborts_before_switching(self):
        self.prepare_cutover()
        with patch.object(admin, 'run') as run, patch.object(admin, 'sql', return_value='0'), \
                patch.object(admin.time, 'sleep'), patch.object(admin, 'dump'), \
                patch.object(admin, 'restore'), \
                patch.object(admin, 'manifest', side_effect=[{'v': 1}, {'v': 1}, {'v': 2}]):
            with self.assertRaisesRegex(RuntimeError, 'Final data validation'):
                admin.cutover()
        self.assertEqual(run.call_args_list[-1].args[0], ['docker', 'start', admin.BOT])
        self.assertFalse((self.root / 'cutover-started').exists())

    def test_cutover_cannot_run_twice(self):
        self.prepare_cutover()
        (self.root / 'cutover-started').write_text('started')
        with patch.object(admin, 'run') as run:
            with self.assertRaisesRegex(RuntimeError, 'already attempted'):
                admin.cutover()
        run.assert_not_called()

    def test_recreation_requires_stopped_bot(self):
        with patch.object(admin, 'run', return_value=b'true') as run:
            with self.assertRaisesRegex(RuntimeError, 'Stop the bot'):
                admin.recreate()
        self.assertEqual(run.call_count, 1)

    def test_failed_backup_validation_preserves_old_backups(self):
        old = self.root / 'backup-20000101T000000Z.dump'
        old.write_bytes(b'previous backup')
        with patch.object(admin, 'dump', return_value=old), \
                patch.object(admin, 'run', side_effect=RuntimeError('archive invalid')):
            with self.assertRaises(RuntimeError):
                admin.backup()
        self.assertTrue(old.exists())

    def test_restore_refuses_arbitrary_database_names(self):
        with patch.object(admin, 'sql') as sql:
            with self.assertRaisesRegex(RuntimeError, 'Unexpected restore'):
                admin.restore(Path('unused.dump'), 'postgres')
        sql.assert_not_called()
