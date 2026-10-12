# /// script
# requires-python = ">=3.11,<3.13"
# dependencies = [
#     "numpy",
#     "torch==2.5.1",
#     "torchaudio==2.5.1",
#     "demucs==4.0.1",
#     "silero-vad>=5.1",
#     "mlx-whisper>=0.4; sys_platform == 'darwin' and platform_machine == 'arm64'",
# ]
# ///
"""The heavy half of `musicdb lyrics transcribe`: one song's audio in, one JSON document out (stdout).

Run through `uv run --script` so torch, demucs, Silero VAD and the Whisper backend live in this script's own
environment and never in rormpc-tools'. rormpc-tools (transcribe.py) decides which songs qualify, passes the
thresholds, judges the result and writes the sidecar; this script only computes. Progress goes to stderr.

  transcribe_backend.py check                 # the environment imports: exit 0 (no network, no model download)
  transcribe_backend.py prepare               # download the models once (htdemucs, Silero, Whisper)
  transcribe_backend.py run --audio FILE --key HEX --cache-dir DIR [--language CODE] [--model REPO]
                            --vad-threshold P --vad-min-speech-ms MS --vad-min-silence-ms MS
                            --min-speech-seconds S --min-speech-ratio R --lang-windows N

Pipeline of `run` (docs/transcribe-lyrics-plan.md, "Pipeline per song"):
  1. decode with ffmpeg at 44.1 kHz stereo (demucs' rate);
  2. demucs htdemucs, vocals stem, on CPU (MPS fails on htdemucs), cached as <cache-dir>/<key>.npy (16 kHz mono);
  3. Silero VAD on the stem; below the minimum singing the output says skipped "no_vocals" and Whisper never runs;
  4. language: --language when given, else a majority vote of Whisper's detection over 30 s windows that start at
     VAD speech regions;
  5. Whisper on the stem: task transcribe (never translate), condition_on_previous_text False, word timestamps,
     hallucination_silence_threshold 2.

Output: {"backend", "model", "versions", "duration", "vad": {"regions": [[start, end], ...], "speech_seconds"},
"language": {"code", "how": "given" | "vote", "votes", "probabilities"}, "skipped": null | "no_vocals",
"segments": [{"start", "end", "text", "words": [{"word", "start", "end", "probability"}]}]}.

Backends: macOS on Apple Silicon uses mlx-whisper. Linux (faster-whisper, CTranslate2) is a later step: add a class
with the same `detect` and `transcribe` methods to ASR_BACKENDS and its dependency above.
"""
import argparse, importlib.metadata, json, os, pathlib, platform, subprocess, sys, tempfile

SAMPLE_RATE = 16000  # Whisper's and Silero's rate
DEMUCS_RATE = 44100
WINDOW = 30  # seconds: Whisper detects the language from one 30 s window
DEFAULT_MODEL = "mlx-community/whisper-large-v3"


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def decode(path, rate, channels):
    """float32 samples, shape (channels, n), through ffmpeg."""
    import numpy as np
    out = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-f", "f32le", "-ac", str(channels),
                          "-ar", str(rate), "-"], capture_output=True, check=True).stdout
    return np.frombuffer(out, dtype=np.float32).reshape(-1, channels).T.copy()


def separate_vocals(path, cache_dir, key):
    """The vocal stem as 16 kHz mono float32, from the cache when this audio was separated before."""
    import numpy as np
    cached = pathlib.Path(cache_dir) / f"{key}.npy"
    if cached.exists():
        log(f"vocals: cached {cached.name}")
        return np.load(cached)
    import torch, torchaudio
    from demucs.apply import apply_model
    from demucs.pretrained import get_model
    log("vocals: demucs htdemucs on CPU (about 2 min)")
    model = get_model("htdemucs")
    model.cpu().eval()
    wav = torch.from_numpy(decode(path, DEMUCS_RATE, 2))
    ref = wav.mean(0)
    mean, std = ref.mean(), ref.std() + 1e-8
    with torch.no_grad():
        sources = apply_model(model, ((wav - mean) / std)[None], device="cpu", split=True, overlap=0.25,
                              progress=False)[0]
    vocals = sources[model.sources.index("vocals")] * std + mean
    mono = torchaudio.functional.resample(vocals.mean(0), DEMUCS_RATE, SAMPLE_RATE).numpy().astype(np.float32)
    cached.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=cached.parent, suffix=".npy.tmp")
    with os.fdopen(fd, "wb") as f:
        np.save(f, mono)
    os.replace(tmp, cached)
    return mono


def speech_regions(vocals, threshold, min_speech_ms, min_silence_ms):
    """Silero VAD speech regions of the vocal stem, in seconds."""
    import torch
    from silero_vad import get_speech_timestamps, load_silero_vad
    stamps = get_speech_timestamps(torch.from_numpy(vocals), load_silero_vad(), threshold=threshold,
                                   sampling_rate=SAMPLE_RATE, min_speech_duration_ms=min_speech_ms,
                                   min_silence_duration_ms=min_silence_ms, return_seconds=True)
    return [[float(s["start"]), float(s["end"])] for s in stamps]


def window_starts(regions, count):
    """Starts of up to `count` non-overlapping 30 s windows, each at a speech region's start, spread over the song."""
    starts = []
    for start, _end in regions:
        if not starts or start >= starts[-1] + WINDOW:
            starts.append(start)
    if len(starts) <= count:
        return starts
    step = len(starts) / count
    return [starts[int(i * step)] for i in range(count)]


