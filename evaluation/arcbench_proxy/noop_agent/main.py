from __future__ import annotations

import argparse


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("requirements_dir")
    parser.add_argument("--output-dir", required=True)
    parser.parse_args()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
