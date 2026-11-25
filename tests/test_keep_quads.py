"""Regression tests for #18: Keep Quads export option (triangulate n-gons only).

Verifies:
  1. keep_quads property exists on wm.collada_export and export_scene.beamng_cdae, defaulting to False.
  2. Pure quad grid (with non-planar distortion + custom normals) exported with
     triangulate=True, keep_quads=True, keep_normals=True writes <polylist> with all 4-sided faces
     and re-imports with identical quad count, 0 tris, 0 n-gons, and max normal error < 0.1 deg.
  3. Mixed mesh (quads + tris + 5-gon + 6-gon, with custom normals and sharp edges) exported with
     triangulate=True, keep_quads=True, keep_normals=True keeps all quads, triangulates only n-gons
     (5-gon -> 3 tris, 6-gon -> 4 tris), writes <polylist> with vcount in {3, 4}, and preserves
     normals within < 0.1 deg on both quads and split n-gons.
  4. Default triangulate=True, keep_quads=False triangulates everything into <triangles>.
  5. .cdae export always exports triangles even if keep_quads=True is passed via Python API.

Usage:
    python tests/test_keep_quads.py
    BLENDER_5X=... python tests/test_keep_quads.py
"""

from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
import sys
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
import math
import os
import sys
import xml.etree.ElementTree as ET

import bmesh
import bpy
import numpy as np
from mathutils import Vector

argv = sys.argv[sys.argv.index("--") + 1:]
repo_root, workdir, out_json = argv[0], argv[1], argv[2]
sys.path.insert(0, repo_root)

import io_scene_collada
io_scene_collada.register()


def clear_scene():
    for ob in list(bpy.data.objects):
        bpy.data.objects.remove(ob, do_unlink=True)
    for col in (bpy.data.meshes, bpy.data.materials, bpy.data.images, bpy.data.armatures, bpy.data.actions):
        for item in list(col):
            col.remove(item)


def poly_counts(me):
    tris = sum(1 for p in me.polygons if len(p.vertices) == 3)
    quads = sum(1 for p in me.polygons if len(p.vertices) == 4)
    ngons = sum(1 for p in me.polygons if len(p.vertices) > 4)
    return {"tris": tris, "quads": quads, "ngons": ngons, "total": len(me.polygons)}


def parse_dae_prim_info(dae_path):
    tree = ET.parse(dae_path)
    root = tree.getroot()
    ns = {"c": "http://www.collada.org/2005/11/COLLADASchema"}
    tags = []
    vcounts = []
    for mesh in root.findall(".//c:geometry/c:mesh", ns):
        for child in mesh:
            tag = child.tag.split("}")[-1]
            if tag in ("triangles", "polylist", "polygons"):
                tags.append(tag)
                vc = child.find("c:vcount", ns)
                if vc is not None and vc.text:
                    vcounts.extend(int(x) for x in vc.text.split())
    return {"tags": tags, "vcounts": vcounts}


def snapshot_normals_by_pos(ref_me):
    ref_vco = np.empty(len(ref_me.vertices) * 3, dtype=np.float32)
    ref_me.vertices.foreach_get("co", ref_vco)
    ref_vco = ref_vco.reshape(-1, 3)

    ref_cn = np.empty(len(ref_me.loops) * 3, dtype=np.float32)
    ref_me.corner_normals.foreach_get("vector", ref_cn)
    ref_cn = ref_cn.reshape(-1, 3)

    ref_by_pos = {}
    for p in ref_me.polygons:
        for li in range(p.loop_start, p.loop_start + p.loop_total):
            vi = ref_me.loops[li].vertex_index
            key = tuple(round(float(c), 4) for c in ref_vco[vi])
            ref_by_pos.setdefault(key, []).append(ref_cn[li].copy())
    return ref_by_pos


def max_corner_normal_error_deg(ref_by_pos, got_me):
    got_vco = np.empty(len(got_me.vertices) * 3, dtype=np.float32)
    got_me.vertices.foreach_get("co", got_vco)
    got_vco = got_vco.reshape(-1, 3)

    got_cn = np.empty(len(got_me.loops) * 3, dtype=np.float32)
    got_me.corner_normals.foreach_get("vector", got_cn)
    got_cn = got_cn.reshape(-1, 3)

    max_err = 0.0
    for p in got_me.polygons:
        for li in range(p.loop_start, p.loop_start + p.loop_total):
            vi = got_me.loops[li].vertex_index
            key = tuple(round(float(c), 4) for c in got_vco[vi])
            cands = ref_by_pos.get(key)
            assert cands, f"Missing vertex {key} in reference mesh"
            gn = got_cn[li].astype(np.float64)
            gn /= max(np.linalg.norm(gn), 1e-12)
            best_ang = 180.0
            for rn in cands:
                rn64 = rn.astype(np.float64)
                rn64 /= max(np.linalg.norm(rn64), 1e-12)
                dot = float(np.clip(np.dot(gn, rn64), -1.0, 1.0))
                ang = math.degrees(math.acos(dot))
                if ang < best_ang:
                    best_ang = ang
            if best_ang > max_err:
                max_err = best_ang
    return max_err


