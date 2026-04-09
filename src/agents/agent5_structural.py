"""
Agent 5: Structural Refactor (Reasoning LLM)

For TYPE IV/V patches. Handles deep structural changes where code
has been refactored between versions. Uses GumTree edit scripts,
japicmp reports, and call graphs to achieve semantic equivalence.

Uses Claude Opus for extended reasoning on complex refactorings.
"""

from typing import Dict, List, Any, Optional
from enum import Enum
from pydantic import BaseModel, Field
from src.core.state import BackportState, LocalizationResult, PatchRetryContext


class EditType(str, Enum):
    """GumTree edit operation types."""
    INSERT = "Insert"
    DELETE = "Delete"
    UPDATE = "Update"
    MOVE = "Move"


class GumTreeEdit(BaseModel):
    """Represents a single GumTree edit operation."""
    operation: EditType = Field(description="Type of edit")
    node_type: str = Field(description="AST node type (e.g., MethodDeclaration)")
    old_code: Optional[str] = Field(default=None, description="Original code snippet")
    new_code: Optional[str] = Field(default=None, description="New code snippet")
    old_line: Optional[int] = Field(default=None, description="Line in original file")
    new_line: Optional[int] = Field(default=None, description="Line in target file")


class StructuralRefactorOutput(BaseModel):
    """Output from structural refactoring."""
    refactored_code: str = Field(description="The refactored code")
    semantic_equivalence: float = Field(description="Confidence in semantic equivalence (0-1)")
    edit_summary: str = Field(description="Summary of structural changes made")
    success: bool = Field(description="Whether refactoring succeeded")
    error_message: Optional[str] = Field(description="Error if refactoring failed")


