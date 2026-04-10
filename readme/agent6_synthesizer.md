# Agent 6: Hunk Synthesizer Agent (`agent6_synthesizer.py`)

Agent 6 is the **final synthesis and verification agent**. It takes all outputs from previous agents (applied, adapted, refactored hunks) and produces final CLAW-compatible `old_string`/`new_string` pairs. It verifies that each `old_string` actually exists in the target file before committing to application.

## Core Responsibility

**Synthesize exact-match CLAW strings and verify they apply cleanly to the target file.**

Agent 6 receives:
- Hunks already applied to disk by Agent 3
- Adapted hunks from Agent 4 (not yet on disk)
- Refactored hunks from Agent 5 (not yet on disk)
- Any remaining raw hunks

Its job is to:
1. Synthesize CLAW `old_string`/`new_string` for adapted/refactored hunks
2. Verify each `old_string` exists verbatim in target file
3. If verification fails, expand context progressively and retry
4. Write verified hunks to disk
5. Collect failures and report

## Routing Rules

**Agent 6 processes:**
- All `adapted_hunks` from Agent 4
- All `refactored_hunks` from Agent 5
- Remaining raw hunks NOT in `processed_hunk_indices`

**Agent 6 skips:**
- Hunks in `applied_hunks` (Agent 3 already wrote them)
- Hunks already applied to disk

## Algorithm

### Input
- `adapted_hunks`: LLM-adapted hunks from Agent 4
- `refactored_hunks`: LLM-refactored hunks from Agent 5
- `hunks`: All original parsed hunks
- `processed_hunk_indices`: Hunks already claimed
- `target_repo_path`: Path to codebase
- `localization_results`: File paths and line numbers

### Processing Steps

```
Process adapted hunks:
  For each hunk in adapted_hunks:
    1. Get original hunk index from state
    2. Read target file at localized location
    3. Extract surrounding context (±5/7/10/15 lines)
    4. Build CLAW strings:
       old_string = adapted_old_content + context
       new_string = adapted_new_content + context
    5. Verify old_string exists in target file:
       if old_string found:
         write new_string to disk
         add to synthesized_hunks with verified=True
       else:
         expand context and retry (context_lines += 5)
    6. If still not found after max_context:
       add to failed_hunks with error message

Process refactored hunks:
  For each hunk in refactored_hunks:
    1. Similar to adapted hunks
    2. Build CLAW strings:
       old_string = hunk.old_content + context
       new_string = refactored_code + context
    3. Verify and apply

Process remaining raw hunks:
  For each hunk NOT in processed_hunk_indices:
    1. This is a last-resort fallback
    2. Attempt synthesis with high context windows
    3. If still fails, mark as failed

Final verification pass:
  1. For all synthesized_hunks marked for disk write
  2. Validate no overlapping ranges
  3. Sort by line number
  4. Apply atomically to disk (all at once)
```

### Output

Updates `BackportState` with:
- `synthesized_hunks`: List of successfully synthesized hunks
  ```python
  SynthesizedHunk(
    file_path="src/main/java/org/elasticsearch/api/Client.java",
    old_string="// 10 lines of context with original code",
    new_string="// 10 lines of context with adapted/refactored code",
    confidence=0.89,
    context_lines_included=10,
    verified=True
  )
  ```
- `failed_hunks`: List of hunks that couldn't be synthesized/verified
  ```python
  {
    "hunk_index": 12,
    "file_path": "src/main/java/...",
    "error": "old_string not found even with max_context (±15 lines)",
    "reason": "File changed too much; context mismatch",
    "old_string_attempted": "..."
  }
  ```
- `synthesis_status`: "success", "partial", or "failed"
- `processed_hunk_indices`: Completed with all synthesized indices

## Context Expansion Strategy

If `old_string` not found on first try, expand context progressively:

```
Attempt 1: ±5 lines
Attempt 2: ±7 lines
Attempt 3: ±10 lines
Attempt 4: ±15 lines
Attempt 5: ±20 lines (last resort)

If still not found → mark as failed
```

Rationale:
- Tighter context is more specific (less risk of wrong location)
- Gradual expansion handles minor drifts
- Max expansion prevents matching wrong code section

## Example: Synthesize Adapted Hunk

### Input from Agent 4
```python
adapted_hunk = {
  "hunk_index": 3,
  "file_path": "src/main/java/org/elasticsearch/Client.java",
  "adapted_old_content": """client.search(request)
    .onResponse(response -> { ... })
    .onFailure(error -> { ... })""",
  "adapted_new_content": """client.asyncSearch(request)
    .onResponse(response -> { ... })
    .onFailure(error -> { ... })""",
  "confidence": 0.92
}
```

