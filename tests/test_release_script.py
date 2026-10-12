"""scripts/release.sh in a throwaway repository: a bare repo as origin, a fake pyproject.toml and a stub `uv`.
Nothing here touches this repository or the network."""
import os, pathlib, shutil, subprocess

import pytest

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "release.sh"

pytestmark = pytest.mark.skipif(not (shutil.which("bash") and shutil.which("git")), reason="needs bash and git")


def git(cwd, *args, env):
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path):
    """A clone of a bare origin on main at version 1.2.3, pushed, with a stub uv whose exit code is UV_EXIT."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "uv").write_text('#!/bin/sh\nexit "${UV_EXIT:-0}"\n')
    (bin_dir / "uv").chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "HOME": str(tmp_path),
           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
    origin, work = tmp_path / "origin.git", tmp_path / "work"
    git(tmp_path, "init", "--bare", "-b", "main", str(origin), env=env)
    git(tmp_path, "init", "-b", "main", str(work), env=env)
    (work / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "1.2.3"\n')
    git(work, "add", "pyproject.toml", env=env)
    git(work, "commit", "-m", "1.2.3", env=env)
    git(work, "remote", "add", "origin", str(origin), env=env)
    git(work, "push", "origin", "main", env=env)
    return work, origin, env


def release(repo, version="1.2.3", uv_exit=0):
    work, _, env = repo
    return subprocess.run(["bash", str(SCRIPT), version], cwd=work, env={**env, "UV_EXIT": str(uv_exit)},
                          capture_output=True, text=True)


def refs(cwd, env):
    return sorted(git(cwd, "for-each-ref", "--format=%(refname)", env=env).split())


def local_tags(repo):
    work, _, env = repo
    return git(work, "tag", env=env).split()


def test_bad_version_string(repo):
    for bad in ("1.2", "v1.2.3", "1.2.3-rc1", "1.2.3\n"):
        r = release(repo, bad)
        assert r.returncode != 0 and "not X.Y.Z" in r.stderr
    assert local_tags(repo) == []


def test_dirty_tree(repo):
    work, _, _ = repo
    (work / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "1.2.3"\n# edit\n')
    r = release(repo)
    assert r.returncode != 0 and "uncommitted" in r.stderr
    assert local_tags(repo) == []


def test_untracked_files_are_allowed(repo):
    work, _, _ = repo
    (work / "TODO.md.lock").write_text("")
    r = release(repo)
    assert r.returncode == 0, r.stderr


def test_not_on_main(repo):
    work, _, env = repo
    git(work, "checkout", "-b", "other", env=env)
    r = release(repo)
    assert r.returncode != 0 and "not main" in r.stderr


def test_behind_origin(repo, tmp_path):
    work, origin, env = repo
    other = tmp_path / "other"
    git(tmp_path, "clone", str(origin), str(other), env=env)
    (other / "f").write_text("x")
    git(other, "add", "f", env=env)
    git(other, "commit", "-m", "elsewhere", env=env)
    git(other, "push", "origin", "main", env=env)
    r = release(repo)
    assert r.returncode != 0 and "fast-forward" in r.stderr
    assert local_tags(repo) == []


def test_version_mismatch(repo):
    r = release(repo, "1.2.4")
    assert r.returncode != 0 and "1.2.3, not 1.2.4" in r.stderr
    assert local_tags(repo) == []


def test_existing_local_tag(repo):
    work, _, env = repo
    git(work, "tag", "v1.2.3", env=env)
    r = release(repo)
    assert r.returncode != 0 and "exists locally" in r.stderr


def test_existing_remote_tag(repo):
    work, origin, env = repo
    git(work, "tag", "v1.2.3", env=env)
    git(work, "push", "origin", "refs/tags/v1.2.3", env=env)
    git(work, "tag", "-d", "v1.2.3", env=env)
    r = release(repo)
    assert r.returncode != 0 and "exists on origin" in r.stderr
    assert local_tags(repo) == []


def test_failing_tests_create_no_tag(repo):
    work, origin, env = repo
    r = release(repo, uv_exit=1)
    assert r.returncode != 0 and "tests failed" in r.stderr
    assert local_tags(repo) == []
    assert refs(origin, env) == ["refs/heads/main"]


def test_success_pushes_only_main_and_the_tag(repo):
    work, origin, env = repo
    # an unrelated local branch and tag must stay local
    git(work, "branch", "side", env=env)
    git(work, "tag", "unrelated", env=env)
    (work / "f").write_text("release commit")
    git(work, "add", "f", env=env)
    git(work, "commit", "-m", "release commit", env=env)
    r = release(repo)
    assert r.returncode == 0, r.stderr
    assert "landed v1.2.3" in r.stdout
    assert refs(origin, env) == ["refs/heads/main", "refs/tags/v1.2.3"]
    head = git(work, "rev-parse", "HEAD", env=env).strip()
    assert git(origin, "rev-parse", "main", env=env).strip() == head
    assert git(origin, "rev-parse", "v1.2.3^{commit}", env=env).strip() == head
    assert git(origin, "cat-file", "-t", "v1.2.3", env=env).strip() == "tag"  # annotated


def test_failing_push_removes_local_tag(repo):
    work, origin, env = repo
    hook = origin / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    r = release(repo)
    assert r.returncode != 0 and "push failed" in r.stderr
    assert local_tags(repo) == []
    assert refs(origin, env) == ["refs/heads/main"]
