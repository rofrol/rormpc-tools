"""External programs the tools run, and one readable line instead of a traceback when one is missing.

subprocess raises FileNotFoundError with the program as `filename` when it is not on PATH; `cli` turns that into
"<tool>: <program> not found on PATH; <what needs it>. Install: <README's dependency table>" and exit 1.
Optional programs (fpcalc, rsgain) are checked with shutil.which where they are used; `optional` says once what
stops working. Package names per package manager live only in README.md's table, not here.
"""
import functools, os, sys

INSTALL = "https://github.com/rofrol/rormpc-tools#dependencies"

# program: what needs it (required) or what stops working without it (optional)
PURPOSE = {
    "ffmpeg": "audio hashes, covers and mp3 conversion need it",
    "yt-dlp": "YouTube searches and downloads need it",
    "mpc": "MPD database updates and the current song need it",
    "git": "the history directory's commits need it",
    "fpcalc": "without it AcoustID matching and the Versions audio comparison stop working",
    "rsgain": "without it downloads get no ReplayGain tags",
}


def tool():
    """The running command's name (musicdb, hits, yt-mp3-mb, ...), or None."""
    return os.path.basename(sys.argv[0]).removesuffix(".py") or None


def missing(program, tool=None):
    """The one-line message for a missing program, or None when it is not one of PURPOSE."""
    purpose = PURPOSE.get(program)  # a bare name, as the tools pass it; a path is a missing file, not a program
    if not purpose:
        return None
    prefix = f"{tool}: " if tool else ""
    return f"{prefix}{program} not found on PATH; {purpose}. Install: {INSTALL}"


@functools.cache
def optional(program):
    """Say once per run on stderr that an optional program is missing and what stops working."""
    print(missing(program, tool()), file=sys.stderr)


def cli(main):
    """Wrap a console entry point: a missing external program exits with `missing`'s line, not a traceback."""
    @functools.wraps(main)
    def run(*args, **kwargs):
        try:
            return main(*args, **kwargs)
        except FileNotFoundError as e:
            msg = missing(e.filename, tool())
            if not msg:
                raise
            sys.exit(msg)
    return run
