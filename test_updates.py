import argparse
import importlib.util
from pathlib import Path
import tempfile
import subprocess
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location("resume_state", Path(__file__).with_name("state.py"))
state = importlib.util.module_from_spec(spec)
spec.loader.exec_module(state)


class UpdateTests(unittest.TestCase):
    def test_failed_update_still_resumes_without_update_dialog(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = argparse.Namespace(agent="codex", session_id="saved-conversation", cwd=directory,
                                      goal_status="complete", continue_turn=False)
            with patch.object(state, "CODEX_EXE", "/bin/false"), \
                 patch.object(state, "STATE_DIR", root), \
                 patch.object(state, "record_managed_launcher"), \
                 patch.object(state, "launch_shell", return_value=0), \
                 patch.object(state.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run:
                state.launch_command(args)
            command = run.call_args.args[0]
            self.assertIn("check_for_update_on_startup=false", command)
            self.assertEqual(command[-2:], ["resume", "saved-conversation"])
            self.assertEqual(state.read_json(root / "update-boot.json", {})["status"], 1)

    def test_timeout_stops_installer_children(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(state, "STATE_DIR", Path(directory)), \
             patch.object(state.subprocess, "Popen") as popen, \
             patch.object(state.os, "killpg") as killpg:
            process = popen.return_value
            process.wait.side_effect = [subprocess.TimeoutExpired("update", 180), -9]
            state.prepare_codex_update("codex")
            killpg.assert_called_once_with(process.pid, state.signal.SIGKILL)
            self.assertTrue(popen.call_args.kwargs["start_new_session"])
            self.assertEqual(process.wait.call_count, 2)

    def test_claude_resume_does_not_run_codex_update(self):
        args = argparse.Namespace(agent="claude", session_id="claude-conversation", cwd="/tmp",
                                  goal_status=None, continue_turn=False)
        with patch.object(state, "CLAUDE_EXE", "/bin/true"), \
             patch.object(state, "prepare_codex_update") as update, \
             patch.object(state, "record_managed_launcher"), \
             patch.object(state, "launch_shell", return_value=0), \
             patch.object(state.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run:
            state.launch_command(args)
        update.assert_not_called()
        self.assertEqual(run.call_args.args[0], ["/bin/true", "--resume", "claude-conversation"])

    def test_restore_retains_first_boot_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = {"sessions": [{"name": "saved", "windows": []}]}
            args = argparse.Namespace(dry_run=False)
            with patch.object(state, "STATE_DIR", root), \
                 patch.object(state, "SNAPSHOT_PATH", root / "snapshot.json"), \
                 patch.object(state, "RESTORE_MARKER_PATH", root / "restored.json"), \
                 patch.object(state, "LOCK_PATH", root / "state.lock"), \
                 patch.object(state, "RESTORE_LOCK_PATH", root / "restore.lock"), \
                 patch.object(state, "boot_id", return_value="test-boot"), \
                 patch.object(state, "existing_sessions", return_value={"saved": "$1"}):
                state.atomic_write_json(state.SNAPSHOT_PATH, snapshot)
                state.restore_command(args)
                state.atomic_write_json(state.SNAPSHOT_PATH, {"sessions": [{"name": "saved", "windows": [], "changed": True}]})
                state.restore_command(args)
            self.assertEqual(state.read_json(root / "recovery/test-boot.json", {}), snapshot)

    def test_update_finishes_before_saved_session_resumes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            events = root / "events"
            ready = root / "ready"
            executable = root / "codex"
            executable.write_text(
                "#!/usr/bin/python3\nimport pathlib, sys\n"
                f"events = pathlib.Path({str(events)!r})\n"
                f"ready = pathlib.Path({str(ready)!r})\n"
                "if 'update' in sys.argv:\n"
                "    ready.touch()\n"
                "    with events.open('a') as f: f.write('updated\\n')\n"
                "elif ready.exists() and 'check_for_update_on_startup=false' in sys.argv:\n"
                "    with events.open('a') as f: f.write('resumed ' + sys.argv[sys.argv.index('resume') + 1] + '\\n')\n"
                "else:\n"
                "    with events.open('a') as f: f.write('update prompt; exited without resuming\\n')\n"
            )
            executable.chmod(0o700)
            args = argparse.Namespace(agent="codex", session_id="saved-conversation", cwd=directory,
                                      goal_status="complete", continue_turn=False)
            with patch.object(state, "CODEX_EXE", str(executable)), \
                 patch.object(state, "STATE_DIR", root), \
                 patch.object(state, "record_managed_launcher"), \
                 patch.object(state, "launch_shell", return_value=0):
                state.launch_command(args)
                state.launch_command(args)
            self.assertEqual(events.read_text().splitlines(),
                             ["updated", "resumed saved-conversation", "resumed saved-conversation"])


if __name__ == "__main__":
    unittest.main()
