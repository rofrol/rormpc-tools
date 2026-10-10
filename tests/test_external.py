"""A missing external program ends a command with one readable line naming it and pointing at the dependency table."""
import pathlib, subprocess, sys

import pytest

from rormpc_tools import dedupe, external, hits, liveplaylist, musicdb, yt_mp3_mb


@pytest.fixture
def no_programs(tmp_path, monkeypatch):
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    monkeypatch.setattr(sys, "argv", ["/usr/local/bin/musicdb", "update"])


def test_missing_ffmpeg_in_a_worker_thread(no_programs, tmp_path, monkeypatch):
    # musicdb update hashes audio with ffmpeg in a thread pool: the error crosses future.result()
    music = tmp_path / "music"
    music.mkdir()
    (music / "a.mp3").write_bytes(b"x")
    monkeypatch.setattr(musicdb, "MUSIC", music)
    monkeypatch.setattr(dedupe, "CACHE", tmp_path / "hash-cache.json")
    with pytest.raises(SystemExit) as e:
        external.cli(lambda: dedupe.hashes(["a.mp3"]))()
    assert e.value.code == ("musicdb: ffmpeg not found on PATH; audio hashes, covers and mp3 conversion need it. "
                            "Install: https://github.com/rofrol/rormpc-tools#dependencies")


@pytest.mark.parametrize("program", ["yt-dlp", "fpcalc", "mpc", "git"])
def test_missing_program_points_at_the_dependency_table(no_programs, program):
    with pytest.raises(SystemExit) as e:
        external.cli(lambda: subprocess.run([program, "--version"]))()
    assert e.value.code.startswith(f"musicdb: {program} not found on PATH; ")
    assert e.value.code.endswith(f". Install: {external.INSTALL}")
    assert "brew" not in e.value.code and "apt" not in e.value.code


def test_every_program_is_in_the_readme_table():
    readme = (pathlib.Path(__file__).parent.parent / "README.md").read_text()
    assert "\n## Dependencies\n" in readme  # the #dependencies anchor of external.INSTALL
    for program in external.PURPOSE:
        assert f"| `{program}` |" in readme


def test_missing_optional_program_says_once_what_stops_working(no_programs, tmp_path, capsys, monkeypatch):
    external.optional.cache_clear()
    monkeypatch.setattr(sys, "argv", ["/usr/local/bin/yt-mp3-mb"])
    song = tmp_path / "a.mp3"
    yt_mp3_mb.replaygain(song)
    yt_mp3_mb.replaygain(song)
    assert capsys.readouterr().err == ("yt-mp3-mb: rsgain not found on PATH; without it downloads get no ReplayGain "
                                       f"tags. Install: {external.INSTALL}\n")
    external.optional.cache_clear()


def test_missing_file_is_not_a_missing_program(tmp_path):
    def main():
        (tmp_path / "git").read_text()  # a data file that happens to share a program's name
    with pytest.raises(FileNotFoundError):
        external.cli(main)()


def test_entry_points_are_wrapped():
    for main in (musicdb.main, hits.main, yt_mp3_mb.main, liveplaylist.main):
        assert main.__wrapped__ and main.__module__.startswith("rormpc_tools.")
