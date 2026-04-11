
import os
import sys
from pathlib import Path

# Add src to path
sys.path.append(str(Path(__file__).parent))

from src.agents.agent2_classifier import classify_patch
from src.core.state import BackportState

def test_repro():
    patch_path = "/home/kaushal/backport-claw/tests/shadow_run_results_v3/crate/TYPE-V_6e18150a/mainline.patch"
    if not os.path.exists(patch_path):
        print(f"Patch not found at {patch_path}")
        return

    with open(patch_path, "r") as f:
        patch_content = f.read()

    state: BackportState = {
        "patch_content": patch_content,
        "localization_results": [], # Mock empty
        "tokens_used": 0,
    }

    print("Running classify_patch...")
    try:
        new_state = classify_patch(state)
        print("Success!")
        print(new_state["classification"])
    except Exception as e:
        print("Failed!")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    test_repro()
