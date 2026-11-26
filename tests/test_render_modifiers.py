"""Regression tests for #19: modifier evaluation mode (None / Viewport / Render) on .dae & .cdae export.

Verifies:
  1. Cube with Subdivision modifier (levels=1, render_levels=2):
     - apply_modifiers=False -> 12 triangles (base cube)
     - apply_modifiers=True, export_mesh_type_selection='view' -> 48 triangles (level 1: 24 quads)
     - apply_modifiers=True, export_mesh_type_selection='render' -> 192 triangles (level 2: 96 quads)
     Tested for BOTH .dae and .cdae export.
  2. Render-only modifier (show_viewport=False, show_render=True) and viewport-only modifier
     (show_viewport=True, show_render=False) are respected in each mode.
  3. Original object's modifier properties (levels, show_viewport) are completely unchanged after export.

Usage:
    python tests/test_render_modifiers.py
    BLENDER_5X=... python tests/test_render_modifiers.py
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


def clear_scene():
    for ob in list(bpy.data.objects):
        bpy.data.objects.remove(ob, do_unlink=True)
    for col in (bpy.data.meshes, bpy.data.materials):
        for item in list(col):
            col.remove(item)


def make_subsurf_cube():
    clear_scene()
    bpy.ops.mesh.primitive_cube_add(size=2.0)
    ob = bpy.context.active_object
    ob.name = "SubCube"
    sub = ob.modifiers.new("Subsurf", "SUBSURF")
    sub.levels = 1
    sub.render_levels = 2
    bpy.context.view_layer.update()
    return ob, sub


def count_imported_tris(path: str, fmt: str) -> int:
    clear_scene()
    if fmt == "dae":
        assert "FINISHED" in bpy.ops.wm.collada_import(filepath=path)
    else:
        assert "FINISHED" in bpy.ops.import_scene.beamng_cdae(filepath=path)
    me = next(o.data for o in bpy.data.objects if o.type == "MESH")
    me.calc_loop_triangles()
    return len(me.loop_triangles)


results = {}

for fmt in ("dae", "cdae"):
    counts = {}
    for mode_name, apply_m, res_mode in (
        ("none", False, "view"),
        ("view", True, "view"),
        ("render", True, "render"),
    ):
        ob, sub = make_subsurf_cube()
        out_p = os.path.join(workdir, f"sub_{mode_name}.{fmt}")
        if fmt == "dae":
            r = bpy.ops.wm.collada_export(
                filepath=out_p,
                apply_modifiers=apply_m,
                export_mesh_type_selection=res_mode,
            )
        else:
            r = bpy.ops.export_scene.beamng_cdae(
                filepath=out_p,
                apply_modifiers=apply_m,
                export_mesh_type_selection=res_mode,
            )
        assert "FINISHED" in r, (fmt, mode_name, r)
        # Verify original modifier state was NOT mutated
        assert sub.levels == 1 and sub.render_levels == 2 and sub.show_viewport is True, (
            sub.levels, sub.render_levels, sub.show_viewport
        )
        counts[mode_name] = count_imported_tris(out_p, fmt)
    results[fmt] = counts

# Also test show_viewport vs show_render visibility on a cube
clear_scene()
bpy.ops.mesh.primitive_cube_add(size=2.0)
ob = bpy.context.active_object
m_view_only = ob.modifiers.new("ViewOnly", "SUBSURF")
m_view_only.levels = 1
m_view_only.render_levels = 1
m_view_only.show_viewport = True
m_view_only.show_render = False

m_render_only = ob.modifiers.new("RenderOnly", "SUBSURF")
m_render_only.levels = 2
m_render_only.render_levels = 2
m_render_only.show_viewport = False
m_render_only.show_render = True
bpy.context.view_layer.update()

dae_v = os.path.join(workdir, "vis_view.dae")
dae_r = os.path.join(workdir, "vis_render.dae")
assert "FINISHED" in bpy.ops.wm.collada_export(filepath=dae_v, apply_modifiers=True, export_mesh_type_selection="view")
assert "FINISHED" in bpy.ops.wm.collada_export(filepath=dae_r, apply_modifiers=True, export_mesh_type_selection="render")
assert m_view_only.show_viewport is True and m_render_only.show_viewport is False
results["vis_toggle"] = {
    "view": count_imported_tris(dae_v, "dae"),
    "render": count_imported_tris(dae_r, "dae"),
}

with open(out_json, "w", encoding="utf-8") as fh:
    json.dump(results, fh, indent=2)
'''


def main() -> int:
    blender = _find_blender()
    print(f"Running #19 Render modifier evaluation tests using: {blender}")
    workdir = tempfile.mkdtemp(prefix="collada_rendermod_")
    try:
        worker = os.path.join(workdir, "_worker.py")
        out_json = os.path.join(workdir, "out.json")
        with open(worker, "w", encoding="utf-8") as fh:
            fh.write(WORKER_SCRIPT)
        proc = subprocess.run(
            [blender, "-b", "--factory-startup", "--python-exit-code", "1", "--python", worker, "--", REPO_ROOT, workdir, out_json],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if proc.returncode != 0:
            print(proc.stdout[-1500:])
            print(proc.stderr[-1500:])
            return 1
        res = json.load(open(out_json, "r", encoding="utf-8"))
        print("  results:", res)
        assert res["dae"] == {"none": 12, "view": 48, "render": 192}, res["dae"]
        assert res["cdae"] == {"none": 12, "view": 48, "render": 192}, res["cdae"]
        assert res["vis_toggle"] == {"view": 48, "render": 192}, res["vis_toggle"]
        print("All #19 Render modifier evaluation tests passed!")
        return 0
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
