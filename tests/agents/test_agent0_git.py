import pytest
import os
import subprocess
import shutil
from pathlib import Path
from src.agents.agent0_git import GitOrchestrator


@pytest.fixture
def git_repo(tmp_path):
    repo_dir = tmp_path / "test_repo"
    repo_dir.mkdir()

    # Initialize git repo
    subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=repo_dir, check=True
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"], cwd=repo_dir, check=True
    )

    # Create initial commit
    test_file = repo_dir / "file.txt"
    test_file.write_text("initial content\n")
    subprocess.run(["git", "add", "file.txt"], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=repo_dir, check=True)

    return repo_dir


def test_git_orchestrator_is_clean(git_repo):
    orchestrator = GitOrchestrator(str(git_repo))
    assert orchestrator.is_clean() is True

    # Make it dirty
    (git_repo / "file.txt").write_text("modified content\n")
    assert orchestrator.is_clean() is False


def test_git_orchestrator_create_worktree(git_repo):
    orchestrator = GitOrchestrator(str(git_repo))

    # Create a new branch
    subprocess.run(["git", "branch", "feature-branch"], cwd=git_repo, check=True)

    wt_dir = str(git_repo / "wt_feature")
    created_dir = orchestrator.create_worktree("feature-branch", wt_dir)

    assert os.path.exists(created_dir)
    assert os.path.exists(os.path.join(created_dir, "file.txt"))

    orchestrator.remove_worktree(created_dir)
    assert not os.path.exists(created_dir)


def test_git_orchestrator_patch_extraction(git_repo):
    # Modify and commit
    (git_repo / "file.txt").write_text("initial content\nnew line\n")
    subprocess.run(["git", "commit", "-am", "second commit"], cwd=git_repo, check=True)

    # Get hash
    res = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=git_repo,
        check=True,
        capture_output=True,
        text=True,
    )
    commit_hash = res.stdout.strip()

    orchestrator = GitOrchestrator(str(git_repo))
    patch = orchestrator.get_patch_from_commit(commit_hash)

    assert "new line" in patch
    assert "Subject: [PATCH] second commit" in patch
