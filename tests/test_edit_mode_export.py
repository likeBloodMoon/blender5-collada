"""Regression tests for #35: exporting while in Edit Mode writes unsaved edits.

Verifies that exporting a mesh while still in Edit Mode flushes unsaved edits
(min z = 4.0 instead of -1.0 after translating by (0, 0, 5)) for:
  - .dae with apply_modifiers=False
  - .dae with apply_modifiers=True
  - .cdae with apply_modifiers=False
  - .cdae with apply_modifiers=True
and leaves the object in Edit Mode after export.

Usage:
    python tests/test_edit_mode_export.py
    BLENDER_5X=... python tests/test_edit_mode_export.py
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


def run_case(fmt: str, apply_modifiers: bool) -> dict:
    bpy.ops.wm.read_homefile(use_empty=True)
    bpy.ops.mesh.primitive_cube_add()
    ob = bpy.context.active_object
    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.mesh.select_all(action="SELECT")
    bpy.ops.transform.translate(value=(0, 0, 5))

    out_path = os.path.join(workdir, f"edit_{fmt}_mod{int(apply_modifiers)}.{fmt}")
    if fmt == "dae":
        res = bpy.ops.wm.collada_export(filepath=out_path, apply_modifiers=apply_modifiers)
    else:
        res = bpy.ops.export_scene.beamng_cdae(filepath=out_path, apply_modifiers=apply_modifiers)
    assert "FINISHED" in res, f"Export failed for {fmt} apply_modifiers={apply_modifiers}: {res}"
    mode_after = ob.mode

    bpy.ops.object.mode_set(mode="OBJECT")
    bpy.ops.wm.read_homefile(use_empty=True)
    if fmt == "dae":
        res = bpy.ops.wm.collada_import(filepath=out_path)
    else:
        res = bpy.ops.import_scene.beamng_cdae(filepath=out_path)
    assert "FINISHED" in res, f"Import failed for {fmt}: {res}"

    mesh_ob = next(o for o in bpy.data.objects if o.type == "MESH")
    min_z = min(v.co.z for v in mesh_ob.data.vertices)
    return {"min_z": min_z, "mode_after": mode_after}


results = {}
for fmt in ("dae", "cdae"):
    for apply_m in (False, True):
        key = f"{fmt}_mod{int(apply_m)}"
        results[key] = run_case(fmt, apply_m)

with open(out_json, "w", encoding="utf-8") as f:
    json.dump(results, f, indent=2)
'''


def main() -> None:
    blender = _find_blender()
    workdir = tempfile.mkdtemp(prefix="collada_edit_mode_")
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
            results = json.load(f)

        print("Results:", json.dumps(results, indent=2))
        for key, info in results.items():
            assert abs(info["min_z"] - 4.0) < 1e-3, f"{key}: expected min_z=4.0, got {info['min_z']}"
            assert info["mode_after"] == "EDIT", f"{key}: expected mode_after=EDIT, got {info['mode_after']}"
        print("PASS: Edit Mode export (#35) verified for .dae and .cdae")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    main()
