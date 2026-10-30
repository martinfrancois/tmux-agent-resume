"""Claude bypass state survives hooks, snapshots, and managed resumes."""

import argparse
import importlib.util
import io
import json
import shlex
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

spec = importlib.util.spec_from_file_location("resume_state", Path(__file__).with_name("state.py"))
state = importlib.util.module_from_spec(spec)
spec.loader.exec_module(state)

SESSION = "12345678-1234-1234-1234-123456789abc"
OTHER_SESSION = "abcdef12-1234-1234-1234-123456789abc"


@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "STATE_DIR", tmp_path)
    monkeypatch.setattr(state, "LOCK_PATH", tmp_path / "lock")
    monkeypatch.setattr(state, "HOOK_STATE_PATH", tmp_path / "hooks.json")
    monkeypatch.setattr(state, "tmux_server_key", lambda *args: "server")
    monkeypatch.setenv("TMUX_PANE", "%1")
    monkeypatch.setenv("TMUX", "/synthetic/socket,123,0")
    monkeypatch.setattr(state, "owns_foreground_job", lambda process: True)
    monkeypatch.setattr(state, "pane_screen_is_busy", lambda pane: False)
    monkeypatch.setattr(state, "AUTO_CONTINUE", "never")
    return tmp_path


def send_hook(event="SessionStart", session=SESSION, **fields):
    payload = {"hook_event_name": event, "session_id": session, "source": "startup", **fields}
    with patch.object(state.sys, "stdin", io.StringIO(json.dumps(payload))):
        state.hook_command(argparse.Namespace(agent="claude"))


def snapshot_and_launch(processes=()):
    metadata = state.agent_metadata(
        "claude", {"pane_id": "%1", "pane_current_command": "claude"},
        state.load_hook_state(), "server", {("server", "%1"): list(processes)},
    )
    command = shlex.split(state.pane_launch_command({"path": "/tmp", "claude": metadata}))
    args = state.parser().parse_args(command[command.index("launch"):])
    with patch.object(state, "CLAUDE_EXE", "/bin/true"), \
         patch.object(state, "launch_shell", return_value=0), \
         patch.object(state.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run:
        state.launch_command(args)
    return run.call_args.args[0]


def process_fixture(root, arguments, name="123"):
    process = root / name
    process.mkdir()
    (process / "cmdline").write_bytes(b"\0".join(arg.encode() for arg in ["claude", *arguments]) + b"\0")
    return process


@pytest.mark.parametrize("options", [
    ["--dangerously-skip-permissions"],
    ["--permission-mode", "bypassPermissions"],
    ["--permission-mode=bypassPermissions"],
])
def test_new_session_without_open_transcript_keeps_bypass(isolated_state, options):
    process = process_fixture(isolated_state, [*options, "--model", "sonnet"])
    send_hook()
    command = snapshot_and_launch([process])
    assert "--dangerously-skip-permissions" in command or "bypassPermissions" in command
    assert command[-4:] == ["--model", "sonnet", "--resume", SESSION]


def test_hook_bypass_is_saved_without_readable_procfs(isolated_state):
    send_hook()
    send_hook("UserPromptSubmit", permission_mode="bypassPermissions")
    send_hook("Stop", permission_mode="bypassPermissions")
    assert snapshot_and_launch() == ["/bin/true", "--dangerously-skip-permissions", "--resume", SESSION]


@pytest.mark.parametrize("mode", ["auto", "default", "manual", "plan", "acceptEdits", "dontAsk"])
@pytest.mark.parametrize("options", [["--dangerously-skip-permissions"], ["--permission-mode=bypassPermissions"]])
def test_hook_leaving_bypass_overrides_launch_flag(isolated_state, mode, options):
    process = process_fixture(isolated_state, [*options, "--resume", SESSION])
    send_hook("UserPromptSubmit", permission_mode="bypassPermissions")
    send_hook("Stop", permission_mode=mode)
    assert snapshot_and_launch([process]) == ["/bin/true", "--permission-mode", mode, "--resume", SESSION]


def test_reused_pane_does_not_inherit_previous_session_bypass(isolated_state):
    send_hook("UserPromptSubmit", permission_mode="bypassPermissions")
    send_hook(session=OTHER_SESSION)
    assert snapshot_and_launch() == ["/bin/true", "--resume", OTHER_SESSION]


def test_stale_hook_does_not_override_identified_process(isolated_state):
    send_hook("UserPromptSubmit", permission_mode="bypassPermissions")
    process = process_fixture(isolated_state, ["--resume", OTHER_SESSION])
    assert snapshot_and_launch([process]) == ["/bin/true", "--resume", OTHER_SESSION]


def test_no_bypass_evidence_keeps_default_permissions(isolated_state):
    send_hook()
    assert snapshot_and_launch() == ["/bin/true", "--resume", SESSION]


def test_bypass_survives_a_second_restore_before_next_prompt(isolated_state):
    send_hook("UserPromptSubmit", permission_mode="bypassPermissions")
    first = snapshot_and_launch()
    send_hook(source="resume")
    assert snapshot_and_launch() == first
    assert "--dangerously-skip-permissions" in first


def test_new_manual_resume_clears_old_mode_for_same_session(isolated_state):
    send_hook("UserPromptSubmit", permission_mode="bypassPermissions")
    send_hook(source="resume")
    assert snapshot_and_launch() == ["/bin/true", "--resume", SESSION]


def test_compaction_preserves_mode_when_event_omits_it(isolated_state):
    send_hook("UserPromptSubmit", permission_mode="bypassPermissions")
    send_hook(source="compact")
    assert "--dangerously-skip-permissions" in snapshot_and_launch()


@pytest.mark.parametrize("mode", [None, [], "futureMode", "bypassPermissions --model injected"])
def test_unknown_mode_is_not_replayed(isolated_state, mode):
    send_hook("UserPromptSubmit", permission_mode=mode)
    assert snapshot_and_launch() == ["/bin/true", "--resume", SESSION]


def test_two_unidentified_foreground_processes_do_not_guess_options(isolated_state):
    send_hook()
    processes = [
        process_fixture(isolated_state, ["--dangerously-skip-permissions"], "123"),
        process_fixture(isolated_state, [], "124"),
    ]
    assert snapshot_and_launch(processes) == ["/bin/true", "--resume", SESSION]


def test_other_pane_or_server_cannot_supply_bypass(isolated_state, monkeypatch):
    send_hook("UserPromptSubmit", permission_mode="bypassPermissions")
    monkeypatch.setenv("TMUX_PANE", "%2")
    send_hook(session=OTHER_SESSION)
    hooks = state.load_hook_state()
    pane = {"pane_id": "%2", "pane_current_command": "claude"}
    assert state.agent_metadata("claude", pane, hooks, "server", {})["launch_options"] == []
    pane["pane_id"] = "%1"
    assert state.agent_metadata("claude", pane, hooks, "other-server", {})["session_id"] is None


def test_other_agent_does_not_inherit_claude_permissions(isolated_state):
    send_hook("UserPromptSubmit", permission_mode="bypassPermissions")
    payload = {"hook_event_name": "SessionStart", "session_id": SESSION, "source": "startup"}
    with patch.object(state.sys, "stdin", io.StringIO(json.dumps(payload))):
        state.hook_command(argparse.Namespace(agent="codex"))
    assert "permission_mode" not in state.load_hook_state()["servers"]["server"]["panes"]["%1"]
