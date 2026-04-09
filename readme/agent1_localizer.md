# Agent 1: Code Localizer (`agent1_localizer.py`)

Agent 1 represents the **localization phase** of OmniPort. It operates exactly one commit before the target backport commit (the "parent" commit) and attempts to definitively locate where each hunk from the original mainline patch should be applied in the target branch.

## 5-Stage Hybrid Pipeline

Agent 1 processes each incoming Java hunk through a progressively slower but more powerful 5-stage pipeline, stopping as soon as high-confidence localization is found.

### Stage 1: Git-Native (Fast & Precise)
1. **Exact Match (`git apply --numstat`)**: Checks if the hunk can be applied cleanly or with minor fuzz to the target branch.
2. **Rename Tracking (`git log -S`)**: Uses Git Pickaxe to detect if the target file was renamed, moved, or deleted, avoiding wasted effort on missing files.

### Stage 2: Sliding Window Text Search (RapidFuzz)
Uses RapidFuzz to perform fuzzy string matching of the hunk's context lines against the target file's current contents. This handles cases where surrounding lines have changed significantly but the core structure remains similar.

### Stage 3: AST Structural Matching (GumTree)
Uses the Java microservice via `java_client.py` to parse the target file into an Abstract Syntax Tree (AST) using GumTree. It compares the structural AST changes implied by the mainline patch against the target file's AST to find matching nodes or scopes, even if the text diverges.

### Stage 4: Symbol Resolution (JavaParser)
Uses the Java microservice to resolve class names, method signatures, and field declarations in the mainline patch. It then searches the target branch for matching symbols to locate where the code *logically* moved (e.g., if a method was moved to a different file entirely).

### Stage 5: Semantic Search (UniXcoder + FAISS)
A final fallback mechanism for extremely diverged patches. It uses the Microsoft UniXcoder LLM model to generate semantic embeddings of the hunk's intent and performs a FAISS vector search across the target codebase to find code sections that are functionally similar, even if syntactically distinct.

## Outputs

The Localizer outputs a `HunkLocalization` object for each hunk, indicating:
- The target `file_path`.
- The specific `start_line` and `end_line` where the hunk should go.
- The `confidence` score (0.0 to 1.0) and the `method_used` (e.g., `git_exact`, `fuzzy`, `gumtree`).

## Tools Used
- `java_client.py`: The Python-to-Java RPC bridge for communicating with GumTree and JavaParser.
- `patch_parser.py`: Extracts exact line numbers, context lines, and file paths from unified diffs.
- `RapidFuzz` (Python Library): For optimized sliding window text matching.
