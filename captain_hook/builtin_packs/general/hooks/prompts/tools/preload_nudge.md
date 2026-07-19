Before anything else on your first turn, load the always-used deferred tools in one batch:

ToolSearch with query "select:TaskCreate,TaskGet,TaskList,TaskOutput,TaskStop,TaskUpdate,Monitor,SendMessage,EnterPlanMode,ExitPlanMode" and max_results 10.

One call covers the whole set — do not load these names one at a time as you first need them. Names already resident or absent in this version simply don't match; that is harmless. Leave every other deferred tool (Cron*, Design*, rarely-used MCP tools) unloaded until the task at hand actually calls for it.
