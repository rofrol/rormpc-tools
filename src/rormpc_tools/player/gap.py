"""Silence of N seconds between songs, which MPD has no setting for.

While a song plays, single mode is set to oneshot. MPD 0.24 then pauses at 0:00 of the next song (in queue or
random order) when the song ends and resets single to off; after N seconds this resumes it. Nothing of the next
song is heard early. A user pause keeps single at oneshot and the song, so it is never resumed by this; anything
the user does during the silence (play, next, pause, another song) cancels it, and so does "Pause for…" (the pause
module holds the pause and plays on itself; the gap arms again once playing). Single on (1) and repeat on are left
alone. rormpc shows single as "gap" while this runs.

Commands: `gap set N` (seconds, 0 = off, at most 60). The chosen N is kept in gap.json and survives restarts;
`--seconds` is only the default before anything was chosen. gap.json: {"seconds": N}.
"""
import time

from . import Module, read_state, write_state

MAX_SECONDS = 60


class Gap(Module):
    name = "gap"

    def __init__(self, default_seconds):
        saved = (read_state("gap") or {}).get("seconds")
        self.seconds = float(saved) if isinstance(saved, (int, float)) else float(default_seconds)
        self.armed_for = None  # songid whose end we asked MPD to pause after
        self.gap_until = None  # (songid paused at 0:00, wall-clock deadline)

    async def start(self, d):
        write_state("gap", {"seconds": self.seconds})  # rormpc shows the value in effect

    async def on_message(self, d, verb, args):
        if verb != "set":
            return await super().on_message(d, verb, args)
        seconds = float(args)
        if not 0 <= seconds <= MAX_SECONDS:
            raise ValueError(f"seconds must be 0..{MAX_SECONDS}, got {args!r}")
        self.seconds = seconds
        write_state("gap", {"seconds": seconds})
        if seconds == 0:
            self.gap_until = None
            if self.armed_for is not None and d.status.get("single") == "oneshot":
                await d.mpd.single("0")  # we armed it: take it back
            self.armed_for = None

    async def on_status(self, d, s, changed):
        song = s.get("songid")
        if self.gap_until and (s.get("state") != "pause" or song != self.gap_until[0]):
            self.gap_until = None  # the user did something during the silence
        if s.get("state") == "pause" and d.pause_held():  # paused for a while: the pause module plays on
            self.gap_until = None
            self.armed_for = None
            return
        if self.seconds <= 0:
            return
        if s.get("state") == "play" and s.get("single") == "0" and s.get("repeat") == "0":
            await d.mpd.single("oneshot")
            self.armed_for = song
        elif (s.get("state") == "pause" and s.get("single") == "0" and self.armed_for not in (None, song)
              and float(s.get("elapsed", 0)) == 0 and not self.gap_until):
            self.gap_until = (song, time.time() + self.seconds)  # MPD paused at the start of the next song
            self.armed_for = None

    def deadline(self):
        return self.gap_until[1] if self.gap_until else None

    async def on_timer(self, d):
        self.gap_until = None
        await d.mpd.play()
