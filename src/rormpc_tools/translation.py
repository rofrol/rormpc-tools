"""Polish translations of a song's lyrics from tekstowo.pl, kept beside the lyrics for rormpc's Lyrics pane.

One song per call, on demand (`musicdb lyrics translate`): tekstowo.pl has no API, so this reads its HTML (its
robots.txt allows song and search pages; its terms allow private use). At most three requests per call, 2 s apart:
the song page under its usual URL, then the site search and the best result when that page is missing or holds
other lyrics. Nothing crawls or runs in batch.

Storage: <lyrics_dir>/<song path stem>.pl.json next to the .lrc/.txt, which stay untouched, and outside index.json:
  state           translated | none (the page has no translation) | not_found | mismatch (pages found hold other
                  lyrics) | instrumental | original_pl (the original is Polish: nothing to translate)
  source, url     "tekstowo.pl" and the song page; kind: human | machine (tekstowo's own AI translation) | mine
  checked_at      when it was fetched
  original_file   lrc | txt; original_hash: fnv1a64 of the original's lines joined by "\\n" (stale when it changes)
  original_lang   ISO 639-1 code, detected once (langdetect) unless original_lang_manual (`musicdb lyrics lang`)
  pairing         line (each unit is one original line and its translation: safe to highlight), stanza (a unit is a
                  run of original lines and a stanza of the translation, or with "line": true one line of a stanza
                  that pairs line by line) or none (one unit: the whole text)
  units           [{"ids": [original line ids, consecutive], "text": [translated lines], "line": bool (optional)}]
Line ids number the original's lines as rormpc reads them: for .lrc every line with a timestamp tag in file order,
for .txt every line. A translation of kind "mine" is never overwritten.
"""
import datetime as dt, difflib, html.parser, json, re, time, unicodedata, urllib.error, urllib.parse, urllib.request

from . import settings

LYRICS = settings.LYRICS_DIR
SITE = "https://www.tekstowo.pl"
UA = f"rormpc-tools/{settings.version()} (musicdb lyrics: one song's translation on request; {settings.CONTACT})"
INTERVAL = 2.0  # seconds between requests to tekstowo.pl: politeness, it has no published rate limit
MATCH = 0.45  # word overlap (Jaccard) above which a page holds our song's lyrics; other songs score about 0.1-0.3
_last = [0.0]


def sidecar(rel):
    return (LYRICS / rel).with_suffix(".pl.json")


def split_lines(text):
    """Lines the way Rust's str::lines splits them (rormpc numbers them the same way)."""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return [l[:-1] if l.endswith("\r") else l for l in lines]


def lrc_lines(text):
    """The lyrics of an .lrc, one entry per line with a timestamp tag, in file order (as rmpc's parser reads them)."""
    out = []
    for line in split_lines(text):
        s = line.strip()
        if not s.startswith("["):
            continue
        stamps = 0
        while s.startswith("["):
            end = s.find("]")
            tag = s[1:end] if end > 0 else ""
            if not (tag[:1].isdigit() and ":" in tag):
                break
            stamps += 1
            s = s[end + 1:]
        if stamps:
            out.append(s.strip())
    return out


def original(rel):
    """("lrc" | "txt", lines) of the song's lyrics file, or None without one."""
    base = LYRICS / rel
    for kind in ("lrc", "txt"):
        p = base.with_suffix("." + kind)
        if p.exists():
            text = p.read_text(errors="replace")
            return kind, lrc_lines(text) if kind == "lrc" else split_lines(text)
    return None


def fnv1a64(s):
    h = 0xcbf29ce484222325
    for b in s.encode():
        h = ((h ^ b) * 0x100000001b3) & 0xffffffffffffffff
    return f"{h:016x}"


def lines_hash(lines):
    return "fnv1a64:" + fnv1a64("\n".join(lines))


def detect_lang(lines):
    """ISO 639-1 code of the lyrics' language, "und" when there is too little text to tell."""
    text = "\n".join(lines)
    if sum(c.isalpha() for c in text) < 20:
        return "und"
    from langdetect import DetectorFactory, detect
    from langdetect.lang_detect_exception import LangDetectException
    DetectorFactory.seed = 0  # langdetect is random otherwise: the same lyrics could flip between runs
    try:
        return detect(text)
    except LangDetectException:
        return "und"


def load(rel):
    p = sidecar(rel)
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return None


def save(rel, rec):
    p = sidecar(rel)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(rec, ensure_ascii=False, indent=1))
    tmp.replace(p)


# ---------------------------------------------------------------- tekstowo.pl

