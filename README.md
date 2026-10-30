# tmux-agent-resume

Restore tmux layouts and saved Codex or Claude conversations after a Linux
reboot. A systemd user service restores the saved panes, and a timer refreshes
the snapshot every five minutes. Agent hooks record conversation IDs and
whether a turn was running.

The repository contains the installed script, its regression tests, an
invented example snapshot, and systemd unit templates. Installation paths and
service names remain `codex-tmux-resume` for compatibility.

See the [setup runbook](docs/setup.md) to install and uninstall it and the
[Codex update notes](docs/codex-updates.md) for failure handling.

## Resume behavior

The script restores missing tmux sessions and skips sessions that already
exist. It reopens saved conversations by ID. It continues interrupted turns
only when `CODEX_TMUX_RESUME_AUTO_CONTINUE` is `interrupted`.
Codex goal handling preserves the distinction between active, paused, and
blocked goals.

Snapshots retain supported launch options from the process that owns each
conversation. For Codex, the script saves model and profile selections, the
`model_provider` override, feature toggles, and the approval bypass flag. Each
value may follow its option as the next argument or after `=`, as in
`--model X` and `--model=X`. The short options `-m`, `-p` and `-c` also accept
an attached value, as in `-mX`. For Claude, it saves `--model` in split or
inline form, `--permission-mode`, and `--dangerously-skip-permissions`. It
omits prompts, tokens, and other configuration arguments.

Claude hooks also record the permission mode reported at prompt submission
and turn completion. That mode overrides the original launch options for the
same conversation, including a switch out of bypass mode. Without a recorded
mode, the snapshot uses the conversation's process options. When only hooks
identify a fresh conversation, it reads options from the single foreground
Claude process in that pane. An ambiguous process match supplies no options.
This works with direct commands, aliases, and wrappers; no alias name is
required. It never adds bypass merely because another pane uses it.

Before managed Codex instances launch, the script attempts one unattended
`codex update` per boot. A shared lock makes other panes wait for that attempt.
If updating fails or times out, restoration proceeds with the installed
executable. That failure path suppresses the startup update prompt so the
fallback does not block on "Update now / Skip".

After a successful update check, Codex launches with the saved options and can
use its shared app-server daemon. If the update fails, the prompt-suppression
override makes that boot's managed launches use embedded mode.

Codex replays a saved bypass flag when present. Without one, `inherit` uses
Codex's own approval and sandbox settings, and `bypass` adds the bypass flag.

Codex panes may share the app-server daemon. When the TUI process does not own
its transcript, the snapshot matches the pane's terminal title to the unique
thread name in Codex's state database. An absent or ambiguous match is left
unrestored instead of guessing.

For such a pane, the snapshot does not fall back to the session ID that Codex
hooks recorded. Hooks can run inside the shared daemon, which files them under
the pane that started the daemon, so that ID may belong to another pane. Only a
pane where the managed launcher still runs uses its cached session ID.

## Configuration

All settings are environment variables. The units set them, and
`state.py launch` passes them on into the restored panes.

| Variable | Default | Meaning |
| --- | --- | --- |
| `CODEX_TMUX_RESUME_CODEX_PERMISSION_MODE` | `inherit` | Saved bypass flags replay in either mode. Without one, `inherit` uses Codex's config and `bypass` adds `--dangerously-bypass-approvals-and-sandbox`. |
| `CODEX_TMUX_RESUME_AUTO_CONTINUE` | `never` | `never` reopens conversations and waits for you. `interrupted` also sends the prompt below to conversations that were mid-turn at snapshot time. |
| `CODEX_TMUX_RESUME_PROMPT` | `continue` | The prompt sent when `AUTO_CONTINUE` is `interrupted`. |
| `CODEX_TMUX_RESUME_CODEX_BIN`, `CODEX_TMUX_RESUME_CLAUDE_BIN`, `CODEX_TMUX_RESUME_TMUX_BIN` | found on `PATH` | Executables to use. |
| `CODEX_TMUX_RESUME_TMUX_SOCKET` | tmux default | Socket path passed to `tmux -S`. |
| `CODEX_HOME`, `CLAUDE_CONFIG_DIR` | `~/.codex`, `~/.claude` | Where the agents keep hooks, sessions and transcripts. |
| `XDG_STATE_HOME` | `~/.local/state` | Parent of the `codex-tmux-resume` state directory. |

Both `bypass` and `interrupted` mean an agent acts after a reboot without
anyone at the keyboard. Turn them on only on a machine and user account where
that is acceptable, by editing
`~/.config/systemd/user/codex-tmux-resume.service` and running
`systemctl --user daemon-reload`. A Codex conversation with an active goal
continues on its own once Codex reopens it, whatever `AUTO_CONTINUE` says.

The script reads both settings when it starts. An unknown value makes every
`state.py` command exit with a message naming the allowed values: the restore
and the launcher, but also the snapshot timer and the agent hooks.

## Example

`examples/snapshot.json` is an invented snapshot with two tmux sessions, one
Codex pane and one Claude Code pane. Read it through the script without
touching your own state:

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

With tmux installed, `python3 state.py restore --dry-run` in the same shell
prints `restore tmux session 'notes' (1 windows)` and
`restore tmux session 'work' (2 windows)` for each of the two that does not
already exist, and creates no session.

## Agent interfaces the script depends on

The script has no version check. It was written against the interfaces below,
read from `state.py`, and breaks when an agent changes one of them. The
command-line flags were checked against the help output of codex-cli 0.155.1
and Claude Code 2.1.314.

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
- The [`permission_mode` hook field](https://code.claude.com/docs/en/hooks#common-input-fields), when present. Accepted values are `default`, `manual`, `plan`, `acceptEdits`, `auto`, `dontAsk`, and `bypassPermissions`.
- Transcripts under `$CLAUDE_CONFIG_DIR/projects/` named `<uuid>.jsonl`.

## Limitations

- Grouped tmux sessions and linked windows stop the snapshot with an error.
- A session that exists at restore time is skipped as a whole, even if some of its panes are back at a shell. Resume those by hand with the printed command.
- Layouts are restored by pane count and position. A window whose pane sizes fall below tmux's minimum keeps the tiled layout.
- Busy detection for conversations without hook records reads the last 20 screen lines, so a conversation that hides that text is treated as idle.
- Claude mode changes made inside the UI are recorded on the next prompt submission or turn completion. A change followed immediately by a reboot has no hook record. Old snapshots that already lost the bypass setting cannot reconstruct it.
- The Codex update step is a single attempt per boot. A stuck installer is killed after 180 seconds.

## Tests

```sh
python3 -m venv .venv && . .venv/bin/activate
python -m pip install -r requirements.txt
python -m pytest
```

The tests use temporary files, fake executables, and the files under
`examples/`. They exercise update and resume behavior without starting tmux,
opening real conversations, or updating Codex.

## License

MIT, see [LICENSE](LICENSE).
