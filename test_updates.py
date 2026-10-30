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


SETTING_NAMES = ("CODEX_TMUX_RESUME_CODEX_PERMISSION_MODE", "CODEX_TMUX_RESUME_AUTO_CONTINUE")


def load_state_with_settings(**settings):
    """Import a fresh copy of state.py, since it reads both settings at import time."""
    environ = {name: value for name, value in os.environ.items() if name not in SETTING_NAMES}
    environ.update(settings)
    fresh_spec = importlib.util.spec_from_file_location("resume_state_fresh", spec.origin)
    module = importlib.util.module_from_spec(fresh_spec)
    with patch.dict(os.environ, environ, clear=True):
        fresh_spec.loader.exec_module(module)
    return module


class SettingsTests(unittest.TestCase):
    def test_defaults_are_the_safe_choices(self):
        fresh = load_state_with_settings()
        self.assertEqual(fresh.CODEX_PERMISSION_MODE, "inherit")
        self.assertEqual(fresh.AUTO_CONTINUE, "never")

    def test_blank_settings_fall_back_to_the_defaults(self):
        fresh = load_state_with_settings(CODEX_TMUX_RESUME_CODEX_PERMISSION_MODE=" ",
                                         CODEX_TMUX_RESUME_AUTO_CONTINUE="")
        self.assertEqual(fresh.CODEX_PERMISSION_MODE, "inherit")
        self.assertEqual(fresh.AUTO_CONTINUE, "never")

    def test_unknown_setting_stops_the_import(self):
        with self.assertRaises(SystemExit) as raised:
            load_state_with_settings(CODEX_TMUX_RESUME_AUTO_CONTINUE="sometimes")
        self.assertIn("CODEX_TMUX_RESUME_AUTO_CONTINUE must be one of never, interrupted", str(raised.exception))

    def test_values_are_normalized_and_validated(self):
        self.assertEqual(state.parse_choice({"X": " Bypass "}, "X", state.CODEX_PERMISSION_MODES, "inherit"), "bypass")
        with self.assertRaises(SystemExit) as raised:
            state.parse_choice({"X": "yes"}, "X", state.AUTO_CONTINUE_MODES, "never")
        self.assertIn("never, interrupted", str(raised.exception))

    def test_codex_resumes_without_bypass_flag_unless_configured(self):
        args = argparse.Namespace(agent="codex", session_id="saved-conversation", cwd="/tmp",
                                  goal_status="complete", continue_turn=False)
        for mode, expected in (("inherit", False), ("bypass", True)):
            with self.subTest(mode=mode), \
                 patch.object(state, "CODEX_EXE", "/bin/true"), \
                 patch.object(state, "CODEX_PERMISSION_MODE", mode), \
                 patch.object(state, "prepare_codex_update"), \
                 patch.object(state, "record_managed_launcher"), \
                 patch.object(state, "launch_shell", return_value=0), \
                 patch.object(state.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run:
                state.launch_command(args)
            command = run.call_args.args[0]
            self.assertEqual("--dangerously-bypass-approvals-and-sandbox" in command, expected)
            self.assertEqual(command[-2:], ["resume", "saved-conversation"])

    def test_interrupted_turn_continues_only_when_configured(self):
        pane = {"path": "/tmp", "codex": {"session_id": "saved-conversation", "was_running": True}}
        with patch.object(state, "AUTO_CONTINUE", "never"):
            self.assertNotIn("--continue-turn", state.pane_launch_command(pane))
        with patch.object(state, "AUTO_CONTINUE", "interrupted"):
            self.assertIn("--continue-turn", state.pane_launch_command(pane))

    def test_launch_environment_carries_both_settings(self):
        pane = {"path": "/tmp", "claude": {"session_id": "saved-conversation", "was_running": False}}
        with patch.object(state, "AUTO_CONTINUE", "interrupted"), \
             patch.object(state, "CODEX_PERMISSION_MODE", "bypass"):
            command = state.pane_launch_command(pane)
        self.assertIn("CODEX_TMUX_RESUME_AUTO_CONTINUE=interrupted", command)
        self.assertIn("CODEX_TMUX_RESUME_CODEX_PERMISSION_MODE=bypass", command)


class ManagedLauncherTests(unittest.TestCase):
    session_id = "12345678-1234-1234-1234-123456789abc"

    def setUp(self):
        self.entry = {
            "agent": "codex", "managed_launcher": True, "session_id": self.session_id,
            "busy": True, "launcher_pid": 123, "launcher_start_ticks": 456,
        }
        self.hooks = {"servers": {"server": {"panes": {"%1": self.entry}}}}
        self.pane = {"pane_id": "%1", "pane_pid": "100", "pane_title": "",
                     "pane_current_command": "pypy3"}
        self.arguments = ["/synthetic/pypy3", str(Path(state.__file__).resolve()),
                          "launch", "--agent", "codex", "--session-id", self.session_id]
        self.statuses = {"123": (100, 123, 123, 456), "100": (1, 100, 123, 10)}
        for name, kwargs in (
            ("process_stat", {"side_effect": lambda path: self.statuses.get(path.name)}),
            ("process_arguments", {"side_effect": lambda path: self.arguments}),
            ("owns_foreground_job", {"return_value": True}),
            ("codex_session_from_pane_title", {"return_value": (None, None)}),
            ("codex_goal_status", {"return_value": None}),
            ("pane_screen_is_busy", {"return_value": False}),
        ):
            patcher = patch.object(state, name, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)

    def metadata(self, processes=None):
        return state.agent_metadata("codex", self.pane, self.hooks, "server", processes or {})

    def test_waiting_launcher_identity_is_independent_of_interpreter_name(self):
        for command in ("python3", "pypy3", "custom-python"):
            with self.subTest(command=command):
                self.pane["pane_current_command"] = command
                self.arguments[0] = f"/synthetic/{command}"
                metadata = self.metadata()
                self.assertIsNotNone(metadata)
                self.assertEqual(metadata["session_id"], self.session_id)
                self.assertTrue(metadata["was_running"])

    def test_replaced_process_and_returned_shell_reject_stale_hook(self):
        self.pane["pane_current_command"] = "python3"
        for arguments in (["/bin/bash", "-l"], ["python3", "unrelated.py"]):
            with self.subTest(arguments=arguments):
                self.arguments = arguments
                self.assertIsNone(self.metadata())

    def test_reused_pid_or_different_pane_rejects_stale_hook(self):
        self.pane["pane_current_command"] = "python3"
        for status in ((100, 123, 123, 999), (999, 123, 123, 456), None):
            with self.subTest(status=status):
                self.statuses["123"] = status
                self.assertIsNone(self.metadata())

    def test_unverified_legacy_hook_is_not_a_launcher_identity(self):
        self.pane["pane_current_command"] = "python3"
        del self.entry["launcher_pid"]
        self.assertIsNone(self.metadata())

    def test_background_launcher_does_not_claim_foreground_pane(self):
        self.pane["pane_current_command"] = "python3"
        with patch.object(state, "owns_foreground_job", return_value=False):
            self.assertIsNone(self.metadata())

    def test_recorded_launcher_keeps_process_identity(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.dict(os.environ, {"TMUX_PANE": "%1", "TMUX": "/synthetic/socket,100,0"}), \
             patch.object(state, "tmux_server_key", return_value="server"), \
             patch.object(state.os, "getpid", return_value=123), \
             patch.object(state, "LOCK_PATH", Path(directory) / "lock"), \
             patch.object(state, "HOOK_STATE_PATH", Path(directory) / "hooks.json"):
            state.record_managed_launcher("codex", self.session_id, directory, True, [])
            entry = json.loads((Path(directory) / "hooks.json").read_text())["servers"]["server"]["panes"]["%1"]
        self.assertEqual(entry["launcher_pid"], 123)
        self.assertEqual(entry["launcher_start_ticks"], 456)

    def test_live_agent_child_takes_precedence(self):
        other = "87654321-4321-4321-4321-cba987654321"
        with patch.object(state, "root_codex_session_from_process", return_value=(other, "/other.jsonl")):
            metadata = self.metadata({("server", "%1"): [Path("/proc/124")]})
        self.assertEqual(metadata["session_id"], other)
        self.assertFalse(metadata["was_running"])

    def test_verified_launcher_retains_identity_with_shared_daemon_only(self):
        with patch.object(state, "root_codex_session_from_process", return_value=(None, None)):
            metadata = self.metadata({("server", "%1"): [Path("/proc/124")]})
        self.assertEqual(metadata["session_id"], self.session_id)


class UpdateTests(unittest.TestCase):
    def test_title_lookup_does_not_match_fixed_or_partial_segments(self):
        wrong = "12345678-1234-1234-1234-123456789abc"
        right = "87654321-4321-4321-4321-cba987654321"
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "state.sqlite"
            with sqlite3.connect(database) as connection:
                connection.execute(
                    "CREATE TABLE threads (id TEXT, name TEXT, rollout_path TEXT, archived INTEGER)"
                )
                connection.execute("INSERT INTO threads VALUES (?, ?, ?, 0)",
                                   (wrong, "codex", "/wrong.jsonl"))
            with patch.object(state, "CODEX_STATE_DB_PATH", database):
                self.assertEqual(state.codex_session_from_pane_title("Actual work | codex"),
                                 (None, None))
                with sqlite3.connect(database) as connection:
                    connection.execute("INSERT INTO threads VALUES (?, ?, ?, 0)",
                                       (right, "Actual work", "/right.jsonl"))
                for title in ("Actual work | codex", "⠋ Actual work | codex"):
                    with self.subTest(title=title):
                        self.assertEqual(state.codex_session_from_pane_title(title),
                                         (right, "/right.jsonl"))
                self.assertEqual(state.codex_session_from_pane_title("codex | codex"),
                                 (wrong, "/wrong.jsonl"))
                for title in ("folder | Actual work | codex", "Actual work | other app"):
                    with self.subTest(title=title):
                        self.assertEqual(state.codex_session_from_pane_title(title),
                                         (None, None))
                with sqlite3.connect(database) as connection:
                    connection.execute("INSERT INTO threads VALUES (?, ?, ?, 1)",
                                       (wrong, "Actual work", "/archived.jsonl"))
                self.assertEqual(state.codex_session_from_pane_title("Actual work | codex"),
                                 (right, "/right.jsonl"))

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

    def test_claude_model_is_preserved_in_split_and_inline_forms(self):
        for arguments in (["--model", "sonnet"], ["--model=sonnet"]):
            with self.subTest(arguments=arguments):
                self.assertEqual(
                    state.launch_options_from_arguments("claude", arguments),
                    ["--model", "sonnet"],
                )
        self.assertEqual(
            state.launch_options_from_arguments("claude", ["--model", "bad value"]), []
        )

    def test_codex_launch_options_accept_inline_and_short_forms(self):
        arguments = [
            "--model=openrouter/gpt-6-sol", "-p", "work", "--config", "model_provider=openai",
            "--enable=cli_daybreak", "-c=model_provider=\"oss\"", "-mllama3:8b", "--yolo",
        ]
        self.assertEqual(state.launch_options_from_arguments("codex", arguments), [
            "--model", "openrouter/gpt-6-sol", "-p", "work", "--config", "model_provider=openai",
            "--enable", "cli_daybreak", "-c", "model_provider=\"oss\"", "-m", "llama3:8b", "--yolo",
        ])

    def test_codex_launch_options_drop_other_values_and_positionals(self):
        arguments = [
            "-c", "check_for_update_on_startup=false", "--config=model=\"o3\"",
            "--profile=../escape", "-m", "has space", "resume", "saved-conversation",
            "--", "--yolo",
        ]
        self.assertEqual(state.launch_options_from_arguments("codex", arguments), [])

    def test_codex_state_problems_report_renamed_or_changed_database(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            database = home / "state_5.sqlite"
            with patch.object(state, "CODEX_HOME", home), \
                 patch.object(state, "CODEX_STATE_DB_PATH", database):
                (home / "state_6.sqlite").touch()
                problems = state.codex_state_problems()
                self.assertEqual(len(problems), 1)
                self.assertIn("state_6.sqlite", problems[0])

                connection = sqlite3.connect(database)
                try:
                    connection.execute("CREATE TABLE threads (id TEXT, rollout_path TEXT, archived INTEGER)")
                    connection.commit()
                finally:
                    connection.close()
                problems = state.codex_state_problems()
                self.assertEqual(len(problems), 1)
                self.assertIn("name", problems[0])

                connection = sqlite3.connect(database)
                try:
                    connection.execute("ALTER TABLE threads ADD COLUMN name TEXT")
                    connection.commit()
                finally:
                    connection.close()
                self.assertEqual(state.codex_state_problems(), [])

    def test_snapshot_warns_about_codex_state_only_with_codex_panes(self):
        codex_snapshot = {"sessions": [{"windows": [{"panes": [{"codex": {"session_id": None}}]}]}]}
        shell_snapshot = {"sessions": [{"windows": [{"panes": [{"codex": None, "claude": None}]}]}]}
        with patch.object(state, "codex_state_problems", return_value=["state database is missing"]), \
             patch("builtins.print") as output:
            state.warn_about_codex_state(shell_snapshot)
            self.assertFalse(output.called)
            state.warn_about_codex_state(codex_snapshot)
        self.assertIn("state database is missing", output.call_args.args[0])

    def test_status_fails_when_codex_state_database_is_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            with patch.object(state, "CODEX_HOME", home), \
                 patch.object(state, "CODEX_STATE_DB_PATH", home / "state_5.sqlite"), \
                 patch.object(state, "SNAPSHOT_PATH", home / "snapshot.json"), \
                 patch.object(state, "RESTORE_MARKER_PATH", home / "restored-boot.json"), \
                 patch("builtins.print") as output:
                self.assertEqual(state.status_command(argparse.Namespace()), 1)
            self.assertTrue(any(
                "state_5.sqlite is missing" in str(call.args[0]) for call in output.call_args_list
            ))

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
