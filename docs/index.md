# captain-hook

**Declarative hooks for Claude Code.** Define rules that intercept tool calls, enforce policies, and guide agent behavior — in pure Python.

```python
from captain_hook import block_command

block_command(["git", "stash"], reason="Use jj instead", hint="Run `jj shelve`")
```

That's it. One line blocks `git stash` across your entire project.

---

<div class="grid cards" markdown>

-   :material-shield-check:{ .lg .middle } **Declarative by default**

    ---

    Most hooks are a single function call. No classes, no boilerplate, no YAML. Define what to block, warn, or enforce — captain-hook handles the rest.

-   :material-filter:{ .lg .middle } **Composable conditions**

    ---

    Filter hooks with typed conditions: match tools, file paths, commands, transcript history, and more. Combine with `only_if` / `skip_if` for precise targeting.

-   :material-brain:{ .lg .middle } **LLM-powered evaluation**

    ---

    Gate or nudge with LLM verdicts. Signal scoring detects patterns in transcript text, then an LLM decides whether to intervene.

-   :material-test-tube:{ .lg .middle } **Inline testing**

    ---

    Test hooks where you define them. `Input(command="git stash")` / `Block("jj")` — run with `captain-hook test`.

-   :material-format-list-checks:{ .lg .middle } **Multi-step workflows**

    ---

    Enforce checklists before the agent stops. Tests, linting, artifacts — each step must pass before the agent can proceed.

-   :material-message-text:{ .lg .middle } **Rich transcript API**

    ---

    Query conversation history with a typed API. Filter tool uses, extract commands, check what files were edited or read.

</div>

---

## Get started

=== "Install"

    ```bash
    uv add cc-captain-hook
    ```

=== "Scaffold"

    ```bash
    captain-hook init
    ```

=== "Write a hook"

    ```python
    from captain_hook import gate, RanCommand

    gate("Run tests before stopping", skip_if=[RanCommand(r"pytest")])
    ```

[:octicons-arrow-right-24: Installation](getting-started/installation.md){ .md-button .md-button--primary }
[:octicons-arrow-right-24: Quickstart](getting-started/quickstart.md){ .md-button }
