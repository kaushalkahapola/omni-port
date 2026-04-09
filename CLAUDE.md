# Backport CLAW — Multi-Agent Java Patch Backporting System

**Status:** Production-grade multi-agent system under active development.

**Model:** Recommended Haiku 4.5 for fast iteration on classifier/router logic, Sonnet 4.6 for agents requiring domain reasoning.

---

## Project Overview

**Backport CLAW** is a LangGraph-based orchestration system for automatically backporting Java patches across repository branches. The system combines:

- **Hybrid code localization** (git pickaxe, RapidFuzz fuzzy text, GumTree AST diffing, JavaParser symbol resolution, embedding search)
- **Per-hunk TYPE I–V classification** (informed by localization evidence, not abstract diff analysis)
- **Specialized agents** routed by patch complexity: Fast-Apply (TYPE I/II), Namespace Adapter (TYPE III), Structural Refactor (TYPE IV/V)
- **Validation-driven retry loops** with structured failure routing
- **Persistent memory** for cross-patch learning (API renames, failure patterns, per-repo conventions)

The system is designed to spend minimal tokens on simple patches and concentrate expensive reasoning on genuinely hard structural adaptations.

---

## Architecture

See `/docs/plan.md` for the full 9-agent LangGraph state machine, hybrid localization pipeline, token budgets, and evaluation methodology.

**Key insight:** Classification is for observability/metrics only. Downstream agents completely ignore classifier output. Routing and all processing decisions are driven by localization evidence alone (method used: git_exact, fuzzy_text, gumtree_ast, javaparser; confidence scores; API changes detected). This eliminates misalignment between what the classifier thinks and what the code actually needs.

---

## Project Structure

```
/backport-claw
├── src/                           # Core application code
│   ├── agents/                    # Agent implementations (Classifier, Localizer, Adapters, Validator, Memory)
│   ├── backport_claw/             # CLAW hunk application logic (exact-string replacement, pre-validation)
│   ├── core/                      # Core models, state definitions, shared utilities
│   ├── localization/              # 5-stage localization pipeline (git native, fuzzy, GumTree, JavaParser, embedding)
│   ├── memory/                    # Persistent PatchKnowledgeIndex (SQLite + MEMORY.md)
│   ├── tools/                     # Tool wrappers (java-microservice client, git CLI, Gradle, Maven, SpotBugs)
│   └── validation/                # Validation loop (compilation, tests, SpotBugs)
│
├── tests/                         # Test suite
│   ├── unit/                      # Unit tests per module
│   ├── integration/               # Integration tests (e.g., localizer on Elasticsearch patches)
│   └── evaluation/                # Evaluation harness against 491-example dataset
│
├── docs/
│   ├── plan.md                    # Full system design (agents, localization, integration plan, risks)
│   ├── architecture.md            # Detailed architectural diagrams and design decisions
│   └── evaluation.md              # Metrics, golden set, ablation study methodology
│
├── repos/                         # Target repositories (currently Elasticsearch)
│   ├── elasticsearch/
│   │   ├── CLAUDE.md              # Elasticsearch-specific agent guide (build system, testing, conventions)
│   │   ├── AGENTS.md              # Elasticsearch toolchain snapshot and CI/CD guidance
│   │   └── [cloned elasticsearch repo...]
│   └── [other repos...]
│
├── java-microservice/             # Long-lived JVM process for JavaParser + GumTree
│   ├── src/main/java/             # JavaParser symbol resolver, GumTree wrapper, japicmp client
│   ├── pom.xml
│   └── Dockerfile
│
├── dataset/                       # Training/evaluation datasets
│   ├── 491_examples.jsonl         # Original 491-example patch dataset (Elasticsearch, CrateDB, etc.)
│   ├── golden_set_50.jsonl        # Golden set (10 per TYPE I–V, used for regression detection)
│   └── memory_snapshots/          # Per-repo API diffs, common failure patterns
│
├── pyproject.toml                 # Poetry dependencies + scripts
└── .env, .env.example             # Configuration (Claude API key, repo paths, token budgets)
```

