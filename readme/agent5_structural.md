# Agent 5: Structural Refactor Agent (`agent5_structural.py`)

Agent 5 is the **deep reasoning agent**. It handles hunks where the code structure itself has changed significantly — control flow refactored, classes reorganized, methods split/merged. It uses the most powerful LLM (Opus 4.6 with extended thinking) to understand semantic intent and produce functionally equivalent code.

## Core Responsibility

**Adapt hunks that require deep architectural understanding, not just symbol remapping.**

Agent 5 receives hunks where:
- Localization method is `gumtree_ast` (structural AST changes detected)
- Localization confidence < 0.6 (uncertain location, needs human-like reasoning)
- Previous agents (3, 4) couldn't handle it

Agent 5's job is to read GumTree edit scripts, understand the *intent* of the original change, and produce semantically equivalent code for the target architecture.

## Routing Rules

**Agent 5 claims a hunk if ANY of:**
1. Localization method is `gumtree_ast` (structural AST-level changes)
2. Localization method is `embedding` (semantic search found it, high uncertainty)
3. Localization confidence < 0.6 (uncertain location → needs extended reasoning)
4. Previous agents explicitly marked for structural refactor

**Agent 5 passes to Agent 6 if:**
- Hunk successfully refactored (move to synthesis)
- Refactoring failed (mark as failed, Agent 6 reports)

## Algorithm

### Input
- Unclaimed hunks (from Agents 3, 4)
- `localization_results`: Includes GumTree edit scripts
- Target file/class context (larger window than Agent 4)
- japicmp API change report (if available)
- `target_repo_path`: Path to codebase

### Processing Steps

```
For each unclaimed hunk:
  1. Check if structural refactoring needed:
     - method_used == "gumtree_ast" OR
     - confidence < 0.6 OR
     - file marked for refactoring
     → If true, claim this hunk
  
  2. Gather evidence:
     - Parse GumTree edit script:
       Insert nodes, Delete nodes, Update nodes, Move nodes
     - Extract japicmp API diff (if available)
     - Read target file with large context (±20 lines)
     - Identify surrounding class/method signatures
  
  3. Build extended thinking prompt:
     ```
     You are Agent 5 (Structural Refactor).
     
     Original code intent:
     {hunk.old_content}
     
     Original change:
     {hunk.old_content} → {hunk.new_content}
     
     GumTree structural changes:
     {edit_script}
     
     API differences (japicmp):
     {api_changes}
     
     Target codebase context:
     {target_context}
     
     Task:
     1. Understand the intent of the original change
     2. Identify what structural changes the target codebase needs
     3. Produce refactored code that achieves semantic equivalence
     4. Explain your reasoning
     ```
  
  4. Call LLM (Opus 4.6) with extended thinking:
     - Budget: 8000-15000 tokens (deep analysis)
     - Time: 10-30 seconds per hunk
     - Output: StructuralAdaptationOutput {
         refactored_code,
         confidence,
         explanation,
         success,
         error_message
       }
  
  5. Validate refactored code:
     - Check syntax validity (parse as Java AST if possible)
     - Verify no obvious logic errors
     - Ensure confidence > 0.5
  
  6. Store refactoring result:
     refactored_hunks.append({
       original_hunk_index: i,
       refactored_code: llm_refactored,
       confidence: llm_confidence,
       explanation: llm_explanation,
       gumtree_edits_applied: [list of edits],
       success: True/False,
       error: error_message
     })
  
  7. Mark hunk as processed:
     processed_hunk_indices.append(i)
```

### Output

Updates `BackportState` with:
- `refactored_hunks`: List of LLM-refactored hunks
  ```python
  {
    "hunk_index": 8,
    "file_path": "src/main/java/org/elasticsearch/core/Handler.java",
    "refactored_code": """
      // Old: manual try-finally
      // New: try-with-resources (Java 7+ pattern)
      try (Resource r = acquire()) {
        process(r);
      }
    """,
    "confidence": 0.87,
    "explanation": "Refactored manual resource management to try-with-resources pattern...",
    "gumtree_edits": ["Delete try-finally", "Insert try-with-resources"],
    "success": true,
    "error": null
  }
  ```
