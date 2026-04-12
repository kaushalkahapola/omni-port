"""
Agent 8: Syntax Repair (Balanced LLM)

Sits between HunkSynthesizer (Agent 6) and Validator (Agent 7).

For each file affected by synthesized_hunks the agent:
  1. Applies the hunks to an IN-MEMORY copy of the file (no disk writes).
  2. Calls the Java microservice /api/javaparser/parse-check endpoint to detect
     structural syntax errors (missing braces, unclosed blocks, etc.).
     Falls back to a brace-balance count when the service is unreachable.
  3. If errors are found, calls the Balanced LLM to diagnose and fix the
     specific synthesized_hunk(s) that caused the problem.
  4. Re-checks the repaired content.  Retries up to MAX_REPAIR_ATTEMPTS times
     per file, then gives up and lets the validator surface the error.

applied_hunks (Agent 3) are already on disk and therefore part of the current
file content that is read from disk — they are NOT re-applied here.

State fields written:
  synthesized_hunks    — potentially updated with repaired new_string values
  syntax_repair_status — "clean" | "repaired" | "failed" | "skipped"
  syntax_repair_attempts — cumulative repair iterations across all files
  syntax_repair_log    — per-file repair events
"""

import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field

from src.core.llm_router import get_default_router, LLMTier
from src.core.state import BackportState

logger = logging.getLogger(__name__)

MAX_REPAIR_ATTEMPTS = 2
# Only send this many lines around the error to the LLM to keep the prompt tight.
_ERROR_WINDOW_LINES = 60
# Brace imbalance threshold when microservice is unavailable.
_BRACE_IMBALANCE_THRESHOLD = 2


# ── Pydantic output schema ────────────────────────────────────────────────────

class RepairedHunk(BaseModel):
    synthesized_hunk_index: int = Field(
        description="0-based index into the synthesized_hunks list in state"
    )
    fixed_new_string: str = Field(
        description="The corrected replacement string for this hunk"
    )
    explanation: str = Field(
        description="One-sentence explanation of what was wrong and what was fixed"
    )


class SyntaxRepairOutput(BaseModel):
    file_path: str
    repaired_hunks: List[RepairedHunk] = Field(default_factory=list)
    diagnosis: str = Field(
        description="Human-readable description of the syntax problem found"
    )
    confident: bool = Field(
        description="True if the LLM is confident the repair resolves the issue"
    )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _read_file(repo_path: str, file_path: str) -> Optional[str]:
    """Read a file from disk relative to repo_path, returning None on error."""
    try:
        full = Path(repo_path) / file_path
        if not full.exists():
            # Try as absolute path
            full = Path(file_path)
        return full.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        logger.debug("syntax_repair: cannot read %s: %s", file_path, exc)
        return None


def _apply_hunks_in_memory(content: str, hunks: List[Dict[str, Any]]) -> Tuple[str, List[str]]:
    """
    Apply a list of synthesized CLAW hunks to *content* using exact-string
    replacement.  Returns (modified_content, list_of_apply_errors).
    """
    errors: List[str] = []
    for hunk in hunks:
        old_s = hunk.get("old_string", "")
        new_s = hunk.get("new_string", "")
        if not old_s:
            continue
        if old_s not in content:
            errors.append(
                f"old_string not found in {hunk.get('file_path', '?')} "
                f"(first 60 chars: {old_s[:60]!r})"
            )
            continue
        content = content.replace(old_s, new_s, 1)
    return content, errors


def _parse_check_via_service(file_content: str, file_path: str) -> Optional[Dict[str, Any]]:
    """
    Call the Java microservice parse-check endpoint.
    Returns the response dict, or None if the service is unavailable.
    """
    try:
        from src.tools.java_http_client import javaparser_parse_check
        result = javaparser_parse_check(file_content, context_path=file_path)
        # Distinguish a real "service unavailable" error from a parse result.
        if "parseable" in result:
            return result
        return None
    except Exception:
        return None


def _brace_balance_check(content: str) -> List[Dict[str, Any]]:
    """
    Fallback syntax check: count `{` vs `}` in content.
    Returns a list of one error dict if imbalanced, else [].
    """
    open_b = content.count("{")
    close_b = content.count("}")
    diff = abs(open_b - close_b)
    if diff > _BRACE_IMBALANCE_THRESHOLD:
        direction = "more `{`" if open_b > close_b else "more `}`"
        return [{
            "line": 0,
            "column": 0,
            "message": (
                f"Brace imbalance: {open_b} '{{' vs {close_b} '}}' "
                f"(off by {diff}, {direction})"
            ),
        }]
    return []


