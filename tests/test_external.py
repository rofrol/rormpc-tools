"""A missing external program ends a command with one readable line naming it and how to install it."""
import subprocess, sys

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
    assert e.value.code == ("musicdb: ffmpeg is not installed: brew install ffmpeg (macOS) "
                            "or sudo apt install ffmpeg (Debian/Ubuntu)")


@pytest.mark.parametrize("program, brew, apt", [("yt-dlp", "yt-dlp", "yt-dlp"),
                                                ("fpcalc", "chromaprint", "libchromaprint-tools"),
                                                ("mpc", "mpc", "mpc")])
def test_missing_program_names_its_packages(no_programs, program, brew, apt):
    with pytest.raises(SystemExit) as e:
        external.cli(lambda: subprocess.run([program, "--version"]))()
    assert e.value.code == (f"musicdb: {program} is not installed: brew install {brew} (macOS) "
                            f"or sudo apt install {apt} (Debian/Ubuntu)")


def test_missing_file_is_not_a_missing_program(tmp_path):
    def main():
        (tmp_path / "git").read_text()  # a data file that happens to share a program's name
    with pytest.raises(FileNotFoundError):
        external.cli(main)()


def test_entry_points_are_wrapped():
    for main in (musicdb.main, hits.main, yt_mp3_mb.main, liveplaylist.main):
        assert main.__wrapped__ and main.__module__.startswith("rormpc_tools.")