- `processed_hunk_indices`: Updated with claimed hunk indices
- `tokens_used`: Incremented by extended-thinking LLM cost

## Example: Deep Refactoring

### Scenario: Control Flow Refactored

**Original code (source branch):**
```java
// Old pattern: manual iteration with index
for (int i = 0; i < items.size(); i++) {
    Item item = items.get(i);
    if (item.isValid()) {
        process(item);
    }
}
```

**New target API (target branch uses streams):**
```java
// New pattern: stream-based API
items.stream()
     .filter(Item::isValid)
     .forEach(this::process);
```

### GumTree Edit Script
```
Delete ForStatement
Delete AssignmentStatement
Insert MethodInvocation (items.stream())
Insert MethodInvocation (filter)
Insert MethodInvocation (forEach)
Move MethodCall (process)
```

### Localization Result
```python
LocalizationResult(
  method_used="gumtree_ast",
  confidence=0.45,  # Low confidence: pattern completely changed
  edit_script="Delete ForStatement; Insert MethodInvocation...",
  symbol_mappings={}
)
```

### Processing

**Agents 3 & 4 failed:** old_content (for loop) not found; API changed fundamentally

**Agent 5 action:**
1. Detects `gumtree_ast` + low confidence
2. Builds extended thinking prompt:
   ```
   Original intent: Iterate through valid items and process them
   
   Original code pattern:
   for (int i = 0; i < items.size(); i++) {
       Item item = items.get(i);
       if (item.isValid()) process(item);
   }
   
   GumTree edits:
   - Delete ForStatement
   - Delete manual indexing
   - Insert stream().filter().forEach()
   
   Target context shows widespread use of streams API.
   How would you refactor this to streams?
   ```

3. LLM (with extended thinking) reasons:
   ```
   Intent: Filter and process valid items
   
   Original uses C-style for loop with manual filtering.
   Target codebase heavily uses Java Streams (evident from context).
   
   Refactoring:
   - Replace for loop with items.stream()
   - Use filter(Item::isValid) instead of if statement
   - Use forEach(this::process) instead of direct call
   
   This achieves semantic equivalence and aligns with target API.
   Confidence: 0.87 (high confidence in streams translation)
   ```

4. LLM returns:
   ```
   refactored_code: """
     items.stream()
          .filter(Item::isValid)
          .forEach(this::process);
   """
   confidence: 0.87
   explanation: "Refactored C-style for loop to Java 8+ Streams API..."
   success: true
   ```

5. Store refactored hunk → Agent 6 will synthesize

## GumTree Edit Script Parsing

Agent 5 parses GumTree's structural edit operations:

```python
class EditType(str, Enum):
    INSERT = "Insert"
    DELETE = "Delete"
    UPDATE = "Update"
    MOVE = "Move"

class GumTreeEdit(BaseModel):
    operation: EditType
    node_type: str           # MethodCall, ForStatement, etc.
    old_code: Optional[str]  # What was there before
    new_code: Optional[str]  # What replaces it
    old_line: Optional[int]
    new_line: Optional[int]
```

Example parsing:
```
Script line: "Insert MethodInvocation Stream.filter()"
Parsed as:   GumTreeEdit(
               operation=INSERT,
               node_type="MethodInvocation",
               new_code="Stream.filter(...)",
               new_line=42
             )
```

## japicmp API Change Report

If available, Agent 5 uses detailed API difference information:

```json
{
  "removed_methods": [
    "java.util.Iterator Iterator.next()"
  ],
  "added_methods": [
    "java.util.stream.Stream Collection.stream()"
  ],
  "changed_signatures": [
    "forEach(Consumer) added to Iterable"
  ]
}
```

This informs refactoring decisions: "Method X no longer exists → use Y instead"

## Extended Thinking Strategy

Agent 5 uses Claude's extended thinking mode for deep analysis:

```python
response = opus_llm.invoke(
    prompt=build_refactoring_prompt(...),
    temperature=1.0,  # Required for extended thinking
    thinking_budget_tokens=15000,  # Allow deep reasoning
    max_tokens=5000  # Answer budget
)
```

