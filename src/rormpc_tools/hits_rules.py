"""The selection rules of `hits`: ± sets, Rank by and Years of (rormpc's plans/combined-view.md, phase 1).

    selection = (union of + sets, or the whole library when no set is +)
              - (union of - sets)
              ∩ period ∩ genres ∩ artists ∩ Top % ∩ owned

Top % needs a rank and is computed in the rank's own population (the Billboard cohort of the chosen years, the
library by my plays), never among the songs the sets or the genre/artist filters leave, so a song's rank does not
depend on which chips are on. A song outside the population has no rank and stays only while Top % is off.

Everything here is pure: hits.py gathers the candidates (library songs, chart rows, recommendations) and the
members of each set, this module decides which of them are selected and in which order.
"""
import math, re, sys
from types import SimpleNamespace

# set kinds with a fixed chip row in rormpc's Hits pane: key -> name in the printed formula
SET_KINDS = {"billboard": "Billboard", "likes": "Likes", "playlists": "Playlists", "recommended": "Recommended"}
SET_ALIASES = {"recs": "recommended", "like": "likes", "playlist": "playlists", "charts": "billboard"}
# KIND:NAME sets picked through rormpc's "+ set…" (tag lists, stored MPD playlists, Live playlists, smart lists);
# hits_sets.py reads their members and keeps their keys canonical
NAMED_KINDS = ("tag", "playlist", "live", "list")
NAMED_LABELS = {"tag": "Tag", "playlist": "Playlist", "live": "Live", "list": "Smart"}  # in the formula
RANKS = ("billboard", "plays", "rediscover", "none")
YEARS_OF = ("release", "chart", "listened")
RULES_SCHEMA = 1

# the old `--source` as a shorthand: (sets, rank, years of); likes/library/playlists take the rank from --sort
LEGACY = {
    "billboard": ({"billboard": 1}, "billboard", "chart"),
    "likes": ({"likes": 1}, "sort", "release"),
    "library": ({}, "sort", "release"),
    "playlists": ({"playlists": 1}, "sort", "release"),
    "mine": ({}, "plays", "listened"),
    "recs": ({"recommended": 1}, "none", "release"),
}


def default_years_of(rank):
    """Years of follows Rank by: Billboard -> chart year, my plays -> listened year, else release year."""
    return {"billboard": "chart", "plays": "listened"}.get(rank, "release")


def default_rank(sets):
    """Rank by when only sets are given: Billboard when it is +, nothing for recommendations alone, else my plays."""
    plus = [k for k, s in sets.items() if s > 0]
    if "billboard" in plus:
        return "billboard"
    return "none" if plus == ["recommended"] else "plays"


def parse_set(tok):
    """'+billboard', '-likes', 'recs' (no sign = +) -> ('billboard', 1); '+tag:God' -> ('tag:God', 1). A name
    keeps its case, colons and commas; its spaces are trimmed and collapsed (hits_sets.canonical stores it)."""
    tok = (tok or "").strip()
    sign = -1 if tok[:1] == "-" else 1
    body = tok[1:].strip() if tok[:1] in "+-" else tok
    kind, colon, name = body.partition(":")
    kind = kind.strip().lower() if colon else SET_ALIASES.get(kind.lower(), kind.lower())
    if colon:
        if kind not in NAMED_KINDS:
            raise ValueError(f"unknown set kind {kind!r} in {tok!r} (named sets: {', '.join(NAMED_KINDS)})")
        name = re.sub(r"\s+", " ", name.strip())
        if not name:
            raise ValueError(f"set {tok!r}: give a name after {kind}:")
        return f"{kind}:{name}", sign
    if kind not in SET_KINDS:
        raise ValueError(f"unknown set {tok!r}; use ±{'|'.join(SET_KINDS)}")
    return kind, sign


def parse_sets(tokens):
    """--set values in order -> {key: +1|-1}; a later value for the same set wins. Commas separate fixed sets
    ("+billboard,-likes"); a value naming a KIND:NAME set is that one set, commas and all."""
    out = {}
    for tok in tokens or []:
        parts = [tok] if ":" in tok else tok.split(",")
        for part in filter(None, (p.strip() for p in parts)):
            k, sign = parse_set(part)
            out[k] = sign
    return out


