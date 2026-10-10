"""musicdb restore: bring a deleted song back with its history, for any deletion (Trash or permanent).

  musicdb restore ID [--json]           # dry run: what will be restored and what cannot; changes nothing
  musicdb restore ID --yes [--json]     # do it; run it again to continue a step that waits or failed
  musicdb restore ID --yes --lb-resubmit   # also send again a ListenBrainz listen whose submission is unconfirmed

ID is a deletion journal id (`musicdb deletions --json --all`). Steps, each journaled in deletions/restored.jsonl
(one record per deletion id, its state per step) before and after it acts, so a crash or a rerun continues:
- journal: a pending deletion record moves to done.jsonl, so the hourly retry never deletes its history again.
- file: back at its original path (MPD playlists, stickers, aliases and the history all name that path): from the
  Trash, else the same YouTube video downloaded again into a staging dir outside the library with the deletion's
  block lifted for that one download. It goes in only when the video id and the duration (within
  DURATION_TOLERANCE_S of what the history logged) agree with the deletion record and the tagger finds no other
  recording than the record's; otherwise it stays staged and the step says why. A download the tagger leaves
  unmatched gets the record's recording MBID, artist and title (the library had identified this video so). No fingerprint of the deleted file exists: the audio is compared by the
  registry's MD5 of the audio stream, and a different encode is restored but reported as such.
- identity: the file carries its old song id again and the registry row is live again (same id, new MD5).
- stickers: the ones saved at the deletion (play counts, like); `musicdb sync` recounts plays from the history.
- local: the journaled play events back into the history, their tombstones gone (one transaction).
- listenbrainz: only the listens the deletion deleted (lb_deleted) are submitted again (listen_type "import", the
  original listened_at). ListenBrainz deletes asynchronously by (user, listened_at, recording_msid), so a listen is
  submitted only once the deleted one is no longer listed at its second; while it is, the step waits. A listen
  already listed at its second with the same artist and title counts as restored. The state is "submitting" before
  the POST and "submitted" after its acknowledgement; an unconfirmed one is never sent again on its own
  (--lb-resubmit does). The local event keeps the msid ListenBrainz lists, so a later delete finds it.
- youtube: the video back into the playlists it was removed from (appended: the position was not kept), skipped
  where it is already there.
- download: `deletions allow ID`, so the downloaders no longer skip the song.
`musicdb update` continues restores whose ListenBrainz or YouTube steps wait or failed. Undoing a restore is a new
deletion (Ctrl-x), with a new journal id.
"""
import argparse, contextlib, datetime as dt, fcntl, json, os, pathlib, shutil, subprocess, sys, urllib.request

from . import deleted, identity, mbtag, musicdb, settings

PLAN_VERSION = 1  # of the --json plan, read by rormpc's Deleted overlay: bump when a field changes meaning
# delay: not a wait, the accepted length difference of a re-download (yt-dlp's mp3 encoder padding)
DURATION_TOLERANCE_S = 2.0
STEPS = ("journal", "file", "identity", "stickers", "local", "listenbrainz", "youtube", "download")
LB_INTERVAL_S = 0.5  # delay: external rate limit, the same ListenBrainz pacing as `import-lb`
LB_CALL_S = 30  # delay: network deadline for one ListenBrainz API call, as in `delete`


def restored_path():
    return musicdb.DATA / "deletions" / "restored.jsonl"


def staging(rid):
    return settings.XDG_CACHE / "rormpc-tools" / "restore" / rid.replace("/", "_")


def now():
    return dt.datetime.now().isoformat(timespec="seconds")


@contextlib.contextmanager
def restore_lock():
    """One restore at a time (rormpc and the hourly update may both run one); deletions keep their own lock."""
    lock = musicdb.DB.parent / "restore.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    with open(lock, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield


