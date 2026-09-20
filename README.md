# tmux-agent-resume

Restore tmux layouts and saved Codex or Claude conversations after a Linux
reboot. A systemd user service restores the saved panes, and a timer refreshes
the snapshot every five minutes. Agent hooks record conversation IDs and
whether a turn was running.

The repository contains the installed script, its update regression tests,
and systemd unit templates. Installation paths and service names remain
`codex-tmux-resume` for compatibility.

See the [setup runbook](docs/setup.md) to install it and the
[Codex update notes](docs/codex-updates.md) for failure handling.

## Resume behavior

The script restores missing tmux sessions and skips sessions that already
exist. It reopens saved conversations by ID and continues interrupted turns.
Codex goal handling preserves the distinction between active, paused, and
blocked goals.

Snapshots retain supported launch options from the process that owns each
conversation. For Codex, the script saves model and profile selections, the
`model_provider` override, feature toggles, and the approval bypass flag. Each
value may follow its option as the next argument or after `=`, as in
`--model X` and `--model=X`. The short options `-m`, `-p` and `-c` also accept
an attached value, as in `-mX`. For Claude, it saves `--model` in split or
inline form and `--dangerously-skip-permissions`. It omits prompts, tokens, and other
configuration arguments.

Before managed Codex instances launch, the script attempts one unattended
`codex update` per boot. A shared lock makes other panes wait for that attempt.
If updating fails or times out, restoration proceeds with the installed
executable. That failure path suppresses the startup update prompt so the
fallback does not block on "Update now / Skip".

After a successful update check, Codex launches with the saved options and can
use its shared app-server daemon. If the update fails, the prompt-suppression
override makes that boot's managed launches use embedded mode.

Two settings in `systemd/codex-tmux-resume.service` decide how much the restored
agents may do on their own. Both default to the safe choice.

`CODEX_TMUX_RESUME_CODEX_PERMISSION_MODE` is `inherit` by default: a resumed
Codex conversation replays a saved bypass flag when present and otherwise uses
the approval and sandbox settings from Codex's own config. Set it to `bypass`
to add `--dangerously-bypass-approvals-and-sandbox` when no bypass flag was saved. Only do that on a machine and user account
where an agent running without approvals after a reboot is acceptable.

`CODEX_TMUX_RESUME_AUTO_CONTINUE` is `never` by default: conversations are
reopened and wait for you. Set it to `interrupted` to also send the prompt from
`CODEX_TMUX_RESUME_PROMPT` (default `continue`) to conversations that were
mid-turn when the snapshot was taken. A Codex conversation with an active goal
continues on its own once Codex reopens it, whatever this setting says.

An unknown value stops the script with a message naming the allowed values.

Codex panes may share the app-server daemon. When the TUI process does not own
its transcript, the snapshot matches the pane's terminal title to the unique
thread name in Codex's state database. An absent or ambiguous match is left
unrestored instead of guessing.

For such a pane, the snapshot does not fall back to the session ID that Codex
hooks recorded. Hooks can run inside the shared daemon, which files them under
the pane that started the daemon, so that ID may belong to another pane. Only a
pane where the managed launcher still runs uses its cached session ID.

## Tests

```sh
python3 test_updates.py
```

The tests use temporary files and fake executables. They exercise update and
resume behavior without opening real conversations or updating Codex.
