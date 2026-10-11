"""Banned songs: a lasting "never play, never filter in" (decided 2026-10-11), separate from Deleted.

The file stays on disk and ListenBrainz is untouched (no listens deleted, no feedback). The state is an append-only
event log in data_dir, `banned.jsonl`, one line per ban or unban:
{"ts", "action": "ban"|"unban", "key" (identity.key, as likes.jsonl and tag lists use it), "file", "artist",
"title", "op" (one id per command, so an undo takes back a whole ban command), "undo" (on an unban: the op it
undid)}. The last event per key wins.

- `musicdb ban|unban [FILE...]` (FILE, else $SELECTED_SONGS, else $FILE, as `musicdb delete` takes them),
  `musicdb ban --undo` (unban the songs of the newest ban still in effect; repeat to go further back),
  `musicdb banned [--json]` (the current list).
- `hits` drops banned songs from every result after the ranks are set (like hidden chart songs: ranks never move).
- mpd-player never plans a banned song, even when it is in the queue (it reads this log, by file path).

This module reads and writes the log only; it imports nothing heavy, because mpd-player reads it too.
"""
import datetime as dt, fcntl, json, uuid

from . import settings

DATA = settings.DATA_DIR


def log_path():
    return DATA / "banned.jsonl"


def events():
    p = log_path()
    if not p.exists():
        return []
    out = []
    for line in p.read_text().splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue  # a torn last line (a crash mid-append) is not a ban
    return out


def fold(evs=None):
    """{key: the ban event in effect}, in the log's order (the newest ban last): the last event per key wins."""
    state = {}
    for e in events() if evs is None else evs:
        if e.get("action") == "ban":
            state.pop(e["key"], None)
            state[e["key"]] = e
        elif e.get("action") == "unban":
            state.pop(e.get("key"), None)
    return state


def current():
    """The banned songs, newest ban first."""
    return list(reversed(fold().values()))


def files(evs=None):
    """The file paths of the songs banned now."""
    return {e["file"] for e in fold(evs).values() if e.get("file")}


def append(rows):
    p = log_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)  # rormpc and a terminal may both write
        f.write("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))


def now():
    return dt.datetime.now().isoformat(timespec="seconds")


def ban(refs):
    """Ban the songs (tags.song_ref rows: key, file, artist, title); returns the ones newly banned."""
    state, op, ts = fold(), uuid.uuid4().hex, now()
    rows = [{"ts": ts, "action": "ban", "key": r["key"], "file": r["file"], "artist": r.get("artist") or "",
             "title": r.get("title") or "", "op": op} for r in refs if r["key"] not in state]
    if rows:
        append(rows)
    return rows


def unban(refs, undo=None):
    """Unban the songs (rows with a key); returns the unban events written for the ones that were banned."""
    state, ts = fold(), now()
    rows = []
    for r in refs:
        e = state.get(r["key"])
        if e:
            rows.append({"ts": ts, "action": "unban", "key": e["key"], "file": e.get("file"),
                         "artist": e.get("artist", ""), "title": e.get("title", ""), "op": uuid.uuid4().hex}
                        | ({"undo": undo} if undo else {}))
    if rows:
        append(rows)
    return rows


def undo_last():
    """Unban the songs of the newest ban command whose bans are still in effect; [] when there is none."""
    state = fold()
    newest = next((e for e in reversed(state.values()) if e.get("op")), None)
    if newest is None:
        return []
    return unban([e for e in state.values() if e.get("op") == newest["op"]], undo=newest["op"])


def matches(state, file, mbid=None):
    """True when the library file `file` is banned in `state` (fold()): by its path or its song key (a YouTube id
    or MBID survives a rename)."""
    from . import identity
    keys = {e["key"] for e in state.values()}
    by_file = {e.get("file") for e in state.values()}
    return file in by_file or identity.key(file) in keys or (mbid is not None and identity.key(file, mbid) in keys)