class StructuralRefactor:
    """
    Handles deep structural refactoring for TYPE IV/V patches.
    Uses reasoning-tier LLM for complex code transformations.
    """

    def __init__(self, repo_path: str, llm_client=None):
        self.repo_path = repo_path
        self.llm_client = llm_client

    def parse_gumtree_edits(self, edit_script: str) -> List[GumTreeEdit]:
        """
        Parses GumTree edit script into structured edits.

        GumTree edit script format:
        Insert parent_id node_type attributes
        Delete node_id
        Update node_id property value
        Move node_id old_parent_id new_parent_id

        Returns:
            List of GumTreeEdit objects
        """
        edits = []
        for line in edit_script.strip().splitlines():
            parts = line.split()
            if not parts:
                continue

            op = parts[0]
            if op == "Insert":
                # Insert parent_id node_type ...
                edits.append(
                    GumTreeEdit(
                        operation=EditType.INSERT,
                        node_type=parts[2] if len(parts) > 2 else "Unknown",
                        old_code=None,
                        new_code=" ".join(parts[3:]) if len(parts) > 3 else None
                    )
                )
            elif op == "Delete":
                edits.append(
                    GumTreeEdit(
                        operation=EditType.DELETE,
                        node_type="Unknown",
                        old_code=" ".join(parts[1:]) if len(parts) > 1 else None,
                        new_code=None
                    )
                )
            elif op == "Update":
                edits.append(
                    GumTreeEdit(
                        operation=EditType.UPDATE,
                        node_type="Unknown",
                        old_code=parts[2] if len(parts) > 2 else None,
                        new_code=parts[3] if len(parts) > 3 else None
                    )
                )
            elif op == "Move":
                edits.append(
                    GumTreeEdit(
                        operation=EditType.MOVE,
                        node_type="Unknown",
                        old_code=" ".join(parts[1:3]) if len(parts) > 2 else None,
                        new_code=" ".join(parts[3:]) if len(parts) > 3 else None
                    )
                )

        return edits

    def summarize_structural_changes(self, edits: List[GumTreeEdit]) -> str:
        """
        Creates a human-readable summary of structural changes.
        """
        inserts = sum(1 for e in edits if e.operation == EditType.INSERT)
        deletes = sum(1 for e in edits if e.operation == EditType.DELETE)
        updates = sum(1 for e in edits if e.operation == EditType.UPDATE)
        moves = sum(1 for e in edits if e.operation == EditType.MOVE)

        summary = f"Inserts: {inserts}, Deletes: {deletes}, Updates: {updates}, Moves: {moves}"
        return summary

    def refactor_with_llm(
        self,
        original_code: str,
        target_context: str,
        edits: List[GumTreeEdit],
        api_changes: Optional[Dict[str, str]] = None
    ) -> StructuralRefactorOutput:
        """
        Uses LLM (Reasoning tier) to refactor code for structural differences.

        Args:
            original_code: Code from the original (source) patch
            target_context: Context from target codebase
            edits: GumTree edit operations describing structural changes
            api_changes: Optional API change mappings (from japicmp)

        Returns:
            StructuralRefactorOutput with refactored code or error
        """
        if not self.llm_client:
            # Fallback: return with low confidence
            return StructuralRefactorOutput(
                refactored_code=original_code,
                semantic_equivalence=0.0,
                edit_summary=self.summarize_structural_changes(edits),
                success=False,
                error_message="LLM client not configured"
            )

        # Build prompt for LLM
        edit_summary = self.summarize_structural_changes(edits)
        prompt = self._build_refactor_prompt(
            original_code, target_context, edits, api_changes
        )

        try:
            # Call LLM with extended thinking
            # This is a placeholder; actual implementation would call LLM
            response = self._call_llm_with_reasoning(prompt)

            refactored = response.get("refactored_code", original_code)
            confidence = response.get("confidence", 0.5)

            return StructuralRefactorOutput(
                refactored_code=refactored,
                semantic_equivalence=confidence,
                edit_summary=edit_summary,
                success=True,
                error_message=None
            )
        except Exception as e:
            return StructuralRefactorOutput(
                refactored_code=original_code,
                semantic_equivalence=0.0,
                edit_summary=edit_summary,
                success=False,
                error_message=str(e)
            )

    def _build_refactor_prompt(
        self,
        original_code: str,
        target_context: str,
        edits: List[GumTreeEdit],
        api_changes: Optional[Dict[str, str]]
    ) -> str:
        """
        Builds the prompt for the reasoning LLM.
        """
        prompt = f"""
You are a Java code refactoring expert. Your task is to adapt the following patch
to work with a structurally different version of the codebase.

Original Code (from patch):
```java
{original_code}
```

Target Context (from target codebase):
```java
{target_context}
```

Structural Changes (GumTree edits):
{self._format_edits(edits)}

API Changes:
{self._format_api_changes(api_changes) if api_changes else "None detected"}

Your task:
1. Analyze the structural differences between the original and target
2. Adapt the original code to work with the target structure
3. Maintain semantic equivalence with the original logic
4. Handle any API changes indicated above

Return ONLY valid Java code that achieves semantic equivalence.
"""
        return prompt

    def _format_edits(self, edits: List[GumTreeEdit]) -> str:
        """Formats edits for prompt."""
        lines = []
        for edit in edits:
            lines.append(f"- {edit.operation.value}: {edit.node_type}")
        return "\n".join(lines) if lines else "No edits"

    def _format_api_changes(self, api_changes: Dict[str, str]) -> str:
        """Formats API changes for prompt."""
        lines = []
        for old, new in api_changes.items():
            lines.append(f"- {old} -> {new}")
        return "\n".join(lines) if lines else "None"

    def _call_llm_with_reasoning(self, prompt: str) -> Dict[str, Any]:
        """
        Calls LLM with extended reasoning (placeholder).
        In real implementation, this would use Claude Opus with extended thinking.
        """
        # Placeholder return
        return {
            "refactored_code": "// Placeholder refactored code",
            "confidence": 0.5
        }

    def refactor_hunk(
        self,
        hunk: Dict[str, Any],
        loc_result: LocalizationResult,
        edit_script: Optional[str] = None
    ) -> StructuralRefactorOutput:
        """
        Refactors a hunk for structural differences.

        Args:
            hunk: The hunk to refactor
            loc_result: Localization result with context
            edit_script: Optional GumTree edit script

        Returns:
            StructuralRefactorOutput with refactored code or error
        """
        original_code = hunk.get("old_content", "")
        target_context = loc_result.context_snapshot

        edits = []
        if edit_script:
            edits = self.parse_gumtree_edits(edit_script)

        return self.refactor_with_llm(
            original_code,
            target_context,
            edits,
            loc_result.symbol_mappings if loc_result.symbol_mappings else None
        )


def structural_refactor_agent(state: BackportState) -> BackportState:
    """
    LangGraph node: Structural Refactor.

    Processes hunks that require deep structural refactoring (TYPE IV/V).
    Uses reasoning-tier LLM for complex transformations.
    """
    repo_path = state["target_repo_path"]
    refactor = StructuralRefactor(repo_path)

    hunks = state.get("hunks", [])
    loc_results = state.get("localization_results", [])
    refactored_hunks = []
    failed_hunks = []
    retry_contexts = state.get("retry_contexts", [])

    for i, hunk in enumerate(hunks):
        if i >= len(loc_results):
            break

        loc_result = loc_results[i]

        # Check if this hunk needs structural refactoring
        # (low confidence or gumtree method indicates complex changes)
        if not (loc_result.confidence < 0.6 or loc_result.method_used == "gumtree_ast"):
            continue

        # Attempt refactoring
        output = refactor.refactor_hunk(hunk, loc_result)

        if output.success and output.semantic_equivalence > 0.5:
            refactored_hunk = {
                **hunk,
                "new_content": output.refactored_code,
                "refactored": True
            }
            refactored_hunks.append(refactored_hunk)
        else:
            failed_hunks.append(hunk)
            retry_contexts.append(
                PatchRetryContext(
                    error_type="structural_refactor_failed",
                    error_message=output.error_message or "Low semantic equivalence",
                    attempt_count=state.get("current_attempt", 1),
                    suggested_action="manual_review"
                )
            )

    state["refactored_hunks"] = refactored_hunks
    state["retry_contexts"] = retry_contexts
    state["current_attempt"] = state.get("current_attempt", 1) + 1

    return state
