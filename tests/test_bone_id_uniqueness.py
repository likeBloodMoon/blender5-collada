"""Regression test for #42: bones like 'Bone L' and 'Bone_L' export unique id/sid and round-trip names.

Verifies the exact repro from #42:
  - Armature with bones 'Bone L' and 'Bone_L' (plus 'Bone.L' -> three colliding sanitized names)
    and a skinned cube with vertex groups for all three.
  - Exported .dae has NO duplicate XML id= attributes and unique joint SIDs in Name_array.
  - Re-import restores exact original bone names and vertex group names ('Bone L', 'Bone_L', 'Bone.L').

Usage:
    python tests/test_bone_id_uniqueness.py
    BLENDER_5X=... python tests/test_bone_id_uniqueness.py
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
import xml.etree.ElementTree as ET

import bpy

argv = sys.argv[sys.argv.index("--") + 1:]
repo_root, workdir, out_json = argv[0], argv[1], argv[2]
sys.path.insert(0, repo_root)

import io_scene_collada
io_scene_collada.register()

bpy.ops.wm.read_homefile(use_empty=True)
bpy.ops.object.armature_add()
rig = bpy.context.object
bpy.ops.object.mode_set(mode="EDIT")
rig.data.edit_bones["Bone"].name = "Bone L"
b2 = rig.data.edit_bones.new("Bone_L")
b2.head = (1, 0, 0)
b2.tail = (1, 0, 1)
b3 = rig.data.edit_bones.new("Bone.L")
b3.head = (2, 0, 0)
b3.tail = (2, 0, 1)
bpy.ops.object.mode_set(mode="OBJECT")

bpy.ops.mesh.primitive_cube_add(location=(0.5, 0, 0.5))
body = bpy.context.object
body.name = "Body"
body.vertex_groups.new(name="Bone L").add([0, 1, 2], 1.0, "REPLACE")
body.vertex_groups.new(name="Bone_L").add([3, 4, 5], 1.0, "REPLACE")
body.vertex_groups.new(name="Bone.L").add([6, 7], 1.0, "REPLACE")
body.parent = rig
body.modifiers.new("Armature", "ARMATURE").object = rig

dae_path = os.path.join(workdir, "bonename.dae")
assert "FINISHED" in bpy.ops.wm.collada_export(filepath=dae_path)

# Check XML uniqueness of id= and Name_array entries
tree = ET.parse(dae_path)
root = tree.getroot()
all_ids = [el.attrib["id"] for el in root.iter() if "id" in el.attrib]
dup_ids = sorted({x for x in all_ids if all_ids.count(x) > 1})

name_arrays = []
for el in root.iter():
    if el.tag.endswith("Name_array") and el.text:
        name_arrays.append(el.text.split())

# Re-import and verify original names restored
bpy.ops.wm.read_homefile(use_empty=True)
assert "FINISHED" in bpy.ops.wm.collada_import(filepath=dae_path)

arm_imp = next((o for o in bpy.data.objects if o.type == "ARMATURE"), None)
body_imp = bpy.data.objects.get("Body")

res = {
    "dup_ids": dup_ids,
    "joint_sids": name_arrays[0] if name_arrays else [],
    "imp_bones": sorted(b.name for b in arm_imp.data.bones) if arm_imp else [],
    "imp_vgroups": sorted(vg.name for vg in body_imp.vertex_groups) if body_imp else [],
}
with open(out_json, "w", encoding="utf-8") as f:
    json.dump(res, f, indent=2)
'''


def main() -> int:
    blender = _find_blender()
    workdir = tempfile.mkdtemp(prefix="test_bone_id_42_")
    try:
        worker_py = os.path.join(workdir, "worker.py")
        out_json = os.path.join(workdir, "out.json")
        with open(worker_py, "w", encoding="utf-8") as f:
            f.write(WORKER_SCRIPT)

        proc = subprocess.run(
            [blender, "-b", "--factory-startup", "--python-exit-code", "1",
             "--python", worker_py, "--", REPO_ROOT, workdir, out_json],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if proc.returncode != 0:
            print("STDOUT:\n", proc.stdout)
            print("STDERR:\n", proc.stderr)
            raise RuntimeError(f"Blender worker failed with exit code {proc.returncode}")

        with open(out_json, encoding="utf-8") as f:
            res = json.load(f)

        expected = sorted(["Bone L", "Bone_L", "Bone.L"])
        failures: list[str] = []
        if res["dup_ids"]:
            failures.append(f"Duplicate XML IDs in exported .dae: {res['dup_ids']}")
        if len(res["joint_sids"]) != len(set(res["joint_sids"])):
            failures.append(f"Duplicate joint SIDs in Name_array: {res['joint_sids']}")
        if res["imp_bones"] != expected:
            failures.append(f"Imported bones expected {expected}, got {res['imp_bones']}")
        if res["imp_vgroups"] != expected:
            failures.append(f"Imported vertex groups expected {expected}, got {res['imp_vgroups']}")

        if failures:
            print("FAIL test_bone_id_uniqueness:")
            for msg in failures:
                print(f"  - {msg}")
            return 1

        print(f"PASS test_bone_id_uniqueness (#42: unique SIDs={res['joint_sids']}, restored={res['imp_bones']})")
        return 0
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
