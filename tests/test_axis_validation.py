#!/usr/bin/env python3
"""Regression test for #48: collinear Forward/Up axes refused from Python and swapped in UI."""

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

bpy.ops.wm.read_homefile(use_empty=True)
bpy.ops.mesh.primitive_cube_add()

# 1. Exact repro from #48: Forward=Z, Up=Z with apply_global_orientation=True must be refused
dae_same = os.path.join(tmpdir, "same.dae")
try:
    res = bpy.ops.wm.collada_export(
        filepath=dae_same,
        export_global_forward_selection="Z",
        export_global_up_selection="Z",
        apply_global_orientation=True,
    )
    raise AssertionError(f"#48 failed: expected RuntimeError/CANCELLED for Z/Z, got {res}")
except RuntimeError as exc:
    assert "same axis" in str(exc).lower(), f"unexpected error message: {exc}"
assert not os.path.exists(dae_same), "#48 failed: file should not be written when cancelled"

# 2. Opposite collinear axes (Y / -Y) must also be refused
dae_opp = os.path.join(tmpdir, "opp.dae")
try:
    res = bpy.ops.wm.collada_export(
        filepath=dae_opp,
        export_global_forward_selection="Y",
        export_global_up_selection="-Y",
        apply_global_orientation=True,
    )
    raise AssertionError(f"#48 failed: expected RuntimeError/CANCELLED for Y/-Y, got {res}")
except RuntimeError as exc:
    assert "same axis" in str(exc).lower(), f"unexpected error message: {exc}"
assert not os.path.exists(dae_opp), "#48 failed: file should not be written for Y/-Y"

# 3. Valid perpendicular axes (-Z / Y) must succeed
dae_ok = os.path.join(tmpdir, "ok.dae")
res = bpy.ops.wm.collada_export(
    filepath=dae_ok,
    export_global_forward_selection="-Z",
    export_global_up_selection="Y",
    apply_global_orientation=True,
)
assert res == {"FINISHED"}, f"#48 failed: expected FINISHED for -Z/Y, got {res}"
assert os.path.exists(dae_ok)

# 4. UI axis-swap helper swaps collinear axis to a perpendicular axis
from bl_ext.user_default.collada_io import _pick_swapped_axis

swapped_up = _pick_swapped_axis("Z", "Z", "Y")
assert swapped_up.lstrip("-") != "Z", f"expected non-Z swapped axis, got {swapped_up}"
assert swapped_up == "Y", f"expected swap to previous axis Y, got {swapped_up}"

swapped_fallback = _pick_swapped_axis("Z", "-Z", "Z")
assert swapped_fallback.lstrip("-") != "Z", f"expected fallback non-Z axis, got {swapped_fallback}"

print("PASS test_axis_validation (#48: Z/Z and Y/-Y refused, -Z/Y ok, UI swap verified)")
"""


def main() -> int:
    blender = find_blender5()
    with tempfile.TemporaryDirectory(prefix="collada_test_axis_") as tmpdir:
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