### Localization Info
```python
LocalizationResult(
  file_path="src/main/java/org/elasticsearch/Client.java",
  start_line=145,
  end_line=155,
  confidence=0.85
)
```

### Synthesis Process

**Step 1: Read target file**
```java
// Line 140-160 of target file
public class Client {
  ...
  public void executeSearch(Request request) {
    client.search(request)
      .onResponse(response -> {
        // response handling
      })
      .onFailure(error -> {
        // error handling
      });
  }
  ...
}
```

**Step 2: Extract context (attempt 1: ±5 lines)**
```java
// Lines 140-160 (start_line=145 ± 5)
public void executeSearch(Request request) {
  client.search(request)
    .onResponse(response -> {
      // response handling
    })
    .onFailure(error -> {
      // error handling
    });
}
```

**Step 3: Build CLAW strings**
```python
old_string = """public void executeSearch(Request request) {
  client.search(request)
    .onResponse(response -> {
      // response handling
    })
    .onFailure(error -> {
      // error handling
    });
}"""

new_string = """public void executeSearch(Request request) {
  client.asyncSearch(request)
    .onResponse(response -> {
      // response handling
    })
    .onFailure(error -> {
      // error handling
    });
}"""
```

**Step 4: Verify old_string in file**
```
Check: is old_string found verbatim in target file? YES
Verification: PASSED ✓
```

**Step 5: Write to disk**
```
Replace old_string with new_string in target file
Write file
Record as synthesized_hunk with verified=True
```

**Output:**
```python
SynthesizedHunk(
  file_path="src/main/java/org/elasticsearch/Client.java",
  old_string="{...}",  # 11 lines from public void through closing }
  new_string="{...}",  # same with client.asyncSearch
  confidence=0.92,
  context_lines_included=5,
  verified=True
)
```

## Verification Failure Handling

### Scenario: Verification Fails at ±5

**Problem:** old_string not found at ±5 context

**Action:** Expand to ±7

```python
for context_lines in [5, 7, 10, 15, 20]:
    old_string = extract_with_context(..., context_lines)
    if verify_in_file(old_string, file_content):
        write_to_disk(old_string, new_string)
        return success
    # else: try next context size
    
# All attempts failed
failed_hunks.append({
    "hunk_index": idx,
    "error": "Verification failed at all context levels (±5 to ±20)",
    "file_path": file_path
})
```

### When to Give Up