results = {}

# 1. Operator property checks
dae_props = bpy.ops.wm.collada_export.get_rna_type().properties
cdae_props = bpy.ops.export_scene.beamng_cdae.get_rna_type().properties
results["props"] = {
    "dae_has_keep_quads": "keep_quads" in dae_props,
    "dae_default": dae_props["keep_quads"].default if "keep_quads" in dae_props else None,
    "cdae_has_keep_quads": "keep_quads" in cdae_props,
    "cdae_default": cdae_props["keep_quads"].default if "keep_quads" in cdae_props else None,
}

# 2. Pure quad grid (non-planar quads + custom normals)
clear_scene()
bm = bmesh.new()
bmesh.ops.create_grid(bm, x_segments=6, y_segments=6, size=2.0)
for v in bm.verts:
    v.co.z = 0.35 * math.sin(v.co.x * 1.3) * math.cos(v.co.y * 1.1) + 0.15 * ((v.index % 3) - 1)
me_grid = bpy.data.meshes.new("QuadGrid")
bm.to_mesh(me_grid)
bm.free()
me_grid.shade_smooth()
# Assign tilted custom normals so Keep Normals is actively tested
custom_n = []
for p in me_grid.polygons:
    for li in range(p.loop_start, p.loop_start + p.loop_total):
        co = me_grid.vertices[me_grid.loops[li].vertex_index].co
        n = Vector((0.25 * math.cos(co.x), 0.25 * math.sin(co.y), 1.0)).normalized()
        custom_n.append(tuple(n))
me_grid.normals_split_custom_set(custom_n)
ob_grid = bpy.data.objects.new("QuadGrid", me_grid)
bpy.context.scene.collection.objects.link(ob_grid)
bpy.context.view_layer.update()

ref_grid_counts = poly_counts(me_grid)
ref_grid_normals = snapshot_normals_by_pos(me_grid)

dae_quads = os.path.join(workdir, "quad_grid_keep.dae")
assert "FINISHED" in bpy.ops.wm.collada_export(filepath=dae_quads, triangulate=True, keep_quads=True, keep_normals=True)
prim_quads = parse_dae_prim_info(dae_quads)

dae_tris = os.path.join(workdir, "quad_grid_default.dae")
assert "FINISHED" in bpy.ops.wm.collada_export(filepath=dae_tris, triangulate=True)
prim_tris = parse_dae_prim_info(dae_tris)

cdae_path = os.path.join(workdir, "quad_grid.cdae")
assert "FINISHED" in bpy.ops.export_scene.beamng_cdae(filepath=cdae_path, keep_quads=True)

clear_scene()
assert "FINISHED" in bpy.ops.wm.collada_import(filepath=dae_quads)
got_grid = bpy.data.objects["QuadGrid"].data
got_grid_counts = poly_counts(got_grid)
grid_nmax = max_corner_normal_error_deg(ref_grid_normals, got_grid)

clear_scene()
assert "FINISHED" in bpy.ops.import_scene.beamng_cdae(filepath=cdae_path)
got_cdae = next(ob.data for ob in bpy.data.objects if ob.type == "MESH")
cdae_counts = poly_counts(got_cdae)

results["quad_grid"] = {
    "ref_counts": ref_grid_counts,
    "got_counts": got_grid_counts,
    "dae_keep_tags": prim_quads["tags"],
    "dae_keep_vcounts_set": sorted(set(prim_quads["vcounts"])),
    "dae_default_tags": prim_tris["tags"],
    "max_normal_err_deg": grid_nmax,
    "cdae_counts": cdae_counts,
}

# 3. Mixed mesh: 4 quads + 1 tri + 1 pentagon (5-gon -> 3 tris) + 1 hexagon (6-gon -> 4 tris)
clear_scene()
bm = bmesh.new()
# 2x2 quad grid
bmesh.ops.create_grid(bm, x_segments=2, y_segments=2, size=1.0)
# Add a separate triangle
v_t = [bm.verts.new((3.0, 0.0, 0.0)), bm.verts.new((4.0, 0.0, 0.0)), bm.verts.new((3.5, 1.0, 0.2))]
bm.faces.new(v_t)
# Add a 5-gon (slightly tilted)
v_5 = [
    bm.verts.new((3.0 + math.cos(2 * math.pi * k / 5), 3.0 + math.sin(2 * math.pi * k / 5), 0.1 * (k % 2)))
    for k in range(5)
]
bm.faces.new(v_5)
# Add a 6-gon
v_6 = [
    bm.verts.new((-3.0 + math.cos(2 * math.pi * k / 6), 3.0 + math.sin(2 * math.pi * k / 6), 0.15 * (k % 2)))
    for k in range(6)
]
bm.faces.new(v_6)

