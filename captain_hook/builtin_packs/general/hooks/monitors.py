from __future__ import annotations

from captain_hook import Allow, BaseHookEvent, Call, Cmd, CustomCondition, Event, Input, Tool, Warn, hook

BUFFERING = frozenset({"cut", "sed", "awk", "gawk", "mawk", "tr", "head", "tail", "grep", "egrep", "fgrep"})


def flushes_per_line(call: Call) -> bool:
    match call.name:
        case "stdbuf":
            return True
        case "sed":
            return any(
                arg == "--unbuffered" or (arg.startswith("-") and not arg.startswith("--") and "u" in arg)
                for arg in call.args
            )
        case "awk" | "gawk" | "mawk":
            return any("fflush" in arg for arg in call.args)
        case "grep" | "egrep" | "fgrep":
            return "--line-buffered" in call.args
        case _:
            return False


class StarvesMonitor(CustomCondition):
    def check(self, evt: BaseHookEvent) -> bool:
        if (cmd := Cmd.parse(evt.input.raw.get("command", ""))) is None:
            return False
        stages = [call for call in cmd.calls() if call.occurrence.nesting == 0]
        pipelines: list[list[Call]] = []
        for stage in stages:
            if pipelines and stage.occurrence.prev_op == "|":
                pipelines[-1].append(stage)
            else:
                pipelines.append([stage])
        return any(
            stage.name in BUFFERING and not flushes_per_line(stage)
            for pipeline in pipelines
            if pipeline[-1] is stages[-1] or any(map(flushes_per_line, pipeline))
            for stage in pipeline[1:]
        )


hook(
    Event.PostToolUse,
    only_if=[Tool("Monitor"), StarvesMonitor()],
    message=(
        "A block-buffered pipe stage holds Monitor's per-line events until the stream ends. "
        "Stop it with `TaskStop` and restart without the stage, or with `stdbuf -oL <stage>`, `sed -u`, "
        "or `grep --line-buffered -oE '^.{1,N}'` to truncate."
    ),
    tests={
        Input(
            tool="Monitor",
            command="cci watch --to lane --reader me | grep --line-buffered -E 'GO|HOLD' | cut -c1-600",
        ): Warn(pattern="TaskStop"),
        Input(tool="Monitor", command="tail -F run.log | sed 's/x/y/'"): Warn(pattern="TaskStop"),
        Input(tool="Monitor", command="cci watch | tr a b | grep --line-buffered x"): Warn(pattern="TaskStop"),
        Input(tool="Monitor", command="cci watch | head -n 5 | grep --line-buffered x"): Warn(pattern="TaskStop"),
        Input(tool="Monitor", command="cci watch | env LC_ALL=C cut -c1-9"): Warn(pattern="TaskStop"),
        Input(tool="Monitor", command="cci watch --to lane | grep --line-buffered x"): Allow(),
        Input(tool="Monitor", command="cci watch | stdbuf -oL cut -c1-600"): Allow(),
        Input(tool="Monitor", command="cci watch | sed -u 's/x/y/'"): Allow(),
        Input(tool="Monitor", command="cci watch | sed --unbuffered 's/x/y/'"): Allow(),
        Input(tool="Monitor", command="cci watch | awk '{print substr($0,1,300); fflush()}'"): Allow(),
        Input(tool="Monitor", command="cci watch | grep --line-buffered -oE '^.{1,300}'"): Allow(),
        Input(tool="Monitor", command="cut -c1-9 events.log"): Allow(),
        Input(tool="Monitor", command="until gh pr view 5 | jq -r .state | head -1; do sleep 5; done"): Allow(),
        Input(tool="Bash", command="cci watch | grep --line-buffered x | cut -c1-600"): Allow(),
    },
)
