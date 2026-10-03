"""Settings shared by the tools.

Read from $RORMPC_TOOLS_CONFIG or ~/.config/rormpc-tools/config.toml (see config.toml.sample); an environment
variable named next to each setting wins over the file. The defaults assume nothing about the user: no account
names, no private repositories.
"""
import os, pathlib, sys, tomllib

HOME = pathlib.Path.home()
XDG_CONFIG = pathlib.Path(os.environ.get("XDG_CONFIG_HOME", HOME / ".config"))
XDG_DATA = pathlib.Path(os.environ.get("XDG_DATA_HOME", HOME / ".local/share"))
XDG_CACHE = pathlib.Path(os.environ.get("XDG_CACHE_HOME", HOME / ".cache"))
CONFIG_FILE = pathlib.Path(os.environ.get("RORMPC_TOOLS_CONFIG", XDG_CONFIG / "rormpc-tools/config.toml"))
_file = tomllib.loads(CONFIG_FILE.read_text()) if CONFIG_FILE.exists() else {}


def _get(key, env, default):
    value = os.environ.get(env) or _file.get(key)
    return default if value in (None, "") else value


def _path(key, env, default):
    return pathlib.Path(_get(key, env, default)).expanduser()


# MPD's music_directory: where yt-mp3-mb saves and musicdb deletes files
MUSIC_DIR = _path("music_dir", "YTMB_MUSIC_DIR", HOME / "Music")
MPD_PLAYLISTS = _path("mpd_playlists", "MPD_PLAYLISTS", XDG_CONFIG / "mpd/playlists")
MPD_LOG = _path("mpd_log", "MPD_LOG", XDG_CONFIG / "mpd/log")
# play history as JSONL (the source of truth); musicdb commits it when this is a git repository
DATA_DIR = _path("data_dir", "MUSICDB_DATA", XDG_DATA / "rormpc-tools/data")
# SQLite cache rebuilt from DATA_DIR when missing
DB_FILE = _path("db_file", "MUSICDB", XDG_DATA / "rormpc-tools/plays.db")
# YouTube OAuth client and token for yt-playlist
SECRETS_DIR = _path("secrets_dir", "MUSICDB_SECRETS", XDG_DATA / "rormpc-tools/secrets")
# ListenBrainz user whose listens musicdb imports; empty: the owner of the ListenBrainz token
LB_USER = _get("lb_user", "LB_USER", None)
# when the scrobbler started: MPD log plays before it, ListenBrainz listens after it; empty: ListenBrainz only
LB_SINCE = _get("lb_since", "LB_CUTOFF", None)
# listens and skipped songs logged by ro-listenbrainz-mpd, next to its submission cache
SCROBBLER_DIR = _path("scrobbler_dir", "MUSICDB_SCROBBLER_DIR",
                      (HOME / "Library/Application Support" if sys.platform == "darwin" else XDG_DATA) / "listenbrainz-mpd")
LISTENS_LOG = SCROBBLER_DIR / "listens.jsonl"
SKIPS_LOG = SCROBBLER_DIR / "skips.jsonl"
# contact in the User-Agent that MusicBrainz asks for: an e-mail or URL
CONTACT = _get("contact", "RORMPC_TOOLS_CONTACT", "https://github.com/rofrol/rormpc-tools")