If old_string not found even at max_context (±20 lines):
1. Record detailed failure info
2. Include attempted old_string (for debugging)
3. Add to failed_hunks
4. Continue with next hunk (don't block pipeline)

## Overlapping Range Detection

Before writing to disk, Agent 6 validates no two synthesized hunks overlap:

```python
def check_overlaps(synthesized_hunks):
    by_file = {}
    for hunk in synthesized_hunks:
        file = hunk.file_path
        if file not in by_file:
            by_file[file] = []
        by_file[file].append(hunk)
    
    for file, hunks in by_file.items():
        # Sort by start line (estimated from old_string)
        hunks_sorted = sorted(hunks, key=lambda h: h.start_line)
        
        for i in range(len(hunks_sorted) - 1):
            if hunks_sorted[i].end_line > hunks_sorted[i+1].start_line:
                # Overlap detected
                raise OverlapError(f"Hunks {i} and {i+1} overlap in {file}")
```

## Fuzzy Matching Fallback

If exact-string verification fails, Agent 6 can attempt fuzzy matching as final fallback:

```python
def fuzzy_find_in_file(file_content, old_string, threshold=0.85):
    """
    Sliding-window fuzzy search for old_string in file.
    Returns best match if similarity >= threshold.
    """
    from rapidfuzz import fuzz
    
    lines = file_content.splitlines(keepends=True)
    old_lines = old_string.splitlines(keepends=True)
    
    best_match = None
    best_score = 0.0
    
    for i in range(len(lines) - len(old_lines)):
        window = "".join(lines[i:i+len(old_lines)])
        score = fuzz.ratio(window, old_string)
        if score > best_score:
            best_score = score
            best_match = (i, window)
    
    return best_match if best_score >= threshold else None
```

Used only as last resort (confidence reduced significantly).

## Synthesis Status Reporting

Agent 6 reports synthesis outcome:

```python
synthesis_status = "success"  # All hunks applied
synthesis_status = "partial"  # Some hunks failed
synthesis_status = "failed"   # All hunks failed
```

Stored in state and results.json for visibility.

## CLAW Application

After synthesis, hunks are written to disk via CLAW:

```python
class CLAWHunkApplier:
    def find_and_replace(self, old_string, new_string, context_lines=0):
        """
        Exact-string find/replace.
        Returns (success: bool, result_content: str)
        """
        if old_string not in self.file_content:
            return False, ""
        
        # Safe replacement: replace only first occurrence
        result = self.file_content.replace(old_string, new_string, 1)
        return True, result
```

## Import Handling

Agent 6 also applies import statements from Agent 4:

```python
def apply_imports(file_content, imports_to_add, imports_to_remove):
    """
    Add/remove import statements at top of file.
    Preserves existing import grouping.
    """
    lines = file_content.splitlines(keepends=True)
    
    # Find import section (after package declaration)
    import_end = find_last_import_line(lines)
    
    # Remove unwanted imports
    for imp in imports_to_remove:
        lines = [l for l in lines if imp not in l]
    
    # Add new imports (avoid duplicates)
    for imp in imports_to_add:
        if imp not in file_content:
            lines.insert(import_end, f"{imp}\n")
    
    return "".join(lines)
```

## Error Recovery

If disk write fails:

```python
try:
    target.write_text(new_content, encoding="utf-8")
except (IOError, UnicodeDecodeError) as e:
    failed_hunks.append({
        "hunk_index": idx,
        "error": f"Disk write failed: {str(e)}",
        "file_path": file_path
    })
    # Restore original file
    target.write_text(original_content, encoding="utf-8")
```

## Performance

- **Time per hunk (synthesis):** ~100ms (verification + context expansion)
- **Time per hunk (file write):** ~50ms (I/O)
- **Total for 20 hunks:** ~3 seconds

This is fast because synthesis is pure Python (no LLM).

## Model

**None for synthesis** — Pure Python logic and string operations.

**Sonnet 4.6** optionally used for complex context interpretation (rare, < 5% of hunks).

## Integration with Pipeline

```
Agent 5 (Structural Refactor) refactors deep changes
        ↓
All adapted/refactored/unclaimed hunks → Agent 6 (Synthesizer)
        ↓
Agent 6 synthesizes CLAW strings and verifies
        ↓
All synthesized hunks written to disk atomically
        ↓
Results: synthesized_hunks + failed_hunks + synthesis_status
```

## Common Patterns

### Pattern 1: Simple Text Change
```
old_string: "methodName(args)"
new_string: "renamedMethod(args)"
context: ±3 lines (method call + surrounding code)
```

### Pattern 2: Multi-line Block
```
old_string: "for (int i = 0; i < size; i++) { ... }"
new_string: "items.stream().forEach(...)"
context: ±5 lines (method signature + loop + closing brace)
```

### Pattern 3: Import Addition
```
old_string: "// no change to code"
new_string: "// still no change"
imports_added: ["import org.new.api.*"]
Apply import modification separately
```

## Debugging

Check synthesis results:
```bash
# In results.json after shadow run:
"agent6_synthesizer": {
  "synthesized_count": 18,
  "failed_count": 2,
  "synthesis_status": "partial",
  "elapsed_s": 3.2,
  "synthesized_hunks": [
    {
      "file_path": "...",
      "old_string": "...",
      "new_string": "...",
      "confidence": 0.89,
      "context_lines_included": 5,
      "verified": true
    }
  ],
  "failed_hunks": [
    {
      "hunk_index": 12,
      "file_path": "...",
      "error": "old_string not found even with max_context",
      "reason": "File diverged significantly"
    }
  ]
}
```

## Edge Cases

### Case 1: Same old_string in Multiple Locations
```python
if count_occurrences(old_string, file_content) > 1:
    # Replace first occurrence only
    # Log warning: "Multiple matches; replaced first"
    result = file_content.replace(old_string, new_string, 1)
```

### Case 2: Empty old_string
```python
if not old_string.strip():
    # Can't verify empty strings
    failed_hunks.append({
        "error": "Empty old_string after context extraction"
    })
```

### Case 3: Very Large Files
```python
# For files > 10MB, read only needed sections
start_line = loc_result.start_line
end_line = loc_result.end_line
# Read file[start-100 : end+100]  # ~200 lines max
```

## Future Enhancements

- Parallel verification via `Send` API
- Caching of file reads (avoid re-reading same file)
- Incremental context expansion (smarter retry strategy)
- Statistical analysis (which context_lines most successful?)
- Backup/rollback (save pre-write state)
