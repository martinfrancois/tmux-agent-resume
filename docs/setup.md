# Setup runbook

## Requirements

Use a Linux account with Python 3.10 or newer, tmux, and a working systemd user
manager. Install and authenticate Codex or Claude under that account before
enabling restoration. Codex must support `codex update` and
`check_for_update_on_startup=false`.

The unit templates expect `/usr/bin/python3`, `/usr/bin/tmux`, and agent
executables in `~/.local/bin`. Edit the templates before installation if those
paths differ. `%h` in a systemd unit expands to the user's home directory.
Include any runtime directories required by your agent installation in the
units' `PATH` values.

## Install

Run these commands from the repository root as the account that owns the tmux
sessions. Back up existing Codex and Claude configuration first.

```sh
install -d -m 755 ~/.local/libexec/codex-tmux-resume ~/.config/systemd/user
install -m 755 state.py ~/.local/libexec/codex-tmux-resume/state.py
install -m 644 test_updates.py ~/.local/libexec/codex-tmux-resume/test_updates.py
install -m 644 docs/codex-updates.md ~/.local/libexec/codex-tmux-resume/README.md
install -m 644 systemd/*.service systemd/*.timer ~/.config/systemd/user/
python3 ~/.local/libexec/codex-tmux-resume/state.py configure-hooks
systemctl --user daemon-reload
python3 ~/.local/libexec/codex-tmux-resume/state.py snapshot --reason installation
systemctl --user enable --now codex-tmux-resume.service codex-tmux-resume.timer
```

`configure-hooks` edits Codex's `hooks.json` and Claude's `settings.json` to
register the script's handlers. It preserves unrelated hook handlers.

To start the user service at boot without waiting for a login, enable lingering
for the account. This command requires the host's authorization:

```sh
loginctl enable-linger "$USER"
```

The templates use configuration and state directories under the user's home.
For different directories, update `CODEX_HOME`, `CLAUDE_CONFIG_DIR`,
`XDG_CONFIG_HOME`, and `XDG_STATE_HOME` consistently in both services and in the
environment used for manual script commands.

## Verify

```sh
python3 ~/.local/libexec/codex-tmux-resume/test_updates.py
python3 ~/.local/libexec/codex-tmux-resume/state.py status
python3 ~/.local/libexec/codex-tmux-resume/state.py restore --dry-run
systemctl --user status codex-tmux-resume.service codex-tmux-resume.timer
```

Open the tmux panes and confirm that the next snapshot records their expected
conversation IDs. The restore command skips existing tmux sessions, including
sessions whose panes have returned to a shell. Recover those panes individually
using a known conversation ID.

## Codex updates

Keep the update step and `-c check_for_update_on_startup=false` together.
The update runs first. Successful installation of the latest version should
already remove the prompt. The explicit setting protects restoration when an
update fails or exceeds the 180-second timeout, so the fallback does not stop
at an interactive update screen.

The override applies only to managed launches. It does not change the global
Codex configuration. Failed attempts are recorded for the current boot; the
next boot retries. See [Codex update notes](codex-updates.md) for logs and manual
retry instructions.

## Recovery and maintenance

The default state directory is `~/.local/state/codex-tmux-resume`.
`snapshot.json` holds the current layout and conversation metadata.
`hook-state.json` holds agent hook records. The `recovery` directory retains
the snapshot used for each boot, even after periodic snapshots change.

Keep these state files on the machine. They contain local paths and conversation
identifiers and are not repository source files.

Inspect service output with:

```sh
journalctl --user -u codex-tmux-resume.service -u codex-tmux-resume-snapshot.service
```

To update an existing installation, rerun the file installation commands and
`systemctl --user daemon-reload`. New launcher processes use the new script;
already running launchers retain the code they loaded. A reboot is not required
to install the files.

To disable future automatic restoration and snapshots:

```sh
systemctl --user disable --now codex-tmux-resume.timer
systemctl --user disable codex-tmux-resume.service
```

This leaves existing tmux sessions running. Agent hooks remain configured until
you remove their `CODEX_TMUX_RESUME_HOOK=1` handlers from agent configuration.
