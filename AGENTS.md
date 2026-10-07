# Agent guide

- Read README.md first: what each tool does and the invariants to keep.
- Commits carry "(AI-assisted)" when an agent wrote them.
- Nothing personal in this public repository: no account names, private paths, tokens or listening data, not in
  defaults, tests, examples or commit messages. Personal values belong in the user's
  `~/.config/rormpc-tools/config.toml`.
- Release: bump `version` in pyproject.toml, tag `vX.Y.Z`, push the tag, then bump `RORMPC_TOOLS_TAG` in rormpc's
  `scripts/rormpc_install.sh` and run `rormpc_install.sh companions` there. In the agent Bash tool's shell, `sed` is BSD, not GNU; GNU-style `sed -i 's/…/…/'` is not portable.
  Bump the version with Python and verify the actual change in `git diff` before committing or tagging.
  Gate the tag on the tests
  without a pipe in between (`uv run pytest -q && ...`, or `set -o pipefail`): `pytest | tail` returns tail's 0,
  and 0.2.23 was tagged with a failing test (2026-10-06). While developing, use
  `uv tool install --editable .` so the commands on PATH run the checkout.
- Serial daemon callbacks do not freeze MPD: validate again after individual priority writes and before
  acknowledgement. If playback changed, reconcile live state; never roll an expired snapshot back over it.
- Tests: `uv run pytest` (offline: fake MPD, temporary data dir, no network). Add a test for every bug that
  could silently miscount or mismatch a song.
- Check with `uvx pyflakes src` and run the changed command against MPD; `musicdb sync` writes stickers to the real
  MPD, so test with `MUSICDB`, `MUSICDB_DATA` and `MPD_PORT` pointing at a scratch DB, directory and MPD.
- Stop a scratch `mpd-player` by this checkout's path (`pkill -f <worktree>/.venv/bin/mpd-player`) or its pid,
  never `pkill -f mpd-player` or `bin/mpd-player`: that also matches the user's launchd daemon (killed that way
  on 2026-10-07; launchd restarted it).
