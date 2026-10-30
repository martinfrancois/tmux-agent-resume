# Setup runbook

## Requirements

Use a Linux account with Python 3.10 or newer, tmux, and a working systemd user
manager. The script reads `/proc` and uses `fcntl`, so it does not run on macOS
or Windows. It needs no Python packages; the tests need the `pytest` version
pinned in `requirements.txt`. Install and authenticate Codex or Claude under
that account before enabling restoration. Codex must support `codex update` and
`check_for_update_on_startup=false`.

The unit templates expect `/usr/bin/python3`, `/usr/bin/tmux`, and agent
executables in `~/.local/bin`. Edit the templates before installation if those
paths differ. `%h` in a systemd unit expands to the user's home directory.
Include any runtime directories required by your agent installation in the
units' `PATH` values.

## Install

Run these commands from the repository root as the account that owns the tmux
sessions. Back up `~/.codex/hooks.json` and `~/.claude/settings.json` first.

```sh
install -d -m 755 ~/.local/libexec/codex-tmux-resume ~/.config/systemd/user
install -m 755 state.py ~/.local/libexec/codex-tmux-resume/state.py
install -m 644 systemd/*.service systemd/*.timer ~/.config/systemd/user/
python3 ~/.local/libexec/codex-tmux-resume/state.py configure-hooks
systemctl --user daemon-reload
python3 ~/.local/libexec/codex-tmux-resume/state.py snapshot --reason installation
systemctl --user enable --now codex-tmux-resume.service codex-tmux-resume.timer
```

`configure-hooks` edits Codex's `hooks.json` and Claude's `settings.json` to
register the script's handlers. It preserves unrelated hook handlers.

Codex only runs hook handlers it has been told to trust. `codex --help`
documents `--dangerously-bypass-hook-trust` as the way around that, which this
tool does not use. Start Codex once by hand after `configure-hooks` and accept
the handlers from `hooks.json` if it asks.

By default, restoration preserves a saved bypass flag but adds none when the
snapshot has no such flag. It does not continue interrupted turns. The
[Configuration](../README.md#configuration) section lists the overrides.

For Claude, launch with `claude --dangerously-skip-permissions` or
`claude --permission-mode bypassPermissions` when bypass is intended.
An alias or wrapper that passes these options works too. The snapshot keeps
the choice separately for each conversation. Hooks record subsequent mode
changes on prompt submission and turn completion, including leaving bypass.
When upgrading an installation whose old snapshots lost this choice, select
the intended mode again in each affected Claude session and submit a prompt
before the next reboot. Installing the script cannot recover a setting that
was never saved.

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
python3 ~/.local/libexec/codex-tmux-resume/state.py status
python3 ~/.local/libexec/codex-tmux-resume/state.py restore --dry-run
systemctl --user status codex-tmux-resume.service codex-tmux-resume.timer
```

Open the tmux panes and confirm that the next snapshot records their expected
conversation IDs. The restore command skips existing tmux sessions, including
sessions whose panes have returned to a shell. Recover those panes individually
using a known conversation ID.

The regression tests run from a checkout of this repository, as the
[README](../README.md#tests) describes.

## Codex updates

The update runs first. A successful update resumes without a configuration
override, allowing Codex to use its shared app-server daemon. If the update
fails or exceeds the 180-second timeout, the launcher adds
`-c check_for_update_on_startup=false` so the fallback does not stop at an
interactive update screen.

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
to install the files. Installing the unit templates again replaces the settings
in `codex-tmux-resume.service`, so set any value you changed there again before
the reload.

To disable future automatic restoration and snapshots:

```sh
systemctl --user disable --now codex-tmux-resume.timer
systemctl --user disable codex-tmux-resume.service
```

This leaves existing tmux sessions running. Agent hooks remain configured until
you run `state.py remove-hooks`, which deletes only the
`CODEX_TMUX_RESUME_HOOK=1` handlers that `configure-hooks` added.

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

Running tmux sessions and agents keep running. Run
`loginctl disable-linger "$USER"` if you enabled lingering for this tool alone.
