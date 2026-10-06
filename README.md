# rormpc-tools

The MPD music tools behind [rormpc](https://github.com/rofrol/rormpc)'s Hits pane, play counts and delete menu.
They work from the shell too; each one's usage is in its `--help`.

| Command | What it does |
|---|---|
| `hits` | Billboard year-end chart hits by decade or years (a seed of the MusicBrainz matches ships with the package, see `src/rormpc_tools/data/LICENSES.md`; the hourly `musicdb update` fills gaps, e.g. a new chart year, 30 lookups at a time), genre filter (MusicBrainz), what you own; `--json` feeds rormpc's Hits pane, `--playlist` writes an MPD playlist; `hits fetch` is a verified import queue for the missing ones; `hits genres` counts the library's genres and pins the Hits checkboxes, `hits genres of FILE` lists one song's |
| `musicdb` | play history (ListenBrainz, MPD log, Takeout, Spotify export) -> MPD stickers `plays`, `lastPlayed`, `skips` and mpd-player's shuffle weights; likes to ListenBrainz; `delete` / `undo` behind rormpc's Ctrl-x / Ctrl-y |
| `musicdb chart` | a standalone HTML page: my top 10 of each listening year as an animated bar chart race (the weighted shuffle's own picks left out), how my most played songs rose and fell (top 10 ranks, top 100 shares), which source the plays come from |
| `musicdb lyrics` | lyrics from LRCLIB into `lyrics_dir` (`.lrc` synced, `.txt` plain) for rormpc's Lyrics pane; `candidates` / `use` pick another entry |
| `mpd-player` | the playback daemon (runs with rormpc closed): silence between songs, Up next, weighted shuffle by plays and likes with "heard enough" cooldowns, mute for a while (the volume comes back at a wall-clock deadline); commands over MPD messages on channel `rormpc`, see its `--help` |
| `yt-mp3-mb` | YouTube -> mp3 identified on MusicBrainz, tagged, cover embedded |
| `yt-playlist` | your YouTube playlists through the YouTube Data API (OAuth), for removing deleted songs |

## Install

    uv tool install 'rormpc-tools @ git+https://github.com/rofrol/rormpc-tools@v0.1.6'

rormpc's `scripts/rormpc_install.sh companions` installs the pinned version and runs `musicdb update` hourly
(and the scrobbler and `mpd-player`) as launchd agents or systemd user units. For a checkout:
`uv tool install --editable .`.

Needs MPD with `sticker_file` set (stickers hold the counts), `mpc`; `yt-mp3-mb` needs `yt-dlp` and `ffmpeg`,
`fpcalc` (AcoustID) and `rsgain` (ReplayGain tags for MPD's `replaygain "track"`) are optional. When neither the video's MusicBrainz link nor AcoustID knows a song, `yt-mp3-mb`
asks Shazam through `shazamio` (an unofficial API: answers are cached, a Shazam-only match is always confirmed by
you, never written on its own). ListenBrainz features read the token from the
[listenbrainz-mpd](https://codeberg.org/elomatreb/listenbrainz-mpd) config (or `$LISTENBRAINZ_TOKEN`).

### YouTube playlists (optional)

When rormpc's delete menu deletes a song's history, `musicdb` can also remove the video from your YouTube
playlists. It is off until you set it up once (`yt-playlist --help` has the details):

1. In Google Cloud, a project with "YouTube Data API v3" enabled, an OAuth consent screen in "Testing" with
   yourself as a test user, and an OAuth client of type "Desktop app"; save its JSON as `youtube-client.json`
   in `secrets_dir` (see the settings).
2. `yt-playlist auth` (opens the browser), then `yt-playlist list` and `yt-playlist use ID ...` to choose the
   music playlists to clean up.

A "Testing" app's login expires after 7 days; the next run from a terminal logs in again.

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
  Newly added Play now songs are registered as waiting before the start, including their random-on priority;
  a failed start is never mistaken for a completed play at the next daemon wake. There is no automatic retry.
- The `plays` and `skips` stickers are space-padded on purpose: rmpc sorts sticker values as text. Do not "clean up"
  the padding.
- `mbtag.http()` returns `None` on 404 and, by default, when retries run out; with `strict=True` it raises instead.
  `musicdb import-lb` uses `strict=True` so a failed page is never read as "end of history" (a silent partial import).
- Deleting is two-phase in effect: `musicdb delete` trashes and journals (`musicdb undo` restores); remote history
  (ListenBrainz listens, YouTube playlist entries) goes only with `--listenbrainz`, and failed remote steps are
  retried by `musicdb update`. Deleted events become tombstones that re-imports skip. Listens of a recording that
  another library file still has are not deleted.
- `hits fetch` puts a download into the library only when its MusicBrainz recording is the chart's own; everything
  else waits in review outside the music dir. It never retags a file to make it agree. Rejected songs stay in the
  queue file so they are never fetched again.
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
- `musicdb update` pauses its ListenBrainz steps after a failed run (1, 2, 4 ... 12 hours, state in
  ~/.cache/rormpc-tools/lb-backoff.json); the local steps (scrobbler log, stickers, export, doctor) run every hour.
- `musicdb doctor` lists silent data errors (duplicate listens, a song in several files, plays credited to no
  file, paths that no longer exist); run it after anything that changes history or moves files.
- Personal data (play history, exports, OAuth secrets, account names) never goes into this repository: it is
  public. Paths and accounts come from the settings, with defaults that assume nothing about the user.
