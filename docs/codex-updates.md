# Codex updates during session restoration

This note documents the update behavior of the installed `state.py` launcher.

## Setup requirement

Keep the unattended update step when installing or modifying this script. The
configured Codex executable must support `codex update`.

The launcher runs `codex update` with `CODEX_NON_INTERACTIVE=1` before resuming
saved Codex conversations. It attempts the update once per boot and holds a
shared lock while doing so. Other restored Codex panes wait for that attempt
to finish before launching.

After a successful update check, the launcher resumes without a command-line
configuration override, which lets Codex use its shared app-server daemon. If
the update fails or exceeds 180 seconds, the launcher attempts to resume the
saved conversation using the installed executable and adds
`-c check_for_update_on_startup=false`. Suppressing the prompt keeps that
fallback from stopping at the same "Update now / Skip" screen that interrupted
restoration before. Codex uses embedded mode for those failed-update launches.

The setting applies to Codex instances started by this launcher. It does not
change the user's global Codex configuration or disable the preceding
unattended update step. Claude restoration does not run this Codex update step.

## Check an update attempt

State files live in `$XDG_STATE_HOME/codex-tmux-resume`, or
`~/.local/state/codex-tmux-resume` when `XDG_STATE_HOME` is unset.

- `update.log` contains the updater's output.
- `update-boot.json` records the boot ID, attempt time, and exit status.
  Status `0` means success; `null` means the attempt timed out or encountered
  an operating-system error.
- `update.lock` serializes update attempts across restored panes.

A failed attempt is also recorded for the current boot. Later managed launches
in that boot skip the update attempt and resume with the startup prompt
suppressed. After resolving the update failure, run `codex update` manually,
or let the next boot retry it.

## Verify a script change

Run the regression tests beside the installed script:

```sh
python3 ~/.local/libexec/codex-tmux-resume/test_updates.py
```

They cover updating before resuming the same conversation, skipping a second
update in the same boot, resuming after an update failure, stopping installer
children after a timeout, preserving the boot recovery snapshot, and leaving
Claude's launch command independent of Codex updates.
