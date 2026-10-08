import argparse
import importlib.util
import json
import os
from pathlib import Path
import shlex
import sqlite3
import tempfile
import subprocess
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location("resume_state", Path(__file__).with_name("state.py"))
state = importlib.util.module_from_spec(spec)
spec.loader.exec_module(state)


class UpdateTests(unittest.TestCase):
    def test_pane_title_resolves_unique_codex_thread_name(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "state.sqlite"
            connection = sqlite3.connect(database)
            try:
                connection.execute(
                    "CREATE TABLE threads (id TEXT, name TEXT, rollout_path TEXT, archived INTEGER)"
                )
                connection.executemany(
                    "INSERT INTO threads VALUES (?, ?, ?, 0)",
                    [
                        ("12345678-1234-1234-1234-123456789abc", "Security review", "/one.jsonl"),
                        ("87654321-4321-4321-4321-cba987654321", "Other work", "/two.jsonl"),
                    ],
                )
                connection.commit()
            finally:
                connection.close()
            with patch.object(state, "CODEX_STATE_DB_PATH", database):
                self.assertEqual(
                    state.codex_session_from_pane_title("⠋ Security review | codex"),
                    ("12345678-1234-1234-1234-123456789abc", "/one.jsonl"),
                )
                connection = sqlite3.connect(database)
                try:
                    connection.execute(
                        "INSERT INTO threads VALUES (?, ?, ?, 0)",
                        ("aaaaaaaa-1234-1234-1234-123456789abc", "Security review", "/three.jsonl"),
                    )
                    connection.commit()
                finally:
                    connection.close()
                self.assertEqual(
                    state.codex_session_from_pane_title("⠋ Security review | codex"),
                    (None, None),
                )

    def test_manual_codex_replacement_prefers_pane_title_over_managed_session_id(self):
        pane = {
            "pane_current_command": "codex",
            "pane_id": "%1",
            "pane_title": "⠋ Correct thread | codex",
        }
        hooks = {
            "servers": {
                "server": {
                    "panes": {
                        "%1": {
                            "agent": "codex",
                            "session_id": "12345678-1234-1234-1234-123456789abc",
                            "managed_launcher": True,
                        },
                    },
                },
            },
        }
        processes = {("server", "%1"): [Path("/proc/123")]}
        with patch.object(state, "root_codex_session_from_process", return_value=(None, None)), \
             patch.object(
                 state,
                 "codex_session_from_pane_title",
                 return_value=("87654321-4321-4321-4321-cba987654321", "/correct.jsonl"),
             ), \
             patch.object(state, "pane_screen_is_busy", return_value=False):
            metadata = state.agent_metadata("codex", pane, hooks, "server", processes)

        self.assertEqual(metadata["session_id"], "87654321-4321-4321-4321-cba987654321")
        self.assertEqual(metadata["transcript_path"], "/correct.jsonl")

    def test_shared_app_server_transcript_is_not_attributed_to_its_starting_pane(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            process = root / "proc" / "123"
            descriptors = process / "fd"
            descriptors.mkdir(parents=True)
            transcript = root / "sessions" / "saved.jsonl"
            transcript.parent.mkdir()
            transcript.write_text(json.dumps({
                "type": "session_meta",
                "payload": {
                    "id": "12345678-1234-1234-1234-123456789abc",
                    "originator": "codex-tui",
                    "source": "vscode",
                },
            }) + "\n")
            (process / "cmdline").write_bytes(b"codex\0app-server\0--listen\0unix://\0")
            os.symlink(transcript, descriptors / "7")

            self.assertEqual(state.root_codex_session_from_process(process), (None, None))

    def test_subagent_source_does_not_abort_root_session_lookup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            process = root / "proc" / "123"
            descriptors = process / "fd"
            descriptors.mkdir(parents=True)
            transcript = root / "sessions" / "child.jsonl"
            transcript.parent.mkdir()
            transcript.write_text(json.dumps({
                "type": "session_meta",
                "payload": {
                    "id": "12345678-1234-1234-1234-123456789abc",
                    "originator": "codex-tui",
                    "source": {"subagent": "review"},
                },
            }) + "\n")
            (process / "cmdline").write_bytes(b"codex\0--yolo\0")
            os.symlink(transcript, descriptors / "7")

            self.assertEqual(state.root_codex_session_from_process(process), (None, None))

    def test_codex_snapshot_preserves_launch_options_from_selected_root_process(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            process = root / "proc" / "123"
            descriptors = process / "fd"
            descriptors.mkdir(parents=True)
            transcript = root / "sessions" / "root.jsonl"
            transcript.parent.mkdir()
            transcript.write_text(json.dumps({
                "type": "session_meta",
                "payload": {
                    "id": "12345678-1234-1234-1234-123456789abc",
                    "originator": "codex-tui",
                    "source": "cli",
                },
            }) + "\n")
            (process / "cmdline").write_bytes(
                b"codex\0-m\0gpt-6-sol\0-c\0model_provider=openai\0"
                b"--enable\0cli_daybreak\0--yolo\0resume\0"
                b"12345678-1234-1234-1234-123456789abc\0"
            )
            os.symlink(transcript, descriptors / "7")
            pane = {"pane_id": "%1", "pane_current_command": "codex", "pane_title": ""}
            with patch.object(state, "owns_foreground_job", return_value=True), \
                 patch.object(state, "process_stat", return_value=(1, 1, 1, 1)), \
                 patch.object(state, "pane_screen_is_busy", return_value=True):
                metadata = state.agent_metadata(
                    "codex", pane, {"servers": {}}, "server", {("server", "%1"): [process]}
                )
            self.assertEqual(metadata["session_id"], "12345678-1234-1234-1234-123456789abc")
            self.assertEqual(metadata["launch_options"], [
                "-m", "gpt-6-sol", "-c", "model_provider=openai",
                "--enable", "cli_daybreak", "--yolo",
            ])

    def test_codex_resume_replays_saved_launch_options(self):
        args = argparse.Namespace(
            agent="codex", session_id="saved-conversation", cwd="/tmp",
            goal_status="complete", continue_turn=False,
            launch_options=json.dumps([
                "-m", "gpt-6-sol", "-c", "model_provider=openai",
                "--enable", "cli_daybreak", "--yolo",
            ]),
        )
        with patch.object(state, "CODEX_EXE", "/bin/true"), \
             patch.object(state, "prepare_codex_update", return_value=True), \
             patch.object(state, "record_managed_launcher"), \
             patch.object(state, "launch_shell", return_value=0), \
             patch.object(state.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run:
            state.launch_command(args)
        self.assertEqual(run.call_args.args[0], [
            "/bin/true", "-m", "gpt-6-sol", "-c", "model_provider=openai",
            "--enable", "cli_daybreak", "--yolo", "resume", "saved-conversation",
        ])

    def test_pane_launch_carries_options_to_launcher(self):
        pane = {
            "path": "/tmp",
            "codex": {
                "session_id": "12345678-1234-1234-1234-123456789abc",
                "launch_options": ["-m", "gpt-6-sol", "--enable", "cli_daybreak"],
            },
        }
        command = shlex.split(state.pane_launch_command(pane))
        arguments = state.parser().parse_args(command[command.index("launch"):])
        self.assertEqual(json.loads(arguments.launch_options), pane["codex"]["launch_options"])

    def test_claude_permission_flag_is_preserved_without_prompt_or_token(self):
        arguments = [
            "--dangerously-skip-permissions", "--resume", "saved-conversation",
            "--token", "private-value",
        ]
        self.assertEqual(
            state.launch_options_from_arguments("claude", arguments),
            ["--dangerously-skip-permissions"],
        )

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
            self.assertFalse(state.prepare_codex_update("codex"))
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
                "elif ready.exists() and 'resume' in sys.argv:\n"
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

    def test_codex_resume_uses_shared_app_server_daemon(self):
        with tempfile.TemporaryDirectory() as directory:
            args = argparse.Namespace(agent="codex", session_id="saved-conversation", cwd=directory,
                                      goal_status="complete", continue_turn=False)
            with patch.object(state, "CODEX_EXE", "/bin/true"), \
                 patch.object(state, "prepare_codex_update", return_value=True), \
                 patch.object(state, "record_managed_launcher"), \
                 patch.object(state, "launch_shell", return_value=0), \
                 patch.object(state.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run:
                state.launch_command(args)
            command = run.call_args.args[0]
            self.assertNotIn("--no-daemon", command)
            self.assertNotIn("check_for_update_on_startup=false", command)


if __name__ == "__main__":
    unittest.main()
