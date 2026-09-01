"""Deprecated entry point — the machinery moved into `harness/`.

`python shell.py <graph> ...` keeps working, unchanged, because muscle memory
and shell history are real; but the thing it invokes is the harness, and new
code should import from there. The name "shell" described the file's job —
own the side effects around a pure graph — and that job now has its proper
noun: this repo's runtime is a HARNESS, a graph is a program it runs, a
cartridge configures who the run works for, and a runner executes its nodes.
"""

from harness.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
