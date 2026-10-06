"""Mute for a while: volume 0 now, the volume from before comes back at the deadline.

Playback goes on while muted (songs still count as listens: the scrobbler counts elapsed time, not audibility), and
the expiry only restores the volume, never sends `play`. The deadline is wall-clock time kept in mute.json, so a Mac
that slept past it unmutes on wake and a restart of this daemon keeps the timer.

Commands: `mute start SECONDS` (again while muted: a new deadline from now), `mute extend SECONDS`, `mute unmute`
(now), `mute cancel` (forget the timer, stay muted). Anyone setting the volume while muted (rormpc, mpc, a phone)
cancels the timer and keeps that volume; a stop or the end of the queue unmutes at once, so the next `play` is not
silently muted. The volume is restored only if it is still 0, and read back before it counts as restored.

mute.json: {"deadline": wall-clock seconds or null, "volume": the volume to restore, "generation": +1 per command
handled, "last": what happened last, "error": why the last command failed or null}.
"""
import time

from . import Module, log, read_state, write_state

MAX_SECONDS = 24 * 3600


def _volume(s):
    """MPD's volume, or None without a mixer (status has no volume, or -1)."""
    try:
        v = int(s.get("volume", -1))
    except ValueError:
        return None
    return v if v >= 0 else None


def _seconds(args):
    seconds = float(args)
    if not 0 < seconds <= MAX_SECONDS:
        raise ValueError(f"seconds must be 1..{MAX_SECONDS}, got {args!r}")
    return seconds


class Mute(Module):
    name = "mute"

    def __init__(self):
        saved = read_state("mute") or {}
        self.deadline_at = saved.get("deadline") if isinstance(saved.get("deadline"), (int, float)) else None
        self.volume = saved.get("volume") if isinstance(saved.get("volume"), int) else None
        if self.volume is None:
            self.deadline_at = None
        self.generation = saved.get("generation", 0) if isinstance(saved.get("generation"), int) else 0
        self.last = saved.get("last")
        self.error = None

    def save(self):
        write_state("mute", {"deadline": self.deadline_at, "volume": self.volume, "generation": self.generation,
                             "last": self.last, "error": self.error})

    def active(self):
        return self.deadline_at is not None

    def finish(self, last):
        self.deadline_at = None
        self.last = last

    async def start(self, d):
        self.save()

    async def on_message(self, d, verb, args):
        self.generation += 1
        self.error = None
        try:
            await self.command(d, verb, args)
        except Exception as e:
            self.error = str(e)
            raise
        finally:
            self.save()  # rormpc waits for the generation to change, also after an error

    async def command(self, d, verb, args):
        s = await d.mpd.status()
        if verb == "start":
            seconds = _seconds(args)
            if not self.active():
                if s.get("state") == "stop":
                    raise ValueError("nothing is playing")
                v = _volume(s)
                if v is None:
                    raise ValueError("MPD has no volume control")
                if v == 0:
                    raise ValueError("the volume is already 0")
                await d.mpd.setvol(0)
                self.volume = v
            self.deadline_at = time.time() + seconds
            self.last = "started"
        elif verb == "extend":
            if not self.active():
                raise ValueError("not muted")
            self.deadline_at = min(self.deadline_at + _seconds(args), time.time() + MAX_SECONDS)
            self.last = "extended"
        elif verb == "unmute":
            if not self.active():
                raise ValueError("not muted")
            await self.restore(d, s, "unmuted")
        elif verb == "cancel":
            if not self.active():
                raise ValueError("not muted")
            self.finish("cancelled")
        else:
            raise ValueError(f"unknown command {verb!r}")

    async def restore(self, d, s, last):
        """Back to the saved volume if it is still 0 (someone else's volume stays)."""
        self.finish(last)
        if _volume(s) != 0:
            return
        await d.mpd.setvol(self.volume)
        back = _volume(await d.mpd.status())
        if back != self.volume:
            self.error = f"volume is {back} after restoring {self.volume}"
            log(f"mute: {self.error}")

    async def on_status(self, d, s, changed):
        if not self.active():
            return
        v = _volume(s)
        if v != 0:  # set by someone during the mute (or before this daemon started again): keep it
            self.finish("overridden")
            self.save()
        elif s.get("state") == "stop":
            self.generation += 1
            try:
                await self.restore(d, s, "stopped")
            finally:
                self.save()

    def deadline(self):
        return self.deadline_at

    async def on_timer(self, d):
        self.generation += 1
        self.error = None
        try:
            await self.restore(d, await d.mpd.status(), "expired")
        except Exception as e:
            self.error = str(e)
            raise
        finally:
            self.save()
