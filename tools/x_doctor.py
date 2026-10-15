"""Check the local installation without starting a browser or reading account data."""
import argparse
from pathlib import Path

from x_workbench.runtime import diagnose_runtime, print_diagnostics


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="Print a machine-readable report.")
    args = parser.parse_args()
    return print_diagnostics(diagnose_runtime(Path(__file__).resolve().parents[1]), as_json=args.json)


if __name__ == "__main__":
    raise SystemExit(main())
