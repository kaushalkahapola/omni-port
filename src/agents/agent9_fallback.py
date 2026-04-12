"""
Agent 9: Fallback Agent (Fast LLM → Balanced LLM with tools)

Triggered when the Validator (Agent 7) has failed and normal retry paths are
exhausted.  Rather than re-submitting the raw patch, this agent:

  Phase 1 — Description Builder (Fast LLM, single call)
    Analyses each mainline hunk (state["hunks"]) and produces a structured
    natural-language description of WHAT changed, WHERE, and WHY.  The raw
    unified-diff syntax is never forwarded; only semantic intent is captured.

  Phase 2 — Change Application Agent (Balanced LLM, ReAct loop, max 8 turns)
    A tool-calling agent that:
      • Receives the HunkDescriptions + LocalizationResults + validation failure
        context.
      • Uses tools (read_target_file, search_in_target_repo,
        get_class_hierarchy, get_memory_lessons) to freely explore the repo.
      • Produces CLAW-compatible old_string / new_string pairs, verifying each
        old_string exists verbatim in the target file before committing to it.
      • Only regenerates hunks for files in state["validation_retry_files"];
        successfully applied hunks are preserved unchanged.

State fields written:
  synthesized_hunks   — entries for retry-files replaced; others preserved
  hunk_descriptions   — HunkDescription list from Phase 1
  fallback_status     — "applied" | "failed"
  fallback_attempts   — incremented each time this node runs
  tokens_used         — cumulative token counter
"""

import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field

from src.core.llm_router import get_default_router, LLMTier
from src.core.state import BackportState, LocalizationResult

logger = logging.getLogger(__name__)

_PHASE2_MAX_TURNS = 8
_FILE_READ_MAX_CHARS = 12_000   # truncate very large files in tool responses
_SEARCH_MAX_RESULTS = 20


# ── Pydantic schemas ──────────────────────────────────────────────────────────

class HunkDescription(BaseModel):
    hunk_index: int = Field(description="0-based index into state['hunks']")
    file_path: str = Field(description="Target file path from localization")
    what_changed: str = Field(
        description="E.g. 'Added `timeout` parameter to execute() method'"
    )
    where_in_code: str = Field(
        description="E.g. 'Class SearchRequest, method execute(Query q)'"
    )
    intent: str = Field(
        description="E.g. 'Introduce timeout support for long-running searches'"
    )
    change_type: str = Field(
        description=(
            "One of: add_param, modify_method, add_field, modify_import, "
            "add_method, delete_method, modify_signature, add_annotation, "
            "modify_logic, other"
        )
    )
    key_symbols: List[str] = Field(
        default_factory=list,
        description="Identifiers central to the change (method/field/class names)",
    )


class DescriptionBatch(BaseModel):
    descriptions: List[HunkDescription]


class FallbackSynthesizedHunk(BaseModel):
    file_path: str
    old_string: str = Field(description="Exact string verified to exist verbatim in the file")
    new_string: str = Field(description="Replacement text")
    confidence: float = Field(ge=0.0, le=1.0)
    verified: bool = Field(description="True when old_string was confirmed in the file")


class FallbackOutput(BaseModel):
    synthesized_hunks: List[FallbackSynthesizedHunk] = Field(default_factory=list)
    failed_descriptions: List[int] = Field(
        default_factory=list,
        description="hunk_index values for changes the agent could not apply",
    )
    approach_summary: str
    tools_used: List[str] = Field(default_factory=list)


# ── Tool implementations ──────────────────────────────────────────────────────

def _get_repo_path(state: BackportState) -> str:
    return state.get("worktree_path") or state.get("target_repo_path", "")


def _tool_read_file(repo_path: str, file_path: str) -> str:
    try:
        full = Path(repo_path) / file_path
        if not full.exists():
            full = Path(file_path)
        content = full.read_text(encoding="utf-8", errors="replace")
        if len(content) > _FILE_READ_MAX_CHARS:
            return content[:_FILE_READ_MAX_CHARS] + "\n... [file truncated]"
        return content
    except Exception as exc:
        return f"ERROR: could not read {file_path}: {exc}"


