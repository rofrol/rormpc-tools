"""Exceptions to hits' rules (rormpc's plans/combined-view.md, phase 2): pins and exclusions with a scope.

  hits except pin --scope library --file PATH          # in every result, whatever the filters say
  hits except exclude --scope set:billboard --id ID    # out of every result while Billboard is a + set
  hits except exclude --chart-key 'toto|africa'        # a chart song you do not have (as `hits hide`)
  hits except pin --scope "list:80s party" --file PATH # only while that smart list is open (name or id)
  hits except remove --scope library --id ID           # drop that exception again
  hits exceptions [--json]                             # every exception, `hits hide` included

- Pin ✚: the song is in, whatever the sets, period, Top %, genres and artists say. It needs an owned file, has
  no rank, sits after the ranked rows and is not part of the ranking or the Top % cut.
- Exclusion ⊘: the song is out. Any applicable exclusion beats any pin; a pin beats `-` sets, genres and artists.
- Scope: `library` (every result) or one set (`set:billboard`, `set:likes`, a named set `set:tag:God`,
  `set:playlist:NAME`, `set:live:ID`, `set:list:ID`): a set-scoped exception applies only while that set is + in
  the selection; `list:<id>` (a smart list, `hits lists`) only while that list is
  open (`hits --list`, or rormpc's Play passing `--open-list`). Deleting a smart list removes its exceptions.

Identity: a library song by music-data's song `id` (songs.jsonl; a merged id leads to the song it was merged
into), a chart song without a file by its chart key (`main artist|title`, as `hits hide`). The log is
`<data_dir>/exceptions.jsonl`, one event per line with its own UUID; folding takes the file order and the later
line wins per (song, scope). `hits hide` keeps its own log and is read here as exclusions scoped to Billboard
(on a recommendation row, which is no chart song, scoped to Recommended, as the hide always worked there).
"""
import argparse, datetime as dt, json, sys, uuid

from . import hits_rules, identity, musicdb

ACTIONS = ("pin", "exclude")


def log_path():
    return musicdb.DATA / "exceptions.jsonl"


def parse_scope(text):
    """'library' | 'set:billboard' (aliases as --set reads them) | 'set:tag:God' (a named set, stored by its
    canonical key, hits_sets.canonical) | 'list:ID' (a smart list by id or name) -> the canonical scope; anything
    else refused."""
    text = (text or "library").strip()
    if text == "library":
        return text
    kind, colon, name = text.partition(":")
    if kind == "set" and colon:
        from . import hits_sets
        key, _ = hits_rules.parse_set(name)
        return f"set:{hits_sets.canonical(key)}"
    if kind == "list" and colon:
        from . import smartlists
        try:
            return f"list:{smartlists.find(name)['id']}"
        except LookupError as err:
            raise ValueError(f"scope {text}: {err}") from None
    raise ValueError(f"unknown scope {text!r}; use library, set:{'|'.join(hits_rules.SET_KINDS)}, "
                     f"set:KIND:NAME ({', '.join(hits_rules.NAMED_KINDS)}) or list:ID")


applies = hits_rules.exception_applies


def survivor(song_id):
    """The song an id stands for now: a merged id leads to the song it was merged into (songs.jsonl `into`)."""
    rows, seen = identity.load()["rows"], set()
    while song_id in rows and rows[song_id].get("state") == "merged" and song_id not in seen:
        seen.add(song_id)
        song_id = rows[song_id].get("into") or song_id
    return song_id


def id_of_file(path):
    row = identity.resolve(path)
    return row["id"] if row else None


def live_path(song_id):
    """The song's file now, or None when it is gone."""
    row = identity.load()["rows"].get(survivor(song_id)) if song_id else None
    return row.get("path") if row and row.get("state") == "live" else None


def target(e):
    return ("id", survivor(e["song"])) if e.get("song") else ("chart", e.get("chart_key"))


def events():
    return musicdb.jsonl(log_path())


def fold(evts=None):
    """The exceptions in force from the log: {(target, scope): event}, a later line replacing an earlier one and
    `remove` dropping it."""
    state = {}
    for e in events() if evts is None else evts:
        k = (target(e), e["scope"])
        if e["action"] == "remove":
            state.pop(k, None)
        elif e["action"] in ACTIONS:
            state[k] = e
    return state


def hide_exclusions():
    """`hits hide`'s log read as exclusions scoped to Billboard, keyed by chart key."""
    from .hits import hidden_set
    return [{"id": f"hide:{key}", "action": "exclude", "scope": "set:billboard", "song": None, "chart_key": key,
             "artist": h.get("artist"), "title": h.get("title"), "ts": h.get("ts"), "via": "hide"}
            for key, h in hidden_set().items()]


def active():
    """Every exception in force: the log's, then the hides."""
    return list(fold().values()) + hide_exclusions()


def by_candidate(cands, exceptions, chart_key):
    """{candidate key: [exceptions]}: a song id matches the candidate's file (its registry id), a chart key the
    candidate's artist and title (`chart_key(artist, title)`). A hide on a recommendation row is scoped to
    Recommended (that row is no chart song)."""
    if not exceptions:
        return {}
    by_id, by_chart = {}, {}
    for e in exceptions:
        kind, value = target(e)
        (by_id if kind == "id" else by_chart).setdefault(value, []).append(e)
    out = {}
    for k, c in cands.items():
        found = []
        if by_id and c.get("file"):
            found += by_id.get(id_of_file(c["file"]), [])
        if by_chart:
            for e in by_chart.get(chart_key(c["artist"], c["title"]), []):
                if e.get("via") == "hide" and k.startswith("rec:"):
                    e = dict(e, scope="set:recommended")
                found.append(e)
        if found:
            out[k] = found
    return out


