"""musicdb years: the songs' original release years, computed on MusicBrainz, reviewed row by row, undoable.

  musicdb years --dry-run [--file REL ...] [--limit N]   # compute; write years-report.json and years-report.md
  musicdb years [--json]                     # the report's counts (--json: the whole report, for rormpc's view)
  musicdb years --accept ID ... / --reject ID ... / --undecide ID ...   # record decisions in the report
  musicdb years --mbid ROW MBID              # the recording this file is, by hand (rows marked "needs MBID")
  musicdb years --apply                      # write the accepted rows whose audio and tags are unchanged
  musicdb years --rollback [ID ...]          # put back the values the newest apply of each file replaced

Year definition (docs/release-years-plan.md): TDOR (MPD OriginalDate) is the song's original release, the year
the views show; TDRC (MPD Date) stays this recording's own first release. Provenance in TXXX:DATE_SOURCE
(`musicbrainz:<recording MBID>`, the recording the TDOR came from) and TXXX:DATE_RULE (`same-length`,
`any-clean`, `own`; `first-release` when a download kept its recording's own first release). FLAC/Ogg: DATE,
ORIGINALDATE, DATE_SOURCE, DATE_RULE.

The rule, per file with a recording MBID:
  1. The matched recording is itself a version (remix, live, cover, cast, dub, bootleg, demo, re-recording,
     "Taylor's Version", instrumental, acoustic, unplugged; MusicBrainz's cover/live/instrumental/karaoke
     attributes): TDOR = its own earliest official Album/Single/EP release that is not a compilation (`own`).
     Mixes of the same audio (Atmos, 360, stereo, remaster, clean/explicit, radio edit, video mix) are no version.
  2. Else the candidates are its work's recordings by the same first artist that are no video, no DJ-mix segment
     and no version, within 10 s of the matched length: TDOR = the earliest date of an official Album, Single or
     EP release (no compilation, live, DJ-mix, soundtrack, remix or demo group) over them, looked up in
     first-release order, 8 recordings at most (`same-length`).
  3. Only when none has one, the same over every such candidate whatever its length (`any-clean`).
  4. Release dates, never release-group dates; a release group dated earlier is evidence and keeps the row in
     review.
  5. No work relationship: no proposal, the row "needs MBID"; nothing studio found: no proposal.
Confidence `high`: same-length, release-group year = release year, and earlier than the year shown now; else
`review`. Every row starts undecided: nothing is applied without an accept.

MusicBrainz at 1 request/s with the shared User-Agent (mbtag.http, which backs 503 off), every response cached in
the ytmb cache, so a second run reads only the cache.
"""
import argparse, contextlib, datetime as dt, fcntl, json, os, pathlib, re, shutil, subprocess, sys, urllib.parse

from . import mbtag, musicdb

REPORT_VERSION = 1  # of years-report.json, read by rormpc's "Years to review" view
MB = "https://musicbrainz.org/ws/2"
SAME_LENGTH_MS = 10_000
LOOKUPS_MAX = 8  # candidates whose release lists are fetched per rule
SEARCH_BATCH = 40  # recording ids per `rid:` search (one request, the URL stays short)
SEARCH_MAX = 200  # candidates dated per work: popular standards have thousands of covers
FIELDS = ("TDRC", "TDOR", "DATE_SOURCE", "DATE_RULE")
VORBIS = {"TDRC": "date", "TDOR": "originaldate", "DATE_SOURCE": "date_source", "DATE_RULE": "date_rule"}
AUDIO_EXT = (".mp3", ".flac", ".ogg", ".opus")

SAME_AUDIO = re.compile(r"\b(dolby atmos|atmos|360(°| reality audio)?|spatial|stereo|mono|remaster(ed)?(\s+\d{4})?|"
                        r"clean|explicit|radio edit|single edit|edit|video (mix|edit|version)|album version|"
                        r"single version|radio version)\b", re.I)
VERSION = re.compile(r"\b(remix(ed)?|rmx|live|cover|cast|dub|bootleg|demo|re-?record(ed|ing)?|taylor'?s version|"
                     r"instrumental|acoustic|unplugged|karaoke|a ?cappella|extended|club mix|12\" mix|rework|"
                     r"reprise|orchestral|piano version|mashup|sped up|slowed)\b", re.I)
