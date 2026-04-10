# Agent 3: Fast-Apply Agent (`agent3_fastapply.py`)

Agent 3 is the **first processing agent** in the OmniPort pipeline. It performs deterministic, exact-string patch application on hunks that were successfully localized by Agent 1. No LLM is used here — speed and correctness through pure code logic.

## Core Responsibility

**Apply patches directly to disk when the exact old content is found verbatim in the target file.**

This agent is designed to spend zero LLM tokens on straightforward hunks. For every localized hunk:
1. Read the target file at the location Agent 1 found
2. Extract the `old_string` from the hunk
3. Search for exact-string match in the file
4. If found → apply the replacement and write to disk immediately
5. If not found → claim the hunk as unclaimed and pass to Agent 4

## Routing Rules

**Agent 3 claims a hunk if:**
- Localization method is `git_exact` or `git_pickaxe` AND confidence ≥ 0.85, OR
- The `old_content` from the hunk is found verbatim in the target file (any localization method)

**Agent 3 passes to Agent 4 if:**
- `old_content` is NOT found in the target file (API drift detected)
- Localization failed or confidence too low

## Algorithm

### Input
- `hunks`: List of parsed diff hunks
- `localization_results`: Output from Agent 1 (file paths, line numbers, methods, confidence)
- `target_repo_path`: Path to the target repository

### Processing Steps

```
For each hunk:
  1. Get localization result (file_path, start_line, end_line, method_used, confidence)
  
  2. Extract CLAW strings:
     old_string = hunk.old_content.rstrip("\n")
     new_string = hunk.new_content.rstrip("\n")
  
  3. Read target file:
     file_content = read(target_repo_path / file_path)
     if file not found → fail this hunk
  
  4. Attempt exact-string match (context_lines=0):
     if old_string found verbatim in file_content:
       new_content = file_content.replace(old_string, new_string, 1)
       write new_content to disk
       mark as "applied"
       add to processed_hunk_indices
       continue to next hunk
  
  5. Fallback: expand context (±5 lines) and retry:
     context_window = extract_context(file_content, start_line, end_line, context_lines=5)
     if old_string found in context_window:
       apply replacement
       mark as "applied"
       continue
  
  6. If still not found:
     mark as "unclaimed"
     leave for Agent 4 to handle
```

### Output

Updates `BackportState` with:
- `applied_hunks`: List of successfully applied hunks
  ```python
  {
    "applied": True,
    "file_path": "path/to/File.java",
    "error": None
  }
  ```
- `processed_hunk_indices`: Indices of all hunks claimed (successful or failed)
- Unmodified hunks stay in state for Agent 4 processing

## Key Design Decisions

### 1. No LLM — Pure Determinism

Fast-Apply intentionally avoids LLM calls. For TYPE I/II patches, exact-string matching is:
- **Faster**: Microseconds vs seconds per hunk
- **More reliable**: No hallucination risk
- **Cheaper**: Zero token cost

### 2. Context Expansion Strategy

If exact match fails, expand context progressively:
- Start with ±0 lines (just the old_content)
- Fallback to ±5 lines
- Never go wider (risks matching wrong location)

This handles minor whitespace drift without resorting to fuzzy matching.

### 3. "Claim and Write" Philosophy

Once Fast-Apply applies a hunk to disk, it's **claimed** — no other agent re-applies it. This is enforced by `processed_hunk_indices`:

```python
# Agent 4 only processes unclaimed hunks
unprocessed = [h for i, h in enumerate(state["hunks"]) 
               if i not in state["processed_hunk_indices"]]
```

## Example

### Input Hunk
```diff
--- a/src/main/java/org/elasticsearch/transport/InboundDecoder.java
+++ b/src/main/java/org/elasticsearch/transport/InboundDecoder.java
@@ -18,7 +18,7 @@ class InboundDecoder {
     private int messageSize = 0;
     
-    public void reset() {
+    public void resetDecoder() {
         position = 0;
         messageSize = 0;
     }
```

### Localization Result
```python
LocalizationResult(
  file_path="src/main/java/org/elasticsearch/transport/InboundDecoder.java",
  method_used="git_exact",
  confidence=1.0,
  start_line=21,
  end_line=25
)
```

### Processing

1. **Extract strings:**
   ```
   old_string = "    public void reset() {"
   new_string = "    public void resetDecoder() {"
   ```

2. **Read file** at target location (lines 21-25)

3. **Search:** Find exact match in file → FOUND

4. **Apply:**
   ```java
   // Before:
   public void reset() {
   
   // After:
   public void resetDecoder() {
   ```

5. **Write to disk** and mark as applied ✓

## Edge Cases

### Case 1: Confidence Too Low
If `localization_results.confidence < 0.85` and method is fuzzy/gumtree:
- Still try exact-string match (cheap operation)
- If fails → pass to Agent 4 for LLM-based adaptation

### Case 2: File Not Found
- Record error: `"File not found: path/to/File.java"`
- Pass to Agent 4 (maybe file was moved/renamed)

### Case 3: Partial Context Match
If old_string not found exactly but ±5 context window matches:
- Apply with expanded context
- Mark as applied (contextual confidence is high enough)

### Case 4: Multiple Possible Locations
If old_string appears N times in the file:
- Replace first occurrence only (default behavior)
- Log warning if N > 1
- Pass to Agent 6 for verification if ambiguous

## Performance

- **Time per hunk:** ~1-5ms (file read + string search)
- **Total for 13-hunk patch:** ~50-100ms (negligible)
- **Token cost:** $0.00 (no LLM)

## Tools Used

- `CLAWHunkApplier` (from `src.backport_claw.apply_hunk`): Handles exact-string find/replace with context management
- Standard Python file I/O

## Model

**None** — This agent is pure Python logic, no LLM involved.

## Integration with Pipeline

```
Agent 1 (Localizer) outputs hunks + locations
        ↓
Agent 3 (Fast-Apply) applies trivial hunks to disk
        ↓
Unclaimed hunks passed to Agent 4 (Namespace Adapter)
        ↓
Agent 4 handles namespace/import drift
        ↓
Agent 5 handles structural refactoring
        ↓
Agent 6 synthesizes and verifies remaining hunks
```

## Common Issues

### Issue: "old_string not found"
- **Cause:** Hunk is in wrong file or target has drifted significantly
- **Resolution:** Agent 4/5 will adapt the hunk using LLM reasoning

### Issue: "Applied multiple locations"
- **Cause:** old_string appears N times; we replaced first occurrence
- **Resolution:** Check Agent 6 verification; may need expanded context in synthesizer

### Issue: Very slow patch application
- **Cause:** Large files or many hunks
- **Resolution:** Parallelize in Phase 3; currently sequential

## Future Enhancements

- Parallel application via `Send` API (max_concurrency=5)
- Intelligent context expansion strategy (learn from failures)
- Duplicate detection (same old_string in multiple locations)
