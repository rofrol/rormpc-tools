"""Smart lists (rormpc's plans/combined-view.md, phase 4): hits' rules saved under a name.

  hits lists [--json]                              # every smart list with its rules and when it was exported
  hits lists create "80s party" --set +billboard --years 1985-1992 --top 1-10 -g "+rock"   # hits' filter options
  hits lists update "80s party" --set +billboard --years 1980-1989 --top 1-10    # replace its rules
  hits lists rename "80s party" "80s"               # by name or id
  hits lists duplicate "80s" "80s, no Christmas"     # a copy with its rules and its own exceptions
  hits lists delete "80s"                           # the list, its exceptions (scope list:ID) and its playlist
  hits lists export ["80s" ...]                     # write MPD playlists "Smart NAME" (all lists by default)
  hits --list "80s"                                 # run a list's rules; its exceptions apply

The log is `<data_dir>/smartlists.jsonl`, one event per line: {id (the list's UUID), event create|update|rename|
delete, name, schema, rules, ts}. Folding takes the file order (the later line wins), so a git merge of two
machines' appends keeps both. The rules are semantic fields, not argv: {schema, sets {key: ±1}, rank, years_of,
period, top, genres ["+rock", "-country"], artists ["+Queen"], owned}. A list whose rules or events this version
cannot read is blocked ("made by a newer rormpc-tools, update it"): it never runs with a field dropped, and its
last export stays as it was.

Exceptions scoped to a list (`hits except … --scope list:ID`) apply only while it is open. The export writes each
list as the MPD playlist "Smart NAME" (`musicdb update`, hourly, and rormpc's Apply of a list): a snapshot of the
owned songs, never read back as rules; hits' "my playlists" set leaves "Smart " playlists out.
"""
import argparse, contextlib, datetime as dt, io, json, re, sys, uuid

from . import hits_exceptions, hits_rules, musicdb

PREFIX = "Smart "
NEWER = "made by a newer rormpc-tools, update it"
FIELDS = ("schema", "sets", "rank", "years_of", "period", "top", "genres", "artists", "owned")
DECADE = re.compile(r"^(\d{2}|\d{4})s$|^all$")
YEARS = re.compile(r"^\d{4}(-\d{4})?(,\d{4}(-\d{4})?)*$")


def log_path():
    return musicdb.DATA / "smartlists.jsonl"


def playlist_path(name):
    return musicdb.PLAYLISTS / f"{PREFIX}{name}.m3u"


# ---------------------------------------------------------------- rules

def rules_of(a):
    """hits' parsed filter options -> the rules a smart list stores (ValueError when they cannot be stored)."""
    r = hits_rules.resolve(a.source, a.set, a.rank, a.years_of, a.sort)
    if r.order:
        raise ValueError("--rank listens cannot be saved in a smart list (Rank by billboard, plays or rediscover)")
    top = hits_rules.top_for(r, a.top)
    signed = lambda spec: [("+" if s > 0 else "-") + t for s, t in hits_rules.signed_tokens(spec)]
    rules = {"schema": hits_rules.RULES_SCHEMA, "sets": dict(r.sets), "rank": r.rank, "years_of": r.years_of,
             "period": a.years or a.decade, "top": a.top if top else None, "genres": signed(a.genre),
             "artists": signed(a.artist), "owned": bool(a.owned)}
    err = problem(rules)
    if err:
        raise ValueError(err)
    return rules


def problem(rules):
    """What this version cannot read in a list's rules, or None. Anything unknown blocks the list."""
    if not isinstance(rules, dict):
        return "rules are not an object"
    unknown = sorted(set(rules) - set(FIELDS))
    if unknown:
        return f"unknown rule field {', '.join(unknown)}"
    schema = rules.get("schema")
    if type(schema) is not int or not 1 <= schema <= hits_rules.RULES_SCHEMA:
        return f"rules schema {schema!r}"
    sets = rules.get("sets", {})
    if not isinstance(sets, dict):
        return "sets are not an object"
    for key, sign in sets.items():
        try:
            parsed = hits_rules.parse_set(f"+{key}")[0]
        except ValueError:
            parsed = None
        if parsed != key:
            return f"set {key!r}"
        if sign not in (1, -1):
            return f"set {key!r}: {sign!r}"
    if rules.get("rank") not in hits_rules.RANKS:
        return f"rank {rules.get('rank')!r}"
    if rules.get("years_of") not in hits_rules.YEARS_OF:
        return f"years of {rules.get('years_of')!r}"
    period = rules.get("period")
    if period is not None and not (isinstance(period, str) and (DECADE.match(period) or YEARS.match(period))):
        return f"period {period!r}"
    if period is None and rules["rank"] == "billboard" and rules["years_of"] == "chart":
        return "Billboard chart years need a period"
    top = rules.get("top")
    try:
        if top is not None and (not isinstance(top, str) or not hits_rules.parse_top(top)):
            raise ValueError
        hits_rules.top_for(argparse.Namespace(rank=rules["rank"]), top)
    except ValueError:
        return f"top {top!r}"
    for field in ("genres", "artists"):
        items = rules.get(field, [])
        if not isinstance(items, list) or not all(isinstance(t, str) and t[:1] in "+-" and t[1:].strip() for t in items):
            return f"{field} {items!r}"
    if type(rules.get("owned", False)) is not bool:
        return f"owned {rules.get('owned')!r}"
    return None


