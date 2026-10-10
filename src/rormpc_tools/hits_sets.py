"""Named sets of `hits` (rormpc's plans/combined-view.md, phase 5): the sets picked through rormpc's "+ set…".

  hits --set +tag:God                  # a tag list (musicdb tag, collections.jsonl); its name, case ignored
  hits --set "-playlist:Road trip"     # a stored MPD playlist (the generated ones are not offered)
  hits --set +live:yt-PL123            # a followed Live playlist (liveplaylist): its accepted, ready songs
  hits --set "+list:80s party"         # a smart list (hits lists): what it selects, evaluated with its own
                                       # exceptions; a list that leads back to itself is refused (a cycle)
  hits sets [--json]                   # every named set with its size, for the picker

Keys: `tag:NAME`, `playlist:NAME`, `live:ID`, `list:ID`. A name may hold spaces, colons and commas (one named set
per --set value: commas there are part of the name). Canonical keys are stored in smart lists and exception
scopes: a tag keeps its first spelling, a Live playlist and a smart list are kept by id (given by name, the id is
looked up), so a rename keeps them. Exceptions scoped to a named set are `set:tag:NAME`, `set:playlist:NAME`,
`set:live:ID` and `set:list:ID` (only while that set is +).

A missing tag list, playlist, Live playlist or smart list is an error naming it, never an empty set.
"""
import argparse, contextlib, io, json, re

from . import hits_rules, musicdb, tags

class SetError(ValueError):
    """A named set that cannot be read: missing, blocked, or a smart list cycle."""


def norm(name):
    return re.sub(r"\s+", " ", (name or "").strip())


# ---------------------------------------------------------------- the sources

def tag_lists():
    return tags.lists(musicdb.DATA / "collections.jsonl")


def stored_playlists():
    """{name: [file, ...]} of the stored MPD playlists a user made: the generated ones ("Hits …", "Smart …", ...),
    the tag lists' "Tag …" and the Live playlists' own playlists are offered in their own sections or not at
    all."""
    from .hits import GENERATED_PLAYLISTS
    c = musicdb.mpd()
    live = {s.get("playlist") for s in live_subs().values()}
    out = {}
    for p in sorted(c.listplaylists(), key=lambda p: p["playlist"].lower()):
        name = p["playlist"]
        if name.startswith(GENERATED_PLAYLISTS) or name.startswith("Tag ") or name in live:
            continue
        out[name] = None  # read on demand: listing hundreds of playlists only for their sizes is slow
    return out, c


def live_subs():
    """{subscription id: state} of the followed Live playlists (liveplaylist's <data_dir>/liveplaylists)."""
    d = musicdb.DATA / "liveplaylists"
    out = {}
    for p in sorted(d.glob("yt-*.json")) if d.is_dir() else ():
        try:
            out[p.stem] = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
    return out


def live_files(sub):
    """The songs the Live playlist plays: accepted, ready, still listed items with a file, in its order."""
    items = sorted(sub.get("items", {}).values(), key=lambda it: (it.get("position") or 0))
    return [it["path"] for it in items if it.get("active") and it.get("decision") == "accepted"
            and it.get("job") == "ready" and it.get("path")]


def live_name(sub):
    return sub.get("playlist") or sub.get("title") or sub.get("id")


# ---------------------------------------------------------------- canonical keys

def canonical(key):
    """A parsed named-set key -> its stored form: a tag in its first spelling, a Live playlist and a smart list
    by id (a name is looked up). A fixed set or an unknown name stays as it is (reading it reports the error)."""
    kind, colon, name = key.partition(":")
    if not colon:
        return key
    name = norm(name)
    if kind == "tag":
        return f"tag:{tags.norm_name(name, tag_lists())}"
    if kind == "live":
        subs = live_subs()
        if name not in subs:
            found = [sid for sid, s in subs.items() if name.lower() in
                     {(s.get("playlist") or "").lower(), (s.get("title") or "").lower()}]
            name = found[0] if found else name
        return f"live:{name}"
    if kind == "list":
        from . import smartlists
        try:
            return f"list:{smartlists.find(name)['id']}"
        except LookupError:
            return f"list:{name}"
    return f"{kind}:{name}"


def canonical_sets(sets):
    """{key: ±1} with every named key in its stored form (a later duplicate wins, as on the command line)."""
    out = {}
    for k, v in sets.items():
        out[canonical(k)] = v
    return out


def labels(sets):
    """{key: name in the formula}: "Tag God", "Playlist Road trip", "Live <its playlist>", "Smart 80s party"; a
    fixed set by its fixed name. A Live playlist or smart list that is gone keeps its id."""
    out, lists, subs = {}, None, None
    for key in sets:
        kind, colon, name = key.partition(":")
        if not colon:
            out[key] = hits_rules.SET_KINDS.get(key, key)
            continue
        if kind == "list":
            if lists is None:
                from . import smartlists
                lists = smartlists.fold()
            name = (lists.get(name) or {}).get("name") or name
        elif kind == "live":
            subs = live_subs() if subs is None else subs
            name = live_name(subs[name]) if name in subs else name
        out[key] = f"{hits_rules.NAMED_LABELS[kind]} {name}"
    return out


# ---------------------------------------------------------------- members