def resolve(source=None, sets=None, rank=None, years_of=None, sort="plays"):
    """The rules from the command line: the new options, or the old --source mapped onto them. Returns a namespace
    {sets, rank, years_of, order, source}: `order` "listens" is the old `--rank listens` (the Billboard cohort by
    ListenBrainz listens), `source` the old source name when the rules came from one (labels, playlist names)."""
    if source and sets:
        raise ValueError("--source and --set: use one (--source is the old shorthand of --set/--rank/--years-of)")
    order = "listens" if rank == "listens" else None
    if not sets and not source and rank in (None, "chart", "listens") and years_of is None:
        source = "billboard"  # `hits 1980s` as always, also with the old --rank chart|listens
    rank = "billboard" if rank in ("chart", "listens") else rank
    if source:
        s, r, y = LEGACY[source]
        r = sort if r == "sort" else r
        if rank and rank != r:
            r, y = rank, default_years_of(rank)
        return SimpleNamespace(sets=dict(s), rank=r, years_of=years_of or y, order=order, source=source)
    parsed = parse_sets(sets)
    rank = rank or default_rank(parsed)
    return SimpleNamespace(sets=parsed, rank=rank, years_of=years_of or default_years_of(rank), order=order,
                           source=None)


def legacy_source(rules):
    """The old --source these rules equal, if any (for its label); None for a combination it never had."""
    for name, (s, r, y) in LEGACY.items():
        ranks = ("plays", "rediscover") if r == "sort" else (r,)
        if rules.sets == s and rules.rank in ranks and rules.years_of == y:
            return name
    return None


def as_dict(rules):
    """The rules as JSON: sets as {key: ±1}; a smart list stores them with its period, Top %, genres, artists
    and owned (smartlists.rules_of)."""
    return {"schema": RULES_SCHEMA, "sets": dict(rules.sets), "rank": rules.rank, "years_of": rules.years_of}


# ---------------------------------------------------------------- Top %

def parse_top(spec):
    """'1-10,11-20' -> [(1, 10), (11, 20)] percent ranges of the rank."""
    out = []
    for part in filter(None, (x.strip() for x in (spec or "").split(","))):
        lo, _, hi = part.partition("-")
        out.append((int(lo), int(hi or lo)))
    return out


def in_top(i, n, ranges):
    """Is rank i (1-based) of n inside one of the percent ranges? Top 10% = ranks 1..ceil(0.1 n)."""
    return any(math.ceil((lo - 1) / 100 * n) < i <= math.ceil(hi / 100 * n) for lo, hi in ranges)


def top_for(rules, spec):
    """The Top % ranges to cut by, or None. With Rank by none there is no rank to cut: only "1-100" (everything,
    what rormpc passes with no box ticked) is accepted, anything else is refused."""
    top = parse_top(spec)
    if rules.rank == "none" and top:
        if top == [(1, 100)]:
            return None
        raise ValueError("Top % needs a rank: --rank none has none (tick no Top % range)")
    return top or None


# ---------------------------------------------------------------- years

def axis_years(c, years_of):
    """The candidate's years on the chosen axis."""
    if years_of == "chart":
        return set(c.get("chart_years") or ())
    if years_of == "listened":
        return {y for y, n in (c.get("listened") or {}).items() if n}
    return {c["release"]} if c.get("release") else set()


def in_period(c, years_of, wanted):
    """No period = every year; with one, a song without a year on the axis is out."""
    return not wanted or bool(axis_years(c, years_of) & wanted)


# ---------------------------------------------------------------- rank population

def score(c, rules, wanted):
    """The rank's score of a candidate (higher ranks first)."""
    if rules.rank == "plays" and rules.years_of == "listened":
        return sum(n for y, n in (c.get("listened") or {}).items() if not wanted or y in wanted)
    if rules.rank == "rediscover":
        return math.log1p(c.get("plays", 0)) * min(c.get("idle_days", 3650), 365)
    return c.get("plays", 0)