def get_html(url):
    """The page's HTML, None on 404."""
    wait = _last[0] + INTERVAL - time.time()
    if wait > 0:
        time.sleep(wait)
    err = None
    for attempt in range(3):
        if attempt:
            time.sleep(5 * attempt)  # designed backoff: tekstowo.pl's transient 429/5xx
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html"})
            with urllib.request.urlopen(req, timeout=20) as r:
                _last[0] = time.time()
                return r.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            _last[0] = time.time()
            if e.code == 404:
                return None
            if e.code not in (429, 500, 502, 503, 504):
                raise
            err = e
        except (urllib.error.URLError, TimeoutError, ConnectionResetError) as e:
            err = e
    raise RuntimeError(f"tekstowo.pl: {err}")


class _Text(html.parser.HTMLParser):
    """Text of the first <div> with the class `inner-text` inside the element with id `scope`; <br> ends a line."""

    def __init__(self, scope):
        super().__init__(convert_charrefs=True)
        self.scope, self.in_scope, self.depth, self.parts, self.done, self.classes = scope, 0, 0, [], False, ""

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if self.done:
            return
        if self.depth:
            if tag == "br":
                self.parts.append(None)
            elif tag not in ("br", "img", "input", "hr", "meta", "link"):
                self.depth += 1
            return
        if self.in_scope:
            if tag != "br":
                self.in_scope += 1
            if tag == "div" and "inner-text" in (a.get("class") or "").split():
                self.depth, self.classes = 1, a.get("class") or ""
        elif a.get("id") == self.scope:
            self.in_scope = 1

    def handle_startendtag(self, tag, attrs):
        if self.depth and tag == "br":
            self.parts.append(None)

    def handle_endtag(self, tag):
        if self.done or tag == "br":
            return
        if self.depth:
            self.depth -= 1
            if not self.depth:
                self.done = True
        elif self.in_scope:
            self.in_scope -= 1

    def handle_data(self, data):
        if self.depth:
            self.parts.append(data)


def _br_split(parts):
    """Join text parts into lines, breaking only at <br> markers (None) when there are any."""
    if None not in parts:
        return split_lines("".join(parts))
    lines, cur = [], []
    for p in parts:
        if p is None:
            lines.append("".join(cur).replace("\n", " "))
            cur = []
        else:
            cur.append(p)
    lines.append("".join(cur).replace("\n", " "))
    while lines and not lines[-1].strip():
        lines.pop()
    while lines and not lines[0].strip():
        lines.pop(0)
    return lines


def _inner(page, scope):
    p = _Text(scope)
    p.feed(page)
    return [l.strip() for l in _br_split(p.parts)], p.classes


def parse_song(page):
    """{"original": [lines], "translation": [lines] (empty: none), "machine": bool} of a song page."""
    orig, _ = _inner(page, "songText")
    trans, classes = _inner(page, "translation")
    machine = "auto-translation" in classes.split() or bool(re.search(r"<h2[^>]*>\s*Tłumaczenie AI", page))
    return {"original": orig, "translation": trans, "machine": machine}


_RESULT = re.compile(r'<a href="(/[^"]+)"\s+class="title"\s+title="([^"]*)"[^>]*>.*?</a>\s*</div>\s*'
                     r'<div class="flex-group">\s*<i title="([^"]*)" class="icon i18 ([^"]*)"', re.S)


def parse_search(page):
    """Song results of a search page: [{"url", "label" ("Artist - Title"), "translation": human|machine|None}]."""
    songs = page.split("Znalezieni artyści")[0]
    out = []
    for href, label, _t, icon in _RESULT.findall(songs):
        kind = "machine" if "icon_ai_trans" in icon else "human" if "icon_pl" in icon else None
        out.append({"url": SITE + href, "label": html.unescape(label), "translation": kind})
    return out


# ---------------------------------------------------------------- matching

def norm(s):
    s = unicodedata.normalize("NFKD", (s or "").lower().replace("ł", "l"))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^\w]+", " ", s).replace("_", " ").split())


def clean_title(title):
    """The title without feat./version/remaster suffixes, which tekstowo.pl's titles usually lack."""
    t = re.sub(r"\s*[\(\[][^)\]]*(feat|ft\.|with|remaster|version|edit|mix|live|official|video|audio|lyric)[^)\]]*[\)\]]",
               "", title or "", flags=re.I)
    t = re.sub(r"\s+-\s+.*(remaster|version|edit|mix|live|mono|stereo).*$", "", t, flags=re.I)
    return re.sub(r"\s+(feat\.?|ft\.?)\s.*$", "", t, flags=re.I).strip()


def slug(s):
    return "-".join(norm(s).split())


def words(lines):
    return {w for l in lines for w in norm(l).split() if len(w) > 1}


def similarity(ours, theirs):
    a, b = words(ours), words(theirs)
    return len(a & b) / len(a | b) if a and b else 0.0


