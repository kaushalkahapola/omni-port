# Agent 4: Namespace Adapter Agent (`agent4_namespace.py`)

Agent 4 is the **first LLM-assisted processing agent**. It handles hunks where the API has drifted between branches — method names changed, imports shifted, symbols got renamed. It uses a balanced LLM (Sonnet 4.6) to perform symbol remapping and import adjustments so the code can apply correctly.

## Core Responsibility

**Adapt hunks that have API drift, import changes, or symbol renames but whose core logic remains intact.**

Agent 4 receives hunks that Agent 3 couldn't apply directly (exact-string match failed). It analyzes:
1. Symbol mappings from JavaParser/GumTree localization
2. Import statement differences between original and target
3. Method signature changes

Then uses the LLM to produce adapted hunks ready for synthesis.

## Routing Rules

**Agent 4 claims a hunk if ANY of:**
1. Localization found `symbol_mappings` (javaparser or gumtree_ast with symbols detected)
2. Hunk diff contains differing import statements
3. `old_content` not found verbatim in target file (indicates API name drift)
4. Localization method is `javaparser` or `gumtree_ast` (higher-level analysis detected structural changes)

**Agent 4 passes to Agent 5 if:**
- Hunk requires deep structural refactoring beyond symbol remapping
- Localization method is `embedding` (semantic divergence, need reasoning)
- Confidence < 0.6 (low confidence needs extended reasoning)

## Algorithm

