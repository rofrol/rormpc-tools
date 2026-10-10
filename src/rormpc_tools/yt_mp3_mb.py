"""Download YouTube videos/playlists as mp3 with yt-dlp, identify them on MusicBrainz, tag and name them.

  yt-mp3-mb URL [URL ...]            # single video -> <music>/Singles, playlist -> <music>/<playlist title>
  yt-mp3-mb -d Music2026 URL         # target subdirectory of the music dir (or absolute path)
  yt-mp3-mb --yes URL                # no questions: uncertain matches get cleaned names without MBID
  yt-mp3-mb --batch --json URL       # for programs: no questions, one JSON report on stdout (below)
  yt-mp3-mb --retag FILE.mp3 ...     # only identify + retag already downloaded files
  yt-mp3-mb URL -- --playlist-items 1-5   # extra args after -- go to yt-dlp

Music dir: music_dir in the settings ($YTMB_MUSIC_DIR). Needs: yt-dlp, ffmpeg; optional: fpcalc (AcoustID),
mpc, listenbrainz-mpd config or $LISTENBRAINZ_TOKEN (better matching).
File name: NNN--Artist--Title--youtubeID--uploaddate.mp3 (NNN = playlist index, omitted for singles).
Cover: Cover Art Archive front of the earliest official release, else the YouTube thumbnail cropped to a square.
Uncertain matches are asked about interactively; decisions are logged to ~/.cache/ytmb/log.jsonl.

--batch never asks: an uncertain match is not guessed but kept as "needs review": the file is saved with cleaned
names and no MBID, and its proposal (MusicBrainz candidates) is reported. Videos whose file is already in the
target dir (by YouTube id in the name) are not downloaded again, so a rerun after an interruption resumes.
--json prints one object on stdout, messages go to stderr:
  {"files": [{"path", "ytid", "status" auto|review|nomatch, "artist", "title", "mbid"}],
   "needs_review": [{"path", "ytid", "proposal": {"artist", "title", "mbid", "score", "method", "alternatives"}}],
   "skipped": [ytid already in the target dir], "failed": [{"ytid", "error"}] (identifying or tagging failed),
   "error": null | "yt-dlp failed (1)"}  (exit status 1 with an error)
"""
import argparse, json, pathlib, re, shutil, subprocess, sys, tempfile

from . import external, identity, mbtag, settings

MUSIC = settings.MUSIC_DIR
LOG = mbtag.CACHE / "log.jsonl"
COOKIES = pathlib.Path.home() / "Downloads/cookies.txt"
FIELDS = "filepath,id,title,channel,uploader,artist,track,album,description,upload_date,playlist_index,playlist_title,duration"


def safe(s, limit=120):
    s = mbtag.fix(s)
    s = re.sub(r'[/\\:*?"<>|\x00-\x1f]', "", s)
    s = re.sub(r"[\s']+", "_", s).strip("._")
    return s.encode()[:limit].decode(errors="ignore") or "unknown"


def download(urls, extra, skip_ids=()):
    """Run yt-dlp; returns info dicts of the files it produced. skip_ids are not downloaded (a download archive)."""
    tmp = tempfile.mkdtemp(prefix="ytmb-")
    cmd = ["yt-dlp", "--ignore-config", "-x", "--audio-format", "mp3", "--audio-quality", "0", "--progress",
           "--embed-metadata", "--no-embed-info-json", "--no-mtime", "--no-abort-on-error", "--windows-filenames",
           "-P", tmp, "-o", "%(id)s.%(ext)s", "--print", "after_move:%(.{" + FIELDS + "})j"]
    if COOKIES.exists():
        cmd += ["--cookies", str(COOKIES)]
    if skip_ids:
        archive = pathlib.Path(tmp) / "archive.txt"
        archive.write_text("".join(f"youtube {i}\n" for i in sorted(skip_ids)))
        cmd += ["--download-archive", str(archive)]
    cmd += extra + urls
    print("+", " ".join(cmd), file=sys.stderr)
    p = subprocess.run(cmd, stdout=subprocess.PIPE, text=True)
    infos = [json.loads(line) for line in p.stdout.splitlines() if line.startswith("{")]
    if p.returncode and not infos:
        sys.exit(f"yt-dlp failed ({p.returncode})")
    return infos


