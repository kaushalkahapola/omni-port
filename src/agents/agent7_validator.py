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

import os
import re
import subprocess
import tempfile
from typing import Any

from src.backport_claw.apply_hunk import CLAWHunkApplier
from src.core.state import BackportState, PatchRetryContext
from src.agents.agent1_localizer import _is_test_file
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


# ── Fix H: error-tail capture ─────────────────────────────────────────────────

_ERROR_KEYWORDS = ("error:", "error[", "ERROR", "FAILED", "Exception", "BUILD FAILURE",
                   "cannot find symbol", "symbol:", "incompatible types", "warning:")

def _extract_error_tail(log: str, max_chars: int = 3000) -> str:
    """
    Return the most useful portion of a build log for diagnostics.
    Prefers lines containing error/failure keywords from the TAIL of the log,
    so that Docker preamble (which appears at the top) is skipped.
    Falls back to the last max_chars characters when no keyword lines found.
    """
    lines = log.splitlines()
    # Collect lines with error-relevant keywords
    error_lines = [l for l in lines if any(kw in l for kw in _ERROR_KEYWORDS)]
    if error_lines:
        tail = "\n".join(error_lines[-120:])  # at most 120 error lines
        return tail[-max_chars:]
    # No keyword lines — just return the tail
    return log[-max_chars:] if len(log) > max_chars else log


# ── Unused-import stripper ────────────────────────────────────────────────────

_JAVA_IMPORT_RE = re.compile(
    r"^import\s+(?:static\s+)?([\w.]+);\s*$"
)

def _strip_unused_java_imports(repo_path: str, file_paths: list[str]) -> dict[str, int]:
    """
    For each .java file in file_paths, remove any import whose simple class name
    (the last token after the last '.') does not appear anywhere in the file body
    (lines after the import block).

    Returns a dict mapping file_path → number of imports removed.

    This is a deterministic pre-build fix for checkstyle UnusedImports failures
    that arise when an import is added to the wrong file by the localizer.
    """
    removed: dict[str, int] = {}

    for rel_path in file_paths:
        if not rel_path.endswith(".java"):
            continue
        abs_path = os.path.join(repo_path, rel_path)
        if not os.path.isfile(abs_path):
            continue
        try:
            with open(abs_path, "r", encoding="utf-8", errors="replace") as fh:
                lines = fh.readlines()
        except Exception:
            continue

        # Separate the import block from the rest.
        import_indices: list[int] = []
        body_lines: list[str] = []
        in_imports = False
        package_done = False

        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("package "):
                package_done = True
                body_lines.append(line)
                continue
            if package_done and _JAVA_IMPORT_RE.match(stripped):
                in_imports = True
                import_indices.append(i)
                continue
            if in_imports and not stripped:
                # blank line inside the import block — keep tracking
                import_indices.append(i)
                continue
            # Once we hit a non-import, non-blank line after imports, stop
            if in_imports and stripped:
                in_imports = False
            body_lines.append(line)

        if not import_indices:
            continue

        # Build a set of all tokens that appear in the body.
        body_text = "".join(body_lines)

        new_lines = list(lines)
        count = 0
        seen_fqns: dict[str, int] = {}  # fqn → first-seen index (for duplicate detection)
        for idx in import_indices:
            line = lines[idx]
            m = _JAVA_IMPORT_RE.match(line.strip())
            if not m:
                continue  # blank line — keep as-is
            fqn = m.group(1)
            # Handle static imports: strip member name to get class name
            parts = fqn.split(".")
            simple_name = parts[-1]
            # Wildcard imports (import foo.*) — never strip, too risky
            if simple_name == "*":
                continue
            # Remove duplicate imports (same FQN seen before) — these cause
            # checkstyle UnusedImports when the pipeline injects an import that
            # already exists in the file (e.g. java.util.UUID added to a file
            # that already imports it).
            if fqn in seen_fqns:
                new_lines[idx] = ""  # remove duplicate
                count += 1
                continue
            seen_fqns[fqn] = idx
            # Check if simple_name appears anywhere in the body
            if simple_name not in body_text:
                new_lines[idx] = ""  # remove this import line
                count += 1

        if count:
            # Remove consecutive blank lines left by removed imports.
            result: list[str] = []
            prev_blank = False
            for line in new_lines:
                if line == "":
                    continue  # was removed import — skip
                is_blank = not line.strip()
                if is_blank and prev_blank:
                    continue  # collapse double-blank gaps
                result.append(line)
                prev_blank = is_blank

            try:
                with open(abs_path, "w", encoding="utf-8") as fh:
                    fh.writelines(result)
                removed[rel_path] = count
                print(f"  [agent7] Stripped {count} unused import(s) from {rel_path}")
            except Exception as e:
                print(f"  [agent7] Failed to write {rel_path} after import strip: {e}")

    return removed