def deletion(rid):
    for r in musicdb.jsonl(musicdb.PENDING) + musicdb.jsonl(musicdb.DONE):
        if r["id"] == rid:
            return r
    raise SystemExit(f"no deletion with id {rid} (musicdb deletions --json --all)")


def records():
    return {x["id"]: x for x in musicdb.jsonl(restored_path())}


def save(rec):
    with musicdb.journal_lock():
        rows = records()
        rows[rec["id"]] = rec
        musicdb.write_jsonl(restored_path(), sorted(rows.values(), key=lambda x: x["started_at"]))


def lb_deleted(r):
    """[(event, old msid)] of the listens the deletion deleted on ListenBrainz."""
    gone = set(r.get("lb_deleted") or [])
    out = []
    for e in r.get("events") or []:
        msid = json.loads(e.get("extra") or "{}").get("msid")
        if e["source"] == "lb" and msid and f"{e['ts']} {msid}" in gone:
            out.append((e, msid))
    return out


def expected_duration(r):
    """The song's length as the history logged it (ListenBrainz duration, else the scrobbler's log), or None."""
    for e in r.get("events") or []:
        if e["source"] == "lb" and str(e.get("ms_played") or "").isdigit():
            return int(e["ms_played"]) / 1000
    for x in musicdb.jsonl(musicdb.LISTENS_LOG):
        if x.get("file") == r["file"] and x.get("duration_s"):
            return float(x["duration_s"])
    return None


def same_track(listen, e):
    tm = listen.get("track_metadata") or {}
    return (mbtag.norm(mbtag.main_artist(tm.get("artist_name") or "")) == mbtag.norm(mbtag.main_artist(e["artist"] or ""))
            and mbtag.norm(tm.get("track_name") or "") == mbtag.norm(e["title"] or ""))


def lb_listens_at(epoch):
    """The user's ListenBrainz listens at exactly this second. max_ts is exclusive and pages are newest first, so
    the listens of that second lead the page (a second never holds a page of listens)."""
    user = mbtag.lb_user()
    page = mbtag.http(mbtag.lb_api(f"/1/user/{user}/listens?count=25&max_ts={epoch + 1}"),
                      host_interval=LB_INTERVAL_S, strict=True)
    ls = (page or {}).get("payload", {}).get("listens")
    if not isinstance(ls, list):
        raise RuntimeError("unexpected ListenBrainz response")
    return [x for x in ls if x.get("listened_at") == epoch]


def lb_submit(e):
    tm = {"artist_name": e["artist"], "track_name": e["title"],
          "additional_info": {"submission_client": "musicdb restore", "submission_client_version": settings.version()}}
    if e.get("mbid"):
        tm["additional_info"]["recording_mbid"] = e["mbid"]
    if str(e.get("ms_played") or "").isdigit():
        tm["additional_info"]["duration_ms"] = int(e["ms_played"])
    req = urllib.request.Request(mbtag.lb_api("/1/submit-listens"), method="POST",
                                 data=json.dumps({"listen_type": "import", "payload": [
                                     {"listened_at": int(musicdb.epoch_of(e["ts"])), "track_metadata": tm}]}).encode(),
                                 headers={"Authorization": "Token " + (mbtag.lb_token() or ""),
                                          "Content-Type": "application/json", "User-Agent": mbtag.UA})
    urllib.request.urlopen(req, timeout=LB_CALL_S).read()  # an HTTP error raises: no acknowledgement


def yt(*args):
    """yt-playlist's last JSON line, or raises with its last error line."""
    p = subprocess.run([sys.executable, "-m", "rormpc_tools.yt_playlist", *args], capture_output=True, text=True,
                       stdin=subprocess.DEVNULL)
    if p.returncode:
        raise RuntimeError(((p.stderr or p.stdout).strip().splitlines() or ["yt-playlist failed"])[-1])
    return json.loads(p.stdout.strip().splitlines()[-1])


# ------------------------------------------------------------------ the file

