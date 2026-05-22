from __future__ import annotations

import shlex
from typing import Any


def command_step(*argv: object) -> dict[str, Any]:
    args = [str(item) for item in argv]
    return {
        "argv": args,
        "display": shlex.join(args),
    }


def command_display(step: object) -> str:
    if isinstance(step, dict):
        display = step.get("display")
        if isinstance(display, str):
            return display
        argv = step.get("argv")
        if isinstance(argv, list):
            return shlex.join(str(item) for item in argv)
    return str(step)
