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

Before managed Codex instances launch, the script attempts one unattended
`codex update` per boot. A shared lock makes other panes wait for that attempt.
If updating fails or times out, restoration proceeds with the installed
executable. The startup update prompt is suppressed for managed launches so
that this fallback does not block on "Update now / Skip".

If updating succeeds and installs the latest version, that prompt should
already be absent. Suppression protects the failure path and does not disable
the preceding update attempt.

Codex resumes with `--dangerously-bypass-approvals-and-sandbox`, matching the
original installation. Account for that permission mode when choosing the
machine and user account for installation.

## Tests

```sh
python3 test_updates.py
```

The tests use temporary files and fake executables. They exercise update and
resume behavior without opening real conversations or updating Codex.
