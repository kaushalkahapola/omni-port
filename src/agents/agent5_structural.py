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

        prompt = f"""You are Agent 5 (Structural Refactor) for OmniPort, a Java patch backporting system.
The target-branch code below must be structurally refactored to match the intent of a
mainline patch.

Current code in the TARGET file (this is what refactored_code will REPLACE verbatim):
```java
{original_code}
```
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
"""

        structured_llm = self.llm_client.with_structured_output(StructuralAdaptationOutput)
        try:
            result: StructuralAdaptationOutput = structured_llm.invoke(prompt)
            return result
        except Exception as e:
            return StructuralAdaptationOutput(
                refactored_code=original_code,
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
    ) -> StructuralAdaptationOutput:
        # original_code is the TARGET's current code (context_snapshot), since Agent 5
        # produces refactored_code that replaces it verbatim in the target file.
        # intended_new_code is the mainline's new_content — it shows the structural
        # intent (e.g. remove if/else) that must be applied to the target version.
        original_code = loc_result.context_snapshot
        target_context = loc_result.context_snapshot
        intended_new_code = hunk.get("new_content", "") or None

        edits = self.parse_gumtree_edits(edit_script) if edit_script else []

        return self.refactor_with_llm(
            original_code,
            target_context,
            edits,
            loc_result.symbol_mappings if loc_result.symbol_mappings else None,
            intended_new_code=intended_new_code,
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
        # Skip hunks where localization found no file at all — Agent 5 needs at
        # least a file path and context snapshot to reason about structure.
        if loc_result.method_used == "failed" or not loc_result.file_path:
            continue
        # Process if routing criteria met OR if Agent 4 explicitly escalated this hunk.
        if not _should_structural_refactor(loc_result) and i not in escalation_indices:
            continue

        # Claim this hunk.
        processed_indices.append(i)

        output = refactor.refactor_hunk(hunk, loc_result)

        if output.success and output.confidence > 0.5:
            refactored_hunks.append({
                **hunk,
                # old_content = target's CURRENT code (context_snapshot), so Agent 6
                # can find it verbatim in the target file for CLAW pair construction.
                # new_content = Agent 5's refactored replacement.
                "old_content": loc_result.context_snapshot,
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
