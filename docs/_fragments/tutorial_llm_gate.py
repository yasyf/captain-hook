from captain_hook import Event, Runs, Tool, llm_gate

llm_gate(
    "You review database migrations for irreversible data loss. A migration is destructive when it "
    "drops or truncates a table, or deletes rows, with no backup, transaction guard, or reversible "
    "down-migration. Block only when the migration is irreversibly destructive against production data.",
    message=(
        "This migration is irreversibly destructive against production data. "
        "Snapshot the table, or add a reversible down-migration, before running it."
    ),
    events=Event.PreToolUse,
    only_if=[Tool("Bash"), Runs("psql")],
    agent=False,
    transcript=False,
)