def record(action, scope, *, song=None, chart_key=None, artist=None, title=None, file=None):
    """Append one event to the log (append-only: the history of decisions stays reviewable in git)."""
    e = {"id": str(uuid.uuid4()), "ts": dt.datetime.now().isoformat(timespec="seconds"), "action": action,
         "scope": scope, "song": song, "chart_key": chart_key, "artist": artist, "title": title, "file": file}
    path = log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        fh.write(json.dumps(e, ensure_ascii=False) + "\n")
    return e


def resolve_target(a):
    """(song id, chart key, file) of the command line's --id / --file / --chart-key."""
    if a.file:
        sid = id_of_file(a.file)
        if not sid:
            raise ValueError(f"{a.file}: not in songs.jsonl yet (run `musicdb identity sync`)")
        return sid, None, a.file
    if a.id:
        if a.id not in identity.load()["rows"]:
            raise ValueError(f"unknown song id {a.id} (not in songs.jsonl)")
        return survivor(a.id), None, live_path(a.id)
    return None, a.chart_key.strip(), None


def except_cmd(argv):
    """hits except pin|exclude|remove --scope S (--id ID | --file PATH | --chart-key KEY) [--artist A --title T]"""
    ap = argparse.ArgumentParser(prog="hits except", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=["pin", "exclude", "remove"])
    ap.add_argument("--scope", default="library", help="library (every result), set:KIND (only while KIND is +) "
                    "or list:ID (only while that smart list is open; its name works too)")
    who = ap.add_mutually_exclusive_group(required=True)
    who.add_argument("--id", help="music-data song id (songs.jsonl)")
    who.add_argument("--file", help="library file (MPD path)")
    who.add_argument("--chart-key", help="a chart song without a file: 'main artist|title' as `hits hide` keys it")
    ap.add_argument("--artist", help="shown in the exceptions list")
    ap.add_argument("--title", help="shown in the exceptions list")
    a = ap.parse_args(argv)
    try:
        scope = parse_scope(a.scope)
        song, chart_key, file = resolve_target(a)
        if a.action == "pin" and not (song and live_path(song)):
            raise ValueError("a pin needs an owned file (a chart song you do not have can only be excluded)")
    except ValueError as err:
        sys.exit(f"hits except: {err}")
    if a.action == "remove":
        return remove(scope, song, chart_key)
    record(a.action, scope, song=song, chart_key=chart_key, artist=a.artist, title=a.title, file=file)
    print(f"{a.action}: {f'{a.artist} - {a.title}' if a.artist else song or chart_key} ({scope})")


def remove(scope, song, chart_key):
    """Drop the exception of (song, scope): a `remove` event, and an unhide when it is a hide."""
    t = ("id", song) if song else ("chart", chart_key)
    found = (t, scope) in fold()
    hide = None
    if t[0] == "chart" and scope == "set:billboard":
        from .hits import HIDDEN, hidden_set
        hide = hidden_set().get(chart_key)
    if not found and not hide:
        sys.exit(f"hits except remove: no exception for {t[1]} in scope {scope}")
    if found:
        record("remove", scope, song=song, chart_key=chart_key)
    if hide:
        e = {"ts": dt.datetime.now().isoformat(timespec="seconds"), "action": "unhide", "key": chart_key,
             "artist": hide.get("artist"), "title": hide.get("title"), "mbid": hide.get("mbid")}
        with HIDDEN.open("a") as fh:
            fh.write(json.dumps(e, ensure_ascii=False) + "\n")
    print(f"removed: {t[1]} ({scope})")


def listing():
    """Every exception in force as rows for rormpc's Exceptions list: the current file (None and `gone` for a
    pinned song whose file went away: it is skipped, never dropped from the log); `scope_name` names a smart
    list's scope ("80s party") or a named set's ("Tag God")."""
    from . import hits_sets, smartlists
    names = {f"list:{i}": lst["name"] for i, lst in smartlists.fold().items()}
    found = active()
    named = {e["scope"][4:] for e in found if e["scope"].startswith("set:") and ":" in e["scope"][4:]}
    names |= {f"set:{k}": v for k, v in hits_sets.labels(named).items()}
    rows = []
    for e in found:
        song = survivor(e["song"]) if e.get("song") else None
        file = live_path(song) if song else None
        rows.append({"id": e["id"], "action": e["action"], "scope": e["scope"], "song": song,
                     "chart_key": e.get("chart_key"), "artist": e.get("artist"), "title": e.get("title"),
                     "file": file, "gone": bool(song) and file is None, "via": e.get("via"), "ts": e.get("ts"),
                     "scope_name": names.get(e["scope"])})
    return sorted(rows, key=lambda r: (r["scope"] != "library", r["scope"], r["action"],
                                       (r["artist"] or "").lower(), (r["title"] or "").lower()))


def list_cmd(argv):
    """hits exceptions [--json]"""
    ap = argparse.ArgumentParser(prog="hits exceptions")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    rows = listing()
    if a.json:
        print(json.dumps({"version": 1, "exceptions": rows}, ensure_ascii=False))
        return
    for r in rows:
        mark = "✚" if r["action"] == "pin" else "⊘"
        what = f"{r['artist'] or '?'} - {r['title'] or '?'}" if r["artist"] or r["title"] else (r["file"] or r["song"] or r["chart_key"])
        note = " (file gone)" if r["gone"] else " (hits hide)" if r["via"] == "hide" else ""
        print(f"{mark} {r['scope']:<16} {what}{note}")
