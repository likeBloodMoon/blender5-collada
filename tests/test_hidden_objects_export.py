"""Regression tests for #36: hidden objects (H / monitor icon) are exported.

Verifies:
  1. Objects hidden with H (hide_set(True)) and Disable in Viewports (hide_viewport=True)
     are both exported by .dae, matching Blender 4.5.1.
  2. Objects in an excluded collection (layer_collection.exclude = True) are still skipped.

Usage:
    python tests/test_hidden_objects_export.py
    BLENDER_5X=... python tests/test_hidden_objects_export.py
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
for name, x in (("Shown", 0), ("HiddenEye", 3), ("HiddenMonitor", 6)):
    bpy.ops.mesh.primitive_cube_add(location=(x, 0, 0))
    bpy.context.object.name = name

bpy.data.objects["HiddenEye"].hide_set(True)
bpy.data.objects["HiddenMonitor"].hide_viewport = True

# Also create an object in an excluded collection; it must stay skipped.
excl_coll = bpy.data.collections.new("ExcludedColl")
bpy.context.scene.collection.children.link(excl_coll)
bpy.ops.mesh.primitive_cube_add(location=(9, 0, 0))
excl_ob = bpy.context.object
excl_ob.name = "ExcludedObj"
for col in list(excl_ob.users_collection):
    col.objects.unlink(excl_ob)
excl_coll.objects.link(excl_ob)
for lc in bpy.context.view_layer.layer_collection.children:
    if lc.collection == excl_coll:
        lc.exclude = True

out_path = os.path.join(workdir, "hidden.dae")
res = bpy.ops.wm.collada_export(filepath=out_path)
assert "FINISHED" in res, f"Export failed: {res}"

bpy.ops.wm.read_homefile(use_empty=True)
res = bpy.ops.wm.collada_import(filepath=out_path)
assert "FINISHED" in res, f"Import failed: {res}"

imported_names = sorted(o.name for o in bpy.data.objects)
with open(out_json, "w", encoding="utf-8") as f:
    json.dump({"imported_names": imported_names}, f, indent=2)
'''


def main() -> None:
    blender = _find_blender()
    workdir = tempfile.mkdtemp(prefix="collada_hidden_objs_")
    try:
        worker_py = os.path.join(workdir, "worker.py")
        out_json = os.path.join(workdir, "results.json")
        with open(worker_py, "w", encoding="utf-8") as f:
            f.write(WORKER_SCRIPT)
        proc = subprocess.run(
            [blender, "--background", "--factory-startup", "--python", worker_py, "--", REPO_ROOT, workdir, out_json],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            print(proc.stdout)
            print(proc.stderr)
            raise SystemExit(f"Blender worker failed (rc={proc.returncode})")

        with open(out_json, encoding="utf-8") as f:
            results = json.load(f)

        print("Results:", json.dumps(results, indent=2))
        expected = ["HiddenEye", "HiddenMonitor", "Shown"]
        assert results["imported_names"] == expected, (
            f"Expected {expected}, got {results['imported_names']}"
        )
        print("PASS: Hidden objects export (#36) verified")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    main()
