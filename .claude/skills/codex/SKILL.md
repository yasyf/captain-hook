---
name: codex
description: Get a second opinion from OpenAI Codex CLI on difficult debugging, code analysis, or architecture problems. Use when stuck after multiple attempts.
allowed-tools: Bash(cat:*, codex:*, echo:*), Read, Grep, Glob
context: fork
effort: medium
---

# Codex Second Opinion

Get a second perspective from OpenAI's Codex CLI when stuck on difficult problems.

## When to Use

- After 2+ failed approaches to the same problem
- Debugging subtle bugs (off-by-one, race conditions, state corruption)
- Analyzing complex algorithms against specifications
- Understanding unfamiliar code patterns, protocols, or file formats
- When a fresh perspective would break a deadlock

## Workflow

### Step 1: Gather Context

Before invoking Codex, collect all relevant context using Read, Grep, and Glob.
Build a comprehensive question with:

- Clear problem statement with the specific error or symptom
- Complete functions (never truncated snippets)
- What has already been tried and why it failed
- Specific questions to answer

### Step 2: Write Question and Invoke Codex

```bash
cat <<'QUESTION' > /tmp/question.txt
I have a [component] that fails with [specific error].

Here is the full function:
```
[paste complete code]
```

Key observations:
1. [What works]
2. [What fails]
3. [When it fails]

What has been tried:
- [approach 1 and why it failed]
- [approach 2 and why it failed]

Questions:
1. [specific question]
2. [specific question]
QUESTION

cat /tmp/question.txt | codex exec -o /tmp/codex_reply.txt --full-auto
```

For harder problems, use a stronger model:
```bash
cat /tmp/question.txt | codex exec -o /tmp/codex_reply.txt --full-auto -m o3
```

### Step 3: Evaluate the Reply

```
Read /tmp/codex_reply.txt
```

Evaluate suggestions critically. Codex is helpful but not infallible -- it can occasionally misinterpret specifications. Always verify against authoritative sources before applying.

## Alternative: Direct Piping

For shorter questions:
```bash
echo "Explain the JPEG progressive AC refinement algorithm" | codex exec --full-auto
```

The file-based pattern is better for debugging because you can refine the question and keep a record.

## Response Format

Return a structured summary:

```
## Codex Analysis

**Problem:** <1 sentence>
**Codex Findings:**
1. <finding with assessment: agree/disagree/needs-verification>
2. <finding with assessment>

**Recommended Actions:**
- <concrete next step based on verified findings>

**Confidence:** <high/medium/low based on how well Codex understood the problem>
```

## Tips

1. **Provide complete code** -- don't truncate functions. Codex needs full context.
2. **Be specific** -- "Why does Huffman decoding fail after 1477 blocks in AC refinement scan?" not "Why does this fail?"
3. **Include the spec** -- if debugging against a standard, mention the relevant spec sections.
4. **Verify suggestions** -- Codex is helpful but not infallible. Always verify against authoritative sources.
5. **Iterate if needed** -- if the first response doesn't solve the problem, create a new question with additional context from what you learned.

## Common Issues

**"stdin is not a terminal"**: Use `codex exec` not bare `codex`

**No output**: Check that `-o` flag has a valid path

**Timeout**: For complex questions, Codex may take time. The `--full-auto` flag avoids interactive prompts that would block.
