"""Weighted shuffle: with MPD's random on, the next song is drawn here instead of uniformly, and nominated with MPD
priority 1, below the Up next requests (2-255), so a request always plays first.

The weight comes from `musicdb sync` (weights.json, hourly): 1 for a song never played, up to 3 for songs played a
lot lately (log-compressed, a play counts half after 60 days) or liked; a dislike makes it rare. One pick in five
ignores the weights (exploration), so songs that were never played still come up. Excluded from automatic picks:
the songs played lately, and songs in a "heard enough" cooldown (1, 3, 7, then 14 days, growing each time it is
asked again while the last one is recent). Enter and Play next still play them: the cooldown only stops this pick.

When the source is a Hits result (rormpc's source.json kind "hits"), a round plays each song once: no song is
picked again until all were heard (a song played by hand counts). When the round is done this stops nominating
and says so; `shuffle newround` starts the next one.

Commands: `shuffle on|off`, `shuffle reroll`, `shuffle heardenough FILE` (the playing song also skips to the
next), `shuffle unheardenough FILE`, `shuffle newround`.
shuffle.json: {"enabled", "nominee": {"id", "file", "why"} or null, "cooldown": {file: {"until", "level"}},
"recent": [files], "round": {"source", "heard": [files], "total", "done"} or null, "active", "reason"}.
"""
import random, time

from . import Module, queue_entry, read_state, state_dir, write_state

EXPLORE = 0.2
COOLDOWN_DAYS = [1, 3, 7, 14]
# asking "heard enough" again within this many days after the last cooldown ended grows it
COOLDOWN_MEMORY_DAYS = 30
RECENT_MAX = 30
NOMINEE_PRIO = 1