def _tool_search(repo_path: str, pattern: str, glob: str) -> str:
    """Run a text search in the target repo and return formatted matches."""
    try:
        import subprocess
        args = ["grep", "-rn", "--include", glob, "-m", str(_SEARCH_MAX_RESULTS), pattern, repo_path]
        result = subprocess.run(args, capture_output=True, text=True, timeout=15)
        out = result.stdout.strip()
        if not out:
            return "No matches found."
        # Make paths relative for readability.
        lines = []
        for line in out.splitlines()[:_SEARCH_MAX_RESULTS]:
            lines.append(line.replace(repo_path + "/", "", 1))
        return "\n".join(lines)
    except Exception as exc:
        return f"ERROR: search failed: {exc}"


def _tool_get_class_hierarchy(repo_path: str, file_path: str) -> str:
    """Return the class hierarchy for a Java file via the Java microservice."""
    try:
        from src.tools.java_http_client import javaparser_find_method
        result = javaparser_find_method(repo_path, file_path, [])
        if result.get("status") == "ok":
            return json.dumps(result, indent=2)
        return f"JavaParser error: {result.get('message', 'unknown')}"
    except Exception as exc:
        return f"ERROR: {exc}"


def _tool_get_memory_lessons(repo_path: str, repo_name: str) -> str:
    """Return relevant API-rename lessons from the PatchKnowledgeIndex."""
    try:
        from src.memory.db import MemoryDB
        db_path = Path(repo_path) / "memory.db"
        if not db_path.exists():
            return "No memory database found."
        db = MemoryDB(str(db_path))
        lessons = db.get_lessons_for_repo(repo_name, limit=20)
        if not lessons:
            return "No lessons found for this repo."
        parts = []
        for lesson in lessons:
            parts.append(
                f"- {lesson.get('original_symbol', '?')} → "
                f"{lesson.get('new_symbol', '?')}: "
                f"{lesson.get('description', '')}"
            )
        return "\n".join(parts)
    except Exception as exc:
        return f"ERROR: {exc}"


# ── Tool schema for LLM ───────────────────────────────────────────────────────

_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_target_file",
            "description": (
                "Read a file from the target repository. "
                "Use this to understand the current state of code before generating changes."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Path to the file, relative to the repo root",
                    }
                },
                "required": ["file_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_in_target_repo",
            "description": (
                "Search the target repository for a text pattern using grep. "
                "Useful for finding method/class definitions or usages."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Grep regex pattern to search for",
                    },
                    "glob": {
                        "type": "string",
                        "description": "File glob filter, e.g. '*.java' or '*.xml'",
                    },
                },
                "required": ["pattern", "glob"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_class_hierarchy",
            "description": (
                "Return class hierarchy and method location information for a Java file "
                "via the Java microservice (JavaParser BFS over superclasses)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Path to the Java file, relative to the repo root",
                    }
                },
                "required": ["file_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_memory_lessons",
            "description": (
                "Retrieve known API-rename lessons from the PatchKnowledgeIndex "
                "for this repository (symbol mappings learned from past patches)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "repo_name": {
                        "type": "string",
                        "description": "Repository name, e.g. 'elasticsearch'",
                    }
                },
                "required": ["repo_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "submit_changes",
            "description": (
                "Submit the final CLAW-compatible old_string/new_string pairs. "
                "Call this ONLY after verifying every old_string exists verbatim in "
                "the corresponding file (use read_target_file to confirm). "
                "This ends the agent loop."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "synthesized_hunks": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "file_path": {"type": "string"},
                                "old_string": {"type": "string"},
                                "new_string": {"type": "string"},
                                "confidence": {"type": "number"},
                                "verified": {"type": "boolean"},
                            },
                            "required": ["file_path", "old_string", "new_string",
                                         "confidence", "verified"],
                        },
                        "description": "CLAW pairs, one per changed code block",
                    },
                    "failed_descriptions": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "hunk_index values the agent could not handle",
                    },
                    "approach_summary": {
                        "type": "string",
                        "description": "One-paragraph summary of the approach taken",
                    },
                    "tools_used": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Names of tools that were called",
                    },
                },
                "required": ["synthesized_hunks", "approach_summary"],
            },
        },
    },
]