VERSION_ATTRS = {"cover", "live", "instrumental", "karaoke", "partial", "medley"}
DJ_MIX = re.compile(r"\bpart of .*\bmix\b|\bmixed\b|\bdj[- ]?mix\b|\bcontinuous mix\b", re.I)
STUDIO = {"Album", "Single", "EP"}
NOT_STUDIO = {"Compilation", "Live", "DJ-mix", "Soundtrack", "Remix", "Mixtape/Street", "Demo", "Interview",
              "Spokenword", "Audiobook", "Audio drama"}
NOT_OWN = {"Compilation", "DJ-mix", "Mixtape/Street"}  # a live or cast recording's own release is a live or cast album


def report_path():
    return musicdb.DATA / "years-report.json"


def markdown_path():
    return musicdb.DATA / "years-report.md"


def backup_path():
    return musicdb.DATA / "years-backup.jsonl"


def now():
    return dt.datetime.now().isoformat(timespec="seconds")


def write_atomic(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


# ------------------------------------------------------------------ MusicBrainz (cached)

def cached(name, url):
    """A MusicBrainz response from the ytmb cache, else fetched (1 req/s, 503 backed off) and cached; None when
    MusicBrainz has nothing or keeps failing (not cached: the next run asks again)."""
    f = mbtag.CACHE / name
    if f.exists():
        try:
            r = json.loads(f.read_text())
        except ValueError:
            r = None
        if r:  # mbtag.mb_releases caches a failed lookup as {}: asked again
            return r
    r = mbtag.http(url)
    if r:
        write_atomic(f, json.dumps(r, ensure_ascii=False))
    return r


def recording(mbid):
    return cached(f"recw-{mbid}.json", f"{MB}/recording/{mbid}?inc=artist-credits+work-rels&fmt=json")


def work(wid):
    return cached(f"work-{wid}.json", f"{MB}/work/{wid}?inc=recording-rels&fmt=json")


def releases(mbid):
    """The recording's releases with their groups (the same cache file as mbtag.mb_releases)."""
    return cached(f"rels-{mbid}.json", f"{MB}/recording/{mbid}?inc=releases+release-groups&fmt=json") or {}


def dated(ids):
    """{id: search entry (artist-credit, length, video, first-release-date)} for recording ids, through `rid:`
    batch searches (a work's relations carry no artist or date); each entry cached on its own."""
    out, todo = {}, []
    for i in ids:
        f = mbtag.CACHE / f"srec-{i}.json"
        if f.exists():
            out[i] = json.loads(f.read_text())
        else:
            todo.append(i)
    for k in range(0, len(todo), SEARCH_BATCH):
        batch = todo[k:k + SEARCH_BATCH]
        q = " OR ".join(f"rid:{i}" for i in batch)
        r = mbtag.http(f"{MB}/recording?" + urllib.parse.urlencode({"query": q, "limit": 100, "fmt": "json"})) or {}
        for rec in r.get("recordings", []):
            if rec.get("id") in batch:
                rec.pop("releases", None)  # long and not needed: releases() has the full list
                write_atomic(mbtag.CACHE / f"srec-{rec['id']}.json", json.dumps(rec, ensure_ascii=False))
                out[rec["id"]] = rec
    return out


# ------------------------------------------------------------------ the rule

def year(date):
    return int(date[:4]) if date and date[:4].isdigit() else None


def first_artist(rec):
    ac = rec.get("artist-credit") or []
    return (ac[0].get("artist") or {}).get("id") if ac else None


def version_word(rec, attrs=()):
    """Why a recording is a version of the song (a word), or None for the song itself or a mix of its audio."""
    if bad := VERSION_ATTRS & set(attrs):
        return sorted(bad)[0]
    text = SAME_AUDIO.sub(" ", f"{rec.get('title') or ''} ({rec.get('disambiguation') or ''})")
    m = VERSION.search(text)
    return m.group(1).lower() if m else None


def dj_mix(rec):
    return bool(DJ_MIX.search(rec.get("disambiguation") or ""))


def earliest(rels, skip):
    """(date, release) of the earliest official Album/Single/EP release outside the `skip` secondary types."""
    best = None
    for rel in rels.get("releases", []):
        rg = rel.get("release-group") or {}
        if rel.get("status") != "Official" or rg.get("primary-type") not in STUDIO or not rel.get("date"):
            continue
        if skip & set(rg.get("secondary-types") or []):
            continue
        if best is None or rel["date"] < best[0]:
            best = (rel["date"], rel)
    return best


def scan(cands):
    """The earliest studio release over candidates, in first-release order, LOOKUPS_MAX lookups at most:
    (date, release, candidate) or None. A candidate first released after the best date found cannot beat it."""
    best, looked = None, 0
    for c in sorted(cands, key=lambda c: c.get("first-release-date") or "9999"):
        if best and (c.get("first-release-date") or "9999") > best[0]:
            break
        if looked >= LOOKUPS_MAX:
            break
        looked += 1
        e = earliest(releases(c["id"]), NOT_STUDIO)
        if e and (best is None or e[0] < best[0]):
            best = (e[0], e[1], c)
    return best


def short(mbid):
    return (mbid or "")[:8]


def performances(rec):
    return [r for r in rec.get("relations", []) if r.get("target-type") == "work" and r.get("type") == "performance"]


def work_candidates(rec, matched, perf, attrs, length, classes):
    """(clean, same): the recordings of `rec`'s first work by its first artist that are no video, no DJ-mix segment
    and no version (search entries, `rec` itself when it is clean), and those of them within SAME_LENGTH_MS of
    `length`."""
    w = work(perf[0]["work"]["id"]) or {}
    rels = [r for r in w.get("relations", []) if r.get("target-type") == "recording" and r.get("recording")]
    # a video or DJ-mix segment of a cover: the artist's plain recordings are covers too (the same first artist
    # keeps them from collapsing into the original's)
    own_attrs = VERSION_ATTRS & set(attrs)
    ids = []
    for r in rels:
        c = r["recording"]
        if c.get("video") or dj_mix(c) or version_word(c, set(r.get("attributes", [])) - own_attrs):
            continue
        ids.append((0 if length and c.get("length") and abs(c["length"] - length) <= SAME_LENGTH_MS else 1, c["id"]))
    ids = [i for _, i in sorted(ids)]
    if len(ids) > SEARCH_MAX:
        classes.append("candidates-truncated")
    found_ids = dated([i for i in ids[:SEARCH_MAX] if i != matched])
    if not rec.get("video") and not dj_mix(rec):
        found_ids[matched] = rec
    artist = first_artist(rec)
    clean = [c for c in found_ids.values() if first_artist(c) == artist and not c.get("video") and not dj_mix(c)
             and not version_word(c)]
    same = [c for c in clean if length and c.get("length") and abs(c["length"] - length) <= SAME_LENGTH_MS]
    return clean, same


def audio_for(mbid, length=None):
    """For a download matched to a video or DJ-mix segment with no audio alternative among its candidates: the
    work's recording to take instead, by the same first artist, no video, no DJ-mix segment, no version, within
    10 s of the matched length (the recording's, else `length` in ms, the download's), the one with the earliest
    official Album/Single/EP release first, else the earliest first release.
    Returns (recording id, rule, why): the id is None, with the reason in why, when there is none."""
    rec = recording(mbid)
    if not rec:
        return None, None, "recording not found on MusicBrainz"
    perf = performances(rec)
    if not perf:
        return None, None, "no work relationship on MusicBrainz"
    attrs = perf[0].get("attributes", [])
    length = rec.get("length") or length
    if not length:
        return None, None, "no length to compare"
    _, same = work_candidates(rec, rec.get("id") or mbid, perf, attrs, length, [])
    same = [c for c in same if c["id"] != (rec.get("id") or mbid)]
    if not same:
        return None, None, "no audio recording of the work within 10 s"
    if found := scan(same):
        c = found[2]
        return c["id"], "work-same-length", f"\"{c.get('title')}\" on \"{found[1].get('title')}\" {found[0]}"
    c = min(same, key=lambda c: c.get("first-release-date") or "9999")
    return c["id"], "work-same-length-no-studio", f"\"{c.get('title')}\", first released {c.get('first-release-date')}"


def compute(mbid):
    """The rule for one recording MBID: {"tdor", "tdrc_own", "rule", "source", "release", "rg_date", "classes",
    "needs_mbid", "evidence"}; "tdor" is None without a proposal."""
    rec = recording(mbid)
    out = {"tdor": None, "rule": None, "source": None, "release": None, "rg_date": None, "classes": [],
           "needs_mbid": False, "matched": mbid, "tdrc_own": None, "evidence": ""}
    if not rec:
        out["classes"].append("recording-not-found")
        out["evidence"] = f"recording `{short(mbid)}` not found on MusicBrainz"
        return out
    out["matched"] = rec.get("id") or mbid  # MusicBrainz redirects merged recordings
    out["tdrc_own"] = rec.get("first-release-date") or None
    perf = performances(rec)
    attrs = perf[0].get("attributes", []) if perf else []
    flags = []
    if rec.get("video"):
        flags.append("video")
        out["classes"].append("video")
    if dj_mix(rec):
        flags.append("DJ-mix segment")
        out["classes"].append("dj-mix")
    word = None if rec.get("video") or dj_mix(rec) else version_word(rec, attrs)
    if word:
        flags.append(f"version: {word}")
        out["classes"].append("version")
    if len(perf) > 1:
        out["classes"].append("several-works")
    matched = f"matched `{short(out['matched'])}` \"{rec.get('title')}\"" + (f" ({', '.join(flags)})" if flags else "")
    if rec.get("disambiguation"):
        matched += f" [{rec['disambiguation']}]"
    found, rule = None, None
    if word:
        e = earliest(releases(out["matched"]), NOT_OWN)
        if e:
            found, rule = (e[0], e[1], rec), "own"
    elif not perf:
        out["needs_mbid"] = True
        out["classes"].append("no-work")
        out["evidence"] = matched + "; no work relationship on MusicBrainz"
        return out
    else:
        clean, same = work_candidates(rec, out["matched"], perf, attrs, rec.get("length"), out["classes"])
        for rule, group in (("same-length", same), ("any-clean", clean)):
            if group and (found := scan(group)):
                break
        else:
            rule = None
    if not found:
        out["classes"].append("no-studio-release")
        out["evidence"] = matched + "; no official studio release found"
        return out
    date, rel, src = found
    rg = rel.get("release-group") or {}
    out.update(tdor=date, rule=rule, source=src["id"], rg_date=rg.get("first-release-date") or None,
               release={"id": rel.get("id"), "title": rel.get("title"), "date": date,
                        "type": " + ".join([rg.get("primary-type") or "?"] + (rg.get("secondary-types") or []))})
    if year(out["rg_date"]) and year(out["rg_date"]) < year(date):
        out["classes"].append("rg-earlier")
    ev = [matched, f"source `{short(src['id'])}` \"{src.get('title')}\" on {out['release']['type']} "
                   f"\"{rel.get('title')}\" {date}"]
    if out["rg_date"] and year(out["rg_date"]) != year(date):
        ev.append(f"RG {out['rg_date']}")
    out["evidence"] = "; ".join(ev)
    return out


def confidence(c, shown):
    """high only for same-length, a release group of the same year, and earlier than the year shown now."""
    if not c["tdor"]:
        return None
    if c["rule"] == "same-length" and year(c["rg_date"] or c["tdor"]) == year(c["tdor"]) and shown \
            and year(c["tdor"]) < shown:
        return "high"
    return "review"


def for_download(row):
    """(TDOR, DATE_SOURCE recording, DATE_RULE) for a new download's matched recording, or None when the rule has no
    proposal or proposes a later date than the recording's own first release (later proposals were mostly wrong in
    the sample, and a download is not reviewed). A failed lookup never loses the download."""
    try:
        c = compute(row["mbid"])
    except Exception as e:  # network, an unexpected MusicBrainz answer
        print(f"years: no original release for {row.get('mbid')}: {type(e).__name__}: {e}", file=sys.stderr)
        return None
    own = row.get("first_release") or ""
    if c["tdor"] and (not own or c["tdor"][:len(own)] <= own):
        return c["tdor"], c["source"], c["rule"]
    return None


# ------------------------------------------------------------------ tags

def read_dates(path):
    """{TDRC, TDOR, DATE_SOURCE, DATE_RULE: text or None} as the file stores them."""
    import mutagen
    f = mutagen.File(path)
    t = f.tags if f is not None else None
    out = dict.fromkeys(FIELDS)
    if t is None:
        return out
    if hasattr(t, "getall"):  # ID3
        for k in ("TDRC", "TDOR"):
            if t.get(k) and t[k].text:
                out[k] = str(t[k].text[0])
        for k in ("DATE_SOURCE", "DATE_RULE"):
            if t.get(f"TXXX:{k}"):
                out[k] = str(t[f"TXXX:{k}"].text[0])
    else:
        for k, v in VORBIS.items():
            out[k] = (t.get(v) or [None])[0]
    return out


def set_dates(path, values):
    """Set the four fields in place (None removes one)."""
    import mutagen
    from mutagen.id3 import TDOR, TDRC, TXXX
    f = mutagen.File(path)
    if f is None:
        raise ValueError(f"not an audio file mutagen can tag: {path}")
    if f.tags is None:
        f.add_tags()
    t = f.tags
    if hasattr(t, "getall"):
        for k, cls in (("TDRC", TDRC), ("TDOR", TDOR)):
            t.delall(k)
            if values.get(k):
                t.add(cls(encoding=3, text=[values[k]]))
        for k in ("DATE_SOURCE", "DATE_RULE"):
            t.delall(f"TXXX:{k}")
            if values.get(k):
                t.add(TXXX(encoding=3, desc=k, text=[values[k]]))
    else:
        for k, v in VORBIS.items():
            if values.get(k):
                t[v] = [values[k]]
            elif v in t:
                del t[v]
    f.save()


def write_dates(path, values):
    """Write the fields to a copy in the same directory and os.replace it over the file: MPD never reads a
    half-written file. The copy's name starts with a dot, which MPD does not index."""
    path = pathlib.Path(path)
    tmp = path.with_name(f".{path.name}.years-tmp")
    shutil.copy2(path, tmp)
    try:
        set_dates(tmp, values)
        with open(tmp, "rb+") as f:
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def audio_hashes(rels):
    from . import dedupe
    return dedupe.hashes(rels)


def mpc_update(dirs):
    """MPD rereads the touched directories, so its Date / OriginalDate change (--wait: until it has)."""
    for d in sorted(dirs):
        subprocess.run(["mpc", "-q", "update", "--wait", d or "/"], check=False)


# ------------------------------------------------------------------ the report

def load_report():
    try:
        return json.loads(report_path().read_text())
    except (OSError, ValueError):
        return {"version": REPORT_VERSION, "generated": None, "next_id": 1, "rows": []}


def save_report(rep):
    rows = rep["rows"]
    rep["version"] = REPORT_VERSION
    rep["counts"] = counts(rows)
    write_atomic(report_path(), json.dumps(rep, ensure_ascii=False, indent=1) + "\n")
    write_atomic(markdown_path(), markdown(rep))


def counts(rows):
    out = {"rows": len(rows), "class": {}, "confidence": {}, "decision": {}, "needs_mbid": 0, "applied": 0}
    for r in rows:
        for c in r["classes"] or ["plain"]:
            out["class"][c] = out["class"].get(c, 0) + 1
        k = r["confidence"] or "none"
        out["confidence"][k] = out["confidence"].get(k, 0) + 1
        out["decision"][r["decision"]] = out["decision"].get(r["decision"], 0) + 1
        out["needs_mbid"] += bool(r["needs_mbid"])
        out["applied"] += bool(r.get("applied"))
    return out


def cell(s):
    return str(s if s not in (None, "") else "–").replace("|", "\\|").replace("\n", " ")


def markdown(rep):
    c = rep.get("counts") or counts(rep["rows"])
    lines = ["# Release years to review", "", f"Generated {rep.get('generated')}; {c['rows']} rows, "
             f"{c['needs_mbid']} need an MBID, {c['applied']} applied.", "",
             "| class | rows |", "|---|---|"]
    lines += [f"| {k} | {v} |" for k, v in sorted(c["class"].items())]
    lines += ["", "| confidence | rows |", "|---|---|"] + [f"| {k} | {v} |" for k, v in sorted(c["confidence"].items())]
    lines += ["", "| decision | rows |", "|---|---|"] + [f"| {k} | {v} |" for k, v in sorted(c["decision"].items())]
    lines += ["", "| id | file | current TDRC / TDOR | proposed TDRC / TDOR | rule | confidence | decision | evidence |",
              "|---|---|---|---|---|---|---|---|"]
    for r in rep["rows"]:
        cur, p = r["current"], r["proposed"] or {}
        dec = r["decision"] + (" (applied)" if r.get("applied") else "") + (" · needs MBID" if r["needs_mbid"] else "")
        lines.append(f"| {r['id']} | {cell(r['file'])} | `{cell(cur.get('TDRC'))}` / `{cell(cur.get('TDOR'))}` | "
                     f"`{cell(p.get('TDRC'))}` / `{cell(p.get('TDOR'))}` | {cell(r['rule'])} | {cell(r['confidence'])} | "
                     f"{dec} | {cell(r['evidence'])} |")
    return "\n".join(lines) + "\n"


def shown_year(cur):
    """The year the views show: originaldate, falling back to date (a YYYYMMDD upload date is no release)."""
    for k in ("TDOR", "TDRC"):
        v = cur.get(k)
        if v and not re.fullmatch(r"\d{8}", v):
            return year(v)
    return None


def build_row(rel, mbid, cur, md5, c):
    """A report row, or None when nothing would change and nothing is suspect."""
    tdrc = cur.get("TDRC") if cur.get("TDRC") and not re.fullmatch(r"\d{8}", cur["TDRC"]) else None
    shown = shown_year(cur)
    row = {"file": rel, "md5": md5, "mbid": mbid, "current": cur, "proposed": None, "rule": None, "confidence": None,
           "classes": [], "evidence": "", "detail": None, "needs_mbid": False}
    if c is None:
        row.update(classes=["no-recording"], needs_mbid=True, evidence="no MusicBrainz recording in the tags")
        return row
    row.update(classes=list(c["classes"]), needs_mbid=c["needs_mbid"], evidence=c["evidence"],
               detail={k: c[k] for k in ("matched", "source", "release", "rg_date", "tdrc_own")})
    if c["tdor"]:
        row["proposed"] = {"TDRC": tdrc or c["tdrc_own"] or c["tdor"], "TDOR": c["tdor"],
                           "DATE_SOURCE": f"musicbrainz:{c['source']}", "DATE_RULE": c["rule"]}
        row["rule"] = c["rule"]
        row["confidence"] = confidence(c, shown)
        if shown == year(c["tdor"]):  # the year shown holds: the views would not change
            if "rg-earlier" not in row["classes"]:
                return None
            # e.g. a recording only on reissues: its release group's earlier date is worth a look (or an MBID)
            return dict(row, proposed=None, rule=None, confidence=None, classes=row["classes"] + ["keeps-year"])
        if shown and year(c["tdor"]) > shown:
            row["classes"].append("later")
        if shown is None:
            row["classes"].append("undated")
        return row
    return row  # no proposal: always a suspect class (no work, nothing studio, not found)


def row_key(r):
    return r.get("song_id") or r["file"]


def song_ids():
    from . import identity
    return {p: r["id"] for r in identity.load(fresh=True)["rows"].values() if (p := r.get("path"))}


@contextlib.contextmanager
def locked():
    lock = musicdb.DB.parent / "years.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    with open(lock, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield


def library(only=None):
    """[(rel, recording MBID or None)] of the audio files MPD lists."""
    out = []
    for s in musicdb.mpd().listallinfo():
        rel = s.get("file")
        if not rel or not rel.lower().endswith(AUDIO_EXT) or (only and rel not in only):
            continue
        out.append((rel, musicdb.one(s.get("musicbrainz_trackid", "")) or None))
    return sorted(out)


def dry_run(a):
    with locked():
        old = load_report()
        prev = {row_key(r): r for r in old["rows"]}
        files = library(set(a.file) if a.file else None)
        if a.limit:
            files = files[:a.limit]
        partial = bool(a.file or a.limit)
        ids = song_ids()
        hashes = audio_hashes([rel for rel, _ in files])
        rows, next_id = [], old.get("next_id", 1)
        for n, (rel, mbid) in enumerate(files, 1):
            o = prev.get(ids.get(rel) or rel)
            mbid = (o or {}).get("mbid_override") or mbid
            cur = read_dates(musicdb.MUSIC / rel)
            try:
                c = compute(mbid) if mbid else None
            except Exception as e:  # an unexpected MusicBrainz answer: this row only, asked again on the next run
                c = {"tdor": None, "classes": ["lookup-failed"], "needs_mbid": False, "matched": mbid, "source": None,
                     "release": None, "rg_date": None, "tdrc_own": None, "evidence": f"{type(e).__name__}: {e}"}
            r = build_row(rel, mbid, cur, hashes.get(rel), c)
            print(f"[{n}/{len(files)}] {rel}: " + (f"{r['current'].get('TDOR') or r['current'].get('TDRC')} -> "
                  f"{(r['proposed'] or {}).get('TDOR')} ({r['rule']}, {r['confidence']}) {r['classes']}" if r else "keeps its year"),
                  file=sys.stderr)
            if r is None:
                if o and (o.get("applied") or o.get("mbid_override")):
                    rows.append(o)  # done (the file now has the proposal) or picked by hand: kept for the view
                continue
            r["song_id"] = ids.get(rel)
            same = o and o.get("proposed") == r["proposed"] and o.get("md5") == r["md5"] and o.get("current") == cur
            r["id"] = o["id"] if o else next_id
            next_id = max(next_id, r["id"] + 1)
            r["decision"] = o["decision"] if same else "undecided"
            r["decided"] = o.get("decided") if same else None
            r["applied"] = o.get("applied") if same else None
            if o and o.get("mbid_override"):
                r["mbid_override"] = o["mbid_override"]
            rows.append(r)
        if partial:  # rows of files not looked at this time stay as they were
            seen = {row_key(r) for r in rows} | {ids.get(rel) or rel for rel, _ in files}
            rows += [r for k, r in prev.items() if k not in seen]
        rows.sort(key=lambda r: r["id"])
        rep = {"version": REPORT_VERSION, "generated": now(), "next_id": next_id, "rows": rows}
        save_report(rep)
    return rep


def decide(ids, decision):
    with locked():
        rep = load_report()
        by = {r["id"]: r for r in rep["rows"]}
        bad = [i for i in ids if i not in by]
        if bad:
            sys.exit(f"musicdb years: no row {', '.join(map(str, bad))} in {report_path()}")
        if decision == "accepted" and (none := [i for i in ids if not by[i]["proposed"]]):
            sys.exit(f"musicdb years: row {', '.join(map(str, none))} has no proposal to accept "
                     "(--mbid ROW MBID picks the recording)")
        for i in ids:
            by[i]["decision"], by[i]["decided"] = decision, now()
        save_report(rep)
    return {"ok": True, "decision": decision, "ids": ids}


def set_mbid(row_id, mbid):
    """The recording this row's file is, by hand: the row is computed again from it and needs a new decision."""
    if not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", mbid):
        sys.exit(f"musicdb years: not a MusicBrainz id: {mbid}")
    with locked():
        rep = load_report()
        by = {r["id"]: r for r in rep["rows"]}
        if row_id not in by:
            sys.exit(f"musicdb years: no row {row_id} in {report_path()}")
        o = by[row_id]
        cur = read_dates(musicdb.MUSIC / o["file"])
        r = build_row(o["file"], mbid, cur, o["md5"], compute(mbid)) or {
            "file": o["file"], "md5": o["md5"], "mbid": mbid, "current": cur, "proposed": None, "rule": None,
            "confidence": None, "classes": ["keeps-year"], "evidence": "the picked recording gives the same year",
            "detail": None, "needs_mbid": False}
        r.update(id=row_id, song_id=o.get("song_id"), mbid_override=mbid, decision="undecided", decided=None,
                 applied=None)
        rep["rows"] = [r if x["id"] == row_id else x for x in rep["rows"]]
        save_report(rep)
    return r


def append_backup(entry):
    path = backup_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())


