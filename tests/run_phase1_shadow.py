import os
import sys
import subprocess

# Add root to pythonpath
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.core.state import BackportState
from src.agents.agent1_classifier import classify_patch
from src.agents.agent2_localizer import localize_hunks
from src.tools.patch_parser import parse_unified_diff


def run_evaluation():
    repo_path = "repos/elasticsearch"
    if not os.path.exists(repo_path):
        print(f"Error: target repo {repo_path} does not exist.")
        sys.exit(1)

    commits = [
        {
            "type": "TYPE-I",
            "original_commit": "da51c8ccbf0171abefb2978810efd6db0f6f743e",
            "backport_commit": "b25542fd147164cd0e89a803bbe49f865972abdb",
        },
        {
            "type": "TYPE-II",
            "original_commit": "595251d5a10d6c4dc14ddfa7d4d7b6e2d5fab3ab",
            "backport_commit": "cd93409c3450fa843ed48cfc6c2042835b9e39b7",
        },
        {
            "type": "TYPE-III",
            "original_commit": "88cf2487e7deb38686870e40bf08b2b729b7d848",
            "backport_commit": "223d50f0524d3d3e9a19f18a3b146212ff830042",
        },
        {
            "type": "TYPE-V",
            "original_commit": "c94c021d0e2695f571a1fc841c46ee43f2d5a3ae",
            "backport_commit": "e867dcdb61cafeaa590dccfec0bc595ad3aac495",
        },
    ]

    for item in commits:
        patch_type = item["type"]
        original_commit = item["original_commit"]
        backport_commit = item["backport_commit"]

        print(f"\n" + "=" * 60)
        print(f"Evaluating {patch_type} Patch")
        print(
            f"Original: {original_commit} -> Target Before Backport: {backport_commit}~1"
        )
        print(f"=" * 60 + "\n")

        # 1. Extract patch content via git (from the original commit)
        try:
            cmd = ["git", "-C", repo_path, "show", original_commit]
            patch_content = subprocess.check_output(
                cmd, text=True, stderr=subprocess.DEVNULL
            )
        except subprocess.CalledProcessError:
            print(f"Failed to get commit {original_commit} from {repo_path}")
            continue

        # 2. Checkout the repository to the state exactly before the backport was applied
        try:
            checkout_cmd = [
                "git",
                "-C",
                repo_path,
                "checkout",
                "-f",
                f"{backport_commit}~1",
            ]
            subprocess.check_call(
                checkout_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
        except subprocess.CalledProcessError:
            print(
                f"Failed to checkout parent of backport commit {backport_commit}~1 in {repo_path}"
            )
            continue

        # 3. Parse hunks
        hunks = parse_unified_diff(patch_content)

        # Filter down to just Java files for localization
        java_hunks = [
            h for h in hunks if h["file_path"] and h["file_path"].endswith(".java")
        ]

        # 4. Create State
        state = BackportState(
            patch_content=patch_content,
            target_repo_path=repo_path,
            target_branch=f"{backport_commit}~1",  # Evaluate against parent of backport
            worktree_path=None,
            clean_state=True,
            classification=None,
            localization_results=[],
            hunks=java_hunks,
            retry_contexts=[],
            current_attempt=1,
            max_retries=3,
            tokens_used=0,
            wall_clock_time=0.0,
            status="started",
        )

        # 4. Agent 1: Classify Patch
        print(f"Running Agent 1: Patch Classifier...")
        try:
            state = classify_patch(state)
            cls = state["classification"]
            print(f"  Classified as: {cls.patch_type}")
            print(f"  Confidence: {cls.confidence}")
            print(f"  Reasoning: {cls.reasoning}")
        except Exception as e:
            print(f"  [SKIPPED] Agent 1 Classification (Requires LLM API Key): {e}")

        # 5. Agent 2: Code Localizer
        print(f"\nRunning Agent 2: Code Localizer ({len(java_hunks)} Java hunks)...")
        try:
            state = localize_hunks(state)
            results = state["localization_results"]
            for i, res in enumerate(results):
                print(f"  Hunk {i + 1}/{len(java_hunks)} -> {res.file_path}")
                print(
                    f"    Method: {res.method_used} | Lines: {res.start_line}-{res.end_line} | Confidence: {res.confidence:.2f}"
                )
        except Exception as e:
            print(f"  Localization Failed: {e}")

    print("\nRestoring repository to main branch...")
    try:
        subprocess.check_call(
            ["git", "-C", repo_path, "checkout", "-f", "main"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except subprocess.CalledProcessError:
        pass


if __name__ == "__main__":
    run_evaluation()
