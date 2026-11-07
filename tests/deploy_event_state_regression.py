import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


class DeploymentStateTests(unittest.TestCase):
    def test_migration_existing_state_and_rollback(self):
        bash = shutil.which("bash") if os.name != "nt" else "C:/Program Files/Git/bin/bash.exe"
        if not bash or not Path(bash).exists():
            self.skipTest("Bash is required for deployment checks")
        source = Path("scripts/deploy-remote.sh").read_text()
        for mode in ("migration", "existing", "rollback"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "repo/.git").mkdir(parents=True)
                (root / "repo/.env").touch()
                (root / "old.json").write_text('{"events":[],"marker":"old"}')
                if mode == "existing":
                    (root / "state/events").mkdir(parents=True)
                    (root / "state/events/events.json").write_text('{"events":[],"marker":"persistent"}')
                mocks = r'''
function git() {
  case "$1" in
    branch) echo refs/remotes/origin/test ;;
    rev-parse) echo abcdef123456 ;;
  esac
}
function flock() { return 0; }
function docker() {
  echo "$*" >> "$test_root/commands"
  case "$1" in
    container)
      [[ "$3" == para-bot-container ]] ;;
    exec)
      case "$*" in
        *abspath*) echo /app/data/events.json ;;
        *isfile*) echo 1 ;;
      esac ;;
    cp)
      if [[ "$2" == para-bot-*:* ]]; then
        cp "$test_root/old.json" "$3"
      else
        cp "$2" "$test_root/restored.json"
      fi ;;
    run)
      if [[ "$test_mode" == rollback ]]; then
        echo '{"events":[],"marker":"delivered"}' > "$state_dir/events/events.json"
        return 1
      fi ;;
  esac
}
'''
                script = source.replace('repo_dir="${HOME}/UmaCore"',
                                        'repo_dir="' + root.as_posix() + '/repo"')
                script = script.replace('state_dir="${HOME}/umacore-state"',
                                        'state_dir="' + root.as_posix() + '/state"')
                script = script.replace('set -Eeuo pipefail', 'set -Eeuo pipefail\n' +
                    'test_root="' + root.as_posix() + '"\ntest_mode="' + mode + '"\n' + mocks)
                path = root / "deploy.sh"
                path.write_text(script, newline="\n")
                result = subprocess.run([bash, str(path), "test"], capture_output=True, text=True)
                self.assertEqual(result.returncode, 1 if mode == "rollback" else 0, result.stderr)
                expected = {"migration": "old", "existing": "persistent", "rollback": "delivered"}[mode]
                state_file = "events.rollback.json" if mode == "rollback" else "events.json"
                self.assertIn(expected, (root / "state/events" / state_file).read_text())
                commands = (root / "commands").read_text()
                self.assertIn("dst=/app/event-state", commands)
                self.assertIn("LOG_FILE=/app/logs/bot.log", commands)
                if mode == "rollback":
                    self.assertIn("delivered", (root / "restored.json").read_text())
                    self.assertLess(commands.rindex("cp "), commands.index("start para-bot-container"))
