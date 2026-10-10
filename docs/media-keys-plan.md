# Media keys through mpd-player: plan

Status: stage 1 built (2026-10-10): the command socket (section 1, `player/control.py`, the client is
`mpd-player send`) and the installer's hint and status (section 4 steps 5-6); the Karabiner switch is the user's,
with the old shell script deleted. Stage 2, Now Playing and MPRIS (sections 2-3, section 4 steps 3-4), is not
built. Checked on a scratch MPD 0.24.15: `next`, `previous` and `playid` from a paused player play; `seekcur`
leaves it paused; `next` at the last song stops; `next` while stopped answers "Not playing". The decisions on the
open choices are in rormpc's TODO ("Media keys through mpd-player").

## Why

Media keys today reach MPD through two paths, and neither is mpd-player:

- A Karabiner-Elements complex modification maps the bare F7/F8/F9 to `shell_command`, which starts a zsh script
  that runs `mpc prev|toggle|next` (Previous goes as `mpc sendmessage rormpc "shuffle prev"` when mpd-player is
  subscribed). Measured on 2026-10-10: the script itself takes 26 ms to MPD's answer on an idle machine, but real
  presses spend 18-600 ms between zsh's start and mpc, because Karabiner's console user server has no launchd
  ProcessType and its children wait for CPU while other agents run, and mpc resolves `localhost` through
  mDNSResponder (up to 260 ms under load). MPD restarting the CoreAudio output adds 60-125 ms to every song change
  whatever sends it.
- mpd-now-playable (a separate Python daemon, PyObjC) is the macOS Now Playing provider. Keys that reach it
  (Shift+F7/F8/F9, AirPods, external keyboards, Control Center) send plain MPD `next` / `previous`. MPD's
  `previous` skips the weighted shuffle's trail and counts a skip.
- On Linux nothing in this project provides MPRIS; a hand-installed bridge (mpd-mpris, mpDris2, rmpcd) sends plain
  MPD commands too.

Decided 2026-10-10: mpd-player gets a command socket as the one control interface; Karabiner calls it with
`send_user_command` (no process start, no mpc, no DNS lookup per press); mpd-player registers as the macOS Now
Playing provider and the Linux MPRIS player with the same commands; `rormpc_install.sh companions` installs and
starts it and retires mpd-now-playable.

## 1. The command socket

### What Karabiner sends (checked)

Karabiner-Elements 16.3.0 is installed (`karabiner_cli --version`); `to.send_user_command` arrived in 16.0.0 and the
later release notes do not change it. From the documentation and the console user server's source
(`send_user_command_handler.hpp`):

- The console user server, running as the logged-in user, opens one unbound `AF_UNIX` **datagram** socket and
  `send_to`s each command to `endpoint`. The sender is unbound, so no reply can come back.
- `endpoint` is optional. The default is `/Library/Application Support/org.pqrs/tmp/user/<uid>/user_command_receiver.sock`
  (the directory belongs to the user, mode 0700). The path is used as given: no `~` or variable expansion, and
  it must be shorter than `sun_path` (104 bytes on macOS).
- The payload is `payload` serialised as JSON (`json.dump()`): `{"command":"next"}` arrives as those bytes, a
  string `"next"` arrives with its quotes. One press, one datagram (send buffer 32 KiB).
- A failed send (no socket, nobody bound) is only logged as a warning in Karabiner's console user server log
  (`send_user_command: send_to failed: ...`). The key does nothing else.

The rule in a user's `karabiner.json` becomes:

    {"type": "basic", "from": {"key_code": "f9"},
     "to": [{"send_user_command": {"endpoint": "<socket path>", "payload": {"command": "next"}}}]}

(F7 `prev`, F8 `toggle`). `endpoint` is always given: Karabiner's default path is its own per-user namespace
for any receiver, and binding it would collide with other tools (both models said so).

Key repeat in Karabiner applies to key events, so a held F9 should send one datagram per press, not a stream;
that is expected rather than read in the source, so the live test checks it (a held F9 must not send a burst).

### Socket

- Type: `SOCK_DGRAM`, because that is all Karabiner speaks. One datagram is one command; at most 4 KiB is read,
  anything longer is logged and dropped.
