# rormpc-tools

The MPD music tools behind [rormpc](https://github.com/rofrol/rormpc)'s Hits pane, play counts and delete menu.
They work from the shell too; each one's usage is in its `--help`.

| Command | What it does |
|---|---|
| `hits` | Billboard year-end chart hits by decade or years, genre filter (MusicBrainz), what you own; `--json` feeds rormpc's Hits pane, `--playlist` writes an MPD playlist |
| `musicdb` | play history (ListenBrainz, MPD log, Takeout, Spotify export) -> MPD stickers `plays`, `lastPlayed`, `skips`; likes to ListenBrainz; `delete` / `undo` behind rormpc's Ctrl-x / Ctrl-y |
| `mpd-gap` | seconds of silence between songs |
| `yt-mp3-mb` | YouTube -> mp3 identified on MusicBrainz, tagged, cover embedded |
| `yt-playlist` | your YouTube playlists through the YouTube Data API (OAuth), for removing deleted songs |

## Install

    uv tool install 'rormpc-tools @ git+https://github.com/rofrol/rormpc-tools@v0.1.6'

rormpc's `scripts/rormpc_install.sh companions` installs the pinned version and runs `musicdb update` hourly
(and the scrobbler and `mpd-gap`) as launchd agents or systemd user units. For a checkout:
`uv tool install --editable .`.

Needs MPD with `sticker_file` set (stickers hold the counts), `mpc`; `yt-mp3-mb` needs `yt-dlp` and `ffmpeg`,
`fpcalc` (AcoustID) is optional. ListenBrainz features read the token from the
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

- The `plays` and `skips` stickers are space-padded on purpose: rmpc sorts sticker values as text. Do not "clean up"
  the padding.
- `mbtag.http()` returns `None` on 404 and, by default, when retries run out; with `strict=True` it raises instead.
  `musicdb import-lb` uses `strict=True` so a failed page is never read as "end of history" (a silent partial import).
- Deleting is two-phase in effect: `musicdb delete` trashes and journals (`musicdb undo` restores); remote history
  (ListenBrainz listens, YouTube playlist entries) goes only with `--listenbrainz`, and failed remote steps are
  retried by `musicdb update`. Deleted events become tombstones that re-imports skip. Listens of a recording that
  another library file still has are not deleted.
- Likes: rmpc's `like` sticker is the source of truth; only changes go to ListenBrainz, and a song without a like
  sticker never clears LB feedback.
- Skips stay local: nothing about them is sent to ListenBrainz.
- Plays of MPD songs come from ro-listenbrainz-mpd's local `listens.jsonl`, so counts stay current when
  ListenBrainz is down. The same listen imported back from ListenBrainz has the same timestamp and is counted
  once; it is kept because deleting the history needs its msid. `import-lb` reads only what is new (7 days of
  overlap for listens the scrobbler's offline cache submits late), and a failed `import-lb` no longer stops
  `update` from syncing.
- Personal data (play history, exports, OAuth secrets, account names) never goes into this repository: it is
  public. Paths and accounts come from the settings, with defaults that assume nothing about the user.
