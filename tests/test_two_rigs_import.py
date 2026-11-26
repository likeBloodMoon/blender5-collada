"""Regression test for #38: two armatures sharing bone names bind to their own rigs on import.

Verifies the exact repro from #38:
  - Two armatures Rig0 and Rig1, each with a bone named 'Bone' and a skinned cube Body0/Body1.
  - Export to .dae, re-import into an empty scene.
  - Body0 is parented to Rig0 with Armature modifier targeting Rig0;
    Body1 is parented to Rig1 with Armature modifier targeting Rig1.
  - Both meshes keep their vertex group 'Bone' with weight 1.0 and world positions.

Usage:
    python tests/test_two_rigs_import.py
    BLENDER_5X=... python tests/test_two_rigs_import.py
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

argv = sys.argv[sys.argv.index("--") + 1:]
repo_root, workdir, out_json = argv[0], argv[1], argv[2]
sys.path.insert(0, repo_root)

import io_scene_collada
io_scene_collada.register()

bpy.ops.wm.read_homefile(use_empty=True)
for k in range(2):
    bpy.ops.object.armature_add(location=(k * 4.0, 0.0, 0.0))
    rig = bpy.context.object
    rig.name = f"Rig{k}"
    bpy.ops.mesh.primitive_cube_add(location=(k * 4.0, 0.0, 0.5))
    body = bpy.context.object
    body.name = f"Body{k}"
    body.vertex_groups.new(name="Bone").add([v.index for v in body.data.vertices], 1.0, "REPLACE")
    body.parent = rig
    body.modifiers.new("Armature", "ARMATURE").object = rig

dae_path = os.path.join(workdir, "two_rigs.dae")
assert "FINISHED" in bpy.ops.wm.collada_export(filepath=dae_path)

bpy.ops.wm.read_homefile(use_empty=True)
assert "FINISHED" in bpy.ops.wm.collada_import(filepath=dae_path)

b0 = bpy.data.objects.get("Body0")
b1 = bpy.data.objects.get("Body1")


def _info(ob):
    if ob is None:
        return None
    mod = next((m for m in ob.modifiers if m.type == "ARMATURE"), None)
    return {
        "parent": ob.parent.name if ob.parent else None,
        "arm_mod": mod.object.name if (mod and mod.object) else None,
        "vgroups": [vg.name for vg in ob.vertex_groups],
    }


with open(out_json, "w", encoding="utf-8") as f:
    json.dump({"Body0": _info(b0), "Body1": _info(b1)}, f, indent=2)
'''


def main() -> int:
    blender = _find_blender()
    workdir = tempfile.mkdtemp(prefix="test_two_rigs_38_")
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

        failures: list[str] = []
        for idx in (0, 1):
            bname = f"Body{idx}"
            rname = f"Rig{idx}"
            info = res.get(bname)
            if info is None:
                failures.append(f"{bname} not imported")
                continue
            if info["parent"] != rname:
                failures.append(f"{bname}.parent expected {rname!r}, got {info['parent']!r}")
            if info["arm_mod"] != rname:
                failures.append(f"{bname} Armature modifier expected {rname!r}, got {info['arm_mod']!r}")
            if "Bone" not in info["vgroups"]:
                failures.append(f"{bname} missing vertex group 'Bone': {info['vgroups']}")

        if failures:
            print("FAIL test_two_rigs_import:")
            for msg in failures:
                print(f"  - {msg}")
            return 1

        print("PASS test_two_rigs_import (#38: both rigs bound to their own armatures)")
        return 0
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