def to_options(rules, a):
    """A list's rules onto hits' parsed options (the namespace `hits.show` reads); every row, no -n cut."""
    a.set = [("+" if sign > 0 else "-") + key for key, sign in rules.get("sets", {}).items()]
    a.rank, a.years_of, a.source = rules["rank"], rules["years_of"], None
    period = rules.get("period")
    a.decade, a.years = (period, None) if period and DECADE.match(period) else (None, period)
    a.top = rules.get("top")
    a.genre = ", ".join(rules.get("genres", []))
    a.artist = "; ".join(rules.get("artists", []))
    a.owned = rules.get("owned", False)
    a.n = 0


def as_args(lst):
    """The rules as the `args` of a hits result file: what rormpc's Play loads into its filter column. No Top %
    is "1-100" there (absent means the pane's default)."""
    rules = lst["rules"]
    return {"period": rules.get("period"), "top": rules.get("top") or "1-100",
            "genre": ", ".join(rules.get("genres", [])), "artist": "; ".join(rules.get("artists", [])),
            "owned": rules.get("owned", False), "rank": rules["rank"], "years_of": rules["years_of"],
            "sets": [("+" if v > 0 else "-") + k for k, v in rules.get("sets", {}).items()],
            "show_excluded": False, "source": None, "open_list": lst["id"], "open_list_name": lst["name"]}


def formula(rules):
    """The list's selection in one line, as hits prints it."""
    r = argparse.Namespace(sets=rules.get("sets", {}))
    return hits_rules.formula(r, period=rules.get("period"), top=hits_rules.parse_top(rules.get("top")),
                              genre=", ".join(rules.get("genres", [])), artist="; ".join(rules.get("artists", [])),
                              owned=rules.get("owned", False))


# ---------------------------------------------------------------- the log

def events():
    return musicdb.jsonl(log_path())


def fold(evts=None):
    """The smart lists in force: {id: {id, name, rules, created, updated, blocked}}. An event this version cannot
    read (another kind, a newer schema) blocks its list instead of being skipped."""
    lists = {}
    for e in events() if evts is None else evts:
        lid, kind = e.get("id"), e.get("event")
        if kind == "create":
            lists[lid] = {"id": lid, "name": e.get("name"), "rules": e.get("rules"), "created": e.get("ts"),
                          "updated": e.get("ts"), "unreadable": None}
            lst = lists[lid]
        elif lid in lists:
            lst = lists[lid]
            lst["updated"] = e.get("ts")
            if kind == "update":
                lst["rules"] = e.get("rules")
            elif kind == "rename":
                lst["name"] = e.get("name")
            elif kind == "delete":
                lists.pop(lid)
                continue
            else:
                lst["unreadable"] = f"event {kind!r}"
        else:
            continue  # an event of a list deleted (or never created) here
        if type(e.get("schema", 1)) is not int or e.get("schema", 1) > hits_rules.RULES_SCHEMA:
            lst["unreadable"] = f"event schema {e.get('schema')!r}"
    for lst in lists.values():
        why = lst.pop("unreadable") or problem(lst["rules"])
        lst["blocked"] = f"{NEWER} ({why})" if why else None
    return lists


def find(ref, lists=None):
    """A smart list by id, then by name (case ignored); LookupError when there is none."""
    lists = fold() if lists is None else lists
    ref = (ref or "").strip()
    if ref in lists:
        return lists[ref]
    named = [lst for lst in lists.values() if (lst["name"] or "").lower() == ref.lower()]
    if named:
        return named[0]
    raise LookupError(f"no smart list {ref!r} (hits lists shows them)")


