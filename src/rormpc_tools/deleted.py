"""Deleted songs are never downloaded again.

The block list is derived from the deletion journal (`musicdb delete` writes deletions/pending.jsonl and
done.jsonl in data_dir before it touches a file), so every deletion blocks from the moment it is journaled, the ones
made before this module existed included. `musicdb undo` removes a trashed song's record and with it the block.
A deliberate re-download goes through deletions/allowed.jsonl, an append-only log of allow / block events per
journal id (`musicdb deletions allow|block ID`); a later deletion of the same song has a new id and blocks again.

A deletion blocks:
- its YouTube video, in every downloader, before anything is downloaded;
- its recording MBID and its chart key (main artist|title, version notes kept, as `hits hide`), unless the delete
  found another library file of the same recording (a duplicate removed: the recording is still wanted).
The video id is exact. A recording MBID that a download is identified as after the download is never moved into
the library on its own: it waits for review (hits fetch, liveplaylist) or is dropped with the reason (yt-mp3-mb
without a question). Chart keys only gate chart rows (Hits, hits fetch), never a URL someone gave.
A source without video ids (liveplaylist's Omarchy Radio) is blocked by the deleted file's paths before a download
and by its audio hash (the registry's md5) after it.
Older journal records without a video id or MBID take them from the identity registry by the file's path.
"""
import datetime as dt, json

from . import mbtag, musicdb


def allowed_path():
    return musicdb.DATA / "deletions" / "allowed.jsonl"


def chart_key(artist, title):
    """The same key as `hits.hide_key` (that one imports half of hits, this module stays light)."""
    return f"{mbtag.norm(mbtag.main_artist(artist or ''))}|{mbtag.norm(title or '')}"


def allowed():
    """Fold the allow/block log into the set of journal ids allowed to be downloaded again."""
    ids = set()
    for e in musicdb.jsonl(allowed_path()):
        (ids.add if e["action"] == "allow" else ids.discard)(e["id"])
    return ids


def kept_other_copy(r):
    return (r.get("ops") or {}).get("listenbrainz", "").startswith("kept: another file")


def entry(r):
    """One journal record as a block: what it matches by, and when it was deleted."""
    from . import identity
    row = None
    if not (r.get("ytid") and r.get("mbid")):
        try:
            row = identity.resolve(r["file"])
        except Exception:  # a broken registry must not unblock anything the record itself names
            row = None
    row = row or {}
    first = (r.get("events") or [{}])[0]
    artist = r.get("artist") or first.get("artist") or ""
    title = r.get("title") or first.get("title") or ""
    whole = not kept_other_copy(r)
    return {"id": r["id"], "file": r["file"], "deleted_at": r["queued_at"], "mode": r.get("mode", "trash"),
            "ytid": r.get("ytid") or row.get("ytid") or identity.ytid_from_name(r["file"]),
            "mbid": (r.get("mbid") or row.get("mbid")) if whole else None,
            "paths": sorted({r["file"], *row.get("paths", [])}), "md5": row.get("md5"),
            "chart_key": chart_key(artist, title) if whole and artist and title else None,
            "artist": artist, "title": title}


class Blocks:
    """The deleted songs that block downloads (allowed ones left out). Read once per command: a long worker
    calls `fresh()` before each song, so a deletion or an allow made meanwhile counts."""

    def __init__(self):
        self.fresh()

    def fresh(self):
        ok = allowed()
        recs = [r for r in musicdb.jsonl(musicdb.PENDING) + musicdb.jsonl(musicdb.DONE) if r["id"] not in ok]
        self.entries = sorted((entry(r) for r in recs), key=lambda e: e["deleted_at"])
        self.ytid = {e["ytid"]: e for e in self.entries if e["ytid"]}
        self.mbid = {e["mbid"]: e for e in self.entries if e["mbid"]}
        self.chart = {e["chart_key"]: e for e in self.entries if e["chart_key"]}
        self.paths = {p: e for e in self.entries for p in e["paths"]}
        self.md5 = {e["md5"]: e for e in self.entries if e["md5"]}
        return self

    def video(self, ytid):
        return self.ytid.get(ytid) if ytid else None

    def path(self, rel):
        """A file a downloader would write at a path that a deleted file had (sources without a video id)."""
        return self.paths.get(rel) if rel else None

    def audio(self, md5):
        """A download whose audio stream (dedupe.audio_hash) is a deleted file's, under any name."""
        return self.md5.get(md5) if md5 else None

    def recording(self, mbid):
        return self.mbid.get(mbid) if mbid else None

    def chart_row(self, mbid=None, artist=None, title=None):
        """A chart row (Hits, hits fetch): by its recording MBID, else by its chart key."""
        return self.recording(mbid) or (self.chart.get(chart_key(artist, title)) if artist and title else None)


def reason(e):
    """What a downloader shows for a skipped song."""
    return f"deleted on {e['deleted_at'][:10]} (musicdb deletions allow {e['id']} to download it again)"


def mark(e):
    """The short form a JSON row carries."""
    return {"id": e["id"], "deleted_at": e["deleted_at"], "file": e["file"]}


def log(action, rid):
    """Append an allow / block event (the history of the decisions stays reviewable in git)."""
    if action not in ("allow", "block"):
        raise ValueError(action)
    known = {r["id"] for r in musicdb.jsonl(musicdb.PENDING) + musicdb.jsonl(musicdb.DONE)}
    if rid not in known:
        raise SystemExit(f"no deletion with id {rid} (musicdb deletions --json --all)")
    path = allowed_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with musicdb.journal_lock(), path.open("a") as fh:
        fh.write(json.dumps({"ts": dt.datetime.now().isoformat(timespec="seconds"), "action": action, "id": rid},
                            ensure_ascii=False) + "\n")