# ── Fix A helpers ──────────────────────────────────────────────────────────────

def _is_already_applied(repo_path: str, patch_content: str) -> bool:
    """
    Return True if the patch has already been applied (reverse-check passes).
    """
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".patch", delete=False, encoding="utf-8"
        ) as tmp:
            tmp.write(patch_content)
            tmp_path = tmp.name
        r = subprocess.run(
            ["git", "apply", "--reverse", "--check", "--ignore-space-change",
             "--ignore-whitespace", tmp_path],
            capture_output=True, text=True, cwd=repo_path,
        )
        return r.returncode == 0
    except Exception:
        return False
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass


def _remap_rst_path(patch_content: str, repo_path: str) -> str:
    """
    For release-notes .rst hunks whose target file does not exist on the target
    branch, remap the path to the highest-version .rst file in the same directory.
    Returns potentially modified patch_content.
    """
    rst_dir = os.path.join(repo_path, "docs", "appendices", "release-notes")
    if not os.path.isdir(rst_dir):
        return patch_content

    # Find all .rst files that match X.Y.Z.rst pattern
    try:
        rst_files = [
            f for f in os.listdir(rst_dir)
            if re.match(r"^\d+\.\d+\.\d+\.rst$", f)
        ]
    except OSError:
        return patch_content

    if not rst_files:
        return patch_content

    def version_key(fname: str):
        try:
            parts = fname[:-4].split(".")
            return tuple(int(p) for p in parts)
        except ValueError:
            return (0, 0, 0)

    rst_files.sort(key=version_key, reverse=True)
    highest_rst = rst_files[0]

    # Look for lines like "+++ b/docs/appendices/release-notes/X.Y.Z.rst"
    # where X.Y.Z.rst does not exist on disk.
    lines = patch_content.splitlines(keepends=True)
    new_lines = []
    for line in lines:
        if line.startswith("+++ b/docs/appendices/release-notes/") and line.strip().endswith(".rst"):
            fname = line.strip().split("/")[-1]
            target_abs = os.path.join(rst_dir, fname)
            if not os.path.isfile(target_abs):
                # Remap to highest existing version
                line = line.replace(fname, highest_rst)
        elif line.startswith("--- a/docs/appendices/release-notes/") and line.strip().endswith(".rst"):
            fname = line.strip().split("/")[-1]
            target_abs = os.path.join(rst_dir, fname)
            if not os.path.isfile(target_abs):
                line = line.replace(fname, highest_rst)
        elif line.startswith("diff --git ") and "/docs/appendices/release-notes/" in line:
            # Also fix the diff --git header
            parts = line.strip().split()
            if len(parts) >= 3:
                for fname in rst_files:
                    pass  # keep existing file references as-is
        new_lines.append(line)
    return "".join(new_lines)