def staged_file(rid, ytid):
    d = staging(rid)
    return next((f for f in sorted(d.glob("*.mp3")) if identity.ytid_from_name(f.name) == ytid), None) if d.is_dir() else None


def recording_of(path):
    import mutagen
    f = mutagen.File(path)
    t = f.tags if f is not None else None
    u = t.get("UFID:http://musicbrainz.org") if t is not None and hasattr(t, "getall") else None
    return u.data.decode("ascii", "replace") if u else None


def length_of(path):
    import mutagen
    f = mutagen.File(path)
    return f.info.length if f is not None else None


def verify(r, path):
    """Why the staged download is not the deleted song, or None. The video id comes from the download's name."""
    if identity.ytid_from_name(path.name) != r.get("ytid"):
        return f"downloaded video {identity.ytid_from_name(path.name)} is not {r.get('ytid')}"
    want, got = expected_duration(r), length_of(path)
    if want and (got is None or abs(got - want) > DURATION_TOLERANCE_S):
        return f"length {got and round(got, 1)} s, the deleted song had {round(want, 1)} s"
    if r.get("mbid") and (got := recording_of(path)) and got != r["mbid"]:
        return f"identified as recording {got}, the deleted song was {r['mbid']}"
    return None


def download(r):
    """yt-mp3-mb into the staging dir, the deletion's block lifted for this one download. Returns the staged file."""
    from . import yt_mp3_mb
    d = staging(r["id"])
    d.mkdir(parents=True, exist_ok=True)
    if not staged_file(r["id"], r["ytid"]):  # a rerun takes the file an interrupted run staged
        rep = yt_mp3_mb.batch([f"https://www.youtube.com/watch?v={r['ytid']}"], str(d), allow_deleted=True,
                              known=({d}, {r["ytid"]}))
        if rep.get("error") or rep.get("failed"):
            raise RuntimeError(rep.get("error") or rep["failed"][0]["error"])
    f = staged_file(r["id"], r["ytid"])
    if not f:
        raise RuntimeError("yt-dlp produced no file")
    return f