def apply(_a=None):
    """Write the accepted rows whose audio (md5) and date tags are what the report saw; the old values go to
    years-backup.jsonl (fsynced) before each write."""
    done, skipped, dirs = [], [], set()
    with locked():
        rep = load_report()
        todo = [r for r in rep["rows"] if r["decision"] == "accepted" and r["proposed"] and not r.get("applied")]
        live = [r for r in todo if (musicdb.MUSIC / r["file"]).exists()]
        skipped += [{"id": r["id"], "file": r["file"], "why": "file gone"} for r in todo if r not in live]
        hashes = audio_hashes([r["file"] for r in live])
        for r in live:
            path = musicdb.MUSIC / r["file"]
            if not r["md5"] or hashes.get(r["file"]) != r["md5"]:
                skipped.append({"id": r["id"], "file": r["file"], "why": "audio changed since the report"})
                continue
            cur = read_dates(path)
            if cur != r["current"]:
                skipped.append({"id": r["id"], "file": r["file"], "why": "date tags changed since the report"})
                continue
            new = {k: r["proposed"].get(k) for k in FIELDS}
            append_backup({"ts": now(), "kind": "apply", "row": r["id"], "file": r["file"], "md5": r["md5"],
                           "old": cur, "new": new})
            write_dates(path, new)
            r["applied"] = now()
            done.append({"id": r["id"], "file": r["file"], "new": new})
            dirs.add(os.path.dirname(r["file"]))
        save_report(rep)
    if dirs:
        mpc_update(dirs)
    return {"applied": done, "skipped": skipped}


