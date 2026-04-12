from typing import TypedDict, List, Dict, Optional, Any
from enum import Enum
from pydantic import BaseModel, Field


class PatchType(str, Enum):
    TYPE_I = "TYPE_I"
    TYPE_II = "TYPE_II"
    TYPE_III = "TYPE_III"
    TYPE_IV = "TYPE_IV"
    TYPE_V = "TYPE_V"
    MIXED = "MIXED"


class PatchClassification(BaseModel):
    patch_type: PatchType = Field(
        description="The complexity classification of the patch"
    )
    confidence: float = Field(description="Confidence score between 0.0 and 1.0")
    token_budget_estimate: int = Field(
        description="Estimated token budget required for this patch"
    )
    reasoning: str = Field(description="Explanation for the classification")
    is_auto_generated: bool = Field(
        default=False, description="True if the target file is auto-generated"
    )


class LocalizationResult(BaseModel):
    method_used: str = Field(
        description="The localization method used (e.g., git, fuzzy, gumtree, javaparser, embedding)"
    )
    confidence: float = Field(description="Confidence score for localization")
    context_snapshot: str = Field(
        description="The localized code snippet from the target codebase"
    )
    symbol_mappings: Dict[str, str] = Field(
        default_factory=dict,
        description="Mapping of original symbols to target symbols",
    )
    file_path: str = Field(description="Target file path")
    start_line: int = Field(description="Start line in target file")
    end_line: int = Field(description="End line in target file")


class PatchRetryContext(BaseModel):
    error_type: str = Field(
        description="Type of failure (e.g., compile_error_missing_import, test_failure_assertion)"
    )
    error_message: str = Field(description="The raw error message")
    attempt_count: int = Field(
        default=1, description="Number of attempts for this specific hunk/patch"
    )
    suggested_action: str = Field(description="Suggested action for next iteration")


class BackportState(TypedDict):
    # Core inputs
    patch_content: str
    target_repo_path: str
    target_branch: str

    # Git / Workspace State
    worktree_path: Optional[str]
    clean_state: bool

    # Execution Tracking
    classification: Optional[PatchClassification]
    localization_results: List[LocalizationResult]
    hunks: List[Dict[str, Any]]

    # Per-agent hunk outputs
    applied_hunks: List[Dict[str, Any]]      # Agent 3: already written to disk
    adapted_hunks: List[Dict[str, Any]]      # Agent 4: namespace-adapted, awaiting synthesis
    refactored_hunks: List[Dict[str, Any]]   # Agent 5: structurally refactored, awaiting synthesis
    synthesized_hunks: List[Dict[str, Any]]  # Agent 6: CLAW-verified, ready to apply
    failed_hunks: List[Dict[str, Any]]       # Hunks that failed all processing

    # Tracks which hunk indices (into state["hunks"]) have been claimed by an agent.
    # Subsequent agents skip claimed indices so hunks aren't double-processed.
    processed_hunk_indices: List[int]

    # Hunk indices that Agent 4 couldn't handle (structural change, not just symbol
    # renames) — Agent 5 picks these up regardless of localization confidence/method.
    structural_escalation_indices: List[int]

    # Developer auxiliary hunks: hunks from the original developer patch that bypass
    # the LLM pipeline and are applied directly during validation. Includes:
    #   - Test file hunks (src/test/java/, src/internalClusterTest/java/, etc.)
    #   - Non-Java file hunks (config, XML, build files, etc.)
    #   - Auto-generated Java file hunks (ANTLR, Protobuf, gRPC, etc.)
    # Populated by Agent 1 during hunk segregation before localization.
    developer_aux_hunks: List[Dict[str, Any]]

    # Retry and Validation
    retry_contexts: List[PatchRetryContext]
    current_attempt: int
    max_retries: int

    # Synthesis outcome
    synthesis_status: str  # "success" | "partial" | "failed"

    # HunkRouter decision: "fast_apply" | "namespace_adapter" | "structural_refactor"
    routing_decision: str

    # Validation outcome (Agent 7)
    validation_passed: bool
    validation_attempts: int
    validation_error_context: str
    validation_failure_category: str  # "context_mismatch" | "api_mismatch" | "test_failure" | "infrastructure" | ""
    validation_retry_files: List[str]  # files to re-localize on retry
    validation_results: Dict[str, Any]  # detailed per-step results

    # Set to True by the pipeline harness after it pre-applies synthesized_hunks to disk
    # and captures generated.patch. The validator skips CLAW re-application on attempt 0
    # when this flag is True (the hunks are already on disk).
    synthesized_hunks_pre_applied: bool

    # Agent 8: Syntax Repair
    # "clean"   — all files parsed without errors; no repair needed
    # "repaired" — one or more hunks were fixed by the LLM
    # "failed"  — repair was attempted but syntax errors persist (validator will surface them)
    # "skipped" — no synthesized_hunks to check (nothing to repair)
    syntax_repair_status: str
    syntax_repair_attempts: int           # repair iterations tried per file (max 2)
    syntax_repair_log: List[Dict[str, Any]]  # [{file_path, errors, attempts, outcome}, ...]

    # Agent 9: Fallback
    # "not_run"  — fallback has not been triggered yet
    # "applied"  — fallback generated new synthesized_hunks and returned to validator
    # "failed"   — fallback could not produce valid CLAW pairs
    fallback_status: str
    fallback_attempts: int                # number of completed fallback runs (max 2)
    hunk_descriptions: List[Dict[str, Any]]  # HunkDescription objects from Phase 1

    # File-level operations detected from the patch (populated by Agent 1).
    # These are structural filesystem changes that are distinct from content-change
    # hunks and are executed by Agent 7 before the build step.
    #
    # Each entry is a dict with:
    #   operation:       "DELETED" | "RENAMED"
    #   file_path:       target path (new path for RENAMED, path to remove for DELETED)
    #   old_file_path:   previous path (RENAMED only — the path that exists in target)
    #   target_new_path: desired final path in target branch (RENAMED only — may differ
    #                    from mainline's new path when package structure differs)
    #
    # ADDED files are handled inline by Agent 6 (new_file localization path) and do
    # not need a separate file_operations entry.
    file_operations: List[Dict[str, Any]]

    # Metrics
    tokens_used: int
    llm_token_usage: Dict[str, Dict[str, int]]
    wall_clock_time: float
    status: str
