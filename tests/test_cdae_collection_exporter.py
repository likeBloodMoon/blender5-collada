#!/usr/bin/env python3
"""Regression test for #50: .cdae FileHandler bl_export_operator and Collection Exporter support."""

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

# 1. FileHandler registers bl_export_operator for .cdae
fh = bpy.types.IO_FH_beamng_cdae
assert getattr(fh, "bl_export_operator", "") == "export_scene.beamng_cdae", (
    f"#50 failed: IO_FH_beamng_cdae.bl_export_operator={getattr(fh, 'bl_export_operator', None)!r}"
)

# 2. Build two collections: Wheel (WheelCyl) and Outside (OutsideCube)
bpy.ops.wm.read_homefile(use_empty=True)
wheel_coll = bpy.data.collections.new("Wheel")
bpy.context.scene.collection.children.link(wheel_coll)

bpy.ops.mesh.primitive_cylinder_add()
cyl = bpy.context.object
cyl.name = "WheelCyl"
for c in list(cyl.users_collection):
    c.objects.unlink(cyl)
wheel_coll.objects.link(cyl)

bpy.ops.mesh.primitive_cube_add(location=(5, 0, 0))
cube = bpy.context.object
cube.name = "OutsideCube"

# 3. Direct operator call with collection="Wheel"
cdae_direct = os.path.join(tmpdir, "wheel_direct.cdae")
res = bpy.ops.export_scene.beamng_cdae(filepath=cdae_direct, collection="Wheel")
assert res == {"FINISHED"}, f"direct collection export failed: {res}"

bpy.ops.wm.read_homefile(use_empty=True)
res = bpy.ops.import_scene.beamng_cdae(filepath=cdae_direct)
assert res == {"FINISHED"}
names_direct = sorted(o.name for o in bpy.data.objects if o.type == "MESH")
assert names_direct == ["WheelCyl"], f"#50 direct failed: expected ['WheelCyl'], got {names_direct}"

# 4. Real Blender Collection Exporter panel workflow (exporters.new + collection.exporter_export)
bpy.ops.wm.read_homefile(use_empty=True)
wheel_coll = bpy.data.collections.new("Wheel")
bpy.context.scene.collection.children.link(wheel_coll)

bpy.ops.mesh.primitive_cylinder_add()
cyl = bpy.context.object
cyl.name = "WheelCyl"
for c in list(cyl.users_collection):
    c.objects.unlink(cyl)
wheel_coll.objects.link(cyl)

bpy.ops.mesh.primitive_cube_add(location=(5, 0, 0))
cube = bpy.context.object
cube.name = "OutsideCube"

cdae_panel = os.path.join(tmpdir, "wheel_panel.cdae")
exp = wheel_coll.exporters.new("IO_FH_beamng_cdae")
exp.export_properties.filepath = cdae_panel
with bpy.context.temp_override(collection=wheel_coll):
    res = bpy.ops.collection.exporter_export(index=0)
assert res == {"FINISHED"}, f"collection.exporter_export failed: {res}"
assert os.path.exists(cdae_panel), "collection exporter did not write .cdae"

bpy.ops.wm.read_homefile(use_empty=True)
res = bpy.ops.import_scene.beamng_cdae(filepath=cdae_panel)
assert res == {"FINISHED"}
names_panel = sorted(o.name for o in bpy.data.objects if o.type == "MESH")
assert names_panel == ["WheelCyl"], f"#50 panel failed: expected ['WheelCyl'], got {names_panel}"

print(f"PASS test_cdae_collection_exporter (#50: direct={names_direct}, panel={names_panel})")
"""


def main() -> int:
    blender = find_blender5()
    with tempfile.TemporaryDirectory(prefix="collada_test_cdae_coll_") as tmpdir:
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
