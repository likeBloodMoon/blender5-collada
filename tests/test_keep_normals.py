"""Regression tests for #10: preserve pre-triangulation corner normals on .dae and .cdae export.

Verifies:
  1. custom_normals (wavy grid with custom split normals): max normal error < 0.1 deg (.dae & .cdae)
  2. nonplanar_quads (flat-shaded twisted quads): max normal error < 0.1 deg (.dae & .cdae)
  3. monkey_sharp_edges (subdivided Suzanne, sharp edges at 30 deg): p99 < 0.1 deg and 0 corners > 5 deg
     (excluding any flipped triangles from diagonal choice)
  4. keep_normals=False restores legacy post-triangulation normal behavior
  5. keep_normals property defaults to True on wm.collada_export, export_scene.beamng_cdae,
     and Collection Exporter (IO_FH_collada)

Usage:
    python tests/test_keep_normals.py
    BLENDER_5X=... python tests/test_keep_normals.py
"""

from __future__ import annotations

import glob
import os
import shutil
import subprocess
import sys
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _find_blender() -> str:
    env_p = os.environ.get("BLENDER_5X")
    if env_p and os.path.exists(env_p):
        return env_p
    exe = "blender.exe" if os.name == "nt" else "blender"
    matches = sorted(glob.glob(os.path.join(REPO_ROOT, ".tools", "blender-5.*", exe)))
    if matches:
        return matches[-1]
    raise RuntimeError("Could not find Blender 5.x (set BLENDER_5X)")


