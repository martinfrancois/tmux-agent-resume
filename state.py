#!/usr/bin/env python3
"""Persist and restore tmux topology plus Codex and Claude sessions."""

from __future__ import annotations

import argparse
import copy
import fcntl
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


APP = "codex-tmux-resume"
HOME = Path.home()
CODEX_HOME = Path(os.environ.get("CODEX_HOME", HOME / ".codex")).expanduser()
CLAUDE_HOME = Path(os.environ.get("CLAUDE_CONFIG_DIR", HOME / ".claude")).expanduser()
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", HOME / ".local/state")) / APP
SNAPSHOT_PATH = STATE_DIR / "snapshot.json"
HOOK_STATE_PATH = STATE_DIR / "hook-state.json"
RESTORE_MARKER_PATH = STATE_DIR / "restored-boot.json"
LOCK_PATH = STATE_DIR / "state.lock"
RESTORE_LOCK_PATH = STATE_DIR / "restore.lock"
CODEX_HOOKS_PATH = CODEX_HOME / "hooks.json"
CLAUDE_SETTINGS_PATH = CLAUDE_HOME / "settings.json"
GOALS_DB_PATH = CODEX_HOME / "goals_1.sqlite"
CODEX_STATE_DB_PATH = CODEX_HOME / "state_5.sqlite"
TMUX_EXE = os.environ.get("CODEX_TMUX_RESUME_TMUX_BIN") or shutil.which("tmux") or "tmux"
CODEX_EXE = os.environ.get("CODEX_TMUX_RESUME_CODEX_BIN") or shutil.which("codex")
CLAUDE_EXE = os.environ.get("CODEX_TMUX_RESUME_CLAUDE_BIN") or shutil.which("claude")
ENV_EXE = shutil.which("env") or "/usr/bin/env"
TMUX_SOCKET = os.environ.get("CODEX_TMUX_RESUME_TMUX_SOCKET") or None
UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F-]{27,}$")
ROOT_CODEX_SOURCES = {"cli", "vscode"}
CODEX_DEFAULT_BYPASS_FLAG = "--dangerously-bypass-approvals-and-sandbox"
CODEX_BYPASS_FLAGS = ("--yolo", CODEX_DEFAULT_BYPASS_FLAG)
# Codex prefixes the thread name with a spinner or status glyph of a few
# characters; a longer prefix is other title text.
MAX_TITLE_ACTIVITY_WIDTH = 4
CODEX_TITLE_SEPARATOR = " | "
SQLITE_TIMEOUT_SECONDS = 0.2
CODEX_THREAD_NAMES_QUERY = (
    "SELECT id, rollout_path, name FROM threads "
    "WHERE archived = 0 AND name IS NOT NULL AND name != ''"
)
BUSY_RE = re.compile(r"(?:Working.*?)?esc to interrupt", re.IGNORECASE | re.DOTALL)
MANAGED_COMMAND_MARKER = "CODEX_TMUX_RESUME_HOOK=1"
LEGACY_MANAGED_SCRIPT_FRAGMENT = "/.local/libexec/codex-tmux-resume/state.py"
CONTINUE_PROMPT = os.environ.get("CODEX_TMUX_RESUME_PROMPT", "continue")
GOAL_STATUSES = {
    "active", "paused", "blocked", "usage_limited", "budget_limited", "complete",
}
GOAL_PROMPT_STATUSES = {"paused", "blocked", "usage_limited"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def boot_id() -> str | None:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        pass
    runtime_root = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
    marker = runtime_root / APP / "boot-id"
    try:
        marker.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        value = f"runtime-{uuid.uuid4()}"
        try:
            descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return marker.read_text(encoding="utf-8").strip() or None
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(value)
        return value
    except OSError:
        return None


@contextmanager
def locked():
    STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    with LOCK_PATH.open("a+", encoding="utf-8") as handle:
        os.chmod(LOCK_PATH, 0o600)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield


@contextmanager
def restore_locked():
    STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    with RESTORE_LOCK_PATH.open("a+", encoding="utf-8") as handle:
        os.chmod(RESTORE_LOCK_PATH, 0o600)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return copy.deepcopy(default)


def atomic_write_json(path: Path, value: Any, mode: int = 0o600) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def ensure_tmux_socket_parent() -> None:
    if not TMUX_SOCKET:
        return
    Path(TMUX_SOCKET).expanduser().parent.mkdir(mode=0o700, parents=True, exist_ok=True)


def tmux(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    ensure_tmux_socket_parent()
    return subprocess.run(
        [*tmux_command(), *args],
        check=check,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def tmux_command() -> list[str]:
    command = [TMUX_EXE]
    if TMUX_SOCKET:
        command.extend(["-S", TMUX_SOCKET])
    return command


def parse_rows(output: str, fields: list[str], separator: str) -> list[dict[str, str]]:
    parsed: list[dict[str, str]] = []
    for line in output.splitlines():
        values = line.split(separator)
        if len(values) == len(fields):
            parsed.append(dict(zip(fields, values)))
    return parsed


def query_rows(
    args: list[str], fields: list[str], *, check: bool = True
) -> tuple[int, list[dict[str, str]]]:
    separator = "\x1f"
    fmt = separator.join(f"#{{{field}}}" for field in fields)
    result = tmux([*args, "-F", fmt], check=check)
    if result.returncode != 0:
        return result.returncode, []
    return result.returncode, parse_rows(result.stdout, fields, separator)


def rows(args: list[str], fields: list[str], *, check: bool = True) -> list[dict[str, str]]:
    return query_rows(args, fields, check=check)[1]


def load_hook_state() -> dict[str, Any]:
    state = read_json(HOOK_STATE_PATH, {"version": 3, "servers": {}})
    if not isinstance(state, dict) or not isinstance(state.get("servers"), dict):
        return {"version": 3, "servers": {}}
    return state


def tmux_server_key(value: str | None, server_pid: str | None = None) -> str | None:
    if not value:
        return None
    socket_path = value
    if server_pid is None:
        parts = value.rsplit(",", 2)
        if len(parts) == 3:
            socket_path, server_pid, _client_id = parts
    try:
        socket_path = str(Path(socket_path).resolve())
    except OSError:
        pass
    identity = [socket_path, server_pid or "unknown-pid", boot_id() or "unknown-boot"]
    if server_pid and server_pid.isdigit():
        status = process_stat(Path("/proc") / server_pid)
        identity.append(str(status[3]) if status is not None else "unknown-start")
    else:
        identity.append("unknown-start")
    return ",".join(identity)


def proc_agent_index(agent: str) -> dict[tuple[str, str], list[Path]]:
    matches: dict[tuple[str, str], list[Path]] = {}
    server_keys: dict[str, str | None] = {}
    proc = Path("/proc")
    try:
        candidates = list(proc.iterdir())
    except OSError:
        return matches
    for candidate in candidates:
        if not candidate.name.isdigit():
            continue
        try:
            comm = (candidate / "comm").read_text().strip()
            if comm != agent:
                continue
            environment_items = (candidate / "environ").read_bytes().split(b"\0")
            environment = {}
            for item in environment_items:
                key, separator, value = item.partition(b"=")
                if separator:
                    environment[key.decode(errors="ignore")] = value.decode(errors="ignore")
            pane_id = environment.get("TMUX_PANE")
            tmux_value = environment.get("TMUX")
            if pane_id and tmux_value:
                if tmux_value not in server_keys:
                    server_keys[tmux_value] = tmux_server_key(tmux_value)
                server_key = server_keys[tmux_value]
                if server_key:
                    matches.setdefault((server_key, pane_id), []).append(candidate)
        except (OSError, UnicodeError):
            continue
    for processes in matches.values():
        processes.sort(key=lambda path: int(path.name))
    return matches


def process_stat(process_dir: Path) -> tuple[int, int, int, int] | None:
    """Return (parent PID, process group, foreground group, start ticks)."""
    try:
        value = (process_dir / "stat").read_text(encoding="utf-8")
        fields = value[value.rindex(")") + 2:].split()
        return int(fields[1]), int(fields[2]), int(fields[5]), int(fields[19])
    except (OSError, ValueError, IndexError):
        return None


def owns_foreground_job(process_dir: Path) -> bool:
    """Whether this process or one of its children owns its tty."""
    status = process_stat(process_dir)
    if status is None:
        return False
    _parent, process_group, foreground_group, _started = status
    if foreground_group <= 0:
        return False
    if process_group == foreground_group:
        return True

    pending = [process_dir]
    visited: set[str] = set()
    while pending:
        current = pending.pop()
        if current.name in visited:
            continue
        visited.add(current.name)
        try:
            child_ids = (current / "task" / current.name / "children").read_text().split()
        except OSError:
            continue
        for child_id in child_ids:
            child = Path("/proc") / child_id
            child_status = process_stat(child)
            if child_status is not None and child_status[1] == foreground_group:
                return True
            pending.append(child)
    return False


def process_arguments(process_dir: Path) -> list[str]:
    try:
        return [
            item.decode(errors="ignore")
            for item in (process_dir / "cmdline").read_bytes().split(b"\0")
            if item
        ]
    except OSError:
        return []


def is_codex_app_server(process_dir: Path) -> bool:
    return "app-server" in process_arguments(process_dir)[1:]


def root_codex_session_from_process(process_dir: Path) -> tuple[str | None, str | None]:
    if is_codex_app_server(process_dir):
        # The shared daemon inherits one client's TMUX_PANE while serving many
        # clients. Its open transcripts cannot identify the pane that owns a
        # session.
        return None, None
    candidates: list[tuple[str, str]] = []
    fd_dir = process_dir / "fd"
    try:
        descriptors = list(fd_dir.iterdir())
    except OSError:
        return None, None
    for descriptor in descriptors:
        try:
            target = Path(os.readlink(descriptor))
        except OSError:
            continue
        target_text = str(target)
        if not target_text.endswith(".jsonl") or "sessions" not in target.parts:
            continue
        try:
            with target.open("r", encoding="utf-8") as handle:
                metadata = json.loads(handle.readline())
        except (OSError, json.JSONDecodeError, UnicodeError):
            continue
        payload = metadata.get("payload", {})
        session_id = payload.get("id")
        if (
            metadata.get("type") == "session_meta"
            and payload.get("originator") in {"codex-tui", "codex_cli_rs"}
            and isinstance(payload.get("source"), str)
            and payload["source"] in ROOT_CODEX_SOURCES
            and isinstance(session_id, str)
            and UUID_RE.match(session_id)
        ):
            candidates.append((session_id, target_text))
    if not candidates:
        return None, None
    candidates.sort()
    return candidates[0]


def launch_options_from_arguments(agent: str, arguments: list[str]) -> list[str]:
    """Keep supported user options without saving prompts or credentials."""
    options: list[str] = []
    index = 0
    while index < len(arguments):
        arg = arguments[index]
        next_arg = arguments[index + 1] if index + 1 < len(arguments) else None
        if agent == "codex":
            if arg in ("-m", "--model", "--profile") and next_arg and re.fullmatch(r"[A-Za-z0-9_.-]+", next_arg):
                options.extend((arg, next_arg))
                index += 2
                continue
            if arg in ("--enable", "--disable") and next_arg and re.fullmatch(r"[A-Za-z0-9_]+", next_arg):
                options.extend((arg, next_arg))
                index += 2
                continue
            if arg == "-c" and next_arg and re.fullmatch(r"model_provider=[A-Za-z0-9_-]+", next_arg):
                options.extend((arg, next_arg))
                index += 2
                continue
            if arg in CODEX_BYPASS_FLAGS:
                options.append(arg)
                index += 1
                continue
        elif agent == "claude" and arg == "--dangerously-skip-permissions":
            options.append(arg)
            index += 1
            continue
        index += 1
    return options


def launch_options_from_process(agent: str, process_dir: Path) -> list[str]:
    return launch_options_from_arguments(agent, process_arguments(process_dir)[1:])


def session_id_from_arguments(process_dir: Path) -> str | None:
    arguments = process_arguments(process_dir)
    for index, argument in enumerate(arguments):
        for option in ("--resume", "--session-id"):
            prefix = f"{option}="
            if argument.startswith(prefix):
                candidate = argument[len(prefix):]
                if UUID_RE.match(candidate):
                    return candidate
            if argument == option and index + 1 < len(arguments):
                candidate = arguments[index + 1]
                if UUID_RE.match(candidate):
                    return candidate
        if argument == "-r" and index + 1 < len(arguments):
            candidate = arguments[index + 1]
            if UUID_RE.match(candidate):
                return candidate
    return None


def root_claude_session_from_process(process_dir: Path) -> tuple[str | None, str | None]:
    candidates: list[tuple[str, str]] = []
    try:
        descriptors = list((process_dir / "fd").iterdir())
    except OSError:
        descriptors = []
    for descriptor in descriptors:
        try:
            target = Path(os.readlink(descriptor))
        except OSError:
            continue
        if (
            target.suffix == ".jsonl"
            and "projects" in target.parts
            and UUID_RE.match(target.stem)
        ):
            candidates.append((target.stem, str(target)))
    if candidates:
        candidates.sort()
        return candidates[0]
    return session_id_from_arguments(process_dir), None


def read_only_rows(path: Path, query: str, parameters: tuple[Any, ...] = ()) -> list[Any]:
    """Run one query without writing to, or waiting long on, an agent's database."""
    uri = f"{path.resolve().as_uri()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=SQLITE_TIMEOUT_SECONDS)
    try:
        return connection.execute(query, parameters).fetchall()
    finally:
        connection.close()


def codex_goal_status(session_id: str) -> str | None:
    if not GOALS_DB_PATH.exists():
        return None
    try:
        rows = read_only_rows(
            GOALS_DB_PATH,
            "SELECT status FROM thread_goals WHERE thread_id = ?",
            (session_id,),
        )
    except (OSError, sqlite3.Error):
        return None
    status = rows[0][0] if rows else None
    return status if status in GOAL_STATUSES else None


def pane_title_has_thread_name(pane_title: str, thread_name: str) -> bool:
    segments = pane_title.split(CODEX_TITLE_SEPARATOR)
    if thread_name in segments:
        return True
    if not segments or not segments[0].endswith(f" {thread_name}"):
        return False
    activity = segments[0][:-len(thread_name)].strip()
    return bool(activity) and len(activity) <= MAX_TITLE_ACTIVITY_WIDTH and not any(
        character.isalnum() for character in activity
    )


def codex_session_from_pane_title(pane_title: str) -> tuple[str | None, str | None]:
    if not pane_title or not CODEX_STATE_DB_PATH.exists():
        return None, None
    try:
        rows = read_only_rows(CODEX_STATE_DB_PATH, CODEX_THREAD_NAMES_QUERY)
    except (OSError, sqlite3.Error):
        return None, None
    matches = {
        (session_id, transcript_path)
        for session_id, transcript_path, thread_name in rows
        if isinstance(session_id, str)
        and UUID_RE.match(session_id)
        and isinstance(thread_name, str)
        and pane_title_has_thread_name(pane_title, thread_name)
    }
    return next(iter(matches)) if len(matches) == 1 else (None, None)


def pane_screen_is_busy(pane_id: str) -> bool:
    result = tmux(["capture-pane", "-p", "-t", pane_id, "-S", "-20"], check=False)
    return result.returncode == 0 and bool(BUSY_RE.search(result.stdout))


def agent_metadata(
    agent: str,
    pane: dict[str, str], hook_state: dict[str, Any], server_key: str,
    process_index: dict[tuple[str, str], list[Path]],
) -> dict[str, Any] | None:
    command = Path(pane.get("pane_current_command", "")).name
    pane_id = pane["pane_id"]
    hook_entry = (
        hook_state.get("servers", {}).get(server_key, {}).get("panes", {}).get(pane_id, {})
    )
    if isinstance(hook_entry, dict):
        hook_agent = hook_entry.get("agent", "codex")
        if hook_agent != agent:
            hook_entry = {}
    processes = process_index.get((server_key, pane_id), [])
    if (
        command.startswith("python")
        and isinstance(hook_entry, dict)
        and hook_entry.get("managed_launcher")
        and isinstance(hook_entry.get("session_id"), str)
        and not processes
    ):
        metadata = {
            "session_id": hook_entry["session_id"],
            "was_running": bool(hook_entry.get("busy")),
            "transcript_path": hook_entry.get("transcript_path"),
            "launch_options": hook_entry.get("launch_options", []),
        }
        if agent == "codex":
            metadata["goal_status"] = codex_goal_status(hook_entry["session_id"])
        return metadata

    cached_session_id = hook_entry.get("session_id") if isinstance(hook_entry, dict) else None
    session_id = None
    transcript_path = None
    launch_options: list[str] = []
    candidates: list[tuple[bool, bool, int, str, str | None, list[str]]] = []
    session_reader = (
        root_codex_session_from_process
        if agent == "codex"
        else root_claude_session_from_process
    )
    for process_dir in processes:
        process_session_id, process_transcript = session_reader(process_dir)
        if not process_session_id:
            continue
        status = process_stat(process_dir)
        started = status[3] if status is not None else 0
        candidates.append((
            owns_foreground_job(process_dir),
            process_session_id == cached_session_id,
            started,
            process_session_id,
            process_transcript,
            launch_options_from_process(agent, process_dir),
        ))
    if candidates:
        # Prefer the tree that currently owns the tty, then the session
        # most recently recorded by hooks, then the newest process.
        _foreground, _hook_match, _started, session_id, transcript_path, launch_options = max(
            candidates, key=lambda item: item[:3]
        )
    if not session_id and agent == "codex":
        session_id, transcript_path = codex_session_from_pane_title(
            pane.get("pane_title", "")
        )
        if session_id:
            foreground_processes = [
                process for process in processes if not is_codex_app_server(process)
            ]
            if foreground_processes:
                selected = max(
                    foreground_processes,
                    key=lambda process: (
                        owns_foreground_job(process),
                        (process_stat(process) or (0, 0, 0, 0))[3],
                    ),
                )
                launch_options = launch_options_from_process(agent, selected)
    if (
        not session_id
        and (command == agent or processes)
        and isinstance(cached_session_id, str)
        and UUID_RE.match(cached_session_id)
        and (
            agent == "claude"
            or (
                hook_entry.get("managed_launcher")
                and command.startswith("python")
            )
        )
    ):
        # The cache is scoped by socket path, tmux server PID, and pane ID, so
        # it remains a safe fallback when procfs is restricted.
        session_id = cached_session_id
        transcript_path = hook_entry.get("transcript_path")
        launch_options = hook_entry.get("launch_options", [])
    if command != agent and not processes:
        return None
    authoritative_hook_busy = (
        isinstance(hook_entry, dict)
        and cached_session_id == session_id
        and isinstance(hook_entry.get("busy"), bool)
        and not hook_entry.get("managed_launcher")
    )

    if not session_id:
        return {"session_id": None, "was_running": pane_screen_is_busy(pane_id), "warning": "session ID unavailable"}

    # A matching hook is authoritative, including an idle Stop event. Screen
    # scraping is only for sessions that predate hooks or lost hook delivery.
    was_running = (
        bool(hook_entry["busy"])
        if authoritative_hook_busy
        else pane_screen_is_busy(pane_id)
    )
    metadata = {
        "session_id": session_id,
        "was_running": was_running,
        "transcript_path": transcript_path,
        "launch_options": launch_options,
    }
    if agent == "codex":
        metadata["goal_status"] = codex_goal_status(session_id)
    return metadata


def collect_snapshot(reason: str) -> dict[str, Any] | None:
    # One tmux command supplies a consistent session/window/pane hierarchy.
    topology_fields = [
        "socket_path", "pid", "session_id", "session_name", "session_group",
        "window_id", "window_index", "window_name", "window_active", "window_zoomed_flag",
        "window_layout",
        "pane_id", "pane_index", "pane_current_path", "pane_active",
        "pane_current_command", "pane_title",
    ]
    topology = None
    for attempt in range(3):
        returncode, candidate = query_rows(
            ["list-panes", "-a"], topology_fields, check=False
        )
        if returncode == 0:
            topology = candidate
            break
        if attempt < 2:
            time.sleep(0.05)
    if topology is None:
        # During shutdown systemd may stop tmux before ExecStop runs. Retain
        # the last periodic snapshot whenever tmux is already unavailable.
        # Periodic snapshots still record deliberate closure as an empty state.
        if reason == "shutdown":
            return None
        topology = []
    if any(pane.get("session_group") for pane in topology):
        raise RuntimeError("grouped tmux sessions are outside this installer's scope")
    topology.sort(key=lambda pane: (
        pane.get("session_name", ""),
        int(pane.get("window_index") or 0),
        int(pane.get("pane_index") or 0),
    ))
    hooks = load_hook_state()
    server_key = tmux_server_key(topology[0]["socket_path"], topology[0]["pid"]) if topology else None
    process_indexes = {
        agent: proc_agent_index(agent) if topology else {}
        for agent in ("codex", "claude")
    }

    sessions_by_id: dict[str, dict[str, Any]] = {}
    windows_by_id: dict[tuple[str, str], dict[str, Any]] = {}
    window_owners: dict[str, tuple[str, str]] = {}
    for pane in topology:
        session = sessions_by_id.setdefault(pane["session_id"], {
            "name": pane["session_name"],
            "windows": [],
        })
        owner = (pane["session_id"], pane["window_index"])
        prior_owner = window_owners.setdefault(pane["window_id"], owner)
        if prior_owner != owner:
            raise RuntimeError("linked tmux windows are outside this installer's scope")
        window_key = (pane["session_id"], pane["window_id"])
        window = windows_by_id.get(window_key)
        if window is None:
            window = {
                "index": int(pane["window_index"]),
                "name": pane["window_name"],
                "active": pane["window_active"] == "1",
                "zoomed": pane["window_zoomed_flag"] == "1",
                "layout": pane["window_layout"],
                "panes": [],
            }
            windows_by_id[window_key] = window
            session["windows"].append(window)
        pane_record: dict[str, Any] = {
            "pane_id": pane["pane_id"],
            "index": int(pane["pane_index"]),
            "path": pane["pane_current_path"] or str(HOME),
            "active": pane["pane_active"] == "1",
            "codex": agent_metadata(
                "codex", pane, hooks, server_key, process_indexes["codex"]
            ) if server_key else None,
            "claude": agent_metadata(
                "claude", pane, hooks, server_key, process_indexes["claude"]
            ) if server_key else None,
        }
        window["panes"].append(pane_record)

    sessions = list(sessions_by_id.values())
    for session in sessions:
        session["windows"].sort(key=lambda item: item["index"])
        for window in session["windows"]:
            window["panes"].sort(key=lambda item: item["index"])

    return {
        "version": 4,
        "source_boot_id": boot_id(),
        "captured_at": now_iso(),
        "reason": reason,
        "tmux_socket": topology[0]["socket_path"] if topology else TMUX_SOCKET,
        "sessions": sorted(sessions, key=lambda item: item["name"]),
    }


def semantic_snapshot(value: dict[str, Any]) -> dict[str, Any]:
    comparable = copy.deepcopy(value)
    comparable.pop("captured_at", None)
    comparable.pop("reason", None)
    return comparable


def snapshot_command(args: argparse.Namespace) -> int:
    with locked():
        snapshot = collect_snapshot(args.reason)
        if snapshot is None:
            print("tmux was unavailable during shutdown; retained the previous snapshot.")
            return 0
        previous = read_json(SNAPSHOT_PATH, {})
        if isinstance(previous, dict) and semantic_snapshot(previous) == semantic_snapshot(snapshot):
            print(f"Snapshot unchanged ({len(snapshot['sessions'])} sessions); no disk write.")
            return 0
        atomic_write_json(SNAPSHOT_PATH, snapshot)
        pane_count = sum(len(window["panes"]) for session in snapshot["sessions"] for window in session["windows"])
        print(f"Saved {len(snapshot['sessions'])} tmux sessions and {pane_count} panes.")
    return 0


def hook_command(args: argparse.Namespace) -> int:
    try:
        event = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0
    event_name = event.get("hook_event_name")
    pane_id = os.environ.get("TMUX_PANE")
    server_key = tmux_server_key(os.environ.get("TMUX"))
    session_id = event.get("session_id")
    if pane_id and server_key and isinstance(session_id, str):
        with locked():
            state = load_hook_state()
            state["version"] = 3
            server = state.setdefault("servers", {}).setdefault(server_key, {})
            panes = server.setdefault("panes", {})
            entry = panes.get(pane_id, {})
            previous_session_id = entry.get("session_id")
            entry.update({
                "agent": args.agent,
                "session_id": session_id,
                "cwd": event.get("cwd"),
                "transcript_path": event.get("transcript_path"),
                "updated_at": now_iso(),
            })
            if event_name == "SessionStart":
                source = event.get("source")
                if source != "compact" or previous_session_id != session_id:
                    entry["busy"] = False
                else:
                    entry.setdefault("busy", False)
                entry["source"] = source
                entry.pop("managed_launcher", None)
            elif event_name == "UserPromptSubmit":
                entry["busy"] = True
            elif event_name == "Stop":
                entry["busy"] = False
            panes[pane_id] = entry
            atomic_write_json(HOOK_STATE_PATH, state)
    if event_name == "Stop":
        # Stop hooks require JSON output on successful exit.
        print("{}")
    return 0


def record_managed_launcher(
    agent: str, session_id: str, cwd: str, busy: bool, launch_options: list[str],
) -> None:
    pane_id = os.environ.get("TMUX_PANE")
    server_key = tmux_server_key(os.environ.get("TMUX"))
    if not pane_id or not server_key:
        return
    with locked():
        state = load_hook_state()
        state["version"] = 3
        server = state.setdefault("servers", {}).setdefault(server_key, {})
        server.setdefault("panes", {})[pane_id] = {
            "agent": agent,
            "session_id": session_id,
            "cwd": cwd,
            "busy": busy,
            "launch_options": launch_options,
            "managed_launcher": True,
            "updated_at": now_iso(),
        }
        atomic_write_json(HOOK_STATE_PATH, state)


def managed_hook(command: str, status: str | None = None) -> dict[str, Any]:
    hook: dict[str, Any] = {"type": "command", "command": command, "timeout": 5}
    if status:
        hook["statusMessage"] = status
    return hook


def read_user_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SystemExit(f"Refusing to modify unreadable or malformed {path}: {error}") from error
    if not isinstance(value, dict):
        raise SystemExit(f"Refusing to replace non-object JSON in {path}")
    return value


def remove_managed_handlers(document: dict[str, Any]) -> bool:
    changed = False
    hooks = document.get("hooks")
    if not isinstance(hooks, dict):
        return False
    for event_name in list(hooks):
        groups = hooks.get(event_name)
        if not isinstance(groups, list):
            continue
        new_groups = []
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                new_groups.append(group)
                continue
            handlers = [
                handler for handler in group["hooks"]
                if not (
                    isinstance(handler, dict)
                    and (
                        MANAGED_COMMAND_MARKER in str(handler.get("command", ""))
                        or LEGACY_MANAGED_SCRIPT_FRAGMENT in str(handler.get("command", ""))
                    )
                )
            ]
            if len(handlers) != len(group["hooks"]):
                changed = True
            if handlers:
                updated_group = dict(group)
                updated_group["hooks"] = handlers
                new_groups.append(updated_group)
        if new_groups:
            hooks[event_name] = new_groups
        else:
            hooks.pop(event_name, None)
    return changed


def configure_agent_hooks(path: Path, agent: str) -> bool:
    document = read_user_json(path)
    remove_managed_handlers(document)
    hooks = document.setdefault("hooks", {})
    python = shlex.quote(sys.executable)
    script = shlex.quote(str(Path(__file__).resolve()))
    command = f"{MANAGED_COMMAND_MARKER} {python} {script} hook --agent {agent}"
    display_name = agent.capitalize()
    hooks.setdefault("SessionStart", []).append({
        "matcher": "startup|resume|clear|compact",
        "hooks": [managed_hook(command, f"Recording restorable {display_name} session")],
    })
    hooks.setdefault("UserPromptSubmit", []).append({"hooks": [managed_hook(command)]})
    hooks.setdefault("Stop", []).append({"hooks": [managed_hook(command)]})
    current = read_json(path, None)
    if current == document:
        print(f"{display_name} hooks already configured in {path}.")
        return False
    atomic_write_json(path, document)
    print(f"Configured {display_name} hooks in {path}.")
    return True


def configure_hooks_command(_: argparse.Namespace) -> int:
    configure_agent_hooks(CODEX_HOOKS_PATH, "codex")
    configure_agent_hooks(CLAUDE_SETTINGS_PATH, "claude")
    return 0


def pane_launch_command(pane: dict[str, Any]) -> str | None:
    agent = "codex"
    metadata = pane.get(agent)
    if not isinstance(metadata, dict) or not metadata.get("session_id"):
        agent = "claude"
        metadata = pane.get(agent)
    if not isinstance(metadata, dict) or not metadata.get("session_id"):
        return None
    environment = {
        "HOME": str(HOME),
        "CODEX_HOME": str(CODEX_HOME),
        "CLAUDE_CONFIG_DIR": str(CLAUDE_HOME),
        "XDG_CONFIG_HOME": os.environ.get("XDG_CONFIG_HOME", str(HOME / ".config")),
        "XDG_STATE_HOME": str(STATE_DIR.parent),
        "CODEX_TMUX_RESUME_TMUX_BIN": TMUX_EXE,
        "CODEX_TMUX_RESUME_CODEX_BIN": CODEX_EXE or "",
        "CODEX_TMUX_RESUME_CLAUDE_BIN": CLAUDE_EXE or "",
        "CODEX_TMUX_RESUME_TMUX_SOCKET": TMUX_SOCKET or "",
        "CODEX_TMUX_RESUME_PROMPT": CONTINUE_PROMPT,
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "SHELL": os.environ.get("SHELL", "/bin/bash"),
    }
    if os.environ.get("XDG_RUNTIME_DIR"):
        environment["XDG_RUNTIME_DIR"] = os.environ["XDG_RUNTIME_DIR"]
    command = [
        ENV_EXE,
        *(f"{key}={value}" for key, value in environment.items()),
        sys.executable,
        str(Path(__file__).resolve()),
        "launch",
        "--agent",
        agent,
        "--session-id",
        metadata["session_id"],
        "--cwd",
        pane.get("path") or str(HOME),
    ]
    if metadata.get("was_running"):
        command.append("--continue-turn")
    if agent == "codex" and metadata.get("goal_status") in GOAL_STATUSES:
        command.extend(["--goal-status", metadata["goal_status"]])
    if metadata.get("launch_options"):
        command.extend(["--launch-options", json.dumps(metadata["launch_options"])])
    return shlex.join(command)


def restored_cwd(value: Any) -> str:
    candidate = Path(value).expanduser() if value else HOME
    return str(candidate) if candidate.is_dir() else str(HOME)


def window_target(session_id: str, index: int) -> str:
    return f"{session_id}:{index}"


def layout_pane_ids(layout: str) -> list[str]:
    _checksum, separator, body = layout.partition(",")
    if not separator:
        return []
    return [
        f"%{pane_id}"
        for pane_id in re.findall(
            r"\d+x\d+,\d+,\d+,(\d+)(?=,|[}\]]|$)", body
        )
    ]


def align_panes_to_saved_layout(target: str, saved_layout: str, pane_map: dict[str, str]) -> None:
    desired = [pane_map.get(old_id) for old_id in layout_pane_ids(saved_layout)]
    if not desired or any(pane_id is None for pane_id in desired):
        return
    live_layout = tmux(["display-message", "-p", "-t", target, "#{window_layout}"]).stdout.strip()
    current = layout_pane_ids(live_layout)
    if len(current) != len(desired):
        return
    for position, desired_id in enumerate(desired):
        if current[position] == desired_id:
            continue
        try:
            other_position = current.index(desired_id)
        except ValueError:
            return
        tmux(["swap-pane", "-d", "-s", desired_id, "-t", current[position]])
        current[position], current[other_position] = current[other_position], current[position]


def restore_session(session: dict[str, Any], dry_run: bool) -> None:
    name = session["name"]
    windows = sorted(session.get("windows", []), key=lambda item: item["index"])
    if not windows:
        raise RuntimeError(f"saved tmux session {name!r} has no windows")
    first_window = windows[0]
    first_pane = (first_window.get("panes") or [{"path": str(HOME)}])[0]
    command = [
        "new-session", "-d", "-P", "-F",
        "#{session_id}\x1f#{window_id}\x1f#{pane_id}\x1f#{window_index}",
        "-s", name,
        "-n", first_window.get("name") or "shell",
        "-c", restored_cwd(first_pane.get("path")),
    ]
    launch = pane_launch_command(first_pane)
    if launch:
        command.append(launch)
    print(f"restore tmux session {name!r} ({len(windows)} windows)")
    if dry_run:
        return
    created = tmux(command).stdout.strip().split("\x1f")
    if len(created) != 4:
        raise RuntimeError(f"tmux returned unexpected new-session metadata: {created!r}")
    live_session_id, first_id, first_live_pane, current_index_text = created
    session["_live_session_id"] = live_session_id

    pane_map: dict[str, str] = {}

    # Move the automatically allocated first window to its saved index.
    if first_pane.get("pane_id"):
        pane_map[first_pane["pane_id"]] = first_live_pane
    current_index = int(current_index_text)
    if current_index != first_window["index"]:
        tmux([
            "move-window", "-s", first_id,
            "-t", window_target(live_session_id, first_window["index"]),
        ])

    for window in windows[1:]:
        panes = window.get("panes") or [{"path": str(HOME)}]
        first = panes[0]
        new_command = [
            "new-window", "-d", "-P", "-F", "#{pane_id}",
            "-t", window_target(live_session_id, window["index"]),
            "-n", window.get("name") or "shell", "-c", restored_cwd(first.get("path")),
        ]
        launch = pane_launch_command(first)
        if launch:
            new_command.append(launch)
        new_pane_id = tmux(new_command).stdout.strip()
        if first.get("pane_id"):
            pane_map[first["pane_id"]] = new_pane_id

    for window in windows:
        target = window_target(live_session_id, window["index"])
        panes = window.get("panes") or []
        for pane in panes[1:]:
            split_command = [
                "split-window", "-d", "-P", "-F", "#{pane_id}",
                "-t", target, "-c", restored_cwd(pane.get("path")),
            ]
            launch = pane_launch_command(pane)
            if launch:
                split_command.append(launch)
            new_pane_id = tmux(split_command).stdout.strip()
            # Keep temporary panes balanced so the next split does not hit the
            # minimum size before the exact saved layout is applied below.
            tmux(["select-layout", "-t", target, "tiled"], check=False)
            if pane.get("pane_id"):
                pane_map[pane["pane_id"]] = new_pane_id

        layout = window.get("layout")
        if layout:
            applied = tmux(["select-layout", "-t", target, layout], check=False)
            if applied.returncode == 0:
                align_panes_to_saved_layout(target, layout, pane_map)
        live_panes = rows(["list-panes", "-t", target], ["pane_id", "pane_index"])
        live_panes.sort(key=lambda item: int(item["pane_index"]))
        saved_panes = sorted(panes, key=lambda item: item["index"])
        active_saved = next((pane for pane in saved_panes if pane.get("active")), None)
        active_live_id = pane_map.get(active_saved.get("pane_id")) if active_saved else None
        selected_live_id = None
        if active_live_id:
            tmux(["select-pane", "-t", active_live_id], check=False)
            selected_live_id = active_live_id
        elif live_panes:
            active_position = next((idx for idx, pane in enumerate(saved_panes) if pane.get("active")), 0)
            if active_position < len(live_panes):
                tmux(["select-pane", "-t", live_panes[active_position]["pane_id"]], check=False)
                selected_live_id = live_panes[active_position]["pane_id"]
        if window.get("zoomed") and selected_live_id:
            tmux(["resize-pane", "-Z", "-t", selected_live_id], check=False)

    active_window = next((window for window in windows if window.get("active")), windows[0])
    tmux([
        "select-window", "-t", window_target(live_session_id, active_window["index"]),
    ], check=False)


def cleanup_failed_restore(
    sessions: list[dict[str, Any]], existing: dict[str, str],
) -> None:
    for session in reversed(sessions):
        if session["name"] in existing:
            continue
        live_session_id = session.get("_live_session_id")
        if live_session_id:
            tmux(["kill-session", "-t", live_session_id], check=False)


def existing_sessions() -> dict[str, str]:
    return {
        row["session_name"]: row["session_id"]
        for row in rows(
            ["list-sessions"], ["session_name", "session_id"], check=False
        )
    }


def ordered_snapshot_sessions(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    return sorted(
        copy.deepcopy(snapshot["sessions"]),
        key=lambda session: session.get("name", ""),
    )


def restore_command(args: argparse.Namespace) -> int:
    global TMUX_SOCKET
    with restore_locked():
        with locked():
            snapshot = read_json(SNAPSHOT_PATH, {})
            if not isinstance(snapshot, dict) or not snapshot.get("sessions"):
                print("No saved tmux snapshot to restore.")
                return 0
            current_boot = boot_id()
        if not TMUX_SOCKET and snapshot.get("tmux_socket"):
            TMUX_SOCKET = snapshot["tmux_socket"]

        existing = existing_sessions()
        sessions = ordered_snapshot_sessions(snapshot)
        if not args.dry_run:
            # Periodic snapshots must not erase the input used for this boot.
            recovery_path = STATE_DIR / "recovery" / f"{current_boot or 'unknown'}.json"
            if not recovery_path.exists():
                atomic_write_json(recovery_path, snapshot)
        restored: list[str] = []
        skipped: list[str] = []
        try:
            for session in sessions:
                if session.get("name") in existing:
                    skipped.append(session["name"])
                    continue
                restore_session(session, args.dry_run)
                restored.append(session["name"])
        except Exception:
            if not args.dry_run:
                cleanup_failed_restore(sessions, existing)
            raise

        if not args.dry_run:
            with locked():
                atomic_write_json(RESTORE_MARKER_PATH, {
                    "boot_id": current_boot,
                    "restored_at": now_iso(),
                    "restored": restored,
                    "skipped_existing": skipped,
                })
        print(f"Restored {len(restored)} sessions; skipped {len(skipped)} existing sessions.")
    return 0


def wait_for_network(host: str, timeout: int = 300) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
            return
        except OSError:
            time.sleep(5)


def codex_resume_prompt(goal_status: str | None, continue_turn: bool) -> str | None:
    if goal_status == "active":
        return None
    if goal_status in GOAL_PROMPT_STATUSES:
        # An explicit empty initial prompt suppresses the paused-goal dialog
        # without submitting a user turn. The stored goal remains unchanged.
        return ""
    return CONTINUE_PROMPT if continue_turn else None


def prepare_codex_update(executable: str) -> bool:
    """Attempt one unattended update per boot before any managed Codex resumes."""
    STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    marker = STATE_DIR / "update-boot.json"
    current_boot = boot_id()
    with (STATE_DIR / "update.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        previous = read_json(marker, {})
        if current_boot and previous.get("boot_id") == current_boot:
            return previous.get("status") == 0
        print("Checking for a Codex update before resuming the saved session...", flush=True)
        status = None
        try:
            with (STATE_DIR / "update.log").open("w") as output:
                process = subprocess.Popen(
                    [executable, "update"], stdin=subprocess.DEVNULL,
                    stdout=output, stderr=subprocess.STDOUT,
                    env={**os.environ, "CODEX_NON_INTERACTIVE": "1"},
                    start_new_session=True,
                )
                try:
                    status = process.wait(timeout=180)
                except subprocess.TimeoutExpired:
                    # Stop installer children too, before another pane resumes.
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
        except OSError as error:
            print(f"Codex update failed: {error}", file=sys.stderr)
        atomic_write_json(marker, {
            "boot_id": current_boot, "attempted_at": now_iso(), "status": status,
        })
        if status != 0:
            print(f"Codex update did not finish successfully; resuming the installed version. See {STATE_DIR / 'update.log'}.", file=sys.stderr)
        return status == 0


def launch_command(args: argparse.Namespace) -> int:
    cwd = Path(restored_cwd(args.cwd))
    goal_status = args.goal_status
    try:
        saved_options = json.loads(getattr(args, "launch_options", "[]"))
    except (TypeError, json.JSONDecodeError):
        saved_options = []
    if not isinstance(saved_options, list) or not all(isinstance(item, str) for item in saved_options):
        saved_options = []
    launch_options = launch_options_from_arguments(args.agent, saved_options)
    if args.agent == "codex" and goal_status is None:
        goal_status = codex_goal_status(args.session_id)
    resumes_work = (
        args.continue_turn
        if args.agent == "claude"
        else goal_status == "active"
        or (args.continue_turn and goal_status not in GOAL_PROMPT_STATUSES)
    )
    record_managed_launcher(args.agent, args.session_id, str(cwd), resumes_work, launch_options)
    if resumes_work:
        host = "chatgpt.com" if args.agent == "codex" else "api.anthropic.com"
        wait_for_network(host)

    executable = CODEX_EXE if args.agent == "codex" else CLAUDE_EXE
    display_name = args.agent.capitalize()
    if not executable or not Path(executable).is_file() or not os.access(executable, os.X_OK):
        print(f"{args.agent} executable not found; opening a shell instead", file=sys.stderr)
        return launch_shell(cwd)
    if args.agent == "codex":
        update_succeeded = prepare_codex_update(executable)
        command = [executable]
        if not update_succeeded:
            command.extend(["-c", "check_for_update_on_startup=false"])
        command.extend(launch_options)
        if not any(option in launch_options for option in CODEX_BYPASS_FLAGS):
            command.append(CODEX_DEFAULT_BYPASS_FLAG)
        command.extend(["resume", args.session_id])
        prompt = codex_resume_prompt(goal_status, args.continue_turn)
    else:
        command = [executable, *launch_options, "--resume", args.session_id]
        prompt = CONTINUE_PROMPT if args.continue_turn else None
    if prompt is not None:
        command.append(prompt)
    print(f"Resuming {display_name} session {args.session_id}", flush=True)
    try:
        result = subprocess.run(command, cwd=cwd)
    except OSError as error:
        print(f"could not launch {display_name} ({error}); opening a shell instead", file=sys.stderr)
        return launch_shell(cwd)
    print(
        f"{display_name} exited with status {result.returncode}; opening a login shell.",
        file=sys.stderr,
    )
    print(f"Resume this conversation with: {shlex.join(command)}", file=sys.stderr)
    return launch_shell(cwd)


def launch_shell(cwd: Path) -> int:
    shell = os.environ.get("SHELL") or "/bin/bash"
    os.chdir(cwd)
    os.execv(shell, [shell, "-l"])
    return 127


def status_command(_: argparse.Namespace) -> int:
    snapshot = read_json(SNAPSHOT_PATH, {})
    marker = read_json(RESTORE_MARKER_PATH, {})
    print(f"snapshot: {SNAPSHOT_PATH}")
    if isinstance(snapshot, dict) and snapshot.get("sessions") is not None:
        sessions = snapshot.get("sessions", [])
        panes = sum(len(window.get("panes", [])) for session in sessions for window in session.get("windows", []))
        codex_panes = sum(
            1 for session in sessions for window in session.get("windows", [])
            for pane in window.get("panes", []) if (pane.get("codex") or {}).get("session_id")
        )
        claude_panes = sum(
            1 for session in sessions for window in session.get("windows", [])
            for pane in window.get("panes", []) if (pane.get("claude") or {}).get("session_id")
        )
        active_turns = sum(
            1 for session in sessions for window in session.get("windows", [])
            for pane in window.get("panes", [])
            if any((pane.get(agent) or {}).get("was_running") for agent in ("codex", "claude"))
        )
        active_goals = sum(
            1 for session in sessions for window in session.get("windows", [])
            for pane in window.get("panes", [])
            if (pane.get("codex") or {}).get("goal_status") == "active"
        )
        digest = hashlib.sha256(json.dumps(semantic_snapshot(snapshot), sort_keys=True).encode()).hexdigest()[:12]
        print(f"captured: {snapshot.get('captured_at')} ({snapshot.get('reason')})")
        print(
            f"contents: {len(sessions)} sessions, {panes} panes, "
            f"{codex_panes} Codex panes, {claude_panes} Claude panes, "
            f"{active_turns} active turns, {active_goals} active goals"
        )
        print(f"semantic hash: {digest}")
    else:
        print("captured: never")
    if marker:
        print(f"last restore boot: {marker.get('boot_id')} at {marker.get('restored_at')}")
    return 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    sub = root.add_subparsers(dest="command", required=True)

    snapshot_parser = sub.add_parser("snapshot")
    snapshot_parser.add_argument("--reason", default="manual")
    snapshot_parser.set_defaults(func=snapshot_command)

    hook_parser = sub.add_parser("hook")
    hook_parser.add_argument("--agent", choices=("codex", "claude"), default="codex")
    hook_parser.set_defaults(func=hook_command)

    configure_parser = sub.add_parser("configure-hooks")
    configure_parser.set_defaults(func=configure_hooks_command)

    restore_parser = sub.add_parser("restore")
    restore_parser.add_argument("--dry-run", action="store_true")
    restore_parser.set_defaults(func=restore_command)

    launch_parser = sub.add_parser("launch")
    launch_parser.add_argument("--agent", choices=("codex", "claude"), default="codex")
    launch_parser.add_argument("--session-id", required=True)
    launch_parser.add_argument("--cwd", required=True)
    launch_parser.add_argument("--continue-turn", action="store_true")
    launch_parser.add_argument("--goal-status", choices=sorted(GOAL_STATUSES))
    launch_parser.add_argument("--launch-options")
    launch_parser.set_defaults(func=launch_command)

    status_parser = sub.add_parser("status")
    status_parser.set_defaults(func=status_command)
    return root


if __name__ == "__main__":
    arguments = parser().parse_args()
    raise SystemExit(arguments.func(arguments))
