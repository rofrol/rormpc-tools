"""Up next: songs asked for with "Play next" play before the rest of the queue (the source), with rormpc open or
closed. The whole queue stays in MPD.

The waiting songs get MPD priorities 255, 254, ... in order; with random off they are also moved right after the
current song, in order (MPD ignores priorities there; after the user jumps to a later song they are moved again). A
song that was not in the queue is added for Up next and deleted again after it played, so the source stays as it
was; a song from the source keeps its place. Consume must be off (it would delete the source as it plays).

Commands (channel "rormpc"; FILE is a path in the music directory, ID an MPD song id):
  upnext add FILE        append; a song already waiting moves to the top
  upnext playnow FILE    play it now: its queue entry, else added right after the current song
  upnext play ID         play a waiting entry now
  upnext first ID        make it the next one
  upnext move ID DELTA   move it DELTA places (negative: earlier)
  upnext remove ID       drop it (an added song leaves the queue, a source song loses its priority)
  upnext clear           drop them all
upnext.json: {"entries": [{"id", "file", "added"}], "playing": {...} or null, "error": str or null, "marked": true};
entries are the waiting ones in play order, "playing" the entry that is playing now.

When an entry has started, from MPD's state alone: MPD resets a song's priority to 0 whenever it starts (next,
previous, play, a song ending; random on or off), and nothing else here gives a waiting entry priority 0. So a
waiting entry whose id still holds its file and whose priority is 0 has started, even when this never saw it play:
- several `mpc next` between two wakes skip past it (random on or off; with random off the entries then sit before
  the current song, as they do after the user jumped to a later song, but a jump starts only the song jumped to, so
  the skipped entries keep their priority and stay waiting);
- it played while mpd-player was down (checked at startup, before any priority is written).
This holds because every waiting entry carries its priority in MPD before it is saved ("marked": state from older
versions, which gave no priority with random off, is not judged once) and the entry playing now is never written
again (apply_order skips it and puts back a 0 it may have overwritten). An entry re-found by file after an MPD
restart or a replaced queue (new id) is not judged: MPD's state file brings priorities back, but a replaced queue
starts at 0 as well, so it stays waiting.
"""
from . import Module, log, queue_entry, read_state, write_state

MAX_PRIO = 255


class UpNext(Module):
    name = "upnext"

    def __init__(self):
        saved = read_state("upnext") or {}
        self.entries = [e for e in saved.get("entries", []) if {"id", "file", "added"} <= e.keys()]
        self.playing = saved.get("playing")
        self.marked = bool(saved.get("marked"))  # the saved entries carry their priority in MPD
        self.error = None
        self.current = None  # songid seen playing last time
        self.random = None

    def save(self):
        write_state("upnext", {"entries": self.entries, "playing": self.playing, "error": self.error,
                               "marked": True})

    async def start(self, d):
        d.status = await d.mpd.status()  # the daemon starts its modules before its first status read
        if self.marked:
            # entries that started while this was down; the one playing now is taken by on_status
            await self.started_unseen(d, int(d.status["songid"]) if "songid" in d.status else None)
        await self.resync(d)
        await self.apply_order(d)
        self.marked = True
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

    async def resync(self, d):
        """sync_ids, and new ids get their priority at once (a waiting entry at 0 would count as started)."""
        if await self.sync_ids(d):
            await self.apply_order(d)
            return True
        return False

    async def apply_order(self, d):
        """Priorities in order, and with random off places after the current song. MPD plays on while this writes:
        the song playing now is left alone (MPD reset its priority when it started), and when another one started
        after its write, its 0 is put back."""
        live = await d.mpd.status()
        playing = live.get("songid")
        written = set()
        k = 0
        for e in self.entries:
            if str(e["id"]) == playing:
                continue
            await d.mpd.prioid(max(1, MAX_PRIO - k), e["id"])
            written.add(str(e["id"]))
            if live.get("random") != "1" and live.get("song") is not None:
                await d.mpd.moveid(e["id"], f"+{k}")
            k += 1
            now = (await d.mpd.status()).get("songid")
            if now != playing:
                playing = now
                if now in written:
                    await d.mpd.prioid(0, int(now))

    async def finished(self, d, entry):
        """An entry stopped playing: one added only for Up next leaves the queue (if it is still that file)."""
        if entry and entry.get("added"):
            e = await queue_entry(d.mpd, entry["id"])
            if e and e.get("file") == entry["file"]:
                await d.mpd.deleteid(entry["id"])

    async def on_status(self, d, s, changed):
        dirty = False
        if "playlist" in changed and await self.resync(d):  # new ids carry no priority or place yet
            dirty = True
        song = int(s["songid"]) if "songid" in s else None
        moved = song != self.current
        if moved:
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
        if "player" in changed and await self.started_unseen(d, song):
            dirty = True
        random = s.get("random") == "1"
        if random != self.random or (moved and not random):
            self.random = random
            if self.entries:
                await self.apply_order(d)  # with random off: after the current song again (after a jump)
        if s.get("state") == "stop" and self.playing:
            await self.finished(d, self.playing)
            self.playing = None
            dirty = True
        if dirty:
            self.save()

    async def started_unseen(self, d, song):
        """Entries that started and were skipped past between two wakes (`mpc next` three times in a row) or while
        this was down: a waiting entry whose MPD priority went back to 0 has played (see the module docstring)."""
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
        await self.resync(d)  # the queue may have changed since the last wake (ids in the command are current)
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
                await self.switch_to(d, waiting)
            elif queued:
                await self.switch_to(d, None, int(queued[0]["id"]))
            else:
                id_ = await self.add_to_queue(d, args, True)
                entry = {"id": id_, "file": args, "added": True}
                self.entries.append(entry)
                # Establish a waiting request before starting it; a failed play must not leave priority 0,
                # which a later random-on wake would mistake for a request already played.
                await self.apply_order(d)
                await self.switch_to(d, entry)
        elif verb == "play":
            e = self.find(args)
            if not e:
                return
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
        target = entry["id"] if entry else id_
        try:
            await d.mpd.playid(target)
        except Exception as exc:
            # A failed start is not a completed request: waiting and previous playback remain unchanged.
            self.error = f"Cannot play {entry['file'] if entry else target}: {exc}"
            self.save()
            raise
        prev = self.playing
        if entry and entry in self.entries:
            self.entries.remove(entry)
        self.playing = entry
        self.current = target  # on_status must not take this successful start for a song change
        if prev and prev["id"] != self.current:
            await self.finished(d, prev)

    async def drop(self, d, e):
        found = await queue_entry(d.mpd, e["id"])
        if not found or found.get("file") != e["file"]:
            return
        if e["added"]:
            await d.mpd.deleteid(e["id"])
        else:
            await d.mpd.prioid(0, e["id"])
        log(f"upnext: dropped {e['file']}")
