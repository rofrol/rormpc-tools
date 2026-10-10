"""External programs the tools run, and one readable line instead of a traceback when one is missing.

subprocess raises FileNotFoundError with the program as `filename` when it is not on PATH; `cli` turns that into
"<tool>: <program> is not installed: brew install ... (macOS) or sudo apt install ... (Debian/Ubuntu)" and exit 1.
Optional programs (fpcalc, rsgain, terminal-notifier) are checked with shutil.which where they are used.
"""
import functools, os, sys

# program: (Homebrew formula, Debian/Ubuntu package)
PACKAGES = {
    "ffmpeg": ("ffmpeg", "ffmpeg"),
    "ffprobe": ("ffmpeg", "ffmpeg"),
    "fpcalc": ("chromaprint", "libchromaprint-tools"),
    "yt-dlp": ("yt-dlp", "yt-dlp"),
    "mpc": ("mpc", "mpc"),
    "rsgain": ("rsgain", "rsgain"),
    "git": ("git", "git"),
}


def missing(program, tool=None):
    """The one-line message for a missing program, or None when it is not one of PACKAGES."""
    pkg = PACKAGES.get(program)  # a bare name, as the tools pass it; a path is a missing file, not a program
    if not pkg:
        return None
    prefix = f"{tool}: " if tool else ""
    return (f"{prefix}{program} is not installed: brew install {pkg[0]} (macOS) "
            f"or sudo apt install {pkg[1]} (Debian/Ubuntu)")


def cli(main):
    """Wrap a console entry point: a missing external program exits with `missing`'s line, not a traceback."""
    @functools.wraps(main)
    def run(*args, **kwargs):
        try:
            return main(*args, **kwargs)
        except FileNotFoundError as e:
            msg = missing(e.filename, os.path.basename(sys.argv[0]).removesuffix(".py") or None)
            if not msg:
                raise
            sys.exit(msg)
    return run
