"""Unit and integration tests for UX helpers (#7, #8, #9)."""

from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS_DIR = os.path.join(REPO_ROOT, ".tools")


def _find_blender() -> str:
    env = os.environ.get("BLENDER_5X")
    if env and os.path.exists(env):
        return env
    exe = "blender.exe" if os.name == "nt" else "blender"
    matches = glob.glob(os.path.join(TOOLS_DIR, "blender-5.*", exe))
    if matches:
        return matches[0]
    raise RuntimeError("Could not find Blender 5.x (set BLENDER_5X)")


WORKER_SCRIPT = r'''
import json
import os
import sys

argv = sys.argv[sys.argv.index("--") + 1:]
repo_root, out_json = argv[0], argv[1]
sys.path.insert(0, repo_root)

import io_scene_collada
from io_scene_collada.collada_utils import report_capped


class _DummyOp:
    def __init__(self):
        self.calls = []

    def report(self, level_set, msg):
        self.calls.append((sorted(level_set), msg))


# Test #7: report_capped
op1 = _DummyOp()
report_capped(op1, "WARNING", [])
assert op1.calls == [], op1.calls

op2 = _DummyOp()
report_capped(op2, "ERROR", [], fallback="Default failure")
assert op2.calls == [(["ERROR"], "Default failure")], op2.calls

op3 = _DummyOp()
report_capped(op3, "WARNING", [f"w{i}" for i in range(7)], limit=5)
assert len(op3.calls) == 6, op3.calls
assert op3.calls[:5] == [(["WARNING"], f"w{i}") for i in range(5)]
assert op3.calls[5] == (["WARNING"], "... and 2 more warning(s)"), op3.calls[5]

op4 = _DummyOp()
report_capped(op4, "ERROR", [f"e{i}" for i in range(6)], limit=5)
assert len(op4.calls) == 6 and op4.calls[5] == (["ERROR"], "... and 1 more error(s)"), op4.calls

with open(out_json, "w", encoding="utf-8") as fh:
    json.dump({"ok": True}, fh)
'''


def main() -> int:
    blender = _find_blender()
    print(f"Running UX helper tests using: {blender}")
    workdir = tempfile.mkdtemp(prefix="collada_ux_")
    try:
        worker = os.path.join(workdir, "_worker.py")
        out_json = os.path.join(workdir, "out.json")
        with open(worker, "w", encoding="utf-8") as fh:
            fh.write(WORKER_SCRIPT)
        proc = subprocess.run(
            [blender, "-b", "--factory-startup", "--python-exit-code", "1", "--python", worker, "--", REPO_ROOT, out_json],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if proc.returncode != 0:
            print(proc.stdout[-1500:])
            print(proc.stderr[-1500:])
            return 1
        assert json.load(open(out_json, "r", encoding="utf-8"))["ok"]
        print("  PASS report_capped (#7)")
        return 0
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
