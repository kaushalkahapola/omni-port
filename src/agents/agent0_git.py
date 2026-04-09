import os
import subprocess
from typing import Optional, Dict, Any


class GitOrchestrator:
    """
    Agent 0 - Git Orchestrator (No LLM)
    Manages branch checkouts, git worktree isolation, patch extraction.
    Maintains clean/dirty state. Fails closed on bash operations.
    """

    def __init__(self, repo_path: str):
        self.repo_path = os.path.abspath(repo_path)

    def _run_git(self, *args, check=True) -> str:
        try:
            cmd = ["git", "-C", self.repo_path] + list(args)
            result = subprocess.run(cmd, capture_output=True, text=True, check=check)
            return result.stdout.strip()
        except subprocess.CalledProcessError as e:
            raise RuntimeError(
                f"Git command failed: {' '.join(cmd)}\nError: {e.stderr}"
            )

    def create_worktree(self, branch: str, worktree_dir: str) -> str:
        """
        Creates a new git worktree for isolated operations.
        """
        self._run_git("worktree", "add", "-f", worktree_dir, branch)
        return worktree_dir

    def remove_worktree(self, worktree_dir: str):
        """
        Removes an isolated worktree.
        """
        self._run_git("worktree", "remove", "-f", worktree_dir)

    def is_clean(self) -> bool:
        """
        Checks if the main repository is clean.
        """
        status = self._run_git("status", "--porcelain")
        return len(status) == 0

    def get_patch_from_commit(self, commit_hash: str) -> str:
        """
        Extracts patch diff from a given commit hash.
        """
        return self._run_git("format-patch", "-1", commit_hash, "--stdout")

    def apply_patch(self, patch_path: str, directory: Optional[str] = None) -> bool:
        """
        Applies a patch to the repository or worktree.
        """
        target_dir = directory or self.repo_path
        cmd = ["git", "-C", target_dir, "apply", patch_path]
        try:
            subprocess.run(cmd, check=True, capture_output=True, text=True)
            return True
        except subprocess.CalledProcessError as e:
            return False
