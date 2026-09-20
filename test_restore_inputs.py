"""Tests for the parts of state.py that read files and settings. None of them need tmux."""

import argparse
import importlib.util
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("resume_state", Path(__file__).with_name("state.py"))
state = importlib.util.module_from_spec(spec)
spec.loader.exec_module(state)

EXAMPLES = Path(__file__).with_name("examples")
SNAPSHOT = json.loads((EXAMPLES / "snapshot.json").read_text())
CODEX_SESSION = EXAMPLES / "sessions" / "rollout-2026-09-01T09-58-00-0f1e2d3c-4b5a-4697-8877-665544332211.jsonl"
CODEX_ID = "0f1e2d3c-4b5a-4697-8877-665544332211"
CLAUDE_ID = "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d"


def state_dir_with_snapshot(root: Path, snapshot: dict) -> dict:
    state_dir = root / "codex-tmux-resume"
    state.atomic_write_json(state_dir / "snapshot.json", snapshot)
    return {
        "STATE_DIR": state_dir,
        "SNAPSHOT_PATH": state_dir / "snapshot.json",
        "RESTORE_MARKER_PATH": state_dir / "restored-boot.json",
        "LOCK_PATH": state_dir / "state.lock",
        "RESTORE_LOCK_PATH": state_dir / "restore.lock",
    }


def captured(function, *args) -> str:
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        function(*args)
    return buffer.getvalue()


class SessionFileTests(unittest.TestCase):
    def test_codex_session_id_comes_from_the_session_meta_line(self):
        self.assertEqual(state.codex_session_id_from_file(CODEX_SESSION), CODEX_ID)

    def test_codex_session_file_must_be_an_interactive_cli_session(self):
        with tempfile.TemporaryDirectory() as directory:
            sessions = Path(directory) / "sessions"
            sessions.mkdir()
            meta = json.loads(CODEX_SESSION.read_text().splitlines()[0])
            cases = {
                "exec.jsonl": {**meta, "payload": {**meta["payload"], "source": "exec"}},
                "vscode.jsonl": {**meta, "payload": {**meta["payload"], "originator": "codex_vscode"}},
                "no-id.jsonl": {**meta, "payload": {**meta["payload"], "id": "not-a-uuid"}},
                "not-meta.jsonl": {**meta, "type": "event_msg"},
                "list.jsonl": [1, 2],
            }
            for name, first_line in cases.items():
                path = sessions / name
                path.write_text(json.dumps(first_line) + "\n")
                self.assertIsNone(state.codex_session_id_from_file(path), name)
            (sessions / "broken.jsonl").write_text("{not json\n")
            self.assertIsNone(state.codex_session_id_from_file(sessions / "broken.jsonl"))
            (sessions / "empty.jsonl").write_text("")
            self.assertIsNone(state.codex_session_id_from_file(sessions / "empty.jsonl"))
            outside = Path(directory) / "elsewhere.jsonl"
            outside.write_text(CODEX_SESSION.read_text())
            self.assertIsNone(state.codex_session_id_from_file(outside))

    def test_claude_session_id_comes_from_the_transcript_name(self):
        good = Path("/home/demo/.claude/projects/-home-demo-project-b") / f"{CLAUDE_ID}.jsonl"
        self.assertEqual(state.claude_session_id_from_path(good), CLAUDE_ID)
        self.assertIsNone(state.claude_session_id_from_path(Path("/home/demo/.claude/todos") / f"{CLAUDE_ID}.jsonl"))
        self.assertIsNone(state.claude_session_id_from_path(Path("/home/demo/.claude/projects/x/notes.jsonl")))
        self.assertIsNone(state.claude_session_id_from_path(Path("/home/demo/.claude/projects/x") / f"{CLAUDE_ID}.json"))

    def test_session_id_from_arguments(self):
        with tempfile.TemporaryDirectory() as directory:
            process = Path(directory)
            (process / "cmdline").write_bytes(b"claude\0--resume\0" + CLAUDE_ID.encode() + b"\0")
            self.assertEqual(state.session_id_from_arguments(process), CLAUDE_ID)
            (process / "cmdline").write_bytes(b"claude\0-r\0latest\0")
            self.assertIsNone(state.session_id_from_arguments(process))
            (process / "cmdline").write_bytes(b"codex\0--session-id=" + CODEX_ID.encode() + b"\0")
            self.assertEqual(state.session_id_from_arguments(process), CODEX_ID)


