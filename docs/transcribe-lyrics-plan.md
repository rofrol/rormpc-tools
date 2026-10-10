# Transcribed lyrics: measurement and plan

Status: plan only (2026-10-11), nothing built. The open choices at the end need the user's answers before stage 1.

## Goal

Songs with no lyrics on LRCLIB get a transcript of their sung words, made locally by a speech recognizer, shown in
rormpc's Lyrics pane and clearly labelled as machine-transcribed. The user asked about the Omarchy Radio tracks
(2026-10-10): "extract them with whisper or something? see [the yt-wh script]". That script downloads audio with
yt-dlp and transcribes it with MLX Whisper (`whisper-large-v3`) plus pyannote speaker diarization; diarization is
not useful for songs, the Whisper part is.

Scope: about 274 library songs whose LRCLIB state is `none`, and new radio.omarchy.org tracks (English, AI-made, never
on LRCLIB). A transcript never replaces real lyrics, and a song with lyrics is never transcribed.

## Measurements (2026-10-11)

Sample: three radio.omarchy.org tracks (English), one library song with no lyrics (Polish), one instrumental
library track, and two library songs with LRCLIB synced lyrics as references: an English ballad with long
melismatic lines and a Polish pop song with a 10 s instrumental intro. Each is 3-5 minutes. Copies in a temporary
directory; nothing in the library or the lyrics directory was touched. M-series Mac (pre-M5), macOS.

Tools: mlx-whisper (`large-v3-turbo` and `large-v3`, already in the Hugging Face cache), whisper.cpp 1.9.4 from
Homebrew (`ggml-large-v3-turbo-q5_0`, 548 MB, and the Silero v5.1.2 VAD model, downloaded for the test), demucs
4.0.1 `htdemucs --two-stems vocals` in a throwaway `uv run --with` environment (torch 2.5.1). Decoding options for
mlx-whisper: `condition_on_previous_text=False`, word timestamps, `hallucination_silence_threshold=2`.

WER: word error rate against the LRCLIB text after lowercasing and removing punctuation; it also counts ad-libs and
repeats the published text leaves out, so it overstates errors a little. Sync: for each reference line, the
distance from its timestamp to the nearest transcript segment start.

### Time per song

| Step | Time per 4-5 min song |
|---|---|
| demucs vocals, CPU | 105-125 s (153 s the first time, with the model download) |
| demucs vocals, MPS | fails: `Output channels > 65536 not supported at the MPS device`; `PYTORCH_ENABLE_MPS_FALLBACK=1` does not help |
| mlx-whisper turbo | 13-22 s (39 s once) |
| mlx-whisper large-v3 | 18-43 s |
| whisper.cpp turbo q5_0 | 17-36 s; with VAD 2-23 s |

The whole library's 274 songs with turbo on separated vocals: about 274 × 2.3 min ≈ 10.5 hours, almost all demucs.

### Accuracy on the references

| Variant | English WER | English sync | Polish WER (language forced to pl) | Polish sync |
|---|---|---|---|---|
| mlx turbo, full mix | 0.28 | median 0.7 s, 35/53 lines within 1 s | 0.19 | median 1.2 s, 22/47 |
| mlx turbo, vocals | 0.28 | 0.5 s, 38/53 | **0.05** | 1.0 s, 25/47 |
| mlx large-v3, full mix | 0.29 | 0.4 s, 40/53 | 0.19 | 1.2 s, 21/47 |
| mlx large-v3, vocals | **0.23** | 0.5 s, 39/53 | 0.07 | 1.1 s, 23/47 |
| whisper.cpp, full mix | 0.39 | 0.8 s, 32/53 | 0.15 | 3.6 s, 9/47 |
| whisper.cpp, vocals | 0.37 | 4.0 s, 6/53 | 0.15 | 1.8 s, 16/47 |
| whisper.cpp + VAD, full mix | 0.98 (7 words) | - | 1.00 (3 words) | - |
| whisper.cpp + VAD, vocals | 0.79 | 8.8 s, 5/53 | 0.07 (auto-detected pl) | 7.4 s, 3/47 |

