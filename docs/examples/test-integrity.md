# Test Integrity

When a test fails, the path of least resistance is to weaken the test: replace the assertion with `assert True`, swap an integration call for a `Mock()`, or sprinkle `@pytest.mark.skip` to make red go green. You want an LLM reviewer that catches these patterns at the moment the test edit lands, with full diff context.

```python
--8<-- "examples/test_integrity.py"
```

**What to learn:** `prompt_check()` runs an inline LLM evaluation and returns a `HookResult | None` you can pass straight back from an `@on` handler. `Prompt.from_template(TEMPLATE, **vars)` renders the template with `str.format`-style substitution and dedents the block. The `SourceEdits(lang="py", include_tests=True) + TestFile()` combo restricts the hook to *test* file edits only, which is the only slice where weakening matters.
