# tmux-agent-resume

Restores tmux sessions after a Linux reboot and reopens the Codex or Claude Code conversation that was running in each pane.

## What it does

A systemd user timer runs `state.py snapshot` every five minutes and once more at shutdown. The snapshot records every tmux session, window, layout and pane, and for each pane the conversation ID of the Codex or Claude Code process inside it and whether that conversation was mid-turn. Hooks that the script registers in Codex's `hooks.json` and Claude Code's `settings.json` report conversation starts, prompt submissions and turn ends, so the snapshot does not have to guess.

At boot, `state.py restore` recreates every saved session that does not exist yet, applies the saved layouts, and starts `state.py launch` in each agent pane. The launcher runs `codex resume <id>` or `claude --resume <id>` in the saved directory. When the agent exits, the pane drops into a login shell and prints the resume command. Before the first Codex resume of a boot, the launcher runs one unattended `codex update` with a 180 second timeout and continues with the installed version if that fails. [docs/codex-updates.md](docs/codex-updates.md) covers the failure path.

Sessions that already exist at restore time are skipped whole. Panes that ran something other than an agent come back as a shell in the saved directory.

Snapshots retain supported launch options from the process that owns each
conversation. For Codex, the script saves model and profile selections, the
`model_provider` override, feature toggles, and the approval bypass flag. Each
value may follow its option as the next argument or after `=`, as in
`--model X` and `--model=X`. The short options `-m`, `-p` and `-c` also accept
an attached value, as in `-mX`. For Claude, it saves `--model` in split or
inline form and `--dangerously-skip-permissions`. It omits prompts, tokens, and other
configuration arguments.

After a successful update, managed launches use the saved options without a startup
override. After a failed update, they suppress the startup update prompt.

Codex panes may share the app-server daemon. When the TUI process does not own
its transcript, the snapshot matches the pane's terminal title to the unique
thread name in Codex's state database. An absent or ambiguous match is left
unrestored instead of guessing.

For such a pane, the snapshot does not fall back to the session ID that Codex
hooks recorded. Hooks can run inside the shared daemon, which files them under
the pane that started the daemon, so that ID may belong to another pane. Only a
pane where the managed launcher still runs uses its cached session ID.

## Requirements

- Linux with a systemd user manager, tmux and Python 3.10 or newer. The script reads `/proc` and uses `fcntl`, so it does not run on macOS or Windows.
- Codex CLI, Claude Code, or both, installed and logged in for the account that owns the tmux sessions. The unit templates expect `~/.local/bin/codex`, `~/.local/bin/claude`, `/usr/bin/tmux` and `/usr/bin/python3`. Edit `systemd/*.service` before installing if your paths differ.
- No Python packages at runtime. The tests need `pytest`, pinned in `requirements.txt`.

## Install

Run this from the repository root as the account that owns the tmux sessions. Back up `~/.codex/hooks.json` and `~/.claude/settings.json` first, because `configure-hooks` edits both. It keeps handlers it did not add.

```sh
install -d -m 755 ~/.local/libexec/codex-tmux-resume ~/.config/systemd/user
install -m 755 state.py ~/.local/libexec/codex-tmux-resume/state.py
install -m 644 systemd/*.service systemd/*.timer ~/.config/systemd/user/
python3 ~/.local/libexec/codex-tmux-resume/state.py configure-hooks
systemctl --user daemon-reload
python3 ~/.local/libexec/codex-tmux-resume/state.py snapshot --reason installation
systemctl --user enable --now codex-tmux-resume.service codex-tmux-resume.timer
```

Codex only runs hook handlers it has been told to trust. `codex --help` documents `--dangerously-bypass-hook-trust` as the way around that, which this tool does not use. Start Codex once by hand after `configure-hooks` and accept the handlers from `hooks.json` if it asks.

For the restore to run at boot without a login, let the account's user manager start on its own:

```sh
loginctl enable-linger "$USER"
```

The installed paths and service names are `codex-tmux-resume`, which the script used before it also handled Claude Code.

## Configuration

All settings are environment variables. The units set them; `state.py launch` passes them on into the restored panes.

| Variable | Default | Meaning |
| --- | --- | --- |
| `CODEX_TMUX_RESUME_CODEX_PERMISSION_MODE` | `inherit` | Saved bypass flags replay in either mode. Without one, `inherit` uses Codex's config and `bypass` adds `--dangerously-bypass-approvals-and-sandbox`. |
| `CODEX_TMUX_RESUME_AUTO_CONTINUE` | `never` | `never` reopens conversations and waits for you. `interrupted` also sends the prompt below to conversations that were mid-turn at snapshot time. |
| `CODEX_TMUX_RESUME_PROMPT` | `continue` | The prompt sent when `AUTO_CONTINUE` is `interrupted`. |
| `CODEX_TMUX_RESUME_CODEX_BIN`, `CODEX_TMUX_RESUME_CLAUDE_BIN`, `CODEX_TMUX_RESUME_TMUX_BIN` | found on `PATH` | Executables to use. |
| `CODEX_TMUX_RESUME_TMUX_SOCKET` | tmux default | Socket path passed to `tmux -S`. |
| `CODEX_HOME`, `CLAUDE_CONFIG_DIR` | `~/.codex`, `~/.claude` | Where the agents keep hooks, sessions and transcripts. |
| `XDG_STATE_HOME` | `~/.local/state` | Parent of the `codex-tmux-resume` state directory. |

Both `bypass` and `interrupted` mean an agent acts after a reboot without anyone at the keyboard. Turn them on only on a machine and user account where that is acceptable, by editing `~/.config/systemd/user/codex-tmux-resume.service` and running `systemctl --user daemon-reload`. A Codex conversation with an active goal continues on its own once Codex reopens it, whatever `AUTO_CONTINUE` says. An unknown value stops the script with a message naming the allowed values.

