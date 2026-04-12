# Graph Report - .  (2026-04-11)

## Corpus Check
- Large corpus: 36930 files · ~23,006,275 words. Semantic extraction will be expensive (many Claude tokens). Consider running on a subfolder, or use --no-semantic to run AST-only.

## Summary
- 825 nodes · 1706 edges · 44 communities detected
- Extraction: 67% EXTRACTED · 33% INFERRED · 0% AMBIGUOUS · INFERRED: 558 edges (avg confidence: 0.51)
- Token cost: 0 input · 0 output

## God Nodes (most connected - your core abstractions)
1. `BackportState` - 141 edges
2. `LocalizationResult` - 124 edges
3. `HunkSynthesizer` - 54 edges
4. `CLAWHunkApplier` - 53 edges
5. `FastApplyAgent` - 48 edges
6. `SynthesizedHunk` - 48 edges
7. `ValidationToolkit` - 47 edges
8. `CLAWHunkError` - 43 edges
9. `PatchRetryContext` - 40 edges
10. `run_full_pipeline()` - 20 edges

## Surprising Connections (you probably didn't know these)
- `Unstaged diff against the current HEAD (modifications not yet committed).` --uses--> `BackportState`  [INFERRED]
  tests/run_full_pipeline_shadow.py → src/core/state.py
- `Recursively convert Pydantic models to plain dicts for JSON serialisation.` --uses--> `BackportState`  [INFERRED]
  tests/run_full_pipeline_shadow.py → src/core/state.py
- `Runs a single agent node, records timing and any exception.     If the agent rai` --uses--> `BackportState`  [INFERRED]
  tests/run_full_pipeline_shadow.py → src/core/state.py
- `Applies Agent 6 synthesized hunks (old_string → new_string) to disk.     Returns` --uses--> `BackportState`  [INFERRED]
  tests/run_full_pipeline_shadow.py → src/core/state.py
- `Load YAML config with patch definitions.` --uses--> `BackportState`  [INFERRED]
  tests/run_full_pipeline_shadow_v2.py → src/core/state.py

## Hyperedges (group relationships)
- **Localization Evidence → Classification Pipeline** — agent1_localizer, git_native_stage, fuzzy_text_stage, gumtree_stage, javaparser_stage, embedding_stage, agent2_classifier [EXTRACTED 1.00]
- **Hunk Processing Pipeline (Type-Driven Routing)** — agent3_fastapply, agent4_namespace, agent5_structural, agent6_synthesizer [EXTRACTED 1.00]
- **Validation and Retry Loop with Structured Routing** — agent7_validator, validation_loop_detailed, agent1_localizer, agent4_namespace, agent5_structural, patch_retry_context, memory_manager [EXTRACTED 1.00]

## Communities

### Community 0 - "Core Agents"
Cohesion: 0.04
Nodes (93): GumTreeEdit, Structured output from the Reasoning LLM., StructuralAdaptationOutput, hunk_synthesizer_agent(), HunkSynthesizer, Returns (exists, confidence).         1.0 → unique exact match; 0.9 → exact matc, Synthesizes a CLAW hunk pair and verifies old_string exists in the target file., Synthesizes a list of hunks.          loc_index_override: when set, hunk[k] maps (+85 more)

### Community 1 - "Agent Orchestration"
Cohesion: 0.05
Nodes (70): fast_apply_agent(), FastApplyAgent, Agent 3: Fast-Apply Agent (No LLM)  Strategy: for EVERY localized hunk, attempt, LangGraph node: Fast-Apply Agent.      For every localized hunk, checks if old_c, Applies patches using deterministic exact-string replacement.     Tries ALL hunk, Builds CLAW old_string / new_string from hunk content.         The patch_parser, Returns (success, modified_content, error_message)., _apply_developer_aux_hunks() (+62 more)

### Community 2 - "Agent Routing"
Cohesion: 0.04
Nodes (57): _analyze_failure(), _build_symbol_to_file_candidates(), _classify_apply_failure(), _classify_build_failure(), _detect_type_v_retry_scope(), _extract_method_mismatch_details(), _extract_structured_failure_context(), _extract_test_classes() (+49 more)

### Community 3 - "Agent Interfaces"
Cohesion: 0.05
Nodes (59): classify_patch(), Agent 2: Patch Classifier     Classifies the patch/hunks (TYPE I-V), assigns con, _adapt_with_llm(), _compute_hunk_diff(), _has_import_changes(), namespace_adapter_agent(), NamespaceAdaptationOutput, Agent 4: Namespace Adapter (Balanced LLM)  Handles unclaimed hunks where the old (+51 more)

### Community 4 - "Agent State"
Cohesion: 0.07
Nodes (62): _apply_patch_with_temp_index(), _build_agent_eligible_patch(), _build_auxiliary_hunks_from_developer_patch(), _build_generated_patch_from_hunks(), _build_hunk_comparison_markdown(), _build_recovery_intelligence_report(), _build_touched_test_state_markdown(), _build_transition_summary_markdown() (+54 more)