me_mix = bpy.data.meshes.new("MixedMesh")
bm.to_mesh(me_mix)
bm.free()
me_mix.shade_smooth()
# Set custom normals on all corners so Keep Normals across partial triangulation is verified
mix_n = []
for p in me_mix.polygons:
    for li in range(p.loop_start, p.loop_start + p.loop_total):
        co = me_mix.vertices[me_mix.loops[li].vertex_index].co
        n = Vector((0.3 * math.sin(co.x), -0.2 * math.cos(co.y), 1.0)).normalized()
        mix_n.append(tuple(n))
me_mix.normals_split_custom_set(mix_n)
ob_mix = bpy.data.objects.new("MixedMesh", me_mix)
bpy.context.scene.collection.objects.link(ob_mix)
bpy.context.view_layer.update()

ref_mix_counts = poly_counts(me_mix)
ref_mix_normals = snapshot_normals_by_pos(me_mix)

dae_mix = os.path.join(workdir, "mixed_keep.dae")
assert "FINISHED" in bpy.ops.wm.collada_export(filepath=dae_mix, triangulate=True, keep_quads=True, keep_normals=True)
prim_mix = parse_dae_prim_info(dae_mix)

clear_scene()
assert "FINISHED" in bpy.ops.wm.collada_import(filepath=dae_mix)
got_mix = bpy.data.objects["MixedMesh"].data
got_mix_counts = poly_counts(got_mix)
mix_nmax = max_corner_normal_error_deg(ref_mix_normals, got_mix)

results["mixed"] = {
    "ref_counts": ref_mix_counts,
    "got_counts": got_mix_counts,
    "dae_tags": prim_mix["tags"],
    "dae_vcounts_set": sorted(set(prim_mix["vcounts"])),
    "max_normal_err_deg": mix_nmax,
}

with open(out_json, "w", encoding="utf-8") as fh:
    json.dump(results, fh, indent=2)
'''


def main() -> int:
    blender = _find_blender()
    print(f"Running #18 Keep Quads tests using: {blender}")
    workdir = tempfile.mkdtemp(prefix="collada_keep_quads_")
    try:
        worker = os.path.join(workdir, "_worker.py")
        out_json = os.path.join(workdir, "results.json")
        with open(worker, "w", encoding="utf-8") as fh:
            fh.write(WORKER_SCRIPT)
        proc = subprocess.run(
            [blender, "-b", "--factory-startup", "--python-exit-code", "1", "--python", worker, "--", REPO_ROOT, workdir, out_json],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if proc.returncode != 0:
            print(proc.stdout[-2000:])
            print(proc.stderr[-2000:])
            return 1
        res = json.load(open(out_json, "r", encoding="utf-8"))
        print("  props:", res["props"])
        print("  quad_grid:", res["quad_grid"])
        print("  mixed:", res["mixed"])

        p = res["props"]
        assert p["dae_has_keep_quads"] and p["dae_default"] is False, p
        assert p["cdae_has_keep_quads"] and p["cdae_default"] is False, p

        qg = res["quad_grid"]
        assert qg["ref_counts"]["quads"] == 36 and qg["ref_counts"]["tris"] == 0
        assert qg["got_counts"] == qg["ref_counts"], f"Quad grid counts mismatch: {qg}"
        assert qg["dae_keep_tags"] == ["polylist"] and qg["dae_keep_vcounts_set"] == [4], qg
        assert qg["dae_default_tags"] == ["triangles"], qg
        assert qg["max_normal_err_deg"] < 0.1, f"Quad grid normal err {qg['max_normal_err_deg']} >= 0.1 deg"
        assert qg["cdae_counts"]["quads"] == 0 and qg["cdae_counts"]["ngons"] == 0 and qg["cdae_counts"]["tris"] == 72, qg

        mx = res["mixed"]
        assert mx["ref_counts"] == {"tris": 1, "quads": 4, "ngons": 2, "total": 7}, mx
        # 1 original tri + 3 tris from 5-gon + 4 tris from 6-gon = 8 tris; 4 quads preserved; 0 n-gons
        assert mx["got_counts"] == {"tris": 8, "quads": 4, "ngons": 0, "total": 12}, mx
        assert mx["dae_tags"] == ["polylist"] and mx["dae_vcounts_set"] == [3, 4], mx
        assert mx["max_normal_err_deg"] < 0.1, f"Mixed mesh normal err {mx['max_normal_err_deg']} >= 0.1 deg"

        print("\nAll #18 Keep Quads regression tests passed!")
        return 0
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