def rank_results(results, artist, title):
    """Search results that are this artist's song with this title, best first."""
    na, nt = norm(artist), norm(clean_title(title))
    ranked = []
    for r in results:
        their_artist, _, their_title = r["label"].partition(" - ")
        ta, tt = norm(their_artist), norm(their_title)
        if not (ta == na or (na and (na in ta or ta in na))):
            continue
        if tt == nt:
            score = 3
        elif norm(clean_title(their_title)) == nt:
            score = 2
        elif tt.startswith(nt):
            score = 1
        else:
            continue
        ranked.append((-score, r["translation"] is None, r["translation"] == "machine", r))
    return [r for *_k, r in sorted(ranked, key=lambda k: k[:3])]


def is_instrumental(lines):
    text = norm(" ".join(lines))
    return text in ("instrumental", "instrumentalny", "utwor instrumentalny", "instrumental song", "")


# ---------------------------------------------------------------- alignment

def stanzas(lines):
    """Lists of indices of non-blank lines, split at blank lines."""
    out, cur = [], []
    for i, l in enumerate(lines):
        if l.strip():
            cur.append(i)
        elif cur:
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return out


def map_ours(ours, theirs):
    """Our line id -> the index of their (tekstowo original) line with the same words. Repeated choruses that the
    page writes once map to that one line."""
    oi = [i for i, l in enumerate(ours) if norm(l)]
    ti = [j for j, l in enumerate(theirs) if norm(l)]
    on, tn = [norm(ours[i]) for i in oi], [norm(theirs[j]) for j in ti]
    out = {}
    sm = difflib.SequenceMatcher(None, on, tn, autojunk=False)
    for op, a1, a2, b1, b2 in sm.get_opcodes():
        if op == "equal":
            for k in range(a2 - a1):
                out[oi[a1 + k]] = ti[b1 + k]
        elif op == "replace" and a2 - a1 == b2 - b1:
            for k in range(a2 - a1):
                if difflib.SequenceMatcher(None, on[a1 + k], tn[b1 + k]).ratio() >= 0.6:
                    out[oi[a1 + k]] = ti[b1 + k]
    # lines the page writes once, or without the backing vocals in parentheses
    bare = lambda l: norm(re.sub(r"\([^)]*\)", " ", l))
    first = {}
    for j, n in zip(ti, tn):
        first.setdefault(n, j)
    for j in ti:
        if bare(theirs[j]):
            first.setdefault(bare(theirs[j]), j)
    for i, n in zip(oi, on):
        j = first.get(n, first.get(bare(ours[i]) or None))
        if i not in out and j is not None:
            out[i] = j
    return out


def runs(ids):
    """Consecutive runs of sorted ids."""
    out = []
    for i in ids:
        if out and out[-1][-1] == i - 1:
            out[-1].append(i)
        else:
            out.append([i])
    return out


def align(ours, their_orig, their_pl):
    """(pairing, units) of the page's translation against our original lines (see the module doc)."""
    nonblank = [i for i, l in enumerate(ours) if l.strip()]
    if not nonblank:
        return "none", []
    to_ = [j for j, l in enumerate(their_orig) if l.strip()]
    tp = [j for j, l in enumerate(their_pl) if l.strip()]
    so, sp = stanzas(their_orig), stanzas(their_pl)
    if len(sp) > len(so) and [len(s) for s in sp[:len(so)]] == [len(s) for s in so]:
        # stanzas after the last one of the original: the translator's footnotes ("* ...")
        their_pl = their_pl[:sp[len(so) - 1][-1] + 1]
        tp, sp = [j for j, l in enumerate(their_pl) if l.strip()], sp[:len(so)]
    # the same number of lines is 1:1 unless the stanzas, equal in number, differ in length (merged verses)
    same_shape = len(so) != len(sp) or [len(s) for s in so] == [len(s) for s in sp]
    line_pl = dict(zip(to_, tp)) if len(to_) == len(tp) and same_shape else {}
    stanza_of = {j: k for k, st in enumerate(so) for j in st}
    mapping = map_ours(ours, their_orig)
    covered = [i for i in nonblank if i in mapping]
    if line_pl:
        return "line", [{"ids": [i], "text": [their_pl[line_pl[mapping[i]]]]} for i in covered]
    if len(so) == len(sp) and len(covered) >= 0.5 * len(nonblank):
        # stanzas of the same length pair line by line ("line": true), the others as a whole
        units, prev = [], None
        for i in covered:
            j = mapping[i]
            k = stanza_of[j]
            if len(so[k]) == len(sp[k]):
                units.append({"ids": [i], "text": [their_pl[sp[k][so[k].index(j)]]], "line": True})
                prev = None
                continue
            if units and k == prev and units[-1]["ids"][-1] == i - 1:
                units[-1]["ids"].append(i)
            else:
                units.append({"ids": [i], "text": [their_pl[j] for j in sp[k]]})
            prev = k
        return "stanza", units
    return "none", [{"ids": list(range(nonblank[0], nonblank[-1] + 1)), "text": their_pl}]


