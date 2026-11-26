"""Reload at a service boundary, after the active job has returned."""
import os
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]

def signature():
    return tuple((str(p),p.stat().st_mtime_ns) for d in (root,root/'scripts',root/'dashboard') for p in sorted(d.glob('*.py')))

initial = signature()

def reload_changed():
    takeover = os.getenv('RH_YIELD_TO_AGENT')
    if takeover and Path(takeover).exists():
        raise SystemExit(0)
    if signature() != initial:
        os.execv(sys.executable,[sys.executable,*sys.argv])
