"""
Agent 7: Validation Loop — "Prove Red, Make Green"

Steps
-----
1. Apply synthesized_hunks (CLAW exact-string replacement) — Agents 4/5/6 output.
   (applied_hunks from Agent 3 are already on disk; they are skipped here.)
2. Apply developer_aux_hunks (git apply) — test files, non-Java files, auto-generated files
   that were segregated from the LLM pipeline in Agent 1.
3. Run build (helpers/{project}/run_build.sh or Maven/Gradle).
4. Detect and run relevant tests.
5. Evaluate test state transition vs optional baseline.
6. On failure: restore repo state, populate retry_context, return for graph retry routing.

Retry routing decisions (failure_category → suggested_action):
  "context_mismatch"  → re-localize (back to code_localizer)
  "api_mismatch"      → route to namespace_adapter or structural_refactor
  "test_failure"      → regenerate hunk (back to hunk_synthesizer)
  "infrastructure"    → abort (transient infra issue, cannot fix)
  ""                  → success
"""

from __future__ import annotations

import glob
import os
import re
import subprocess
import tempfile
from typing import Any

from src.backport_claw.apply_hunk import CLAWHunkApplier
from src.core.state import BackportState, PatchRetryContext
from src.tools.build_systems import (
    BuildResult,
    TestResult,
    classify_build_failure,
    detect_test_targets,
    evaluate_test_transition,
    parse_compile_errors,
    restore_repo_state,
    run_build,
    run_tests,
)

MAX_VALIDATION_ATTEMPTS = 3


# ──────────────────────────────────────────────────────────────────────────────
# Fix H — Error-tail capture
# ──────────────────────────────────────────────────────────────────────────────

_ERROR_SIGNAL_RE = re.compile(
    r"(?:error:|ERROR|FAILED|Exception|\.java:\d+:|BUILD FAILURE)",
    re.IGNORECASE,
)


def _extract_build_errors(log: str, max_lines: int = 100, max_chars: int = 4000) -> str:
    """
    Extract the most actionable lines from a build/test log.

    Preference order:
     1. Lines matching error signals (javac errors, FAILED, Exception, etc.)
        — collected from the tail of the log so we get actual compile errors,
        not Docker/BuildKit preamble.
     2. Last max_chars characters of the full log as a fallback.

    Fix H: previously the code used log[:2000] which captured only Docker preamble.
    """
    if not log:
        return ""
    lines = log.splitlines()
    # Collect matching lines from the full log (keep tail order)
    error_lines = [l for l in lines if _ERROR_SIGNAL_RE.search(l)]
    if error_lines:
        # Take last max_lines of matched lines to avoid overwhelming output
        excerpt_lines = error_lines[-max_lines:]
        excerpt = "\n".join(excerpt_lines)
        if len(excerpt) > max_chars:
            excerpt = excerpt[-max_chars:]
        return excerpt
    # Fallback: just the tail
    tail = log[-max_chars:]
    return tail


# ──────────────────────────────────────────────────────────────────────────────
# Patch application helpers
# ──────────────────────────────────────────────────────────────────────────────

def _norm_path(path: str) -> str:
    p = (path or "").strip().replace("\\", "/").lstrip("/")
    while p.startswith("a/") or p.startswith("b/"):
        p = p[2:]
    return "" if p == "dev/null" else p


def _build_patch_header(
    target_file: str,
    hunk_text: str,
    file_operation: str,
    old_file_path: str | None = None,
) -> str:
    """
    Wrap a bare @@ hunk in a minimal unified diff header so git apply can handle it.
    """
    op = (file_operation or "MODIFIED").upper()
    p = _norm_path(target_file)
    old_p = _norm_path(old_file_path or target_file)

    header_lines: list[str] = []

    if op == "ADDED":
        header_lines = [
            f"diff --git a/{p} b/{p}",
            "new file mode 100644",
            "index 0000000..0000000",
            "--- /dev/null",
            f"+++ b/{p}",
        ]
    elif op == "DELETED":
        header_lines = [
            f"diff --git a/{p} b/{p}",
            "deleted file mode 100644",
            "index 0000000..0000000",
            f"--- a/{p}",
            "+++ /dev/null",
        ]
    elif op == "RENAMED":
        header_lines = [
            f"diff --git a/{old_p} b/{p}",
            f"rename from {old_p}",
            f"rename to {p}",
            "similarity index 90%",
            "index 0000000..0000000",
            f"--- a/{old_p}",
            f"+++ b/{p}",
        ]
    else:
        header_lines = [
            f"diff --git a/{p} b/{p}",
            "index 0000000..0000000 100644",
            f"--- a/{p}",
            f"+++ b/{p}",
        ]

    body = hunk_text if hunk_text.endswith("\n") else hunk_text + "\n"
    return "\n".join(header_lines) + "\n" + body