### Source Code Organization

The `src/` directory mirrors the agent/function decomposition:

**`agents/`** — LangGraph node implementations:
- `patch_classifier.py` — Uses Claude Haiku + structured output; MUST validate against localization evidence
- `code_localizer.py` — 5-stage hybrid pipeline (git native → fuzzy text → GumTree → JavaParser → embeddings)
- `fast_apply_agent.py` — Direct CLAW application for TYPE I/II (no LLM)
- `namespace_adapter.py` — Symbol remapping for TYPE III patches
- `structural_refactor_agent.py` — Deep reasoning for TYPE IV/V (extended thinking)
- `hunk_synthesizer.py` — Produces CLAW-compatible `old_string`/`new_string` pairs with verification
- `memory_manager.py` — Updates PatchKnowledgeIndex; runs background consolidation

**`core/`** — Shared data models and utilities:
- `models.py` — `BackportState`, `PatchClassification`, `LocalizationResult`, `PatchRetryContext`
- `state.py` — LangGraph state machine definition and reducers
- `constants.py` — Token budgets, timeouts, retry limits, TYPE I–V thresholds

**`localization/`** — Hybrid code localization:
- `git_native.py` — Stage 1: `git log -S`, `git diff --find-renames`, blob SHA comparison
- `fuzzy_text.py` — Stage 2: RapidFuzz token_sort_ratio, LCS line matching
- `gumtree_wrapper.py` — Stage 3: Subprocess call to java-microservice (GumTree AST diffing)
- `javaparser_resolver.py` — Stage 4: Symbol resolution via java-microservice (`CombinedTypeSolver`)
- `embedding_search.py` — Stage 5: UniXcoder embeddings + FAISS index (fallback for TYPE V)

**`tools/`** — External tool integration:
- `java_microservice_client.py` — HTTP/JSON client to java-microservice (GumTree, JavaParser, japicmp)
- `gradle_maven_tools.py` — Subprocess wrappers for `./gradlew` and `mvn` with parsed output
- `git_tools.py` — Git CLI operations (branch management, worktree, picking)
- `spotbugs_tools.py` — SpotBugs XML parsing and structured violation extraction

**`backport_claw/`** — The CLAW application engine (mostly existing, preserved from prior work):
- `apply_hunk.py` — CLAW exact-string hunk application with pre-validation
- `hunk_models.py` — `CLAWHunk` data structures

**`memory/`** — Persistent knowledge:
- `patch_knowledge_index.py` — SQLite schema for `PatchLesson`, query/insert/consolidate methods
- `consolidation_agent.py` — AutoDream-style background consolidation (after every 50 patches)

**`validation/`** — The validation loop (enhanced from existing `validation_agent.py`):
- `validation_loop.py` — "Prove Red, Make Green" orchestration
- `compile_validator.py` — Maven/Gradle compilation with structured error parsing
- `test_validator.py` — Test runner with failure signal classification
- `spotbugs_validator.py` — SpotBugs analysis with custom detector integration

---

## Key Design Constraints

### 1. Classifier output is for observability; agents ignore it

- Patch Classifier runs AFTER localization and outputs a `PatchClassification` (TYPE_I–V label).
- This is for metrics, logging, and observability only — NOT for driving routing decisions.
- Downstream agents **do not read** the classifier output. They route and process based purely on localization results.

### 2. All routing and processing is driven by localization evidence

- Localization method (`git_exact`, `fuzzy_text`, `gumtree_ast`, `javaparser`) + confidence score determine everything.
- Example: `git_exact` + confidence >0.85 → Fast-Apply; `gumtree_ast` → Structural Refactor
- This keeps the system grounded in reality (where we actually found the code) rather than abstract classification.

