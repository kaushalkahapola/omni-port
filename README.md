# OmniPort — Multi-Agent Java Patch Backporting System

A LangGraph-based orchestration system for automatically backporting Java patches across repository branches. Uses hybrid code localization, specialized agents, and structured validation to achieve high-accuracy patch application with minimal token usage.

## Evaluation

We evaluate on the **JavaBackports** benchmark — 491 manually-validated backports across 15 open-source Java repositories, categorised by structural complexity (Types I–V). See the paper: [JavaBackports: A Benchmark for Automated Patch Backporting in Java](https://rshariffdeen.com/paper/MSR26.pdf).

### Results

OmniPort (powered by **GPT-4.1 mini**) achieves **85.95% overall success** (422/491), outperforming both portGPT and a naive one-shot LLM baseline.

| Approach | Type I | Type II | Type III | Type IV | Type V | **Total** |
|---|---|---|---|---|---|---|
| **OmniPort (Ours)** | **219/233 (94.0%)** | **139/151 (92.1%)** | **18/23 (78.3%)** | **4/5 (80.0%)** | **42/79 (53.2%)** | **422/491 (85.95%)** |
| portGPT (Baseline) | 218/233 (93.6%) | 115/151 (76.2%) | 11/23 (47.8%) | 1/5 (20.0%) | 17/79 (16.5%) | 362/491 (73.73%) |
| One-Shot LLM | 174/233 (74.7%) | 74/151 (49.0%) | 10/23 (43.5%) | 1/5 (20.0%) | 9/79 (11.4%) | 268/491 (54.58%) |

Success metric: ≥1 previously-failing test now passes, verified against the developer's own test suite.

#### Success Rate by Patch Complexity

```
             OmniPort ████  portGPT ▓▓▓▓  One-Shot ░░░░

Type I   94% ████████████████████████████████████████████
         94% ████████████████████████████████████████████
         75% ███████████████████████████████████

Type II  92% ███████████████████████████████████████████
         76% ████████████████████████████████████
         49% ████████████████████████

Type III 78% ██████████████████████████████████████
         48% ████████████████████████
         44% ██████████████████████

Type IV  80% ████████████████████████████████████████
         20% ██████████
         20% ██████████

Type V   53% ██████████████████████████
         17% ████████
         11% █████

         0%       25%       50%       75%      100%
```

**Key observations:**
- OmniPort's hybrid pipeline excels on moderate complexity (Types II–IV), where deterministic components handle simple cases (>92% on Types I/II) and LLM agents resolve the rest.
- On the hardest complete-API-redesign cases (Type V), OmniPort achieves **53.2%** vs portGPT's **16.5%** — a 3× improvement.

## Quick Start

### Setup

```bash
# Clone and install
git clone <repo>
cd omni-port
uv sync  # or: pip install -r requirements.txt

# Set API key
cp .env.example .env
# Edit .env: add your API key

# Build Java microservice
cd java-microservice && mvn clean package && cd ..
```

### Run a Shadow Test

```bash
# Run all 5 reference patches (Elasticsearch)
python tests/run_full_pipeline_shadow_v3.py

# Run just TYPE-I patches
python tests/run_full_pipeline_shadow_v3.py --type TYPE-I

# Run first 3 patches
python tests/run_full_pipeline_shadow_v3.py --count 3

# Add your own patches to tests/patches.yaml, then run
python tests/run_full_pipeline_shadow_v3.py --repo myrepo --count 10
```

Results appear in `tests/shadow_run_results/<TYPE>_<sha>/`:
- `mainline.patch` — the original commit being backported
- `target.patch` — the reference backport (what we're trying to match)
- `generated.patch` — our system's output
- `results.json` — per-agent timing, outputs, and metrics

## Architecture Overview

![OmniPort Architecture](readme-images/omniport-arch.png)

The system processes patches through a **10-agent pipeline**:

```
[Patch Input]
    ↓
[Agent 0: Git Orchestrator] — Branch checkout, worktree isolation, patch extraction (no LLM)
    ↓
[Agent 1: Code Localizer] — Finds hunk locations in target codebase (5-stage pipeline)
    ↓
[Agent 2: Patch Classifier] — Classifies complexity (TYPE I–V) & estimates tokens
    ↓
[Agent 3: Fast-Apply] — Exact-string match application (no LLM)
    ↓
[Agent 4: Namespace Adapter] — Symbol/import remapping (balanced LLM)
    ↓
[Agent 5: Structural Refactor] — Deep code restructuring (reasoning LLM)
    ↓
[Agent 6: Hunk Synthesizer] — Produces final OmniPortSource-compatible patches
    ↓
[Agent 8: Syntax Repair] — Pre-validates and repairs Java syntax errors (balanced LLM)
    ↓
[Agent 7: Validation Loop] — Build + test execution, failure categorisation (no LLM)
    ↓ (on failure)
[Agent 9: Fallback] — Semantic re-application via ReAct tool loop (fast + balanced LLM)
    ↓
[Output: Applied patches + results.json]
```

Each agent reads from a shared `BackportState` and updates it with its results. Agents only process hunks they're responsible for, using `processed_hunk_indices` to avoid duplication.

---

## Agent Details

### Agent 0: Git Orchestrator

**Role:** Manages all git-level operations before the LLM pipeline begins — branch checkouts, worktree isolation, and patch extraction.

**Input:**
- `target_repo_path` — path to the target repository
- `target_branch` — branch to backport onto
- `original_commit` — SHA of the mainline commit to extract

**Output:**
- `worktree_path` — isolated git worktree for this run
- `patch_content` — unified diff extracted from the original commit

**Key Operations:**
- `create_worktree(branch, dir)` — creates an isolated git worktree so the main repo stays clean
- `remove_worktree(dir)` — tears down the worktree after the run
- `get_patch_from_commit(sha)` — runs `git format-patch -1 <sha>` to extract the diff
- `apply_patch(path)` — applies a patch file via `git apply`
- `is_clean()` — checks repo cleanliness before starting

**Model:** None (pure git subprocess logic, fails closed on errors)

---

### Agent 1: Code Localizer

**Role:** Finds where hunks from the original commit apply in the target branch.

**Input:**
- `patch_content` — unified diff from original commit
- `target_repo_path` — path to target codebase
- `hunks` — list of parsed diff hunks (from `PatchParser`)

**Output:**
- `localization_results` — list of `LocalizationResult` objects with:
  - `file_path` — where the hunk was found
  - `method_used` — how it was found (`git_exact`, `fuzzy_text`, `gumtree_ast`, `javaparser`, `embedding`)
  - `confidence` — 0.0–1.0 score
  - `start_line`, `end_line` — target line numbers
  - `symbol_mappings` — API renames detected (from JavaParser/GumTree stages)

**5-Stage Pipeline:**

1. **Git Native** (`git_exact`, `git_pickaxe`) — Exact SHA lookup, blame-based search, rename detection
2. **Fuzzy Text** (`fuzzy_text`) — RapidFuzz token_sort_ratio on file content
3. **GumTree AST** (`gumtree_ast`) — Structural AST diffing via Java microservice
4. **JavaParser** (`javaparser`) — Symbol resolution, import analysis, type resolution
5. **Embedding Search** (`embedding`) — UniXcoder embeddings + FAISS fallback

Each stage has a **confidence threshold** — if met, processing stops (no point going deeper). Stages are ordered by computational cost.

---

### Agent 2: Patch Classifier

**Role:** Classifies patch complexity to understand token/reasoning requirements and provide observability.

**Input:**
- `patch_content` — original unified diff
- `localization_results` — evidence from Agent 1

**Output:**
- `classification` — `PatchClassification` object with:
  - `patch_type` — TYPE_I, TYPE_II, TYPE_III, TYPE_IV, TYPE_V, or MIXED
  - `confidence` — 0.0–1.0 score
  - `reasoning` — explanation of classification
  - `token_budget_estimate` — estimated tokens needed to resolve
  - `is_auto_generated` — whether file is generated (skips expensive LLM analysis)

**Classification Levels:**
- **TYPE_I** — Exact string match, trivial backport (git_exact, high confidence)
- **TYPE_II** — Whitespace/formatting drift, minor API renames (fuzzy_text, git_pickaxe)
- **TYPE_III** — Namespace changes, method renames, import adjustments (gumtree_ast with symbol_mappings)
- **TYPE_IV** — Significant structural refactoring, API surface changes (gumtree_ast, low confidence)
- **TYPE_V** — Complete redesign, fundamentally different implementation (embedding fallback)
- **MIXED** — Different hunks have very different complexities

**Important:** Classifier output is **for observability only**. Downstream agents do NOT read `classification` for routing decisions. All routing is driven purely by localization evidence (method + confidence).

---

### Agent 3: Fast-Apply

**Role:** Direct exact-string patch application (no LLM needed for TYPE I/II).

**Input:**
- `hunks` — all parsed hunks
- `localization_results` — target file locations
- `target_repo_path` — where to apply patches

**Output:**
- `applied_hunks` — list of hunks successfully written to disk
- `processed_hunk_indices` — indices of all hunks this agent claimed

**Algorithm:**
1. For each localized hunk:
   - Read target file at the localized location
   - Extract `old_string` from hunk content
   - Try exact-string match (context_lines=0) → if found, replace and write to disk
   - If not found, expand context (±5 lines) and retry
2. Mark hunk as "processed" (claimed or failed)
3. Pass unclaimed hunks to Agent 4

**Routing:**
- Claims hunks where old_string found verbatim (or high-confidence git localization found the file)
- Skips hunks it couldn't apply (passes to Agent 4)

---

### Agent 4: Namespace Adapter

**Role:** Handles API drift — symbol renames, import changes, method signature adjustments.

**Input:**
- Unclaimed hunks from Agent 3
- `symbol_mappings` from localization results
- Target file context

**Output:**
- `adapted_hunks` — hunks with symbol renames and imports applied
- Updated `processed_hunk_indices`

**Routing Triggers:**
- Localization found `symbol_mappings` (javaparser/gumtree_ast)
- Hunk diff contains differing import statements
- old_content not found verbatim in target file

**Algorithm:**
1. For each unclaimed hunk:
   - Check if it has symbol_mappings or import drift
   - Use LLM to remap symbols and fix imports
   - Produce `adapted_old_content` and `adapted_new_content`
   - Mark for synthesis in Agent 6
2. Pass remaining hunks to Agent 5

---

### Agent 5: Structural Refactor

**Role:** Deep semantic adaptation for hunks with significant structural changes.

**Input:**
- Unclaimed hunks from Agent 4
- `localization_results` (including GumTree edit scripts)
- Target file/class context

**Output:**
- `refactored_hunks` — semantically adapted code
- Updated `processed_hunk_indices`

**Routing Triggers:**
- Localization method is `gumtree_ast` (AST-level changes detected)
- Localization confidence < 0.6 (low confidence → needs human-like reasoning)

**Algorithm:**
1. For each unclaimed hunk:
   - Parse GumTree edit script (Insert, Delete, Update, Move operations)
   - Fetch japicmp API change report (if available)
   - Use Reasoning LLM with extended thinking to:
     - Understand the original intent
     - Map to semantically equivalent code in target codebase
     - Handle class renames, method signature changes, refactored control flow
   - Produce `refactored_code` with confidence score
   - Mark for synthesis in Agent 6
2. Pass unclaimed hunks (failed refactoring) to Agent 6 as-is

---

### Agent 6: Hunk Synthesizer

**Role:** Final stage — produces OmniPortSource-compatible `old_string`/`new_string` pairs and verifies they can be applied.

**Input:**
- `adapted_hunks` from Agent 4
- `refactored_hunks` from Agent 5
- Raw hunks not yet processed
- Target file content

**Output:**
- `synthesized_hunks` — list of `SynthesizedHunk` objects with:
  - `file_path`, `old_string`, `new_string`
  - `verified` — True if old_string found in target file
  - `confidence` — likelihood of correct application
- `failed_hunks` — hunks that couldn't be synthesized or verified

**Algorithm:**
1. For adapted and refactored hunks:
   - Extract context around the localized line range
   - Build `old_string` from hunk's old_content + surrounding context
   - Build `new_string` from adapted/refactored content + surrounding context
   - Verify old_string exists in target file
   - If not found, expand context (±3/5/7/10 lines progressively) and retry
   - If still not found, mark as failed
2. Write synthesized hunks to disk (final application)

---

### Agent 8: Syntax Repair

**Role:** Sits between Agent 6 and Agent 7. Applies synthesized hunks in-memory and checks each modified Java file for structural syntax errors before they hit the build, repairing broken hunks via LLM when needed.

**Input:**
- `synthesized_hunks` — output from Agent 6
- `worktree_path` / `target_repo_path` — repo location
- `hunks` — original mainline hunks (for intent context)

**Output:**
- `synthesized_hunks` — potentially updated with repaired `new_string` values
- `syntax_repair_status` — `"clean"` | `"repaired"` | `"failed"` | `"skipped"`
- `syntax_repair_attempts` — cumulative repair iterations
- `syntax_repair_log` — per-file repair events

**Algorithm:**
1. Group synthesized hunks by file
2. Apply each file's hunks in-memory (no disk writes)
3. Call Java microservice `/api/javaparser/parse-check` to detect structural errors; falls back to brace-balance counting if the service is unreachable
4. If errors found, call LLM with the error location, the broken hunk, and the mainline intent to produce a `fixed_new_string`
5. Re-check the repaired content; retry up to `MAX_REPAIR_ATTEMPTS` times per file
6. If still failing, pass through to Agent 7 to surface the error via the build

**Model:** Balanced LLM (only invoked when syntax errors are detected)

---

### Agent 7: Validation Loop

**Role:** "Prove Red, Make Green" — applies all patches to the worktree, runs the build and relevant tests, evaluates the test state transition, and on failure populates retry context for graph re-routing.

**Input:**
- `synthesized_hunks` — from Agent 8 (post syntax repair)
- `applied_hunks` — from Agent 3 (already on disk, skipped here)
- `developer_aux_hunks` — test files, non-Java files, auto-generated files segregated by Agent 1
- `worktree_path` — isolated repo to build in

**Output:**
- `validation_status` — `"success"` | `"failed"`
- `validation_error_context` — build/test error tail for downstream agents
- `validation_failure_category` — `"context_mismatch"` | `"api_mismatch"` | `"test_failure"` | `"infrastructure"`
- `validation_retry_files` — files that need attention on retry
- `retry_context` — `PatchRetryContext` populated for graph retry routing

**Algorithm:**
1. Apply `synthesized_hunks` via CLAW exact-string replacement
2. Apply `developer_aux_hunks` via `git apply` (with git-strict → git-tolerant → git-3way → GNU patch fallbacks)
3. Execute file-level operations (RENAMED / DELETED production files)
4. Strip unused Java imports (deterministic pre-build fix for checkstyle failures)
5. Run build (`helpers/{project}/run_build.sh` or Maven/Gradle)
6. Detect and run relevant tests
7. Evaluate test state transition vs optional baseline
8. On failure: restore repo state, classify failure category, populate `retry_context`

**Retry routing:**
- `"context_mismatch"` → re-localize (back to Agent 1)
- `"api_mismatch"` → route to Agent 4 or Agent 5
- `"test_failure"` → regenerate hunk (back to Agent 6)
- `"infrastructure"` → abort

**Model:** None (pure build/test orchestration logic)

---

### Agent 9: Fallback

**Role:** Last resort when Agent 7 has failed and normal retry paths are exhausted. Rather than re-submitting raw patch syntax, it builds a semantic understanding of each change and applies it via a ReAct tool-calling loop.

**Input:**
- `hunks` — original mainline hunks
- `localization_results` — from Agent 1
- `validation_error_context` / `validation_failure_category` — from Agent 7
- `validation_retry_files` — files that need regeneration
- `synthesized_hunks` — existing hunks (non-retry files are preserved)

**Output:**
- `synthesized_hunks` — retry-file entries replaced; others preserved
- `hunk_descriptions` — semantic descriptions from Phase 1
- `fallback_status` — `"applied"` | `"failed"`
- `fallback_attempts` — incremented each run

**Two-phase algorithm:**

**Phase 1 — Description Builder (Fast LLM, single call)**
- Analyses each mainline hunk and produces structured natural-language descriptions: WHAT changed, WHERE in the code, WHY (semantic intent), change type, and key symbols
- Raw unified-diff syntax is never forwarded to Phase 2

**Phase 2 — Change Application Agent (Balanced LLM, ReAct loop, max 8 turns)**
- Receives hunk descriptions + localization results + full error history
- Uses tools to freely explore the repo:
  - `read_target_file` — read current file content
  - `search_in_target_repo` — grep for method/class definitions
  - `get_class_hierarchy` — JavaParser BFS over superclasses
  - `get_memory_lessons` — known API-rename lessons from PatchKnowledgeIndex
  - `submit_changes` — submit final CLAW pairs (ends the loop)
- Verifies every `old_string` exists verbatim before submitting
- Handles version constant remapping (e.g. `Version.V_5_9_0` → correct release-branch constant)
- Strips unused imports before submitting to avoid checkstyle failures

**Model:** Fast LLM (Phase 1) + Balanced LLM with tool use (Phase 2)

---

## Configuration & Parameterization

### Patch Config (`tests/patches.yaml`)

Define patches for any repo and type:

```yaml
repos:
  elasticsearch:
    path: repos/elasticsearch
    patches:
      - type: TYPE-I
        original_commit: da51c8cc...
        backport_commit: b25542fd...
        description: "Simple transport refactor"
      
      - type: TYPE-V
        original_commit: c94c021d...
        backport_commit: e867dcdb...
        description: "Deep API changes"

  myrepo:
    path: repos/myrepo
    patches:
      - type: TYPE-III
        original_commit: abc123...
        backport_commit: def456...
```

### Running with Filters

```bash
# Run all patches
python tests/run_full_pipeline_shadow_v3.py

# By repo
python tests/run_full_pipeline_shadow_v3.py --repo elasticsearch

# By type
python tests/run_full_pipeline_shadow_v3.py --type TYPE-II

# By count
python tests/run_full_pipeline_shadow_v3.py --count 3

# Combine filters
python tests/run_full_pipeline_shadow_v3.py --repo elasticsearch --type TYPE-I --count 5
```

---

## State Machine & Data Flow

### BackportState

All agents communicate via the `BackportState` TypedDict:

```python
@dataclass
class BackportState(TypedDict):
    # Input
    patch_content: str               # unified diff
    target_repo_path: str            # path to target codebase
    target_branch: str               # branch being backported to
    hunks: List[Dict[str, Any]]      # parsed hunks
    
    # Agent 1 output
    localization_results: List[LocalizationResult]
    
    # Agent 2 output
    classification: Optional[PatchClassification]
    
    # Agent 3 output
    applied_hunks: List[Dict[str, Any]]
    processed_hunk_indices: List[int]
    
    # Agent 4 output
    adapted_hunks: List[Dict[str, Any]]
    
    # Agent 5 output
    refactored_hunks: List[Dict[str, Any]]
    
    # Agent 6 output
    synthesized_hunks: List[SynthesizedHunk]
    failed_hunks: List[Dict[str, Any]]
    synthesis_status: str
    
    # Metadata
    retry_contexts: List[PatchRetryContext]
    tokens_used: int
    wall_clock_time: float
    status: str
```

### Hunk Routing

Agents decide which hunks to process using `processed_hunk_indices`:

```python
# Agent 3
unprocessed = [h for i, h in enumerate(state["hunks"]) 
               if i not in state["processed_hunk_indices"]]

# Apply hunks to disk, then update:
state["processed_hunk_indices"].extend([hunk_indices_we_applied])
```

This prevents:
- Duplicate application (Agent 3 applies, Agent 6 re-applies)
- Out-of-order processing
- Partial overlaps

---

## Design Principles

### 1. Classifier is observability-only

The `PatchClassification` output is logged and tracked for metrics, but **agents never consult it for routing decisions**. All routing is driven by **localization evidence** (method + confidence).

This separation means:
- Agents are decoupled from classifier accuracy
- Routing is grounded in reality (where we actually found code)
- System adapts to localization evidence, not abstract classification

### 2. Confidence trumps method

Example routing logic in Structural Refactor:
```python
if loc_result.confidence < 0.6:
    # Use extended reasoning, regardless of method_used
    route_to_structural_refactor()
```

Low confidence is a signal that the hunk is probably in the wrong file or nearby but not exact. Reasoning agent takes over to verify semantics.

### 3. Verification is mandatory

Before application, all synthesized hunks must have `old_string` verified as present in the target file:

```python
# Fails if not found, expands context, retries
def verify_old_string(file_content, old_string, context_lines=5):
    if old_string not in file_content:
        return None  # Failed
    return True
```

This prevents silent misapplications.

### 4. Token budgets are enforced

Track cumulative tokens across all agents. At thresholds:
- **70%:** Downgrade to balanced reasoning model
- **85%:** Downgrade all to fast model with simplified prompts
- **95%:** Abort with partial progress

Example:
```python
if tokens_used / TOKEN_BUDGET > 0.85:
    llm_tier = LLMTier.FAST
```

---

## Project Structure

```
/omniportsource
├── src/
│   ├── agents/                    # Agent 0–9 implementations
│   │   ├── agent0_git.py
│   │   ├── agent1_localizer.py
│   │   ├── agent2_classifier.py
│   │   ├── agent3_fastapply.py
│   │   ├── agent4_namespace.py
│   │   ├── agent5_structural.py
│   │   ├── agent6_synthesizer.py
│   │   ├── agent7_validator.py
│   │   ├── agent8_syntax_repair.py
│   │   ├── agent9_fallback.py
│   │   └── hunk_router.py
│   ├── localization/              # 5-stage localization pipeline
│   │   ├── stage1_git.py
│   │   ├── stage2_fuzzy.py
│   │   ├── stage3_gumtree.py
│   │   ├── stage4_javaparser.py
│   │   └── stage5_embedding.py
│   ├── tools/                     # Tool wrappers
│   │   ├── patch_parser.py
│   │   ├── java_client.py
│   │   └── preprocessor.py
│   ├── omniportsource/            # Patch application logic
│   │   └── apply_hunk.py
│   ├── core/                      # State, models, graph
│   │   ├── state.py
│   │   ├── graph.py
│   │   └── llm_router.py
│   └── memory/                    # Persistent knowledge (future)
│       └── patch_knowledge_index.py
├── tests/
│   ├── patches.yaml               # Patch config (any repo/type/count)
│   ├── run_full_pipeline_shadow_v3.py  # Parameterized shadow run
│   ├── shadow_run_results/        # Output directory
│   └── unit/                      # Unit tests
├── java-microservice/             # GumTree + JavaParser service
│   └── pom.xml
├── repos/                         # Target repositories (cloned)
│   ├── elasticsearch/
│   └── crate/
└── docs/
    ├── plan.md                    # Full system design
    └── architecture.md            # Detailed architecture
```

---

## Running Tests

```bash
# Unit tests (no API key required)
pytest tests/unit -v

# Shadow run on 5 reference patches
python tests/run_full_pipeline_shadow_v3.py

# Full run on custom patches
python tests/run_full_pipeline_shadow_v3.py --config custom_patches.yaml
```

---

## Adding Patches to Test

1. Clone your target repo into `repos/`:
   ```bash
   git clone https://github.com/myorg/myrepo.git repos/myrepo
   ```

2. Find commit SHAs for original and backport:
   ```bash
   cd repos/myrepo
   git log --oneline | head -20  # Find commits
   ```

3. Add to `tests/patches.yaml`:
   ```yaml
   repos:
     myrepo:
       path: repos/myrepo
       patches:
         - type: TYPE-I
           original_commit: abc123def456...
           backport_commit: xyz789uvw012...
           description: "Your description here"
   ```

4. Run:
   ```bash
   python tests/run_full_pipeline_shadow_v3.py --repo myrepo
   ```

Results appear in `tests/shadow_run_results/TYPE-I_abc123de/results.json`.

---

## Troubleshooting

**Java microservice not starting?**
```bash
cd java-microservice && mvn clean package
java -jar target/omniport-java-service-1.0-SNAPSHOT.jar
```

**Localization failing for all hunks?**
```bash
# Check git history in target repo
cd repos/elasticsearch
git log --oneline | grep <original_commit_sha>
# If not found, the commit may not exist in that branch
```

**Token budget exceeded?**
- Reduce patch count with `--count`
- Check `results.json` to see which agents are consuming most tokens
- Ensure Java microservice is running (GumTree/JavaParser stages may fail and retry with LLM)

**Old_string verification failing?**
- Target file may have drifted more than expected
- Check `synthesized_hunks[].error` in `results.json`
- Consider using extended thinking (Agent 5) for this patch

---

## References

- **CLAUDE.md** — Project instructions, design principles, development patterns
- **docs/plan.md** — Full 9-agent system design (Phase 2+), hybrid localization architecture
- **src/core/state.py** — State machine definition and TypedDicts
- **src/core/graph.py** — LangGraph orchestration
- **Dataset:** [JavaBackports](https://github.com/Javabackports/javabackports) — [MSR'26 Paper](https://rshariffdeen.com/paper/MSR26.pdf)

---

## License

[Your License Here]