def _apply_patch_with_fallbacks(repo_path: str, patch_content: str) -> dict[str, Any]:
    """
    Try git apply → git apply --ignore-whitespace → GNU patch, in order.
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

        strategies = [
            ("git-strict",   ["git", "apply", "--recount", "--whitespace=nowarn", tmp_path]),
            ("git-tolerant", ["git", "apply", "--recount", "--ignore-space-change",
                              "--ignore-whitespace", "--whitespace=nowarn", tmp_path]),
            ("git-3way",     ["git", "apply", "--3way", "--ignore-space-change",
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

        # Already-applied check: if the patch was already applied (sibling backport),
        # treat as success rather than failing.
        if _is_already_applied(repo_path, patch_content):
            print("  [agent7] Patch detected as already applied — treating as success")
            return {"success": True, "output": "Already applied (reverse-check passed).", "strategy": "already-applied"}

        print("  [agent7] All patch application strategies failed")
        return {"success": False, "output": "\n".join(errors), "strategy": "all-failed"}
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass


def _apply_synthesized_hunks(
    repo_path: str, synthesized_hunks: list[dict[str, Any]]
) -> dict[str, Any]:
    """
    Apply CLAW-verified synthesized hunks (old_string → new_string replacement) to disk.
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

        # Fix G: new-file hunks have empty old_string — the file was written by agent6.
        # On the first pass it already exists; on retry passes (after repo restore wipes
        # the file) recreate it from new_string so the build sees the file.
        if not old_string:
            abs_path = os.path.join(repo_path, file_path)
            if os.path.exists(abs_path):
                if file_path not in applied_files:
                    applied_files.append(file_path)
            elif new_string:
                # Repo restore deleted the file — recreate it from the sentinel's new_string.
                try:
                    os.makedirs(os.path.dirname(abs_path), exist_ok=True)
                    with open(abs_path, "w", encoding="utf-8") as f:
                        f.write(new_string)
                    if file_path not in applied_files:
                        applied_files.append(file_path)
                    print(f"  [agent7] Re-created new file after repo restore: {file_path}")
                except Exception as e:
                    errors.append(f"{file_path}: failed to recreate new file — {e}")
            else:
                errors.append(f"{file_path}: new-file hunk — file not found on disk and new_string is empty")
            continue

        abs_path = os.path.join(repo_path, file_path)
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
            # Fix A: remap .rst paths that don't exist on the target branch
            raw = h["raw_patch"]
            if fp.endswith(".rst"):
                raw = _remap_rst_path(raw, repo_path)
            result = _apply_patch_with_fallbacks(repo_path, raw)
            if result["success"]:
                applied_files.append(fp)
            else:
                # Fix A: non-critical aux hunks (release notes, non-test non-Java)
                # log a warning and continue rather than aborting.
                is_critical = _is_test_file(fp) or fp.endswith(".java")
                if is_critical:
                    print(f"  [agent7] Critical aux patch apply failed for {fp}")
                    all_errors.append(f"aux patch apply failed for {fp}: {result['output'][:500]}")
                    restore_repo_state(repo_path)
                    return {"success": False, "output": "\n".join(all_errors), "applied_files": []}
                else:
                    print(f"  [agent7] Non-critical aux patch skipped for {fp}: {result['output'][:200]}")
                    all_errors.append(f"[non-critical skip] {fp}: {result['output'][:300]}")
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
            # Fix A: remap .rst paths
            if target_file.endswith(".rst"):
                combined_patch = _remap_rst_path(combined_patch, repo_path)
            result = _apply_patch_with_fallbacks(repo_path, combined_patch)
            if not result["success"]:
                is_critical = _is_test_file(target_file) or target_file.endswith(".java")
                if is_critical:
                    all_errors.append(
                        f"aux patch apply failed for {target_file}: {result['output'][:500]}"
                    )
                    restore_repo_state(repo_path)
                    return {
                        "success": False,
                        "output": "\n".join(all_errors),
                        "applied_files": [],
                    }
                else:
                    print(f"  [agent7] Non-critical aux patch skipped for {target_file}")
                    all_errors.append(f"[non-critical skip] {target_file}: {result['output'][:300]}")

    return {
        "success": True,
        "output": "Developer aux hunks applied." + (
            "\nWarnings:\n" + "\n".join(all_errors) if all_errors else ""
        ),
        "applied_files": applied_files,
    }


# ──────────────────────────────────────────────────────────────────────────────
# File-operation executor (DELETED / RENAMED production Java files)
# ──────────────────────────────────────────────────────────────────────────────