Change `CODEX_HOME`, `CLAUDE_CONFIG_DIR`, `XDG_CONFIG_HOME` and `XDG_STATE_HOME` in both service files and in the environment of any manual `state.py` command, or the snapshot and the hooks will disagree about where state lives.

## Usage

```sh
python3 ~/.local/libexec/codex-tmux-resume/state.py status
python3 ~/.local/libexec/codex-tmux-resume/state.py restore --dry-run
python3 ~/.local/libexec/codex-tmux-resume/state.py snapshot --reason manual
journalctl --user -u codex-tmux-resume.service -u codex-tmux-resume-snapshot.service
```

`status` prints what the current snapshot holds. `restore --dry-run` prints which sessions a restore would create and starts none. After installing, open the tmux panes you care about and check that the next snapshot lists their conversation IDs.

State lives in `~/.local/state/codex-tmux-resume`. `snapshot.json` is the current layout, `hook-state.json` holds the hook records, and `recovery/<boot-id>.json` keeps the snapshot each boot restored from. These files contain your directory paths and conversation IDs. Keep them on the machine.

## Example

`examples/snapshot.json` is an invented snapshot with two tmux sessions, one Codex pane and one Claude Code pane. Read it through the script without touching your own state:

```sh
XDG_STATE_HOME="$(mktemp -d)"
export XDG_STATE_HOME
install -D -m 600 examples/snapshot.json "$XDG_STATE_HOME/codex-tmux-resume/snapshot.json"
python3 state.py status
```

Expected output, except for the first line:

```
snapshot: /tmp/tmp.XXXXXXXXXX/codex-tmux-resume/snapshot.json
captured: 2026-09-01T10:00:00+00:00 (periodic)
contents: 2 sessions, 4 panes, 1 Codex panes, 1 Claude panes, 1 active turns, 0 active goals
semantic hash: 3af2c0e8710f
```

With tmux installed, `python3 state.py restore --dry-run` in the same shell prints `restore tmux session 'notes' (1 windows)` and `restore tmux session 'work' (2 windows)` for each of the two that does not already exist, and creates no session.

## Uninstall

```sh
systemctl --user disable --now codex-tmux-resume.timer codex-tmux-resume.service
rm ~/.config/systemd/user/codex-tmux-resume.service \
   ~/.config/systemd/user/codex-tmux-resume-snapshot.service \
   ~/.config/systemd/user/codex-tmux-resume.timer
systemctl --user daemon-reload
python3 ~/.local/libexec/codex-tmux-resume/state.py remove-hooks
rm -r ~/.local/libexec/codex-tmux-resume ~/.local/state/codex-tmux-resume
```

`remove-hooks` deletes only the handlers `configure-hooks` added. Running tmux sessions and agents keep running. Run `loginctl disable-linger "$USER"` if you enabled lingering for this tool alone.

## Agent interfaces the script depends on

The script has no version check. It was written against the interfaces below, read from `state.py`, and breaks when an agent changes one of them. I confirmed the command-line flags in the help output of codex-cli 0.155.1 and Claude Code 2.1.278.

Codex CLI:

- `codex resume <uuid> [prompt]`, `codex update`, `-c check_for_update_on_startup=false`, `--dangerously-bypass-approvals-and-sandbox`, and `CODEX_NON_INTERACTIVE=1` for the unattended update.
- `$CODEX_HOME/hooks.json` with `SessionStart`, `UserPromptSubmit` and `Stop` handlers in the same shape as Claude Code's, and a `session_id` field in each event.
- Session files under `$CODEX_HOME/sessions/` whose first line is a `session_meta` record with `payload.id`, `payload.originator` in `codex-tui` or `codex_cli_rs`, and `payload.source` equal to `cli` or `vscode`.
- `$CODEX_HOME/state_5.sqlite` with thread IDs, names, and rollout paths for unique title matching. Issue [#4](https://github.com/martinfrancois/tmux-agent-resume/issues/4) tracks replacing this internal lookup.
- `$CODEX_HOME/goals_1.sqlite` with a `thread_goals(thread_id, status)` table for goal-aware resumes.
- The text `esc to interrupt` on screen while a turn runs, used only for panes without hook records.

Claude Code:

- `claude --resume <uuid> [prompt]`, and `--resume`, `-r` or `--session-id` on the command line of a running process.
- `$CLAUDE_CONFIG_DIR/settings.json` hooks for `SessionStart` (matcher `startup|resume|clear|compact`), `UserPromptSubmit` and `Stop`, with the `Stop` handler printing `{}`.
- Transcripts under `$CLAUDE_CONFIG_DIR/projects/` named `<uuid>.jsonl`.

## Limitations

- Grouped tmux sessions and linked windows stop the snapshot with an error.
- A session that exists at restore time is skipped as a whole, even if some of its panes are back at a shell. Resume those by hand with the printed command.
- Layouts are restored by pane count and position. A window whose pane sizes fall below tmux's minimum keeps the tiled layout.
- Busy detection for conversations without hook records reads the last 20 screen lines, so a conversation that hides that text is treated as idle.
- The Codex update step is a single attempt per boot. A stuck installer is killed after 180 seconds.

## Tests

```sh
python3 -m venv .venv && . .venv/bin/activate
python -m pip install -r requirements.txt
python -m pytest
```

The tests use temporary directories, fake executables and the files under `examples/`. They do not start tmux, open conversations or update anything.

## License

MIT, see [LICENSE](LICENSE).
