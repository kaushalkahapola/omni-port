"""
Agent 5: Structural Refactor (Reasoning LLM)

Routed when:
  - localization method is 'gumtree_ast', OR
  - localization confidence < 0.6

Uses the Reasoning-tier LLM with GumTree edit scripts, japicmp API change
reports, and target context to achieve semantic equivalence across structurally
divergent codebases.
"""

from typing import Dict, List, Any, Optional
from enum import Enum
from pydantic import BaseModel, Field
from src.core.state import BackportState, LocalizationResult, PatchRetryContext
from src.core.llm_router import get_default_router, LLMTier

_STRUCTURAL_METHODS = {"gumtree_ast"}
_LOW_CONFIDENCE_THRESHOLD = 0.6


# ── Data models ───────────────────────────────────────────────────────────────

class EditType(str, Enum):
    INSERT = "Insert"
    DELETE = "Delete"
    UPDATE = "Update"
    MOVE = "Move"


class GumTreeEdit(BaseModel):
    operation: EditType
    node_type: str
    old_code: Optional[str] = None
    new_code: Optional[str] = None
    old_line: Optional[int] = None
    new_line: Optional[int] = None


class StructuralAdaptationOutput(BaseModel):
    """Structured output from the Reasoning LLM."""
    refactored_code: str = Field(
        description="The adapted Java code that achieves semantic equivalence in the target codebase"
    )
    confidence: float = Field(
        description="Confidence in semantic equivalence (0.0–1.0)"
    )
    explanation: str = Field(
        description="Brief explanation of the structural changes made"
    )
    success: bool = Field(description="Whether refactoring succeeded")
    error_message: Optional[str] = Field(default=None)


# ── Core class ────────────────────────────────────────────────────────────────