def _execute_file_operations(
    repo_path: str, file_operations: list[dict[str, Any]]
) -> dict[str, Any]:
    """
    Execute filesystem-level file operations queued by Agent 1:

      RENAMED — move old_file_path → target_new_path (falls back to file_path when
                target_new_path is absent).  Content changes were already applied to
                old_file_path by the CLAW step, so we just rename the file.

      DELETED — remove file_path from disk.  Missing files are silently skipped
                (another hunk may have already removed them, or they never existed
                in this target branch).

    Renames are executed before deletions so that a file renamed and then deleted in
    the same patch is handled correctly.

    Returns {"success": True, "applied": [...], "errors": [...], "output": str}.
    File operations are non-fatal by design — failures are logged but do not abort
    the build step.  The build compiler will surface any resulting inconsistencies.
    """
    if not file_operations:
        return {"success": True, "applied": [], "errors": [], "output": ""}

    applied: list[str] = []
    errors: list[str] = []

    renames = [op for op in file_operations if op.get("operation") == "RENAMED"]
    deletions = [op for op in file_operations if op.get("operation") == "DELETED"]

    # ── Renames first ─────────────────────────────────────────────────────────
    for op in renames:
        old_rel = _norm_path(op.get("old_file_path") or "")
        # Prefer the computed target-branch destination; fall back to the mainline new path.
        new_rel = _norm_path(op.get("target_new_path") or op.get("file_path") or "")

        if not old_rel or not new_rel:
            errors.append(f"RENAME skipped: missing old_file_path or file_path in {op}")
            continue

        src = os.path.join(repo_path, old_rel)
        dst = os.path.join(repo_path, new_rel)

        if not os.path.isfile(src):
            # Source already gone (e.g. a previous attempt renamed it).
            if os.path.isfile(dst):
                applied.append(f"RENAME already done: {old_rel} → {new_rel}")
            else:
                errors.append(f"RENAME skipped (src not found): {old_rel}")
            continue

        if os.path.isfile(dst) and dst != src:
            # Destination already exists — content changes are already in src so we
            # overwrite the destination (idempotent retry).
            errors.append(
                f"RENAME dst already exists ({new_rel}); overwriting with content from {old_rel}"
            )

        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            os.replace(src, dst)  # atomic on POSIX; overwrites dst if present
            applied.append(f"RENAMED: {old_rel} → {new_rel}")
            print(f"  [agent7] file_ops: renamed {old_rel} → {new_rel}")
        except Exception as exc:
            errors.append(f"RENAME failed ({old_rel} → {new_rel}): {exc}")

    # ── Deletions ─────────────────────────────────────────────────────────────
    for op in deletions:
        rel = _norm_path(op.get("file_path") or "")
        if not rel:
            errors.append(f"DELETE skipped: missing file_path in {op}")
            continue

        abs_path = os.path.join(repo_path, rel)
        if not os.path.isfile(abs_path):
            applied.append(f"DELETE skipped (already absent): {rel}")
            continue

        try:
            os.remove(abs_path)
            applied.append(f"DELETED: {rel}")
            print(f"  [agent7] file_ops: deleted {rel}")
        except Exception as exc:
            errors.append(f"DELETE failed ({rel}): {exc}")

    output_parts = applied + ([f"ERRORS: {e}" for e in errors] if errors else [])
    return {
        "success": True,  # non-fatal — build will surface real problems
        "applied": applied,
        "errors": errors,
        "output": "\n".join(output_parts),
    }


# ──────────────────────────────────────────────────────────────────────────────
# Phase 0 baseline (used by shadow/evaluation harness)
# ──────────────────────────────────────────────────────────────────────────────