def members(key, cands, a):
    """The candidate keys of one named set (a subset of `cands`); SetError when it cannot be read. `a`: hits'
    options of the run (its library songs, and the smart lists being evaluated, for the cycle check)."""
    kind, _, name = key.partition(":")
    if kind == "tag":
        return tag_members(name, cands)
    if kind == "playlist":
        return playlist_members(name, cands)
    if kind == "live":
        return live_members(name, cands)
    if kind == "list":
        return list_members(name, cands, a)
    raise SetError(f"unknown set {key!r}")


def tag_members(name, cands):
    state = tag_lists()
    songs = state.get(tags.norm_name(name, state))
    if not songs:
        raise SetError(f"no tag list {name!r} (musicdb tag list shows them)")
    keys = set(songs)
    al = musicdb.aliases()
    files = {musicdb.canon(e["song"].get("file") or "", al) for e in songs.values()}
    return {k for k, c in cands.items() if c.get("file") and (
        c["file"] in files or tags.key_of_file(c["file"], c.get("mbid")) in keys)}


def playlist_members(name, cands):
    c = musicdb.mpd()
    if name not in {p["playlist"] for p in c.listplaylists()}:
        raise SetError(f"no MPD playlist {name!r}")
    al = musicdb.aliases()
    return {f for f in (musicdb.canon(x, al) for x in c.listplaylist(name)) if f in cands}


def live_members(sid, cands):
    subs = live_subs()
    if sid not in subs:
        raise SetError(f"no Live playlist {sid!r} (liveplaylist list shows them)")
    al = musicdb.aliases()
    return {f for f in (musicdb.canon(x, al) for x in live_files(subs[sid])) if f in cands}


def list_members(ref, cands, a):
    """What the smart list selects (its own exceptions applied, excluded rows left out), evaluated as `hits --list`
    does, with the lists already being evaluated on a stack: a list that leads back to one of them is a cycle."""
    from . import hits, smartlists
    lists = smartlists.fold()
    try:
        lst = smartlists.find(ref, lists)
    except LookupError:
        raise SetError(f"no smart list {ref!r} (hits lists shows them)") from None
    stack = list(getattr(a, "list_stack", None) or [])
    if lst["id"] in stack:
        names = [(lists.get(i) or {}).get("name") or i for i in stack + [lst["id"]]]
        raise SetError("smart list cycle: " + " → ".join(names))
    if lst["blocked"]:
        raise SetError(f"smart list {lst['name']!r}: {lst['blocked']}")
    inner = hits.parser().parse_args([])
    smartlists.to_options(lst["rules"], inner)
    inner.open_list, inner.songs, inner.nested = lst["id"], a.songs, True
    inner.list_stack = stack + [lst["id"]]
    with contextlib.redirect_stdout(io.StringIO()):
        try:
            rows = hits.show(inner)
        except SetError as err:
            raise SetError(f"in smart list {lst['name']!r}: {err}") from None
    return {r["key"] for r in rows if not r.get("excluded") and r["key"] in cands}


def cycle(list_id, rules, lists):
    """The smart list cycle a list's rules would make ("A → B → A"), or None: follows its +/- list sets through
    the stored lists without running them."""
    def walk(lid, sets, path):
        for key in sets:
            kind, _, ref = key.partition(":")
            if kind != "list":
                continue
            if ref in path:
                return path[path.index(ref):] + [ref]
            nxt = lists.get(ref)
            if nxt and isinstance(nxt.get("rules"), dict):
                found = walk(ref, nxt["rules"].get("sets") or {}, path + [ref])
                if found:
                    return found
        return None
    found = walk(list_id, (rules or {}).get("sets") or {}, [list_id])
    if not found:
        return None
    return "smart list cycle: " + " → ".join((lists.get(i) or {}).get("name") or i for i in found)


# ---------------------------------------------------------------- the picker

def listing():
    """Every named set for rormpc's "+ set…" picker, per kind: {key, kind, name, songs (None: not counted), error}."""
    from . import smartlists
    out = []
    for name, songs in sorted(tag_lists().items(), key=lambda x: x[0].lower()):
        out.append({"key": f"tag:{name}", "kind": "tag", "name": name, "songs": len(songs), "error": None})
    try:
        pls, c = stored_playlists()
        for name in pls:
            out.append({"key": f"playlist:{name}", "kind": "playlist", "name": name,
                        "songs": len(c.listplaylist(name)), "error": None})
    except (OSError, ConnectionError) as err:
        out.append({"key": "playlist:", "kind": "playlist", "name": "", "songs": None, "error": f"MPD: {err}"})
    for sid, sub in sorted(live_subs().items(), key=lambda x: live_name(x[1]).lower()):
        out.append({"key": f"live:{sid}", "kind": "live", "name": live_name(sub), "songs": len(live_files(sub)),
                    "error": None})
    lists = smartlists.fold()
    for lst in sorted(lists.values(), key=lambda x: (x["name"] or "").lower()):
        _, n = smartlists.exported(lst)
        out.append({"key": f"list:{lst['id']}", "kind": "list", "name": lst["name"], "songs": n if n else None,
                    "error": lst["blocked"] or cycle(lst["id"], lst["rules"], lists)})
    return [r for r in out if r["name"] or r["error"]]


def main(argv):
    """hits sets [--json]"""
    ap = argparse.ArgumentParser(prog="hits sets", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    rows = listing()
    if a.json:
        print(json.dumps({"version": 1, "sets": rows}, ensure_ascii=False))
        return
    for r in rows:
        size = "" if r["songs"] is None else f" ({r['songs']})"
        print(f"{r['kind']:<9}{r['name']}{size}" + (f"  ! {r['error']}" if r["error"] else ""))