WORKER_SCRIPT = r'''
import json
import math
import os
import random
import sys
import bmesh
import bpy
import numpy as np
from mathutils import Vector
from mathutils.bvhtree import BVHTree
from mathutils.kdtree import KDTree

argv = sys.argv[sys.argv.index("--") + 1:]
repo_root, workdir, out_json = argv[0], argv[1], argv[2]
sys.path.insert(0, repo_root)

import io_scene_collada
try:
    io_scene_collada.register()
except ValueError:
    pass


def clear_scene():
    for coll in (bpy.data.objects, bpy.data.meshes, bpy.data.materials, bpy.data.collections):
        for item in list(coll):
            try:
                coll.remove(item)
            except Exception:
                pass


def new_obj(name, bm, rot=(0, 0, 0), smooth=True):
    me = bpy.data.meshes.new(name)
    bm.to_mesh(me)
    bm.free()
    me.shade_smooth() if smooth else me.shade_flat()
    ob = bpy.data.objects.new(name, me)
    bpy.context.scene.collection.objects.link(ob)
    ob.rotation_euler = rot
    return ob


def build_case(case):
    rnd = random.Random(42)
    if case == "custom_normals":
        bm = bmesh.new()
        bmesh.ops.create_grid(bm, x_segments=12, y_segments=12, size=2.0)
        for v in bm.verts:
            v.co.z = 0.2 * math.sin(v.co.x * 2) * math.cos(v.co.y * 2)
        ob = new_obj("custom_normals", bm, smooth=True)
        me = ob.data
        nrm = [
            Vector((0.4 * math.sin(v.co.x * 3), 0.4 * math.cos(v.co.y * 3), 1.0)).normalized()
            for v in me.vertices
        ]
        me.normals_split_custom_set_from_vertices(nrm)
    elif case == "nonplanar_quads":
        bm = bmesh.new()
        bmesh.ops.create_grid(bm, x_segments=8, y_segments=8, size=2.0)
        for v in bm.verts:
            v.co.z = rnd.uniform(-0.3, 0.3)
        new_obj("nonplanar_quads", bm, smooth=False)
    elif case == "monkey_sharp_edges":
        bm = bmesh.new()
        bm.loops.layers.uv.new("UVMap")
        bmesh.ops.create_monkey(bm, calc_uvs=True)
        bmesh.ops.subdivide_edges(bm, edges=bm.edges[:], cuts=1, use_grid_fill=True)
        ob = new_obj("monkey_sharp_edges", bm, rot=(0, 0, 0.5), smooth=True)
        ob.data.set_sharp_from_angle(angle=math.radians(30))
    else:
        raise ValueError(case)
    bpy.context.view_layer.update()


def extract():
    dg = bpy.context.evaluated_depsgraph_get()
    obs = [o for o in bpy.context.scene.objects if o.type == "MESH"]
    P, N, POLY = [], [], []
    poly_off = 0
    for ob in obs:
        oe = ob.evaluated_get(dg)
        me = oe.to_mesh()
        mw = oe.matrix_world.copy()
        nm = mw.to_3x3().inverted_safe().transposed()
        me.calc_loop_triangles()
        nt = len(me.loop_triangles)
        if nt == 0:
            oe.to_mesh_clear()
            continue
        tl = np.empty(nt * 3, np.int32)
        me.loop_triangles.foreach_get("loops", tl)
        tp = np.empty(nt, np.int32)
        me.loop_triangles.foreach_get("polygon_index", tp)
        lv = np.empty(len(me.loops), np.int32)
        me.loops.foreach_get("vertex_index", lv)
        co = np.empty(len(me.vertices) * 3, np.float64)
        me.vertices.foreach_get("co", co)
        co = co.reshape(-1, 3)
        M4 = np.array(mw)
        co_w = co @ M4[:3, :3].T + M4[:3, 3]
        P.append(co_w[lv[tl]].reshape(nt, 3, 3))
        cn = np.empty(len(me.loops) * 3, np.float32)
        me.corner_normals.foreach_get("vector", cn)
        cnw = cn.reshape(-1, 3).astype(np.float64) @ np.array(nm).T
        cnw /= np.maximum(np.linalg.norm(cnw, axis=1, keepdims=True), 1e-12)
        N.append(cnw[tl].reshape(nt, 3, 3))
        POLY.append(tp + poly_off)
        poly_off += len(me.polygons)
        oe.to_mesh_clear()
    return dict(P=np.concatenate(P), N=np.concatenate(N), POLY=np.concatenate(POLY))


def tri_area_normal(P):
    cr = np.cross(P[:, 1] - P[:, 0], P[:, 2] - P[:, 0])
    a = np.linalg.norm(cr, axis=1)
    return 0.5 * a, cr / np.maximum(a, 1e-30)[:, None]


def compare_normals(gt, im):
    gvP = gt["P"].reshape(-1, 3)
    kd = KDTree(len(gvP))
    for i, v in enumerate(gvP):
        kd.insert(Vector(v), i)
    kd.balance()
    tol = max(1e-4, 1e-6 * float(np.abs(gvP).max()))

    def polys_at(v):
        return {int(gt["POLY"][i // 3]) for (_, i, _) in kd.find_range(Vector(v), tol)}

    gtri = np.arange(len(gvP)).reshape(-1, 3)
    bvh_g = BVHTree.FromPolygons([tuple(v) for v in gvP], [tuple(t) for t in gtri], epsilon=0.0)
    pools = {}
    for t, p in enumerate(gt["POLY"]):
        pools.setdefault(int(p), []).append(t)
    poly_n = {}
    for p, ts in pools.items():
        a, n = tri_area_normal(gt["P"][ts])
        v = (n * a[:, None]).sum(axis=0)
        poly_n[p] = v / max(np.linalg.norm(v), 1e-30)

    ia, inn = tri_area_normal(im["P"])
    cent = im["P"].mean(axis=1)
    nerr_all = []
    nerr_unflipped = []
    flipped = 0
    for t in range(len(im["P"])):
        s0 = polys_at(im["P"][t, 0]) & polys_at(im["P"][t, 1]) & polys_at(im["P"][t, 2])
        if len(s0) == 1:
            sp = next(iter(s0))
        elif len(s0) > 1:
            sp = min(s0, key=lambda p: np.linalg.norm(gt["P"][pools[p]].reshape(-1, 3).mean(axis=0) - cent[t]))
        else:
            sp = int(gt["POLY"][bvh_g.find_nearest(Vector(cent[t]))[2]])
        is_flip = bool(ia[t] > 1e-12 and float(np.dot(inn[t], poly_n[sp])) < -0.5)
        if is_flip:
            flipped += 1
        cand = pools[sp]
        cp = gt["P"][cand].reshape(-1, 3)
        for k in range(3):
            j = int(np.argmin(np.linalg.norm(cp - im["P"][t, k], axis=1)))
            st, sk = cand[j // 3], j % 3
            c = float(np.clip(np.dot(gt["N"][st, sk], im["N"][t, k]), -1.0, 1.0))
            ang = math.degrees(math.acos(c))
            nerr_all.append(ang)
            if not is_flip:
                nerr_unflipped.append(ang)

    a_all = np.array(nerr_all)
    a_unf = np.array(nerr_unflipped)
    return {
        "flipped": flipped,
        "max_all": float(a_all.max()),
        "max_unflipped": float(a_unf.max()),
        "p99_unflipped": float(np.percentile(a_unf, 99)),
        "gt5_unflipped": int((a_unf > 5.0).sum()),
    }


results = {}

# Check operator & Collection Exporter properties
clear_scene()
dae_props = bpy.ops.wm.collada_export.get_rna_type().properties
cdae_props = bpy.ops.export_scene.beamng_cdae.get_rna_type().properties
results["dae_has_keep_normals"] = "keep_normals" in dae_props
results["dae_default_keep_normals"] = bool(dae_props["keep_normals"].default) if "keep_normals" in dae_props else None
results["cdae_has_keep_normals"] = "keep_normals" in cdae_props
results["cdae_default_keep_normals"] = bool(cdae_props["keep_normals"].default) if "keep_normals" in cdae_props else None

col = bpy.data.collections.new("TestExpCol")
bpy.context.scene.collection.children.link(col)
exp_entry = col.exporters.new(type="IO_FH_collada")
results["coll_exp_has_keep_normals"] = hasattr(exp_entry.export_properties, "keep_normals")
results["coll_exp_default_keep_normals"] = (
    bool(exp_entry.export_properties.keep_normals)
    if hasattr(exp_entry.export_properties, "keep_normals")
    else None
)

for case in ("custom_normals", "nonplanar_quads", "monkey_sharp_edges"):
    clear_scene()
    build_case(case)
    gt = extract()

    # DAE default (keep_normals=True)
    dae_path = os.path.join(workdir, f"{case}_keep.dae")
    assert "FINISHED" in bpy.ops.wm.collada_export(filepath=dae_path)
    clear_scene()
    assert "FINISHED" in bpy.ops.wm.collada_import(filepath=dae_path)
    results[f"dae_keep|{case}"] = compare_normals(gt, extract())

    # CDAE default (keep_normals=True)
    clear_scene()
    build_case(case)
    cdae_path = os.path.join(workdir, f"{case}_keep.cdae")
    assert "FINISHED" in bpy.ops.export_scene.beamng_cdae(filepath=cdae_path)
    clear_scene()
    assert "FINISHED" in bpy.ops.import_scene.beamng_cdae(filepath=cdae_path)
    results[f"cdae_keep|{case}"] = compare_normals(gt, extract())

# Verify keep_normals=False restores legacy behavior on custom_normals
if results["dae_has_keep_normals"] and results["cdae_has_keep_normals"]:
    clear_scene()
    build_case("custom_normals")
    gt = extract()
    dae_legacy = os.path.join(workdir, "custom_normals_legacy.dae")
    assert "FINISHED" in bpy.ops.wm.collada_export(filepath=dae_legacy, keep_normals=False)
    clear_scene()
    assert "FINISHED" in bpy.ops.wm.collada_import(filepath=dae_legacy)
    results["dae_legacy|custom_normals"] = compare_normals(gt, extract())

    clear_scene()
    build_case("custom_normals")
    cdae_legacy = os.path.join(workdir, "custom_normals_legacy.cdae")
    assert "FINISHED" in bpy.ops.export_scene.beamng_cdae(filepath=cdae_legacy, keep_normals=False)
    clear_scene()
    assert "FINISHED" in bpy.ops.import_scene.beamng_cdae(filepath=cdae_legacy)
    results["cdae_legacy|custom_normals"] = compare_normals(gt, extract())

with open(out_json, "w", encoding="utf-8") as fh:
    json.dump(results, fh, indent=2)
'''