- Path: `$XDG_RUNTIME_DIR/rormpc/player.sock` when `XDG_RUNTIME_DIR` is set (Linux), otherwise
  `$XDG_STATE_HOME/rormpc/player.sock` (macOS: `~/.local/state/rormpc/player.sock`, next to the state files rormpc
  already reads). `mpd-player --socket PATH` overrides it, and `mpd-player socket-path` prints the path in effect
  (for the installer's hint and for tests). A path too long for `sun_path` is never truncated: the daemon logs it
  and runs without the socket, like every other socket failure below.
- Access: the directory is created 0700 and the socket chmod 0600 right after `bind` (with `umask 077` around the
  bind, so there is no window with wider bits). At start the daemon checks that the directory is a real directory
  (not a symlink) owned by its uid, and refuses the socket otherwise; group/other bits on it are taken away (built:
  the state directory older mpd-players created is 0755, and refusing it would leave every Mac without the
  socket). Only the same user
  can send. No token: anyone with the user's uid can already send the same commands over MPD's client-to-client
  channel.
- One owner: the daemon takes an exclusive `flock` on `player.lock` beside the socket and holds it for its whole
  life (a crash releases it). Only under the lock does it unlink a leftover socket at that path (a regular file or
  symlink there is refused, not unlinked) and bind. A second instance (a manual `mpd-player` next to the launchd
  one, or launchd briefly overlapping a restart) fails the lock, logs it and runs without the socket rather than
  stealing it.
- Start and exit: bind before subscribing to MPD's channel, unlink on clean exit. mpd-player exits when MPD drops
  the connection (launchd/systemd restarts it within its 10 s throttle); the socket is bound again at each start.

### Protocol

One canonical form, a JSON object with an allowlisted command (both models: several syntaxes and the module
grammar over the socket are a parsing and compatibility surface):

    {"command": "next"}                      # v 1 implied
    {"v": 1, "command": "seek", "position": 83.5}

- `command` must be one of the transport commands below; typed fields (`position`, a number) only where a command
  takes one; unknown fields are ignored; `v` other than 1 is refused and logged.
- One lenient form for people and `socat`: a datagram that is not JSON and is exactly one allowlisted command word
  (`next`) is taken as `{"command": "next"}`. Anything else (a JSON string, a module command, an unknown word,
  bad JSON, more than 4 KiB) is dropped with one log line, rate-limited so a stuck sender cannot fill the log.
- Module commands (`shuffle on`, `gap set 5`, ...) stay on the MPD channel; they are not keys. One internal command
  type is fed by both adapters (socket, MPD channel), and later Now Playing / MPRIS.

Transport commands (new, owned by the daemon itself rather than a module). Each reads a fresh MPD status first (as
`go_back` already does), so toggle and Previous never decide from the status of the last wake:

- `next`: MPD `next`. With the weighted shuffle active the plan is published as MPD priorities (Up next above it),
  so MPD's own `next` already plays the plan's head; the shuffle sees the change as a skip exactly as today with
  `mpc next`. During the gap's silence or a "Pause for…" pause, Next means "go on with the next song": the
  timer is cancelled and the next song plays (if MPD `next` leaves a paused player paused, the command presses play
  after it when the pause was the gap's or the timer's). Next from a pause the user made: open choice 4.
- `prev`: the same path as `shuffle prev` (trail, `playid`, prev.jsonl, the existing 0.25 s debounce); when the
  shuffle is not active `go_back` already falls back to MPD `previous`. Inside the gap's silence or a timed pause
  it cancels the timer like Next, and `playid` starts playback.
- `toggle`: play when paused or stopped, pause when playing (as `mpc toggle`). During the gap's silence a play ends
  the silence (the gap module already cancels it on any change of state or song); during "Pause for…" a play
  cancels the timer (the pause module already does). A pause from the toggle during a song keeps the gap armed as
  today.
- `play`, `pause`, `stop`: idempotent, for Now Playing / MPRIS, which have separate commands; they never toggle.
- `seek` with `position` (seconds): absolute position, for Now Playing's scrubber and MPRIS `SetPosition`/`Seek`.

Whether MPD `next` (and `playid`) leave a paused player paused is checked on a scratch MPD before the code relies
on it; the rule above is the required outcome either way.

### Inside the daemon

The socket is a `loop.create_datagram_endpoint(..., sock=...)` on the daemon's own loop; its `datagram_received`
only parses and appends the command to a bounded queue (32 entries; beyond that the newest is dropped and logged),
then sets the daemon's wake event, the same event the MPD idle task sets. `step()` drains that queue first, then MPD
channel messages, then the status refresh and the modules, exactly where channel messages run today. So socket
commands are serialised with everything else, a command never runs inside a callback, and two quick presses run in
order. Order is kept within each source; between the socket and the MPD channel the order is "socket first within
one step", which nothing depends on. A command that arrived while MPD was disconnected is never replayed after a
restart: the queue lives in the process.

Each command is logged to stderr with its source (`socket`, `channel`, `nowplaying`, `mpris`) and the time from
receipt to MPD's answer, so a lag report can be read from the daemon's log instead of a separate script log.

### When mpd-player is not running

The key does nothing: `send_to` fails with `ENOENT` (no socket file) or `ECONNREFUSED` (a leftover file, nobody
bound), and Karabiner logs `send_to failed`. A wedged daemon (alive but not reading) loses presses once the
socket's receive buffer is full. There is no fallback inside Karabiner: a rule cannot try one action and then
another. This is accepted because mpd-player is a KeepAlive agent (launchd restarts it within 10 s;
it only stops with MPD itself, when no client could play anything anyway) and because the alternative, a shell
fallback, brings back the cost the change removes. `rormpc_install.sh status` reports the socket (bound or not), and
`mpd-player send next` exits non-zero with "mpd-player is not running (no socket at PATH)" so scripts can fall back
to `mpc` if they want.

### A small client

`mpd-player send COMMAND` (a transport command; `seek` takes the position) sends one canonical JSON datagram to the
socket (for scripts and tests). Fire and forget, like Karabiner; whether it worked shows in the state files and the log. rormpc keeps the MPD channel for now; moving
it to the socket is not part of this plan.

## 2. macOS Now Playing

### What mpd-now-playable does (1.6.2, read on this Mac)

- About 110 lines in `receivers/cocoa/now_playing.py`: `NSApplication.sharedApplication()` with
  `NSApplicationActivationPolicyAccessory` (no Dock icon), `MPRemoteCommandCenter` handlers for toggle, play,
  pause, stop, next, previous and `changePlaybackPosition`; rate, skip and seek-by-interval commands disabled;
  `MPNowPlayingInfoCenter` gets the song info and playback state on each MPD change.
- No app bundle, no Info.plist, no entitlements, no audio of its own: a launchd agent running the uv tool's Python,
  which works on this Mac today.
- It forces the playback state to Playing at startup, so that a paused MPD can be resumed with the keys.
- Threading: its asyncio loop is `corefoundationasyncio.CoreFoundationEventLoop` (vendored in its wheel), a
  `SelectorEventLoop` driven by the main thread's CFRunLoop through `PyObjCTools.AppHelper.runConsoleEventLoop`.
  MPD I/O and the Cocoa callbacks share one thread. Its `loop.time()` is `CFAbsoluteTimeGetCurrent()`, a wall
  clock, not a monotonic one.
- Dependencies: `pyobjc-framework-MediaPlayer` (pulls `pyobjc-core`, `-Cocoa`, `-AVFoundation`, `-CoreMedia`,
  ...), version 12.2.2 here, with wheels for the Python versions uv installs.

### Options

- **A. In the daemon.** On macOS mpd-player runs its asyncio loop on a CoreFoundation loop and registers with
  `MPRemoteCommandCenter` itself. One process, the commands go straight into the queue. Risks: the playback daemon
  (gap, shuffle, pause timers) then depends on PyObjC and on a third-party event loop whose clock is a wall clock;
  a PyObjC crash or a hang on the main thread stops the gap and the shuffle too.
- **B. A second process from the same package.** `mpd-player nowplaying` (its own launchd agent, installed by
  `companions` beside mpd-player) holds the CFRunLoop, PyObjC and its own MPD connection (idle for song info),
  and sends every remote command to the command socket. The playback daemon keeps the plain asyncio loop and no
  PyObjC import; the Now Playing part can crash, restart or be turned off on its own. It costs a second Python
  process (~50 MB, as mpd-now-playable does today) and one more agent.
- **C. A small Swift helper.** Same split as B, the helper written in Swift. Needs a Swift toolchain at install
  time (or a prebuilt, signed binary per release), which the installer has avoided so far.

Recommended: **B** (both models, and it matches the earlier Rust-port notes in rormpc's TODO, which kept the
provider out of every other daemon). A wall-clock `loop.time()` breaks asyncio's monotonic assumption: the gap's
seconds, the Previous debounce, `wait_for` timeouts and python-mpd2's timeouts would misfire on a clock step or
after sleep, and an uncaught Objective-C exception or a stuck main thread would take the playback rules down with
the widget. C (Swift) only if B shows a Python-specific problem: Swift does not change what macOS requires of a
Now Playing app, and it adds a toolchain or signed binaries to the release.

B in detail:

- Same package, entry point `mpd-player nowplaying`; imports PyObjC only there, so `mpd-player` itself never loads
  it. Dependencies `pyobjc-framework-MediaPlayer` (and `-Cocoa` for NSApplication) with `sys_platform == 'darwin'`.
- No asyncio in this process. corefoundationasyncio, which mpd-now-playable vendors, is a single 0.0.1 release on
  PyPI from 2019; the separate process does not need it. Main thread: NSApplication with the accessory policy and
  `PyObjCTools.AppHelper.runConsoleEventLoop`, the MPRemoteCommandCenter handlers and every MPNowPlayingInfoCenter
  update. One plain thread: a blocking python-mpd2 `MPDClient` that idles on player/playlist and posts each new
  snapshot to the main thread with `AppHelper.callAfter`. Remote commands need no MPD connection: a handler sends
  one datagram to mpd-player's socket (non-blocking, microseconds) and returns Success, or
  `MPRemoteCommandHandlerStatusCommandFailed` when the send fails (mpd-player down). Handlers that arrive off the
  main thread do the same, since a `sendto` is thread-safe.
- A lost MPD connection ends the process, and launchd restarts it (as mpd-player does today); each start and each
  wake (`NSWorkspaceDidWakeNotification`) does a full refresh. Artwork is fetched on the MPD thread, capped in
  size, and dropped when it arrives for a song that is no longer current.
- Song info: title, artist, album, duration, elapsed and rate set on every MPD player change and seek (no 1 Hz
  ticking; the system extrapolates from elapsed and rate). Stopped clears the info.
- Commands: toggle, play, pause, stop, next, previous, change position. Previous goes to `prev` (the trail), never
  MPD `previous`; that is the point of the change for AirPods and Control Center. Rate and skip/seek-by-interval
  commands disabled as in mpd-now-playable.
- Claiming Now Playing: mpd-now-playable forces Playing at startup so the keys can resume a paused MPD, which also
  takes the slot from a browser or Music while MPD is idle. Both models advise against it; open choice 2.
- Whether the unbundled process keeps working is an acceptance gate, not an assumption: mpd-now-playable proves it
  on this Mac today, but cold login, the lock screen and a competing player are tested before the switch (test
  plan). An Info.plist/bundle id is added only if a test fails without one.

## 3. Linux MPRIS

- `org.mpris.MediaPlayer2.mpd_player` (MPRIS names allow `[A-Za-z0-9_]`; one instance per user, so no `.instance`
  suffix) on the session bus, object `/org/mpris/MediaPlayer2`, interfaces `org.mpris.MediaPlayer2` (Identity
  "mpd-player", `CanQuit` false, `CanRaise` false, `HasTrackList` false) and `org.mpris.MediaPlayer2.Player`
  (`PlayPause`, `Play`, `Pause`, `Stop`, `Next`, `Previous`, `Seek`, `SetPosition`; `PlaybackStatus`, `Metadata`
  with `mpris:trackid` as `/org/mpd_player/song/<MPD song id>`, `xesam:title`, `xesam:artist` (a list),
  `xesam:album`, `mpris:length` in microseconds; `PropertiesChanged` for `PlaybackStatus`, `Metadata` and the
  `Can*` properties whenever they change; `Position` computed from a fresh MPD status on every read and never
  signalled, as the spec says; `Seeked` emitted after a seek or a jump within the same song; `CanGoNext` /
  `CanGoPrevious` from the real queue (false on an empty queue), `CanPlay`/`CanPause`/`CanSeek` from the state,
  `CanControl` true). `Play`/`Pause`/`Stop` map to the idempotent commands, never to toggle.
- Library: `dbus-fast` (asyncio, maintained; `dbus-next` is no longer maintained), a dependency only with
  `sys_platform == 'linux'`. It runs in the daemon's own loop, so on Linux it is option A without its risks.
- No session bus (a headless box, a systemd user unit started before login without
  `DBUS_SESSION_BUS_ADDRESS`): log once and run without MPRIS; the socket and MPD channel still work. The unit is
  ordered `After=dbus.socket` so a normal login has the bus at start. When the bus connection drops or the name is
  lost (another player took `org.mpris.MediaPlayer2.mpd_player`), dbus-fast's disconnect / NameLost signal is the
  event: the daemon logs it and reconnects or re-requests the name once on that event, never on a timer.
- Keys: GNOME and KDE route media keys to the active MPRIS player; Hyprland/Omarchy bind them to `playerctl` or
  their own shell service, which then reaches mpd-player as the MPRIS player. A playing browser can still be the
  active player there; `playerctl -p mpd_player ...` bindings, or `mpd-player send`, pin the keys to MPD.
- Only one MPD MPRIS bridge should run: the installer warns when `mpd-mpris`, `mpDris2` or `rmpcd` with MPRIS is
  found running.

## 4. Install and retiring mpd-now-playable

`rormpc_install.sh companions` (in rormpc):

1. Installs rormpc-tools as today; the PyObjC and dbus-fast dependencies come with it through platform markers.
2. `service mpd-player ...` as today (the daemon binds the socket at start).
3. macOS: retire mpd-now-playable first (both models: two providers flap the metadata and both answer the keys):
   if its LaunchAgent (`me.00dani.mpd-now-playable`) is loaded, `mpd-now-playable uninstall-launchagent` (its own
   command, so the installer never edits its plist), then check with `launchctl print` that the job is gone before
   going on; print that `uv tool uninstall mpd-now-playable` is left to the user.
4. macOS: `service mpd-player-nowplaying "" "$tools/mpd-player" nowplaying`, then check that it is running
   (`service_state`). If it is not, say so and how to bring mpd-now-playable back
   (`mpd-now-playable install-launchagent`); the installer does not restore it on its own.
5. Prints the socket path and the Karabiner snippet for users who want the bare keys to reach MPD while another app
   holds Now Playing. The installer never edits `karabiner.json`.
6. `status` gains: socket bound or not, Now Playing provider (which agent), MPRIS name owned or not.

A rollback (`rormpc_install.sh companions` from an older tag) brings back an older mpd-player without the socket;
the Karabiner rule then sends into nothing until mpd-now-playable or the old shell rule is back. The release notes
must say so.

Docs: rormpc's RORMPC.md "Media keys" is rewritten (the socket, the snippet, Now Playing / MPRIS from mpd-player,
mpd-now-playable removed); this repository's README gets the socket and the commands under mpd-player; the TODO's
"Media keys and Now Playing" history stays as history.

