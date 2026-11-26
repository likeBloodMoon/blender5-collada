#!/usr/bin/env python3
"""Regression test for #44: include_all_actions exports unassigned (fake-user) actions."""

from __future__ import annotations

import glob
import os
import shutil
import subprocess
import sys
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS_DIR = os.path.join(REPO_ROOT, ".tools")


def find_blender5() -> str:
    env = os.environ.get("BLENDER_5X")
    if env and os.path.exists(env):
        return env
    for pat in (
        os.path.join(TOOLS_DIR, "blender-5.*", "blender.exe"),
        os.path.join(TOOLS_DIR, "blender-5.*", "blender"),
    ):
        hits = sorted(glob.glob(pat))
        if hits:
            return hits[-1]
    which = shutil.which("blender")
    if which:
        return which
    raise RuntimeError("Blender 5.x executable not found; set BLENDER_5X")


WORKER = r"""
import os
import sys
import bpy

tmpdir = sys.argv[-1]

# Exact repro from #44
bpy.ops.wm.read_homefile(use_empty=True)
bpy.ops.mesh.primitive_cube_add()
ob = bpy.context.object
ob.keyframe_insert("location", frame=1)
ob.location.x = 5.0
ob.keyframe_insert("location", frame=20)
ob.animation_data.action.name = "Walk"
ob.animation_data.action.use_fake_user = True
ob.animation_data.action = None
ob.location.x = 0.0
ob.keyframe_insert("location", frame=1)
ob.location.y = 5.0
ob.keyframe_insert("location", frame=20)
ob.animation_data.action.name = "Run"

# 1. include_all_actions=True must export both Walk and Run
dae_all = os.path.join(tmpdir, "actions_all.dae")
res = bpy.ops.wm.collada_export(
    filepath=dae_all,
    include_animations=True,
    include_all_actions=True,
)
assert res == {"FINISHED"}
txt_all = open(dae_all, encoding="utf-8").read()
assert "Walk" in txt_all and "Run" in txt_all, (
    f"#44 failed: Walk in dae={'Walk' in txt_all}, Run in dae={'Run' in txt_all}"
)
# Original active action must be restored
assert ob.animation_data.action is not None and ob.animation_data.action.name == "Run", (
    f"active action not restored: {ob.animation_data.action}"
)

# 2. include_all_actions=False must export only Run, not Walk
dae_active = os.path.join(tmpdir, "actions_active.dae")
res = bpy.ops.wm.collada_export(
    filepath=dae_active,
    include_animations=True,
    include_all_actions=False,
)
assert res == {"FINISHED"}
txt_active = open(dae_active, encoding="utf-8").read()
assert "Run" in txt_active and "Walk" not in txt_active, (
    f"include_all_actions=False failed: Walk={'Walk' in txt_active}, Run={'Run' in txt_active}"
)

print("PASS test_include_all_actions (#44: Walk+Run exported with include_all_actions=True, Run-only with False)")
"""


def main() -> int:
    blender = find_blender5()
    with tempfile.TemporaryDirectory(prefix="collada_test_actions_") as tmpdir:
        worker_py = os.path.join(tmpdir, "worker.py")
        with open(worker_py, "w", encoding="utf-8") as fh:
            fh.write(WORKER)
        proc = subprocess.run(
            [blender, "-b", "--python-exit-code", "1", "--python", worker_py, "--", tmpdir],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            sys.stdout.write(proc.stdout)
            sys.stderr.write(proc.stderr)
            return proc.returncode
        for line in proc.stdout.splitlines():
            if line.startswith("PASS "):
                print(line)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