class Shuffle(Module):
    name = "shuffle"

    def __init__(self, rng=None):
        saved = read_state("shuffle") or {}
        self.enabled = saved.get("enabled", True)
        self.nominee = saved.get("nominee")
        self.cooldown = saved.get("cooldown", {})
        self.recent = saved.get("recent", [])
        self.round = saved.get("round")
        self.current = None
        self.active = False
        self.reason = ""
        self.rng = rng or random.Random()
        self._weights = (None, {})

    def save(self):
        write_state("shuffle", {"enabled": self.enabled, "nominee": self.nominee, "cooldown": self.cooldown,
                                "recent": self.recent, "round": self.round, "active": self.active,
                                "reason": self.reason})

    def weights(self):
        """weights.json from musicdb, read again when it changes."""
        p = state_dir() / "weights.json"
        try:
            mtime = p.stat().st_mtime
        except OSError:
            return {}
        if self._weights[0] != mtime:
            files = (read_state("weights") or {}).get("files", {})
            self._weights = (mtime, {f: float(v.get("w", 1)) for f, v in files.items()})
        return self._weights[1]

    def source_key(self):
        src = (read_state("source") or {}).get("source") or {}
        return f"{src.get('kind')}:{src.get('name')}" if src.get("kind") == "hits" else None

    def cooled(self, file, now):
        c = self.cooldown.get(file)
        return bool(c) and c["until"] > now

    async def withdraw(self, d):
        if self.nominee:
            e = await queue_entry(d.mpd, self.nominee["id"])
            if e and e.get("file") == self.nominee["file"] and int(e.get("prio", 0)) == NOMINEE_PRIO:
                await d.mpd.prioid(0, self.nominee["id"])
            self.nominee = None

    async def choose(self, d):
        """Nominate the next song (priority 1), or explain why there is none."""
        q = await d.mpd.playlistinfo()
        current = d.status.get("songid")
        now = time.time()
        self.ensure_round()
        heard = set(self.round["heard"]) if self.round else set()
        recent = set(self.recent[-min(RECENT_MAX, max(1, len(q) // 3)):])
        base = [s for s in q if s.get("id") != current and int(s.get("prio", 0)) == 0
                and not self.cooled(s["file"], now)]
        if self.round:
            pool = [s for s in base if s["file"] not in heard]
            self.round["total"] = len({s["file"] for s in q})
            if not pool:
                self.round["done"] = True
                self.reason = f"round done: all {self.round['total']} heard (shuffle newround starts another)"
                return
        else:
            pool = [s for s in base if s["file"] not in recent] or base
        if not pool:
            self.reason = "nothing to pick (all recent, cooling down or requested)"
            return
        if self.rng.random() < EXPLORE:
            pick, why = self.rng.choice(pool), "exploration (uniform)"
        else:
            w = self.weights()
            ws = [w.get(s["file"], 1.0) for s in pool]
            pick = self.rng.choices(pool, weights=ws)[0]
            why = f"weighted (w {w.get(pick['file'], 1.0):g})"
        await d.mpd.prioid(NOMINEE_PRIO, pick["id"])
        self.nominee = {"id": int(pick["id"]), "file": pick["file"], "why": why}
        self.reason = ""

    def is_active(self, s):
        if not self.enabled:
            return False, "off"
        if s.get("random") != "1":
            return False, "random is off"
        if s.get("consume", "0") != "0":
            return False, "consume is on"
        if s.get("single") == "1":
            return False, "single is on"
        if s.get("state") not in ("play", "pause"):
            return False, "stopped"
        return True, ""

    def ensure_round(self):
        """A Hits source has rounds; a new source starts a new one."""
        key = self.source_key()
        if not key:
            self.round = None
        elif not self.round or self.round.get("source") != key:
            self.round = {"source": key, "heard": [], "total": 0, "done": False}

    def heard(self, file):
        self.recent = [f for f in self.recent if f != file][-(RECENT_MAX - 1):] + [file]
        if self.round and file not in self.round["heard"]:
            self.round["heard"].append(file)

    async def on_status(self, d, s, changed):
        dirty = False
        self.ensure_round()
        song = s.get("songid")
        if song != self.current:
            self.current = song
            if song is not None:
                cur = await d.mpd.currentsong()
                if cur.get("file"):
                    self.heard(cur["file"])
                    dirty = True
            if self.nominee and str(self.nominee["id"]) == song:
                self.nominee = None  # it is playing (MPD reset its priority)
                dirty = True
        if self.nominee:
            # gone from the queue, or asked for with Play next (Up next priority): no longer our pick
            e = await queue_entry(d.mpd, self.nominee["id"])
            if not e or e.get("file") != self.nominee["file"] or int(e.get("prio", 0)) != NOMINEE_PRIO:
                self.nominee = None
                dirty = True
        active, why = self.is_active(s)
        if active != self.active or (not active and why != self.reason):
            self.active, self.reason = active, why
            dirty = True
        if not active:
            if self.nominee:
                await self.withdraw(d)
                dirty = True
        elif not self.nominee and not (self.round and self.round.get("done")):
            await self.choose(d)
            dirty = True
        if dirty:
            self.save()

    async def on_message(self, d, verb, args):
        if verb in ("on", "off"):
            self.enabled = verb == "on"
            if not self.enabled:
                await self.withdraw(d)
        elif verb == "reroll":
            await self.withdraw(d)
        elif verb == "heardenough":
            now = time.time()
            c = self.cooldown.get(args)
            recent_cooldown = c and c["until"] + COOLDOWN_MEMORY_DAYS * 86400 > now
            level = min(len(COOLDOWN_DAYS) - 1, c["level"] + 1) if recent_cooldown else 0
            self.cooldown[args] = {"until": now + COOLDOWN_DAYS[level] * 86400, "level": level}
            if self.nominee and self.nominee["file"] == args:
                await self.withdraw(d)
            cur = await d.mpd.currentsong()
            if cur.get("file") == args:
                await d.mpd.next()
        elif verb == "unheardenough":
            self.cooldown.pop(args, None)
        elif verb == "newround":
            if self.round:
                self.round = {**self.round, "heard": [], "done": False}
            await self.withdraw(d)
        else:
            return await super().on_message(d, verb, args)
        # drop cooldowns long past (they no longer grow the next one)
        now = time.time()
        self.cooldown = {f: c for f, c in self.cooldown.items()
                         if c["until"] + COOLDOWN_MEMORY_DAYS * 86400 > now}
        d.status = await d.mpd.status()
        active, self.reason = self.is_active(d.status)
        self.active = active
        if active and not self.nominee and not (self.round and self.round.get("done")):
            await self.choose(d)
        self.save()
