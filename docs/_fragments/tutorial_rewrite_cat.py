from captain_hook import rewrite_command

rewrite_command(
    r"^cat\s+(\S+)$",
    r"ccx read \1 --full",
    note="ccx read renders with syntax highlighting and line numbers",
)
