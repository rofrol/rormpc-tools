# Release years: measurement and repair plan

Status: decided 2026-10-10 (see "Decisions" at the end). Stage A is built: `musicdb years` (src/rormpc_tools/years.py)
computes the rule, writes the dry-run report, records decisions by id, applies and rolls back; `hits` reads
`originaldate`; `write_year` writes TDOR from the rule on new downloads. Stage B: rormpc's "Years to review" view
and its MBID picker; stage C (built): the video-to-audio swap through the work on download.

## The problem

`yt-mp3-mb` (through `mbtag.write_year`) writes the matched MusicBrainz recording's `first-release-date` into both
TDRC and TDOR. That date is "the first release of this recording", not "the song's original release". MusicBrainz
keeps music videos, DJ-mix segments, edits, live takes, new mixes (Dolby Atmos, 360 Reality Audio) and
re-recordings as separate recordings. A YouTube "official video" download is often matched to the *video*
recording through the video's MusicBrainz URL relationship. Such a recording's first release is usually a video
compilation or DVD years later. Examples: a January 1983 single tagged 2000 (its video recording first appears on a
2000 greatest-hits video), and a 1981 single tagged 2004 (its "music video" recording first appears on a 2004 best-of
DVD).

Every view reads TDRC (MPD `Date`): rormpc's Queue Year column (`Truncate(date, 4)`), its album browser (sorted by
date), and `hits` (`library_songs` reads `date`, and the Hits Year column shows that release year for owned
songs). TDOR (MPD `OriginalDate`) is exposed by MPD but read by nothing.

## Measurement (2026-10-10)

Offline scan (tags read with mutagen, recordings from the existing `ytmb` cache, no network): 817 live files, 638 with
a date, 651 with a recording MBID, 556 YouTube downloads. 407 of the 408 dated YouTube downloads have TDRC == TDOR
(written by `write_year`). Suspect classes among the matched recordings:

| Class (offline) | Files |
|---|---|
| Matched recording flagged `video` by MusicBrainz | 69 (49 dated, 20 undated) |
| Disambiguation "part of … DJ-mix" (a segment of a mix) | 29 |
| Film/stage cast recordings | 13 |
| Edits / radio versions | 11 |
| Atmos / 360 / stereo mixes | 9 |
| Earliest release of the matched recording is a compilation, promo or non-album type | 192 of 446 with cached releases |
| Year 3+ years from the album's median (albums with 3+ tracks) | 5 (all classical compilations, noise) |

Network sample: 189 recordings in five strata (all 49 dated video matches, 40 with a version disambiguation, 40
whose earliest release is a compilation, 40 plain YouTube matches, 20 plain non-YouTube rips). Each was looked up on
MusicBrainz at 1 request/s with a cache, 1,098 requests in total (about 5.8 per recording). For each recording the
lookups fetched its work, the work's recordings by the same artist (dated through `rid:` batch searches, because
a title search missed e.g. a 1986 album take entirely), and full release lists for the candidates.

Weighted to the 616 dated files with a cached MusicBrainz recording, using the hybrid rule below as the reference:

- about 206 files keep their year, about 104 differ (69 by 2+ years, 36 by 5+ years), and about 306 have no
  reference (no work relationship in MusicBrainz, which is the case for 46 of the 189 sampled, or no studio
  release found); these keep their year and are listed for review only on a suspect class;
- by stratum: video matches 21 of 36 with a proposal differ (8 by 5+ years); compilation-first 5 of 20 (all by 5+);
  plain YouTube 0 of 24; plain rips 6 of 8; version disambiguations 5 of 17.

The matched video recording for the example above (`bf8373da…`) is confirmed: `video: true`, released only on a
2000 "Greatest Hits" and a 2005 "Ultimate Collection" (both video compilations); its work's audio recordings by the
same artist give 1983.

Precision, checked by hand against well-documented release years for the 37 sampled files whose year would change:
24 proposals are right, 13 wrong. The wrong ones fall into clear groups:

- proposals *later* than the current year: 9, of which 8 are wrong (a deluxe reissue, a 2019 "audio from the music
  video" take, a 2023 Atmos mix treated as a version, 1972 for a 1970 single, 1993 for a 1971 single);
- a cover or cast recording collapsed into the original's work (2 cases), a 2004 album single moved to 2003 (a
  promo);
- restricted to proposals *earlier* than the current year: 28, of which 25 are right or strictly closer
  (e.g. 1962 for a 1961 single tagged 1989).

## Year definition