def rollback(ids=None):
    """Put back the old values of the newest apply of each file not rolled back yet (only rows `ids` when given),
    when the file still has what that apply wrote."""
    entries = musicdb.jsonl(backup_path())
    stacks = {}
    for e in entries:
        s = stacks.setdefault(e["file"], [])
        if e["kind"] == "apply":
            s.append(e)
        elif s:
            s.pop()
    done, skipped, dirs = [], [], set()
    with locked():
        rep = load_report()
        by = {r["id"]: r for r in rep["rows"]}
        for f, s in sorted(stacks.items()):
            if not s or (ids and s[-1]["row"] not in ids):
                continue
            e, path = s[-1], musicdb.MUSIC / f
            if not path.exists():
                skipped.append({"id": e["row"], "file": f, "why": "file gone"})
                continue
            cur = read_dates(path)
            if cur != e["new"]:
                skipped.append({"id": e["row"], "file": f, "why": "date tags changed since the apply"})
                continue
            append_backup({"ts": now(), "kind": "rollback", "row": e["row"], "file": f, "old": e["new"], "new": e["old"]})
            write_dates(path, e["old"])
            done.append({"id": e["row"], "file": f, "restored": e["old"]})
            dirs.add(os.path.dirname(f))
            if (r := by.get(e["row"])) and r["file"] == f:
                r.update(applied=None, decision="undecided", decided=None, rolled_back=now())
        save_report(rep)
    if dirs:
        mpc_update(dirs)
    return {"rolled_back": done, "skipped": skipped}