def ask(row, d):
    """Interactive confirmation for an uncertain match. Returns the (possibly edited) row or None to keep YT names."""
    print(f"\n?  {d['channel']} | {d['yt_title']}  ({round(d['duration'])} s)")
    opts = []
    if row["mbid"]:
        opts.append(row)
    for alt in row["alternatives"]:
        rec = mbtag.mb_recording(alt["mbid"])
        if rec:
            opts.append({**row, **mbtag.from_recording(d, rec)})
    for i, o in enumerate(opts, 1):
        print(f"   {i}) {o['artist']} - {o['title']}  [{o.get('mb_length', 0)} s, https://musicbrainz.org/recording/{o['mbid']}]")
    fallback = row if not row["mbid"] else {**row, "artist": d["cands"][0][0], "title": d["cands"][0][1]}
    print(f"   n) names only, no MBID: {fallback['artist']} - {fallback['title']}")
    print("   m) type 'Artist - Title' manually      (Enter = 1 / n if no candidates)")
    while True:
        c = input("   > ").strip()
        if c == "" and opts:
            return opts[0]
        if c in ("", "n"):
            return {**fallback, "mbid": "", "artist_mbids": []}
        if c == "m":
            m = re.split(r"\s+-\s+", input("   Artist - Title: ").strip(), maxsplit=1)
            if len(m) == 2:
                return {**row, "artist": m[0], "title": m[1], "mbid": "", "artist_mbids": []}
        if c.isdigit() and 1 <= int(c) <= len(opts):
            return opts[int(c) - 1]


def replaygain(path):
    """Track ReplayGain tags (rsgain, -18 LUFS) so MPD's `replaygain "track"` evens out loudness. Optional: without
    rsgain, MPD falls back to replaygain_missing_preamp."""
    if not shutil.which("rsgain"):
        return
    p = subprocess.run(["rsgain", "custom", "-s", "i", "-l", "-18", "-q", str(path)], capture_output=True, text=True)
    if p.returncode:  # loudness tags must not lose the download
        print(f"  rsgain failed: {(p.stderr or p.stdout).strip()[-200:]}", file=sys.stderr)


def proposal(row):
    """What --batch reports for an uncertain match instead of asking about it."""
    return {k: row[k] for k in ("artist", "title", "mbid", "score", "method", "alternatives")}


def process(path, info, target, yes, batch=False):
    """Identify, tag and name one downloaded file and move it into target. Returns the --json file entry
    ({"path", "ytid", "status", "artist", "title", "mbid"}, plus "proposal" for a match left for review)."""
    d = mbtag.collect(path, info["id"], info.get("channel") or info.get("uploader") or "", info.get("title") or "",
                      info.get("description"), float(info.get("duration") or 0), info.get("artist"), info.get("track"))
    row = mbtag.resolve(d)
    review = None
    if row["status"] != "auto" and batch:
        # not guessed: names only, and the proposal goes to the caller
        review = proposal(row)
        names = row if not row["mbid"] else {**row, "artist": d["cands"][0][0], "title": d["cands"][0][1]}
        row = {**names, "mbid": "", "artist_mbids": []}
    elif row["status"] != "auto" and not yes and sys.stdin.isatty():
        row = ask(row, d)
    album = d["provided"]["album"] if d["provided"] else None
    mbtag.write_tags(path, row, album=album)
    try:
        cov = mbtag.cover(info["id"], row["mbid"])
        if cov:
            mbtag.embed_cover(path, cov, row["title"])
    except Exception as e:  # a missing cover must not lose the download
        print(f"  cover failed: {e}", file=sys.stderr)
    replaygain(path)
    idx = info.get("playlist_index")
    name = (f"{int(idx):03d}--" if idx else "") + f"{safe(row['artist'], 60)}--{safe(row['title'])}--{info['id']}--{info.get('upload_date') or ''}.mp3"
    target.mkdir(parents=True, exist_ok=True)
    dest = target / name
    if pathlib.Path(path) != dest:
        pathlib.Path(path).rename(dest)
    with LOG.open("a") as fh:
        fh.write(json.dumps({"file": str(dest), "status": row["status"], "artist": row["artist"], "title": row["title"],
                             "mbid": row["mbid"], "yt_title": d["yt_title"], "channel": d["channel"]}, ensure_ascii=False) + "\n")
    mark = {"auto": "✓", "review": "~", "nomatch": "✗"}[row["status"]]
    print(f"{mark} {row['artist']} - {row['title']}  {row['mbid'] or '(no MBID)'}\n  -> {dest.relative_to(MUSIC) if dest.is_relative_to(MUSIC) else dest}",
          file=sys.stderr if batch else sys.stdout)
    entry = {"path": str(dest), "ytid": info["id"], "status": row["status"], "artist": row["artist"],
             "title": row["title"], "mbid": row["mbid"]}
    return {**entry, "proposal": review} if review else entry


def target_of(info, dir_arg):
    if dir_arg:
        return pathlib.Path(dir_arg) if pathlib.Path(dir_arg).is_absolute() else MUSIC / dir_arg
    if info.get("playlist_title"):
        return MUSIC / safe(info["playlist_title"])
    return MUSIC / "Singles"


def present_ids(target):
    """YouTube ids of the files already in a target dir (by name), so --batch does not download them again."""
    if not target or not target.is_dir():
        return set()
    return {y for f in target.iterdir() if (y := identity.ytid_from_name(f.name))}


