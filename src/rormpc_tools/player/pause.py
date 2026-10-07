"""Pause for a while: MPD pauses now and plays on at a wall-clock deadline.

The deadline is kept in pause.json, so a Mac that slept past it resumes on wake, rormpc may be closed, and a restart
of this daemon keeps the timer (a deadline that passed while it was down resumes at start, if still paused). The
timer holds only the pause it made: anyone pressing play or stop, another song, or a replaced queue (a stop or new
song ids) cancels it, and nothing is resumed later. While it holds the pause, the gap module neither starts nor ends
a silence, so the gap never presses play inside a timed pause; after the resume the gap arms again as usual.

Commands: `pause start SECONDS` (while playing: pause now; while paused: play on after SECONDS; again while the timer
runs: a new deadline from now), `pause extend SECONDS`, `pause resume` (now), `pause cancel` (forget the timer, stay
paused).

pause.json: {"deadline": wall-clock seconds or null, "songid": the song paused, "generation": +1 per command handled,
"last": what happened last, "error": why the last command failed or null}.

A mute.json left by the old "Mute for…" is read once at start: if it was still muting with a saved volume and the
volume is 0, that volume comes back; then the file is removed.
"""
import time

from . import Module, log, read_state, state_dir, write_state

MAX_SECONDS = 24 * 3600


def _seconds(args):
    seconds = float(args)
    if not 0 < seconds <= MAX_SECONDS:
        raise ValueError(f"seconds must be 1..{MAX_SECONDS}, got {args!r}")
    return seconds


async def forget_old_mute(d):
    """The old mute module's leftover: nobody stays silent after the upgrade."""
    old = read_state("mute")
    if old is None:
        return
    if not isinstance(old, dict):
        old = {}
    volume = old.get("volume")
    if isinstance(old.get("deadline"), (int, float)) and isinstance(volume, int):
        s = await d.mpd.status()
        if s.get("volume") == "0":
            await d.mpd.setvol(volume)
            log(f"pause: restored volume {volume} left muted by the old mute module")
    (state_dir() / "mute.json").unlink(missing_ok=True)


class Pause(Module):
    name = "pause"

    def __init__(self):
        saved = read_state("pause") or {}
        self.deadline_at = saved.get("deadline") if isinstance(saved.get("deadline"), (int, float)) else None
        self.songid = saved.get("songid") if isinstance(saved.get("songid"), str) else None
        if self.songid is None:
            self.deadline_at = None
        self.generation = saved.get("generation", 0) if isinstance(saved.get("generation"), int) else 0
        self.last = saved.get("last")
        self.error = None

    def save(self):
        write_state("pause", {"deadline": self.deadline_at, "songid": self.songid, "generation": self.generation,
                              "last": self.last, "error": self.error})

    def active(self):
        return self.deadline_at is not None

    def holds_pause(self):
        return self.active()

    def finish(self, last):
        self.deadline_at = None
        self.last = last

    async def start(self, d):
        await forget_old_mute(d)
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
                if s.get("state") == "play":
                    await d.mpd.pause(1)
                    s = await d.mpd.status()
                if s.get("state") != "pause":
                    raise ValueError("nothing is playing")
                self.songid = s.get("songid")
            self.deadline_at = time.time() + seconds
            self.last = "started"
        elif verb == "extend":
            if not self.active():
                raise ValueError("not paused for a while")
            self.deadline_at = min(self.deadline_at + _seconds(args), time.time() + MAX_SECONDS)
            self.last = "extended"
        elif verb == "resume":
            if not self.active():
                raise ValueError("not paused for a while")
            await self.resume(d, s, "resumed")
        elif verb == "cancel":
            if not self.active():
                raise ValueError("not paused for a while")
            self.finish("cancelled")
        else:
            raise ValueError(f"unknown command {verb!r}")

    def still_ours(self, s):
        return s.get("state") == "pause" and s.get("songid") == self.songid

    async def resume(self, d, s, last):
        """Play on, only if MPD is still paused on the song this timer paused."""
        self.finish(last)
        if not self.still_ours(s):
            self.last = "overridden"
            return
        await d.mpd.pause(0)
        if (await d.mpd.status()).get("state") != "play":
            self.error = "MPD did not play on"
            log(f"pause: {self.error}")

    async def on_status(self, d, s, changed):
        if self.active() and not self.still_ours(s):  # play, stop, another song or queue (also while we were down)
            self.finish("overridden")
            self.save()

    def deadline(self):
        return self.deadline_at

    async def on_timer(self, d):
        self.generation += 1
        self.error = None
        try:
            await self.resume(d, await d.mpd.status(), "expired")
        except Exception as e:
            self.error = str(e)
            raise
        finally:
            self.save()