# ── Phase 1: Description Builder ─────────────────────────────────────────────

def _build_description_prompt(
    hunks: List[Dict[str, Any]],
    loc_results: List[LocalizationResult],
) -> str:
    hunk_blocks = []
    for i, hunk in enumerate(hunks):
        fp = hunk.get("file_path", "?")
        loc = loc_results[i] if i < len(loc_results) else None
        target_fp = loc.file_path if loc else fp
        confidence = f"{loc.confidence:.2f}" if loc else "?"
        method = loc.method_used if loc else "?"
        context = (loc.context_snapshot[:300] + "...") if (loc and len(loc.context_snapshot) > 300) else (loc.context_snapshot if loc else "")

        old_c = hunk.get("old_content", "")
        new_c = hunk.get("new_content", "")

        hunk_blocks.append(
            f"=== Hunk #{i} ===\n"
            f"Source file: {fp}\n"
            f"Target file: {target_fp} (localization: {method}, confidence: {confidence})\n"
            f"Localized context in target:\n```java\n{context}\n```\n"
            f"Lines being REMOVED from mainline:\n```java\n{old_c}\n```\n"
            f"Lines being ADDED in mainline:\n```java\n{new_c}\n```"
        )

    blocks_text = "\n\n".join(hunk_blocks)

    return f"""You are analyzing Java patch hunks to produce semantic descriptions.
For each hunk below, describe the change WITHOUT quoting raw diff syntax.
Focus on: WHAT changed, WHERE in the code it happens, and WHY (the semantic intent).

{blocks_text}

Respond with a JSON object exactly matching this schema:
{{
  "descriptions": [
    {{
      "hunk_index": <int>,
      "file_path": "<target file path>",
      "what_changed": "<concise description of the change>",
      "where_in_code": "<class and method/field name>",
      "intent": "<semantic purpose of this change>",
      "change_type": "<one of: add_param|modify_method|add_field|modify_import|add_method|delete_method|modify_signature|add_annotation|modify_logic|other>",
      "key_symbols": ["<symbol1>", "<symbol2>"]
    }}
  ]
}}
"""


def _run_phase1(
    hunks: List[Dict[str, Any]],
    loc_results: List[LocalizationResult],
    router,
    tokens_used: int,
    token_tracker: Optional[Dict[str, int]] = None,
) -> Tuple[List[HunkDescription], int]:
    """Run the description-builder LLM call (Fast tier). Returns (descriptions, tokens_used)."""
    prompt = _build_description_prompt(hunks, loc_results)
    try:
        llm = router.get_model(LLMTier.FAST, tokens_used)
        response = llm.invoke(prompt)
        raw = response.content if hasattr(response, "content") else str(response)
        
        usage = getattr(response, "usage_metadata", None) or {}
        if token_tracker is not None:
            token_tracker["input"] += usage.get("input_tokens", 0)
            token_tracker["output"] += usage.get("output_tokens", 0)
            
        tokens_used += usage.get("total_tokens", len(prompt.split()) + len(raw.split()))

        # Strip markdown fences.
        clean = re.sub(r"^```(?:json)?\s*", "", raw.strip(), flags=re.MULTILINE)
        clean = re.sub(r"\s*```$", "", clean.strip(), flags=re.MULTILINE)
        data = json.loads(clean)
        batch = DescriptionBatch(**data)
        return batch.descriptions, tokens_used
    except Exception as exc:
        logger.warning("fallback agent Phase 1 failed: %s", exc)
        # Produce minimal stub descriptions so Phase 2 still has something to work with.
        stubs = []
        for i, hunk in enumerate(hunks):
            fp = loc_results[i].file_path if i < len(loc_results) else hunk.get("file_path", "?")
            stubs.append(HunkDescription(
                hunk_index=i,
                file_path=fp,
                what_changed=f"Change in {fp}",
                where_in_code="unknown",
                intent="unknown — description generation failed",
                change_type="other",
                key_symbols=[],
            ))
        return stubs, tokens_used


