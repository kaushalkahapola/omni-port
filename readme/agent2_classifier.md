# Agent 2: Patch Classifier (`agent2_classifier.py`)

Agent 2 is the second component in the OmniPort pipeline, executing *after* the Code Localizer has finished its work. It analyzes the localized target regions, the methods used to find them, and the original mainline patch to determine its complexity and risk level.

## Core Responsibilities

1. **Complexity Classification**:
   Uses an LLM (configured via `LLMRouter`) to classify the patch into one of five categories by examining the evidence from Agent 1 (e.g., if a hunk was found via exact git matching, it's likely TYPE I; if found via GumTree AST with extensive changes, it's likely TYPE III or higher):
   - **TYPE_I**: Simple bug fixes, typos, single-line changes.
   - **TYPE_II**: Medium complexity (minor logic changes, additions).
   - **TYPE_III**: High complexity (structural changes, API additions).
   - **TYPE_IV**: Deep structural refactoring with API divergence.
   - **TYPE_V**: Massive architectural overhauls.

2. **Auto-Generated File Detection**:
   Delegates scanning the patch for common signatures indicating auto-generated code to `preprocessor.py`.

3. **Token Budget Estimation**:
   Dynamically estimates the required LLM token budget for downstream processing (like synthesis) based on the patch complexity and size. Simple TYPE_I patches get lower budgets, while complex TYPE_V patches get maximum allowances.

## Inputs & Outputs

- **Input**: `BackportState` containing the raw parsed hunks from the mainline patch and the `LocalizationResult` list from Agent 1.
- **Output**: An updated `BackportState` featuring a `PatchClassification` object containing the assigned `patch_type`, `confidence` score, textual `reasoning`, and the `token_budget_estimate`.

## Tools Used
- `LLMRouter`: For routing the classification prompt to the configured LLM provider (OpenAI / Azure OpenAI) using LangChain's structured output generation.