def population(cands, rules, wanted):
    """The keys of the rank's population, best first: Billboard = the chart rows of the period (by best year-end
    position, then points summed over years, then ListenBrainz listens); my plays / rediscover = the library songs
    of the period (my plays in listening years: the songs played in them). Sets, genres, artists, owned and
    hidden songs never change it."""
    if rules.rank == "none":
        return []
    if rules.rank == "billboard":
        pop = [k for k, c in cands.items() if c.get("chart_years") and in_period(c, rules.years_of, wanted)]
        if rules.order == "listens":
            key = lambda k: (-cands[k].get("listens", 0), cands[k].get("best", 101), cands[k]["title"])
        else:
            key = lambda k: (-cands[k]["peak"], -cands[k]["points"], -cands[k].get("listens", 0), cands[k]["title"])
        return sorted(pop, key=key)
    pop = []
    for k, c in cands.items():
        if not c.get("file") or not in_period(c, rules.years_of, wanted):
            continue
        c["score"] = score(c, rules, wanted)
        if rules.years_of == "listened" and rules.rank == "plays" and not c["score"]:
            continue
        pop.append(k)
    return sorted(pop, key=lambda k: (-cands[k]["score"], cands[k]["artist"], cands[k]["title"]))


# ---------------------------------------------------------------- selection

def base_keys(cands, members, sets):
    """(union of + sets, or every library song when no set is +) - (union of - sets)."""
    plus = [k for k, s in sets.items() if s > 0]
    minus = [k for k, s in sets.items() if s < 0]
    base = set().union(*(members.get(k, ()) for k in plus)) if plus else {k for k, c in cands.items() if c.get("file")}
    return base - set().union(*(members.get(k, ()) for k in minus))


def select(cands, members, rules, *, wanted=(), top=None, owned=False, show_hidden=False, n=0,
           artist_ok=lambda c: True, genre_ok=lambda c: True):
    """Apply the rules: returns (rows best first, info). Rows are the candidate dicts with rank, pct, cohort and
    ranked set; unranked rows follow the ranked ones in their own order (`order`). `n` (0 = all) cuts the list
    when there is no Top %. info: candidates (after the set algebra), cohort (the population size) and the
    selected rows' artists before the Top % cut (`pool`, for the artist picker)."""
    wanted = set(wanted or ())
    pop = population(cands, rules, wanted)
    ranks = {k: i for i, k in enumerate(pop, 1)}
    base = base_keys(cands, members, rules.sets)
    pool = [cands[k] for k in base if in_period(cands[k], rules.years_of, wanted)]
    for c in pool:
        c["sets"] = sorted(s for s in members if c["key"] in members[s])
    rows = []
    for c in pool:
        i = ranks.get(c["key"])
        if top and not (i and in_top(i, len(pop), top)):
            continue
        if owned and not c.get("file"):
            continue
        if c.get("hidden") and not show_hidden:
            continue
        # the costly filters last (genres may ask MusicBrainz for an artist)
        if not artist_ok(c) or not genre_ok(c):
            continue
        rows.append(c)
    ranked = sorted((c for c in rows if c["key"] in ranks), key=lambda c: ranks[c["key"]])
    unranked = sorted((c for c in rows if c["key"] not in ranks), key=lambda c: c["order"])
    for c in ranked:
        c.update(rank=ranks[c["key"]], pct=round(100 * ranks[c["key"]] / len(pop), 1), cohort=len(pop), ranked=True)
    # unranked rows still get a unique number (the pane keeps its selection by it, `hits fetch --rank` picks by it)
    for j, c in enumerate(unranked, 1):
        c.update(rank=len(pop) + j, pct=0, cohort=0, ranked=False)
    rows = ranked + unranked
    if not top and n:
        rows = rows[:n]
    visible = [c for c in pool if show_hidden or not c.get("hidden")]
    return rows, {"candidates": len(base), "cohort": len(pop), "pool": visible}


# ---------------------------------------------------------------- exceptions

def exception_applies(e, rules):
    """library always; set:KIND (set:tag:NAME, set:list:ID, ...) while that set is +; list:ID while that smart list is open (`rules.list`, set by
    --list / --open-list) (hits_exceptions.applies)."""
    kind, _, key = e["scope"].partition(":")
    if kind == "list":
        return bool(key) and getattr(rules, "list", None) == key
    return e["scope"] == "library" or (kind == "set" and rules.sets.get(key, 0) > 0)


def mark(c, rules, exceptions):
    """Set a candidate's exceptions (each with `applies`), `pinned` and `excluded` (applicable ones only), and
    `hidden` (an applicable `hits hide`)."""
    c["exceptions"] = [dict(e, applies=exception_applies(e, rules)) for e in exceptions]
    on = [e for e in c["exceptions"] if e["applies"]]
    c["excluded"] = any(e["action"] == "exclude" for e in on)
    c["pinned"] = any(e["action"] == "pin" for e in on)
    c["hidden"] = any(e.get("via") == "hide" for e in on)


