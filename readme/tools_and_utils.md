# Tools and Utilities

OmniPort relies on several crucial utilities and tools to bridge the gap between Python (for orchestration and LLM logic) and Java (for precise AST and code manipulation).

## 1. Java Client RPC Bridge (`src/tools/java_client.py`)

Since spinning up a new JVM instance for every patch localization request introduces severe latency (upwards of seconds per file), OmniPort uses a **persistent Python-to-Java RPC bridge**.

- **How it Works**: A background Java process (`OmniPortServer.java`) is spawned on startup. The Python client (`java_client.py`) communicates with this process via standard input/output (`stdin`/`stdout`) using JSON-RPC-like messages.
- **Capabilities**:
  - `parse_java`: Triggers JavaParser to extract classes, methods, and fields.
  - `gumtree_diff`: Uses the GumTree library to compute a structural AST diff between two Java files.
  - `japicmp_check`: Runs japicmp to detect binary/source incompatibilities (for validation).

## 2. Unified Diff Parser (`src/tools/patch_parser.py`)

A robust parser designed to extract meaning from standard `git format-patch` unified diffs.

- **How it Works**: It splits a multi-file diff into individual `ParsedHunk` objects.
- **Capabilities**:
  - Extracts the `file_path` (handling `a/` and `b/` prefixes).
  - Isolates context lines (unchanged), added lines (`+`), and removed lines (`-`).
  - Calculates precise line numbers based on the `@@ -start,count +start,count @@` headers.
  - Discards noise (like diff formatting, index hashes, etc.) for downstream LLMs.

## 3. LLM Router (`src/core/llm_router.py`)

The central component for all language model interactions, built on top of LangChain.

- **How it Works**: Abstracts away the specific LLM provider (e.g., OpenAI vs. Azure OpenAI). It selects the appropriate model tier based on the task:
  - `TIER_1` (e.g., GPT-4o): For complex tasks like Agent 6 (Synthesizer) and Agent 1 (Classifier).
  - `TIER_2` (e.g., GPT-4o-mini): For lighter tasks or fallbacks.
- **Capabilities**:
  - Supports structural outputs using Pydantic (e.g., returning a strongly typed `PatchClassification` object instead of raw text).
  - Handles environment configurations (`AZURE_OPENAI_API_KEY`, `OPENAI_API_VERSION`, etc.) seamlessly.

## 4. Java Microservice (`java-microservice/`)

The actual Java implementation of the RPC server.

- **Stack**: Java 17+, Maven.
- **Libraries**:
  - `com.github.javaparser` for precise syntax tree extraction.
  - `com.github.gumtreediff` for structural differencing.
  - `japicmp` for binary compatibility checking (used in Phase 2).
- **Build**: Uses the `maven-shade-plugin` to package all dependencies into a single fat JAR (`omniport-java-1.0-SNAPSHOT-jar-with-dependencies.jar`), which the Python client invokes.
