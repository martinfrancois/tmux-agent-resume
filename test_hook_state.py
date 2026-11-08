"""Hook records live in the runtime directory, so a slow disk or a running snapshot cannot stall agent hooks."""

import argparse
import fcntl
import importlib.util
import io
import json
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

SCRIPT = Path(__file__).with_name("state.py")
SESSION = "12345678-1234-1234-1234-123456789abc"
CURRENT_BOOT = "11111111-2222-3333-4444-555555555555"


def load_state(monkeypatch, runtime_root, state_home):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_root))
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    spec = importlib.util.spec_from_file_location("resume_state", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "boot_id", lambda: CURRENT_BOOT)
    monkeypatch.setattr(module, "tmux_server_key", lambda *args: f"/socket,1,{CURRENT_BOOT},9")
    monkeypatch.setenv("TMUX_PANE", "%1")
    monkeypatch.setenv("TMUX", "/socket,1,0")
    return module


@pytest.fixture
def state(tmp_path, monkeypatch):
    runtime_root = tmp_path / "run"
    runtime_root.mkdir()
    return load_state(monkeypatch, runtime_root, tmp_path / "state")


def send_hook(state, event="UserPromptSubmit"):
    payload = json.dumps({"hook_event_name": event, "session_id": SESSION, "source": "startup"})
    with patch.object(state.sys, "stdin", io.StringIO(payload)):
        state.hook_command(argparse.Namespace(agent="claude"))


def pane_entry(state):
    return state.load_hook_state()["servers"][f"/socket,1,{CURRENT_BOOT},9"]["panes"]["%1"]


def test_hook_records_go_to_the_runtime_directory(state, tmp_path):
    send_hook(state)
    assert state.HOOK_STATE_PATH == tmp_path / "run" / "codex-tmux-resume" / "hook-state.json"
    assert state.HOOK_STATE_PATH.exists()
    assert not state.LEGACY_HOOK_STATE_PATH.exists()


def test_missing_runtime_directory_falls_back_to_the_state_directory(tmp_path, monkeypatch):
    state = load_state(monkeypatch, tmp_path / "absent", tmp_path / "state")
    send_hook(state)
    assert state.HOOK_STATE_PATH == tmp_path / "state" / "codex-tmux-resume" / "hook-state.json"
    assert pane_entry(state)["busy"] is True


def test_unwritable_runtime_directory_falls_back_to_the_state_directory(tmp_path, monkeypatch):
    runtime_root = tmp_path / "foreign-run"
    runtime_root.mkdir(mode=0o500)
    state = load_state(monkeypatch, runtime_root, tmp_path / "state")
    send_hook(state)
    assert state.HOOK_STATE_PATH == tmp_path / "state" / "codex-tmux-resume" / "hook-state.json"
    assert pane_entry(state)["busy"] is True


def test_upgrade_without_a_boot_id_keeps_unknown_boot_records(state, monkeypatch):
    monkeypatch.setattr(state, "boot_id", lambda: None)
    key = "/socket,1,unknown-boot,9"
    state.LEGACY_HOOK_STATE_PATH.parent.mkdir(parents=True)
    state.LEGACY_HOOK_STATE_PATH.write_text(json.dumps({"version": 3, "servers": {key: {"panes": {}}}}))
    assert list(state.load_hook_state()["servers"]) == [key]


def test_hook_does_not_wait_for_a_snapshot_holding_the_state_lock(state):
    state.LOCK_PATH.parent.mkdir(parents=True)
    with state.LOCK_PATH.open("a+") as snapshot_lock:
        fcntl.flock(snapshot_lock.fileno(), fcntl.LOCK_EX)
        # A daemon thread keeps a regression from hanging the whole test run.
        thread = threading.Thread(target=send_hook, args=(state,), daemon=True)
        thread.start()
        thread.join(timeout=2)
        assert not thread.is_alive()
    assert pane_entry(state)["busy"] is True


def test_upgrade_carries_over_only_this_boots_records(state):
    current = {"panes": {"%1": {"agent": "claude", "session_id": SESSION, "busy": False}}}
    state.LEGACY_HOOK_STATE_PATH.parent.mkdir(parents=True)
    state.LEGACY_HOOK_STATE_PATH.write_text(json.dumps({"version": 3, "servers": {
        f"/socket,1,{CURRENT_BOOT},9": current,
        "/socket,1,old-boot,9": {"panes": {"%1": {"session_id": "old"}}},
    }}))
    assert state.load_hook_state()["servers"] == {f"/socket,1,{CURRENT_BOOT},9": current}
    send_hook(state)
    assert list(json.loads(state.HOOK_STATE_PATH.read_text())["servers"]) == [f"/socket,1,{CURRENT_BOOT},9"]


def test_snapshot_removes_the_old_file_once_records_moved(state):
    state.LEGACY_HOOK_STATE_PATH.parent.mkdir(parents=True)
    state.LEGACY_HOOK_STATE_PATH.write_text("{}")
    snapshot = {"sessions": []}
    with patch.object(state, "collect_snapshot", return_value=snapshot):
        state.snapshot_command(argparse.Namespace(reason="periodic"))
    assert state.LEGACY_HOOK_STATE_PATH.exists()
    send_hook(state)
    with patch.object(state, "collect_snapshot", return_value=snapshot):
        state.snapshot_command(argparse.Namespace(reason="periodic"))
    assert not state.LEGACY_HOOK_STATE_PATH.exists()