- **TDOR (OriginalDate)**: the song's original release, the year the views show.
- **TDRC (Date)**: this recording's own first release (a remix, live take, re-recording or new mix keeps its date
  here), as MusicBrainz gives it for the recording the file really is.
- Never the YouTube upload date, which stays in `TXXX:YouTube Upload Date`.
- Provenance: `TXXX:DATE_SOURCE = musicbrainz:<recording MBID>` naming the recording the TDOR came from, and
  `TXXX:DATE_RULE` naming the rule (`same-length`, `any-clean`, `own`), or `first-release` when a download kept its
  recording's own first release (the rule had no proposal, or a later one).

## Computation: a hybrid, with the evidence

Neither consulted way is right alone on the sample:

- MiMo's way (earliest first release over all recordings of the same work and artist) collapses the wrong things: it
  took 1986 *demo and bootleg* recordings for a 1991 album track, a 2010 original for a 2016 cover by another
  singer credited on the same work, a 1970 album cast for the 1973 film cast, a 2012 original for a 2014 remix.
- Sol's way (the verified audio recording's own earliest release) needs that audio recording first. For a video
  match the obvious swap ("the clean sibling closest in length") picks a remaster on a later reissue (2002 for a
  1986 album track), so alone it does not fix the main error class.
- Release-group dates rescue reissue-only recordings (1963, 1970, 1972, 1974 cases) but are polluted by deluxe
  editions merged into the original group (a 2013 single dated 2012) and by early promo groups.

The rule (the sample's "hybrid"):

1. If the matched recording is itself a version (disambiguation or title: remix, live, cover, cast, dub, bootleg,
   re-recording, "Taylor's Version", instrumental, acoustic, unplugged), TDOR = its own earliest studio release.
   Mixes of the same audio (Atmos, 360, stereo, remaster, clean/explicit, radio edit, "video mix") are not versions.
   DJ-mix segments are swapped for the plain recording like videos.
2. Else the candidates are the work's recordings by the same first artist that are not video, not a version, and
   within ±10 s of the matched length (the same edit). TDOR = the earliest date of an official Album, Single or EP
   release (not compilation, live, DJ-mix, soundtrack or remix group) over them, looked up in first-release order
   until one is found (8 at most).
3. Only if no same-length candidate has one, the same over all clean candidates (`any-clean`, lower confidence).
4. Release dates, not release-group dates; a release-group date earlier than the release date is shown as evidence
   and makes the row "review".
5. No recording reached (no work relationship, nothing studio): no proposal; the file keeps its year.

Confidence: **high** when the rule is `same-length`, the release-group year equals the release year, and the
proposal is *earlier* than the current year (12 of 15 right in the sample, the 3 others within a year); everything
else is **review**. A proposal later than the current year is never applied without review.

## Matching changes (new downloads)

- Done (`mbtag: avoid music-video recordings when matching`): `resolve()` now treats MusicBrainz's `video` flag
  like "(video)" in the title and prefers an audio alternative of the same song already among the candidates.
  In an earlier retag's evidence, 60 matches were video recordings and at least 24 had such an alternative.
- Done (stage C, `mbtag: swap a video match to the work's audio recording`): when no alternative is an audio
  recording, `resolve()` looks the work up (`years.audio_for`) and takes its recording by the same first artist
  that is no video, no DJ-mix segment and no version, within ±10 s of the matched length (the download's length
  when the recording has none), the one with the earliest official Album/Single/EP release first, else the
  earliest first release. A DJ-mix segment gets the same treatment. The row's `swap` evidence records from → to
  and the rule (`alternative`, `work-same-length`, `work-same-length-no-studio`), or the reason the match was kept
  (no work relationship, nothing within 10 s, a failed lookup). Existing library files are not retagged: the
  years review covers them.
- Done: `write_year` writes TDRC = the recording's own first release and TDOR = the rule's result, with
  `DATE_SOURCE` and `DATE_RULE`, instead of the same date twice. A download is not reviewed, so a proposal later
  than the recording's own first release is not taken (8 of 9 later proposals were wrong): TDOR stays that first
  release, rule `first-release`.

## Dry-run report

`musicdb years --dry-run` (or a `years` subcommand of `mbtag`) writes `years-report.json` and a Markdown table to the
data directory, one row per file with a proposal or a suspect class:

| file | current TDRC / TDOR | proposed TDRC / TDOR | rule | confidence | evidence |
|---|---|---|---|---|---|
| relative path | `2000` / `2000` | `2000` / `1983-01-04` | same-length | high | matched `bf8373da…` (video); source `…` on Album "…" 1983-01-04; RG 1983 |

Evidence lists the matched recording (flags, disambiguation), the source recording and release (title, type, date),
the release-group date when it differs, and the class that made the row suspect. Counts per class and confidence
go at the top. Nothing is written in a dry run.

## Review flow

- The report is the review list. Every row starts `undecided` (decided: review every row); `--accept 3 7 12`,
  `--reject 5`, `--undecide 4` record decisions with their time. Row ids are stable: a rerun keeps a file's id (by
  its registry id, else its path) and its decision while the proposal, md5 and current tags are the same; any
  change sends the row back to `undecided`.
