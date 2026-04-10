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
    ) -> StructuralAdaptationOutput:
        if not self.llm_client:
            return StructuralAdaptationOutput(
                refactored_code=original_code,
                confidence=0.0,
                explanation="",
                success=False,
                error_message="LLM client not configured for Structural Refactor agent.",
            )

        prompt = f"""You are Agent 5 (Structural Refactor) for OmniPort, a Java patch backporting system.
The patch code below must be adapted to a target codebase that has undergone structural changes.

Original code (from patch, may not compile in target as-is):
```java
{original_code}
```

Target codebase context (surrounding code at the localized position):
```java
{target_context}
```

Structural changes detected by GumTree:
{self._format_edits(edits)}

API changes detected by japicmp:
{self._format_api_changes(api_changes)}

Task:
1. Analyze how the original code must change to achieve the same semantic effect in the target.
2. Produce refactored_code that compiles and behaves equivalently in the target codebase.
3. Preserve the original logic and intent exactly — only adapt structure and API calls.
4. Assign a confidence score (0.0–1.0) reflecting how certain you are of semantic equivalence.

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
        original_code = hunk.get("old_content", "")
        target_context = loc_result.context_snapshot

        edits = self.parse_gumtree_edits(edit_script) if edit_script else []

        return self.refactor_with_llm(
            original_code,
            target_context,
            edits,
            loc_result.symbol_mappings if loc_result.symbol_mappings else None,
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

    for i, hunk in enumerate(hunks):
        if i in processed_indices:
            continue
        if i >= len(loc_results):
            break

        loc_result = loc_results[i]
        if not _should_structural_refactor(loc_result):
            continue

        # Claim this hunk.
        processed_indices.append(i)

        output = refactor.refactor_hunk(hunk, loc_result)

        if output.success and output.confidence > 0.5:
            refactored_hunks.append({
                **hunk,
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