Findings:

- **Language detection is the biggest failure.** Whisper detects the language from the first 30 s. On the Polish
  reference that is an instrumental intro: every variant except whisper.cpp + VAD on vocals said `en` (p 0.26-0.76)
  and then *translated* the Polish singing into English (WER 0.90-1.52). Detecting on a 30 s window that starts at
  the first loud frame of the separated vocals gave `pl` 0.99; the radio tracks and the Polish no-lyrics song were
  detected correctly either way.
- **Separation helps when the language is right**: Polish 0.19 → 0.05 (turbo), English 0.29 → 0.23 (large-v3);
  turbo English did not change. It is also what makes VAD and language detection usable (below).
- **turbo vs large-v3**: no consistent winner (English 0.28 vs 0.23, Polish 0.05 vs 0.07), large-v3 takes about 1.5×
  as long, and large-v3 has its own hallucinations. turbo is the default.
- **whisper.cpp is worse here** (WER 0.37-0.39 English, sync off on vocals), and its VAD merges the vocals into
  ~30 s segments whose starts move (first line 32.6 s instead of 11.6 s): unusable for `.lrc`. VAD on the full
  mix drops almost all singing.
- **Sync** is good enough for highlighting the current line (median 0.4-1.2 s), not for karaoke: about a third of
  the lines are more than 1 s off.
- **Instrumentals hallucinate.** On the instrumental track turbo wrote "Thank you." every ~30 s (mix and vocals),
  with `no_speech_prob` 0.0 and `avg_logprob` -0.2 to -0.6, so Whisper's own scores do not flag it. large-v3 on the
  vocal stem wrote two lines of Norwegian subtitle credits. whisper.cpp on the mix wrote `*music*`. Silero VAD on the
  vocal stem returned nothing: the only signal that worked. An RMS threshold on the vocal stem did not (demucs
  leaks: 193 of 220 s above 10% of the peak).
- **Radio tracks** read well: turbo on the mix and large-v3 on vocals differ by WER 0.07-0.27 between themselves.
  The proper noun "Omarchy" comes out as "Omar key", "Oh, Marquis" or "All marking"; `initial_prompt` with the
  artist and title did not fix it (with `condition_on_previous_text=False` it only reaches the first window).

## Design

### Command

`musicdb lyrics transcribe FILE ... | --current | --playlist NAME` (rormpc-tools, `lyrics.py` with a new
`transcribe.py`), only for songs whose lyrics state is `none`, `untagged` or not checked; a song with `synced`,
`plain` or `instrumental` from LRCLIB is skipped with a note. `--force` redoes a transcript, never real lyrics.

Pipeline per song:

1. Decode with ffmpeg; the audio identity is the sha256 of the 16 kHz mono PCM plus its length (a tag edit or a move
   does not invalidate it, a different encoding does).
2. demucs `htdemucs --two-stems vocals` on CPU; the vocal stem is cached by audio identity in the tool's cache
   directory, so a re-run (another model, other splitting) costs seconds, not minutes.
3. Silero VAD on the vocal stem. Below a minimum amount of singing (a ratio and a duration, thresholds to calibrate
   on labelled examples, see the open choices) the result is state `no_vocals`: nothing is shown. It is not called
   `instrumental`, which stays LRCLIB's or the user's statement.
4. Language: the stored override (`musicdb lyrics lang FILE CODE`) first; else a majority vote of Whisper's
   language detection over several 30 s windows that start at VAD speech regions of the vocal stem. Always the
   `transcribe` task, never `translate`.
5. Whisper turbo on the vocal stem (macOS: mlx-whisper; Linux: faster-whisper), `condition_on_previous_text=False`,
   word timestamps, `hallucination_silence_threshold`; on faster-whisper also `hotwords` from the artist and title
   (for names like "Omarchy").
