#!/usr/bin/env bash
# Tag and push a release: scripts/release.sh X.Y.Z, run on main after the version-bump commit.
# It refuses unless everything is in place, gates the tag on the tests, and pushes only main and that tag.
set -euo pipefail

die() { echo "release: $*" >&2; exit 1; }

[[ $# -eq 1 ]] || die "usage: scripts/release.sh X.Y.Z"
version=$1
[[ $version =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "'$version' is not X.Y.Z"
tag=v$version

cd "$(git rev-parse --show-toplevel)"

branch=$(git symbolic-ref --quiet --short HEAD) || die "HEAD is detached; check out main"
[[ $branch == main ]] || die "on branch '$branch', not main"
[[ -z $(git status --porcelain --untracked-files=no) ]] || die "the worktree has uncommitted changes"

git fetch origin main || die "git fetch origin main failed"
git merge-base --is-ancestor origin/main HEAD || die "main is not a fast-forward of origin/main"

project_version=$(python3 -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])')
[[ $project_version == "$version" ]] || die "pyproject.toml has version $project_version, not $version"

git rev-parse --quiet --verify "refs/tags/$tag" >/dev/null && die "tag $tag exists locally"
remote_tag=$(git ls-remote --tags origin "refs/tags/$tag") || die "git ls-remote origin failed"
[[ -z $remote_tag ]] || die "tag $tag exists on origin"

uv run pytest -q || die "tests failed; no tag created"

git tag -a "$tag" -m "$tag"
if ! git push origin main "refs/tags/$tag"; then
  git tag -d "$tag" >/dev/null
  die "push failed; deleted the local tag $tag so a rerun starts clean"
fi

echo "release: tagged $(git rev-parse --short HEAD) as $tag and pushed main and $tag to origin"
echo "next: bump RORMPC_TOOLS_TAG to $tag in rormpc's scripts/rormpc_install.sh, then tell todo-rormpc \"landed $tag\""
