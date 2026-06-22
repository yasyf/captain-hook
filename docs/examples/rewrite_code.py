from __future__ import annotations

from captain_hook import Allow, Input, Rewrite, rewrite_code, rewrite_command

rewrite_code(
    "os.system($CMD)",
    "subprocess.run([$CMD], check=True)",
    note="os.system() spawns a shell, so subprocess.run([...], check=True) is the safer call.",
    tests={
        Input(tool="Edit", file="deploy.py", content='os.system("make build")\n'): Rewrite(),
        Input(tool="Edit", file="deploy.py", content='subprocess.run(["make", "build"], check=True)\n'): Allow(),
    },
)


rewrite_command(
    "cat $$$ARGS",
    "bat $$$ARGS",
    note="bat renders files with syntax highlighting and line numbers.",
    tests={
        Input(tool="Bash", command="cat -n pyproject.toml"): Rewrite(pattern="bat -n pyproject.toml"),
        Input(tool="Bash", command="ls -la"): Allow(),
    },
)