def _check_syntax(file_content: str, file_path: str) -> List[Dict[str, Any]]:
    """
    Check the syntax of *file_content*.  Returns a (possibly empty) list of
    error dicts: [{line, column, message}, ...].

    Tries the Java microservice first; falls back to brace-balance counting.
    """
    result = _parse_check_via_service(file_content, file_path)
    if result is not None:
        if result.get("parseable", True):
            return []
        return result.get("errors", [])

    logger.debug(
        "syntax_repair: microservice unavailable for %s, using brace heuristic", file_path
    )
    return _brace_balance_check(file_content)


def _build_error_window(content: str, errors: List[Dict[str, Any]], window: int) -> str:
    """
    Return a subset of *content* centred around the first error location.
    Falls back to the full content when line information is missing.
    """
    if not errors:
        return content
    first_line = errors[0].get("line", 0)
    if first_line <= 0:
        # No location info — return the full content (up to 200 lines).
        lines = content.splitlines()
        return "\n".join(lines[:200]) + ("\n... [truncated]" if len(lines) > 200 else "")

    lines = content.splitlines()
    start = max(0, first_line - window // 2 - 1)
    end = min(len(lines), first_line + window // 2)
    excerpt = lines[start:end]
    prefix = f"[lines {start + 1}–{end}]\n"
    return prefix + "\n".join(excerpt)


def _format_errors(errors: List[Dict[str, Any]]) -> str:
    parts = []
    for e in errors:
        line = e.get("line", 0)
        col = e.get("column", 0)
        msg = e.get("message", "?")
        parts.append(f"  Line {line}, col {col}: {msg}")
    return "\n".join(parts) if parts else "  (no error details)"


def _build_repair_prompt(
    file_path: str,
    modified_content: str,
    errors: List[Dict[str, Any]],
    file_hunks: List[Tuple[int, Dict[str, Any]]],  # [(global_index, hunk), ...]
    mainline_hunks: List[Dict[str, Any]],           # original state["hunks"] for intent
) -> str:
    error_window = _build_error_window(modified_content, errors, _ERROR_WINDOW_LINES)
    error_text = _format_errors(errors)

    hunk_block = ""
    for global_idx, sh in file_hunks:
        old_s = sh.get("old_string", "")
        new_s = sh.get("new_string", "")
        hunk_block += (
            f"\n--- Synthesized hunk #{global_idx} ---\n"
            f"old_string (what was replaced):\n```java\n{old_s}\n```\n"
            f"new_string (replacement applied):\n```java\n{new_s}\n```\n"
        )

    # Collect mainline hunks that target this file for intent context.
    mainline_block = ""
    for mh in mainline_hunks:
        if mh.get("file_path", "") == file_path:
            mainline_block += (
                f"\nMainline intent (DO NOT copy syntax literally — for understanding only):\n"
                f"old_content:\n```java\n{mh.get('old_content', '')}\n```\n"
                f"new_content:\n```java\n{mh.get('new_content', '')}\n```\n"
            )

    return f"""You are Agent 8 (Syntax Repair) for a Java patch backporting system.

A set of synthesized CLAW hunks was applied to `{file_path}` and the result
has Java syntax errors. Your task is to fix the synthesized_hunk(s) that
caused the problem.

=== SYNTAX ERRORS ===
{error_text}

=== FILE CONTENT AROUND ERROR ===
```java
{error_window}
```

=== SYNTHESIZED HUNKS THAT WERE APPLIED ===
(These are the hunks applied to produce the file content above.)
{hunk_block}
{mainline_block}
=== YOUR TASK ===
Identify which synthesized hunk(s) introduced the syntax error (typically a
missing or extra `{{` / `}}`, or a broken statement).  For each broken hunk
output a fixed `new_string`.

Rules:
- The fixed new_string must preserve the intent of the original change.
- Do NOT re-introduce old_string content or copy mainline syntax literally.
- Ensure brace counts in new_string match those in the corresponding old_string
  (i.e. each `{{` opened in new_string must be closed in new_string unless
  old_string already opened it).
- If no hunk needs fixing (e.g. the error is in an unrelated part of the file),
  return an empty repaired_hunks list and set confident=false.

Respond with a JSON object matching this schema exactly:
{{
  "file_path": "{file_path}",
  "diagnosis": "<one sentence describing the root cause>",
  "confident": true | false,
  "repaired_hunks": [
    {{
      "synthesized_hunk_index": <int — the global index from above>,
      "fixed_new_string": "<corrected replacement text>",
      "explanation": "<one sentence>"
    }}
  ]
}}
"""


def _parse_llm_repair_response(content: str) -> Optional[SyntaxRepairOutput]:
    """Extract and validate the JSON block from the LLM response."""
    # Strip markdown fences if present.
    clean = re.sub(r"^```(?:json)?\s*", "", content.strip(), flags=re.MULTILINE)
    clean = re.sub(r"\s*```$", "", clean.strip(), flags=re.MULTILINE)
    try:
        import json
        data = json.loads(clean)
        return SyntaxRepairOutput(**data)
    except Exception as exc:
        logger.debug("syntax_repair: failed to parse LLM response: %s\nraw=%s", exc, content[:300])
        return None


# ── Main agent node ───────────────────────────────────────────────────────────

def syntax_repair_agent(state: BackportState) -> BackportState:
    """
    LangGraph node: Agent 8 — Syntax Repair.

    Applies synthesized_hunks in-memory, checks each modified file for Java
    syntax errors, and repairs broken hunks via LLM when needed.
    """
    synthesized_hunks: List[Dict[str, Any]] = list(state.get("synthesized_hunks") or [])
    if not synthesized_hunks:
        return {
            **state,
            "syntax_repair_status": "skipped",
            "syntax_repair_attempts": 0,
            "syntax_repair_log": [],
        }

    repo_path: str = state.get("worktree_path") or state.get("target_repo_path", "")
    mainline_hunks: List[Dict[str, Any]] = list(state.get("hunks") or [])

    # Group synthesized_hunks by file_path, preserving global indices.
    file_to_hunks: Dict[str, List[Tuple[int, Dict[str, Any]]]] = {}
    for idx, sh in enumerate(synthesized_hunks):
        fp = sh.get("file_path", "")
        if fp:
            file_to_hunks.setdefault(fp, []).append((idx, sh))

    log: List[Dict[str, Any]] = []
    total_repair_attempts = 0
    any_repaired = False
    any_failed = False

    router = get_default_router()
    tokens_used: int = state.get("tokens_used", 0)

    for file_path, indexed_hunks in file_to_hunks.items():
        file_content = _read_file(repo_path, file_path)
        if file_content is None:
            log.append({
                "file_path": file_path,
                "outcome": "skipped",
                "reason": "file not readable",
            })
            continue

        # Apply all hunks for this file in-memory.
        local_hunks = [sh for _, sh in indexed_hunks]
        modified_content, apply_errors = _apply_hunks_in_memory(file_content, local_hunks)

        if apply_errors:
            log.append({
                "file_path": file_path,
                "outcome": "skipped",
                "reason": f"apply errors: {apply_errors}",
            })
            continue

        # Check syntax.
        errors = _check_syntax(modified_content, file_path)
        if not errors:
            log.append({"file_path": file_path, "outcome": "clean"})
            continue

        logger.info(
            "syntax_repair: %d error(s) in %s — attempting LLM repair", len(errors), file_path
        )

        # Repair loop.
        repaired_this_file = False
        for attempt in range(MAX_REPAIR_ATTEMPTS):
            total_repair_attempts += 1

            prompt = _build_repair_prompt(
                file_path, modified_content, errors, indexed_hunks, mainline_hunks
            )

            try:
                llm = router.get_model(LLMTier.BALANCED, tokens_used)
                response = llm.invoke(prompt)
                raw = response.content if hasattr(response, "content") else str(response)
                # Rough token tracking.
                tokens_used += len(prompt.split()) + len(raw.split())
            except Exception as exc:
                logger.warning("syntax_repair: LLM call failed: %s", exc)
                break

            repair_output = _parse_llm_repair_response(raw)
            if repair_output is None or not repair_output.repaired_hunks:
                logger.debug("syntax_repair: no repaired_hunks in LLM response")
                break

            # Apply the fixes to synthesized_hunks.
            for rh in repair_output.repaired_hunks:
                idx = rh.synthesized_hunk_index
                if 0 <= idx < len(synthesized_hunks):
                    synthesized_hunks[idx] = {
                        **synthesized_hunks[idx],
                        "new_string": rh.fixed_new_string,
                    }

            # Re-apply the updated hunks and re-check.
            updated_local = [synthesized_hunks[i] for i, _ in indexed_hunks]
            modified_content, _ = _apply_hunks_in_memory(file_content, updated_local)
            errors = _check_syntax(modified_content, file_path)

            if not errors:
                repaired_this_file = True
                any_repaired = True
                logger.info(
                    "syntax_repair: %s repaired on attempt %d", file_path, attempt + 1
                )
                log.append({
                    "file_path": file_path,
                    "outcome": "repaired",
                    "attempts": attempt + 1,
                    "diagnosis": repair_output.diagnosis,
                })
                break

        if not repaired_this_file:
            any_failed = True
            log.append({
                "file_path": file_path,
                "outcome": "failed",
                "attempts": total_repair_attempts,
                "remaining_errors": errors,
            })
            logger.warning(
                "syntax_repair: could not repair %s after %d attempt(s); "
                "passing to validator",
                file_path,
                total_repair_attempts,
            )

    if any_failed:
        status = "failed"
    elif any_repaired:
        status = "repaired"
    else:
        status = "clean"

    updates: Dict[str, Any] = {
        "synthesized_hunks": synthesized_hunks,
        "syntax_repair_status": status,
        "syntax_repair_attempts": total_repair_attempts,
        "syntax_repair_log": log,
        "tokens_used": tokens_used,
    }
    return {**state, **updates}
