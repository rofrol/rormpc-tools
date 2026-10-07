"""Audio fingerprints of the files in a Versions group: is a second file a copy of the same recording?

Only files that already share a name (a `musicdb versions` group) are compared, never the whole library. A
fingerprint is chromaprint's raw one (`fpcalc -raw`) of the first 120 s; two files score the share of equal bits
at the best alignment within +-60 frames (~+-7 s), over the overlapping frames only, so a short intro or a
trimmed end does not lower the score. Pitch or tempo changes (sped-up, nightcore) break chromaprint: out of scope.

Fingerprinting takes a few tenths of a second per file, so readers never run it: `musicdb versions --json` scores
the pairs whose fingerprints are cached and lists the files still missing; `musicdb versions fingerprint`
(run by rormpc's Versions pane in the background and by the hourly `musicdb update`) fills the cache, kept per
(path, size, mtime) in ~/.cache/rormpc-tools/fingerprints.json. A file fpcalc cannot read is cached as failed
until it changes, so it is not retried on every run.
"""
import array, base64, concurrent.futures, json, os, shutil, subprocess

from . import musicdb, settings

CACHE = settings.XDG_CACHE / "rormpc-tools" / "fingerprints.json"
SECONDS = 120  # fpcalc -length: the start of the file is enough to tell copies from other recordings
MAX_SHIFT = 60  # frames (~0.124 s each): a different intro or silence up to ~7 s
MIN_OVERLAP = 240  # frames (~30 s): a shorter overlap says nothing
# Measured 2026-10-07 on the 72 file pairs of the Versions groups: the same recording 0.87-0.99 (a bass-boosted
# re-upload too); live, remix, edit and other performances 0.51-0.69; 0.75-0.83 in between (maybe another master
# or edit). Recalibrate when the library grows.
SAME = 0.88
SIMILAR = 0.72


def available():
    return shutil.which("fpcalc") is not None


def run_fpcalc(path):
    """[uint32] of the first SECONDS of the file, or None when fpcalc cannot read it."""
    try:
        p = subprocess.run(["fpcalc", "-raw", "-json", "-length", str(SECONDS), str(path)], capture_output=True,
                           text=True, stdin=subprocess.DEVNULL, timeout=120)
        fp = json.loads(p.stdout)["fingerprint"] if p.returncode == 0 else None
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired):
        return None
    return [x & 0xFFFFFFFF for x in fp] if fp else None


def pack(fp):
    return base64.b64encode(array.array("I", fp).tobytes()).decode()


def unpack(text):
    a = array.array("I")
    a.frombytes(base64.b64decode(text))
    return list(a)


def load_cache():
    try:
        return json.loads(CACHE.read_text())
    except (OSError, ValueError):
        return {}


def save_cache(cache):
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CACHE.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(cache, sort_keys=True))
    tmp.replace(CACHE)


def stat(rel):
    try:
        st = (musicdb.MUSIC / rel).stat()
    except OSError:
        return None
    return st.st_size, st.st_mtime


def fresh(entry, st):
    return entry is not None and st is not None and (entry.get("size"), entry.get("mtime")) == st


def cached(files):
    """({rel: fingerprint}, [rel missing], [rel failed]) from the cache only; never runs fpcalc."""
    cache = load_cache()
    out, missing, failed = {}, [], []
    for rel in files:
        e, st = cache.get(rel), stat(rel)
        if not fresh(e, st):
            missing.append(rel)
        elif e.get("fp"):
            out[rel] = unpack(e["fp"])
        else:
            failed.append(rel)
    return out, missing, failed


def compute(files, fpcalc=run_fpcalc):
    """Fingerprint the files whose cache entry is missing or stale; returns (computed, failed) counts."""
    cache = load_cache()
    todo = [(rel, st) for rel in files if (st := stat(rel)) and not fresh(cache.get(rel), st)]
    done = failed = 0
    with concurrent.futures.ThreadPoolExecutor(os.cpu_count() or 4) as ex:
        for (rel, st), fp in zip(todo, ex.map(lambda t: fpcalc(musicdb.MUSIC / t[0]), todo)):
            cache[rel] = {"size": st[0], "mtime": st[1], "fp": pack(fp) if fp else None}
            done, failed = done + bool(fp), failed + (not fp)
    if todo or any(stat(rel) is None for rel in cache):
        save_cache({rel: e for rel, e in cache.items() if stat(rel) is not None})  # deleted files drop out
    return done, failed


def to_bytes(fp):
    return array.array("I", fp).tobytes()


def compare(a, b):
    """(score, shift in frames, overlap in frames): the best share of equal bits over the overlapping frames
    of b shifted by -MAX_SHIFT..MAX_SHIFT against a; (0.0, 0, 0) when no shift overlaps MIN_OVERLAP frames."""
    ab, bb = to_bytes(a), to_bytes(b)
    best = (0.0, 0, 0)
    for shift in range(-MAX_SHIFT, MAX_SHIFT + 1):
        i, j = max(0, shift), max(0, -shift)  # a[i + k] against b[j + k]
        n = min(len(a) - i, len(b) - j)
        if n < MIN_OVERLAP:
            continue
        x = int.from_bytes(ab[4 * i:4 * (i + n)]) ^ int.from_bytes(bb[4 * j:4 * (j + n)])
        score = 1 - x.bit_count() / (32 * n)
        if score > best[0]:
            best = (score, shift, n)
    return best


FRAME_S = 0.1238  # chromaprint's frame step at its 11025 Hz sample rate


def kind(score):
    return "same" if score >= SAME else "similar" if score >= SIMILAR else None


def pairs(fps):
    """[{a, b, score, kind, shift_s, overlap_s}] for every pair of fingerprinted files scoring SIMILAR or more."""
    out = []
    files = sorted(fps)
    for x, a in enumerate(files):
        for b in files[x + 1:]:
            score, shift, n = compare(fps[a], fps[b])
            if kind(score):
                out.append({"a": a, "b": b, "score": round(score, 3), "kind": kind(score),
                            "shift_s": round(shift * FRAME_S, 1), "overlap_s": round(n * FRAME_S)})
    return out


def clusters(same_pairs):
    """Files joined by "same" pairs, as sorted lists of two or more."""
    parent = {}

    def root(f):
        while parent.setdefault(f, f) != f:
            f = parent[f]
        return f
    for p in same_pairs:
        parent[root(p["a"])] = root(p["b"])
    groups = {}
    for f in parent:
        groups.setdefault(root(f), []).append(f)
    return sorted(sorted(g) for g in groups.values() if len(g) > 1)
