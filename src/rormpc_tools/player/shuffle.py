"""Weighted shuffle, a mode of its own next to MPD's plain random: the next song is chosen here and given MPD priority
1, below the Up next requests (2-255), so a request always plays first. Priorities steer MPD only with random on, so
this mode owns MPD's random: `shuffle on` turns random on, `shuffle off` turns it off (the queue plays in order);
random turned off by any client (a phone, mpc) turns this off too; `shuffle release` turns this off and leaves plain
random on (rormpc's x). Nothing in the queue is moved. Other clients see random on while this runs.

How a song is chosen (designed 2026-10-06 with GPT-6.1 Sol and MiMo; not SuperMemo, whose growing intervals would
make favourites disappear): every 10 picks are a shuffled cycle of lanes, 7 familiar, 2 rediscovery, 1 new.
- familiar: songs heard in the last year, sampled by (1 + plays)^0.5, x3 for a like, x the skip factor, x how
  overdue the song is for its own cadence: clamp(days since it played / its usual gap, 0.2, 2). Plays here are
  preference plays: the ones this shuffle picked itself (auto.jsonl) don't count, or it would feed on itself.
- rediscovery: the 20% of heard songs that played longest ago, uniform x the skip factor.
- new: never heard, at most NEW_PER_DAY a day; two early skips on different days rest it 30 days.
- skip factor: 0.6 per early skip and 0.85 per late skip, each fading with a 90-day half-life (history from
  musicdb's weights.json plus what this daemon saw since it was written; floor 0.05). Early = under min(30 s,
  20% of the song). Every skip counts, whoever started the song.
- rests: after a song played to the end 12 h, after a late skip 12 h, after an early skip 48 h; "heard enough"
  (the e key) 1, 3, 7, then 14 days. The last 20 played are avoided when anything else is left.
A lane with nothing eligible lends its turn to familiar, then rediscovery, then new, then any eligible song. The
lane is spent only when its pick starts playing. When the source is a Hits result (rormpc's source.json kind
"hits"), a round plays each song once (hard rule); when all were heard it stops and says so; `shuffle newround`.

Commands: `shuffle on|off|release`, `shuffle reroll`, `shuffle heardenough FILE` (the playing song also skips to
the next), `shuffle unheardenough FILE`, `shuffle newround`.
Files: shuffle.json (state: enabled, nominee {id, file, lane, why}, cycle, new_today, rests, cooldown, recent, live
skips, round, active, reason, outlook: the draw after the pick with its top candidates and their chances); auto.jsonl (one line per song this shuffle started: {start, file}), read by musicdb.
"""
import datetime as dt, json, math, random, time

from . import Module, queue_entry, read_state, state_dir, write_state

LANES = ["familiar"] * 7 + ["rediscovery"] * 2 + ["new"]
NEW_PER_DAY = 5
RECENT_MAX = 20
FAMILIAR_DAYS = 365
REDISCOVERY_SHARE = 0.2
SKIP_EARLY, SKIP_LATE, SKIP_HALF_LIFE_D, SKIP_FLOOR = 0.6, 0.85, 90, 0.05
REST_FINISHED_H, REST_LATE_H, REST_EARLY_H = 12, 12, 48
NEW_QUARANTINE_D = 30
COOLDOWN_DAYS = [1, 3, 7, 14]
# asking "heard enough" again within this many days after the last cooldown ended grows it
COOLDOWN_MEMORY_DAYS = 30
FINISHED_SHARE = 0.8
OUTLOOK_TOP = 30
NOMINEE_PRIO = 1


def today():
    return dt.datetime.now().astimezone().date().isoformat()