# ── Phase 2: Change Application Agent ────────────────────────────────────────

def _format_descriptions(descriptions: List[HunkDescription]) -> str:
    parts = []
    for d in descriptions:
        parts.append(
            f"[Hunk #{d.hunk_index}] {d.file_path}\n"
            f"  What changed : {d.what_changed}\n"
            f"  Where        : {d.where_in_code}\n"
            f"  Intent       : {d.intent}\n"
            f"  Change type  : {d.change_type}\n"
            f"  Key symbols  : {', '.join(d.key_symbols) or 'none'}"
        )
    return "\n\n".join(parts)


def _format_localization(loc_results: List[LocalizationResult], retry_files: List[str]) -> str:
    parts = []
    for i, loc in enumerate(loc_results):
        if retry_files and loc.file_path not in retry_files:
            continue
        symbol_map = ""
        if loc.symbol_mappings:
            entries = [f"    {k} → {v}" for k, v in loc.symbol_mappings.items()]
            symbol_map = "\n  Symbol mappings:\n" + "\n".join(entries)
        context_snippet = (loc.context_snapshot[:500] + "...") if len(loc.context_snapshot) > 500 else loc.context_snapshot
        parts.append(
            f"Hunk #{i}: {loc.file_path} "
            f"(method={loc.method_used}, confidence={loc.confidence:.2f}, "
            f"lines {loc.start_line}–{loc.end_line})\n"
            f"  Localized context:\n```java\n{context_snippet}\n```"
            f"{symbol_map}"
        )
    return "\n\n".join(parts) if parts else "(none)"