6. Checks that mark a transcript `withheld` (kept, not shown by default): words outside VAD speech regions, a line
   repeated far more often than the song's own chorus pattern, a known credit/subtitle line only when it also falls
   outside speech (never a blind phrase blocklist: "Thank you" can be a lyric), very low or very high word density.
7. Lines: Whisper's segments regrouped on pauses and punctuation (stable-ts style regrouping, not a hand-written
   gap split), each line starting at its first word. The raw words with their timestamps are kept, so the splitting
   can change without transcribing again.

### Storage and labelling

Beside the lyrics, mirroring the song path like the translations (`<song stem>.pl.json`):

- `<song stem>.transcript.json`: state (`shown` | `withheld` | `no_vocals` | `failed`), the lines with their times,
  the raw words, language and how it was found, backend, model and versions, VAD speech seconds, the reasons a
  transcript is withheld, the audio identity, `transcribed_at`. Written atomically (temporary file, then rename).
- Never `<song stem>.lrc` or `.txt`: upstream rmpc's pane shows any `.lrc` with no idea of its source, so a
  transcript there would look like real lyrics, and LRCLIB's `fetch --recheck` would overwrite or be confused by it.
  `index.json` stays LRCLIB's record; when LRCLIB later finds lyrics, those win and the transcript is left unused.

Precedence in rormpc's Lyrics pane: `.lrc` > `.txt` > a `shown` transcript > the existing note (none, not checked).
A transcript is shown with a status row "machine-transcribed by Whisper (model), may be wrong" and its own colour;
a `withheld` one only through a menu entry ("Show withheld transcript…") with its reasons. The current line is
highlighted from the transcript's line times.

Translation: `musicdb lyrics translate` may take a shown transcript as its original (tekstowo.pl first only when
the song has real tags; usually Claude). The sidecar records `original_file: transcript` and rormpc labels it
"machine translation of a machine transcript"; it is never labelled human.

### How rormpc triggers it

- Lyrics pane, a song with no lyrics: the note gains "Enter: transcribe (about 2 min)". The command runs in the
  background like the translation lookup, and the pane reloads when the sidecar appears.
- Queue menu: "Transcribe lyrics" for the selected song(s), the same command with the files.
- Radio: `musicdb lyrics transcribe --playlist radio.omarchy.org` runs over the accepted, downloaded tracks with no
  lyrics, resumable (each song's sidecar is written as it finishes; a song with a sidecar for the same audio is
  skipped). Whether new radio downloads are transcribed automatically is an open choice.
- No `--all` by default: the whole library is about 10 hours of CPU and the user decides on it.

### Dependencies per platform

Heavy and optional: torch, demucs and a Whisper backend are not part of the base install. They live in a separate
environment (a `transcribe` extra installed as its own `uv tool`, or a script with inline dependencies run through
`uv run`), because torch, CTranslate2 and platform wheels pin differently on each platform and must not break the
base tools. The command checks the environment and says how to install it when it is missing.

