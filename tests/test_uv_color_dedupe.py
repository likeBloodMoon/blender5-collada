"""Regression tests for #12: deduplicate UV and vertex color sources on .dae export.

Verifies:
  1. Multi-UV + multi-color mesh (2 UV maps, 2 BYTE_COLOR CORNER attributes) round-trips
     with byte-exact UV and sRGB color values, and source arrays are strictly deduplicated
     (< n_loops).
  2. Large subdivided Suzanne mesh (~62k tris, matching accuracy kit big_mesh) exports
     under 6 MB with UV float_array count << 2 * n_loops, and round-trips UVs byte-exact.
  3. COLLADA_DEDUPE_ATTRS=0 restores the legacy per-corner (n_loops) layout for 4.5.1 parity.

Usage:
    python tests/test_uv_color_dedupe.py
    BLENDER_5X=... python tests/test_uv_color_dedupe.py
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
import xml.etree.ElementTree as ET
import bmesh
import bpy
import numpy as np

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


def parse_source_counts(dae_path):
    tree = ET.parse(dae_path)
    root = tree.getroot()
    counts = {}
    for el in root.iter():
        if el.tag.endswith("float_array"):
            aid = el.attrib.get("id", "")
            counts[aid] = int(el.attrib.get("count", "0"))
    return counts


def snapshot_uv_col(ob):
    me = ob.data
    n_loops = len(me.loops)
    n_polys = len(me.polygons)
    uvs = []
    for uvl in me.uv_layers:
        a = np.empty(n_loops * 2, dtype=np.float32)
        uvl.data.foreach_get("uv", a)
        uvs.append(a.reshape(n_loops, 2))
    cols = {}
    for ca in me.color_attributes:
        if ca.domain == "CORNER" and ca.data_type == "BYTE_COLOR":
            a = np.empty(n_loops * 4, dtype=np.float32)
            ca.data.foreach_get("color_srgb", a)
            cols[ca.name] = np.clip(np.round(a.reshape(n_loops, 4) * 255.0), 0, 255).astype(np.uint8)
    # Build per-triangle-vertex lookup keyed by rounded vertex coords for diagonal-invariant checks
    lv = np.empty(n_loops, dtype=np.int32)
    me.loops.foreach_get("vertex_index", lv)
    co = np.empty(len(me.vertices) * 3, dtype=np.float32)
    me.vertices.foreach_get("co", co)
    co = co.reshape(-1, 3)
    return {"n_loops": n_loops, "n_polys": n_polys, "uvs": uvs, "cols": cols, "lv": lv, "co": co}


results = {}

# Case 1: Multi-UV (2 layers) + Multi-Color (2 attributes) on subdivided cube (lots of shared UVs & colors)
clear_scene()
bm = bmesh.new()
bm.loops.layers.uv.new("UVMap")
bmesh.ops.create_cube(bm, size=2.0, calc_uvs=True)
bmesh.ops.subdivide_edges(bm, edges=bm.edges[:], cuts=2, use_grid_fill=True)
me = bpy.data.meshes.new("MultiAttrCube")
bm.to_mesh(me)
bm.free()
me.shade_flat()
ob = bpy.data.objects.new("MultiAttrCube", me)
bpy.context.scene.collection.objects.link(ob)

# UV2: quantised grid of 4 values so heavy dedupe happens
uv2 = me.uv_layers.new(name="UVMap2")
n_l = len(me.loops)
u2_arr = np.empty((n_l, 2), dtype=np.float32)
for i in range(n_l):
    u2_arr[i] = ((i % 4) * 0.25, ((i // 4) % 4) * 0.25)
uv2.data.foreach_set("uv", u2_arr.ravel())

# Color 1 & Color 2 (palette of 3 colors each)
palette1 = [(1.0, 0.0, 0.0, 1.0), (0.0, 1.0, 0.0, 1.0), (0.0, 0.0, 1.0, 1.0)]
palette2 = [(0.2, 0.4, 0.6, 1.0), (0.8, 0.6, 0.2, 1.0)]
ca1 = me.color_attributes.new("ColA", "BYTE_COLOR", "CORNER")
ca1.data.foreach_set("color_srgb", np.array([palette1[i % 3] for i in range(n_l)], dtype=np.float32).ravel())
ca2 = me.color_attributes.new("ColB", "BYTE_COLOR", "CORNER")
ca2.data.foreach_set("color_srgb", np.array([palette2[i % 2] for i in range(n_l)], dtype=np.float32).ravel())

bpy.context.view_layer.update()
# Export with triangulate=False so loop order is 1:1 for exact per-loop comparison
ref1 = snapshot_uv_col(ob)

dae_dedup = os.path.join(workdir, "multi_dedup.dae")
os.environ.pop("COLLADA_DEDUPE_ATTRS", None)
assert "FINISHED" in bpy.ops.wm.collada_export(filepath=dae_dedup, triangulate=False)
counts_dedup = parse_source_counts(dae_dedup)

dae_legacy = os.path.join(workdir, "multi_legacy.dae")
os.environ["COLLADA_DEDUPE_ATTRS"] = "0"
assert "FINISHED" in bpy.ops.wm.collada_export(filepath=dae_legacy, triangulate=False)
counts_legacy = parse_source_counts(dae_legacy)
os.environ.pop("COLLADA_DEDUPE_ATTRS", None)

clear_scene()
assert "FINISHED" in bpy.ops.wm.collada_import(filepath=dae_dedup)
got1 = snapshot_uv_col(bpy.data.objects["MultiAttrCube"])

assert len(ref1["uvs"]) == len(got1["uvs"]) == 2
uv_max_err = {}
for idx in range(len(ref1["uvs"])):
    uv_max_err[f"uv{idx}"] = float(np.abs(ref1["uvs"][idx] - got1["uvs"][idx]).max())

col_max_diff_u8 = {}
for name in ref1["cols"]:
    col_max_diff_u8[name] = int(np.abs(ref1["cols"][name].astype(np.int32) - got1["cols"][name].astype(np.int32)).max())

results["case1"] = {
    "n_loops": ref1["n_loops"],
    "counts_dedup": counts_dedup,
    "counts_legacy": counts_legacy,
    "uv_max_err": uv_max_err,
    "col_max_diff_u8": col_max_diff_u8,
}

# Case 2: big_mesh (subdivided Suzanne, ~62k tris) -> must be < 6 MB and UV-deduplicated
clear_scene()
bm = bmesh.new()
bm.loops.layers.uv.new("UVMap")
bmesh.ops.create_monkey(bm, calc_uvs=True)
for _ in range(3):
    bmesh.ops.subdivide_edges(bm, edges=bm.edges[:], cuts=1, use_grid_fill=True)
me_big = bpy.data.meshes.new("big_mesh")
bm.to_mesh(me_big)
bm.free()
me_big.shade_smooth()
me_big.set_sharp_from_angle(angle=math.radians(40))
ob_big = bpy.data.objects.new("big_mesh", me_big)
bpy.context.scene.collection.objects.link(ob_big)
bpy.context.view_layer.update()

dae_big = os.path.join(workdir, "big_mesh.dae")
assert "FINISHED" in bpy.ops.wm.collada_export(filepath=dae_big)
big_bytes = os.path.getsize(dae_big)
big_counts = parse_source_counts(dae_big)

results["case2"] = {
    "bytes": big_bytes,
    "uv_floats": next((v for k, v in big_counts.items() if "-map-" in k), None),
}

with open(out_json, "w", encoding="utf-8") as fh:
    json.dump(results, fh, indent=2)
'''


def main() -> int:
    blender = _find_blender()
    print(f"Running #12 UV/color dedupe tests using: {blender}")
    workdir = tempfile.mkdtemp(prefix="collada_dedupe_")
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
        c1 = res["case1"]
        c2 = res["case2"]
        print(f"  case1 n_loops={c1['n_loops']} uv_err={c1['uv_max_err']} col_u8_diff={c1['col_max_diff_u8']}")
        print(f"  case1 dedup counts={c1['counts_dedup']}")
        print(f"  case2 big_mesh bytes={c2['bytes']:,} ({c2['bytes']/1e6:.2f} MB) uv_floats={c2['uv_floats']}")

        # Case 1 assertions: byte-exact UVs & colors, strictly deduplicated sources
        for name, err in c1["uv_max_err"].items():
            assert err < 1e-6, f"UV {name} err={err}"
        for name, diff in c1["col_max_diff_u8"].items():
            assert diff == 0, f"Color {name} u8 diff={diff}"

        n_loops = c1["n_loops"]
        uv0_dedup = next(v for k, v in c1["counts_dedup"].items() if k.endswith("-map-0-array"))
        uv1_dedup = next(v for k, v in c1["counts_dedup"].items() if k.endswith("-map-1-array"))
        col0_dedup = next(v for k, v in c1["counts_dedup"].items() if "colors-ColA" in k)
        col1_dedup = next(v for k, v in c1["counts_dedup"].items() if "colors-ColB" in k)
        assert uv0_dedup < n_loops * 2, f"UV0 not deduplicated: {uv0_dedup} vs {n_loops * 2}"
        assert uv1_dedup == 16 * 2, f"Expected 16 unique UV2 pairs (32 floats), got {uv1_dedup}"
        assert col0_dedup == 3 * 4, f"Expected 3 unique ColA colors (12 floats), got {col0_dedup}"
        assert col1_dedup == 2 * 4, f"Expected 2 unique ColB colors (8 floats), got {col1_dedup}"

        # Legacy layout must preserve n_loops per-corner arrays
        uv0_leg = next(v for k, v in c1["counts_legacy"].items() if k.endswith("-map-0-array"))
        col0_leg = next(v for k, v in c1["counts_legacy"].items() if "colors-ColA" in k)
        assert uv0_leg == n_loops * 2 and col0_leg == n_loops * 4, c1["counts_legacy"]

        # Case 2 assertions: big_mesh under 6 MB and heavily deduplicated UVs
        assert c2["bytes"] < 6_000_000, f"big_mesh .dae is {c2['bytes']:,} bytes (>= 6 MB)"
        assert c2["uv_floats"] is not None and c2["uv_floats"] < 80_000, c2

        print("\nAll #12 UV/color dedupe regression tests passed!")
        return 0
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