def publish(src, dest):
    """Into the library atomically: a temporary name in the target dir, then a rename; never over another file."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f".{dest.name}.restoring")
    shutil.move(str(src), tmp)
    if dest.exists():
        shutil.move(str(tmp), src)
        raise RuntimeError(f"{dest} appeared meanwhile; not overwritten")
    os.replace(tmp, dest)


# ------------------------------------------------------------------ the plan

def file_plan(r, rec):
    dest = musicdb.MUSIC / r["file"]
    row = identity.resolve(r["file"]) or {}
    if rec["ops"].get("file", "").startswith("done"):
        return "done", rec["ops"]["file"]
    if dest.exists():
        sid = None
        with contextlib.suppress(Exception):
            sid = identity.read_tags(r["file"])[0]
        if row.get("id") and sid == row["id"]:
            return "done", "already back at its path"
        return "cannot", f"another file is at {r['file']}; not overwritten"
    if row.get("state") == "live" and row.get("path") and row["path"] != r["file"] and (musicdb.MUSIC / row["path"]).exists():
        return "cannot", f"its song id is live at {row['path']} (moved, not deleted)"
    trashed = r.get("trashed_to")
    if trashed and pathlib.Path(trashed).exists():
        return "will", f"move back from the Trash to {r['file']}"
    if not r.get("ytid"):
        return "cannot", "not in the Trash and not a YouTube download: nothing to download"
    want = expected_duration(r)
    checks = ["video id", f"length {round(want, 1)} s ± {DURATION_TOLERANCE_S:g} s" if want else "length (unknown: not checked)"]
    checks.append(f"recording {r['mbid']}" if r.get("mbid") else "recording (none journaled: not checked)")
    audio = ("audio compared with the deleted file's MD5 (a new encode is reported)" if row.get("md5")
             else "no audio hash of the deleted file: audio not compared")
    staged = " (already downloaded, staged)" if staged_file(r["id"], r["ytid"]) else ""
    return "will", (f"download https://youtu.be/{r['ytid']} again{staged} to {r['file']}, checked: "
                    f"{', '.join(checks)}; {audio}; tags and cover from the tagger (album and cover may differ)")


def plan(r, rec, remote=True):
    """[{"step", "state": will|done|cannot|waiting|unknown|failed|skip, "text"}] for a restore of deletion r.
    remote: look ListenBrainz up (read only) to say whether each deleted listen can be submitted yet."""
    ops, out = rec["ops"], []

    def step(name, state, text):
        out.append({"step": name, "state": state, "text": text})

    pending = any(p["id"] == r["id"] for p in musicdb.jsonl(musicdb.PENDING))
    step("journal", "will" if pending else "done", "stop the hourly retry of its deletion steps" if pending
         else "the deletion is finished: nothing retries it")
    if ops.get("file", "").startswith("failed"):
        step("file", "failed", ops["file"].removeprefix("failed: "))
    else:
        step("file", *file_plan(r, rec))
    row = identity.resolve(r["file"])
    step("identity", "done" if ops.get("identity") == "done" else "will" if row else "skip",
         f"song id {row['id']} again" if row else "no registry row: `musicdb identity sync` gives it a new id")
    st = r.get("stickers") or {}
    step("stickers", "done" if ops.get("stickers") == "done" else "will" if st else "skip",
         ", ".join(f"{k} {v.strip()}" for k, v in st.items()) or "none were saved")
    events = r.get("events") or []
    gone = lb_deleted(r)
    local = [e for e in events if not any(e is g for g, _ in gone)]
    step("local", "done" if ops.get("local") == "done" else "will" if events else "skip",
         f"{len(local)} play events back into the history" if events else "no play events were journaled")
    if r.get("history") != "delete":
        step("listenbrainz", "skip", "the history was kept: nothing was deleted there")
    elif not gone:
        step("listenbrainz", "skip", "no listens were deleted there")
    for e, msid in gone:
        key = f"{e['ts']} {msid}"
        x = (rec.get("lb") or {}).get(key, {})
        what = f"listen of {e['ts'].replace('T', ' ')}"
        if x.get("state") == "done":
            step("listenbrainz", "done", f"{what} is back")
            continue
        if not remote:
            state = x.get("state") or "will"
            step("listenbrainz", "unknown" if state in ("submitting", "submitted") else state,
                 f"{what}: {x.get('note') or x.get('error') or state}")
            continue
        try:
            listed = lb_listens_at(int(musicdb.epoch_of(e["ts"])))
        except (Exception, SystemExit) as err:
            step("listenbrainz", "unknown", f"{what}: ListenBrainz not readable ({err})")
            continue
        if any(l.get("recording_msid") == msid for l in listed):
            step("listenbrainz", "waiting", f"{what}: ListenBrainz still lists the deleted listen (its deletion is "
                 "processed later); submitted once it is gone")
        elif any(same_track(l, e) for l in listed):
            step("listenbrainz", "will", f"{what}: already listed again, only recorded locally")
        elif x.get("state") in ("submitting", "submitted"):
            step("listenbrainz", "unknown", f"{what}: {x['state']} on {x['at']}, not listed yet; never sent "
                 "again on its own (--lb-resubmit)")
        else:
            step("listenbrainz", "will", f"{what}: submitted again with its original time")
    kept = [e for e in events if e["source"] == "lb" and not any(e is g for g, _ in gone)]
    if r.get("history") == "delete" and kept:
        step("listenbrainz", "skip", f"{len(kept)} listens were not deleted there: kept as they are")
    lb_ts = {e["ts"] for e in events if e["source"] == "lb"}
    unseen = [e for e in events if e["source"] == "local" and e["ts"] not in lb_ts]
    for e in unseen if r.get("history") == "delete" and remote else []:
        # a local play whose ListenBrainz copy had not been imported yet: the deletion could not delete it there
        what = f"play of {e['ts'].replace('T', ' ')}"
        try:
            listed = lb_listens_at(int(musicdb.epoch_of(e["ts"])))
        except (Exception, SystemExit) as err:
            step("listenbrainz", "unknown", f"{what}: ListenBrainz not readable ({err})")
            continue
        named = r | {"artist": r.get("artist") or "", "title": r.get("title") or ""}
        step("listenbrainz", "skip", f"{what}: still on ListenBrainz (it was never deleted there), imported back hourly"
             if any(same_track(l, named) for l in listed) else
             f"{what}: not on ListenBrainz (the scrobbler never sent it, or it was deleted outside musicdb); not submitted")
    removed = r.get("youtube_removed") or []
    ys = rec.get("youtube") or {}
    for pid in removed:
        state = (ys.get(pid) or {}).get("state") or "will"
        step("youtube", state if state in ("done", "will") else "failed",
             f"back into playlist {pid} (appended: its position was not kept)"
             + (f": {state}" if state not in ("done", "will") else ""))
    if r.get("history") == "delete" and r.get("ytid") and not removed:
        step("youtube", "skip", "it was in none of the chosen playlists")
    allowed = r["id"] in deleted.allowed()
    step("download", "done" if allowed else "will", "downloads allowed" if allowed else "allow downloading it again")
    return out


# ------------------------------------------------------------------ doing it

def insert(c, vals):
    c.execute(f"INSERT OR IGNORE INTO events VALUES ({','.join('?' * len(musicdb.EVENT_COLS))})", vals)


def run(r, rec, lb_resubmit=False):
    """Every step that can run; returns the plan as it stands afterwards (without a ListenBrainz read). A remote
    failure raises nothing: the step's state says it, and `update` continues it."""
    ops = rec["ops"]
    with musicdb.journal_lock():  # 1. the hourly retry must not delete its history again
        pending = musicdb.jsonl(musicdb.PENDING)
        if any(p["id"] == r["id"] for p in pending):
            musicdb.write_jsonl(musicdb.DONE, musicdb.jsonl(musicdb.DONE) + [r | {"restored_at": now()}])
            musicdb.write_jsonl(musicdb.PENDING, [p for p in pending if p["id"] != r["id"]])
    ops["journal"] = "done"
    save(rec)
    state, why = file_plan(r, rec)  # 2. the file
    if state == "cannot":
        ops["file"] = f"failed: {why}"
        save(rec)
        return plan(r, rec, remote=False)
    if state == "will":
        from . import dedupe
        dest = musicdb.MUSIC / r["file"]
        row = identity.resolve(r["file"]) or {}
        trashed = r.get("trashed_to")
        try:
            if trashed and pathlib.Path(trashed).exists():
                src, note = pathlib.Path(trashed), "from the Trash"
            else:
                ops["file"] = "downloading"
                save(rec)
                src = download(r)
                if bad := verify(r, src):
                    ops["file"] = f"failed: {bad}; the download stays in {src}"
                    save(rec)
                    return plan(r, rec, remote=False)
                unmatched = r.get("mbid") and not recording_of(src)
                if unmatched:  # the tagger was unsure (left for review): this video was that recording in the library
                    mbtag.write_tags(src, {"artist": r.get("artist") or "", "title": r.get("title") or "",
                                           "mbid": r["mbid"], "artist_mbids": []})
                if row.get("id"):
                    identity.write_tags(src, row["id"], r["ytid"])  # an absolute path stays itself under MUSIC
                new = dedupe.audio_hash(src)
                note = ("downloaded again, identical audio" if new and new == row.get("md5")
                        else "downloaded again, another encode of the video (audio differs from the deleted file)")
                note += "; the tagger left it unmatched: the deleted song's recording, artist and title written" * bool(unmatched)
            publish(src, dest)
        except Exception as err:
            ops["file"] = f"failed: {err}"
            save(rec)
            return plan(r, rec, remote=False)
        ops["file"] = f"done: {note}"
        save(rec)
        subprocess.run(["mpc", "-q", "update", "--wait"], check=False)  # MPD knows the file before its stickers
    if ops.get("identity") != "done" and (row := identity.resolve(r["file"])):  # 3. identity
        from . import dedupe
        with identity.locked():
            rows = {k: dict(v) for k, v in identity.load(fresh=True)["rows"].items()}
            x = rows[row["id"]]
            x.update(path=r["file"], state="live", md5=dedupe.audio_hash(musicdb.MUSIC / r["file"]) or x.get("md5"))
            x.pop("into", None)
            x["paths"] = x.get("paths", []) + ([r["file"]] if r["file"] not in x.get("paths", []) else [])
            identity.save(rows)
        ops["identity"] = "done"
        save(rec)
    if ops.get("stickers") != "done" and r.get("stickers"):  # 4. stickers
        try:
            m = musicdb.mpd()
            for k, v in r["stickers"].items():
                m.sticker_set("song", r["file"], k, v)
            ops["stickers"] = "done"
        except Exception as err:  # MPD down: the next run sets them
            ops["stickers"] = f"failed: {err}"
        save(rec)
    gone = lb_deleted(r)
    if ops.get("local") != "done":  # 5. local history, one transaction
        c = musicdb.db()
        with c:
            for e in r.get("events") or []:
                vals = ["" if e.get(k) is None else e.get(k) for k in musicdb.EVENT_COLS]
                c.execute(f"DELETE FROM tombstones WHERE ({musicdb.KEY}) = ({','.join('?' * 7)})", vals[:7])
                if not any(e is g for g, _ in gone):  # a deleted listen comes back with its new msid (step 6)
                    insert(c, vals)
        ops["local"] = "done"
        save(rec)
    if gone:  # 6. ListenBrainz
        lb = rec.setdefault("lb", {})
        for e, msid in gone:
            x = lb.setdefault(f"{e['ts']} {msid}", {})
            if x.get("state") == "done":
                continue
            try:
                listed = lb_listens_at(int(musicdb.epoch_of(e["ts"])))
                back = next((l for l in listed if l.get("recording_msid") != msid and same_track(l, e)), None)
                if any(l.get("recording_msid") == msid for l in listed):
                    x.update(state="waiting", note="ListenBrainz still lists the deleted listen")
                elif back:
                    c = musicdb.db()
                    with c:
                        insert(c, ["lb", e["ts"], "", e.get("mbid") or "", "", e["artist"], e["title"],
                                   e.get("ms_played") or "", json.dumps({"msid": back["recording_msid"]})])
                        musicdb.merge_lb_copies(c)
                    x.update(state="done", msid=back["recording_msid"], at=now(), note="listed again")
                    x.pop("error", None)
                elif x.get("state") in ("submitting", "submitted") and not lb_resubmit:
                    x["note"] = f"{x['state']} on {x['at']}, not listed yet; never sent again on its own"
                else:
                    x.update(state="submitting", at=now())
                    save(rec)  # intent first: a crash during the POST leaves "submitting", never a blind resend
                    lb_submit(e)
                    x.update(state="submitted", at=now(), note="submitted, waiting to be listed")
            except (Exception, SystemExit) as err:
                x["error"] = str(err)
                x.setdefault("state", "failed")
            save(rec)
        states = [lb[f"{e['ts']} {m}"]["state"] for e, m in gone]
        ops["listenbrainz"] = ("done" if all(s == "done" for s in states)
                               else ", ".join(f"{states.count(s)} {s}" for s in dict.fromkeys(states)))
        save(rec)
    removed = r.get("youtube_removed") or []
    if removed and r.get("ytid"):  # 7. YouTube playlists
        ys = rec.setdefault("youtube", {})
        for pid in removed:
            y = ys.setdefault(pid, {})
            if y.get("state") == "done":
                continue
            y.update(state="adding", at=now())
            save(rec)
            try:
                out = yt("add", r["ytid"], "--playlist", pid)
                y.update(state="done", items=out["items"].get(pid, []))
            except Exception as err:  # recorded; `add` looks the video up first, so a retry never adds it twice
                y.update(state=f"failed: {err}")
            save(rec)
        ops["youtube"] = "done" if all(ys[p]["state"] == "done" for p in removed) else "failed"
        save(rec)
    if r["id"] not in deleted.allowed():  # 8. downloads
        deleted.log("allow", r["id"])
    ops["download"] = "done"
    if all(ops.get(s, "done").startswith("done") for s in STEPS):
        rec["finished_at"] = rec.get("finished_at") or now()
    save(rec)
    musicdb.export(argparse.Namespace())
    return plan(r, rec, remote=False)