class SnapshotTests(unittest.TestCase):
    def test_status_summarizes_the_example_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = state_dir_with_snapshot(Path(directory), SNAPSHOT)
            with patch.multiple(state, **paths):
                output = captured(state.status_command, argparse.Namespace())
        self.assertIn("contents: 2 sessions, 4 panes, 1 Codex panes, 1 Claude panes, 1 active turns, 0 active goals", output)
        self.assertIn("captured: 2026-09-01T10:00:00+00:00 (periodic)", output)

    def test_status_without_a_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.multiple(state, SNAPSHOT_PATH=root / "missing.json", RESTORE_MARKER_PATH=root / "missing2.json"):
                output = captured(state.status_command, argparse.Namespace())
        self.assertIn("captured: never", output)

    def test_dry_run_lists_sessions_and_never_calls_tmux(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = state_dir_with_snapshot(Path(directory), SNAPSHOT)
            with patch.multiple(state, **paths), \
                 patch.object(state, "existing_sessions", return_value={"notes": "$1"}), \
                 patch.object(state, "tmux", side_effect=AssertionError("tmux must not run in a dry run")):
                output = captured(state.restore_command, argparse.Namespace(dry_run=True))
            self.assertFalse((paths["STATE_DIR"] / "recovery").exists())
            self.assertFalse(paths["RESTORE_MARKER_PATH"].exists())
        self.assertIn("restore tmux session 'work' (2 windows)", output)
        self.assertNotIn("'notes'", output)
        self.assertIn("Restored 1 sessions; skipped 1 existing sessions.", output)

    def test_malformed_snapshot_is_treated_as_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot.json"
            path.write_text("{oops")
            self.assertEqual(state.read_json(path, {}), {})
            with patch.multiple(state, SNAPSHOT_PATH=path, STATE_DIR=Path(directory),
                                LOCK_PATH=Path(directory) / "a.lock", RESTORE_LOCK_PATH=Path(directory) / "b.lock"):
                output = captured(state.restore_command, argparse.Namespace(dry_run=True))
        self.assertIn("No saved tmux snapshot to restore.", output)

    def test_pane_launch_command_for_example_panes(self):
        codex_pane, claude_pane = SNAPSHOT["sessions"][1]["windows"][0]["panes"]
        shell_pane = SNAPSHOT["sessions"][0]["windows"][0]["panes"][0]
        with patch.object(state, "AUTO_CONTINUE", "never"):
            codex_command = state.pane_launch_command(codex_pane)
            claude_command = state.pane_launch_command(claude_pane)
        self.assertIn(f"launch --agent codex --session-id {CODEX_ID} --cwd /home/demo/project-a", codex_command)
        self.assertNotIn("--continue-turn", codex_command)
        self.assertIn(f"launch --agent claude --session-id {CLAUDE_ID} --cwd /home/demo/project-b", claude_command)
        self.assertIsNone(state.pane_launch_command(shell_pane))

    def test_layout_pane_ids_follow_the_saved_layout_string(self):
        layout = SNAPSHOT["sessions"][1]["windows"][0]["layout"]
        self.assertEqual(state.layout_pane_ids(layout), ["%1", "%2"])
        self.assertEqual(state.layout_pane_ids("9a1b,200x50,0,0,4"), ["%4"])
        self.assertEqual(state.layout_pane_ids("garbage"), [])

    def test_parse_rows_keeps_only_complete_rows(self):
        output = "a\x1fb\nc\nd\x1fe\n"
        self.assertEqual(state.parse_rows(output, ["x", "y"], "\x1f"), [{"x": "a", "y": "b"}, {"x": "d", "y": "e"}])


class HookConfigurationTests(unittest.TestCase):
    def test_configure_hooks_keeps_foreign_handlers_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Path(directory) / "settings.json"
            settings.write_text(json.dumps({
                "model": "example",
                "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "notify-send done"}]}]},
            }))
            self.assertTrue(state.configure_agent_hooks(settings, "claude"))
            document = json.loads(settings.read_text())
            self.assertEqual(document["model"], "example")
            stop_commands = [hook["command"] for group in document["hooks"]["Stop"] for hook in group["hooks"]]
            self.assertEqual(stop_commands[0], "notify-send done")
            self.assertIn("CODEX_TMUX_RESUME_HOOK=1", stop_commands[1])
            self.assertIn("hook --agent claude", stop_commands[1])
            self.assertEqual(document["hooks"]["SessionStart"][0]["matcher"], "startup|resume|clear|compact")
            self.assertIn("UserPromptSubmit", document["hooks"])
            self.assertFalse(state.configure_agent_hooks(settings, "claude"))
            self.assertEqual(json.loads(settings.read_text()), document)

    def test_remove_managed_handlers_leaves_the_rest(self):
        document = {"hooks": {
            "Stop": [{"hooks": [{"type": "command", "command": "notify-send done"},
                                {"type": "command", "command": "CODEX_TMUX_RESUME_HOOK=1 python3 state.py hook --agent codex"}]}],
            "SessionStart": [{"matcher": "startup", "hooks": [{"type": "command", "command": "CODEX_TMUX_RESUME_HOOK=1 x"}]}],
        }}
        self.assertTrue(state.remove_managed_handlers(document))
        self.assertEqual(document, {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "notify-send done"}]}]}})
        self.assertFalse(state.remove_managed_handlers(document))

    def test_malformed_settings_are_not_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Path(directory) / "settings.json"
            settings.write_text("[]")
            with self.assertRaises(SystemExit):
                state.configure_agent_hooks(settings, "codex")
            self.assertEqual(settings.read_text(), "[]")


if __name__ == "__main__":
    unittest.main()
