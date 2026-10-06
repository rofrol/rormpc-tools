"""Up next: songs asked for with "Play next" play before the rest of the queue (the source), with rormpc open or
closed. The whole queue stays in MPD.

With random on the waiting songs get MPD priorities 255, 254, ... in order (MPD resets a song's priority when it
starts); with random off they are moved right after the current song, in order. A song that was not in the queue
is added for Up next and deleted again after it played, so the source stays as it was; a song from the source keeps
its place. Consume must be off (it would delete the source as it plays).

Commands (channel "rormpc"; FILE is a path in the music directory, ID an MPD song id):
  upnext add FILE        append; a song already waiting moves to the top
  upnext playnow FILE    play it now: its queue entry, else added right after the current song
  upnext play ID         play a waiting entry now
  upnext first ID        make it the next one
  upnext move ID DELTA   move it DELTA places (negative: earlier)
  upnext remove ID       drop it (an added song leaves the queue, a source song loses its priority)
  upnext clear           drop them all
upnext.json: {"entries": [{"id", "file", "added"}], "playing": {...} or null, "error": str or null}; entries are
the waiting ones in play order, "playing" the entry that is playing now.
"""
from . import Module, log, queue_entry, read_state, write_state

MAX_PRIO = 255


class UpNext(Module):
    name = "upnext"

    def __init__(self):
        saved = read_state("upnext") or {}
        self.entries = [e for e in saved.get("entries", []) if {"id", "file", "added"} <= e.keys()]
        self.playing = saved.get("playing")
        self.error = None
        self.current = None  # songid seen playing last time
        self.random = None

    def save(self):
        write_state("upnext", {"entries": self.entries, "playing": self.playing, "error": self.error})

    async def start(self, d):
        await self.sync_ids(d)
        self.save()

    async def queue(self, d):
        return await d.mpd.playlistinfo()

    async def sync_ids(self, d):
        """Entries whose id left the queue (an MPD restart, a replaced queue): the same file still queued takes over
        (as a source song); a song added only for Up next is added again (the request stands when the source is
        replaced); a source song that is gone is dropped."""
        q = await self.queue(d)
        ids = {s["id"]: s["file"] for s in q}
        kept = []
        for e in self.entries:
            if ids.get(str(e["id"])) == e["file"]:
                kept.append(e)
                continue
            same = next((s["id"] for s in q if s["file"] == e["file"] and int(s["id"]) not in
                         {k["id"] for k in kept}), None)
            if same is not None:
                kept.append({"id": int(same), "file": e["file"], "added": False})
            elif e["added"]:
                kept.append({"id": int(await d.mpd.addid(e["file"])), "file": e["file"], "added": True})
        changed = kept != self.entries
        self.entries = kept
        if self.playing and ids.get(str(self.playing["id"])) != self.playing["file"]:
            self.playing = None
            changed = True
        return changed

    async def apply_order(self, d):
        random = d.status.get("random") == "1"
        current = d.status.get("song")
        for k, e in enumerate(self.entries):
            if random:
                await d.mpd.prioid(max(1, MAX_PRIO - k), e["id"])
            elif current is not None:
                await d.mpd.moveid(e["id"], f"+{k}")

    async def finished(self, d, entry):
        """An entry stopped playing: one added only for Up next leaves the queue (if it is still that file)."""
        if entry and entry.get("added"):
            e = await queue_entry(d.mpd, entry["id"])
            if e and e.get("file") == entry["file"]:
                await d.mpd.deleteid(entry["id"])

    async def on_status(self, d, s, changed):
        dirty = False
        if "playlist" in changed and await self.sync_ids(d):
            dirty = True
            await self.apply_order(d)  # new ids carry no priority or place yet
        song = int(s["songid"]) if "songid" in s else None
        if song != self.current:
            # the previous song stopped: if it was the Up next entry playing, it has played
            if self.playing and self.playing["id"] != song:
                await self.finished(d, self.playing)
                self.playing = None
                dirty = True
            # the new one is waiting in Up next: it is playing now
            hit = next((e for e in self.entries if e["id"] == song), None)
            if hit:
                self.entries.remove(hit)
                self.playing = hit
                dirty = True
            self.current = song
        random = s.get("random") == "1"
        if random and self.random and "player" in changed and await self.started_unseen(d, song):
            dirty = True
        if random != self.random:
            self.random = random
            if self.entries:
                await self.apply_order(d)  # priorities when random is on, positions when off
        if s.get("state") == "stop" and self.playing:
            await self.finished(d, self.playing)
            self.playing = None
            dirty = True
        if dirty:
            self.save()

    async def started_unseen(self, d, song):
        """Entries that started and were skipped past between two wakes (`mpc next` three times in a row): this never
        saw them play, but MPD resets the priority of a song that starts, so with random on (since before this wake:
        turning it on gives the priorities only now) a waiting entry back at 0 has played."""
        gone = []
        for e in self.entries:
            if e["id"] == song:
                continue
            found = await queue_entry(d.mpd, e["id"])
            if found and found.get("file") == e["file"] and int(found.get("prio", 0)) == 0:
                gone.append(e)
        for e in gone:
            self.entries.remove(e)
            await self.finished(d, e)
            log(f"upnext: {e['file']} started and was skipped past")
        return bool(gone)

    def find(self, id_):
        return next((e for e in self.entries if e["id"] == int(id_)), None)

    async def add_to_queue(self, d, file, after_current):
        pos = "+0" if after_current and d.status.get("song") is not None else None
        return int(await d.mpd.addid(file, pos) if pos else await d.mpd.addid(file))

    async def on_message(self, d, verb, args):
        self.error = None
        await self.sync_ids(d)  # the queue may have changed since the last wake (ids in the command are current)
        if verb in ("add", "playnow") and d.status.get("consume", "0") != "0":
            self.error = "Up next needs consume off (consume would delete the source as it plays)"
            self.save()
            return
        if verb == "add":
            e = next((e for e in self.entries if e["file"] == args), None)
            if e:
                self.entries.remove(e)
                self.entries.insert(0, e)  # asked again: to the top
            else:
                q = await self.queue(d)
                current = d.status.get("songid")
                existing = next((s["id"] for s in q if s["file"] == args and s["id"] != current), None)
                if existing is not None:
                    self.entries.append({"id": int(existing), "file": args, "added": False})
                else:
                    self.entries.append({"id": await self.add_to_queue(d, args, False), "file": args, "added": True})
        elif verb == "playnow":
            q = await self.queue(d)
            current = d.status.get("songid")
            queued = [s for s in q if s["file"] == args]
            if any(s["id"] == current for s in queued):
                if d.status.get("state") != "play":
                    await d.mpd.play()
                return
            waiting = next((e for e in self.entries if e["file"] == args), None)
            if waiting:
                self.entries.remove(waiting)
                await self.switch_to(d, waiting)
            elif queued:
                await self.switch_to(d, None, int(queued[0]["id"]))
            else:
                id_ = await self.add_to_queue(d, args, True)
                await self.switch_to(d, {"id": id_, "file": args, "added": True})
        elif verb == "play":
            e = self.find(args)
            if not e:
                return
            self.entries.remove(e)
            await self.switch_to(d, e)
        elif verb == "first":
            e = self.find(args)
            if e:
                self.entries.remove(e)
                self.entries.insert(0, e)
        elif verb == "move":
            id_, delta = args.split()
            e = self.find(id_)
            if e:
                i = self.entries.index(e)
                self.entries.remove(e)
                self.entries.insert(max(0, min(len(self.entries), i + int(delta))), e)
        elif verb == "remove":
            e = self.find(args)
            if e:
                self.entries.remove(e)
                await self.drop(d, e)
        elif verb == "clear":
            gone, self.entries = self.entries, []
            for e in gone:
                await self.drop(d, e)
        else:
            return await super().on_message(d, verb, args)
        await self.apply_order(d)
        self.save()

    async def switch_to(self, d, entry, id_=None):
        """Play `entry` (or the source song `id_`) now; an Up next song that was playing has played."""
        prev = self.playing
        self.playing = entry
        self.current = entry["id"] if entry else id_  # on_status must not take this start for a song change
        await d.mpd.playid(self.current)
        if prev and prev["id"] != self.current:
            await self.finished(d, prev)

    async def drop(self, d, e):
        found = await queue_entry(d.mpd, e["id"])
        if not found or found.get("file") != e["file"]:
            return
        if e["added"]:
            await d.mpd.deleteid(e["id"])
        elif d.status.get("random") == "1":
            await d.mpd.prioid(0, e["id"])
        log(f"upnext: dropped {e['file']}")