### Community 5 - "Notifications"
Cohesion: 0.08
Nodes (33): DiscordNotifier, Read a markdown file and send its content to Telegram., Send formatted test result summaries to Discord., Send embed message to Discord webhook., Send formatted pipeline summary., Send a single patch test notification to configured services., # TODO: Implement Discord embed formatting for single patch, Send pipeline summary notification to configured services. (+25 more)

### Community 6 - "Error Types"
Cohesion: 0.11
Nodes (33): classify_build_failure(), _clear_junit_reports(), collect_test_results(), CompilerErrorDetail, _detect_project_name(), detect_test_targets(), _ensure_docker_image(), evaluate_test_transition() (+25 more)

### Community 7 - "Java Microservice"
Cohesion: 0.08
Nodes (22): ensure_service_running(), get_manager(), gumtree_diff(), japicmp_compare(), JavaMicroserviceClient, JavaMicroserviceManager, javaparser_resolve(), MCP server wrapper for Java microservice integration. (+14 more)

### Community 8 - "Test Utilities"
Cohesion: 0.12
Nodes (25): _extract_class_name(), extract_test_class(), extract_test_class_name(), _find_gradle_module(), _find_maven_module(), find_module_for_file(), get_added_test_files(), get_modified_test_files() (+17 more)

### Community 9 - "Classifier Logic"
Cohesion: 0.16
Nodes (17): _apply_inter_hunk_consistency(), _build_hunk_text(), _is_auto_generated_java_file(), _is_auxiliary_hunk(), _is_false_license_header_match(), _is_test_file(), localize_hunks(), localizer_pipeline() (+9 more)

### Community 10 - "Localizer Logic"
Cohesion: 0.17
Nodes (7): GitOrchestrator, Creates a new git worktree for isolated operations., Removes an isolated worktree., Checks if the main repository is clean., Extracts patch diff from a given commit hash., Applies a patch to the repository or worktree., Agent 0 - Git Orchestrator (No LLM)     Manages branch checkouts, git worktree i

### Community 11 - "Java Components"
Cohesion: 0.21
Nodes (2): JavaParserController, JavaParserService

### Community 12 - "Evaluation Pipeline"
Cohesion: 0.24
Nodes (13): apply_synthesized_hunks(), git_checkout(), git_diff(), git_show(), is_production_java_hunk(), main(), make_serializable(), process_patch() (+5 more)

### Community 13 - "Exceptions"
Cohesion: 0.23
Nodes (7): Exception, get_default_router(), LLMRouter, Raised when cumulative token usage exceeds 95% of the configured budget., Apply circuit-breaker downgrade rules based on token budget consumption., Retrieve the appropriate LLM instance for a given tier.         Applies token ci, TokenBudgetExceeded

### Community 14 - "JavaParser Integration"
Cohesion: 0.31
Nodes (4): close_java_client(), get_java_client(), JavaMicroserviceClient, Reads one line from the microservice stdout with a wall-clock timeout.         R

### Community 15 - "Phase 0 Implementation"
Cohesion: 0.38
Nodes (9): _format_transition_summary(), _is_phase0_cache_reusable(), _load_phase0_cache(), _phase0_cache_dir(), _phase0_cache_file(), phase_0_optimistic(), Phase 0: Optimistic Fast-Path Patching Node  Before spinning up the expensive 4-, Phase 0: The Fast-Path Direct Application Node.      Responsibilities:       1. (+1 more)

### Community 16 - "GumTree Wrapper"
Cohesion: 0.28
Nodes (2): GumTreeController, GumTreeService

### Community 17 - "Memory System"
Cohesion: 0.33
Nodes (4): get_connection(), MemoryDB, Inserts a consolidated lesson learned into the database., Manages SQLite database for PatchLesson schema as part of the Memory Manager.