### Input
- Unclaimed hunks (from Agent 3's `processed_hunk_indices`)
- `localization_results`: Includes `symbol_mappings` dict
- Target file content and context
- `target_repo_path`: Path to codebase

### Processing Steps

```
For each unclaimed hunk:
  1. Check if hunk needs namespace adaptation:
     - Has symbol_mappings from localization?
     - Contains import statement changes?
     - old_content not found verbatim in target?
     → If any true, claim this hunk
  
  2. Build LLM prompt with:
     - Original hunk old_content / new_content
     - Symbol mappings: {original_symbol → target_symbol}
     - Import differences
     - Target file context (surrounding lines)
     - Class/method signatures in target
  
  3. Call LLM (Sonnet 4.6) with structured output:
     Prompt: "Given the following API changes, adapt the hunk..."
     Output: NamespaceAdaptationOutput {
       adapted_old_content,
       adapted_new_content,
       imports_added,
       imports_removed,
       notes,
       success,
       error_message
     }
  
  4. Validate LLM output:
     - Ensure adapted_old_content is not empty
     - Check if import lists make sense
     - Verify no syntax errors introduced
  
  5. Store adaptation result:
     adapted_hunks.append({
       original_hunk_index: i,
       old_content: adapted_old_content,
       new_content: adapted_new_content,
       imports_added: imports_added,
       imports_removed: imports_removed,
       confidence: llm_confidence,
       notes: llm_notes
     })
  
  6. Mark hunk as processed:
     processed_hunk_indices.append(i)
```

### Output

Updates `BackportState` with:
- `adapted_hunks`: List of LLM-adapted hunks
  ```python
  {
    "hunk_index": 5,
    "file_path": "src/main/java/org/elasticsearch/api/Client.java",
    "adapted_old_content": "Client oldClient = ...",
    "adapted_new_content": "Client newClient = ...",
    "imports_added": ["import org.elasticsearch.newapi.Client"],
    "imports_removed": ["import org.elasticsearch.oldapi.Client"],
    "confidence": 0.92,
    "notes": "Remapped old Client to new Client; updated imports"
  }
  ```
- `processed_hunk_indices`: Updated with claimed hunk indices
- `tokens_used`: Incremented by LLM call cost

## Example

### Scenario: Method Renamed Between Branches

**Original hunk (found in source branch):**
```java
// Old API
SearchRequest req = new SearchRequest();
req.setQuery(query);
client.execute(req);
```

**Target branch has new API:**
```java
// New API (refactored)
SearchQueryBuilder builder = new SearchQueryBuilder();
builder.withQuery(query);
client.executeSearch(builder);
```

### Localization Result
```python
LocalizationResult(
  method_used="javaparser",
  confidence=0.75,
  symbol_mappings={
    "SearchRequest": "SearchQueryBuilder",
    "setQuery": "withQuery",
    "execute": "executeSearch"
  }
)
```

### Processing

**Agent 3 failed:** old_content not found (API names changed)

**Agent 4 action:**
1. Detects symbol_mappings in localization result
2. Builds prompt for LLM:
   ```
   Symbol mappings detected:
   - SearchRequest → SearchQueryBuilder
   - setQuery → withQuery
   - execute → executeSearch
   
   Original code:
   SearchRequest req = new SearchRequest();
   req.setQuery(query);
   client.execute(req);
   
   Adapt this code to the target API...
   ```

3. LLM returns:
   ```
   adapted_old_content: "SearchRequest req = new SearchRequest();\nreq.setQuery(query);\nclient.execute(req);"
   adapted_new_content: "SearchQueryBuilder builder = new SearchQueryBuilder();\nbuilder.withQuery(query);\nclient.executeSearch(builder);"
   imports_added: ["import org.elasticsearch.newapi.SearchQueryBuilder"]
   imports_removed: ["import org.elasticsearch.oldapi.SearchRequest"]
   success: true
   ```

4. Store adapted hunk → Agent 6 will synthesize and apply to disk

## Import Management

Agent 4 explicitly handles import statements:

### Detecting Import Changes
```python
def _has_import_changes(hunk):
    old_imports = {line for line in hunk["old_content"].splitlines() 
                   if line.strip().startswith("import ")}
    new_imports = {line for line in hunk["new_content"].splitlines() 
                   if line.strip().startswith("import ")}
    return old_imports != new_imports
```

### Adding/Removing Imports
- `imports_added`: Full statements to add to target file
- `imports_removed`: Full statements to remove

Example:
```python
imports_added=[
  "import org.elasticsearch.newapi.SearchQueryBuilder;",
  "import org.elasticsearch.newapi.Query;"
],
imports_removed=[
  "import org.elasticsearch.oldapi.SearchRequest;",
  "import org.elasticsearch.oldapi.OldQuery;"
]
```

Agent 6 applies these at the top of the file.

## LLM Prompt Structure

```
You are Agent 4 (Namespace Adapter) for the OmniPort backporting system.

The following Java code hunk is from a mainline patch that needs to be backported.
The target branch has API changes: method renames, class renames, or different structure.

Symbol Mappings (detected by JavaParser/GumTree):
{symbol_mappings}

Target File Context (surrounding code):
{target_context}

Original hunk to adapt:
{original_old_content}
↓↓↓ becomes ↓↓↓
{original_new_content}

Task:
1. Apply symbol mappings to adapt the old_content
2. Adapt the new_content to match target API
3. Preserve original intent and semantics
4. Return adapted_old_content and adapted_new_content as exact strings

Output JSON:
{
  "adapted_old_content": "...",
  "adapted_new_content": "...",
  "imports_added": ["..."],
  "imports_removed": ["..."],
  "notes": "Brief explanation of changes",
  "success": true
}
```

## Confidence Scoring

Agent 4's adapted hunks are assigned confidence based on:
- LLM's internal confidence in the adaptation
- Number of symbols mapped (more = higher confidence)
- Whether imports were cleanly resolved
- Syntax validation of adapted code

```python
confidence = 0.95 if len(symbol_mappings) > 0 else 0.80
if not syntax_valid(adapted_code):
    confidence -= 0.15
```

## Performance

- **Time per hunk:** ~3-5 seconds (LLM call)
- **Token cost:** ~500-1000 tokens per hunk
- **Total for 5 hunks:** ~25 seconds, ~5000 tokens

## Tools Used

- `LLMRouter`: Routes to Sonnet 4.6 model
- `NamespaceAdaptationOutput` (Pydantic): Structured output validation
- JavaParser symbol_mappings (from Agent 1)

## Model

**Sonnet 4.6** — Balanced reasoning model for symbol mapping and API adaptation

## Integration with Pipeline

```
Agent 3 (Fast-Apply) applies trivial hunks
        ↓
Unclaimed hunks → Agent 4 (Namespace Adapter)
        ↓
Agent 4 remaps symbols and imports
        ↓
Adapted hunks + remaining hunks → Agent 5 (Structural Refactor)
        ↓
Agent 5 handles deep structural changes
        ↓
Agent 6 synthesizes all and applies to disk
```

## Common Patterns

### Pattern 1: Simple Method Rename
```
Old API: obj.oldMethodName(args)
New API: obj.newMethodName(args)

Symbol mapping: oldMethodName → newMethodName
Adaptation: Straightforward rename
```

### Pattern 2: Constructor Change
```
Old API: new OldClass(arg1, arg2)
New API: OldClass.builder().withArg1(arg1).withArg2(arg2).build()

Symbol mapping: OldClass → Builder pattern
Adaptation: Restructure instantiation
```

### Pattern 3: Import Consolidation
```
Old API: import org.old.package.*;
New API: import org.new.package.*;

Symbol mapping: All symbols from old → new package
Adaptation: Update imports + symbol usage
```

## Edge Cases

### Case 1: Symbol Mapping Incomplete
If localization only found partial mappings:
- Attempt LLM adaptation anyway (context helps)
- Mark lower confidence
- Pass to Agent 5 if confidence < 0.6

### Case 2: Conflicting Mappings
If symbol appears in multiple contexts with different mappings:
- Use target file context to disambiguate
- LLM selects appropriate mapping per occurrence
- Mark for verification in Agent 6

### Case 3: No Imports to Add/Remove
If hunk doesn't change imports:
- Set `imports_added = []` and `imports_removed = []`
- Agent 6 skips import modification
- Proceed normally

## Debugging

Check adapted hunk quality:
```bash
# In results.json after shadow run:
"agent4_namespace": {
  "adapted_count": 3,
  "elapsed_s": 12.5,
  "adapted_hunks": [
    {
      "hunk_index": 2,
      "file_path": "...",
      "adapted_old_content": "...",
      "adapted_new_content": "...",
      "imports_added": [...],
      "imports_removed": [...],
      "confidence": 0.92,
      "notes": "..."
    }
  ]
}
```

## Future Enhancements

- Caching of learned symbol mappings (across patches)
- API diff repository (japicmp pre-analysis)
- Import statement grouping (organize by package)
- Validation via compilation before passing to Agent 6
