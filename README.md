# Backport CLAW — Multi-Agent Java Patch Backporting System

A LangGraph-based orchestration system for automatically backporting Java patches across repository branches. Uses hybrid code localization, specialized agents, and structured validation to achieve high-accuracy patch application with minimal token usage.

## Quick Start

### Setup

```bash
# Clone and install
git clone <repo>
cd backport-claw
uv sync  # or: pip install -r requirements.txt

# Set API key
cp .env.example .env
# Edit .env: add ANTHROPIC_API_KEY=sk-...

# Build Java microservice
cd java-microservice && mvn clean package && cd ..
```

### Run a Shadow Test

```bash
# Run all 5 reference patches (Elasticsearch)
python tests/run_full_pipeline_shadow_v2.py

# Run just TYPE-I patches
python tests/run_full_pipeline_shadow_v2.py --type TYPE-I

# Run first 3 patches
python tests/run_full_pipeline_shadow_v2.py --count 3

# Add your own patches to tests/patches.yaml, then run
python tests/run_full_pipeline_shadow_v2.py --repo myrepo --count 10
```

Results appear in `tests/shadow_run_results/<TYPE>_<sha>/`:
- `mainline.patch` — the original commit being backported
- `target.patch` — the reference backport (what we're trying to match)
- `generated.patch` — our system's output
- `results.json` — per-agent timing, outputs, and metrics

## Architecture Overview

The system processes patches through a **6-agent pipeline**:

```
[Patch Input]
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
[Agent 6: Hunk Synthesizer] — Produces final CLAW-compatible patches
    ↓
[Output: Applied patches + results.json]
```

Each agent reads from a shared `BackportState` and updates it with its results. Agents only process hunks they're responsible for, using `processed_hunk_indices` to avoid duplication.

---

## Agent Details

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

**Model:** Haiku 4.5 (but this agent doesn't use LLM; it's pure code logic)

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

**Model:** Haiku 4.5 (fast classification with structured output)

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

**Model:** None (pure Python logic, deterministic)

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

**Model:** Sonnet 4.6 (balanced reasoning for symbol mapping)

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

**Model:** Opus 4.6 (deepest reasoning for hardest patches; downgraded to Sonnet/Haiku if token budget < 30%)

---

### Agent 6: Hunk Synthesizer

**Role:** Final stage — produces CLAW-compatible `old_string`/`new_string` pairs and verifies they can be applied.

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

**Model:** Sonnet 4.6 (balanced reasoning; used only if synthesis requires LLM adaptation)

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
python tests/run_full_pipeline_shadow_v2.py

# By repo
python tests/run_full_pipeline_shadow_v2.py --repo elasticsearch

# By type
python tests/run_full_pipeline_shadow_v2.py --type TYPE-II

# By count
python tests/run_full_pipeline_shadow_v2.py --count 3

# Combine filters
python tests/run_full_pipeline_shadow_v2.py --repo elasticsearch --type TYPE-I --count 5
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

### 3. CLAW verification is mandatory

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
- **70%:** Downgrade Opus → Sonnet
- **85%:** Downgrade all → Haiku with simplified prompts
- **95%:** Abort with partial progress

Example:
```python
if tokens_used / TOKEN_BUDGET > 0.85:
    llm_tier = LLMTier.FAST  # Use Haiku
```

---

## Project Structure

```
/backport-claw
├── src/
│   ├── agents/                    # Agent 1–6 implementations
│   │   ├── agent1_localizer.py
│   │   ├── agent2_classifier.py
│   │   ├── agent3_fastapply.py
│   │   ├── agent4_namespace.py
│   │   ├── agent5_structural.py
│   │   └── agent6_synthesizer.py
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
│   ├── backport_claw/             # CLAW application logic
│   │   └── apply_hunk.py
│   ├── core/                      # State, models, graph
│   │   ├── state.py
│   │   ├── graph.py
│   │   └── llm_router.py
│   └── memory/                    # Persistent knowledge (future)
│       └── patch_knowledge_index.py
├── tests/
│   ├── patches.yaml               # Patch config (any repo/type/count)
│   ├── run_full_pipeline_shadow_v2.py  # Parameterized shadow run
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
# Unit tests (no LLM API key required)
pytest tests/unit -v

# Shadow run on 5 reference patches
python tests/run_full_pipeline_shadow_v2.py

# Full run on custom patches
python tests/run_full_pipeline_shadow_v2.py --config custom_patches.yaml
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
   python tests/run_full_pipeline_shadow_v2.py --repo myrepo
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

---

## Model Selection

- **Agent 1 (Localizer):** No LLM (pure code logic)
- **Agent 2 (Classifier):** Haiku 4.5 (fast, structured output)
- **Agent 4 (Namespace):** Sonnet 4.6 (balanced reasoning)
- **Agent 5 (Structural):** Opus 4.6 (deepest reasoning; with fallback to Sonnet/Haiku at token limits)
- **Agent 6 (Synthesizer):** Sonnet 4.6 (final verification and context management)

Token budgets are tracked globally; agents downgrade gracefully as budget depletes.

---

## License

[Your License Here]