def apply_exceptions(rows, cands, rules, by_key, *, show_excluded=False, pins=True):
    """The exceptions, after the Top % cut (ranks never move): any applicable exclusion takes a row out (kept,
    marked, with `show_excluded`), and every applicable pin of an owned song that the rules left out is added
    after the rows, unranked; a pin beats `-` sets, genres and artists, an exclusion beats any pin. `by_key`:
    {candidate key: [exceptions]}. Returns (rows, {"pinned", "excluded"}): the rows added by a pin (outside the
    rules), the rows an exclusion took out (shown or not)."""
    out, excluded = [], 0
    for c in rows:
        mark(c, rules, by_key.get(c["key"], ()))
        if c["excluded"]:
            excluded += 1
            if not show_excluded:
                continue
        out.append(c)
    shown = {c["key"] for c in rows}
    extra = []
    for k, es in by_key.items() if pins else ():
        c = cands.get(k)
        if k in shown or c is None or not c.get("file"):
            continue
        mark(c, rules, es)
        if c["pinned"] and not c["excluded"]:
            extra.append(c)
    extra.sort(key=lambda c: c["order"])
    last = max((c.get("rank", 0) for c in out), default=0)
    for j, c in enumerate(extra, 1):
        c.update(rank=last + j, pct=0, cohort=0, ranked=False)  # a unique row number, shown as "—"
    out += extra
    return out, {"pinned": len(extra), "excluded": excluded}


# ---------------------------------------------------------------- formula

def _union(names):
    return names[0] if len(names) == 1 else "(" + " ∪ ".join(names) + ")"


def _signed(tokens):
    """[(sign, name)] -> ' ∩ a' / ' ∩ (a ∪ b)' for the included, ' − x' per excluded."""
    inc = [t for s, t in tokens if s > 0]
    out = f" ∩ {_union(inc)}" if inc else ""
    return out + "".join(f" − {t}" for s, t in tokens if s < 0)


def signed_tokens(spec):
    """'+rock -country' / '+Queen; -Toto' -> [(1, 'rock'), (-1, 'country')], split as hits' genre and artist
    filters split them (";" when present, else commas and a space before a sign)."""
    spec = spec or ""
    toks = spec.split(";") if ";" in spec else re.findall(r"[-+]?[^,\s][^,]*?(?=\s+[-+]|,|$)", spec)
    return [(-1 if t.startswith("-") else 1, t.lstrip("+-").strip()) for t in (t.strip() for t in toks) if t]


def set_name(key, names=None):
    """A set's name in the formula: a fixed set's, else `names` (hits_sets.labels), else "Tag God" from the key."""
    if key in SET_KINDS:
        return SET_KINDS[key]
    if names and key in names:
        return names[key]
    kind, _, name = key.partition(":")
    return f"{NAMED_LABELS.get(kind, kind)} {name}"


def formula(rules, *, period=None, top=None, genre="", artist="", owned=False, names=None):
    """The selection in one line, e.g. "(Billboard ∪ Tag God) − Recommended ∩ 1980-1989 ∩ Top 1-10% ∩ rock";
    `names`: {set key: name} of the named sets (hits_sets.labels). rormpc's Hits pane builds the same text from
    its filters (`rule_formula`); keep both in step."""
    plus = [set_name(k, names) for k, s in rules.sets.items() if s > 0]
    minus = [set_name(k, names) for k, s in rules.sets.items() if s < 0]
    out = _union(plus) if plus else "Library"
    if minus:
        out += f" − {_union(minus)}"
    if period:
        out += f" ∩ {period}"
    if top:
        out += " ∩ Top " + ",".join(f"{lo}-{hi}" for lo, hi in top) + "%"
    out += _signed(signed_tokens(genre))
    out += _signed(signed_tokens(artist))
    if owned:
        out += " ∩ owned"
    return out


def summary(text, selected, candidates, pinned=0, excluded=0):
    """"… · 1,204 of 8,312", then " · +2 pinned" (rows a pin added, not counted in the first number) and
    " · 1 excluded" (rows an exclusion took out). rormpc's Hits pane builds the same text (`summary`)."""
    out = f"{text} · {selected - pinned:,} of {candidates:,}"
    return out + (f" · +{pinned} pinned" if pinned else "") + (f" · {excluded} excluded" if excluded else "")


def fail(err):
    """A rules error as the CLI reports it (rormpc shows stderr's last line)."""
    sys.exit(f"hits: {err}")
