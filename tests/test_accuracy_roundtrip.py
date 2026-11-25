"""Accuracy round-trip CI gate (#22): export -> re-import -> compare against source mesh.

Runs all 11 synthetic accuracy cases through both .dae (wm.collada_export / wm.collada_import)
and .cdae (export_scene.beamng_cdae / import_scene.beamng_cdae), using only this add-on.

First runs a selfcheck (ground-truth vs freshly rebuilt source scene) and requires exact zeros
on every metric so measurement bugs cannot mask regressions.

Usage:
    python tests/test_accuracy_roundtrip.py
    BLENDER_5X=... python tests/test_accuracy_roundtrip.py
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

# All 22 round-trip checks (11 cases x .dae/.cdae) must pass strictly.
XFAIL_CASES: dict[tuple[str, str], str] = {}

MAX_POS_ERR_M = 1e-4       # 0.1 mm
MAX_NORMAL_ERR_DEG = 0.1   # 0.1 deg
MAX_UV_ERR = 1e-5
MAX_COLOR_ERR = 1e-5


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
import random
import sys
import time

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
io_scene_collada.register()

CASES = [
    "cube_flat",
    "sphere_smooth",
    "monkey_sharp_edges",
    "custom_normals",
    "multimat_uv2_color",
    "concave_ngon",
    "nonplanar_quads",
    "hierarchy",
    "mirrored",
    "far_precision",
    "big_mesh",
]


def clear_scene():
    for coll in (
        bpy.data.objects,
        bpy.data.meshes,
        bpy.data.materials,
        bpy.data.armatures,
        bpy.data.images,
        bpy.data.node_groups,
    ):
        for item in list(coll):
            try:
                coll.remove(item)
            except Exception:
                pass
    for c in list(bpy.data.collections):
        bpy.data.collections.remove(c)


def new_obj(name, bm, loc=(0, 0, 0), rot=(0, 0, 0), scale=(1, 1, 1), parent=None, smooth=True):
    me = bpy.data.meshes.new(name)
    bm.to_mesh(me)
    bm.free()
    me.shade_smooth() if smooth else me.shade_flat()
    ob = bpy.data.objects.new(name, me)
    bpy.context.scene.collection.objects.link(ob)
    ob.location, ob.rotation_euler, ob.scale = loc, rot, scale
    if parent is not None:
        ob.parent = parent
    return ob


def make_mat(name, color):
    m = bpy.data.materials.new(name)
    m.diffuse_color = color
    return m


def add_uv(me, name, fn):
    uv = me.uv_layers.new(name=name)
    arr = np.zeros(len(me.loops) * 2, dtype=np.float32)
    for i, l in enumerate(me.loops):
        arr[2 * i : 2 * i + 2] = fn(me.vertices[l.vertex_index].co, i)
    uv.data.foreach_set("uv", arr)


def add_color(me, rnd):
    ca = me.color_attributes.new("Col", "BYTE_COLOR", "CORNER")
    vals = [rnd.randint(0, 255) / 255.0 for _ in range(len(me.loops) * 4)]
    for i in range(len(me.loops)):
        vals[4 * i + 3] = 1.0
    ca.data.foreach_set("color_srgb", vals)


def build_case(case):
    rnd = random.Random(42)
    if case == "cube_flat":
        bm = bmesh.new()
        bmesh.ops.create_cube(bm, size=2.0)
        new_obj("cube_flat", bm, (1, 2, 3), (0.3, 0.2, 0.1), (1, 2, 0.5), smooth=False)
    elif case == "sphere_smooth":
        bm = bmesh.new()
        bm.loops.layers.uv.new("UVMap")
        bmesh.ops.create_uvsphere(bm, u_segments=32, v_segments=16, radius=1.0, calc_uvs=True)
        new_obj("sphere_smooth", bm, (0, 0, 1))
    elif case == "monkey_sharp_edges":
        bm = bmesh.new()
        bm.loops.layers.uv.new("UVMap")
        bmesh.ops.create_monkey(bm, calc_uvs=True)
        bmesh.ops.subdivide_edges(bm, edges=bm.edges[:], cuts=1, use_grid_fill=True)
        ob = new_obj("monkey_sharp_edges", bm, (0, 0, 0), (0, 0, 0.5))
        ob.data.set_sharp_from_angle(angle=math.radians(30))
    elif case == "custom_normals":
        bm = bmesh.new()
        bmesh.ops.create_grid(bm, x_segments=12, y_segments=12, size=2.0)
        for v in bm.verts:
            v.co.z = 0.2 * math.sin(v.co.x * 2) * math.cos(v.co.y * 2)
        ob = new_obj("custom_normals", bm, (0, 0, 0))
        me = ob.data
        nrm = [
            Vector((0.4 * math.sin(v.co.x * 3), 0.4 * math.cos(v.co.y * 3), 1.0)).normalized()
            for v in me.vertices
        ]
        me.normals_split_custom_set_from_vertices(nrm)
    elif case == "multimat_uv2_color":
        bm = bmesh.new()
        bm.loops.layers.uv.new("UVMap")
        bmesh.ops.create_cube(bm, size=2.0, calc_uvs=True)
        bmesh.ops.subdivide_edges(bm, edges=bm.edges[:], cuts=2, use_grid_fill=True)
        ob = new_obj("multimat_uv2_color", bm, (3, 0, 0), smooth=False)
        me = ob.data
        for n, c in (("mat_red", (1, 0, 0, 1)), ("mat_green", (0, 1, 0, 1)), ("mat_blue", (0, 0, 1, 1))):
            me.materials.append(make_mat(n, c))
        for p in me.polygons:
            p.material_index = p.index % 3
        add_uv(me, "UVMap2", lambda co, i: (rnd.random(), rnd.random()))
        add_color(me, rnd)
    elif case == "concave_ngon":
        bm = bmesh.new()
        star = []
        for i in range(10):
            a = 2 * math.pi * i / 10
            r = 1.0 if i % 2 == 0 else 0.35
            star.append(bm.verts.new((r * math.cos(a), r * math.sin(a), 0)))
        bm.faces.new(star)
        l_shape = [
            bm.verts.new(p)
            for p in ((2, 0, 0), (4, 0, 0), (4, 0.5, 0), (2.5, 0.5, 0), (2.5, 2, 0), (2, 2, 0))
        ]
        bm.faces.new(l_shape)
        new_obj("concave_ngon", bm, (0, 0, 0), smooth=False)
    elif case == "nonplanar_quads":
        bm = bmesh.new()
        bmesh.ops.create_grid(bm, x_segments=8, y_segments=8, size=2.0)
        for v in bm.verts:
            v.co.z = rnd.uniform(-0.3, 0.3)
        new_obj("nonplanar_quads", bm, (0, 0, 0), smooth=False)
    elif case == "hierarchy":
        root = bpy.data.objects.new("root_empty", None)
        bpy.context.scene.collection.objects.link(root)
        root.location, root.rotation_euler, root.scale = (5, -2, 1), (0.4, 0, 1.0), (2, 2, 2)
        bm = bmesh.new()
        bmesh.ops.create_cube(bm, size=1.0)
        a = new_obj("child_a", bm, (1, 0, 0), (0, 0.5, 0), (1, 1, 1), parent=root, smooth=False)
        bm = bmesh.new()
        bmesh.ops.create_cone(bm, cap_ends=True, segments=12, radius1=0.5, radius2=0.0, depth=1.0)
        new_obj("grandchild_b", bm, (0, 0, 2), (0.3, 0.3, 0), (0.5, 1, 1.5), parent=a, smooth=False)
    elif case == "mirrored":
        bm = bmesh.new()
        bmesh.ops.create_cone(bm, cap_ends=True, segments=16, radius1=1, radius2=0.3, depth=2)
        new_obj("mirrored", bm, (0, 3, 0), (0, 0, 0.3), (-1, 1, 1), smooth=False)
    elif case == "far_precision":
        bm = bmesh.new()
        bm.loops.layers.uv.new("UVMap")
        bmesh.ops.create_uvsphere(bm, u_segments=24, v_segments=12, radius=0.01, calc_uvs=True)
        new_obj("far_precision", bm, (5000.0, -3000.0, 200.0))
    elif case == "big_mesh":
        bm = bmesh.new()
        bm.loops.layers.uv.new("UVMap")
        bmesh.ops.create_monkey(bm, calc_uvs=True)
        for _ in range(3):
            bmesh.ops.subdivide_edges(bm, edges=bm.edges[:], cuts=1, use_grid_fill=True)
        ob = new_obj("big_mesh", bm, (0, 0, 0))
        ob.data.set_sharp_from_angle(angle=math.radians(40))
    else:
        raise ValueError(case)
    bpy.context.view_layer.update()


def extract():
    dg = bpy.context.evaluated_depsgraph_get()
    obs = [o for o in bpy.context.scene.objects if o.type == "MESH"]
    P, N, U0, U1, C, M, POLY = [], [], [], [], [], [], []
    poly_off = 0
    nverts = 0
    for ob in obs:
        oe = ob.evaluated_get(dg)
        me = oe.to_mesh()
        mw = oe.matrix_world.copy()
        nm = mw.to_3x3().inverted_safe().transposed()
        me.calc_loop_triangles()
        nt = len(me.loop_triangles)
        nverts += len(me.vertices)
        if nt == 0:
            oe.to_mesh_clear()
            continue
        tl = np.empty(nt * 3, np.int32)
        me.loop_triangles.foreach_get("loops", tl)
        if np.linalg.det(np.array(mw)[:3, :3]) < 0:
            tl = tl.reshape(-1, 3)[:, [0, 2, 1]].reshape(-1)
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
        uvl = list(me.uv_layers)

        def uvarr(layer):
            a = np.empty(len(me.loops) * 2, np.float32)
            layer.data.foreach_get("uv", a)
            return a.reshape(-1, 2)[tl].reshape(nt, 3, 2)

        U0.append(uvarr(uvl[0]) if len(uvl) > 0 else np.full((nt, 3, 2), np.nan))
        U1.append(uvarr(uvl[1]) if len(uvl) > 1 else np.full((nt, 3, 2), np.nan))
        ca = next(iter(me.color_attributes), None)
        if ca is not None:
            a = np.empty(len(ca.data) * 4, np.float32)
            ca.data.foreach_get("color_srgb", a)
            a = a.reshape(-1, 4)
            C.append(a[tl].reshape(nt, 3, 4) if ca.domain == "CORNER" else a[lv[tl]].reshape(nt, 3, 4))
        else:
            C.append(np.full((nt, 3, 4), np.nan))
        mats = [(s.material.name.split(".")[0] if s.material else "") for s in ob.material_slots] or [""]
        mi = np.empty(len(me.polygons), np.int32)
        me.polygons.foreach_get("material_index", mi)
        M.extend(mats[min(i, len(mats) - 1)] for i in mi[tp])
        POLY.append(tp + poly_off)
        poly_off += len(me.polygons)
        oe.to_mesh_clear()
    if not P:
        return None
    return dict(
        P=np.concatenate(P),
        N=np.concatenate(N),
        U0=np.concatenate(U0),
        U1=np.concatenate(U1),
        C=np.concatenate(C),
        M=np.array(M),
        POLY=np.concatenate(POLY),
        nverts=nverts,
        nobj=len(obs),
    )


def tri_area_normal(P):
    cr = np.cross(P[:, 1] - P[:, 0], P[:, 2] - P[:, 0])
    a = np.linalg.norm(cr, axis=1)
    return 0.5 * a, cr / np.maximum(a, 1e-30)[:, None]


def compare(gt, im):
    if im is None:
        return {"error": "nothing imported"}
    origin = gt["P"].reshape(-1, 3).mean(axis=0)
    gt = dict(gt)
    im = dict(im)
    gt["P"] = gt["P"] - origin
    im["P"] = im["P"] - origin
    res = {
        "tris_gt": int(len(gt["P"])),
        "tris_im": int(len(im["P"])),
        "verts_gt": gt["nverts"],
        "verts_im": im["nverts"],
    }
    ga, _gn = tri_area_normal(gt["P"])
    ia, inn = tri_area_normal(im["P"])
    res["area_err_pct"] = float(100 * (ia.sum() - ga.sum()) / ga.sum())

    gv = gt["P"].reshape(-1, 3)
    gtri = np.arange(len(gv)).reshape(-1, 3)
    bvh_g = BVHTree.FromPolygons([tuple(v) for v in gv], [tuple(t) for t in gtri], epsilon=0.0)
    iv = im["P"].reshape(-1, 3)
    itri = np.arange(len(iv)).reshape(-1, 3)
    bvh_i = BVHTree.FromPolygons([tuple(v) for v in iv], [tuple(t) for t in itri], epsilon=0.0)
    d1 = np.array([bvh_g.find_nearest(Vector(v))[3] for v in iv[:: max(1, len(iv) // 60000)]])
    d2 = np.array([bvh_i.find_nearest(Vector(v))[3] for v in gv[:: max(1, len(gv) // 60000)]])
    res["pos_max_err"] = float(max(d1.max(), d2.max()))
    res["pos_mean_err"] = float(np.concatenate([d1, d2]).mean())

    kd = KDTree(len(gv))
    for i, v in enumerate(gv):
        kd.insert(Vector(v), i)
    kd.balance()
    tol = max(1e-4, 1e-6 * float(np.abs(gv).max()))

    def polys_at(v):
        return {int(gt["POLY"][i // 3]) for (_, i, _) in kd.find_range(Vector(v), tol)}

    cent = im["P"].mean(axis=1)
    pools = {}
    for t, p in enumerate(gt["POLY"]):
        pools.setdefault(int(p), []).append(t)
    poly_n = {}
    for p, ts in pools.items():
        a, n = tri_area_normal(gt["P"][ts])
        v = (n * a[:, None]).sum(axis=0)
        poly_n[p] = v / max(np.linalg.norm(v), 1e-30)

    src_poly = np.empty(len(im["P"]), np.int64)
    unmatched = 0
    for t in range(len(im["P"])):
        s0 = polys_at(im["P"][t, 0]) & polys_at(im["P"][t, 1]) & polys_at(im["P"][t, 2])
        if len(s0) >= 1:
            if len(s0) == 1:
                src_poly[t] = next(iter(s0))
            else:
                best = None
                for p in s0:
                    d = np.linalg.norm(gt["P"][pools[p]].reshape(-1, 3).mean(axis=0) - cent[t])
                    if best is None or d < best[0]:
                        best = (d, p)
                src_poly[t] = best[1]
        else:
            unmatched += 1
            src_poly[t] = int(gt["POLY"][bvh_g.find_nearest(Vector(cent[t]))[2]])
    res["tris_not_on_source_poly"] = unmatched
    dots = np.array([np.dot(inn[t], poly_n[int(src_poly[t])]) for t in range(len(inn))])
    valid = ia > 1e-12
    res["flipped_tris"] = int(((dots < -0.5) & valid).sum())

    nerr, uv0e, uv1e, cole, materr = [], [], [], [], 0
    for t in range(len(im["P"])):
        cand = pools[int(src_poly[t])]
        cp = gt["P"][cand].reshape(-1, 3)
        for k in range(3):
            j = int(np.argmin(np.linalg.norm(cp - im["P"][t, k], axis=1)))
            st, sk = cand[j // 3], j % 3
            c = np.clip(np.dot(gt["N"][st, sk], im["N"][t, k]), -1, 1)
            nerr.append(math.degrees(math.acos(c)))
            if not np.isnan(gt["U0"][st, sk, 0]):
                uv0e.append(np.abs(gt["U0"][st, sk] - im["U0"][t, k]).max())
            if not np.isnan(gt["U1"][st, sk, 0]):
                uv1e.append(np.abs(gt["U1"][st, sk] - im["U1"][t, k]).max())
            if not np.isnan(gt["C"][st, sk, 0]):
                cole.append(np.abs(gt["C"][st, sk] - im["C"][t, k]).max())
        if gt["M"][cand[0]] != im["M"][t]:
            materr += 1

    nerr = np.array(nerr)
    res["normal_mean_deg"] = float(nerr.mean())
    res["normal_p99_deg"] = float(np.percentile(nerr, 99))
    res["normal_max_deg"] = float(nerr.max())

    def stat(a):
        arr = np.array(a, dtype=np.float64)
        return None if len(arr) == 0 else (None if np.isnan(arr).all() else float(np.nanmax(arr)))

    res["uv0_max_err"] = stat(uv0e)
    res["uv1_max_err"] = stat(uv1e)
    res["color_max_err"] = stat(cole)
    if uv1e and np.isnan(np.array(uv1e)).all():
        res["uv1_max_err"] = "MISSING"
    if cole and np.isnan(np.array(cole)).all():
        res["color_max_err"] = "MISSING"
    res["material_mismatch_tris"] = materr if any(gt["M"]) else None
    res["objects_im"] = im["nobj"]
    return res


results = {"selfcheck": {}, "dae": {}, "cdae": {}}

for case in CASES:
    clear_scene()
    build_case(case)
    gt = extract()

    # Selfcheck: rebuild and compare to gt (must be exact zeros)
    clear_scene()
    build_case(case)
    results["selfcheck"][case] = compare(gt, extract())

    for fmt in ("dae", "cdae"):
        clear_scene()
        build_case(case)
        out_path = os.path.join(workdir, f"{case}.{fmt}")
        t0 = time.perf_counter()
        if fmt == "dae":
            ex_res = bpy.ops.wm.collada_export(filepath=out_path)
        else:
            ex_res = bpy.ops.export_scene.beamng_cdae(filepath=out_path)
        exp_sec = time.perf_counter() - t0
        if "FINISHED" not in ex_res or not os.path.exists(out_path):
            results[fmt][case] = {"error": f"export failed: {ex_res}"}
            continue
        fbytes = os.path.getsize(out_path)

        clear_scene()
        t0 = time.perf_counter()
        if fmt == "dae":
            im_res = bpy.ops.wm.collada_import(filepath=out_path)
        else:
            im_res = bpy.ops.import_scene.beamng_cdae(filepath=out_path)
        imp_sec = time.perf_counter() - t0
        if "FINISHED" not in im_res:
            results[fmt][case] = {"error": f"import failed: {im_res}"}
            continue
        bpy.context.view_layer.update()
        metrics = compare(gt, extract())
        metrics["bytes"] = fbytes
        metrics["exp_sec"] = round(exp_sec, 4)
        metrics["imp_sec"] = round(imp_sec, 4)
        results[fmt][case] = metrics

with open(out_json, "w", encoding="utf-8") as fh:
    json.dump(results, fh, indent=2)
'''


def _check_selfcheck(case: str, r: dict) -> list[str]:
    errs: list[str] = []
    if "error" in r:
        return [f"error={r['error']}"]
    if r["tris_im"] != r["tris_gt"]:
        errs.append(f"tris {r['tris_im']} != {r['tris_gt']}")
    if r["pos_max_err"] != 0.0:
        errs.append(f"posMax={r['pos_max_err']}")
    if r["flipped_tris"] != 0:
        errs.append(f"flip={r['flipped_tris']}")
    if r["normal_max_deg"] > 1e-5:
        errs.append(f"nMax={r['normal_max_deg']}")
    for k in ("uv0_max_err", "uv1_max_err", "color_max_err"):
        if r.get(k) not in (None, 0.0):
            errs.append(f"{k}={r[k]}")
    if r.get("material_mismatch_tris") not in (None, 0):
        errs.append(f"matX={r['material_mismatch_tris']}")
    return errs


def _check_roundtrip(r: dict) -> list[str]:
    errs: list[str] = []
    if "error" in r:
        return [f"error={r['error']}"]
    if r["tris_im"] != r["tris_gt"]:
        errs.append(f"tris {r['tris_im']} != {r['tris_gt']}")
    if r["pos_max_err"] >= MAX_POS_ERR_M:
        errs.append(f"posMax={r['pos_max_err']:.6g}m >= {MAX_POS_ERR_M}m")
    if r["flipped_tris"] != 0:
        errs.append(f"flip={r['flipped_tris']}")
    if r["normal_max_deg"] >= MAX_NORMAL_ERR_DEG:
        errs.append(f"nMax={r['normal_max_deg']:.4f}deg >= {MAX_NORMAL_ERR_DEG}deg")
    for k, lim in (("uv0_max_err", MAX_UV_ERR), ("uv1_max_err", MAX_UV_ERR), ("color_max_err", MAX_COLOR_ERR)):
        v = r.get(k)
        if v is not None and (not isinstance(v, (int, float)) or v >= lim):
            errs.append(f"{k}={v}")
    if r.get("material_mismatch_tris") not in (None, 0):
        errs.append(f"matX={r['material_mismatch_tris']}")
    return errs


def main() -> int:
    blender = _find_blender()
    print(f"Running #22 accuracy round-trip CI gate using: {blender}")
    workdir = tempfile.mkdtemp(prefix="collada_accuracy_")
    try:
        worker = os.path.join(workdir, "_worker.py")
        out_json = os.path.join(workdir, "results.json")
        with open(worker, "w", encoding="utf-8") as fh:
            fh.write(WORKER_SCRIPT)
        proc = subprocess.run(
            [blender, "-b", "--factory-startup", "--python-exit-code", "1", "--python", worker, "--", REPO_ROOT, workdir, out_json],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if proc.returncode != 0:
            print(proc.stdout[-2000:])
            print(proc.stderr[-2000:])
            return 1

        res = json.load(open(out_json, "r", encoding="utf-8"))
        failures: list[str] = []

        print("\n=== SELFCHECK (must be exact zeros) ===")
        for case, r in res["selfcheck"].items():
            errs = _check_selfcheck(case, r)
            if errs:
                print(f"  [FAIL] selfcheck|{case}: {', '.join(errs)}")
                failures.append(f"selfcheck|{case}: {', '.join(errs)}")
            else:
                print(f"  [PASS] selfcheck|{case} (all zeros)")

        for fmt in ("dae", "cdae"):
            print(f"\n=== ROUND-TRIP ACCURACY ({fmt.upper()}) ===")
            for case, r in res[fmt].items():
                errs = _check_roundtrip(r)
                xfail_issue = XFAIL_CASES.get((fmt, case))
                summary = (
                    f"tris={r.get('tris_im')}/{r.get('tris_gt')} "
                    f"area%={r.get('area_err_pct', 0.0):+.4f}% "
                    f"posMax={r.get('pos_max_err', -1):.2e}m "
                    f"flip={r.get('flipped_tris')} "
                    f"nP99={r.get('normal_p99_deg', -1):.4f}deg "
                    f"nMax={r.get('normal_max_deg', -1):.4f}deg"
                )
                if xfail_issue:
                    if not errs:
                        print(f"  [XPASS {xfail_issue}] {fmt}|{case}: {summary} (remove from XFAIL_CASES!)")
                        failures.append(f"XPASS {fmt}|{case} ({xfail_issue})")
                    else:
                        # Even on xfail cases, position and p99 normals must stay within gate
                        guard_errs = []
                        if r.get("pos_max_err", 1.0) >= MAX_POS_ERR_M:
                            guard_errs.append(f"posMax={r.get('pos_max_err')}")
                        if r.get("normal_p99_deg", 180.0) >= MAX_NORMAL_ERR_DEG:
                            guard_errs.append(f"nP99={r.get('normal_p99_deg')}")
                        if guard_errs:
                            print(f"  [FAIL {xfail_issue}] {fmt}|{case}: {summary} ({', '.join(guard_errs)})")
                            failures.append(f"{fmt}|{case}: {', '.join(guard_errs)}")
                        else:
                            print(f"  [XFAIL {xfail_issue}] {fmt}|{case}: {summary} ({'; '.join(errs)})")
                elif errs:
                    print(f"  [FAIL] {fmt}|{case}: {summary} ({'; '.join(errs)})")
                    failures.append(f"{fmt}|{case}: {'; '.join(errs)}")
                else:
                    print(f"  [PASS] {fmt}|{case}: {summary}")

        if failures:
            print(f"\nAccuracy gate FAILED ({len(failures)} failure(s)):")
            for f in failures:
                print(f"  - {f}")
            return 1

        print("\nAll #22 accuracy round-trip CI gate checks passed!")
        return 0
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
