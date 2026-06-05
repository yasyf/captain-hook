# Style Guide

Every team has Python conventions a linter can't express: no `print()` in committed code, no bare `except:`, no `import *`. captain-hook ships *no* rules of its own — you author them as `StyleRule` subclasses and hand them to `styleguide()`, which parses each edited file, runs every rule, and reports only the violations your edit actually introduced.

```python
--8<-- "docs/examples/styleguide.py"
```

**What to learn:** A rule is a subclass whose **docstring is the message** — `{violations}` is substituted at fire time, and the docstring doubles as the rule's API-reference text. The class name is the identity (`NoPrint` → `no-print`). Each `check` walks the post-edit AST and yields `Violation(line, label)`; the runner renders `label (line N)` and, crucially, drops any violation whose line you didn't touch — so editing one function never lights up a pre-existing `print()` elsewhere in the file. `NoNewWildcardImport` subclasses `StyleDiffRule` instead, receiving both the pre- and post-edit trees so it can flag only what the edit *added*. A single `styleguide(...)` call registers one hook; pass `block=True` or scope it with `only_if=` to register a second, differently-scoped hook.