def _build_phase2_system_prompt(
    descriptions: List[HunkDescription],
    loc_results: List[LocalizationResult],
    validation_error_context: str,
    validation_failure_category: str,
    retry_files: List[str],
    retry_contexts: Optional[List[Dict[str, Any]]] = None,
    uncovered_retry_files: Optional[List[str]] = None,
) -> str:
    desc_text = _format_descriptions(descriptions)
    loc_text = _format_localization(loc_results, retry_files)
    retry_files_text = "\n".join(f"  - {f}" for f in retry_files) if retry_files else "  (all files)"

    # Build a condensed error history from all retry contexts.
    error_history = ""
    if retry_contexts:
        history_parts = []
        for i, ctx in enumerate(retry_contexts, 1):
            msg = (ctx.get("error_message") or "").strip()
            etype = ctx.get("error_type", "?")
            if msg:
                history_parts.append(f"Attempt {i} ({etype}):\n{msg}")
        if history_parts:
            error_history = "\n\n".join(history_parts)

    error_history_section = (
        f"\n=== FULL ERROR HISTORY (all previous attempts) ===\n{error_history}"
        if error_history else ""
    )

    # Section shown only when retry_files contains test/aux files not covered by
    # any hunk description.  The agent must READ those files to understand what
    # API they expect before touching production code.
    uncovered_section = ""
    if uncovered_retry_files:
        lines = "\n".join(f"  - {f}" for f in uncovered_retry_files)
        uncovered_section = f"""
=== TEST/AUX FILES CAUSING COMPILE ERRORS (no hunk description available) ===
The following files were applied from the developer's backport patch directly
(they are test or auxiliary files, not processed by the LLM pipeline).
They reference APIs in the production code that do not yet match what they
expect.  You MUST read each of these files first to understand what method
signatures / constants they call, then generate production-code CLAW pairs
that satisfy those call sites.  Do NOT modify the test files themselves.
{lines}
"""

    return f"""You are Agent 9 (Fallback) for OmniPort, a Java patch backporting system.

Previous automated attempts to apply these changes have failed. Your job is to
apply the changes correctly by UNDERSTANDING what needs to happen — not by
copying patch syntax.

=== WHAT NEEDS TO CHANGE ===
{desc_text}

=== WHERE THE CODE IS (localization results) ===
{loc_text}

=== WHY PREVIOUS ATTEMPT FAILED ===
Category : {validation_failure_category}
Details  : {validation_error_context or '(no details)'}
{error_history_section}{uncovered_section}
=== FILES THAT NEED ATTENTION ===
{retry_files_text}

=== YOUR WORKFLOW ===
1. Use read_target_file to read EACH file that needs changes (required — do not skip).
2. Understand the current code structure around the change location.
3. Confirm that every symbol, method, and version constant you plan to use
   actually EXISTS verbatim in the target file or is imported there.
4. Use search_in_target_repo if you need to find method/class definitions.
5. Use get_class_hierarchy if you need to understand inheritance.
6. Use get_memory_lessons to check for known API renames.
7. Generate old_string / new_string CLAW pairs:
   - old_string MUST exist verbatim in the file (verify with read_target_file).
   - new_string is the replacement text.
   - If old_string is not unique in the file, expand context until it is.
8. Before calling submit_changes, scan each file you modified for any
   **unused imports** you may have added — remove them if they are not
   referenced anywhere in the file body. CheckStyle will fail on unused imports.
9. Call submit_changes with all verified pairs.

=== CRITICAL VERSION CONSTANT RULE ===
This is a BACKPORT — the change originated from a newer branch (e.g., master)
being ported to an older release branch (e.g., 5.8.x).

When you see a version sentinel like `Version.V_5_9_0` in the change description,
DO NOT copy it blindly. Instead:
  a) Read the target file to see which `Version.V_*` constants are already used.
  b) If the constant doesn't exist in the target branch, search for the nearest
     available one (e.g., use `search_in_target_repo` with pattern `Version.V_5_`).
  c) Use the version constant that matches the current release branch, NOT the
     master/mainline constant. The correct sentinel is typically named after the
     version where this fix was introduced in the release branch (e.g., `V_5_8_4`).

=== CRITICAL RULES ===
- NEVER include an old_string you have not verified exists in the file.
- NEVER add an import that is not actually used somewhere in the file.
- NEVER fabricate code that was not part of the intended change.
- If you cannot safely apply a change, include its hunk_index in failed_descriptions.
- Brace counts in new_string must balance: every `{{` opened must be closed,
  unless old_string already opened it.
- Apply ALL hunks in the description, including both readValueFrom and writeValueTo
  if both are described — do not skip one.
"""


def _dispatch_tool(
    tool_name: str,
    tool_args: Dict[str, Any],
    repo_path: str,
    repo_name: str,
    tools_called: List[str],
) -> str:
    """Execute a tool call and return the string result."""
    tools_called.append(tool_name)

    if tool_name == "read_target_file":
        return _tool_read_file(repo_path, tool_args.get("file_path", ""))

    elif tool_name == "search_in_target_repo":
        return _tool_search(
            repo_path,
            tool_args.get("pattern", ""),
            tool_args.get("glob", "*.java"),
        )

    elif tool_name == "get_class_hierarchy":
        return _tool_get_class_hierarchy(repo_path, tool_args.get("file_path", ""))

    elif tool_name == "get_memory_lessons":
        return _tool_get_memory_lessons(repo_path, tool_args.get("repo_name", repo_name))

    elif tool_name == "submit_changes":
        # Handled by the caller — return a sentinel.
        return "__SUBMIT__"

    else:
        return f"ERROR: unknown tool '{tool_name}'"