### 3. HunkRouter reads localization evidence, not classifier output

- For patches with mixed localization methods (e.g., some hunks via git_exact, some via gumtree_ast), HunkRouter fans out via `Send` based on localization method + confidence.
- Routes: `git_exact` + high confidence → Fast-Apply; `gumtree_ast` or low confidence → Structural Refactor; import changes → Namespace Adapter.
- Never consults `PatchClassification` from state.

### 4. CLAW exact-string pairs must be verified before application

- `HunkSynthesizer` MUST verify that `old_string` exists verbatim in target file.
- If not found, expand context window (±5 lines) and regenerate.
- This is a critical safety gate.

### 5. Parallel multi-file processing requires dependency analysis

- Use JavaParser import graph analysis to detect cross-file dependencies.
- Independent files → parallel via `Send` API (max_concurrency=5).
- Dependent files → sequential topological order.
- Single atomic validation pass applies all hunks together.

### 6. Token budgets are hard limits with circuit breaker

- Track cumulative tokens across agents.
- At 70% of budget: downgrade Structural Refactor to Sonnet (from Opus).
- At 85%: downgrade all agents to Haiku with simplified prompts.
- At 95%: abort with partial progress report and suggested manual intervention.

---

## Working on the Project

### Setup

1. Clone the repo and `cd backport-claw`
2. Ensure Python 3.11+ and Java JDK 21+ are installed
3. Run `uv sync` to install dependencies (or `pip install -r requirements.txt`)
4. Copy `.env.example` to `.env` and add your Claude API key: `ANTHROPIC_API_KEY=...`
5. Verify java-microservice is built:
   ```bash
   cd java-microservice && mvn clean package && cd ..
   ```

### Running the system

```bash
# Process a single patch through the full pipeline
python -m src.core.main --patch-id <id> --target-repo elasticsearch

# Evaluate against 491-example dataset with metrics
python -m src.evaluation.evaluate_full_workflow --dataset dataset/491_examples.jsonl

# Run golden set (regression detection)
python -m tests.evaluation.golden_set_test

# Run unit tests
pytest tests/unit -v

# Check code style
ruff check src/ tests/
```

### Key development patterns

**Haiku vs. Sonnet vs. Opus:**
- Haiku (4.5): Patch Classifier, Code Localizer initialization, Memory Manager — speed matters
- Sonnet (4.6): Namespace Adapter, Hunk Synthesizer, Validation Loop — reasoning required
- Opus (4.6): Structural Refactor (TYPE IV/V only) — deepest analysis; reserved for hardest patches

**Structured output:**
- All LLM calls should use Claude's structured output mode with Pydantic models
- Avoids JSON parsing errors; LLM cannot refuse to provide a field

**Tool integration:**
- All external tools (git, Gradle, Maven, GumTree, JavaParser, japicmp) go through `tools/` module
- Never hardcode subprocess calls in agent code
- Always set timeouts and handle failures gracefully

**State management:**
- Use `BackportState` TypedDict for all inter-agent communication
- Use `operator.add` reducer for list accumulation (`localization_results`, `adapted_hunks`, `new_lessons`)
- Use `merge_dicts` custom reducer for metrics tracking

### Common tasks

**Adding a new agent:**
1. Create `agents/my_agent.py` with a function `@tool(...)` or async `async def node(state: BackportState) -> ...`
2. Define input models (what the agent reads from state) and output models (what it writes)
3. Add to `src.core.state` LangGraph graph via `graph.add_node(...)`
4. Write unit tests in `tests/unit/agents/test_my_agent.py`

**Adding a new localization stage:**
1. Create `localization/stage_N.py` implementing the stage interface
2. Integrate into `code_localizer.py`'s pipeline orchestration
3. Add timeout, error handling, and fallback-to-next-stage logic
4. Benchmark on a subset of the 491-example dataset

