"""Read-only checkpoint/process audit; never resumes or clears protection."""
import argparse
import json
from pathlib import Path
from x_workbench.continuation_view import audit_continuations


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=Path(__file__).resolve().parents[1]/'outputs/cursor_continuation_20261012')
    args=parser.parse_args()
    print(json.dumps({'accounts':audit_continuations(args.root)},ensure_ascii=False,indent=2))


if __name__=='__main__':main()
