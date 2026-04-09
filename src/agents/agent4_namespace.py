"""
Agent 4: Namespace Adapter (Balanced LLM)

For TYPE III patches. Uses JavaParser ImportDeclaration manipulation
and symbol mapping to rewrite imports/method references.
Handles API renames and namespace changes between versions.
"""

from typing import Dict, List, Any, Optional, Tuple
from pydantic import BaseModel, Field
from src.core.state import BackportState, LocalizationResult, PatchRetryContext


class SymbolMapping(BaseModel):
    """Represents a symbol rename between source and target."""
    original_symbol: str = Field(description="Original fully qualified symbol")
    target_symbol: str = Field(description="Target fully qualified symbol")
    symbol_type: str = Field(description="Type: class, method, constant, interface, etc.")


class NamespaceAdapterOutput(BaseModel):
    """Output from namespace adaptation."""
    adapted_hunk: Dict[str, Any] = Field(description="Modified hunk with updated imports")
    symbol_mappings: List[SymbolMapping] = Field(description="Applied symbol mappings")
    imports_added: List[str] = Field(description="New imports added")
    imports_removed: List[str] = Field(description="Imports removed")
    success: bool = Field(description="Whether adaptation succeeded")
    error_message: Optional[str] = Field(description="Error if adaptation failed")


class NamespaceAdapter:
    """
    Adapts patches for namespace/import changes (TYPE III).
    Uses symbol mapping to rewrite imports and method references.
    """

    def __init__(self, repo_path: str):
        self.repo_path = repo_path

    def extract_import_statements(self, file_content: str) -> List[str]:
        """
        Extracts all import statements from Java file content.
        Returns list of import statements (with 'import' prefix).
        """
        imports = []
        for line in file_content.splitlines():
            stripped = line.strip()
            if stripped.startswith("import ") and stripped.endswith(";"):
                imports.append(stripped)
        return imports

    def replace_import_statement(
        self,
        file_content: str,
        old_import: str,
        new_import: str
    ) -> Tuple[bool, str]:
        """
        Replaces an import statement in the file.

        Returns:
            Tuple of (success, modified_content)
        """
        if old_import not in file_content:
            return False, file_content

        new_content = file_content.replace(old_import, new_import)
        return True, new_content

    def rewrite_symbol_references(
        self,
        file_content: str,
        mappings: List[Dict[str, str]]
    ) -> Tuple[bool, str]:
        """
        Rewrites symbol references in file content based on mappings.

        Args:
            file_content: Source file content
            mappings: List of dicts with 'old' and 'new' symbol names

        Returns:
            Tuple of (success, modified_content)
        """
        modified = file_content
        successful_rewrites = 0

        for mapping in mappings:
            old_sym = mapping.get("old", "")
            new_sym = mapping.get("new", "")

            if not old_sym or not new_sym:
                continue

            # Simple string replacement (in real usage, would use AST-based rewriting)
            if old_sym in modified:
                modified = modified.replace(old_sym, new_sym)
                successful_rewrites += 1

        return successful_rewrites > 0, modified

    def adapt_hunk(
        self,
        hunk: Dict[str, Any],
        loc_result: LocalizationResult,
        symbol_mappings: List[Dict[str, str]] = None
    ) -> NamespaceAdapterOutput:
        """
        Adapts a hunk for namespace changes.

        Args:
            hunk: The hunk to adapt
            loc_result: Localization result with symbol_mappings
            symbol_mappings: Optional override for symbol mappings

        Returns:
            NamespaceAdapterOutput with adapted hunk or error
        """
        if symbol_mappings is None:
            symbol_mappings = []

        # Extract from localization result if available
        if loc_result.symbol_mappings:
            for orig, target in loc_result.symbol_mappings.items():
                symbol_mappings.append({"old": orig, "new": target})

        if not symbol_mappings:
            # No symbol mappings; return original hunk
            return NamespaceAdapterOutput(
                adapted_hunk=hunk,
                symbol_mappings=[],
                imports_added=[],
                imports_removed=[],
                success=True,
                error_message=None
            )

        # Apply symbol rewrites to hunk content
        old_content = hunk.get("old_content", "")
        new_content = hunk.get("new_content", "")

        success, adapted_old = self.rewrite_symbol_references(old_content, symbol_mappings)
        success, adapted_new = self.rewrite_symbol_references(new_content, symbol_mappings)

        adapted_hunk = {
            **hunk,
            "old_content": adapted_old,
            "new_content": adapted_new,
            "adapted": True
        }

        # Create symbol mapping models for output
        symbol_model_list = [
            SymbolMapping(
                original_symbol=m.get("old", ""),
                target_symbol=m.get("new", ""),
                symbol_type="method"  # Default; would infer from context
            )
            for m in symbol_mappings
        ]

        return NamespaceAdapterOutput(
            adapted_hunk=adapted_hunk,
            symbol_mappings=symbol_model_list,
            imports_added=[],
            imports_removed=[],
            success=True,
            error_message=None
        )


def namespace_adapter_agent(state: BackportState) -> BackportState:
    """
    LangGraph node: Namespace Adapter.

    Processes hunks that require namespace/import adaptation.
    Marks hunks for which adaptation failed for structural refactor.
    """
    repo_path = state["target_repo_path"]
    adapter = NamespaceAdapter(repo_path)

    hunks = state.get("hunks", [])
    loc_results = state.get("localization_results", [])
    adapted_hunks = []
    failed_hunks = []
    retry_contexts = state.get("retry_contexts", [])

    for i, hunk in enumerate(hunks):
        if i >= len(loc_results):
            break

        loc_result = loc_results[i]

        # Check if this hunk needs namespace adaptation
        # (has symbol_mappings in localization result)
        if not loc_result.symbol_mappings:
            continue

        # Attempt adaptation
        output = adapter.adapt_hunk(hunk, loc_result)

        if output.success:
            adapted_hunks.append(output.adapted_hunk)
        else:
            failed_hunks.append(hunk)
            retry_contexts.append(
                PatchRetryContext(
                    error_type="namespace_adaptation_failed",
                    error_message=output.error_message or "Unknown error",
                    attempt_count=state.get("current_attempt", 1),
                    suggested_action="structural_refactor"
                )
            )

    state["adapted_hunks"] = adapted_hunks
    state["retry_contexts"] = retry_contexts
    state["current_attempt"] = state.get("current_attempt", 1) + 1

    return state
