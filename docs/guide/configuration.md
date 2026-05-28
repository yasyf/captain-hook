# Configuration

`HooksSettings` is the typed surface for tuning captain-hook from the outside. Every field is overridable via environment variable (default prefix `HOOKS_`) or in code on a subclass.

## Planning agents

```python
HooksSettings.planning_agents: list[str]
```

Agents whose `Event.SubagentStart` shouldn't trigger normal `PreToolUse`-style gates — they're scratch / planning, not real work. Defaults match Claude Code's built-in planning agents (`Explore`, `Plan`, `general-purpose`, and the lowercase aliases).

Override when your team adds custom planning agent types (e.g. Droid's `spec`, Conductor's `research`):

```python
class ProjectSettings(HooksSettings):
    planning_agents: list[str] = ["Explore", "Plan", "general-purpose", "research"]
```

Or via env: `HOOKS_PLANNING_AGENTS='["Explore","Plan","research"]'`.

## Waiting tools

```python
HooksSettings.waiting_tools: list[str]
```

Tool names whose presence in the recent transcript means the agent is **waiting on something external**, not actively working. The `Waiting()` condition uses this list to suppress nudges/gates that would otherwise fire while a subagent or a scheduled job is in flight.

Defaults: `Monitor`, `TeamCreate`, `ScheduleWakeup`, `SendMessage`. Add your own if you have custom long-running tools:

```python
class ProjectSettings(HooksSettings):
    waiting_tools: list[str] = [*HooksSettings.model_fields["waiting_tools"].default, "MyAsyncTask"]
```

## State directory

```python
HooksSettings.state_dir: Path     # env: CAPTAIN_HOOK_STATE_DIR
```

Where `@session_state` / `@workflow_state` models persist their JSON. Defaults to `~/.claude/state`. Set `CAPTAIN_HOOK_STATE_DIR` (or the older `CLAUDE_HOOKS_STATE_DIR`) to relocate — useful in CI sandboxes that disallow writes outside the workspace.

## Log directory

```python
HooksSettings.log_dir: Path       # env: CAPTAIN_HOOK_LOG_DIR
```

Default destination for `audit()` JSONL files and internal hook logs. Defaults to `$XDG_CACHE_HOME/captain-hook/logs` (or `~/.cache/captain-hook/logs`). Per-hook `audit(log_dir=...)` overrides this for that hook only.

## Env-variable prefix

`HooksSettings` extends `pydantic_settings.BaseSettings` with `env_prefix="HOOKS_"`. Override on your subclass to namespace project-specific settings:

```python
from pydantic_settings import SettingsConfigDict

class ProjectSettings(HooksSettings):
    model_config = SettingsConfigDict(env_prefix="MYAPP_")
    require_tests_after_edit: bool = True
```

Now `MYAPP_REQUIRE_TESTS_AFTER_EDIT=false` flips the field. Inherited fields (`planning_agents` etc.) move under the new prefix too — pick the prefix once at the project root and keep it stable.

## Wiring settings into hooks

Register the settings class via `build_settings()` so dispatch hands it to every event:

```python
from captain_hook import build_settings

settings = build_settings(ProjectSettings)
```

Inside a handler:

```python
@on(Event.PreToolUse, only_if=[Tool("Bash")])
def enforce_test_command(evt):
    s = cast(ProjectSettings, evt.ctx.c)
    if s.require_tests_after_edit and ...:
        return evt.block(...)
```