**Updating memory consolidation logic:**
1. Edit `memory/consolidation_agent.py`
2. Ensure backward compatibility with existing `PatchLesson` schema (use SQL migrations)
3. Test with existing memory snapshots in `dataset/memory_snapshots/`

**Working on Elasticsearch patches:**
- Consult `repos/elasticsearch/CLAUDE.md` for build/test specifics
- Understand Gradle composite build structure (Elasticsearch uses it heavily)
- Use `ELASTICSEARCH_HARNESS_EXCLUDED_TEST_MODULES` frozenset when filtering tests
- Be familiar with xpack security defaults and JVM tuning knobs

---

## Common Pitfalls to Avoid

1. **Reading classifier output in agent routing logic.** Never consult `PatchClassification` for routing decisions. Agents read localization_results only.

2. **Forgetting that localization confidence trumps everything.** If localization confidence < 0.6, route to Structural Refactor regardless of method_used or anything else.

3. **Forgetting CLAW verification.** Hunk Synthesizer must verify `old_string` exists; a mismatch means wrong target location.

4. **Parallel processing without dependency analysis.** Always check import graphs before parallelizing files; concurrent mutations can break cross-file references.

5. **Token budget creep.** Always track cumulative tokens. Don't assume Opus is free — at scale, it's expensive.

6. **Ignoring test infrastructure failures.** Distinguish timeout (hung test), OOM (heap dump), and flakiness (run 3x, check variance) from actual code failures.

7. **Not consulting PatchKnowledgeIndex.** Before regenerating a symbol mapping, check if it was learned in a prior patch — reuse saves tokens.

---

## Debugging Tips

**Stuck on a patch?**
- Check localization confidence scores. If < 0.6, you probably found the wrong location.
- Inspect the GumTree edit script for structural changes the classifier missed.
- Run the 5-stage localization pipeline in isolation:
  ```bash
  python -m src.localization.debug_pipeline --patch-id <id> --repo elasticsearch
  ```

**Compilation errors after patch application?**
- Check `retry_context.failure_type` — is it a missing import, missing symbol, or type mismatch?
- Let the Namespace Adapter fix imports; let Structural Refactor fix signatures.
- Never manually patch imports in hunk generation.

**Test failures attributed to wrong hunk?**
- Run `_classify_build_failure()` from `validation_tools.py` on the error output.
- It parses compiler messages and associates them with hunks.
- Trust its attribution; use `RetryRouter` to route to the appropriate fix agent.

**Memory consolidation memory issues?**
- Consolidation runs after every 50 patches to prune stale entries.
- If memory.db grows too large, check for redundant lessons:
  ```bash
  python -c "from src.memory.patch_knowledge_index import consolidate; consolidate()"
  ```

---

## Next Steps for Agents

1. **Implement Code Localizer** (5-stage pipeline in `src/localization/`)
2. **Implement Patch Classifier** (with validation rule from Agent 1 results)
3. **Add GumTree/JavaParser java-microservice** wrappers
4. **Implement Hunk Synthesizer** with CLAW verification
5. **Enhance Validation Loop** with structured retry routing
6. **Add Memory Manager** and consolidation
7. **Run evaluation** against 491-example dataset with metrics
8. **Deploy Release 1** in shadow mode before full migration

---

## References

- **Architecture Plan:** `/docs/plan.md` (full 9-agent system, hybrid localization, integration strategy)
- **Elasticsearch Guide:** `/repos/elasticsearch/CLAUDE.md` (build system, testing, conventions)
- **Claude Code Patterns:** The system adopts three patterns from Claude Code's internals: three-tier memory, AutoDream consolidation, and cache-aware prompt assembly
- **Related Work:** PortGPT (94.5% accuracy), Agentless (85% file-level accuracy), mpatch (44% more patches than cherry-pick)

---

**Model selected:** Haiku 4.5 (default for this conversation; switch to Sonnet for reasoning-heavy work)