def _check_already_applied(repo_path: str, patch_path: str) -> bool:
    """
    Fix A: Return True if the patch appears already applied (reverse-check passes).
    Uses `git apply -R --check` to test whether the patch can be un-applied,
    which means it was already applied forward.
    """
    r = subprocess.run(
        ["git", "apply", "-R", "--check", "--whitespace=nowarn", patch_path],
        capture_output=True, text=True, cwd=repo_path,
    )
    return r.returncode == 0


def _apply_patch_with_fallbacks(repo_path: str, patch_content: str) -> dict[str, Any]:
    """
    Try git apply → git apply --ignore-whitespace → GNU patch → git apply --3way, in order.

    Fix A additions:
    - Pre-check: if the patch is already applied (reverse-check passes), treat as success.
    - 4th strategy: git apply --3way (3-way merge using blob history) for drifted context.

    Returns {"success": bool, "output": str, "strategy": str}.
    """
    print(f"  [agent7] Applying patch to {repo_path}")
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".patch", delete=False, encoding="utf-8"
        ) as tmp:
            tmp.write(patch_content)
            tmp_path = tmp.name

        # Fix A: Pre-check for already-applied patches
        if _check_already_applied(repo_path, tmp_path):
            print("  [agent7] Patch already applied (reverse-check passed) — treating as success")
            return {"success": True, "output": "Patch already applied (no-op).", "strategy": "already-applied"}

        strategies = [
            ("git-strict",   ["git", "apply", "--recount", "--whitespace=nowarn", tmp_path]),
            ("git-tolerant", ["git", "apply", "--recount", "--ignore-space-change",
                              "--ignore-whitespace", "--whitespace=nowarn", tmp_path]),
        ]
        errors: list[str] = []
        for name, cmd in strategies:
            print(f"  [agent7] Trying strategy: {name}")
            r = subprocess.run(cmd, capture_output=True, text=True, cwd=repo_path)
            if r.returncode == 0:
                print(f"  [agent7] Strategy {name} succeeded")
                return {"success": True, "output": r.stdout or f"Applied via {name}.", "strategy": name}
            errors.append(f"[{name}] {(r.stderr or r.stdout or '').strip()}")

        # GNU patch fallback
        print("  [agent7] Trying strategy: gnu-patch")
        patch_cmd = ["patch", "-p1", "--batch", "--forward", "--reject-file=-",
                     "--ignore-whitespace", "-i", tmp_path]
        dry = subprocess.run(patch_cmd + ["--dry-run"], capture_output=True, text=True, cwd=repo_path)
        if dry.returncode == 0:
            apply = subprocess.run(patch_cmd, capture_output=True, text=True, cwd=repo_path)
            if apply.returncode == 0:
                print("  [agent7] Strategy gnu-patch succeeded")
                return {"success": True, "output": apply.stdout or "Applied via gnu-patch.", "strategy": "gnu-patch"}
            errors.append(f"[gnu-patch] {(apply.stderr or apply.stdout or '').strip()}")
        else:
            errors.append(f"[gnu-patch-dry-run] {(dry.stderr or dry.stdout or '').strip()}")

        # Fix A: 4th strategy — git apply --3way (3-way merge using blob history)
        print("  [agent7] Trying strategy: git-3way")
        r3 = subprocess.run(
            ["git", "apply", "--3way", "--recount", "--whitespace=nowarn", tmp_path],
            capture_output=True, text=True, cwd=repo_path,
        )
        if r3.returncode == 0:
            print("  [agent7] Strategy git-3way succeeded")
            return {"success": True, "output": r3.stdout or "Applied via git --3way.", "strategy": "git-3way"}
        errors.append(f"[git-3way] {(r3.stderr or r3.stdout or '').strip()}")

        print("  [agent7] All patch application strategies failed")
        return {"success": False, "output": "\n".join(errors), "strategy": "all-failed"}
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass


# ──────────────────────────────────────────────────────────────────────────────
# Fix A — RST release-notes path remap
# ──────────────────────────────────────────────────────────────────────────────

def _is_rst_release_notes(file_path: str) -> bool:
    """Return True if the file looks like a versioned release-notes RST."""
    p = (file_path or "").replace("\\", "/").lower()
    return p.endswith(".rst") and "release" in p


def _parse_version_tuple(filename: str) -> tuple[int, ...]:
    """Extract a numeric version tuple from a filename like '5.10.12.rst' → (5, 10, 12)."""
    parts = re.findall(r"\d+", filename)
    return tuple(int(x) for x in parts)


