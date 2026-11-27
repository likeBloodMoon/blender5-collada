#!/usr/bin/env python3
"""Regression test for #47: .cdae export of a skinned mesh warns and exports rest pose by default."""

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
import math
import os
import sys
import bpy

repo_root, tmpdir = sys.argv[-2], sys.argv[-1]
sys.path.insert(0, repo_root)
import io_scene_collada
io_scene_collada.register()

# Build an armature and a cube skinned to "Bone"
bpy.ops.wm.read_homefile(use_empty=True)
bpy.ops.object.armature_add(enter_editmode=False, location=(0, 0, 0))
arm_ob = bpy.context.object
arm_ob.name = "Rig"
bone_name = arm_ob.data.bones[0].name

bpy.ops.mesh.primitive_cube_add(size=2.0, location=(0, 0, 0))
body = bpy.context.object
body.name = "Body"
vg = body.vertex_groups.new(name=bone_name)
vg.add(list(range(len(body.data.vertices))), 1.0, "REPLACE")
mod = body.modifiers.new("Armature", "ARMATURE")
mod.object = arm_ob
body.parent = arm_ob

# Record rest-pose bounding box
dg = bpy.context.evaluated_depsgraph_get()
me_rest = body.evaluated_get(dg).to_mesh()
rest_max_x = max(v.co.x for v in me_rest.vertices)
body.evaluated_get(dg).to_mesh_clear()

# Pose the bone by rotating 60 degrees around Z and translating +5 on X
pbone = arm_ob.pose.bones[bone_name]
pbone.rotation_mode = "XYZ"
pbone.rotation_euler = (0.0, 0.0, math.radians(60.0))
pbone.location = (5.0, 0.0, 0.0)
bpy.context.view_layer.update()

# Sanity: evaluated posed mesh is moved away from rest pose
dg = bpy.context.evaluated_depsgraph_get()
me_posed = body.evaluated_get(dg).to_mesh()
posed_max_x = max(v.co.x for v in me_posed.vertices)
body.evaluated_get(dg).to_mesh_clear()
assert posed_max_x > rest_max_x + 3.0, f"expected posed mesh moved, got {posed_max_x} vs {rest_max_x}"

# Export via CdaeExporter directly to inspect warnings AND verify operator behavior
from io_scene_collada.cdae.exporter import CdaeExporter, CdaeExportSettings

cdae_path = os.path.join(tmpdir, "skinned.cdae")
exporter = CdaeExporter(bpy.context, CdaeExportSettings(filepath=cdae_path))
ok = exporter.export_document()
assert ok, f"cdae export failed: {exporter.errors}"

# 1. Warning must be emitted mentioning Body / skinned / rest pose
assert any("Body" in w and "skinned" in w.lower() for w in exporter.warnings), (
    f"#47 failed: expected skinned mesh warning, got {exporter.warnings}"
)
# 2. Armature pose_position must be restored to POSE after export
assert arm_ob.data.pose_position == "POSE", f"pose_position not restored: {arm_ob.data.pose_position}"

# 3. Re-import .cdae and verify exported mesh is in REST pose, not posed
bpy.ops.wm.read_homefile(use_empty=True)
res = bpy.ops.import_scene.beamng_cdae(filepath=cdae_path)
assert res == {"FINISHED"}
imp_body = bpy.data.objects["Body"]
imp_max_x = max((imp_body.matrix_world @ v.co).x for v in imp_body.data.vertices)
assert abs(imp_max_x - rest_max_x) < 1e-3, (
    f"#47 failed: imported max_x={imp_max_x:.4f}, expected rest_max_x={rest_max_x:.4f} (posed was {posed_max_x:.4f})"
)

print(f"PASS test_cdae_skinned_warning (#47: rest_max_x={imp_max_x:.4f}, warnings={len(exporter.warnings)})")
"""


def main() -> int:
    blender = find_blender5()
    with tempfile.TemporaryDirectory(prefix="collada_test_cdae_skin_") as tmpdir:
        worker_py = os.path.join(tmpdir, "worker.py")
        with open(worker_py, "w", encoding="utf-8") as fh:
            fh.write(WORKER)
        proc = subprocess.run(
            [blender, "-b", "--factory-startup", "--python-exit-code", "1", "--python", worker_py, "--", REPO_ROOT, tmpdir],
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
