"""mpd-player: the one playback daemon next to MPD, for what MPD has no setting for. It runs with or without
rormpc, so phones, media keys and mpc see the same behaviour.

  mpd-player [--seconds 3]   # runs until killed; installed and started by `rormpc_install.sh companions`

Modules (one file each in this package) see every change of MPD's player, options, mixer and queue, can set a
wall-clock deadline (it survives the Mac sleeping past it) and take commands from clients over MPD's
client-to-client messages: channel "rormpc", one command per message, `<module> <verb> [args]`, e.g.
`mpc sendmessage rormpc "gap set 5"`. A module's state is `$XDG_STATE_HOME/rormpc/<module>.json`, written only by
this daemon (temp file + rename), read by rormpc to show it.

- gap: N seconds of silence between songs (`gap set N`, 0 = off; remembered across restarts).
"""
import argparse, asyncio, json, os, pathlib, sys, time

from mpd.asyncio import MPDClient

CHANNEL = "rormpc"
SUBSYSTEMS = ["player", "options", "mixer", "playlist", "message"]
# longest sleep between wakes: asyncio's clock may stop while the Mac sleeps, so a wall-clock deadline that passed
# during sleep is noticed at most this late after waking
MAX_WAIT = 30.0


def state_dir():
    base = os.environ.get("XDG_STATE_HOME") or str(pathlib.Path.home() / ".local/state")
    return pathlib.Path(base) / "rormpc"


def read_state(name, default=None):
    try:
        return json.loads((state_dir() / f"{name}.json").read_text())
    except (OSError, ValueError):
        return default


def write_state(name, data):
    d = state_dir()
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / f".{name}.json.tmp"
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, d / f"{name}.json")  # atomic: rormpc never reads half a file


def log(msg):
    print(msg, file=sys.stderr, flush=True)


class Module:
    """Hooks, all optional. `d` is the Daemon: `d.mpd` is the MPD client, `d.status` the latest status."""
    name = ""

    async def start(self, d):
        pass

    async def on_status(self, d, status, changed):
        """After every wake, with the fresh status and the subsystems that changed since the last call."""

    async def on_message(self, d, verb, args):
        log(f"{self.name}: unknown command {verb!r}")

    def deadline(self):
        """Wall-clock time (time.time()) at which on_timer should run, or None."""
        return None

    async def on_timer(self, d):
        pass


class Daemon:
    def __init__(self, mpd, modules):
        self.mpd = mpd
        self.modules = {m.name: m for m in modules}
        self.status = {}
        self._pending = set()
        self._event = asyncio.Event()

    async def _watch(self):
        async for changed in self.mpd.idle(SUBSYSTEMS):
            self._pending.update(changed)
            self._event.set()

    async def dispatch(self, text):
        parts = text.split(maxsplit=2)
        if len(parts) < 2 or parts[0] not in self.modules:
            log(f"ignored message {text!r}")
            return
        name, verb = parts[0], parts[1]
        args = parts[2] if len(parts) > 2 else ""
        try:
            await self.modules[name].on_message(self, verb, args)
        except Exception as e:  # a bad command must not stop the daemon
            log(f"{name} {verb}: {e}")

    async def step(self, changed):
        """One round: messages, status to every module, due timers. True when a timer fired (read again)."""
        if "message" in changed:
            for m in await self.mpd.readmessages():
                if m.get("channel") == CHANNEL:
                    await self.dispatch(m.get("message", ""))
        self.status = await self.mpd.status()
        for m in self.modules.values():
            await m.on_status(self, self.status, changed)
        fired = False
        now = time.time()
        for m in self.modules.values():
            due = m.deadline()
            if due is not None and due <= now:
                await m.on_timer(self)
                fired = True
        return fired

    def wait_time(self):
        dues = [d for m in self.modules.values() if (d := m.deadline()) is not None]
        if not dues:
            return MAX_WAIT
        return min(MAX_WAIT, max(0.0, min(dues) - time.time()))

    async def run(self):
        await self.mpd.subscribe(CHANNEL)
        for m in self.modules.values():
            await m.start(self)
        watcher = asyncio.create_task(self._watch())
        changed = set(SUBSYSTEMS) - {"message"}
        while True:
            self._event.clear()  # before reading: a change after this sets it again
            changed |= self._pending
            self._pending = set()
            if await self.step(changed):
                changed = set()
                continue
            changed = set()
            try:
                await asyncio.wait_for(self._event.wait(), self.wait_time())
            except TimeoutError:
                pass
            if watcher.done():
                watcher.result()  # the connection is gone: raise, launchd restarts us


async def _main():
    from . import gap
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seconds", type=float, default=3,
                    help="silence between songs until one is chosen with `gap set N` (then that is remembered)")
    a = ap.parse_args()
    c = MPDClient()
    await c.connect(os.environ.get("MPD_HOST", "localhost"), int(os.environ.get("MPD_PORT", 6600)))
    await Daemon(c, [gap.Gap(a.seconds)]).run()


def main():
    asyncio.run(_main())


if __name__ == "__main__":
    main()
