"""评测入口：python eval/run.py [--task ID] [--runs N] [--model NAME]。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eval.runner import run_cli  # noqa: E402

if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    raise SystemExit(run_cli())