def main(argv):
    ap = argparse.ArgumentParser(prog="musicdb years", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true", help="compute and write the report; no tag is written")
    g.add_argument("--accept", nargs="+", type=int, metavar="ID")
    g.add_argument("--reject", nargs="+", type=int, metavar="ID")
    g.add_argument("--undecide", nargs="+", type=int, metavar="ID")
    g.add_argument("--mbid", nargs=2, metavar=("ROW", "MBID"))
    g.add_argument("--apply", action="store_true", help="write the accepted rows (old values to years-backup.jsonl)")
    g.add_argument("--rollback", nargs="*", type=int, metavar="ID", help="undo the newest apply (of these rows)")
    ap.add_argument("--file", action="append", help="with --dry-run: only this file (relative to the music dir)")
    ap.add_argument("--limit", type=int, help="with --dry-run: only the first N files")
    ap.add_argument("--json", action="store_true", help="print JSON")
    a = ap.parse_args(argv)
    if a.dry_run:
        out = dry_run(a)
        if not a.json:
            out = {"report": str(report_path()), "markdown": str(markdown_path()), "counts": out["counts"]}
    elif a.accept or a.reject or a.undecide:
        out = decide(a.accept or a.reject or a.undecide,
                     "accepted" if a.accept else "rejected" if a.reject else "undecided")
    elif a.mbid:
        out = set_mbid(int(a.mbid[0]), a.mbid[1].lower())
    elif a.apply:
        out = apply()
    elif a.rollback is not None:
        out = rollback(set(a.rollback))
    else:
        rep = load_report()
        out = rep if a.json else {"report": str(report_path()), "generated": rep.get("generated"),
                                  "counts": rep.get("counts") or counts(rep["rows"])}
    print(json.dumps(out, ensure_ascii=False, indent=None if a.json else 1))
