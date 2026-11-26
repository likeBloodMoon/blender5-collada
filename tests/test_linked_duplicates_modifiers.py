#!/usr/bin/env python3
"""Regression test for #43: linked duplicates with different modifiers must not share one geometry."""

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
import xml.etree.ElementTree as ET
import bpy

tmpdir = sys.argv[-1]

# Case 1: Exact repro from #43 (Plain + PlainCopy unmodified, Arrayed with Array count=3)
bpy.ops.wm.read_homefile(use_empty=True)
bpy.ops.mesh.primitive_cube_add()
plain = bpy.context.object
plain.name = "Plain"
plain_copy = bpy.data.objects.new("PlainCopy", plain.data)
bpy.context.scene.collection.objects.link(plain_copy)
plain_copy.location.x = -4.0

arr = bpy.data.objects.new("Arrayed", plain.data)
bpy.context.scene.collection.objects.link(arr)
arr.location.x = 4.0
arr.modifiers.new("Array", "ARRAY").count = 3

dae1 = os.path.join(tmpdir, "inst.dae")
res = bpy.ops.wm.collada_export(
    filepath=dae1,
    apply_modifiers=True,
    use_object_instantiation=True,
)
assert res == {"FINISHED"}

# Unmodified linked duplicates (Plain, PlainCopy) should still share 1 geometry, Arrayed gets its own -> 2 geometries total
root = ET.parse(dae1).getroot()
ns = {"c": "http://www.collada.org/2005/11/COLLADASchema"}
geoms = root.findall(".//c:library_geometries/c:geometry", ns)
assert len(geoms) == 2, f"expected 2 geometries (shared plain + arrayed), got {len(geoms)}"

bpy.ops.wm.read_homefile(use_empty=True)
res = bpy.ops.wm.collada_import(filepath=dae1)
assert res == {"FINISHED"}
vcounts = {o.name: len(o.data.vertices) for o in bpy.data.objects if o.type == "MESH"}
assert vcounts == {"Plain": 8, "PlainCopy": 8, "Arrayed": 24}, f"#43 failed: got {vcounts}"

# Case 2: Modified object has the same name as ob.data.name ("Cube") while Plain shares it
bpy.ops.wm.read_homefile(use_empty=True)
bpy.ops.mesh.primitive_cube_add()
mod_cube = bpy.context.object
mod_cube.name = "Cube"
mod_cube.data.name = "Cube"
mod_cube.modifiers.new("Array", "ARRAY").count = 3
plain2 = bpy.data.objects.new("Plain", mod_cube.data)
bpy.context.scene.collection.objects.link(plain2)
plain2.location.x = -4.0

dae2 = os.path.join(tmpdir, "inst_same_name.dae")
res = bpy.ops.wm.collada_export(
    filepath=dae2,
    apply_modifiers=True,
    use_object_instantiation=True,
)
assert res == {"FINISHED"}

bpy.ops.wm.read_homefile(use_empty=True)
res = bpy.ops.wm.collada_import(filepath=dae2)
assert res == {"FINISHED"}
vcounts2 = {o.name: len(o.data.vertices) for o in bpy.data.objects if o.type == "MESH"}
assert vcounts2 == {"Plain": 8, "Cube": 24}, f"#43 same-name failed: got {vcounts2}"

print(f"PASS test_linked_duplicates_modifiers (#43: {vcounts}, same_name={vcounts2})")
"""


def main() -> int:
    blender = find_blender5()
    with tempfile.TemporaryDirectory(prefix="collada_test_inst_") as tmpdir:
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