- Rows: files whose shown year (TDOR, else TDRC) would change; rows without a proposal (no work relationship: "needs
  MBID"; no MusicBrainz recording in the tags: "needs MBID"; recording not found; nothing studio found); and rows
  whose year holds but whose release group is dated earlier (`keeps-year`, e.g. reissue-only recordings).
- `--mbid ROW MBID` names the recording a file is (until stage B's picker): the row is computed again from it, kept
  across reruns, and needs a new decision. It changes only the year's source, not the file's recording tag.
- rormpc's "Years to review" view (stage B) reads `musicdb years --json` (the report, `version` 1) and writes
  decisions through `--accept` / `--reject` / `--mbid`; the apply step stays in rormpc-tools.

## Writing and undo

- Apply only accepted rows whose audio hash (the md5 `songs.jsonl` and dedupe use, of the audio stream, so a tag
  write keeps it) and date tags are what the report saw; a changed file is skipped and reported.
- Before writing, append the old TDRC, TDOR, `DATE_SOURCE` and `DATE_RULE` of each file to `years-backup.jsonl` in
  the data directory (fsynced), then write the tags to a temporary copy in the same directory and `os.replace` it
  over the original, so MPD never reads a half-written file.
- `--rollback [ID...]` restores the backup values the same way: the newest apply per file not rolled back yet, and
  only while the file still has the values that apply wrote. The rolled-back row goes back to `undecided`.
- After writing, `mpc update` for the touched directories so MPD's `Date` / `OriginalDate` change.

## Rate limits and run time

- MusicBrainz at 1 request/s with the shared User-Agent (`rormpc-tools/… ( contact )`), every response cached
  under the `ytmb` cache; 503 answers back off.
- About 5.8 requests per recording: ~3,600 for the 616 dated, matched files, about 66 minutes; the 20 undated video
  matches and the remaining undated matched files add ~20 minutes. A second run reads only the cache.

## What the views read

- rormpc: Queue Year column and the album browser read `originaldate`, falling back to `date` (the theme's Year column and
  `rormpc_browse.rs`).
- hits (done): `library_songs` reads `originaldate`, falling back to `date`; `--years-of release` and the Year column use
  it. Chart rows not in the library keep `mb_song`'s first release.
- Until those read TDOR, writing only TDOR changes nothing visible; so either the views change first, or TDRC is
  set to the song's year too (see the choices).

## Decisions (2026-10-10, by the user; the options as asked)

Which year do the views show?
Decided: TDOR (original release), TDRC kept per recording.
Options: TDOR (original release), TDRC kept per recording (recommended) | TDRC = TDOR = the original release, as today | TDOR, and TDRC only for versions
Checked: every view reads TDRC today; MPD exposes TDOR as `originaldate`.

May high-confidence rows be applied without row-by-row review?
Decided: no, review every row.
Options: yes, earlier-only same-length rows with matching release-group year (recommended) | no, review every row | yes, every earlier proposal
Checked: 12 of 15 high rows right in the sample, the other 3 off by one year; earlier-only proposals 25 of 28 right or closer.

Where is the review done?
Decided: a rormpc "Years to review" view (stage B; the CLI ids meanwhile).
Options: the Markdown report with `--accept`/`--reject` ids (recommended) | a rormpc "Years to review" view | both, the view later
Checked: an earlier review of undated files used the report-and-ids flow.

Should a video match with no audio alternative be found through the work on download?
Decided: yes, look the work up and swap to the same-length audio recording (stage C).
Options: yes, look the work up and swap to the same-length audio recording (recommended) | send it to review instead | keep the video recording and fix only the year
Checked: at least 24 of 60 earlier video matches had an audio alternative; the rest need a work lookup.

What happens to files without any MusicBrainz reference (no work relationship)?
Decided: ask for an MBID through the picker (stage B; `--mbid ROW MBID` meanwhile).
Options: keep the current year, list only suspect classes (recommended) | clear the year when it is a video recording's | ask for an MBID through the picker
Checked: 46 of 189 sampled recordings have no work relationship.
