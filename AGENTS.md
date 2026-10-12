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

## Coordination

- Since 2026-10-12 this repository has its own `TODO.md` and its own coordinator (herdr agent name
  `todo-rormpc-tools`), decided by the user (d-33kat4ps), following herdr's rule of one coordinator per git
  repository. rormpc's coordinator (`todo-rormpc`) used to own it; its history of rormpc-tools work is in rormpc's
  `TODO.md`.
- Cross-repository work: rormpc's coordinator sends this repository's coordinator a plain message; the receiver
  decides, queues the item here, and replies "landed <tag>" when it is released. rormpc's consuming item waits in
  rormpc's `TODO.md` as "Waiting on rormpc-tools". Releases (version bump, tag, push) belong to this repository;
  rormpc's install script pins the tag (`RORMPC_TOOLS_TAG`, see Release above).
- `herdr todo run` refuses this repository today ("the repository has no master commit"; the branch is `main`;
  herdr report r-12, a fix is queued in herdr). Until herdr supports `main`, the coordinator records the base
  (`git rev-parse main`), starts a headless worker with `herdr worker start --item <t-id> --folder-slot worker
  --branch todo/<slug> --base <sha> ...`, waits with `herdr worker wait <id> --attention` through herdr-job, then
  runs `herdr worker stop` and `herdr worker verify <id> --base <sha> --message <subject> --paths <globs>
  --cmd "uv run pytest -q && uvx pyflakes src"`. It cherry-picks onto `main` only on exit 0 (verified), then runs
  the tests on `main`. A second slot (`--folder-slot worker2`) runs an independent item in parallel.
- Headless workers run sandboxed: `uv run`/`uvx` cannot write uv's cache, the unix-socket tests in
  `tests/test_control.py` and `tests/test_nowplaying.py` fail with PermissionError, and there is no network. The
  coordinator's verify (outside the sandbox) is the real test run; a worker lists what it could not run.
- `herdr worker verify --message` and `herdr todo run` take only a lowercase conventional commit subject: the type
  is one of `feat`, `fix`, `docs`, `refactor`, `test`, `chore`, `perf`, `build`, `ci`, `style`, `revert`, the
  description does not start with a capital. The area goes in the scope: `feat(hits): …`, `fix(player): …`,
  `chore(tools): 0.2.54 (AI-assisted)`; `hits: …` is refused.