class StructuralRefactor:
    def __init__(self, repo_path: str, llm_client=None):
        self.repo_path = repo_path
        self.llm_client = llm_client

    # ── GumTree helpers ───────────────────────────────────────────────────────

    def parse_gumtree_edits(self, edit_script: str) -> List[GumTreeEdit]:
        edits = []
        for line in edit_script.strip().splitlines():
            parts = line.split()
            if not parts:
                continue
            op = parts[0]
            if op == "Insert":
                edits.append(GumTreeEdit(
                    operation=EditType.INSERT,
                    node_type=parts[2] if len(parts) > 2 else "Unknown",
                    new_code=" ".join(parts[3:]) if len(parts) > 3 else None,
                ))
            elif op == "Delete":
                edits.append(GumTreeEdit(
                    operation=EditType.DELETE,
                    node_type="Unknown",
                    old_code=" ".join(parts[1:]) if len(parts) > 1 else None,
                ))
            elif op == "Update":
                edits.append(GumTreeEdit(
                    operation=EditType.UPDATE,
                    node_type="Unknown",
                    old_code=parts[2] if len(parts) > 2 else None,
                    new_code=parts[3] if len(parts) > 3 else None,
                ))
            elif op == "Move":
                edits.append(GumTreeEdit(
                    operation=EditType.MOVE,
                    node_type="Unknown",
                    old_code=" ".join(parts[1:3]) if len(parts) > 2 else None,
                    new_code=" ".join(parts[3:]) if len(parts) > 3 else None,
                ))
        return edits

    def _format_edits(self, edits: List[GumTreeEdit]) -> str:
        if not edits:
            return "No GumTree edits available."
        return "\n".join(
            f"- {e.operation.value}: {e.node_type}"
            + (f" ({e.old_code} -> {e.new_code})" if e.old_code and e.new_code else "")
            for e in edits
        )

    def _format_api_changes(self, api_changes: Optional[Dict[str, str]]) -> str:
        if not api_changes:
            return "None detected."
        return "\n".join(f"- {old} -> {new}" for old, new in api_changes.items())

    # ── LLM call ──────────────────────────────────────────────────────────────

    def refactor_with_llm(
        self,
        original_code: str,
        target_context: str,
        edits: List[GumTreeEdit],
        api_changes: Optional[Dict[str, str]] = None,
        intended_new_code: Optional[str] = None,
        same_file_applied_hunks: Optional[List[Dict[str, Any]]] = None,
        pre_region_context: str = "",
        post_region_context: str = "",
    ) -> StructuralAdaptationOutput:
        if not self.llm_client:
            return StructuralAdaptationOutput(
                refactored_code=original_code,
                confidence=0.0,
                explanation="",
                success=False,
                error_message="LLM client not configured for Structural Refactor agent.",
            )

        intended_section = ""
        if intended_new_code and intended_new_code.strip():
            intended_section = f"""
Intended new code (mainline version after the patch — shows WHAT structural change to apply,
but may use mainline API names that differ from the target branch):
```java
{intended_new_code}
```

"""

        pre_context_section = ""
        if pre_region_context.strip():
            pre_context_section = f"""
Code immediately BEFORE the localized region in the target file (already exists — do NOT remove unless it is part of the block being replaced):
```java
{pre_region_context}
```
"""

        post_context_section = ""
        if post_region_context.strip():
            post_context_section = f"""
Code immediately AFTER the replaced region in the target file (already exists — do NOT recreate):
```java
{post_region_context}
```
"""

        same_file_section = ""
        if same_file_applied_hunks:
            parts = []
            for h in same_file_applied_hunks:
                old_lines = [l for l in h.get("old_content", "").splitlines() if l.strip()]
                new_lines = [l for l in h.get("new_content", "").splitlines() if l.strip()]
                old_only = set(old_lines) - set(new_lines)
                if old_only:
                    removed = "\n".join(f"  - {l}" for l in sorted(old_only))
                    parts.append(f"Removed:\n{removed}")
            if parts:
                same_file_section = (
                    "\n⚠️  Other changes ALREADY APPLIED to this same file (Agent 3 fast-apply):\n"
                    "Your refactored_code MUST be compatible. "
                    "Do NOT call methods, use fields, or reference symbols listed under 'Removed' — "
                    "they no longer exist in the file.\n\n"
                    + "\n\n".join(parts)
                    + "\n"
                )

        prompt = f"""You are Agent 5 (Structural Refactor) for OmniPort, a Java patch backporting system.
{same_file_section}
The target-branch code below must be structurally refactored to match the intent of a
mainline patch.

Current code in the TARGET file (this is what refactored_code will REPLACE verbatim):
```java
{original_code}
```
{pre_context_section}{post_context_section}
{intended_section}
Structural changes detected by GumTree:
{self._format_edits(edits)}

API changes detected by japicmp:
{self._format_api_changes(api_changes)}

Task:
1. Understand the structural change shown in the "Intended new code" (e.g. removing an
   if/else version check, inlining a branch, simplifying a method body).
2. Apply the SAME structural change to the TARGET's code shown above.
   The target may use different field/method names than the mainline — use the TARGET's
   names throughout (e.g. if the target code shows `barMap` where the mainline has `fooMap`,
   use `barMap`).
3. Produce refactored_code that replaces the "Current code in the TARGET file" block
   exactly and compiles correctly in the target codebase.
4. Assign a confidence score (0.0–1.0) reflecting how certain you are of semantic equivalence.

CRITICAL: refactored_code will be substituted verbatim for the TARGET code shown above.
It must use only identifiers that exist in the target branch (no mainline-specific names).
Return ONLY valid Java code in refactored_code.
Format your response EXACTLY as follows:

# Explanation
<your reasoning here>

# Confidence
<a float between 0.0 and 1.0>

# Search/Replace Block
<<<<
<exact code to replace from the TARGET file>
====
<refactored replacement code>
>>>>
"""

        try:
            response = self.llm_client.invoke(prompt)
            content = response.content if hasattr(response, "content") else str(response)
            
            explanation = ""
            confidence = 0.0
            
            import re
            expl_match = re.search(r'# Explanation\n(.*?)(?=# Confidence|# Search/Replace Block|<<<<)', content, re.DOTALL)
            if expl_match:
                explanation = expl_match.group(1).strip()
                
            conf_match = re.search(r'# Confidence\n(.*?)(?=# Search/Replace Block|<<<<)', content, re.DOTALL)
            if conf_match:
                nums = re.findall(r'0\.\d+|1\.0|0\.0', conf_match.group(1))
                if nums:
                    confidence = float(nums[0])
                    
            block_match = re.search(r'<<<<\n(.*?)\n====\n(.*?)>>>>', content, re.DOTALL)
            if not block_match:
                return StructuralAdaptationOutput(
                    refactored_code=original_code,
                    confidence=0.0,
                    explanation=content,
                    success=False,
                    error_message="Failed to parse Aider SEARCH/REPLACE block `<<<< ==== >>>>`",
                )
                
            new_content = block_match.group(2).strip("\n")
            return StructuralAdaptationOutput(
                refactored_code=new_content,
                confidence=confidence,
                explanation=explanation,
                success=True,
                error_message=None
            )
        except Exception as e:
            return StructuralAdaptationOutput(
                refactored_code=original_code,
                confidence=0.0,
                explanation="",
                success=False,
                error_message=str(e),
            )

    def rewrite_from_target_skeleton(
        self,
        hunk: Dict[str, Any],
        target_file_content: str,
        file_path: str,
    ) -> StructuralAdaptationOutput:
        """
        Fix B fallback: called when localization returned confidence=0 (method_used=failed).
        Sends the FULL target file (up to 8000 chars) to the LLM and asks it to find the
        right location and emit a JSON {old_string, new_string} pair for CLAW replacement.
        The result is stored in refactored_code as a JSON string that agent6 will parse.
        """
        if not self.llm_client:
            return StructuralAdaptationOutput(
                refactored_code="",
                confidence=0.0,
                explanation="",
                success=False,
                error_message="LLM client not configured.",
            )

        # Truncate very large files so the prompt fits in context.
        snippet = target_file_content[:8000]
        if len(target_file_content) > 8000:
            snippet += "\n// ... [file truncated] ..."

        new_code = hunk.get("new_content", "").strip()
        old_code = hunk.get("old_content", "").strip()

        prompt = f"""You are Agent 5 (Structural Refactor) for a Java patch backporting system.

Localization failed for the following hunk — we could not find the exact code snippet in the
target branch. Your task is to:
1. Read the TARGET file below and find where this change should be applied.
2. Output a Search/Replace block that captures the exact lines to be replaced, and the new lines to substitute.

Format your response EXACTLY as follows:

# Explanation
<your reasoning here>

# Confidence
<0.0 to 1.0>

# Search/Replace Block
<<<<
<the exact text from the TARGET file that should be replaced>
====
<the replacement text>
>>>>

Mainline old code (what was removed/changed in the mainline commit):
```java
{old_code}
```

Mainline new code (what was added/changed in the mainline commit):
```java
{new_code}
```

TARGET file ({file_path}):
```java
{snippet}
```

IMPORTANT:
- The text between <<<< and ==== MUST be an exact substring of the target file shown above.
- The text between ==== and >>>> should apply the same semantic change but using the target branch's identifiers.
- CRITICAL: Your <<<< block MUST be globally unique within the target file. If you are replacing a generic line like `}} return null;`, you must include enough surrounding lines (e.g. 3-4 lines before and after) to guarantee the text only appears exactly once in the file.
- If you cannot find a safe replacement point, leave the blocks empty.
"""
        try:
            response = self.llm_client.invoke(prompt)
            content = response.content if hasattr(response, "content") else str(response)
            
            import re
            block_match = re.search(r'<<<<\n(.*?)\n====\n(.*?)>>>>', content, re.DOTALL)
            
            if not block_match:
                return StructuralAdaptationOutput(
                    refactored_code="",
                    confidence=0.0,
                    explanation=content,
                    success=False,
                    error_message="Failed to parse Aider SEARCH/REPLACE block `<<<< ==== >>>>`",
                )
                
            old_str = block_match.group(1).rstrip()
            new_str = block_match.group(2).strip("\n")
            
            import json
            json_str = json.dumps({"old_string": old_str, "new_string": new_str})
            
            return StructuralAdaptationOutput(
                refactored_code=json_str,  # Agent 6 parses this JSON!
                confidence=0.6,
                success=True,
                error_message=None,
            )
        except Exception as e:
            return StructuralAdaptationOutput(
                refactored_code="",
                confidence=0.0,
                explanation="",
                success=False,
                error_message=str(e),
            )

    def refactor_hunk(
        self,
        hunk: Dict[str, Any],
        loc_result: LocalizationResult,
        edit_script: Optional[str] = None,
        same_file_applied_hunks: Optional[List[Dict[str, Any]]] = None,
        pre_region_context: str = "",
        post_region_context: str = "",
    ) -> StructuralAdaptationOutput:
        # original_code is the TARGET's current code (context_snapshot), since Agent 5
        # produces refactored_code that replaces it verbatim in the target file.
        # intended_new_code is the mainline's new_content — it shows the structural
        # intent (e.g. remove if/else) that must be applied to the target version.
        #
        # IMPORTANT: context_snapshot was captured at localization time (before Agent 3
        # applied any fast-apply hunks). If Agent 3 modified the same file, the snapshot
        # may no longer be verbatim in the file. Re-read the current file to get the
        # actual current state of the localized region — Agent 6 needs to find original_code
        # verbatim when it applies the CLAW replacement.
        original_code = loc_result.context_snapshot  # fallback if file read fails
        from pathlib import Path
        target_path = Path(self.repo_path) / loc_result.file_path
        if target_path.exists() and loc_result.start_line > 0 and loc_result.end_line > 0:
            try:
                file_lines = target_path.read_text(encoding="utf-8").splitlines(keepends=True)
                start = max(0, loc_result.start_line - 1)
                end = min(len(file_lines), loc_result.end_line)
                current_region = "".join(file_lines[start:end])
                if current_region.strip():
                    original_code = current_region
            except (IOError, UnicodeDecodeError):
                pass

        intended_new_code = hunk.get("new_content", "") or None

        edits = self.parse_gumtree_edits(edit_script) if edit_script else []

        return self.refactor_with_llm(
            original_code,
            original_code,  # target_context == original_code (current file state)
            edits,
            loc_result.symbol_mappings if loc_result.symbol_mappings else None,
            intended_new_code=intended_new_code,
            same_file_applied_hunks=same_file_applied_hunks,
            pre_region_context=pre_region_context,
            post_region_context=post_region_context,
        )