| | macOS (Apple Silicon) | Linux |
|---|---|---|
| decode | ffmpeg (Homebrew) | ffmpeg (distribution package) |
| separation | demucs 4.0.1 + torch 2.5.x, CPU (MPS fails, see above) | demucs + torch CPU wheels; CUDA when present |
| VAD | Silero VAD (the `silero-vad` package, ONNX or torch) | the same |
| ASR | mlx-whisper, `mlx-community/whisper-large-v3-turbo` (about 1.6 GB, already cached on the user's Mac) | faster-whisper (CTranslate2) with `large-v3-turbo`, CPU int8 or CUDA |
| models on disk | htdemucs about 80 MB, Whisper turbo 1.6 GB, Silero 2 MB | the same, Whisper in CTranslate2 format |

The two ASR backends do not decode identically: both get the same fixture songs in tests (offline: a short
synthetic or public-domain clip, no library files), and the sidecar records the backend so results are not mixed
silently. Linux speed is unmeasured; on CPU expect several times realtime for Whisper.

## Build order

1. An evaluation set before code: the two references above plus about 10 more songs with LRCLIB lyrics (several
   languages, quiet singing, harmonies, long intros, rap) and 5 instrumentals labelled by hand. Calibrate the VAD
   ratio/duration and the withheld checks on it. No library writes.
2. `transcribe.py` with the pipeline, the sidecar and its tests (offline, a fake backend returning fixed words: the
   no-lyrics-only rule, never touching `.lrc`/`.txt`/`index.json`, atomic writes, skip on same audio identity,
   language override, withheld reasons). The macOS backend first.
3. rormpc: precedence, label and status row, Enter to transcribe, the withheld menu entry; README and RORMPC.md.
4. Translation of a transcript (`original_file: transcript`, its label).
5. The radio batch (`--playlist`), then optionally the automatic run after a radio download.
6. The Linux backend (faster-whisper) and its parity test.

## Consult round (2026-10-11, GPT-6.1 Sol and MiMo-V2.6-Pro, one round)

Both were given the measurements and the draft design and asked as devil's advocates.

Agreed (both):

- Whisper's `no_speech_prob`/`avg_logprob` are useless here; the vocal-stem VAD is the only working signal, and a
  phrase blocklist is a trap. Adopted: a `withheld` state with reasons instead of silent filtering.
- The display needs a real rormpc change with explicit precedence; a new file name alone shows nothing. Adopted.
- Provenance must follow the transcript into the translation ("machine translation of a machine transcript").
  Adopted.
- Sync is approximate: fine for highlighting, not karaoke. Keep raw words so lines can be re-split (Sol), use
  proper regrouping instead of a word-gap split (MiMo). Both adopted.
- Linux is a second implementation to validate, in its own environment. Adopted.
- Forced alignment (WhisperX, stable-ts, MMS) beats blind ASR only when a text exists; neither knows a credible local
  singing-specific ASR. Noted.

Disagreed:

- Separation: Sol would make it optional (a quality mode), since it costs about five times the transcription; MiMo
  would keep it always, since the gains and the VAD gate depend on it. The plan keeps it for batch and the
  instrumental gate, and asks the user whether `--current` may skip it (open choice).
- Model: MiMo proposed large-v3 for batch and turbo on request; the measurements do not support it (Polish 0.05 vs
  0.07, English 0.28 vs 0.23, large-v3 hallucinates too), so turbo stays the default with large-v3 as an option.
- States: Sol warned that "no speech" is not "instrumental"; adopted as `no_vocals`. MiMo added a ratio/duration
  gate rather than "any speech", a resumable cache keyed on the audio identity, a precise hash definition,
  multi-window language voting instead of the first-vocal window, and `hotwords` for rare names. All adopted.
- MiMo suggested measuring one commercial API on the same songs as an upper bound; not planned (it sends the audio
  out), listed as an open choice.

## Open choices

Transcribe new Omarchy Radio downloads automatically, or only on request?
Options: on request and by `--playlist` batch | automatically after each radio download | never in batch

Run the whole library's songs without lyrics (about 274, about 10 hours of CPU)?
Options: no, only radio and on request | yes, once overnight | only a playlist the user names

May a single-song `--current` request skip vocal separation to answer in about 20 s instead of about 2 min?
Options: no, always separate (accuracy, the instrumental gate and language detection need the stem) | yes, a fast mode on the mix with a lower-confidence label | ask each time

Show a transcript that passed the checks at once, or only after the user accepts it?
Options: show at once with the machine-transcribed label | keep as draft until accepted in rormpc | show at once for radio tracks only

Default Whisper model?
Options: large-v3-turbo | large-v3 | turbo by default with large-v3 on request per song

Where do the heavy dependencies live?
Options: a separate uv tool with an inline-dependency script | a `transcribe` extra of rormpc-tools | documented manual install only

Try one commercial transcription API on the evaluation set as an upper bound (the audio leaves the machine)?
Options: no | yes, only the public radio tracks | yes, the whole evaluation set