def run_phase0_baseline(
    repo_path: str,
    project: str,
    developer_aux_hunks: list[dict[str, Any]],
    file_entries: list[tuple[str, str]],
) -> dict[str, Any]:
    """
    Apply ONLY test-related hunks from developer_aux_hunks (test files only)
    to the repo at backport_commit~1, run targeted tests, then restore.

    This baseline captures the test state WITH test changes but WITHOUT the Java
    production code changes or non-Java aux changes. Tests that reference new
    APIs/methods introduced by the patch will fail here; after the agents apply
    those code changes they should pass.

    file_entries: list of (status, filepath) pairs extracted from target.patch.
    Passed directly to detect_test_targets so the helper skips its git step —
    targets are derived from the patch itself, not the worktree state.

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

    # Detect targets from the (status, path) entries extracted from target.patch.
    # Using file_entries (not --worktree) ensures phase 0 and validation targets
    # are identical regardless of current worktree state.
    changed_files = [path for _, path in file_entries]
    target_info = detect_test_targets(repo_path, project, file_entries=file_entries)
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
    # Fix H: capture error tail (javac errors) rather than Docker preamble at the top.
    diagnostic = _extract_error_tail(error_message)
    return PatchRetryContext(
        error_type=failure_category,
        error_message=diagnostic,
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

    # ── Step 2.5: Execute structural file operations (RENAMED / DELETED) ────────
    # These are queued by Agent 1 for production Java files whose mainline patch
    # contained rename or delete operations.  Renames run before deletions.
    # Content changes were already written by the CLAW step above; this step only
    # moves or removes files at the filesystem level.
    file_operations: list[dict[str, Any]] = state.get("file_operations") or []
    if file_operations:
        print(f"  agent7: executing {len(file_operations)} file operation(s)...")
        file_ops_result = _execute_file_operations(repo_path, file_operations)
        validation_results["file_operations"] = {
            "applied": file_ops_result["applied"],
            "errors": file_ops_result["errors"],
            "output": file_ops_result["output"],
        }
        if file_ops_result["errors"]:
            print(f"  agent7: file_ops warnings: {'; '.join(file_ops_result['errors'][:3])}")

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

    # ── Step 3.5: Strip unused Java imports (pre-build checkstyle guard) ────────
    # Collect all Java files written to disk during this attempt.
    synth_java_files = [
        h.get("file_path", "") for h in synthesized_hunks
        if (h.get("file_path", "") or "").endswith(".java")
    ]
    aux_java_files = [
        (h.get("file_path") or h.get("target_file") or "")
        for h in developer_aux_hunks
        if (h.get("file_path") or h.get("target_file") or "").endswith(".java")
    ]
    all_modified_java = sorted(set(synth_java_files + aux_java_files + [
        h.get("file_path", "") for h in applied_hunks
        if (h.get("file_path", "") or "").endswith(".java")
    ]) - {""})
    if all_modified_java:
        _strip_unused_java_imports(repo_path, all_modified_java)

    # ── Step 4: Build ─────────────────────────────────────────────────────────
    print(f"  agent7: running build for {project}...")
    build_res: BuildResult = run_build(repo_path, project, changed_files=all_modified_java)
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
            excerpt = _extract_error_tail(build_res.output)
            state["validation_error_context"] = (
                f"Build failed ({build_res.mode}): {excerpt}"
            )
        state["validation_failure_category"] = mapped_category
        state["validation_retry_files"] = retry_files
        state["validation_attempts"] = attempts + 1
        state["validation_results"] = validation_results
        state["retry_contexts"] = list(state.get("retry_contexts", [])) + [ctx]
        return state

    # ── Step 5: Detect test targets ───────────────────────────────────────────
    # Prefer target_patch_file_entries (status+path pairs from target.patch) so
    # the helper skips its git step and derives targets from the patch itself.
    # This guarantees phase 0 and validation always run the SAME set of tests.
    # Fall back to test files from the applied hunks if not available.
    file_entries: list[tuple[str, str]] | None = state.get("target_patch_file_entries") or None
    if file_entries:
        print(f"  agent7: using {len(file_entries)} file entries from target.patch for test targets")
    else:
        # Collect only test files from the hunks applied in this run
        all_hunk_files = {
            h.get("file_path") or h.get("target_file") or ""
            for h in (synthesized_hunks + developer_aux_hunks + applied_hunks)
        }
        test_files = [f for f in all_hunk_files if f and _is_test_file(f)]
        file_entries = [("M", f) for f in test_files]
        print(f"  agent7: using {len(file_entries)} test files from applied hunks for test targets")

    test_targets = detect_test_targets(repo_path, project, file_entries=file_entries)
    validation_results["test_target_detection"] = {
        "success": True,
        "raw": test_targets.__dict__,
    }

    # ── Step 6: Run tests ─────────────────────────────────────────────────────
    skip_test = state.get("skip_test", False)
    if skip_test:
        print(f"  agent7: skip_test is True — skipping test execution for {project}")
        test_res = TestResult(
            success=True,
            compile_error=False,
            output="Skipped tests by user request (--skip-test)",
            mode="skip",
            targets={"test_targets": test_targets.test_targets},
            test_state={},
        )
        transition_eval = {
            "valid_backport_signal": True,
            "reason": "Tests skipped (--skip-test)",
            "fail_to_pass": [],
            "newly_passing": [],
            "pass_to_fail": [],
        }
    else:
        print(
            f"  agent7: running tests for {project} "
            f"({len(test_targets.test_targets)} target(s))..."
        )
        test_res: TestResult = run_tests(
            repo_path, project, target_info=test_targets
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
        # Before failing on compile error, check if we already have a valid
        # backport signal from tests that ran before the error (e.g. newly_passing).
        if transition_eval.get("valid_backport_signal") or transition_eval.get("newly_passing") or transition_eval.get("fail_to_pass"):
            print(f"  agent7: compile error during tests but valid backport signal detected — passing.")
        else:
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
