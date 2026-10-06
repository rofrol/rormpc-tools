# Agent guide

- Read README.md first: what each tool does and the invariants to keep.
- Commits carry "(AI-assisted)" when an agent wrote them.
- Nothing personal in this public repository: no account names, private paths, tokens or listening data, not in
  defaults, tests, examples or commit messages. Personal values belong in the user's
  `~/.config/rormpc-tools/config.toml`.
- Release: bump `version` in pyproject.toml, tag `vX.Y.Z`, push the tag, then bump `RORMPC_TOOLS_TAG` in rormpc's
  `scripts/rormpc_install.sh` and run `rormpc_install.sh companions` there. While developing, use
  `uv tool install --editable .` so the commands on PATH run the checkout.
- Tests: `uv run pytest` (offline: fake MPD, temporary data dir, no network). Add a test for every bug that
  could silently miscount or mismatch a song.
- Check with `uvx pyflakes src` and run the changed command against MPD; `musicdb sync` writes stickers to the real
  MPD, so test with `MUSICDB`, `MUSICDB_DATA` and `MPD_PORT` pointing at a scratch DB, directory and MPD.