class Shuffle(Module):
    name = "shuffle"

    def __init__(self, rng=None):
        saved = read_state("shuffle") or {}
        self.enabled = saved.get("enabled", True)
        self.nominee = saved.get("nominee")
        self.cooldown = saved.get("cooldown", {})  # "heard enough": {file: {until, level}}
        self.rests = saved.get("rests", {})  # after a play or a skip: {file: until}
        self.recent = saved.get("recent", [])
        self.live = saved.get("live", [])  # outcomes seen since weights.json: [{t, file, kind}]
        self.cycle = saved.get("cycle", [])
        self.new_today = saved.get("new_today", {"day": today(), "n": 0})
        self.round = saved.get("round")
        self.outlook = saved.get("outlook")  # the draw after the pick, for rormpc's Shuffle view
        self.current = None  # songid being timed
        self.playing = None  # {id, file, played, last, running, duration, origin}
        self.random_seen = None  # MPD's random at the last wake, to notice it being turned off
        self.active = False
        self.reason = ""
        self.rng = rng or random.Random()
        self._weights = (None, {"files": {}, "global_cadence": 14, "generated": 0})

    def save(self):
        write_state("shuffle", {
            "enabled": self.enabled, "nominee": self.nominee, "cooldown": self.cooldown, "rests": self.rests,
            "recent": self.recent, "live": self.live, "cycle": self.cycle, "new_today": self.new_today,
            "round": self.round, "active": self.active, "reason": self.reason, "outlook": self.outlook})

    # ------------------------------------------------------------ data

    def data(self):
        """weights.json (musicdb sync, hourly), read again when it changes; live outcomes it now covers are
        dropped (the scrobbler's skips and listens reach it through the hourly import)."""
        p = state_dir() / "weights.json"
        try:
            mtime = p.stat().st_mtime
        except OSError:
            return self._weights[1]
        if self._weights[0] != mtime:
            w = read_state("weights") or {}
            if w.get("version") == 2:
                self._weights = (mtime, w)
                gen = w.get("generated", 0)
                self.live = [e for e in self.live if e["t"] > gen]
        return self._weights[1]

    def info(self, file):
        return self.data()["files"].get(file) or {}

    def live_times(self, file, kind):
        return [e["t"] for e in self.live if e["file"] == file and e["kind"] == kind]

    def skip_factor(self, file, now):
        i = self.info(file)
        fade = lambda ts: sum(0.5 ** (max(0, now - t) / 86400 / SKIP_HALF_LIFE_D) for t in ts)
        early = i.get("early", []) + self.live_times(file, "early")
        late = i.get("late", []) + self.live_times(file, "late")
        return max(SKIP_FLOOR, SKIP_EARLY ** fade(early) * SKIP_LATE ** fade(late))

    def last_heard(self, file):
        return max([t for t in [self.info(file).get("last"), *self.live_times(file, "finished")] if t],
                   default=None)

    def heard(self, file):
        return bool(self.info(file).get("heard")) or bool(self.live_times(file, "finished"))

    def source_key(self):
        src = (read_state("source") or {}).get("source") or {}
        return f"{src.get('kind')}:{src.get('name')}" if src.get("kind") == "hits" else None

    def resting(self, file, now):
        c = self.cooldown.get(file)
        return (bool(c) and c["until"] > now) or self.rests.get(file, 0) > now

    def upnext_ids(self, d):
        """Queue ids waiting in Up next (the upnext module's list)."""
        u = d.modules.get("upnext")
        return {int(e["id"]) for e in getattr(u, "entries", [])}

    # ------------------------------------------------------------ choosing

    def next_lane(self):
        if not self.cycle:
            self.cycle = LANES[:]
            self.rng.shuffle(self.cycle)
        return self.cycle[0]

    def new_left(self):
        if self.new_today.get("day") != today():
            self.new_today = {"day": today(), "n": 0}
        return NEW_PER_DAY - self.new_today["n"]

    def quarantined(self, file, now):
        """A new song skipped early on two different days rests NEW_QUARANTINE_D days from the second."""
        ts = sorted(self.info(file).get("early", []) + self.live_times(file, "early"))
        days = {}
        for t in ts:
            days[dt.datetime.fromtimestamp(t).astimezone().date()] = t
        return len(days) >= 2 and now - max(days.values()) < NEW_QUARANTINE_D * 86400

    def pools(self, eligible, now):
        heard = [s for s in eligible if self.heard(s["file"])]
        year = [s for s in heard if (self.last_heard(s["file"]) or 0) > now - FAMILIAR_DAYS * 86400]
        by_age = sorted(heard, key=lambda s: (self.last_heard(s["file"]) or 0, s["file"]))
        old = by_age[:max(1, math.ceil(REDISCOVERY_SHARE * len(by_age)))] if by_age else []
        new = [s for s in eligible if not self.heard(s["file"]) and not self.quarantined(s["file"], now)]
        return {"familiar": year, "rediscovery": old, "new": new if self.new_left() > 0 else []}

    def cadence(self, file):
        return self.info(file).get("cadence") or self.data().get("global_cadence") or 14

    def familiar_score(self, file, now):
        i = self.info(file)
        if i.get("disliked"):
            return 0.1 * self.skip_factor(file, now)
        last = self.last_heard(file)
        g = min(2.0, max(0.2, (now - last) / 86400 / self.cadence(file))) if last else 2.0
        return (1 + i.get("plays", 0)) ** 0.5 * (3 if i.get("liked") else 1) * self.skip_factor(file, now) * g

    def explain(self, lane, file, now):
        i = self.info(file)
        last = self.last_heard(file)
        ago = f"played {round((now - last) / 86400)} d ago" if last else "never played"
        sf = self.skip_factor(file, now)
        skips = f", skips x{sf:.2f}" if sf < 0.99 else ""
        if lane == "familiar":
            like = ", liked" if i.get("liked") else ""
            return f"familiar: {ago}, usually every {self.cadence(file):g} d, {i.get('plays', 0)}x{like}{skips}"
        if lane == "rediscovery":
            return f"rediscovery: {ago}{skips}"
        if lane == "new":
            return f"new ({self.new_today['n'] + 1}/{NEW_PER_DAY} today){skips}"
        return f"any: nothing else was eligible{skips}"

    async def choose(self, d):
        """Nominate the next song (priority 1), or explain why there is none."""
        q = await d.mpd.playlistinfo()
        current = d.status.get("songid")
        now = time.time()
        self.ensure_round()
        in_round = set(self.round["heard"]) if self.round else set()
        waiting = self.upnext_ids(d)
        base = [s for s in q if s.get("id") != current and int(s.get("prio", 0)) == 0
                and int(s["id"]) not in waiting and not self.resting(s["file"], now)]
        if self.round:
            self.round["total"] = len({s["file"] for s in q})
            base = [s for s in base if s["file"] not in in_round]  # each once per round: a hard rule
            if not any(s["file"] not in in_round for s in q if s.get("id") != current):
                self.round["done"] = True
                self.reason = f"round done: all {self.round['total']} heard (shuffle newround starts another)"
                return
        recent = set(self.recent[-RECENT_MAX:])
        eligible = [s for s in base if s["file"] not in recent] or base  # recent is soft
        if not eligible:
            self.reason = "nothing to pick (all resting or requested)"
            return
        lane = self.next_lane()
        used, pool, ws = self.draw_pool(lane, eligible, now)
        pick = self.rng.choices(pool, weights=ws)[0]
        await d.mpd.prioid(NOMINEE_PRIO, pick["id"])
        why = self.explain(used, pick["file"], now) + ("" if used == lane else f" (lent by {lane})")
        self.nominee = {"id": int(pick["id"]), "file": pick["file"], "lane": used, "slot": lane, "why": why}
        self.reason = ""
        self.plan_outlook(eligible, pick, now, len(q), len(base))

    def draw_pool(self, lane, eligible, now):
        """The pool a draw for `lane` samples (a lane with nothing eligible lends its turn) and its weights."""
        pools = self.pools(eligible, now)
        order = [lane] + [x for x in ("familiar", "rediscovery", "new") if x != lane]
        used, pool = next(((x, pools[x]) for x in order if pools[x]), ("any", eligible))
        if used == "familiar":
            ws = [self.familiar_score(s["file"], now) for s in pool]
        else:
            ws = [self.skip_factor(s["file"], now) for s in pool]
        return used, pool, ws

    def plan_outlook(self, eligible, pick, now, queued, not_resting, top=OUTLOOK_TOP):
        """The draw after the pick, as rormpc's Shuffle view shows it: its lane and its top candidates with their
        chance in that draw (if the pick plays and nothing else changes; it is redrawn after every song)."""
        after = [s for s in eligible if s["file"] != pick["file"]]
        lane = self.cycle[1] if len(self.cycle) > 1 else "next cycle"
        used, pool, ws = self.draw_pool(lane if lane != "next cycle" else "familiar", after, now) if after else (
            "none", [], [])
        total = sum(ws) or 1
        ranked = sorted(zip(pool, ws), key=lambda x: -x[1])
        self.outlook = {
            "lane": lane, "drawn_from": used, "pool": len(pool), "eligible": len(after), "queued": queued,
            "resting": queued - not_resting,
            "top": [{"file": s["file"], "p": round(w / total, 4), "why": self.explain(used, s["file"], now)}
                    for s, w in ranked[:top]],
            "rest_p": round(sum(w for _, w in ranked[top:]) / total, 4)}

    async def withdraw(self, d):
        """Take back the pick's priority (its lane is not spent)."""
        if self.nominee:
            e = await queue_entry(d.mpd, self.nominee["id"])
            if e and e.get("file") == self.nominee["file"] and int(e.get("prio", 0)) == NOMINEE_PRIO:
                await d.mpd.prioid(0, self.nominee["id"])
            self.nominee = None

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

    # ------------------------------------------------------------ watching playback

    def tick(self, s, now):
        """Count the seconds the timed song actually played (pauses, the gap's 0:00 pause and stops don't)."""
        p = self.playing
        if p:
            if p["running"]:
                p["played"] += max(0.0, now - p["last"])
            p["last"], p["running"] = now, s.get("state") == "play" and s.get("songid") == str(p["id"])

    def outcome(self, p, now):
        """A song stopped being current: finished, early or late skip; a rest and a live outcome follow."""
        dur = p["duration"] or 0
        if dur and p["played"] >= FINISHED_SHARE * dur:
            kind, rest_h = "finished", REST_FINISHED_H
        elif p["played"] < min(30, 0.2 * dur if dur else 30):
            kind, rest_h = "early", REST_EARLY_H
        else:
            kind, rest_h = "late", REST_LATE_H
        self.rests[p["file"]] = max(self.rests.get(p["file"], 0), now + rest_h * 3600)
        self.live.append({"t": round(now), "file": p["file"], "kind": kind})

    async def song_started(self, d, s, now):
        cur = await d.mpd.currentsong()
        file = cur.get("file")
        if not file:
            self.playing = None
            return
        origin = "other"
        if self.nominee and str(self.nominee["id"]) == s.get("songid"):
            origin = "auto"
            slot = self.nominee.get("slot")
            if slot in self.cycle:
                self.cycle.remove(slot)  # the lane is spent now that its pick plays
            if self.nominee.get("lane") == "new":
                self.new_left()
                self.new_today["n"] += 1
            p = state_dir() / "auto.jsonl"
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("a") as fh:
                fh.write(json.dumps({"start": round(now), "file": file}) + "\n")
            self.nominee = None
        self.playing = {"id": int(s["songid"]), "file": file, "played": float(s.get("elapsed", 0) or 0),
                        "last": now, "running": s.get("state") == "play", "origin": origin,
                        "duration": float(s.get("duration", 0) or cur.get("duration", 0) or 0)}
        self.recent = [f for f in self.recent if f != file][-(RECENT_MAX - 1):] + [file]
        if self.round and file not in self.round["heard"]:
            self.round["heard"].append(file)

    async def on_status(self, d, s, changed):
        dirty = False
        now = time.time()
        self.ensure_round()
        random_on = s.get("random") == "1"
        if self.enabled and not random_on:
            if self.random_seen is None:
                # starting up (or upgraded from a version that ran with random off): this mode owns random
                await d.mpd.random(1)
                random_on = True
                s = d.status = {**s, "random": "1"}
            else:
                # another client turned random off: the user wants the queue in order, this mode yields
                self.enabled = False
                await self.withdraw(d)
            dirty = True
        self.random_seen = random_on
        self.tick(s, now)
        song = s.get("songid")
        if song != self.current:
            if self.playing:
                self.outcome(self.playing, now)
                self.playing = None
            self.current = song
            if song is not None:
                await self.song_started(d, s, now)
            dirty = True
        if self.nominee:
            # gone from the queue, or asked for with Play next (an Up next priority): no longer our pick
            e = await queue_entry(d.mpd, self.nominee["id"])
            if (not e or e.get("file") != self.nominee["file"] or self.nominee["id"] in self.upnext_ids(d)
                    or int(e.get("prio", 0)) != NOMINEE_PRIO):
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
        elif self.nominee and not self.outlook:
            await self.refresh_outlook(d)  # e.g. a pick kept across a restart of an older version
            dirty = True
        if dirty:
            self.save()

    async def refresh_outlook(self, d):
        """The outlook for the pick already made (choose() makes it together with a new pick)."""
        q = await d.mpd.playlistinfo()
        now = time.time()
        current, waiting = d.status.get("songid"), self.upnext_ids(d)
        in_round = set(self.round["heard"]) if self.round else set()
        base = [s for s in q if s.get("id") != current and int(s["id"]) not in waiting
                and int(s.get("prio", 0)) in (0, NOMINEE_PRIO) and not self.resting(s["file"], now)
                and s["file"] not in in_round]
        recent = set(self.recent[-RECENT_MAX:])
        eligible = [s for s in base if s["file"] not in recent] or base
        pick = next((s for s in q if int(s["id"]) == self.nominee["id"]), None)
        if pick:
            self.plan_outlook(eligible, pick, now, len(q), len(base))

    async def on_message(self, d, verb, args):
        if verb == "on":
            self.enabled = True
            if d.status.get("random") != "1":
                await d.mpd.random(1)  # priorities steer MPD only with random on
            self.random_seen = True
        elif verb in ("off", "release"):
            self.enabled = False
            await self.withdraw(d)
            if verb == "off" and d.status.get("random") == "1":
                await d.mpd.random(0)  # off: the queue plays in order; release: plain random stays
            self.random_seen = verb == "release" and d.status.get("random") == "1"
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
        now = time.time()
        # forget what no longer matters: long-past cooldowns (they no longer grow the next one) and rests
        self.cooldown = {f: c for f, c in self.cooldown.items()
                         if c["until"] + COOLDOWN_MEMORY_DAYS * 86400 > now}
        self.rests = {f: t for f, t in self.rests.items() if t > now}
        d.status = await d.mpd.status()
        active, self.reason = self.is_active(d.status)
        self.active = active
        if active and not self.nominee and not (self.round and self.round.get("done")):
            await self.choose(d)
        self.save()