The user's own setup (outside these repositories, done with the install): the Karabiner rule switches from
`shell_command` to `send_user_command`; the old shell script stays as a manual fallback until the new path has
run for a week; the mpd-now-playable LaunchAgent is removed by step 4.

## 5. Test plan

Automated (pytest, offline, the fake MPD of the existing tests):

- Payload parsing: canonical object, `v`, `position`, a bare allowlisted word, a JSON string, a module command,
  invalid JSON, an oversize datagram, an unknown command; each ends as the expected command or a rate-limited drop.
- Socket lifecycle in a temporary directory: 0700/0600 modes, a symlinked or foreign-owned directory refused, a
  leftover socket replaced, a regular file at the path refused, the socket kept when another process holds the
  lock, unlink on exit, a too-long path refused. The queue's bound drops the newest.
- Ordering: socket commands queued while a step runs execute in order in the next step, before the status refresh,
  each after a fresh status.
- Transport matrix: `next`, `prev`, `toggle`, `play`, `pause`, `stop` from play, a user pause, stop, inside the gap's
  silence and inside "Pause for…": the timer is cancelled and never fires later and Next/Previous end up playing;
  Next from a user pause as open choice 4 decides; `prev` keeps the trail path and its debounce.
- Now Playing / MPRIS mapping as pure functions: MPD status + current song → info dict / MPRIS properties; remote
  command → command line. The PyObjC and D-Bus layers stay thin and are not unit-tested.