This budget allows the LLM to:
1. Understand original code intent deeply
2. Analyze structural changes in detail
3. Consider multiple refactoring strategies
4. Reason about semantic equivalence
5. Validate approach before answering

## Confidence Scoring

Agent 5's refactored hunks get confidence based on:

```python
confidence = 0.0

# Start with base confidence from localization
confidence += localization_result.confidence * 0.3

# Add for successful structure mapping
if gumtree_edits_understood:
    confidence += 0.4

# Add for semantic equivalence reasoning
if llm_explained_intent:
    confidence += 0.2

# Reduce if syntax issues detected
if syntax_invalid(refactored_code):
    confidence -= 0.5

# Cap at 0.95 (deep refactoring always has some uncertainty)
confidence = min(confidence, 0.95)
```

## Model Selection

**Primary:** Opus 4.6 (deepest reasoning, extended thinking)
**Fallback:** Sonnet 4.6 (if token budget < 30%)
**Last resort:** Haiku 4.5 (if token budget < 5%, accuracy decreases)

```python
if tokens_remaining > TOKEN_BUDGET * 0.30:
    model = opus_4_6  # Full power
elif tokens_remaining > TOKEN_BUDGET * 0.05:
    model = sonnet_4_6  # Balanced
else:
    model = haiku_4_5  # Fast but less accurate
```

## Performance

- **Time per hunk:** 10-30 seconds (extended thinking)
- **Token cost:** 5000-15000 tokens per hunk
- **Total for 3 hunks:** ~1-2 minutes, ~30000 tokens

This is expensive, which is why:
1. Agents 3 & 4 handle trivial/API-drift hunks first
2. Only truly structural hunks reach Agent 5
3. Token budget tracking prevents runaway costs

## Integration with Pipeline

```
Agent 4 (Namespace Adapter) handles API drift
        ↓
Unclaimed hunks → Agent 5 (Structural Refactor)
        ↓
Agent 5 refactors deep structural changes
        ↓
Refactored hunks + remaining hunks → Agent 6 (Synthesizer)
        ↓
Agent 6 synthesizes and verifies all
```

## Common Refactoring Patterns

### Pattern 1: Loop to Streams
```
Old: for (int i = 0; i < size; i++) { process(items[i]); }
New: items.stream().forEach(this::process);
```

### Pattern 2: Try-Catch to Try-With-Resources
```
Old: try { r = open(); use(r); } finally { r.close(); }
New: try (Resource r = open()) { use(r); }
```

### Pattern 3: Manual Builder to Method Chaining
```
Old: Request r = new Request(); r.setField(v); r.build();
New: new RequestBuilder().withField(v).build();
```

### Pattern 4: Interface Implementation Change
```
Old: implements OldInterface { oldMethod() {...} }
New: implements NewInterface { newMethod() {...} }
```

## Error Handling

### Issue: LLM Returns Invalid Java
```python
try:
    ast = parse_java(refactored_code)
except SyntaxError:
    confidence -= 0.5
    if confidence < 0.5:
        success = False
        error_message = "Refactored code has syntax errors"
```

### Issue: Semantic Equivalence Uncertain
```python
if "I'm not sure" in llm_explanation or confidence < 0.6:
    agent_6_verify = True
    mark_for_compilation_check()
```

### Issue: No GumTree Edits Available
```python
if not gumtree_edits:
    # Fall back to heuristics + LLM
    refactor_without_guidance()
```

## Debugging

Check refactored hunk quality:
```bash
# In results.json after shadow run:
"agent5_structural": {
  "refactored_count": 2,
  "elapsed_s": 45.3,
  "refactored_hunks": [
    {
      "hunk_index": 8,
      "file_path": "...",
      "refactored_code": "...",
      "confidence": 0.87,
      "explanation": "...",
      "success": true,
      "error": null
    }
  ]
}
```

## Future Enhancements

- Parallel refactoring via `Send` API
- Validation via compilation + unit test execution
- Learning from refactoring patterns (store in memory)
- API change repository (pre-computed japicmp diffs)
- Extended thinking budget optimization (measure effectiveness)
