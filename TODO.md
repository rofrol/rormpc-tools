# TODO

rormpc-tools has its own TODO and coordinator from 2026-10-12, decided by the user (d-33kat4ps), following herdr's
rule of one coordinator per git repository. Before that, rormpc's coordinator (todo-rormpc) kept these items in
rormpc's TODO.md, and its history of rormpc-tools work is there.

Work that spans both repositories works like this (herdr's round of 2026-10-12, herdr TODO a12b9c35). rormpc's
coordinator sends this repository's coordinator a plain message. The receiver decides, queues the item here, and
replies "landed <tag>" when it is done. rormpc's consuming item waits in rormpc's TODO.md as "Waiting on
rormpc-tools". Releases (version bump, tag, push) belong to this repository; rormpc's install script pins the tag.

## Next, in order

- [x] Release years, stage B2 backend (moved from rormpc's TODO t-j74galug, decided 2026-10-10 and 2026-10-11): [t-bd3334hn]
      - `musicdb years --mbid ROW MBID` also rewrites the file's MusicBrainz recording tag (UFID, which the
        scrobbler sends as recording_mbid). Decided by the user 2026-10-11 (d-hkqe4mfl).
      - The report marks the files without a recording MBID (class no-recording, about 165), so rormpc can list
        them under a separate filter that is off by default. Decided by the user 2026-10-11 (d-bzidm3vs).
      - Keep `--json`'s shape compatible: rormpc's Years to review panel reads it (the contract test).
      Plan: docs/release-years-plan.md. When it is released, tell todo-rormpc "landed <tag>": its MBID picker and
      filter wait on it.
      Built 2026-10-12: f15e775 (apply writes the UFID, rollback restores it, `identity.rekey` moves the `mb:`
      keys), version 0.2.54 in 2a10a4f. Tag v0.2.54 pushed (decided by the user 2026-10-12,
      d-gm4tvmnb), "landed v0.2.54" sent to todo-rormpc.
- [x] Media keys: `pause` (key, Now Playing, MPRIS) while "Pause for…" holds the pause cancels the timer and stays [t-jeibd5n3]
      paused. Decided by the user 2026-10-11 (d-h2in7kd6). Code: player/control.py `_pause`.
      Done 2026-10-12: 716e299 (not released yet).
- [ ] Transcribed lyrics (moved from rormpc's TODO t-dmpfe6gj). Plan: docs/transcribe-lyrics-plan.md, cd7fe61. [t-jsjs5ngd]
      All seven choices were decided by the user 2026-10-11:
      - transcribe each new Omarchy Radio download automatically;
      - transcribe the ~274 library songs without lyrics once, overnight;
      - always separate the vocals (demucs);
      - show a transcript that passed the checks at once, labelled machine-transcribed;
      - model: Whisper large-v3, not the measured turbo;
      - a separate uv tool with an inline-dependency script;
      - try one commercial API on the whole evaluation set as an upper bound. This needs the user's API key: a
        worker without one reports it and never signs up.
      Stored as <song>.transcript.json, never as .lrc. Tell todo-rormpc "landed <tag>": rormpc's display of
      transcripts waits on it.
      Slices (coordinator 2026-10-12, by the plan's build order): 1. `transcribe.py`, the inline-dependency backend
      script, the sidecar and offline tests with a fake backend (worker w213, in progress); 2. the evaluation set
      and threshold calibration (needs the model downloads and library copies, outside the worker sandbox); 3. the
      radio auto-run and the overnight library batch; 4. the commercial API upper bound (the user's key); 5. Linux
      faster-whisper. rormpc's display and Enter-to-transcribe are rormpc's items.
- [ ] Lyrics timed for another cut (moved from rormpc's TODO t-7cqbsitp; MONTERO by Lil Nas X). [t-fb4wapwk]
      - Flag a suspicious LRCLIB entry, one whose last line is far before the file's end. The round of 2026-10-11
        proposed thresholds: Sol, more than 30 s and more than 20% of the file; MiMo, a line span below 0.72 of the
        length, more than 45 s left, or 15% off the other candidates' median span. Flag it for checking, never
        reject it.
        Decided by the coordinator 2026-10-12 after consult round 20261012-015328-4f97 (Sol, MiMo, DeepSeek; all
        three dropped the standalone span test: long intros make it false-flag and a uniform shift leaves the span
        unchanged). Flag reasons, each shown with its numbers:
        * `past-end`: a non-empty line's timestamp past the file's length + 2 s (all three);
        * `unordered`: timestamps that decrease in file order, after expanding multi-timestamp lines and applying
          `[offset:]` (all three);
        * `ends-early`: tail = length - the last non-empty line's start > clamp(0.20 x length, 25 s, 90 s) (Sol's
          20%, MiMo's and DeepSeek's absolute cap for long songs, Sol's lower floor for short ones);
        * supporting only, never a flag alone: with two or more other synced candidates that agree within 5 s,
          this entry's last line more than max(20 s, 10%) from them (Sol, MiMo).
        Thresholds are named, provisional constants; the label reads "lyrics end early, check timing". Calibration:
        the coordinator counts the flags over the library read-only after the build and expects at most about 5%
        of synced entries; more goes to "Needs a decision" with the numbers.
        Built 2026-10-12: 31710ca (`musicdb lyrics check [FILE ...] [--dry-run]`, `suspect` in index.json and
        `status --json`). Counted over the library read-only: 83 of 444 synced flagged (53 ends-early, 30
        past-end): above 5%, see Needs a decision. The alignment part waits on the transcription.
      - Later, with the transcription: automatic alignment, using Whisper word timestamps on the vocal stem and a
        robust offset fit, refusing when the evidence is weak.
      Decided by the user 2026-10-11 (d-g7ttw73b): the flag now, the alignment with the transcription. rormpc
      already has the manual nudge keys (5ff3403), which write the song's `[offset:]` tag.
- [ ] AGENTS.md: this repository has its own coordinator, so replace the paragraph that names todo-rormpc as the [t-qwdp2q7k]
      owner, and describe the cross-repository messages and "landed <tag>" (above). Also: `herdr todo run` refuses
      this repository ("the repository has no master commit"; its branch is `main`, herdr report r-12), so describe
      the flow that works here (`herdr worker start --base <main sha>`, `herdr worker verify`, cherry-pick onto
      `main`) until herdr supports `main`.
- [ ] Releases without the auto-mode classifier blocking them (the user's request 2026-10-12: "and so the
      classifier does not block it; ask the herdr coordinator"). `git tag v0.2.54 && git push origin main v0.2.54`
      was refused as "Create Public Surface" until the user approved it (d-gm4tvmnb). todo-herdr's answer
      (herdr TODO 1d9be089, t-oe3wrkvz): no broad allow rule; instead a reviewed `scripts/release.sh <version>` that
      refuses unless on a clean `main` equal to origin/main plus the release commit, the version matches
      pyproject.toml and the pattern, and the tag does not exist; then it creates the annotated tag, gates on
      `uv run pytest -q` without a pipe, and pushes `main` as a fast-forward and only that tag (never --force, never
      --tags). Document it in AGENTS.md's release paragraph. Once committed, send its path to todo-herdr
      (herdr-85), who asks the user for one allow rule on that absolute path; later herdr's driver runs release
      recipes itself.

## Proposed

## Needs a decision

- Which ends-early thresholds should the lyrics timing flag use (t-fb4wapwk, herdr decision in the ledger)?
  Options: 30% clamped to 45-120 s, 22 of 441 (5.0%) | 25% clamped to 30-90 s, 30 (6.8%) | keep 20% clamped to 25-90 s, 53 (12%) | 35% clamped to 60-150 s, 12 (2.7%)
  Checked 2026-10-12 with `musicdb lyrics check --dry-run` (index.json unchanged): most of today's 53 are just over
  the limit (tail 50-60 s against 45-50 s), likely outros of YouTube rips. past-end adds 30 (6.8%) regardless;
  those run up to 90 s and 18 lines past the file's end and stay flagged.