On a scratch MPD (per AGENTS.md: own `MPD_PORT`, data dir and `XDG_STATE_HOME`; stop the scratch mpd-player by this
checkout's path):

- `mpd-player send next|prev|toggle` and `socat` with Karabiner's exact payload bytes; latency from the log line
  (receipt → MPD answer) over 100 sends while the machine is busy.
- MPD's own behaviour the rules rely on: `next` and `playid` from a paused player (play or stay paused).
- macOS: the Now Playing widget shows the scratch MPD's song; its next/previous/scrub reach the daemon (Control
  Center by hand). This needs the scratch provider to be the only one, so it runs with the user's
  mpd-now-playable stopped, which is the user's call (open choice 3).
- Acceptance gates for the unbundled Now Playing process before mpd-now-playable is retired: started by launchd at
  a cold login, MPD paused at start, the lock screen's controls, a browser playing a video and then stopping, sleep
  and wake, no audio output device change needed.
- Linux (a VM or the Linux box): `busctl --user introspect org.mpris.MediaPlayer2.mpd_player /org/mpris/MediaPlayer2`,
  `playerctl -p mpd_player next|previous|play-pause|metadata`, MPRIS without a session bus.

Live, by the user (after install): F7/F8/F9 with the new rule while a browser holds Now Playing; a held F9 sends one
`next`, not a burst (from the daemon's log); Shift+F9,
Control Center and AirPods (if used) through the Now Playing provider; F7 walks the trail (prev.jsonl "confirmed",
no skip); a held F7 walks back at most four songs a second; F8 inside a gap and inside "Pause for…"; a reboot and a
sleep/wake with MPD paused; the lag check with `log stream` as in the "Media key Next lags" measurements.

## Consultation

Round 2026-10-10 (GPT-6.1 Sol d8771e3c, MiMo ab87859e; the brief: the daemon's structure, Karabiner's datagram
transport, mpd-now-playable's design, the draft above with options A/B/C). Every claim was checked against the
code and Karabiner's source before it went into the plan.

- Agreed by both, taken: B for Now Playing (a wall-clock CFRunLoop loop and PyObjC faults must not reach the
  playback daemon); keep `SOCK_DGRAM` and promise no delivery; a dedicated `endpoint`, not Karabiner's default;
  one canonical JSON payload with an allowlist instead of several syntaxes; the lock held for the process's life
  and the unlink only under it; no forced Playing at startup; retire mpd-now-playable before the new provider
  starts; MPRIS needs its signals, real `Can*` values and idempotent Play/Pause.
- Sol only, taken: read a fresh status before every transport command; define Next/Previous inside the gap's
  silence and "Pause for…" (cancel the timer, then move and play); treat unbundled Now Playing as acceptance tests
  (cold login, lock screen, competing players); check the socket directory's owner and refuse symlinks; a bounded
  queue and rate-limited drop logs.
- MiMo only, taken: remote-command handlers may run off the main thread (here they only `sendto`, which is
  thread-safe); claim Now Playing on the first MPD activity instead of at startup (open choice 2).
- Considered, not taken now: a version field as mandatory (Sol): `v` is optional, defaulting to 1; a reply-capable
  stream socket for the CLI (Sol, "later if needed").
- Dismissed: "`step()` blocks in MPD idle, so a socket command waits" (MiMo): idle runs in its own task and the
  main loop waits on an event that any source sets. "Debounce every verb, against a held F9 wiping the plan"
  (MiMo): Karabiner's key repeat applies to key events (checked live), Now Playing sends one command per press, and a debounce
  on Next would eat real double presses; the live test checks for bursts instead.

## Open choices

- Now Playing on macOS: a second process, an in-daemon provider, or a Swift helper?
  Options: B, a second process from the same package with its own launchd agent (both models) | A, inside the
  mpd-player daemon on a CoreFoundation loop | C, a small Swift helper
- When should the provider claim Now Playing?
  Options: on the first MPD play after it starts, then the true state (paused stays paused; MiMo) | at startup as
  Playing like mpd-now-playable (the keys resume a paused MPD right after login, but it takes the slot from a
  browser or Music) | only while MPD plays, released when stopped
- The Control Center test on a scratch MPD needs the user's mpd-now-playable stopped for about 15 minutes: may the worker stop it and start it again after?
  Options: the user runs that test after the install instead | the worker stops and restores it (bootout, then
  `mpd-now-playable install-launchagent`)
- Next (key, Now Playing, MPRIS) while the user has paused MPD: play the next song, or stay paused on it?
  Options: play it, as Previous already does through `playid` | stay paused, as MPD's own next does today with mpc
- Karabiner switch: keep the old shell rule's script after the rule moves to `send_user_command`?
  Options: keep it as a manual fallback for a week, then delete | delete it with the switch
- Linux MPRIS: on by default whenever a session bus is present, or opt-in?
  Options: on by default, warn when another MPD MPRIS bridge is running | opt-in with `mpd-player --mpris`