### Community 18 - "Symbol Resolution"
Cohesion: 0.31
Nodes (8): _extract_method_names(), _find_src_main_java(), _grep_method_definitions(), Extract declared method names from Java source content.     Matches: (public|pro, Stage 0: Class hierarchy file redirector.      When the mainline patch modifies, Find the src/main/java root directory for the given file path.     Handles multi, Grep the repo for Java method DECLARATIONS (not calls) matching `method_names`., run_hierarchy_file_redirect()

### Community 19 - "AST Analysis"
Cohesion: 0.39
Nodes (7): gumtree_diff(), japicmp_compare(), javaparser_find_method(), javaparser_resolve(), _post(), Synchronous HTTP client for the Java microservice (Spring Boot on port 8080).  S, Make a synchronous HTTP POST to the Java microservice.

### Community 20 - "Test Collection"
Cohesion: 0.6
Nodes (5): discover_xml_files(), main(), parse_console(), parse_xml(), strip_ansi()

### Community 21 - "Java Tools"
Cohesion: 0.4
Nodes (5): cleanup_java_file(), cleanup_java_imports(), Java import deduplication utility.  When the namespace adapter synthesizes an im, Remove duplicate import statements from Java source content.      Rules:       -, Apply import cleanup to a Java file on disk.     Returns True if the file was mo

### Community 22 - "Telegram Integration"
Cohesion: 0.47
Nodes (5): find_chat_id(), get_updates(), main(), Get updates from Telegram bot., Listen for messages and extract chat ID.

### Community 23 - "Database Tests"
Cohesion: 0.4
Nodes (0): 

### Community 24 - "Hunk Processing"
Cohesion: 0.4
Nodes (0): 

### Community 25 - "Japicmp Integration"
Cohesion: 0.5
Nodes (1): JapicmpController

### Community 26 - "Telegram Utilities"
Cohesion: 0.67
Nodes (3): main(), Run the shadow v3 pipeline., run_test_pipeline()

### Community 27 - "Localization Pipeline"
Cohesion: 0.5
Nodes (0): 

### Community 28 - "Health Monitoring"
Cohesion: 0.5
Nodes (1): HealthController

### Community 29 - "API Diffing"
Cohesion: 0.67
Nodes (1): JapicmpService

### Community 30 - "Shadow Testing"
Cohesion: 1.0
Nodes (2): run_evaluation(), test_hunk()

### Community 31 - "Synthesizer"
Cohesion: 0.67
Nodes (0): 

### Community 32 - "Validator"
Cohesion: 0.67
Nodes (0): 

### Community 33 - "Debug Harness"
Cohesion: 1.0
Nodes (2): run_evaluation(), test_hunk()

### Community 34 - "Microservice Tests"
Cohesion: 0.67
Nodes (0): 

### Community 35 - "Router Tests"
Cohesion: 0.67
Nodes (0): 

### Community 36 - "Spring Boot App"
Cohesion: 0.67
Nodes (1): OmniPortApplication

### Community 37 - "Java Services"
Cohesion: 0.67
Nodes (2): parse_unified_diff(), Parses a unified diff into a list of hunks.     Extracts the old_content (lines

### Community 38 - "Preprocessing"
Cohesion: 0.67
Nodes (2): is_auto_generated_file(), Check if a file content looks like it was auto-generated.     Looks for @Generat

### Community 39 - "Shadow Run"
Cohesion: 1.0
Nodes (0): 

### Community 40 - "Java Config"
Cohesion: 1.0
Nodes (0): 

### Community 41 - "Phase 1 Shadow"
Cohesion: 1.0
Nodes (0): 

### Community 42 - "Module Initialization"
Cohesion: 1.0
Nodes (0): 

### Community 43 - "Documentation"
Cohesion: 1.0
Nodes (1): Extract a rename map {old_class_fqn: new_class_fqn} from developer         auxil

## Knowledge Gaps
- **136 isolated node(s):** `Run the shadow v3 pipeline.`, `Find the Maven module (directory with pom.xml) for a given file.`, `Extract the fully qualified test class name from a test file path.`, `Walk up the file path looking for a build.gradle or build.gradle.kts.     Return`, `Return True if this file looks like a test source file.` (+131 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **Thin community `Shadow Run`** (2 nodes): `run_phase1_shadow_debug.py`, `run_evaluation()`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Java Config`** (2 nodes): `test_java_microservice_javaparser.py`, `test_javaparser_resolve()`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Phase 1 Shadow`** (2 nodes): `run_phase1_shadow.py`, `run_evaluation()`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Module Initialization`** (2 nodes): `__init__.py`, `hello()`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Documentation`** (1 nodes): `Extract a rename map {old_class_fqn: new_class_fqn} from developer         auxil`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `BackportState` connect `Core Agents` to `Agent Orchestration`, `Agent Interfaces`, `Agent State`, `Notifications`, `Classifier Logic`, `Evaluation Pipeline`?**
  _High betweenness centrality (0.319) - this node is a cross-community bridge._
- **Why does `ValidationToolkit` connect `Agent Routing` to `Phase 0 Implementation`?**
  _High betweenness centrality (0.120) - this node is a cross-community bridge._
- **Why does `LocalizationResult` connect `Core Agents` to `Agent Orchestration`, `Agent Interfaces`, `Classifier Logic`?**
  _High betweenness centrality (0.104) - this node is a cross-community bridge._
- **Are the 139 inferred relationships involving `BackportState` (e.g. with `Unstaged diff against the current HEAD (modifications not yet committed).` and `Recursively convert Pydantic models to plain dicts for JSON serialisation.`) actually correct?**
  _`BackportState` has 139 INFERRED edges - model-reasoned connections that need verification._
- **Are the 122 inferred relationships involving `LocalizationResult` (e.g. with `TestCLAWHunkApplier` and `TestFastApplyAgent`) actually correct?**
  _`LocalizationResult` has 122 INFERRED edges - model-reasoned connections that need verification._
- **Are the 45 inferred relationships involving `HunkSynthesizer` (e.g. with `TestNamespaceAdapterRouting` and `TestNamespaceAdapterNode`) actually correct?**
  _`HunkSynthesizer` has 45 INFERRED edges - model-reasoned connections that need verification._
- **Are the 47 inferred relationships involving `CLAWHunkApplier` (e.g. with `TestCLAWHunkApplier` and `TestFastApplyAgent`) actually correct?**
  _`CLAWHunkApplier` has 47 INFERRED edges - model-reasoned connections that need verification._