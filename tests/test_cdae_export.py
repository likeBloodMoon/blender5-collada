"""Round-trip tests for experimental BeamNG .cdae v31 exporter (Phase 2.4).

Builds Blender test scenes (multi-material mesh with sharp edges, UV0+UV1, non-white
sRGB vertex colors, rotated parent-child hierarchy, modifiers, selection-only, and
compressed vs uncompressed), exports to .cdae via export_scene.beamng_cdae, re-imports
via import_scene.beamng_cdae, and verifies exact geometry/attribute/transform parity.

Usage:
    python tests/test_cdae_export.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _find_runner() -> list[str]:
    blender = os.environ.get("BLENDER_5X") or os.environ.get("BLENDER_EXE")
    if blender and os.path.isfile(blender):
        return [blender, "-b", "--factory-startup", "--python"]
    local_bl = os.path.join(REPO_ROOT, ".tools", "blender-5.3.0-windows-x64", "blender.exe")
    if os.path.isfile(local_bl):
        return [local_bl, "-b", "--factory-startup", "--python"]
    return [sys.executable]


WORKER_SCRIPT = r'''
import json
import math
import os
import sys

argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else sys.argv[1:]
repo_root, workdir = argv[0], argv[1]
sys.path.insert(0, repo_root)

import bpy
import bmesh
import numpy as np
from mathutils import Euler, Quaternion, Vector
from mathutils.bvhtree import BVHTree

import io_scene_collada
from io_scene_collada.cdae.exporter import zstd_available
from io_scene_collada.cdae.reader import read_cdae_file

io_scene_collada.register()


def build_test_scene():
    bpy.ops.wm.read_factory_settings(use_empty=True)

    # Parent Empty with non-identity transform
    parent = bpy.data.objects.new("ParentRig", None)
    bpy.context.scene.collection.objects.link(parent)
    parent.location = (1.5, -2.0, 0.5)
    parent.rotation_euler = Euler((math.radians(30.0), math.radians(-15.0), math.radians(45.0)), "XYZ")

    # Child Cube with 2 materials, UV0, UV1, non-white sRGB vertex colors, sharp edges
    bpy.ops.mesh.primitive_cube_add(size=2.0)
    cube = bpy.context.active_object
    cube.name = "HeroPart"
    cube.parent = parent
    cube.location = (0.0, 0.5, 1.0)
    cube.rotation_euler = Euler((math.radians(20.0), 0.0, math.radians(-60.0)), "XYZ")

    me = cube.data
    m1 = bpy.data.materials.new("car_paint")
    m2 = bpy.data.materials.new("chrome_trim")
    me.materials.append(m1)
    me.materials.append(m2)
    for i, p in enumerate(me.polygons):
        p.material_index = 0 if i < 3 else 1
        p.use_smooth = False

    # Add UV1
    uv1 = me.uv_layers.new(name="UVMap.001")
    for li, d in enumerate(uv1.data):
        u0, v0 = me.uv_layers[0].data[li].uv
        d.uv = (u0 * 0.5 + 0.25, v0 * 0.5 + 0.1)

    # Add non-white sRGB BYTE_COLOR
    ca = me.color_attributes.new(name="Color", type="BYTE_COLOR", domain="CORNER")
    for li, d in enumerate(ca.data):
        d.color_srgb = (0.2, 0.5, 0.8, 1.0)

    # Second object (outside selection for use_selection test) with a Bevel modifier
    bpy.ops.mesh.primitive_cylinder_add(vertices=16, radius=0.5, depth=1.0, location=(-3.0, 0.0, 0.0))
    cyl = bpy.context.active_object
    cyl.name = "ExtraWheel"
    bev = cyl.modifiers.new("Bevel", "BEVEL")
    bev.width = 0.05
    bev.segments = 1

    bpy.context.view_layer.update()
    return parent, cube, cyl


def snapshot_obj(ob):
    me = ob.data
    me.calc_loop_triangles()
    mw = ob.matrix_world.copy()
    loc, rot, _ = mw.decompose()
    n_v = len(me.vertices)
    co = np.empty(n_v * 3, dtype=np.float32)
    me.vertices.foreach_get("co", co)
    co = co.reshape(n_v, 3)
    uv0 = np.empty(len(me.loops) * 2, dtype=np.float32)
    me.uv_layers[0].data.foreach_get("uv", uv0)
    uv0 = uv0.reshape(-1, 2)
    uv1 = np.empty(len(me.loops) * 2, dtype=np.float32)
    me.uv_layers[1].data.foreach_get("uv", uv1)
    uv1 = uv1.reshape(-1, 2)
    col = np.empty(len(me.loops) * 4, dtype=np.float32)
    me.color_attributes[0].data.foreach_get("color_srgb", col)
    col = col.reshape(-1, 4)
    return {
        "loc": list(loc),
        "rot": [rot.w, rot.x, rot.y, rot.z],
        "tris": len(me.loop_triangles),
        "bb_min": co.min(0).tolist(),
        "bb_max": co.max(0).tolist(),
        "uv0_min": uv0.min(0).tolist(),
        "uv0_max": uv0.max(0).tolist(),
        "uv1_min": uv1.min(0).tolist(),
        "uv1_max": uv1.max(0).tolist(),
        "col_mean": col.mean(0).tolist(),
        "mats": [m.name for m in me.materials if m],
    }


def quat_deg(q1, q2):
    d = abs(Quaternion(q1).dot(Quaternion(q2)))
    return math.degrees(2.0 * math.acos(min(1.0, max(-1.0, d))))


# Test 1: Full scene round-trip (uncompressed) + modifiers applied
_, cube, cyl = build_test_scene()
ref_cube = snapshot_obj(cube)
deps = bpy.context.evaluated_depsgraph_get()
ev_cyl = cyl.evaluated_get(deps).to_mesh()
ev_cyl.calc_loop_triangles()
expected_cyl_tris = len(ev_cyl.loop_triangles)
cyl.evaluated_get(deps).to_mesh_clear()

p_raw = os.path.join(workdir, "full_uncompressed.cdae")
res = bpy.ops.export_scene.beamng_cdae(filepath=p_raw, use_selection=False, apply_modifiers=True, compress=False)
assert res == {"FINISHED"}

shape = read_cdae_file(p_raw)
assert shape.shape_version == 31 and shape.header_info["compression"] is False
assert len(shape.meshes) == 2 and all(len(m.tangents) == len(m.verts) for m in shape.meshes)

bpy.ops.wm.read_factory_settings(use_empty=True)
res = bpy.ops.import_scene.beamng_cdae(filepath=p_raw, normals_mode="EXACT")
assert res == {"FINISHED"}
assert "HeroPart" in bpy.data.objects and "ExtraWheel" in bpy.data.objects and "ParentRig" in bpy.data.objects
# Verify EXACT mode stores BOTH exact custom_normal attribute AND sharp_edge markers
assert bpy.data.objects["HeroPart"].data.attributes.get("custom_normal") is not None

got_cube = snapshot_obj(bpy.data.objects["HeroPart"])
assert got_cube["tris"] == ref_cube["tris"] == 12
assert np.allclose(got_cube["loc"], ref_cube["loc"], atol=1e-4), (got_cube["loc"], ref_cube["loc"])
assert quat_deg(got_cube["rot"], ref_cube["rot"]) < 0.05
assert np.allclose(got_cube["bb_min"], ref_cube["bb_min"], atol=1e-5)
assert np.allclose(got_cube["bb_max"], ref_cube["bb_max"], atol=1e-5)
assert np.allclose(got_cube["uv0_min"], ref_cube["uv0_min"], atol=1e-5)
assert np.allclose(got_cube["uv0_max"], ref_cube["uv0_max"], atol=1e-5)
assert np.allclose(got_cube["uv1_min"], ref_cube["uv1_min"], atol=1e-5)
assert np.allclose(got_cube["uv1_max"], ref_cube["uv1_max"], atol=1e-5)
assert np.allclose(got_cube["col_mean"], ref_cube["col_mean"], atol=1.0 / 255.0 + 1e-4)
assert got_cube["mats"] == ref_cube["mats"] == ["car_paint", "chrome_trim"]
assert len(bpy.data.objects["ExtraWheel"].data.polygons) == expected_cyl_tris

# Verify all imported meshes pass bmesh + validate cleanly
for ob in bpy.data.objects:
    if ob.type == "MESH":
        bm = bmesh.new(); bm.from_mesh(ob.data); bm.free()
        assert not ob.data.validate(verbose=False, clean_customdata=False)

# Sharp edges recovered on merged HeroPart cube (all 12 cube edges sharp),
# and clearing custom_normal preserves those 12 sharp edges cleanly.
hero_me = bpy.data.objects["HeroPart"].data
sa = hero_me.attributes.get("sharp_edge")
assert sa is not None and sum(1 for d in sa.data if d.value) == 12
hero_me.attributes.remove(hero_me.attributes["custom_normal"])
hero_me.update()
sa_after = hero_me.attributes.get("sharp_edge")
assert sa_after is not None and sum(1 for d in sa_after.data if d.value) == 12
print("  PASS roundtrip_full_uncompressed_hierarchy_uv_color_sharp_modifiers")

# Test 2: Selection-only export (must exclude ExtraWheel)
build_test_scene()
for o in bpy.data.objects:
    o.select_set(o.name == "HeroPart")
p_sel = os.path.join(workdir, "selection_only.cdae")
res = bpy.ops.export_scene.beamng_cdae(filepath=p_sel, use_selection=True, apply_modifiers=True, compress=False)
assert res == {"FINISHED"}
s_sel = read_cdae_file(p_sel)
assert s_sel.header_info["objectNames"] == ["HeroPart"], s_sel.header_info
print("  PASS export_selection_only")

# Test 3: Compressed export if zstandard is available
if zstd_available():
    build_test_scene()
    p_zstd = os.path.join(workdir, "compressed.cdae")
    res = bpy.ops.export_scene.beamng_cdae(filepath=p_zstd, compress=True)
    assert res == {"FINISHED"}
    s_z = read_cdae_file(p_zstd)
    assert s_z.header_info["compression"] is True
    assert os.path.getsize(p_zstd) < os.path.getsize(p_raw)
    print("  PASS export_compressed_zstd")
else:
    print("  SKIP export_compressed_zstd (zstandard not installed)")

# Test 4: Tangent calculation failure records a warning (not silently swallowed)
from io_scene_collada.cdae.exporter import _build_gpu_mesh_data
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.mesh.primitive_cube_add(size=1.0)
me_orig = bpy.context.active_object.data

class _FailingTangentMeshProxy:
    def __init__(self, inner):
        self._inner = inner
    def __getattr__(self, item):
        return getattr(self._inner, item)
    def calc_tangents(self, uvmap=""):
        raise RuntimeError("mock degenerate UV tangent failure")

tan_warnings = []
_build_gpu_mesh_data(_FailingTangentMeshProxy(me_orig), None, {}, [], warnings=tan_warnings, mesh_name="BrokenTanMesh")
assert len(tan_warnings) == 1 and "tangent calculation failed" in tan_warnings[0] and "BrokenTanMesh" in tan_warnings[0], tan_warnings
print("  PASS tangent_failure_warning_reported")


# Test 5: #11 Scaled-parent hierarchies (uniform, non-uniform + shear, negative/mirrored)
def _extract_world_tris(obs):
    """Return world-space (tris_P [N,3,3], tris_N [N,3,3], geom_N [N,3]) with outward orientation."""
    P_list, N_list, G_list = [], [], []
    for ob in obs:
        if ob.type != "MESH":
            continue
        me = ob.data
        me.calc_loop_triangles()
        nt = len(me.loop_triangles)
        if nt == 0:
            continue
        mw = ob.matrix_world.copy()
        det = mw.to_3x3().determinant()
        nm = mw.to_3x3().inverted_safe().transposed()
        tl = np.empty(nt * 3, dtype=np.int32)
        me.loop_triangles.foreach_get("loops", tl)
        lv = np.empty(len(me.loops), dtype=np.int32)
        me.loops.foreach_get("vertex_index", lv)
        co = np.empty(len(me.vertices) * 3, dtype=np.float64)
        me.vertices.foreach_get("co", co)
        M4 = np.array(mw, dtype=np.float64)
        co_w = co.reshape(-1, 3) @ M4[:3, :3].T + M4[:3, 3]
        tri_p = co_w[lv[tl]].reshape(nt, 3, 3)
        cn = np.empty(len(me.loops) * 3, dtype=np.float32)
        me.corner_normals.foreach_get("vector", cn)
        cn_w = cn.reshape(-1, 3).astype(np.float64) @ np.array(nm, dtype=np.float64).T
        cn_w /= np.maximum(np.linalg.norm(cn_w, axis=1, keepdims=True), 1e-12)
        tri_n = cn_w[tl].reshape(nt, 3, 3)
        # Compute outward geometric normal (account for negative-det world matrix)
        cr = np.cross(tri_p[:, 1] - tri_p[:, 0], tri_p[:, 2] - tri_p[:, 0])
        if det < 0.0:
            cr = -cr
        alen = np.linalg.norm(cr, axis=1, keepdims=True)
        gn = cr / np.maximum(alen, 1e-30)
        P_list.append(tri_p)
        N_list.append(tri_n)
        G_list.append(gn)
    return np.concatenate(P_list), np.concatenate(N_list), np.concatenate(G_list)


def _check_scaled_hierarchy(label, parent_scale):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    root = bpy.data.objects.new("root_empty", None)
    bpy.context.scene.collection.objects.link(root)
    root.location = (5.0, -2.0, 1.0)
    root.rotation_euler = (0.4, 0.0, 1.0)
    root.scale = parent_scale

    bm = bmesh.new()
    bmesh.ops.create_cube(bm, size=1.0)
    me_a = bpy.data.meshes.new("child_a")
    bm.to_mesh(me_a)
    bm.free()
    me_a.shade_flat()
    child_a = bpy.data.objects.new("child_a", me_a)
    bpy.context.scene.collection.objects.link(child_a)
    child_a.parent = root
    child_a.location = (1.0, 0.0, 0.0)
    child_a.rotation_euler = (0.0, 0.5, 0.0)
    child_a.scale = (1.0, 1.0, 1.0)

    bm = bmesh.new()
    bmesh.ops.create_cone(bm, cap_ends=True, segments=12, radius1=0.5, radius2=0.0, depth=1.0)
    me_b = bpy.data.meshes.new("grandchild_b")
    bm.to_mesh(me_b)
    bm.free()
    me_b.shade_flat()
    grand_b = bpy.data.objects.new("grandchild_b", me_b)
    bpy.context.scene.collection.objects.link(grand_b)
    grand_b.parent = child_a
    grand_b.location = (0.0, 0.0, 2.0)
    grand_b.rotation_euler = (0.3, 0.3, 0.0)
    grand_b.scale = (0.5, 1.0, 1.5)

    bpy.context.view_layer.update()
    gt_P, gt_N, gt_G = _extract_world_tris([child_a, grand_b])

    out_p = os.path.join(workdir, f"hier_{label}.cdae")
    res = bpy.ops.export_scene.beamng_cdae(filepath=out_p, use_selection=False, apply_modifiers=True)
    assert res == {"FINISHED"}

    bpy.ops.wm.read_factory_settings(use_empty=True)
    res = bpy.ops.import_scene.beamng_cdae(filepath=out_p, normals_mode="EXACT")
    assert res == {"FINISHED"}
    bpy.context.view_layer.update()

    im_obs = [bpy.data.objects["child_a"], bpy.data.objects["grandchild_b"]]
    # Imported objects must have positive unit scale (all scale/reflection baked into verts)
    for o in im_obs:
        assert o.matrix_world.to_3x3().determinant() > 0.0, f"{label}: {o.name} has negative det!"

    im_P, im_N, im_G = _extract_world_tris(im_obs)
    assert len(im_P) == len(gt_P), f"{label}: tri count {len(im_P)} != {len(gt_P)}"

    # Bidirectional surface distance via BVHTree (robust to quad diagonal choice)
    gv = gt_P.reshape(-1, 3)
    gtri = np.arange(len(gv)).reshape(-1, 3)
    bvh_g = BVHTree.FromPolygons([tuple(v) for v in gv], [tuple(t) for t in gtri], epsilon=0.0)
    iv = im_P.reshape(-1, 3)
    itri = np.arange(len(iv)).reshape(-1, 3)
    bvh_i = BVHTree.FromPolygons([tuple(v) for v in iv], [tuple(t) for t in itri], epsilon=0.0)
    d1 = max(bvh_g.find_nearest(Vector(v))[3] for v in iv)
    d2 = max(bvh_i.find_nearest(Vector(v))[3] for v in gv)
    max_pos_err = float(max(d1, d2))

    # Check triangle orientation and corner normals against nearest source surface triangle
    max_norm_err = 0.0
    flipped = 0
    for ti in range(len(im_P)):
        cent = im_P[ti].mean(axis=0)
        gi = bvh_g.find_nearest(Vector(cent))[2]
        if float(np.dot(im_G[ti], gt_G[gi])) < 0.5:
            flipped += 1
        # Verify imported geometric normal agrees with imported outward corner normal (not inside-out)
        if float(np.dot(im_G[ti], im_N[ti].mean(axis=0))) < 0.5:
            flipped += 1
        for k in range(3):
            hit_gi = bvh_g.find_nearest(Vector(im_P[ti, k]))[2]
            # Compare against nearest corner normal on the matched source face
            c_best = max(float(np.clip(np.dot(gt_N[gi, sk], im_N[ti, k]), -1.0, 1.0)) for sk in range(3))
            max_norm_err = max(max_norm_err, math.degrees(math.acos(c_best)))

    assert max_pos_err < 1e-4, f"{label}: max_pos_err={max_pos_err:.6f} m >= 0.1 mm"
    assert flipped == 0, f"{label}: flipped={flipped} != 0"
    assert max_norm_err < 0.1, f"{label}: max_norm_err={max_norm_err:.4f} deg >= 0.1 deg"
    print(f"  PASS scaled_parent_{label} (pos_max={max_pos_err*1000:.4f}mm, flips={flipped}, norm_max={max_norm_err:.4f}deg)")


_check_scaled_hierarchy("uniform_2x", (2.0, 2.0, 2.0))
_check_scaled_hierarchy("nonuniform_shear", (1.0, 2.0, 0.5))
_check_scaled_hierarchy("negative_mirror", (-1.0, 1.0, 1.0))
_check_scaled_hierarchy("negative_nonuniform", (-2.0, 1.0, 0.5))
'''


def main() -> int:
    runner = _find_runner()
    print(f"Running BeamNG .cdae v31 export round-trip tests using: {' '.join(runner)}")
    workdir = tempfile.mkdtemp(prefix="collada_cdae_exp_")
    try:
        worker_path = os.path.join(workdir, "_exp_worker.py")
        with open(worker_path, "w", encoding="utf-8") as f:
            f.write(WORKER_SCRIPT)
        cmd = (
            [runner[0], worker_path, REPO_ROOT, workdir]
            if runner[0] == sys.executable
            else [*runner, worker_path, "--", REPO_ROOT, workdir]
        )
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        print(proc.stdout.strip())
        if proc.returncode != 0 or "Traceback (most recent call last)" in proc.stderr:
            print("STDERR:\n" + proc.stderr)
            return 1
        print("\nAll .cdae export round-trip tests passed!")
        return 0
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