# ── LangGraph node ────────────────────────────────────────────────────────────

def _should_structural_refactor(loc_result: LocalizationResult) -> bool:
    return (
        loc_result.method_used in _STRUCTURAL_METHODS
        or loc_result.confidence < _LOW_CONFIDENCE_THRESHOLD
    )


def structural_refactor_agent(state: BackportState) -> BackportState:
    """
    LangGraph node: Structural Refactor.

    Processes hunks requiring deep structural adaptation (gumtree_ast method or
    confidence < 0.6). Skips hunks already claimed by Agents 3 or 4.
    Passes refactored hunks to Agent 6 (Hunk Synthesizer) for CLAW verification.
    """
    repo_path = state["target_repo_path"]

    # Inject the Reasoning-tier LLM client.
    router = get_default_router()
    llm_client = router.get_model(LLMTier.REASONING)
    refactor = StructuralRefactor(repo_path, llm_client)

    hunks = state.get("hunks", [])
    loc_results = state.get("localization_results", [])
    processed_indices: List[int] = list(state.get("processed_hunk_indices", []))
    refactored_hunks: List[Dict[str, Any]] = list(state.get("refactored_hunks", []))
    failed_hunks: List[Dict[str, Any]] = list(state.get("failed_hunks", []))
    retry_contexts: List[PatchRetryContext] = list(state.get("retry_contexts", []))
    tokens_used: int = state.get("tokens_used", 0)
    # Hunks that Agent 4 explicitly escalated (e.g. structural refactoring disguised
    # as a namespace change — Agent 4 detected empty adapted_new_content for a
    # non-pure-removal hunk and deferred to Agent 5).
    escalation_indices: List[int] = list(state.get("structural_escalation_indices", []))

    for i, hunk in enumerate(hunks):
        if i in processed_indices:
            continue
        if i >= len(loc_results):
            break

        loc_result = loc_results[i]
        # Skip hunks where localization found no file at all, or new-file hunks
        # (handled by Agent 6's new-file path).
        if loc_result.method_used in ("failed", "new_file") or not loc_result.file_path:
            continue
        # Process if routing criteria met OR if Agent 4 explicitly escalated this hunk.
        if not _should_structural_refactor(loc_result) and i not in escalation_indices:
            continue

        # Claim this hunk.
        processed_indices.append(i)

        # Collect other hunks on the same file already applied by Agent 3,
        # so the LLM knows which symbols/methods are no longer available.
        current_file = loc_result.file_path
        same_file_applied: List[Dict[str, Any]] = [
            hunks[j]
            for j in processed_indices
            if j != i and j < len(hunks) and hunks[j].get("file_path") == current_file
        ]
        # Fetch 30 lines of context before and after from the target file.
        pre_region_context = ""
        post_region_context = ""
        current_file_region = loc_result.context_snapshot  # fallback
        from pathlib import Path
        target_path = Path(repo_path) / loc_result.file_path
        if target_path.exists():
            try:
                lines = target_path.read_text(encoding="utf-8").splitlines(keepends=True)
                if loc_result.start_line > 0:
                    pre_start = max(0, loc_result.start_line - 31)
                    pre_end = loc_result.start_line - 1
                    pre_region_context = "".join(lines[pre_start:pre_end])

                    # Current file region: used as old_content so Agent 6 can find it.
                    # This is the post-Agent3 state — may differ from context_snapshot.
                    start = max(0, loc_result.start_line - 1)
                    end = min(len(lines), loc_result.end_line) if loc_result.end_line > 0 else start + 1
                    region = "".join(lines[start:end])
                    if region.strip():
                        current_file_region = region

                if loc_result.end_line > 0:
                    post_start = loc_result.end_line
                    post_end = min(len(lines), post_start + 30)
                    post_region_context = "".join(lines[post_start:post_end])
            except (IOError, UnicodeDecodeError):
                pass

        output = refactor.refactor_hunk(
            hunk, loc_result,
            same_file_applied_hunks=same_file_applied,
            pre_region_context=pre_region_context,
            post_region_context=post_region_context
        )

        if output.success and output.confidence > 0.5:
            refactored_hunks.append({
                **hunk,
                # old_content = current file region (post-Agent3 state), so Agent 6
                # can find it verbatim in the target file for CLAW pair construction.
                # new_content = Agent 5's refactored replacement.
                "old_content": current_file_region,
                "new_content": output.refactored_code,
                "refactored": True,
                "semantic_confidence": output.confidence,
                "loc_index": i,
            })
        else:
            failed_hunks.append({
                **hunk,
                "error": output.error_message or f"Low semantic confidence: {output.confidence:.2f}",
            })
            retry_contexts.append(
                PatchRetryContext(
                    error_type="structural_refactor_failed",
                    error_message=output.error_message or f"confidence={output.confidence:.2f}",
                    attempt_count=state.get("current_attempt", 1),
                    suggested_action="manual_review",
                )
            )

    state["refactored_hunks"] = refactored_hunks
    state["failed_hunks"] = failed_hunks
    state["processed_hunk_indices"] = processed_indices
    state["retry_contexts"] = retry_contexts
    state["tokens_used"] = tokens_used
    state["current_attempt"] = state.get("current_attempt", 1) + 1
    return state