def _find_closest_rst(repo_path: str, target_file: str) -> str | None:
    """
    Fix A: Given a target RST path that doesn't exist, find the closest version
    in the same parent directory (highest version number wins).

    Example: 'docs/appendices/release-notes/5.10.9.rst' missing on target branch
             which has '5.10.12.rst' → return '5.10.12.rst' path.
    """
    abs_target = os.path.join(repo_path, target_file)
    parent_dir = os.path.dirname(abs_target)
    if not os.path.isdir(parent_dir):
        return None
    rst_files = glob.glob(os.path.join(parent_dir, "*.rst"))
    if not rst_files:
        return None
    # Sort by version tuple descending, pick highest
    rst_files.sort(key=lambda f: _parse_version_tuple(os.path.basename(f)), reverse=True)
    best = rst_files[0]
    return os.path.relpath(best, repo_path)


def _try_apply_rst_hunk_remapped(
    repo_path: str, hunk_text: str, original_target: str
) -> dict[str, Any]:
    """
    Fix A: When an RST aux hunk's target file doesn't exist, remap to the
    closest-version RST in the same directory and append the added lines.

    Returns {"success": bool, "output": str}.
    """
    remapped = _find_closest_rst(repo_path, original_target)
    if not remapped:
        return {"success": False, "output": f"No RST file found near {original_target}"}

    print(f"  [agent7] RST remap: {original_target} → {remapped}")

    # Extract only the added lines (+) from the hunk body
    added_lines: list[str] = []
    for line in hunk_text.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            added_lines.append(line[1:])  # strip leading '+'

    if not added_lines:
        return {"success": True, "output": f"RST remap: no added lines to apply to {remapped}"}

    abs_remapped = os.path.join(repo_path, remapped)
    try:
        with open(abs_remapped, "r", encoding="utf-8") as f:
            existing = f.read()
        appended = existing.rstrip("\n") + "\n" + "\n".join(added_lines) + "\n"
        with open(abs_remapped, "w", encoding="utf-8") as f:
            f.write(appended)
        return {"success": True, "output": f"RST remap: appended {len(added_lines)} line(s) to {remapped}"}
    except Exception as e:
        return {"success": False, "output": f"RST remap write error for {remapped}: {e}"}


# ──────────────────────────────────────────────────────────────────────────────
# Synthesized hunk application
# ──────────────────────────────────────────────────────────────────────────────