class MlxWhisper:
    name = "mlx-whisper"

    def __init__(self, model):
        import mlx.core as mx
        from mlx_whisper.transcribe import ModelHolder
        self.repo, self.mx = model, mx
        self.model = ModelHolder.get_model(model, mx.float16)

    def detect(self, chunk):
        """{language: probability} for one window of 16 kHz samples."""
        from mlx_whisper.audio import N_FRAMES, N_SAMPLES, log_mel_spectrogram, pad_or_trim
        mel = log_mel_spectrogram(chunk, n_mels=self.model.dims.n_mels, padding=N_SAMPLES)
        _tokens, probs = self.model.detect_language(pad_or_trim(mel, N_FRAMES, axis=-2).astype(self.mx.float16))
        return dict(probs)

    def transcribe(self, vocals, language):
        import mlx_whisper
        out = mlx_whisper.transcribe(vocals, path_or_hf_repo=self.repo, language=language, task="transcribe",
                                     condition_on_previous_text=False, word_timestamps=True,
                                     hallucination_silence_threshold=2, verbose=None)
        return [{"start": s["start"], "end": s["end"], "text": s["text"],
                 "words": [{"word": w["word"], "start": w["start"], "end": w["end"],
                            "probability": w.get("probability")} for w in s.get("words", [])]}
                for s in out["segments"]]


# platform -> backend class; Linux's faster-whisper (with `hotwords` from the artist and title) goes here later
ASR_BACKENDS = {"darwin-arm64": MlxWhisper}


def asr_backend(model):
    key = f"{sys.platform}-{platform.machine()}"
    cls = ASR_BACKENDS.get(key)
    if not cls:
        sys.exit(f"no Whisper backend for {key} yet (macOS on Apple Silicon only; faster-whisper is planned)")
    return cls(model)


def versions():
    out = {"python": platform.python_version()}
    for pkg in ("torch", "torchaudio", "demucs", "silero-vad", "mlx-whisper", "mlx"):
        try:
            out[pkg] = importlib.metadata.version(pkg)
        except importlib.metadata.PackageNotFoundError:
            pass
    return out


def vote(asr, vocals, regions, count):
    votes, total = {}, {}
    for start in window_starts(regions, count):
        a = int(start * SAMPLE_RATE)
        probs = asr.detect(vocals[a:a + WINDOW * SAMPLE_RATE])
        best = max(probs, key=probs.get)
        votes[best] = votes.get(best, 0) + 1
        for lang, p in probs.items():
            total[lang] = total.get(lang, 0.0) + p
    if not votes:
        return None, votes, {}
    code = max(votes, key=lambda l: (votes[l], total.get(l, 0.0)))  # ties: the higher summed probability
    top = dict(sorted(total.items(), key=lambda kv: -kv[1])[:5])
    return code, votes, {k: round(v, 3) for k, v in top.items()}


def cmd_run(a):
    vocals = separate_vocals(a.audio, a.cache_dir, a.key)
    duration = len(vocals) / SAMPLE_RATE
    regions = speech_regions(vocals, a.vad_threshold, a.vad_min_speech_ms, a.vad_min_silence_ms)
    speech = sum(e - s for s, e in regions)
    out = {"backend": None, "model": a.model, "versions": versions(), "duration": round(duration, 3),
           "vad": {"regions": [[round(s, 3), round(e, 3)] for s, e in regions], "speech_seconds": round(speech, 3)},
           "language": None, "skipped": None, "segments": []}
    if speech < a.min_speech_seconds or (duration and speech / duration < a.min_speech_ratio):
        out["skipped"] = "no_vocals"
        log(f"no vocals: {speech:.1f} s of singing in {duration:.0f} s")
        return out
    asr = asr_backend(a.model)
    out["backend"] = asr.name
    if a.language:
        out["language"] = {"code": a.language, "how": "given"}
    else:
        code, votes, probs = vote(asr, vocals, regions, a.lang_windows)
        out["language"] = {"code": code, "how": "vote", "votes": votes, "probabilities": probs}
    log(f"whisper: {a.model}, language {out['language']['code']}")
    out["segments"] = asr.transcribe(vocals, out["language"]["code"])
    return out


def cmd_check(_a):
    import importlib
    for mod in ("demucs", "mlx_whisper", "silero_vad", "torch", "torchaudio"):
        importlib.import_module(mod)
    return {"ok": True, "versions": versions()}


def cmd_prepare(a):
    from demucs.pretrained import get_model
    from silero_vad import load_silero_vad
    get_model("htdemucs")
    load_silero_vad()
    asr_backend(a.model)
    return {"ok": True, "versions": versions()}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    sp.add_parser("check").set_defaults(fn=cmd_check)
    p = sp.add_parser("prepare")
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.set_defaults(fn=cmd_prepare)
    p = sp.add_parser("run")
    p.add_argument("--audio", required=True)
    p.add_argument("--key", required=True)
    p.add_argument("--cache-dir", required=True)
    p.add_argument("--language")
    p.add_argument("--model", default=DEFAULT_MODEL)
    for name, kind in (("vad-threshold", float), ("vad-min-speech-ms", int), ("vad-min-silence-ms", int),
                       ("min-speech-seconds", float), ("min-speech-ratio", float), ("lang-windows", int)):
        p.add_argument(f"--{name}", type=kind, required=True)
    p.set_defaults(fn=cmd_run)
    a = ap.parse_args()
    print(json.dumps(a.fn(a), ensure_ascii=False))


if __name__ == "__main__":
    main()