# ---------------------------------------------------------------- the command

def translate(rel, artist, title, index_state=None):
    """Fetch the Polish translation of one song and store it; returns the sidecar record (or None when the song
    has no lyrics to translate yet). Raises on a translation of kind "mine" (never overwritten)."""
    old = load(rel) or {}
    if old.get("kind") == "mine":
        raise RuntimeError("the translation is your own: not replaced")
    now = dt.datetime.now().isoformat(timespec="seconds")
    found = original(rel)
    if not found:
        if index_state == "instrumental":
            rec = {"version": 1, "lang": "pl", "state": "instrumental", "checked_at": now}
            save(rel, rec)
            return rec
        return None
    kind, lines = found
    manual = bool(old.get("original_lang_manual"))
    lang = old.get("original_lang") if manual else detect_lang(lines)
    rec = {"version": 1, "lang": "pl", "checked_at": now, "original_file": kind, "original_hash": lines_hash(lines),
           "original_lang": lang, "original_lang_manual": manual}
    if lang == "pl":
        rec["state"] = "original_pl"
        save(rel, rec)
        return rec
    rec.update(fetch_translation(lines, artist, title))
    save(rel, rec)
    return rec


def fetch_translation(lines, artist, title):
    """The state, source and units of the best tekstowo.pl page for these lyrics."""
    if not (artist and title):
        return {"state": "not_found", "note": "no artist/title tags"}
    tried, pages = set(), 0
    best_miss = None

    def page_result(url):
        nonlocal best_miss
        page = get_html(url)
        tried.add(url)
        if page is None:
            return None
        song = parse_song(page)
        if is_instrumental(song["original"]) and is_instrumental(lines):
            return {"state": "instrumental", "source": "tekstowo.pl", "url": url}
        sim = similarity(lines, song["original"])
        if sim < MATCH:
            if not best_miss or sim > best_miss["similarity"]:
                best_miss = {"state": "mismatch", "source": "tekstowo.pl", "url": url, "similarity": round(sim, 2)}
            return None
        base = {"source": "tekstowo.pl", "url": url, "similarity": round(sim, 2)}
        if not song["translation"]:
            return base | {"state": "none"}
        pairing, units = align(lines, song["original"], song["translation"])
        return base | {"state": "translated", "kind": "machine" if song["machine"] else "human", "pairing": pairing,
                       "units": units}

    guess = f"{SITE}/{slug(artist)}/{slug(clean_title(title))}"
    got = page_result(guess)
    pages += 1
    if got:
        return got
    results = get_html(f"{SITE}/szukaj?" + urllib.parse.urlencode({"search-query": f"{artist} {clean_title(title)}"}))
    for r in rank_results(parse_search(results or ""), artist, title):
        if r["url"] in tried:
            continue
        if pages >= 2:
            break
        got = page_result(r["url"])
        pages += 1
        if got:
            return got
    return best_miss or {"state": "not_found"}


def set_lang(rel, code):
    """Override the original's detected language ("auto" detects it again); keeps any translation."""
    rec = load(rel) or {"version": 1, "lang": "pl", "state": "none"}
    found = original(rel)
    if code == "auto":
        rec["original_lang"] = detect_lang(found[1]) if found else "und"
        rec["original_lang_manual"] = False
    else:
        rec["original_lang"], rec["original_lang_manual"] = code, True
    if found:
        rec["original_file"], rec["original_hash"] = found[0], lines_hash(found[1])
    if rec["original_lang"] == "pl" and rec.get("kind") != "mine":
        rec["state"] = "original_pl"
    elif rec.get("state") == "original_pl":
        rec["state"] = "none"
        rec["note"] = "not Polish: run musicdb lyrics translate"
    save(rel, rec)
    return rec


def describe(rec):
    """One status line for rormpc."""
    if rec is None:
        return "no lyrics to translate yet"
    s = rec["state"]
    if s == "translated":
        how = {"line": "line by line", "stanza": "by stanza", "none": "whole text"}[rec.get("pairing", "none")]
        return f"Polish translation ({rec.get('kind')}, {how}) from tekstowo.pl"
    return {"none": "tekstowo.pl has the song but no Polish translation",
            "not_found": "tekstowo.pl does not have this song",
            "mismatch": "tekstowo.pl's page holds other lyrics: no translation taken",
            "instrumental": "instrumental: nothing to translate",
            "original_pl": "the original is Polish: nothing to translate"}.get(s, s)
