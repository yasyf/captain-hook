# API Reference

Auto-generated from source docstrings.

## Core

### HookApp

::: captain_hook.app.HookApp

### Event

::: captain_hook.types.Event

### Action

::: captain_hook.types.Action

### HookResult

::: captain_hook.types.HookResult

### HookSpec

::: captain_hook.types.HookSpec

### HookContext

::: captain_hook.context.HookContext

---

## Events

### BaseHookEvent

::: captain_hook.events.BaseHookEvent

### PreToolUseEvent

::: captain_hook.events.PreToolUseEvent

### PostToolUseEvent

::: captain_hook.events.PostToolUseEvent

### PostToolUseFailureEvent

::: captain_hook.events.PostToolUseFailureEvent

### StopEvent

::: captain_hook.events.StopEvent

### SubagentStopEvent

::: captain_hook.events.SubagentStopEvent

### SubagentStartEvent

::: captain_hook.events.SubagentStartEvent

### UserPromptSubmitEvent

::: captain_hook.events.UserPromptSubmitEvent

### NotificationEvent

::: captain_hook.events.NotificationEvent

---

## Conditions

### Tool

::: captain_hook.types.Tool

### FilePath

::: captain_hook.types.FilePath

### Command

::: captain_hook.types.Command

### Content

::: captain_hook.types.Content

### Agent

::: captain_hook.types.Agent

### TestFile

::: captain_hook.types.TestFile

### ReadFile

::: captain_hook.types.ReadFile

### TouchedFile

::: captain_hook.types.TouchedFile

### RanCommand

::: captain_hook.types.RanCommand

### UsedSkill

::: captain_hook.types.UsedSkill

### InPlanMode

::: captain_hook.types.InPlanMode

### CustomCondition

::: captain_hook.types.CustomCondition

---

## Primitives

### nudge

::: captain_hook.primitives.nudge.nudge

### gate

::: captain_hook.primitives.nudge.gate

### lint

::: captain_hook.primitives.lint.lint

### block_command

::: captain_hook.primitives.commands.block_command

### warn_command

::: captain_hook.primitives.commands.warn_command

### llm_gate

::: captain_hook.primitives.llm.llm_gate

### llm_nudge

::: captain_hook.primitives.llm.llm_nudge

### prompt_check

::: captain_hook.primitives.llm.prompt_check

### llm_evaluate

::: captain_hook.primitives.llm.llm_evaluate

---

## Signals

### Signal

::: captain_hook.types.Signal

### Signals

::: captain_hook.types.Signals

### score_signals

::: captain_hook.signals.score_signals

### extract_signal_context

::: captain_hook.signals.extract_signal_context

### transcript_texts

::: captain_hook.signals.transcript_texts

### cite_message

::: captain_hook.signals.cite_message

### NlpSignal

::: captain_hook.signals.nlp.NlpSignal

### Clause

::: captain_hook.signals.nlp.Clause

### Phrase

::: captain_hook.signals.nlp.Phrase

### nlp_scan

::: captain_hook.signals.nlp.nlp_scan

---

## Transcript

### Transcript

::: captain_hook.transcript.Transcript

### TranscriptMessage

::: captain_hook.transcript.TranscriptMessage

### TranscriptSlice

::: captain_hook.transcript.TranscriptSlice

### Turn

::: captain_hook.transcript.Turn

### ToolUse

::: captain_hook.transcript.ToolUse

### ToolUseQuery

::: captain_hook.transcript.ToolUseQuery

### ToolUseSequence

::: captain_hook.transcript.ToolUseSequence

---

## Transcript Inputs

### BashInput

::: captain_hook.transcript.inputs.BashInput

### EditInput

::: captain_hook.transcript.inputs.EditInput

### WriteInput

::: captain_hook.transcript.inputs.WriteInput

### ReadInput

::: captain_hook.transcript.inputs.ReadInput

### AgentInput

::: captain_hook.transcript.inputs.AgentInput

### GrepInput

::: captain_hook.transcript.inputs.GrepInput

### GlobInput

::: captain_hook.transcript.inputs.GlobInput

### SkillInput

::: captain_hook.transcript.inputs.SkillInput

### GenericInput

::: captain_hook.transcript.inputs.GenericInput

### parse_tool_input

::: captain_hook.transcript.inputs.parse_tool_input

---

## Transcript Models

### TextBlock

::: captain_hook.transcript.models.TextBlock

### ToolUseBlock

::: captain_hook.transcript.models.ToolUseBlock

### ToolResult

::: captain_hook.transcript.models.ToolResult

### parse_content_block

::: captain_hook.transcript.models.parse_content_block

---

## File

### File

::: captain_hook.file.File

### PathMatcher

::: captain_hook.file.PathMatcher

---

## Command

### CommandLine

::: captain_hook.command.CommandLine

### Command

::: captain_hook.command.Command

### Redirect

::: captain_hook.command.Redirect

---

## Workflow

### workflow

::: captain_hook.workflow.workflow

### Workflow

::: captain_hook.workflow.Workflow

### Step

::: captain_hook.workflow.Step

### Artifact

::: captain_hook.workflow.Artifact

### text_matches

::: captain_hook.workflow.text_matches

---

## State & Session

### SessionStore

::: captain_hook.session.SessionStore

### SessionSlot

::: captain_hook.session.SessionSlot

### HookState

::: captain_hook.state.HookState

### PrimitiveState

::: captain_hook.state.PrimitiveState

---

## Settings

### HooksSettings

::: captain_hook.settings.HooksSettings

### AutoConf

::: captain_hook.settings.AutoConf

### build_settings

::: captain_hook.settings.build_settings

---

## Prompt

### Prompt

::: captain_hook.prompt.Prompt

### PromptMessage

::: captain_hook.prompt.PromptMessage

---

## Testing

### mock_event

::: captain_hook.testing.helpers.mock_event

### mock_tool_event

::: captain_hook.testing.helpers.mock_tool_event

### mock_stop_event

::: captain_hook.testing.helpers.mock_stop_event

### mock_subagent_start_event

::: captain_hook.testing.helpers.mock_subagent_start_event

### mock_subagent_stop_event

::: captain_hook.testing.helpers.mock_subagent_stop_event

### mock_user_prompt_event

::: captain_hook.testing.helpers.mock_user_prompt_event

### dispatch_test

::: captain_hook.testing.helpers.dispatch_test

### assert_result

::: captain_hook.testing.helpers.assert_result

### run_inline_tests

::: captain_hook.testing.helpers.run_inline_tests

### Input

::: captain_hook.testing.Input

### Block

::: captain_hook.testing.Block

### Warn

::: captain_hook.testing.Warn

### Allow

::: captain_hook.testing.Allow

### TranscriptFixture

::: captain_hook.testing.TranscriptFixture

---

## Dispatch

### dispatch

::: captain_hook.dispatch.dispatch

### execute_hook

::: captain_hook.dispatch.execute_hook

### format_output

::: captain_hook.dispatch.format_output

### run_declarative

::: captain_hook.dispatch.run_declarative

---

## Conditions (functions)

### check_condition

::: captain_hook.conditions.check_condition

### matches_conditions

::: captain_hook.conditions.matches_conditions

---

## Utilities

### tokens_to_regex

::: captain_hook.types.tokens_to_regex

### hook

::: captain_hook.app.hook

### get_current_app

::: captain_hook.app.get_current_app
