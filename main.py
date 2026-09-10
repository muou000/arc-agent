from __future__ import annotations

import os
import sys

from arc_main import main as arc_main


def main() -> None:
    arguments = sys.argv[1:]
    if "--type" not in arguments:
        arguments.extend(["--type", os.environ.get("ARCBENCH_TASK_TYPE", "web")])
    sys.argv = [sys.argv[0], "compile", *arguments]
    arc_main()


if __name__ == "__main__":
    main()
