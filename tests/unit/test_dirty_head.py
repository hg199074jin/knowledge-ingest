"""Dirty-tree 修订指纹（v0.3 冻结规格 11）。

worktree_fingerprint = sha256( `git diff --binary HEAD` 输出
    + sorted( untracked_relative_path + sha256(untracked_file_bytes) ) )，
untracked 用 `git ls-files --others --exclude-standard` 发现。

MediaAdapter.head() / DocchunkAdapter.head()：
- clean → 纯 HEAD（与 v0.2 完全一致，缓存零失效）
- dirty（diff 非空或有 untracked）→ `HEAD-dirty-<完整 fingerprint hex>`
- dirty 状态禁止任何 legacy fallback（不得回退纯 HEAD）
"""

import re
import subprocess
from pathlib import Path

import pytest

from knowledge_ingest.adapters.docchunk import DocchunkAdapter
from knowledge_ingest.adapters.media import MediaAdapter
from knowledge_ingest.runner import worktree_revision

DIRTY_RE = re.compile(r"^HEAD-dirty-[0-9a-f]{64}$")


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=repo, check=True,
                            capture_output=True, text=True)
    return result.stdout.strip()


@pytest.fixture()
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "adapter-repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "test")
    (repo / "tracked.txt").write_text("v1\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "init")
    return repo


def head_sha(repo: Path) -> str:
    return git(repo, "rev-parse", "HEAD")


def test_clean_repo_returns_pure_head(git_repo: Path):
    assert worktree_revision(git_repo) == head_sha(git_repo)


def test_media_adapter_head_clean_returns_head(git_repo: Path):
    adapter = MediaAdapter(project=git_repo, output_root=git_repo / "out")
    assert adapter.head() == head_sha(git_repo)


def test_docchunk_adapter_head_clean_returns_head(git_repo: Path):
    adapter = DocchunkAdapter(project=git_repo)
    assert adapter.head() == head_sha(git_repo)


def test_modified_tracked_file_returns_dirty_head(git_repo: Path):
    (git_repo / "tracked.txt").write_text("v2\n", encoding="utf-8")
    revision = worktree_revision(git_repo)
    assert DIRTY_RE.match(revision), revision
    assert revision != head_sha(git_repo)


def test_untracked_file_returns_dirty_head(git_repo: Path):
    (git_repo / "extra.txt").write_bytes(b"untracked")
    revision = worktree_revision(git_repo)
    assert DIRTY_RE.match(revision), revision
    assert revision != head_sha(git_repo)


def test_media_adapter_head_dirty(git_repo: Path):
    (git_repo / "tracked.txt").write_text("dirty\n", encoding="utf-8")
    adapter = MediaAdapter(project=git_repo, output_root=git_repo / "out")
    assert DIRTY_RE.match(adapter.head()), adapter.head()


def test_docchunk_adapter_head_dirty(git_repo: Path):
    (git_repo / "tracked.txt").write_text("dirty\n", encoding="utf-8")
    adapter = DocchunkAdapter(project=git_repo)
    assert DIRTY_RE.match(adapter.head()), adapter.head()


def test_same_dirty_content_same_key(git_repo: Path):
    (git_repo / "tracked.txt").write_text("same-dirty\n", encoding="utf-8")
    (git_repo / "u1.txt").write_bytes(b"u")
    first = worktree_revision(git_repo)
    second = worktree_revision(git_repo)
    assert first == second
    assert DIRTY_RE.match(first)


def test_different_untracked_content_different_key(git_repo: Path):
    (git_repo / "u.txt").write_bytes(b"content-A")
    key_a = worktree_revision(git_repo)
    (git_repo / "u.txt").write_bytes(b"content-B")
    key_b = worktree_revision(git_repo)
    assert key_a != key_b
    assert DIRTY_RE.match(key_a) and DIRTY_RE.match(key_b)


def test_different_tracked_diff_different_key(git_repo: Path):
    (git_repo / "tracked.txt").write_text("one\n", encoding="utf-8")
    key_a = worktree_revision(git_repo)
    (git_repo / "tracked.txt").write_text("two\n", encoding="utf-8")
    key_b = worktree_revision(git_repo)
    assert key_a != key_b


def test_commit_restores_clean_head(git_repo: Path):
    (git_repo / "tracked.txt").write_text("committed-later\n", encoding="utf-8")
    dirty = worktree_revision(git_repo)
    assert DIRTY_RE.match(dirty)
    git(git_repo, "add", ".")
    git(git_repo, "commit", "-q", "-m", "second")
    assert worktree_revision(git_repo) == head_sha(git_repo)
    assert worktree_revision(git_repo) != dirty


def test_ignored_files_do_not_make_worktree_dirty(git_repo: Path):
    (git_repo / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    git(git_repo, "add", ".")
    git(git_repo, "commit", "-q", "-m", "gitignore")
    base = worktree_revision(git_repo)
    assert base == head_sha(git_repo)
    (git_repo / "ignored.txt").write_bytes(b"noise")
    assert worktree_revision(git_repo) == base


def test_staged_new_file_returns_dirty_head(git_repo: Path):
    (git_repo / "staged.txt").write_bytes(b"staged")
    git(git_repo, "add", "staged.txt")
    revision = worktree_revision(git_repo)
    assert DIRTY_RE.match(revision), revision
    assert revision != head_sha(git_repo)
