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

    # Retry and Validation
    retry_contexts: List[PatchRetryContext]
    current_attempt: int
    max_retries: int

    # Synthesis outcome
    synthesis_status: str  # "success" | "partial" | "failed"

    # Metrics
    tokens_used: int
    wall_clock_time: float
    status: str