def flat(url):
    """A playlist's entries without downloading anything (`yt-dlp --flat-playlist -J`): the parsed JSON, or
    raises RuntimeError with yt-dlp's last error line."""
    p = subprocess.run(["yt-dlp", "--ignore-config", "--flat-playlist", "-J", url], capture_output=True, text=True)
    if p.returncode == 0:
        try:
            return json.loads(p.stdout)
        except json.JSONDecodeError:
            pass
    lines = [l for l in (p.stderr or "").splitlines() if l.strip()]
    raise RuntimeError(lines[-1] if lines else f"yt-dlp failed ({p.returncode})")


def planned(urls, dir_arg):
    """(target dirs, YouTube ids) the URLs will produce, without downloading: a single video's id comes from its
    URL, a playlist is listed flat. Used to skip what a target dir already has."""
    targets, ids = set(), set()
    for url in urls:
        if m := re.search(r"(?:v=|youtu\.be/|shorts/)([\w-]{11})", url):
            if "list=" not in url:
                ids.add(m.group(1))
                targets.add(target_of({}, dir_arg))
                continue
        try:
            info = flat(url)
        except RuntimeError as e:
            print(f"  listing {url} failed: {e}", file=sys.stderr)
            continue
        ids.update(e["id"] for e in info.get("entries") or [info] if e and e.get("id"))
        targets.add(target_of({"playlist_title": info.get("title") if info.get("entries") is not None else None}, dir_arg))
    return targets, ids


def batch(urls, dir_arg=None, extra=(), known=None):
    """--batch: download, identify and file without a question; returns the --json report. known: (targets, ids)
    when the caller already knows them (skips the listing)."""
    targets, ids = known if known is not None else planned(urls, dir_arg)
    skip = set().union(*(present_ids(t) for t in targets)) & ids
    report = {"files": [], "needs_review": [], "skipped": sorted(skip), "failed": [], "error": None}
    if ids and ids <= skip:
        return report
    try:
        infos = download(list(urls), list(extra), skip)
    except SystemExit as e:  # download exits when yt-dlp produced nothing
        report["error"] = str(e.code)
        return report
    for info in infos:
        try:
            entry = process(info["filepath"], info, target_of(info, dir_arg), yes=True, batch=True)
        except Exception as e:  # one bad file must not lose the report of the others
            report["failed"].append({"ytid": info.get("id"), "error": f"{type(e).__name__}: {e}"})
            continue
        review = entry.pop("proposal", None)
        report["files"].append(entry)
        if review:
            report["needs_review"].append({"path": entry["path"], "ytid": entry["ytid"], "proposal": review})
    return report


@external.cli
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("urls", nargs="*")
    ap.add_argument("-d", "--dir", help="target dir (relative to the music dir or absolute)")
    ap.add_argument("--yes", action="store_true", help="never ask")
    ap.add_argument("--batch", action="store_true", help="never ask; uncertain matches are left for review")
    ap.add_argument("--json", action="store_true", help="one JSON report on stdout (with --batch)")
    ap.add_argument("--retag", nargs="+", metavar="FILE", help="retag existing yt-dlp mp3s instead of downloading")
    argv = sys.argv[1:]
    extra = argv[argv.index("--") + 1:] if "--" in argv else []
    a = ap.parse_args(argv[: argv.index("--")] if "--" in argv else argv)

    if a.retag:
        from mutagen.id3 import ID3
        from mutagen.mp3 import MP3
        for f in map(pathlib.Path, a.retag):
            t = ID3(f)
            g = lambda k: str(t.get(k)) if t.get(k) else ""
            url = g("TXXX:purl") or g("TXXX:comment")
            m = re.search(r"v=([\w-]{11})", url)
            yt = m.group(1) if m else identity.ytid_from_name(f.name)
            date = re.search(r"--(\d{8})\.mp3$", f.name)
            idx = re.match(r"(\d+)--", f.name)
            info = {"id": yt, "channel": g("TXXX:YouTube Channel") or g("TPE1"), "title": g("TIT2"),
                    "description": g("TXXX:description"), "duration": MP3(f).info.length,
                    "upload_date": date.group(1) if date else "", "playlist_index": idx.group(1) if idx else None}
            process(f, info, f.parent, a.yes)
        mpd_update()
        return

    if not a.urls:
        ap.error("give a URL or --retag FILE")
    if a.json and not a.batch:
        ap.error("--json needs --batch")
    if a.batch:
        report = batch(a.urls, a.dir, extra)
        if report["files"]:
            mpd_update()
        if a.json:
            print(json.dumps(report, ensure_ascii=False))
        else:
            for r in report["needs_review"]:
                print(f"needs review: {r['path']}", file=sys.stderr)
        sys.exit(1 if report["error"] else 0)
    infos = download(a.urls, extra)
    for info in infos:
        process(info["filepath"], info, target_of(info, a.dir), a.yes)
    mpd_update()


def mpd_update():
    if shutil.which("mpc"):
        subprocess.run(["mpc", "-q", "update"])


if __name__ == "__main__":
    main()
