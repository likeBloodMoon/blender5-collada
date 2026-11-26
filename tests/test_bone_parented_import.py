"""Regression tests for #34: objects parented to a bone are imported on .dae import.

Verifies:
  1. Repro from #34: a mesh parented to an armature bone (parent_type='BONE') is
     imported back with parent_type='BONE', parent_bone='Bone', and matching
     world transform.
  2. A prop parented to a child bone (Bone.001) on a translated/rotated armature,
     plus a regular child object parented to that prop, all round-trip with
     exact hierarchy and world transforms.

Usage:
    python tests/test_bone_parented_import.py
    BLENDER_5X=... python tests/test_bone_parented_import.py
"""

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

import bpy
from mathutils import Vector

argv = sys.argv[sys.argv.index("--") + 1:]
repo_root, workdir, out_json = argv[0], argv[1], argv[2]
sys.path.insert(0, repo_root)

import io_scene_collada
io_scene_collada.register()


def mat_max_diff(m1, m2) -> float:
    return max(abs(m1[r][c] - m2[r][c]) for r in range(4) for c in range(4))


# Case 1: exact repro from #34
bpy.ops.wm.read_homefile(use_empty=True)
bpy.ops.object.armature_add()
rig = bpy.context.object
bpy.ops.mesh.primitive_cube_add(location=(0, 0, 2))
prop = bpy.context.object
prop.name = "Prop"
prop.parent = rig
prop.parent_type = "BONE"
prop.parent_bone = "Bone"
bpy.context.view_layer.update()
orig_prop_mw = prop.matrix_world.copy()

p1 = os.path.join(workdir, "bone_parented_simple.dae")
assert "FINISHED" in bpy.ops.wm.collada_export(filepath=p1)

bpy.ops.wm.read_homefile(use_empty=True)
assert "FINISHED" in bpy.ops.wm.collada_import(filepath=p1)
bpy.context.view_layer.update()

names1 = sorted(o.name for o in bpy.data.objects)
prop1 = bpy.data.objects.get("Prop")
case1 = {
    "names": names1,
    "has_prop": prop1 is not None,
    "parent_type": prop1.parent_type if prop1 else None,
    "parent_bone": prop1.parent_bone if prop1 else None,
    "parent_name": prop1.parent.name if (prop1 and prop1.parent) else None,
    "mw_diff": mat_max_diff(orig_prop_mw, prop1.matrix_world) if prop1 else None,
}

# Case 2: offset armature + child bone + nested child of prop
bpy.ops.wm.read_homefile(use_empty=True)
bpy.ops.object.armature_add(location=(1.5, -2.0, 0.5))
rig2 = bpy.context.object
bpy.ops.object.mode_set(mode="EDIT")
eb0 = rig2.data.edit_bones["Bone"]
eb1 = rig2.data.edit_bones.new("BoneChild")
eb1.head = eb0.tail.copy()
eb1.tail = eb0.tail + Vector((0.0, 0.0, 1.0))
eb1.parent = eb0
bpy.ops.object.mode_set(mode="OBJECT")

bpy.ops.mesh.primitive_cube_add(location=(0.5, 0.25, 1.0))
prop2 = bpy.context.object
prop2.name = "ChildBoneProp"
prop2.parent = rig2
prop2.parent_type = "BONE"
prop2.parent_bone = "BoneChild"

bpy.ops.mesh.primitive_cube_add(location=(0.0, 0.0, 0.5))
sub2 = bpy.context.object
sub2.name = "SubProp"
sub2.parent = prop2
bpy.context.view_layer.update()
orig_prop2_mw = prop2.matrix_world.copy()
orig_sub2_mw = sub2.matrix_world.copy()

p2 = os.path.join(workdir, "bone_parented_nested.dae")
assert "FINISHED" in bpy.ops.wm.collada_export(filepath=p2)

bpy.ops.wm.read_homefile(use_empty=True)
assert "FINISHED" in bpy.ops.wm.collada_import(filepath=p2)
bpy.context.view_layer.update()

imp_prop2 = bpy.data.objects.get("ChildBoneProp")
imp_sub2 = bpy.data.objects.get("SubProp")
case2 = {
    "names": sorted(o.name for o in bpy.data.objects),
    "prop_parent_type": imp_prop2.parent_type if imp_prop2 else None,
    "prop_parent_bone": imp_prop2.parent_bone if imp_prop2 else None,
    "prop_mw_diff": mat_max_diff(orig_prop2_mw, imp_prop2.matrix_world) if imp_prop2 else None,
    "sub_parent_name": imp_sub2.parent.name if (imp_sub2 and imp_sub2.parent) else None,
    "sub_mw_diff": mat_max_diff(orig_sub2_mw, imp_sub2.matrix_world) if imp_sub2 else None,
}

with open(out_json, "w", encoding="utf-8") as f:
    json.dump({"case1": case1, "case2": case2}, f, indent=2)
'''


def main() -> None:
    blender = _find_blender()
    workdir = tempfile.mkdtemp(prefix="collada_bone_parented_")
    try:
        worker_py = os.path.join(workdir, "worker.py")
        out_json = os.path.join(workdir, "results.json")
        with open(worker_py, "w", encoding="utf-8") as f:
            f.write(WORKER_SCRIPT)
        proc = subprocess.run(
            [blender, "--background", "--factory-startup", "--python-exit-code", "1", "--python", worker_py, "--", REPO_ROOT, workdir, out_json],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            print(proc.stdout)
            print(proc.stderr)
            raise SystemExit(f"Blender worker failed (rc={proc.returncode})")

        with open(out_json, encoding="utf-8") as f:
            res = json.load(f)

        print("Results:", json.dumps(res, indent=2))
        c1 = res["case1"]
        assert c1["names"] == ["Armature", "Prop"], f"case1 names: {c1['names']}"
        assert c1["parent_type"] == "BONE" and c1["parent_bone"] == "Bone", f"case1 parent: {c1}"
        assert c1["parent_name"] == "Armature", f"case1 parent_name: {c1}"
        assert c1["mw_diff"] is not None and c1["mw_diff"] < 1e-4, f"case1 mw_diff: {c1['mw_diff']}"

        c2 = res["case2"]
        assert c2["names"] == ["Armature", "ChildBoneProp", "SubProp"], f"case2 names: {c2['names']}"
        assert c2["prop_parent_type"] == "BONE" and c2["prop_parent_bone"] == "BoneChild", f"case2 prop: {c2}"
        assert c2["prop_mw_diff"] is not None and c2["prop_mw_diff"] < 1e-4, f"case2 prop_mw_diff: {c2['prop_mw_diff']}"
        assert c2["sub_parent_name"] == "ChildBoneProp", f"case2 sub_parent: {c2}"
        assert c2["sub_mw_diff"] is not None and c2["sub_mw_diff"] < 1e-4, f"case2 sub_mw_diff: {c2['sub_mw_diff']}"
        print("PASS: Bone-parented objects import (#34) verified")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    main()
