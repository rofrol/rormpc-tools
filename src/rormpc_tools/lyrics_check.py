"""Flags for synced lyrics timed for another cut of the song: an LRCLIB entry is flagged for checking, never rejected.

Reasons, each recorded with its numbers (seconds, after the [offset:] tag):
  past-end    a non-empty line starts after the file's length + PAST_END_SLACK
  unordered   a line starts before the line above it (a multi-timestamp line [00:10.00][01:20.00]text is expanded:
              its own stamps must increase, and its first stamp counts for the file order, as compressed LRC files
              list a repeated line once, where it first appears)
  ends-early  tail = length - the last non-empty line's start > clamp(EARLY_SHARE x length, EARLY_MIN, EARLY_MAX)
Supporting evidence only, never a flag alone, added to an entry that already has a flag: with two or more other synced
LRCLIB candidates of the same cut (within lyrics.SLACK of the file's length) whose last lines agree within
AGREE seconds, this entry's last line is more than max(DISAGREE_MIN, DISAGREE_SHARE x length) away from theirs. Used
only when the candidates are already at hand (fetch, candidates); nothing fetches them just for this.

The [offset:] tag is in milliseconds, LRC convention: a positive offset shows the lyrics earlier (time - offset).
"""
import re

# provisional thresholds (coordinator's decision of 2026-10-12, t-fb4wapwk), to calibrate over the library
PAST_END_SLACK = 2.0   # seconds: LRCLIB's own matching tolerance
EARLY_SHARE = 0.20     # of the length: the tail above which the lyrics end early...
EARLY_MIN = 25.0       # ...but never below 25 s (short songs)...
EARLY_MAX = 90.0       # ...nor above 90 s (long songs)
AGREE = 5.0            # seconds: other candidates' last lines this close agree
DISAGREE_MIN = 20.0    # seconds and...
DISAGREE_SHARE = 0.10  # ...share of the length: this entry's last line further from theirs is supporting evidence

LABEL = "lyrics end early, check timing"
LABELS = {"past-end": "lyrics run past the end", "unordered": "lyrics out of order",
          "ends-early": "lyrics end early", "candidates": "other entries end elsewhere"}

_STAMP = re.compile(r"\[(\d+):(\d+(?:\.\d+)?)\]")
_OFFSET = re.compile(r"^\s*\[offset:\s*([+-]?\d+)\s*\]", re.I | re.M)


def parse(text):
    """[(start in seconds after the offset, text, first stamp of its line?)] in file order, multi-timestamp lines
    expanded."""
    m = _OFFSET.search(text)
    shift = int(m.group(1)) / 1000 if m else 0.0
    out = []
    for line in text.splitlines():
        s, stamps = line.strip(), []
        while (t := _STAMP.match(s)):
            stamps.append(int(t.group(1)) * 60 + float(t.group(2)) - shift)
            s = s[t.end():]
        for i, at in enumerate(stamps):
            out.append((at, s.strip(), i == 0))
    return out


def last_line(text):
    """The latest start of a non-empty line, or None."""
    starts = [at for at, words, _ in parse(text) if words]
    return max(starts) if starts else None


def r(x):
    return round(x, 1)


def reasons(text, length, others=()):
    """Flag reasons of a synced .lrc for a file of `length` seconds (0: unknown, only `unordered` is checked);
    `others` are the last-line starts of other synced candidates of the same cut (supporting evidence)."""
    lines, out = parse(text), []
    sung = [at for at, words, _ in lines if words]
    if length and sung and max(sung) > length + PAST_END_SLACK:
        out.append({"reason": "past-end", "line": r(max(sung)), "length": r(length),
                    "over": r(max(sung) - length), "lines": sum(at > length + PAST_END_SLACK for at in sung)})
    # a line's first stamp follows the first stamp of the line above; its later stamps follow the one before them
    back, line_prev, line_first = [], None, None
    for at, _words, first in lines:
        if first:
            if line_first is not None and at < line_first:
                back.append((line_first, at))
            line_first = line_prev = at
        else:
            if at < line_prev:
                back.append((line_prev, at))
            line_prev = at
    if back:
        out.append({"reason": "unordered", "count": len(back), "at": r(back[0][0]), "then": r(back[0][1])})
    if length and sung:
        tail, limit = length - max(sung), min(max(EARLY_SHARE * length, EARLY_MIN), EARLY_MAX)
        if tail > limit:
            out.append({"reason": "ends-early", "tail": r(tail), "limit": r(limit), "last_line": r(max(sung)),
                        "length": r(length)})
    if out and sung and (ev := evidence(max(sung), length, others)):
        out.append(ev)
    for rec in out:
        rec["label"] = LABELS[rec["reason"]]
    return out


def evidence(last, length, others):
    """Supporting evidence: the largest group of other candidates' last lines within AGREE seconds, two or more,
    whose median is further than max(DISAGREE_MIN, DISAGREE_SHARE x length) from this entry's last line."""
    xs, group = sorted(others), []
    for i in range(len(xs)):
        g = [x for x in xs[i:] if x - xs[i] <= AGREE]
        if len(g) > len(group):
            group = g
    if len(group) < 2:
        return None
    ref = group[len(group) // 2] if len(group) % 2 else (group[len(group) // 2 - 1] + group[len(group) // 2]) / 2
    limit = max(DISAGREE_MIN, DISAGREE_SHARE * (length or 0))
    if abs(last - ref) <= limit:
        return None
    return {"reason": "candidates", "supporting": True, "others": len(group), "their_last": r(ref),
            "this_last": r(last), "delta": r(last - ref), "limit": r(limit)}


def flagged(recs):
    """The flag reasons among a record's suspect list (supporting evidence left out)."""
    return [x["reason"] for x in recs or () if not x.get("supporting")]
