"""Console entry point that preserves the controller as the only runtime entrance."""
from __future__ import annotations

import sys
from pathlib import Path


def main() -> None:
    package_dir = Path(__file__).resolve().parent / "retro2"
    sys.path.insert(0, str(package_dir))
    from controller.run import main as controller_main

    controller_main()


if __name__ == "__main__":
    main()