def _run_phase2(
    descriptions: List[HunkDescription],
    loc_results: List[LocalizationResult],
    state: BackportState,
    router,
    tokens_used: int,
    retry_contexts: Optional[List[Dict[str, Any]]] = None,
    token_tracker: Optional[Dict[str, int]] = None,
    uncovered_retry_files: Optional[List[str]] = None,
) -> Tuple[FallbackOutput, int]:
    """
    Run the ReAct tool-calling loop (Balanced tier).
    Returns (FallbackOutput, updated_tokens_used).
    """
    repo_path = _get_repo_path(state)
    repo_name = Path(state.get("target_repo_path", "unknown")).name
    retry_files: List[str] = list(state.get("validation_retry_files") or [])
    validation_error_context: str = state.get("validation_error_context", "")
    validation_failure_category: str = state.get("validation_failure_category", "")

    system_prompt = _build_phase2_system_prompt(
        descriptions, loc_results,
        validation_error_context, validation_failure_category, retry_files,
        retry_contexts=retry_contexts,
        uncovered_retry_files=uncovered_retry_files,
    )

    # Enumerate which files need work so the user message is concrete.
    file_list = ", ".join(sorted(set(d.file_path for d in descriptions))) or "the relevant files"

    # Build conversation history.
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": (
            f"Please apply ALL the changes described above to: {file_list}. "
            "Start by reading each file, check version constants, verify every "
            "old_string, then submit your CLAW pairs."
        )},
    ]

    tools_called: List[str] = []
    llm = router.get_model(LLMTier.BALANCED, tokens_used)

    # Bind tools if the model supports it (LangChain ChatOpenAI).
    try:
        llm_with_tools = llm.bind_tools(_TOOLS)
    except AttributeError:
        # Fallback: model doesn't support bind_tools; use plain invoke with
        # a JSON instruction appended to the system prompt.
        llm_with_tools = llm

    submit_payload: Optional[Dict[str, Any]] = None

    for turn in range(_PHASE2_MAX_TURNS):
        try:
            response = llm_with_tools.invoke(messages)
        except Exception as exc:
            logger.warning("fallback agent Phase 2 turn %d failed: %s", turn, exc)
            break

        raw_content = response.content if hasattr(response, "content") else str(response)
        
        usage = getattr(response, "usage_metadata", None) or {}
        if token_tracker is not None:
            token_tracker["input"] += usage.get("input_tokens", 0)
            token_tracker["output"] += usage.get("output_tokens", 0)
            
        tokens_used += usage.get("total_tokens", len(str(messages).split()) // 4 + len(raw_content.split()))

        # Check for tool calls (LangChain tool_calls attribute).
        tool_calls = getattr(response, "tool_calls", None) or []

        if not tool_calls:
            # No tool calls — the model is done or has responded in text.
            # Try to extract a JSON submit_changes payload from the text.
            submit_payload = _try_extract_submit_from_text(raw_content)
            break

        # Process tool calls.
        messages.append({"role": "assistant", "content": raw_content, "tool_calls": tool_calls})

        for tc in tool_calls:
            # LangChain tool_call format: {"name": str, "args": dict, "id": str}
            tool_name = tc.get("name") or tc.get("function", {}).get("name", "")
            raw_args = tc.get("args") or tc.get("function", {}).get("arguments", "{}")
            if isinstance(raw_args, str):
                try:
                    tool_args = json.loads(raw_args)
                except json.JSONDecodeError:
                    tool_args = {}
            else:
                tool_args = raw_args

            tool_result = _dispatch_tool(tool_name, tool_args, repo_path, repo_name, tools_called)

            if tool_result == "__SUBMIT__":
                submit_payload = tool_args
                break

            # Append tool result to messages.
            messages.append({
                "role": "tool",
                "tool_call_id": tc.get("id", f"call_{turn}"),
                "name": tool_name,
                "content": tool_result,
            })

        if submit_payload is not None:
            break

    if submit_payload is None:
        logger.warning("fallback agent Phase 2: no submit_changes call after %d turns", _PHASE2_MAX_TURNS)
        return FallbackOutput(
            synthesized_hunks=[],
            failed_descriptions=list(range(len(descriptions))),
            approach_summary="Phase 2 agent did not submit any changes",
            tools_used=tools_called,
        ), tokens_used

    # Parse and validate the FallbackOutput.
    try:
        raw_hunks = submit_payload.get("synthesized_hunks", [])
        validated_hunks: List[FallbackSynthesizedHunk] = []
        for h in raw_hunks:
            # Final CLAW verification: confirm old_string actually exists in the file.
            fp = h.get("file_path", "")
            old_s = h.get("old_string", "")
            if fp and old_s:
                file_content = _tool_read_file(repo_path, fp)
                verified = old_s in file_content
            else:
                verified = False
            validated_hunks.append(FallbackSynthesizedHunk(
                file_path=fp,
                old_string=old_s,
                new_string=h.get("new_string", ""),
                confidence=float(h.get("confidence", 0.5)),
                verified=verified,
            ))

        output = FallbackOutput(
            synthesized_hunks=validated_hunks,
            failed_descriptions=submit_payload.get("failed_descriptions", []),
            approach_summary=submit_payload.get("approach_summary", ""),
            tools_used=submit_payload.get("tools_used", tools_called),
        )
        return output, tokens_used
    except Exception as exc:
        logger.warning("fallback agent Phase 2: failed to parse submit payload: %s", exc)
        return FallbackOutput(
            synthesized_hunks=[],
            failed_descriptions=list(range(len(descriptions))),
            approach_summary=f"Parse error: {exc}",
            tools_used=tools_called,
        ), tokens_used


def _try_extract_submit_from_text(text: str) -> Optional[Dict[str, Any]]:
    """
    Attempt to extract a submit_changes JSON payload from plain-text LLM output.
    Used as a fallback when the model doesn't produce structured tool calls.
    """
    # Look for a JSON block containing synthesized_hunks.
    pattern = r'\{[^{}]*"synthesized_hunks"[^{}]*\[.*?\][^{}]*\}'
    match = re.search(pattern, text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass

    # Try the whole text as JSON.
    clean = re.sub(r"^```(?:json)?\s*", "", text.strip(), flags=re.MULTILINE)
    clean = re.sub(r"\s*```$", "", clean.strip(), flags=re.MULTILINE)
    try:
        data = json.loads(clean)
        if "synthesized_hunks" in data:
            return data
    except json.JSONDecodeError:
        pass

    return None


# ── Main agent node ───────────────────────────────────────────────────────────

def fallback_agent_node(state: BackportState) -> BackportState:
    """
    LangGraph node: Agent 9 — Fallback Agent.

    Runs Phase 1 (description builder) then Phase 2 (change application with
    tools), replaces synthesized_hunks for retry-files, and increments
    fallback_attempts.
    """
    fallback_attempts: int = state.get("fallback_attempts", 0)
    tokens_used: int = state.get("tokens_used", 0)
    usage_dict = state.setdefault("llm_token_usage", {}).setdefault("agent9_fallback", {"input": 0, "output": 0})

    logger.info("fallback_agent: starting attempt %d", fallback_attempts + 1)

    router = get_default_router()

    hunks: List[Dict[str, Any]] = list(state.get("hunks") or [])
    loc_results: List[LocalizationResult] = list(state.get("localization_results") or [])
    retry_files: List[str] = list(state.get("validation_retry_files") or [])

    if not hunks:
        return {
            **state,
            "fallback_status": "failed",
            "fallback_attempts": fallback_attempts + 1,
            "hunk_descriptions": [],
        }

    # ── Phase 1: Description Builder ─────────────────────────────────────────
    descriptions, tokens_used = _run_phase1(hunks, loc_results, router, tokens_used, token_tracker=usage_dict)
    logger.info("fallback_agent: Phase 1 produced %d description(s)", len(descriptions))

    # Fix E: when retry_files is empty (common after build/checkstyle failures
    # where no javac file was isolated), default to every file in synthesized_hunks
    # so agent 9 has a concrete scope to work against.
    # EXCEPTION: if the failure category is "test_failure" and retry_files is empty,
    # that means the build succeeded but a pre-existing test regressed — a behavioural
    # regression.  Modifying production code logic in response to a behavioural
    # regression almost always makes things worse (e.g. adding imports for classes
    # that don't exist in the target branch).  Bail out immediately.
    existing_synth: List[Dict[str, Any]] = list(state.get("synthesized_hunks") or [])
    if not retry_files and state.get("validation_failure_category") == "test_failure":
        logger.info(
            "fallback_agent: skipping — empty retry_files on test_failure means "
            "behavioural regression, not a fixable compile/API issue"
        )
        return {
            **state,
            "fallback_status": "failed",
            "fallback_attempts": fallback_attempts + 1,
            "hunk_descriptions": [],
        }
    if not retry_files and existing_synth:
        retry_files = sorted(set(
            h.get("file_path", "") for h in existing_synth if h.get("file_path")
        ))
        if retry_files:
            logger.info(
                "fallback_agent: derived retry_files from synthesized_hunks: %s", retry_files
            )

    # Only pass descriptions for retry-files to Phase 2.
    # If retry_files contains test/aux files not covered by any description
    # (they were applied as developer_aux_hunks, not processed by agents 1-6),
    # we still use all descriptions for Phase 2 but explicitly expose the
    # uncovered retry files so the agent reads them and understands the API
    # they expect — rather than blindly modifying production code.
    if retry_files:
        active_descriptions = [d for d in descriptions if d.file_path in retry_files]
        if not active_descriptions:
            # retry_files are test/aux files with no corresponding description.
            # Use all descriptions so Phase 2 has full context about what the
            # production code does, but flag the uncovered files explicitly.
            active_descriptions = descriptions
            uncovered_retry_files = retry_files
        else:
            uncovered_retry_files = [
                f for f in retry_files
                if not any(d.file_path == f for d in active_descriptions)
            ]
    else:
        active_descriptions = descriptions
        uncovered_retry_files = []

    # Fix C: pull full retry_contexts history for richer error context in phase 2.
    all_retry_contexts: List[Dict[str, Any]] = [
        ctx if isinstance(ctx, dict) else (ctx.dict() if hasattr(ctx, "dict") else vars(ctx))
        for ctx in (state.get("retry_contexts") or [])
    ]

    # ── Phase 2: Change Application ──────────────────────────────────────────
    fallback_output, tokens_used = _run_phase2(
        active_descriptions, loc_results, state, router, tokens_used,
        retry_contexts=all_retry_contexts, token_tracker=usage_dict,
        uncovered_retry_files=uncovered_retry_files,
    )
    logger.info(
        "fallback_agent: Phase 2 produced %d hunk(s), %d failed",
        len(fallback_output.synthesized_hunks),
        len(fallback_output.failed_descriptions),
    )

    # ── Merge results into state ──────────────────────────────────────────────
    # Keep existing synthesized_hunks for files NOT in retry_files; replace
    # entries for retry-files with the fallback agent's output.
    existing_hunks: List[Dict[str, Any]] = list(state.get("synthesized_hunks") or [])

    if retry_files:
        kept = [h for h in existing_hunks if h.get("file_path", "") not in retry_files]
    else:
        kept = []

    new_hunks = [
        h.model_dump() for h in fallback_output.synthesized_hunks
        if h.verified
    ]
    merged_hunks = kept + new_hunks

    unverified = [h for h in fallback_output.synthesized_hunks if not h.verified]
    if unverified:
        logger.warning(
            "fallback_agent: %d hunk(s) failed CLAW verification and were dropped",
            len(unverified),
        )

    fallback_status = "applied" if new_hunks else "failed"

    return {
        **state,
        "synthesized_hunks": merged_hunks,
        "hunk_descriptions": [d.model_dump() for d in descriptions],
        "fallback_status": fallback_status,
        "fallback_attempts": fallback_attempts + 1,
        "tokens_used": tokens_used,
    }
