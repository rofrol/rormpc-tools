# TODO

rormpc-tools has its own TODO and coordinator from 2026-10-12, decided by the user (d-33kat4ps), following herdr's
rule of one coordinator per git repository. Before that, rormpc's coordinator (todo-rormpc) kept these items in
rormpc's TODO.md, and its history of rormpc-tools work is there.

Work that spans both repositories works like this (herdr's round of 2026-10-12, herdr TODO a12b9c35). rormpc's
coordinator sends this repository's coordinator a plain message. The receiver decides, queues the item here, and
replies "landed <tag>" when it is done. rormpc's consuming item waits in rormpc's TODO.md as "Waiting on
rormpc-tools". Releases (version bump, tag, push) belong to this repository; rormpc's install script pins the tag.

## Next, in order

- [ ] Release years, stage B2 backend (moved from rormpc's TODO t-j74galug, decided 2026-10-10 and 2026-10-11): [t-bd3334hn]
      - `musicdb years --mbid ROW MBID` also rewrites the file's MusicBrainz recording tag (UFID, which the
        scrobbler sends as recording_mbid). Decided by the user 2026-10-11 (d-hkqe4mfl).
      - The report marks the files without a recording MBID (class no-recording, about 165), so rormpc can list
        them under a separate filter that is off by default. Decided by the user 2026-10-11 (d-bzidm3vs).
      - Keep `--json`'s shape compatible: rormpc's Years to review panel reads it (the contract test).
      Plan: docs/release-years-plan.md. When it is released, tell todo-rormpc "landed <tag>": its MBID picker and
      filter wait on it.
      Built 2026-10-12: f15e775 (apply writes the UFID, rollback restores it, `identity.rekey` moves the `mb:`
      keys), version 0.2.54 in 2a10a4f. Left: tag v0.2.54, push, "landed v0.2.54" to todo-rormpc (see Needs a
      decision).
- [ ] Media keys: `pause` (key, Now Playing, MPRIS) while "Pause for…" holds the pause cancels the timer and stays [t-jeibd5n3]
      paused. Decided by the user 2026-10-11 (d-h2in7kd6). Code: player/control.py `_pause`.
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
- [ ] Lyrics timed for another cut (moved from rormpc's TODO t-7cqbsitp; MONTERO by Lil Nas X). [t-fb4wapwk]
      - Flag a suspicious LRCLIB entry, one whose last line is far before the file's end. The round of 2026-10-11
        proposed thresholds: Sol, more than 30 s and more than 20% of the file; MiMo, a line span below 0.72 of the
        length, more than 45 s left, or 15% off the other candidates' median span. Flag it for checking, never
        reject it.
      - Later, with the transcription: automatic alignment, using Whisper word timestamps on the vocal stem and a
        robust offset fit, refusing when the evidence is weak.
      Decided by the user 2026-10-11 (d-g7ttw73b): the flag now, the alignment with the transcription. rormpc
      already has the manual nudge keys (5ff3403), which write the song's `[offset:]` tag.
- [ ] AGENTS.md: this repository has its own coordinator, so replace the paragraph that names todo-rormpc as the [t-qwdp2q7k]
      owner, and describe the cross-repository messages and "landed <tag>" (above). Also: `herdr todo run` refuses
      this repository ("the repository has no master commit"; its branch is `main`, herdr report r-12), so describe
      the flow that works here (`herdr worker start --base <main sha>`, `herdr worker verify`, cherry-pick onto
      `main`) until herdr supports `main`.

## Proposed

## Needs a decision

- May the coordinator tag v0.2.54 and push main with the tag (release of t-bd3334hn)?
  Options: yes, tag and push now | you tag and push yourself | wait for more items before releasing
  Checked 2026-10-12: `git tag v0.2.54` with the push was refused by Claude Code's auto-mode classifier ("Create
  Public Surface"); main has the tests passing (594) at 2a10a4f, 5 commits ahead of origin/main, no workflow runs
  on tags beyond the tests (.github/workflows/test.yml runs pytest on main and v* pushes).
