from captain_hook import Event, HookResult, PreToolUseEvent, Tool, on


@on(Event.PreToolUse, only_if=[Tool("Bash")])
def recoverable_rm(evt: PreToolUseEvent) -> HookResult | None:
    for call in evt.command.calls("rm"):
        if call.targets.expand().exhausted:
            return evt.block("rm targets too broad to verify — narrow the glob")
        # rewrite `rm` to `trash`, targets and quoting intact: a mistake becomes a restore
        return call.sub("rm", "trash", args=call.targets)
    return None
