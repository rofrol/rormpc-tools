# Agent guide

- Read README.md first: what each tool does and the invariants to keep.
- Commits carry "(AI-assisted)" when an agent wrote them.
- Nothing personal in this public repository: no account names, private paths, tokens or listening data, not in
  defaults, tests, examples or commit messages. Personal values belong in the user's
  `~/.config/rormpc-tools/config.toml`.
- Release: bump `version` in pyproject.toml, tag `vX.Y.Z`, push the tag, then bump `RORMPC_TOOLS_TAG` in rormpc's
  `scripts/rormpc_install.sh` and run `rormpc_install.sh companions` there. Check that the version line really
  changed before tagging: `sed` on this Mac is GNU sed (Homebrew gnubin first on PATH), where BSD's `sed -i ''`
  fails, and a chain with `&&` then stops before the commit and the tag (happened 2026-10-06). Gate the tag on the tests
  without a pipe in between (`uv run pytest -q && ...`, or `set -o pipefail`): `pytest | tail` returns tail's 0,
  and 0.2.23 was tagged with a failing test (2026-10-06). While developing, use
  `uv tool install --editable .` so the commands on PATH run the checkout.
- Tests: `uv run pytest` (offline: fake MPD, temporary data dir, no network). Add a test for every bug that
  could silently miscount or mismatch a song.
- Check with `uvx pyflakes src` and run the changed command against MPD; `musicdb sync` writes stickers to the real
  MPD, so test with `MUSICDB`, `MUSICDB_DATA` and `MPD_PORT` pointing at a scratch DB, directory and MPD.
