# rormpc-tools

The MPD music tools behind [rormpc](https://github.com/rofrol/rormpc)'s Hits pane, play counts and delete menu.
They work from the shell too; each one's usage is in its `--help`.

| Command | What it does |
|---|---|
| `hits` | Billboard year-end chart hits by decade or years (a seed of the MusicBrainz matches ships with the package, see `src/rormpc_tools/data/LICENSES.md`; the hourly `musicdb update` fills gaps, e.g. a new chart year, 30 lookups at a time), genre filter (MusicBrainz), what you own; `--json` feeds rormpc's Hits pane, `--playlist` writes an MPD playlist; `hits fetch` is a verified import queue for the missing ones; `hits genres` counts the library's genres and pins the Hits checkboxes, `hits genres of FILE` lists one song's |
| `musicdb` | play history (ListenBrainz, MPD log, Takeout, Spotify export) -> MPD stickers `plays`, `lastPlayed`, `skips` and mpd-player's shuffle weights; likes to ListenBrainz; `delete` / `undo` behind rormpc's Ctrl-x / Ctrl-y |
| `musicdb chart` | a standalone HTML page: my top 10 of each listening year as an animated bar chart race (the weighted shuffle's own picks left out), how my most played songs rose and fell (top 10 ranks, top 100 shares), which source the plays come from |
| `musicdb lyrics` | lyrics from LRCLIB into `lyrics_dir` (`.lrc` synced, `.txt` plain) for rormpc's Lyrics pane; `candidates` / `use` pick another entry; `translate` takes one song's Polish translation from tekstowo.pl on request (personal use: one song per call, cached in `<song>.pl.json`, never committed anywhere), or when it has none a literal line-by-line machine translation by Claude through the Claude Code CLI (`claude -p` with its own login, no API key; the lyrics go to Anthropic; model: `translate_model`), `lang` overrides the detected language |
| `mpd-player` | the playback daemon (runs with rormpc closed): silence between songs, Up next, weighted shuffle by plays and likes with "heard enough" cooldowns, pause for a while (plays on at a wall-clock deadline unless anyone did anything meanwhile); commands over MPD messages on channel `rormpc`, see its `--help` |
| `yt-mp3-mb` | YouTube -> mp3 identified on MusicBrainz, tagged, cover embedded; `--batch --json` for programs: no questions, uncertain matches left for review, a rerun skips what the target dir has |
| `liveplaylist` | a public YouTube playlist as a "live" MPD playlist (rormpc's Live playlists pane): `add URL`, `check` for new tracks, `accept` / `reject` them, `download`, `list`; every command takes `--json` |
| `yt-playlist` | your YouTube playlists through the YouTube Data API (OAuth), for removing deleted songs |

### hits: sets, ranks, exceptions and smart lists

Selection = (union of `+` sets, or the whole library when no set is `+`) − (union of `-` sets) ∩ period ∩ genres
∩ artists ∩ Top % ∩ owned. Top % is cut in the rank's own population (the chart songs or the library songs of the
period), before sets, genres and artists, so a song's rank never depends on them.

- `--set ±KIND`, repeatable: `billboard` (US year-end charts), `likes` (rmpc's like sticker), `playlists` (your MPD
  playlists except the generated ones), `recommended` (artists similar to your most played, ListenBrainz Radio).
  Named sets, one per `--set`: `tag:NAME`, `playlist:NAME`, `live:ID`, `list:ID|NAME`; `hits sets` lists them with
  their sizes. A missing one is an error, never an empty set.
- `--rank billboard|plays|rediscover|none`: best year-end position; your plays; often played, not lately; no
  ranking and no Top %. My plays and rediscover leave out the weighted shuffle's own picks. Default: `billboard`
  with `+billboard`, `none` for `+recommended` alone, else `plays`.
- `--years-of release|chart|listened`: which years the period means; the default follows `--rank` (billboard ->
  chart, plays -> listened, else release). `--source` is the old shorthand, mapped onto these three.
- `hits except pin|exclude|remove --scope library|set:KIND[:NAME]|list:ID --file PATH` (or `--id`,
  `--chart-key`): a pin ✚ keeps an owned song in whatever the filters say (unranked, after the ranked rows), an
  exclusion ⊘ takes it out and beats any pin. A set scope applies only while that set is `+`, a list scope only
  while that smart list is open. `hits exceptions` lists them, `hits hide` included; `--show-excluded` keeps the
  excluded songs in the result, marked ⊘.
- `hits lists [create|update|rename|duplicate|delete|export]`: smart lists, the filter options saved under a name
  (`<data_dir>/smartlists.jsonl`). `export` (also hourly in `musicdb update`) writes each as the MPD playlist
  "Smart NAME", a snapshot never read back as rules. `--list NAME` runs one with its exceptions, `--rules FILE` runs
  a rules object from a file.

```sh
hits 1980s --set +billboard --set +likes --set -playlists --top 1-10   # (Billboard ∪ Likes) − Playlists
hits --years 1990-1999 --rank plays --years-of release --top 1-10     # my most played songs released then
hits --set +likes --rank rediscover
hits sets; hits exceptions; hits lists                                 # read only
```

## Install

    uv tool install 'rormpc-tools @ git+https://github.com/rofrol/rormpc-tools@v0.1.6'

rormpc's `scripts/rormpc_install.sh companions` installs the pinned version and runs `musicdb update` hourly
(and the scrobbler and `mpd-player`) as launchd agents or systemd user units. For a checkout:
`uv tool install --editable .`.

Needs MPD with `sticker_file` set (stickers hold the counts) and the programs under [Dependencies](#dependencies).
When neither the video's MusicBrainz link nor AcoustID knows a song, `yt-mp3-mb`
asks Shazam through `shazamio` (an unofficial API: answers are cached, a Shazam-only match is always confirmed by
you, never written on its own). ListenBrainz features read the token from the
[listenbrainz-mpd](https://codeberg.org/elomatreb/listenbrainz-mpd) config (or `$LISTENBRAINZ_TOKEN`), and the calls
that concern your account (token check, listens, likes, imports, deletions, recommendation playlists) go to that
config's `api_url`, like the scrobbler's, else to the public ListenBrainz. Anonymous lookups in `hits` (popularity,
Radio) always use the public ListenBrainz.

### YouTube playlists (optional)

When rormpc's delete menu deletes a song's history, `musicdb` can also remove the video from your YouTube
playlists. It is off until you set it up once (`yt-playlist --help` has the details):

1. In Google Cloud, a project with "YouTube Data API v3" enabled, an OAuth consent screen in "Testing" with
   yourself as a test user, and an OAuth client of type "Desktop app"; save its JSON as `youtube-client.json`
   in `secrets_dir` (see the settings).
2. `yt-playlist auth` (opens the browser), then `yt-playlist list` and `yt-playlist use ID ...` to choose the
   music playlists to clean up.

A "Testing" app's login expires after 7 days; the next run from a terminal logs in again.

## Dependencies

A missing program ends a command with one line: `<tool>: <program> not found on PATH; <what needs it>. Install:`
and a link here. An optional one is reported once per run with what stops working. The tools pick no package
manager for you; take the package from your system's column.

| Program | What needs it | | Homebrew (macOS) | Debian/Ubuntu (`apt`) | Arch (`pacman`) | Guix |
|---|---|---|---|---|---|---|
| `mpd` | everything (the stickers hold the counts) | required | `mpd` | `mpd` | `mpd` | `mpd` |
| `mpc` | MPD database updates, the current song | required | `mpc` | `mpc` | `mpc` | `mpd-mpc` |
| `ffmpeg` | audio hashes, covers, mp3 conversion (`yt-mp3-mb`, `musicdb update`) | required | `ffmpeg` | `ffmpeg` | `ffmpeg` | `ffmpeg` |
| `yt-dlp` | YouTube searches and downloads (`yt-mp3-mb`, `liveplaylist`, `hits fetch`) | required for those | `yt-dlp` | `yt-dlp` ¹ | `yt-dlp` | `yt-dlp` |
| `git` | commits of the history directory | required | `git` | `git` | `git` | `git` |
| `fpcalc` | AcoustID matching, the Versions audio comparison | optional | `chromaprint` | `libchromaprint-tools` | `chromaprint` | `chromaprint` |
| `rsgain` | ReplayGain tags on downloads | optional | `rsgain` | `rsgain` ² | `rsgain` ² | `rsgain` |
| `terminal-notifier` | failure notifications on macOS (else `osascript`) | optional | `terminal-notifier` | — | — | — |
| `notify-send` | failure notifications on Linux | optional | — | `libnotify-bin` | `libnotify` | `libnotify` |
| C compiler, `pkg-config`, OpenSSL and SQLite headers | building the scrobbler (rormpc's `rormpc_install.sh companions`) on Linux | required there | — | `build-essential pkg-config libssl-dev libsqlite3-dev` | `base-devel openssl sqlite` | `gcc-toolchain pkg-config openssl sqlite` |

- Homebrew: `brew install <package>`; Debian/Ubuntu: `sudo apt install <package>`; Arch: `sudo pacman -S <package>`.
- Guix: `guix install <package>` now; to keep it, add the package to your Guix Home `home.scm` (`packages`) or a
  manifest (`guix package -m manifest.scm`).
- ¹ Debian's and Ubuntu's `yt-dlp` can lag behind YouTube's changes; `uv tool install yt-dlp` gives the current one.
- ² Not verified: whether this package exists in that distribution's main repositories (`rsgain` may be in Arch's
  AUR only). Check with your package manager's search.
- Windows is not supported.

## Settings

`~/.config/rormpc-tools/config.toml`, see [config.toml.sample](config.toml.sample): the music directory, where the
history is kept, the ListenBrainz user (default: the token's owner) and when your scrobbler started.

## Invariants

- Queue plan edits are daemon-owned priorities, never MPD queue moves. `shuffle swap VERSION ID_A ID_B TOKEN`
  refreshes actual MPD state, checks the session/revision and adjacent live forecast IDs, then publishes priorities
  and reconciles again (MPD can advance during the writes) before an acknowledgement in `shuffle.json`
  (`ack: {token, ok, error, version}` on success). Heartbeats do not
  change `plan_version`; membership, order or source do. Rejected versions are not automatically retried.
  A temporary patch retains the original order of survivors until the next draw (including per-song top-up),
  a planned entry playing/leaving, reroll/new round/source change, or daemon restart. `patch_base` allows a
  restart to restore the canonical order; it never resumes an old patch. Partial priority failure publishes
  `publish_error`, restores in-memory order, and the ordinary next status reconciliation publishes recovery
  immediately when priorities again match. `updated_at` is refreshed at the existing `MAX_WAIT` (30 s) wake;
  UI freshness lasts two heartbeats (60 s), and requires the live daemon channel as well. `pid` is the daemon's
  process: MPD sends no Subscription event when a client disconnects, so rormpc watches that process exit.

- An Up next request becomes playing only after MPD accepts `playid`. A rejected start keeps the waiting
  request and previous playback intact and publishes `upnext.json` with an `error` for rormpc's Up next pane.
  Newly added Play now songs are registered as waiting before the start, including their priority;
  a failed start is never mistaken for a completed play at the next daemon wake. There is no automatic retry.
- Every waiting Up next entry carries an MPD priority (random on or off) before `upnext.json` lists it, and the
  song playing now is never given one back: MPD resets a song's priority when it starts, so a waiting entry at 0
  has started, also when it was skipped past between two wakes or played while mpd-player was down. An entry
  re-found by file under a new id (MPD restart, replaced queue) is not judged and stays waiting.
- With the weighted shuffle on, Previous goes through mpd-player, never MPD's `previous` (whose change of song the
  daemon and the scrobbler would count as a skip): a client sends `mpc sendmessage rormpc "shuffle prev"`
  (optionally a command id after `prev`). The daemon walks back through the songs that really played, by queue id;
  leaving a song that way is neutral (no skip, rest or weight change). Each move is appended to `prev.jsonl` in
  the daemon's state dir before `playid` and confirmed or failed after the transition is observed;
  `musicdb import-skips` drops a scrobbler skip of the same song within 2 s of a move that did not fail. With the
  shuffle off, `shuffle prev` sends MPD's own `previous`.
- The `plays` and `skips` stickers are space-padded on purpose: rmpc sorts sticker values as text. Do not "clean up"
  the padding.
- `mbtag.http()` returns `None` on 404 and, by default, when retries run out; with `strict=True` it raises instead.
  `musicdb import-lb` uses `strict=True` so a failed page is never read as "end of history" (a silent partial import).
- Deleting is two-phase in effect: `musicdb delete` trashes and journals (`musicdb undo` restores); remote history
  (ListenBrainz listens, YouTube playlist entries) goes only with `--listenbrainz`, and failed remote steps are
  retried by `musicdb update`. Deleted events become tombstones that re-imports skip. Listens of a recording that
  another library file still has are not deleted.
- `hits fetch` puts a download into the library only when its MusicBrainz recording is the chart's own; everything
  else waits in review outside the music dir. It never retags a file to make it agree on its own; `accept
  --as-chart` (the person's decision) writes the chart's artist and title and keeps the YouTube channel, video title
  and URL in a comment, never the chart's recording MBID. Plain `accept` keeps the current tags, and `accept` fails
  (the item stays in review) when the staged file is gone. Rejected songs stay in the queue file so they are never
  fetched again; their video ids are stored on the item, so `retry` and `another` take the next search result.
- Likes: rmpc's `like` sticker is the source of truth; only changes go to ListenBrainz, and a song without a like
  sticker never clears LB feedback.
- Skips stay local: nothing about them is sent to ListenBrainz.
- Plays of MPD songs come from ro-listenbrainz-mpd's local `listens.jsonl`, so counts stay current when
  ListenBrainz is down. The same listen imported back from ListenBrainz has the same timestamp and is counted
  once; it is kept because deleting the history needs its msid. `import-lb` reads only what is new (7 days of
  overlap for listens the scrobbler's offline cache submits late), and a failed `import-lb` no longer stops
  `update` from syncing.
- One ListenBrainz listen is one event, identified by (second, artist, title), not by MBID: LB maps listens to
  recordings later, and a re-read copy with the new MBID is merged into the stored one (`merge_lb_copies`).
- Stored timestamps are naive local time in `history_timezone` (default: the system zone); convert only through
  `local_ts` / `epoch_of`, never `fromtimestamp` / `.timestamp()` directly, or a zone change double counts.
- Every tool's "same song" by name goes through `mbtag.main_artist` + `mbtag.norm` (`musicdb.name_key`,
  `hits.hide_key`): don't add another normalisation. A name shared by several files is credited to none.
- MPD keys stickers by path: `sync` snapshots like stickers with the song's YouTube id and MBID into
  `likes.jsonl` in data_dir, so a move or rename can put them back.
- Every library file has an id (a UUID of the file, not of a song): `songs.jsonl` in data_dir is the registry
  (current path, every old path, YouTube id, MBID, audio hash, live/gone/merged), and the id plus the YouTube id
  are also in the file's tags (MP3 TXXX "rormpc Song ID" / "YouTube ID", FLAC RORMPC_SONG_ID / YOUTUBE_ID), so
  `musicdb identity sync` (hourly in `update`) recognises a file renamed or moved by anything. MPD does not show
  custom tags: tools ask `identity.ytid(path)` / `identity.key(path)`, never parse file names; the name pattern
  lives only in identity.py (a test enforces it). A copy carrying the same id is reported, never merged.
- Merged or moved files are recorded in `aliases.jsonl` in data_dir (old path -> current path); history and
  logs keep the path they were written with and readers map it through `musicdb.canon()`. `musicdb dedupe`
  keeps one file per identical audio stream and writes these aliases.
- A play matched only by a name that several files share is credited to none, until `musicdb versions`
  decides it per source track (Spotify URI, recording MBID, or the name for id-less plays): a file, or "a
  version I don't own". Decisions live in `versions.jsonl` (append-only) with the file's identity and the
  group's files at the time; a gone file or a changed group sends the decision back for review, never to a
  guessed file. Suggestions (markers, Spotify album, longest play vs file length) are shown, never applied.
- Files of a Versions group (never the whole library) are compared by audio: chromaprint of the first 120 s
  (`fpcalc -raw`), the share of equal bits at the best offset within ~7 s, over the overlap only. A pair at
  0.88 or more is suggested as one recording ("Same recording? audio match 93%", with the file to keep and why),
  0.72-0.88 is shown as similar audio; nothing is merged without a confirmation. `musicdb versions --json` only
  reads the fingerprint cache (~/.cache/rormpc-tools/fingerprints.json, per path, size and mtime) and lists the
  files still missing; `musicdb versions fingerprint` (hourly in `update`, and from rormpc's Versions pane)
  fills it. Needs fpcalc (chromaprint).
- `musicdb update` pauses its ListenBrainz steps after a failed run (1, 2, 4 ... 12 hours, state in
  ~/.cache/rormpc-tools/lb-backoff.json); the local steps (scrobbler log, stickers, export, doctor) run every hour.
- `musicdb doctor` lists silent data errors (duplicate listens, a song in several files, plays credited to no
  file, paths that no longer exist); run it after anything that changes history or moves files.
- `liveplaylist` keeps a decision (pending / accepted / rejected) apart from a job state (queued / downloading /
  needs_match / ready / failed) per playlist item, in `<data_dir>/liveplaylists/<id>.json` (atomic writes, a lock
  in `~/.cache/rormpc-tools/liveplaylist`, progress of the running command in `status.json` there). Every item is
  reviewed, the first import too; nothing runs on a timer. Rejects are durable. An accepted song already in the
  library is referenced only on a confirmed match (the same YouTube id in songs.jsonl, or the one recording
  MusicBrainz links to the video and a library file carries), never by title. A download whose MusicBrainz match
  is uncertain waits outside the music dir (needs_match). The `.m3u` holds accepted, ready, still listed items in
  the playlist's order (file names carry no position). Nothing deletes a file; a failed, partial or empty listing
  marks nothing gone; an item that comes back is active again with its old decision. SIGTERM (rormpc's cancel)
  kills yt-dlp and queues the item again.
- Personal data (play history, exports, OAuth secrets, account names) never goes into this repository: it is
  public. Paths and accounts come from the settings, with defaults that assume nothing about the user.