def norm_name(name, lists, own_id=None):
    """A list name: trimmed, one line, usable as a file name, not taken by another list (case ignored)."""
    name = re.sub(r"\s+", " ", (name or "").strip())
    if not name or "/" in name or name.startswith("."):
        raise ValueError(f"name {name!r}: give a name without '/' that does not start with '.'")
    taken = [lst for lst in lists.values() if (lst["name"] or "").lower() == name.lower() and lst["id"] != own_id]
    if taken:
        raise ValueError(f"a smart list {taken[0]['name']!r} exists (update it, or pick another name)")
    return name


def record(event, list_id, **fields):
    """Append one event (append-only: the history stays reviewable in git)."""
    e = {"id": list_id, "event": event, "ts": dt.datetime.now().isoformat(timespec="seconds"), **fields}
    path = log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        fh.write(json.dumps(e, ensure_ascii=False) + "\n")
    return e


def own_exceptions(list_id):
    """The exceptions scoped to the list, as folded from the exceptions log."""
    return [e for e in hits_exceptions.fold().values() if e["scope"] == f"list:{list_id}"]


def create(name, rules, exceptions=()):
    lists = fold()
    name = norm_name(name, lists)
    lid = str(uuid.uuid4())
    record("create", lid, name=name, schema=hits_rules.RULES_SCHEMA, rules=rules)
    for e in exceptions:  # a duplicate takes the original's fixes along, scoped to itself
        hits_exceptions.record(e["action"], f"list:{lid}", song=e.get("song"), chart_key=e.get("chart_key"),
                               artist=e.get("artist"), title=e.get("title"), file=e.get("file"))
    return fold()[lid]


def update(ref, rules):
    lst = find(ref)
    record("update", lst["id"], schema=hits_rules.RULES_SCHEMA, rules=rules)
    return fold()[lst["id"]]


def rename(ref, new):
    lists = fold()
    lst = find(ref, lists)
    name = norm_name(new, lists, own_id=lst["id"])
    record("rename", lst["id"], name=name)
    old = playlist_path(lst["name"])
    if old.exists() and name != lst["name"]:
        old.replace(playlist_path(name))  # the export follows the name at once; its time stays
    return fold()[lst["id"]]


def delete(ref):
    """The list, every exception scoped to it, and its exported playlist."""
    lst = find(ref)
    gone = own_exceptions(lst["id"])
    for e in gone:
        hits_exceptions.record("remove", e["scope"], song=e.get("song"), chart_key=e.get("chart_key"))
    record("delete", lst["id"], name=lst["name"])
    playlist_path(lst["name"]).unlink(missing_ok=True)
    return lst, len(gone)


# ---------------------------------------------------------------- export

def owned_files(lst, songs=None):
    """The owned songs a list selects now (its exceptions applied), in rank order, each once. `songs`: the
    library as `hits.library_songs` read it, shared between lists."""
    from . import hits
    a = hits.parser().parse_args([])
    to_options(lst["rules"], a)
    a.open_list, a.songs = lst["id"], songs
    with contextlib.redirect_stdout(io.StringIO()):
        rows = hits.show(a)
    return list(dict.fromkeys(r["file"] for r in rows if r.get("file") and not r.get("excluded"))), a.songs


def export(refs=None, out=print):
    """Write each list (or the given ones) as the MPD playlist "Smart NAME", atomically; playlists of lists that
    no longer exist go (when exporting all). A blocked list keeps its last export. Returns the errors."""
    lists = fold()
    targets = [find(r, lists) for r in refs] if refs else sorted(lists.values(), key=lambda x: (x["name"] or "").lower())
    musicdb.PLAYLISTS.mkdir(parents=True, exist_ok=True)
    if not refs:
        names = {lst["name"] for lst in lists.values()}
        for old in musicdb.PLAYLISTS.glob(f"{PREFIX}*.m3u"):
            if old.stem[len(PREFIX):] not in names:
                old.unlink()
    errors, songs = [], None
    for lst in targets:
        if lst["blocked"]:
            errors.append(f"{lst['name']}: {lst['blocked']}")
            continue
        try:
            files, songs = owned_files(lst, songs)
        except SystemExit as err:  # hits reports a rules error by exiting
            errors.append(f"{lst['name']}: {err}")
            continue
        path = playlist_path(lst["name"])
        tmp = path.with_name(f".{path.name}.tmp")
        tmp.write_text("".join(f + "\n" for f in files))
        tmp.replace(path)
        out(f"{path.stem}: {len(files)} songs")
    for e in errors:
        out(f"not exported: {e}")
    return errors