def unfinished(rec):
    return not rec.get("finished_at") and rec["ops"].get("file", "").startswith("done")


def retry():
    """`musicdb update`: continue restores whose file is back but a ListenBrainz or YouTube step waits or failed."""
    for rid in [x["id"] for x in records().values() if unfinished(x)]:
        with restore_lock():
            rec = records()[rid]
            steps = run(deletion(rid), rec)
        print(f"restore {rec['file']}: " + (", ".join(f"{s['step']} {s['state']}" for s in steps if s["state"] != "done")
                                            or "finished"))


def state_of(rid, recs=None):
    """For the Deleted pane: None (never restored), "restored", or "partial" (a step waits or failed)."""
    rec = (records() if recs is None else recs).get(rid)
    return None if not rec else "restored" if rec.get("finished_at") else "partial"


def describe(r):
    return f"{r.get('artist') or ''} - {r.get('title') or r['file'].rsplit('/', 1)[-1]}".strip(" -")


def main(argv):
    ap = argparse.ArgumentParser(prog="musicdb restore", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("id", help="a deletion journal id (musicdb deletions --json --all)")
    ap.add_argument("--yes", action="store_true", help="restore (without it: the plan only)")
    ap.add_argument("--json", action="store_true", help="the plan as JSON (rormpc's Deleted overlay)")
    ap.add_argument("--lb-resubmit", action="store_true",
                    help="send a ListenBrainz listen again whose earlier submission is unconfirmed")
    a = ap.parse_args(argv)
    r = deletion(a.id)
    with restore_lock() if a.yes else contextlib.nullcontext():
        rec = records().get(a.id) or {"id": a.id, "file": r["file"], "started_at": now(), "ops": {}}
        steps = run(r, rec, a.lb_resubmit) if a.yes else plan(r, rec)
    blocked = [s for s in steps if s["state"] in ("cannot", "failed")]
    if a.json:
        print(json.dumps({"version": PLAN_VERSION, "id": a.id, "song": describe(r), "file": r["file"],
                          "restored": bool(rec.get("finished_at")), "can_restore": not blocked, "steps": steps},
                         ensure_ascii=False))
    else:
        for s in steps:
            print(f"{s['state']:8} {s['step']:12} {s['text']}")
    # the last line is what rormpc shows in its status bar (stdout on success, stderr on failure)
    if not a.yes:
        print(f"Restore plan for {describe(r)}: " + (f"blocked: {blocked[0]['text']}" if blocked else "ready")
              + " (dry run, nothing changed)")
    elif blocked:
        sys.exit(f"Not restored: {describe(r)}: {blocked[0]['step']} {blocked[0]['text']}")
    else:
        rest = [s for s in steps if s["state"] not in ("done", "skip")]
        print(f"Restored: {describe(r)}" + (f"; still to do (retried hourly): "
                                              f"{', '.join(s['step'] + ' ' + s['state'] for s in rest)}" if rest else ""))