def main() -> int:
    blender = _find_blender()
    print(f"Running #10 keep_normals tests using: {blender}")
    workdir = tempfile.mkdtemp(prefix="collada_keep_normals_")
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
        import json

        res = json.load(open(out_json, "r", encoding="utf-8"))
        for k, v in res.items():
            print(f"  {k}: {v}")

        assert res["dae_has_keep_normals"] and res["dae_default_keep_normals"] is True, res
        assert res["cdae_has_keep_normals"] and res["cdae_default_keep_normals"] is True, res
        assert res["coll_exp_has_keep_normals"] and res["coll_exp_default_keep_normals"] is True, res

        for fmt in ("dae_keep", "cdae_keep"):
            for case in ("custom_normals", "nonplanar_quads"):
                r = res[f"{fmt}|{case}"]
                assert r["max_all"] < 0.1, f"{fmt}|{case} max_all={r['max_all']:.4f} >= 0.1"
            r_mon = res[f"{fmt}|monkey_sharp_edges"]
            assert r_mon["p99_unflipped"] < 0.1, f"{fmt}|monkey p99={r_mon['p99_unflipped']:.4f} >= 0.1"
            assert r_mon["gt5_unflipped"] == 0, f"{fmt}|monkey gt5={r_mon['gt5_unflipped']} != 0"

        assert res["dae_legacy|custom_normals"]["max_all"] > 10.0, res["dae_legacy|custom_normals"]
        assert res["cdae_legacy|custom_normals"]["max_all"] > 10.0, res["cdae_legacy|custom_normals"]

        print("\nAll #10 keep_normals regression tests passed!")
        return 0
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