def exported(lst):
    """(when, songs) of the list's last export, or (None, 0)."""
    path = playlist_path(lst["name"])
    if not path.exists():
        return None, 0
    when = dt.datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")
    return when, sum(1 for line in path.read_text().splitlines() if line.strip())


def listing():
    """Every smart list for rormpc's picker: rules, their `args` for the filter column, formula, own exceptions,
    the export's time and size, and why it is blocked."""
    counts = {}
    for e in hits_exceptions.fold().values():
        if e["scope"].startswith("list:"):
            c = counts.setdefault(e["scope"][5:], {"pins": 0, "exclusions": 0})
            c["pins" if e["action"] == "pin" else "exclusions"] += 1
    rows = []
    for lst in sorted(fold().values(), key=lambda x: (x["name"] or "").lower()):
        when, n = exported(lst)
        rows.append({"id": lst["id"], "name": lst["name"], "rules": lst["rules"], "blocked": lst["blocked"],
                     "args": None if lst["blocked"] else as_args(lst),
                     "formula": None if lst["blocked"] else formula(lst["rules"]),
                     "exceptions": counts.get(lst["id"], {"pins": 0, "exclusions": 0}),
                     "playlist": f"{PREFIX}{lst['name']}", "exported": when, "exported_songs": n,
                     "created": lst["created"], "updated": lst["updated"]})
    return rows


# ---------------------------------------------------------------- CLI

def rules_from_argv(argv, prog):
    """hits' filter options -> rules; output options (--json, --show-excluded, --open-list, -n) are ignored."""
    from . import hits
    a = hits.parser(prog).parse_args(hits.set_argv(argv))
    if a.list or a.rules_file:
        raise ValueError("give the filter options themselves, not --list or --rules")
    return rules_of(a)


def main(argv):
    """hits lists [--json] | create NAME [hits options] | update REF [hits options] | rename REF NEW |
    duplicate REF NEW | delete REF | export [REF ...]"""
    cmds = ("create", "update", "rename", "duplicate", "delete", "export")
    if not argv or argv[0] not in cmds:
        ap = argparse.ArgumentParser(prog="hits lists", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
        ap.add_argument("--json", action="store_true")
        a = ap.parse_args(argv)
        rows = listing()
        if a.json:
            print(json.dumps({"version": 1, "lists": rows}, ensure_ascii=False))
            return
        for r in rows:
            fixes = r["exceptions"]
            extra = (f" · +{fixes['pins']} pins" if fixes["pins"] else "") + (
                f" · -{fixes['exclusions']}" if fixes["exclusions"] else "")
            state = r["blocked"] or f"{r['formula']}{extra}" + (
                f" · exported {r['exported'][:16]} ({r['exported_songs']})" if r["exported"] else "")
            print(f"{r['name']}  [{r['id'][:8]}]  {state}")
        return
    cmd, rest = argv[0], argv[1:]
    try:
        if cmd in ("create", "update"):
            if not rest:
                raise ValueError(f"hits lists {cmd} {'NAME' if cmd == 'create' else 'ID|NAME'} [hits options]")
            rules = rules_from_argv(rest[1:], f"hits lists {cmd} {rest[0]}")
            lst = create(rest[0], rules) if cmd == "create" else update(rest[0], rules)
            print(f"{cmd}d {lst['name']} [{lst['id']}]: {formula(lst['rules'])}")
        elif cmd in ("rename", "duplicate"):
            if len(rest) != 2:
                raise ValueError(f"hits lists {cmd} ID|NAME NEW_NAME")
            if cmd == "rename":
                lst = rename(*rest)
                print(f"renamed to {lst['name']} [{lst['id']}]")
            else:
                src = find(rest[0])
                if src["blocked"]:
                    raise ValueError(f"{src['name']}: {src['blocked']}")
                lst = create(rest[1], src["rules"], own_exceptions(src["id"]))
                print(f"duplicated {src['name']} as {lst['name']} [{lst['id']}]")
        elif cmd == "delete":
            if len(rest) != 1:
                raise ValueError("hits lists delete ID|NAME")
            lst, n = delete(rest[0])
            print(f"deleted {lst['name']}" + (f" and its {n} exception{'s' if n != 1 else ''}" if n else ""))
        else:
            errors = export(rest or None)
            if errors:
                sys.exit(f"hits lists export: {len(errors)} not exported")
    except (ValueError, LookupError) as err:
        sys.exit(f"hits lists: {err}")