def _apply_synthesized_hunks(
    repo_path: str, synthesized_hunks: list[dict[str, Any]]
) -> dict[str, Any]:
    """
    Apply CLAW-verified synthesized hunks (old_string → new_string replacement) to disk.

    Fix G: Handles new-file hunks where old_string == "" — creates the file with
    new_string as content instead of attempting a find-and-replace.

    Returns {"success": bool, "output": str, "applied_files": list[str]}.
    """
    print(f"  [agent7] Applying {len(synthesized_hunks)} synthesized hunks via CLAW")
    applied_files: list[str] = []
    errors: list[str] = []

    for hunk in synthesized_hunks:
        if not hunk.get("verified", True):
            print(f"  [agent7] Skipping unverified hunk for {hunk.get('file_path', '?')}")
            errors.append(
                f"{hunk.get('file_path', '?')}: skipped unverified synthesized hunk"
            )
            continue

        file_path = hunk.get("file_path", "")
        old_string = hunk.get("old_string", "")
        new_string = hunk.get("new_string", "")

        if not file_path:
            print(f"  [agent7] Hunk missing file_path: {hunk}")
            errors.append(f"synthesized hunk missing file_path: {hunk}")
            continue

        abs_path = os.path.join(repo_path, file_path)

        # Fix G: new-file hunk — old_string is empty sentinel, create the file
        if not old_string:
            if os.path.exists(abs_path):
                print(f"  [agent7] New-file hunk: {file_path} already exists, skipping creation")
                if file_path not in applied_files:
                    applied_files.append(file_path)
                continue
            try:
                os.makedirs(os.path.dirname(abs_path), exist_ok=True)
                with open(abs_path, "w", encoding="utf-8") as f:
                    f.write(new_string if new_string.endswith("\n") else new_string + "\n")
                print(f"  [agent7] New-file hunk: created {file_path}")
                if file_path not in applied_files:
                    applied_files.append(file_path)
            except Exception as e:
                print(f"  [agent7] New-file creation error for {file_path}: {e}")
                errors.append(f"{file_path}: new-file creation error — {e}")
            continue

        if not os.path.exists(abs_path):
            print(f"  [agent7] File not found: {file_path}")
            errors.append(f"{file_path}: file not found in worktree")
            continue

        try:
            with open(abs_path, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception as e:
            print(f"  [agent7] Read error for {file_path}: {e}")
            errors.append(f"{file_path}: read error — {e}")
            continue

        applier = CLAWHunkApplier(content)
        success, new_content = applier.find_and_replace(old_string, new_string)
        if not success:
            print(f"  [agent7] CLAW find_and_replace failed for {file_path}")
            errors.append(
                f"{file_path}: CLAW exact-string not found — old_string may have drifted"
            )
            continue

        try:
            with open(abs_path, "w", encoding="utf-8") as f:
                f.write(new_content)
        except Exception as e:
            print(f"  [agent7] Write error for {file_path}: {e}")
            errors.append(f"{file_path}: write error — {e}")
            continue

        if file_path not in applied_files:
            applied_files.append(file_path)

    if errors and not applied_files:
        print("  [agent7] Failed to apply any synthesized hunks")
        return {"success": False, "output": "\n".join(errors), "applied_files": []}

    print(f"  [agent7] Applied synthesized hunks to {len(applied_files)} files")
    output = "Synthesized hunks applied."
    if errors:
        output += "\nWarnings:\n" + "\n".join(errors)
    return {"success": True, "output": output, "applied_files": applied_files}


def _normalize_aux_hunk_file_operation(
    hunk: dict[str, Any], repo_path: str
) -> dict[str, Any]:
    """
    Correct file_operation for aux hunks based on actual disk state, mirroring the
    legacy ValidationToolkit.apply_adapted_hunks normalization logic.
    """
    h = dict(hunk)
    target = _norm_path(h.get("target_file") or h.get("file_path") or "")
    h["target_file"] = target
    old_target = _norm_path(h.get("old_target_file") or "")
    h["old_target_file"] = old_target or None

    hunk_text = h.get("hunk_text", "")
    original_op = (h.get("file_operation") or "MODIFIED").upper()

    file_exists = bool(target and os.path.isfile(os.path.join(repo_path, target)))
    old_exists = bool(old_target and os.path.isfile(os.path.join(repo_path, old_target)))

    def _is_full_create(text: str) -> bool:
        """Pure-addition hunk with no context or removal lines."""
        if not text or not text.strip():
            return False
        lines = text.splitlines()
        if not lines or not lines[0].startswith("@@"):
            return False
        body = lines[1:]
        return bool(body) and all(l.startswith("+") for l in body if l)

    corrected = original_op

    if original_op == "ADDED" and file_exists:
        corrected = None if _is_full_create(hunk_text) else "MODIFIED"
    elif original_op == "DELETED" and not file_exists:
        corrected = None
    elif original_op == "MODIFIED" and not file_exists:
        corrected = "RENAMED" if (old_exists and old_target and old_target != target) else "ADDED"
    elif original_op == "RENAMED":
        if old_exists and not file_exists:
            corrected = "RENAMED"
        elif not old_exists and file_exists:
            corrected = "MODIFIED"
        elif old_exists and file_exists:
            corrected = "MODIFIED"
        else:
            corrected = "ADDED" if hunk_text.strip() else None

    h["file_operation"] = corrected
    return h


def _apply_developer_aux_hunks(
    repo_path: str, developer_aux_hunks: list[dict[str, Any]]
) -> dict[str, Any]:
    """
    Apply developer auxiliary hunks (test files, non-Java, auto-generated) via git apply.
    Groups hunks by target file, sorts bottom-to-top, then runs git apply with fallbacks.

    Fix A additions:
    - On failure for RST release-notes files: attempt path remap to closest version RST.
    - On failure: check "already applied" before aborting (handled inside
      _apply_patch_with_fallbacks via the pre-check).
    """
    if not developer_aux_hunks:
        return {"success": True, "output": "No developer aux hunks to apply.", "applied_files": []}

    print(f"  [agent7] Applying {len(developer_aux_hunks)} developer aux hunks")

    # Fast path: if ALL hunks carry raw_patch (verbatim from target_patch), apply
    # them directly without reconstruction — this preserves real line numbers.
    if all(h.get("raw_patch") for h in developer_aux_hunks):
        print("  [agent7] Using raw_patch mode for aux hunks")
        applied_files: list[str] = []
        all_errors: list[str] = []
        for h in developer_aux_hunks:
            fp = h.get("file_path", "unknown")
            print(f"  [agent7] Applying raw patch for {fp}")
            result = _apply_patch_with_fallbacks(repo_path, h["raw_patch"])
            if result["success"]:
                applied_files.append(fp)
            else:
                # Fix A: RST release-notes remap fallback
                if _is_rst_release_notes(fp) and not os.path.isfile(os.path.join(repo_path, fp)):
                    print(f"  [agent7] RST file missing, attempting version remap for {fp}")
                    # Extract hunk_text from the raw_patch for content extraction
                    hunk_body = "\n".join(
                        l for l in h["raw_patch"].splitlines()
                        if l.startswith("@@") or l.startswith("+") or l.startswith("-") or l.startswith(" ")
                    )
                    remap_result = _try_apply_rst_hunk_remapped(repo_path, hunk_body, fp)
                    if remap_result["success"]:
                        print(f"  [agent7] RST remap succeeded for {fp}")
                        applied_files.append(fp)
                        continue
                    else:
                        print(f"  [agent7] RST remap also failed for {fp}: {remap_result['output']}")

                print(f"  [agent7] Raw patch apply failed for {fp}")
                all_errors.append(f"aux patch apply failed for {fp}: {result['output'][:500]}")
                restore_repo_state(repo_path)
                return {"success": False, "output": "\n".join(all_errors), "applied_files": []}
        print(f"  [agent7] Applied {len(applied_files)} raw aux patches")
        return {
            "success": True,
            "output": "Developer aux hunks applied (raw_patch mode)." + (
                "\nWarnings:\n" + "\n".join(all_errors) if all_errors else ""
            ),
            "applied_files": applied_files,
        }

    # Normalize and group by target file
    normalized: list[dict[str, Any]] = []
    for raw in developer_aux_hunks:
        nh = _normalize_aux_hunk_file_operation(raw, repo_path)
        if nh.get("file_operation") is not None:
            normalized.append(nh)
        else:
            print(f"  agent7: skipping aux hunk for {nh.get('target_file')} (no-op after normalization)")

    hunks_by_file: dict[str, list[dict[str, Any]]] = {}
    for h in normalized:
        tf = h["target_file"] or "unknown"
        hunks_by_file.setdefault(tf, []).append(h)

    applied_files: list[str] = []
    all_errors: list[str] = []

    for target_file, file_hunks in hunks_by_file.items():
        # Sort bottom-to-top to prevent line-number shifts
        file_hunks.sort(key=lambda h: h.get("insertion_line", 0), reverse=True)

        # Determine file operation and old path
        ops = {h["file_operation"].upper() for h in file_hunks}
        old_file_path = next(
            (h.get("old_target_file") for h in file_hunks if h.get("old_target_file")),
            None,
        )
        if "RENAMED" in ops:
            file_operation = "RENAMED"
        elif "ADDED" in ops and "DELETED" not in ops:
            file_operation = "ADDED"
        elif "DELETED" in ops and "ADDED" not in ops:
            file_operation = "DELETED"
        else:
            file_operation = "MODIFIED"

        # Handle structural-only operations (rename/delete with no hunk body)
        if file_operation == "RENAMED" and old_file_path:
            src = os.path.join(repo_path, old_file_path)
            dst = os.path.join(repo_path, target_file)
            if os.path.isfile(src) and not os.path.isfile(dst):
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                os.replace(src, dst)
                applied_files.append(target_file)
        elif file_operation == "DELETED":
            src = os.path.join(repo_path, target_file)
            if os.path.isfile(src):
                os.remove(src)
                applied_files.append(target_file)

        # Build one unified diff entry: ONE header + all @@ hunk bodies.
        # Multiple calls to _build_patch_header per file would emit multiple
        # "diff --git" lines for the same file, which git apply rejects.
        hunk_bodies = ""
        for h in file_hunks:
            hunk_text = (h.get("hunk_text") or "").strip()
            if not hunk_text:
                continue
            hunk_bodies += hunk_text if hunk_text.endswith("\n") else hunk_text + "\n"

        combined_patch = ""
        if hunk_bodies:
            current_op = "MODIFIED" if target_file in applied_files else file_operation
            try:
                combined_patch = _build_patch_header(
                    target_file,
                    hunk_bodies,
                    current_op,
                    old_file_path if current_op == "RENAMED" else None,
                )
                if target_file not in applied_files:
                    applied_files.append(target_file)
            except Exception as e:
                all_errors.append(f"aux hunk build error for {target_file}: {e}")

        if combined_patch:
            result = _apply_patch_with_fallbacks(repo_path, combined_patch)
            if not result["success"]:
                # Fix A: RST remap fallback for reconstructed-patch path
                if _is_rst_release_notes(target_file) and not os.path.isfile(
                    os.path.join(repo_path, target_file)
                ):
                    print(f"  [agent7] RST file missing, attempting version remap for {target_file}")
                    remap_result = _try_apply_rst_hunk_remapped(repo_path, hunk_bodies, target_file)
                    if remap_result["success"]:
                        print(f"  [agent7] RST remap succeeded for {target_file}")
                        continue

                all_errors.append(
                    f"aux patch apply failed for {target_file}: {result['output'][:500]}"
                )
                # On aux apply failure restore and abort (these files must apply cleanly)
                restore_repo_state(repo_path)
                return {
                    "success": False,
                    "output": "\n".join(all_errors),
                    "applied_files": [],
                }

    return {
        "success": True,
        "output": "Developer aux hunks applied." + (
            "\nWarnings:\n" + "\n".join(all_errors) if all_errors else ""
        ),
        "applied_files": applied_files,
    }


# ──────────────────────────────────────────────────────────────────────────────
# Phase 0 baseline (used by shadow/evaluation harness)
# ──────────────────────────────────────────────────────────────────────────────

def run_phase0_baseline(
    repo_path: str,
    project: str,
    developer_aux_hunks: list[dict[str, Any]],
    changed_files: list[str],
) -> dict[str, Any]:
    """
    Apply ONLY test-related hunks from developer_aux_hunks (test files only)
    to the repo at backport_commit~1, run targeted tests, then restore.

    This baseline captures the test state WITH test changes but WITHOUT the Java
    production code changes or non-Java aux changes. Tests that reference new
    APIs/methods introduced by the patch will fail here; after the agents apply
    those code changes they should pass.

    Returns a dict compatible with evaluate_test_transition(baseline=...).
    """
    from src.agents.agent1_localizer import _is_test_file

    test_hunks = [h for h in developer_aux_hunks if _is_test_file(h.get("file_path", ""))]
    if not test_hunks:
        print("  [agent7] phase0: no test hunks found, skipping baseline run")
        return {"test_state": {}, "mode": "baseline-no-tests", "skipped": True}

    print(f"  [agent7] phase0: applying {len(test_hunks)} test hunks for baseline...")
    aux_result = _apply_developer_aux_hunks(repo_path, test_hunks)
    if not aux_result["success"]:
        print(f"  [agent7] phase0: test hunk apply failed, skipping baseline: {aux_result['output'][:200]}")
        restore_repo_state(repo_path)
        return {"test_state": {}, "mode": "baseline-apply-failed", "skipped": True}

    # Detect targets from the changed files (same as agent7 will use later).
    target_info = detect_test_targets(repo_path, project, changed_files)
    print(f"  [agent7] phase0: running baseline tests ({len(target_info.test_targets)} targets) directly...")
    test_res = run_tests(repo_path, project, target_info=target_info, changed_files=changed_files)

    restore_repo_state(repo_path)
    print(f"  [agent7] phase0: baseline done — mode={test_res.mode}, success={test_res.success}")
    return {
        "test_state": test_res.test_state,
        "mode": f"baseline-{test_res.mode}",
        "skipped": False,
    }


# ──────────────────────────────────────────────────────────────────────────────
# Retry context helpers
# ──────────────────────────────────────────────────────────────────────────────

def _build_retry_context(
    failure_category: str,
    error_message: str,
    attempt: int,
) -> PatchRetryContext:
    action_map = {
        "context_mismatch": "re-localize",
        "api_mismatch": "route_to_namespace_or_structural",
        "test_failure": "regenerate_hunk",
        "infrastructure": "abort_transient_failure",
    }
    # Fix H: use tail-extraction instead of raw [:2000] truncation
    extracted = _extract_build_errors(error_message)
    return PatchRetryContext(
        error_type=failure_category,
        error_message=extracted[:4000],
        attempt_count=attempt,
        suggested_action=action_map.get(failure_category, "inspect_and_retry"),
    )


# ──────────────────────────────────────────────────────────────────────────────
# Main agent entry point
# ──────────────────────────────────────────────────────────────────────────────

def run_validation(state: BackportState) -> BackportState:
    """
    Agent 7: Validation Loop.

    Applies all pending hunks, builds the project, runs targeted tests,
    and evaluates whether the backport is valid.

    On failure, restores repo state and populates structured retry context
    so graph routing can send the patch back to the appropriate upstream agent.
    """
    repo_path = state.get("worktree_path") or state.get("target_repo_path", "")
    if not repo_path:
        state["validation_passed"] = False
        state["validation_error_context"] = "No repo path available for validation."
        state["validation_failure_category"] = "infrastructure"
        return state

    attempts = state.get("validation_attempts", 0)
    if attempts >= MAX_VALIDATION_ATTEMPTS:
        state["validation_passed"] = False
        state["validation_error_context"] = (
            f"Max validation attempts ({MAX_VALIDATION_ATTEMPTS}) reached."
        )
        state["validation_failure_category"] = "max_retries"
        return state

    synthesized_hunks: list[dict[str, Any]] = state.get("synthesized_hunks", [])
    developer_aux_hunks: list[dict[str, Any]] = state.get("developer_aux_hunks", [])
    applied_hunks: list[dict[str, Any]] = state.get("applied_hunks", [])  # already on disk

    # Nothing to validate if every category is empty
    if not synthesized_hunks and not developer_aux_hunks and not applied_hunks:
        state["validation_passed"] = False
        state["validation_error_context"] = (
            "Validation aborted: no hunks produced by any agent. "
            "Regenerate hunks with target-grounded anchors."
        )
        state["validation_failure_category"] = "context_mismatch"
        state["validation_attempts"] = attempts + 1
        return state

    project = os.path.basename(repo_path).strip().lower()
    validation_results: dict[str, Any] = {}

    # ── Step 1: On retry, restore repo to HEAD before re-applying ────────────
    # On the first attempt (attempts == 0) we skip the restore so that Agent 3's
    # fast-apply changes (already written to disk) are preserved.
    # On retries the caller is expected to have regenerated hunks, so we start clean.
    if attempts > 0:
        restore_repo_state(repo_path)

    # ── Step 2: Apply synthesized hunks (CLAW) ────────────────────────────────
    # If the pipeline already applied synthesized hunks to disk before capturing
    # generated.patch (synthesized_hunks_pre_applied=True), skip re-application on
    # the first attempt. On retries the repo is restored first, so we must re-apply.
    pre_applied = state.get("synthesized_hunks_pre_applied", False) and attempts == 0
    if pre_applied:
        print(f"  agent7: synthesized hunks already on disk (pre-applied), skipping apply step")
        synth_result = {"success": True, "output": "pre-applied by pipeline", "applied_files": []}
    else:
        print(f"  agent7: applying {len(synthesized_hunks)} synthesized hunk(s)...")
        synth_result = _apply_synthesized_hunks(repo_path, synthesized_hunks)
    validation_results["hunk_application"] = {
        "success": synth_result["success"],
        "raw": synth_result["output"],
        "applied_files": synth_result.get("applied_files", []),
    }

    if not synth_result["success"]:
        restore_repo_state(repo_path)
        ctx = _build_retry_context("context_mismatch", synth_result["output"], attempts + 1)
        state["validation_passed"] = False
        state["validation_error_context"] = f"CLAW application failed: {synth_result['output'][:500]}"
        state["validation_failure_category"] = "context_mismatch"
        state["validation_retry_files"] = [
            h.get("file_path", "") for h in synthesized_hunks if not h.get("verified", True)
        ]
        state["validation_attempts"] = attempts + 1
        state["validation_results"] = validation_results
        state["retry_contexts"] = list(state.get("retry_contexts", [])) + [ctx]
        return state

    # ── Step 3: Apply developer aux hunks (git apply) ─────────────────────────
    print(f"  agent7: applying {len(developer_aux_hunks)} developer aux hunk(s)...")
    aux_result = _apply_developer_aux_hunks(repo_path, developer_aux_hunks)
    validation_results["aux_hunk_application"] = {
        "success": aux_result["success"],
        "raw": aux_result["output"],
        "applied_files": aux_result.get("applied_files", []),
    }

    if not aux_result["success"]:
        restore_repo_state(repo_path)
        ctx = _build_retry_context("context_mismatch", aux_result["output"], attempts + 1)
        state["validation_passed"] = False
        state["validation_error_context"] = f"Aux hunk application failed: {aux_result['output'][:500]}"
        state["validation_failure_category"] = "context_mismatch"
        state["validation_retry_files"] = []
        state["validation_attempts"] = attempts + 1
        state["validation_results"] = validation_results
        state["retry_contexts"] = list(state.get("retry_contexts", [])) + [ctx]
        return state

    # ── Step 4: Build ─────────────────────────────────────────────────────────
    print(f"  agent7: running build for {project}...")
    build_res: BuildResult = run_build(repo_path, project)
    validation_results["build"] = {
        "success": build_res.success,
        "raw": build_res.output,
        "mode": build_res.mode,
    }

    if not build_res.success:
        failure_category = classify_build_failure(build_res.output)
        compile_errors = parse_compile_errors(build_res.output)
        retry_files = sorted({e.file_path for e in compile_errors})

        validation_results["build"]["compile_errors"] = [
            {"file": e.file_path, "line": e.line, "message": e.message}
            for e in compile_errors
        ]

        # Map api_mismatch → namespace_adapter route signal
        mapped_category = "api_mismatch" if failure_category == "api_mismatch" else failure_category

        restore_repo_state(repo_path)
        ctx = _build_retry_context(mapped_category, build_res.output, attempts + 1)
        state["validation_passed"] = False
        if compile_errors:
            error_summary = "\n".join(
                f"{e.file_path}:{e.line}: {e.message}" for e in compile_errors[:20]
            )
            state["validation_error_context"] = (
                f"Build failed ({build_res.mode}) — compile errors:\n{error_summary}"
            )
        else:
            # Fix H: use tail extraction for the error context presented to the user
            excerpt = _extract_build_errors(build_res.output)
            state["validation_error_context"] = (
                f"Build failed ({build_res.mode}): {excerpt}"
            )
        state["validation_failure_category"] = mapped_category
        state["validation_retry_files"] = retry_files
        state["validation_attempts"] = attempts + 1
        state["validation_results"] = validation_results
        state["retry_contexts"] = list(state.get("retry_contexts", [])) + [ctx]
        return state

    # ── Step 5: Detect test targets from all changed files ────────────────────
    all_changed_files: list[str] = list(
        {
            h.get("file_path") or h.get("target_file") or ""
            for h in (synthesized_hunks + developer_aux_hunks + applied_hunks)
        }
    )
    all_changed_files = [f for f in all_changed_files if f]

    test_targets = detect_test_targets(repo_path, project, all_changed_files)
    validation_results["test_target_detection"] = {
        "success": True,
        "raw": test_targets.__dict__,
    }

    # ── Step 6: Run tests ─────────────────────────────────────────────────────
    print(
        f"  agent7: running tests for {project} "
        f"({len(test_targets.test_targets)} target(s))..."
    )
    test_res: TestResult = run_tests(
        repo_path, project, target_info=test_targets, changed_files=all_changed_files
    )

    # ── Step 7: Evaluate test state transition vs baseline ────────────────────
    phase_0_baseline = state.get("validation_results", {}).get("phase_0_baseline_test_result")
    transition_eval = evaluate_test_transition(phase_0_baseline, {"test_state": test_res.test_state})

    validation_results["tests"] = {
        "success": test_res.success,
        "compile_error": test_res.compile_error,
        "raw": test_res.output[-3000:] if len(test_res.output) > 3000 else test_res.output,
        "mode": test_res.mode,
        "test_state": test_res.test_state,
        "state_transition": transition_eval,
    }

    # ── Step 8: Determine overall outcome ─────────────────────────────────────
    if test_res.compile_error:
        # Compilation failure during test phase (different from build phase above)
        compile_errors = parse_compile_errors(test_res.output)
        retry_files = sorted({e.file_path for e in compile_errors})
        restore_repo_state(repo_path)
        ctx = _build_retry_context("context_mismatch", test_res.output, attempts + 1)
        state["validation_passed"] = False
        state["validation_error_context"] = "Compilation error during test phase."
        state["validation_failure_category"] = "context_mismatch"
        state["validation_retry_files"] = retry_files
        state["validation_attempts"] = attempts + 1
        state["validation_results"] = validation_results
        state["retry_contexts"] = list(state.get("retry_contexts", [])) + [ctx]
        return state

    if not test_res.success:
        # Distinguish infrastructure failure from test assertion failure
        output_lower = test_res.output.lower()
        is_infra = (
            "connection refused" in output_lower
            or "daemon disappeared" in output_lower
            or "cannot locate tasks" in output_lower
            or "task 'test' is ambiguous" in output_lower
        )
        if is_infra:
            restore_repo_state(repo_path)
            ctx = _build_retry_context("infrastructure", test_res.output, attempts + 1)
            state["validation_passed"] = False
            state["validation_error_context"] = "Test infrastructure failure (transient)."
            state["validation_failure_category"] = "infrastructure"
            state["validation_retry_files"] = []
            state["validation_attempts"] = attempts + 1
            state["validation_results"] = validation_results
            state["retry_contexts"] = list(state.get("retry_contexts", [])) + [ctx]
            return state

        # Check for regressions (pass → fail)
        if transition_eval.get("pass_to_fail"):
            restore_repo_state(repo_path)
            ctx = _build_retry_context("test_failure", test_res.output, attempts + 1)
            regressions = transition_eval["pass_to_fail"]
            state["validation_passed"] = False
            state["validation_error_context"] = (
                f"Test regressions (pass→fail): {regressions[:5]}"
            )
            state["validation_failure_category"] = "test_failure"
            state["validation_retry_files"] = []
            state["validation_attempts"] = attempts + 1
            state["validation_results"] = validation_results
            state["retry_contexts"] = list(state.get("retry_contexts", [])) + [ctx]
            return state

    # ── Success ────────────────────────────────────────────────────────────────
    print(f"  agent7: validation PASSED (attempt {attempts + 1}).")
    state["validation_passed"] = True
    state["validation_error_context"] = ""
    state["validation_failure_category"] = ""
    state["validation_retry_files"] = []
    state["validation_attempts"] = attempts + 1
    state["validation_results"] = validation_results
    return state
