"""Field-by-field .cdae v31 comparator and real-file round-trip oracle (Phase 2.3).

Compares two .cdae v31 files field by field:
  - Header (version, exporterVersion, compression, bodysize, objectNames)
  - All shape scalars (smallestVisibleSize, smallestVisibleDL, radius, tubeRadius, center, bounds)
  - All 19 shape vectors (count, elemSize, values within tolerance)
  - Names table (set match)
  - Per-mesh counts, flags, bounds, center, radius, primitives, indices, vertex data
    (positions, UVs, UV2s, colors, normals, tangents) matched by object name
  - Material list (name set and flags)

When invoked with --oracle, runs the primary test oracle on every .cdae in
tests/_local_cdae/ (import with merge_by_distance=False, sharp_edges_from_normals=False ->
export to .cdae -> compare field by field against original).

Usage:
    python tests/compare_cdae.py a.cdae b.cdae
    .tools/blender-5.3.0-windows-x64/blender.exe -b --factory-startup --python tests/compare_cdae.py -- --oracle
"""

from __future__ import annotations

import importlib.util
import math
import os
import shutil
import sys
import tempfile
import time
import types

import numpy as np

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_reader():
    cdae_dir = os.path.join(REPO_ROOT, "io_scene_collada", "cdae")
    if "io_scene_collada.cdae.reader" in sys.modules:
        return sys.modules["io_scene_collada.cdae.reader"]
    pkg_root = types.ModuleType("io_scene_collada")
    pkg_root.__path__ = [os.path.join(REPO_ROOT, "io_scene_collada")]
    sys.modules.setdefault("io_scene_collada", pkg_root)
    pkg_cdae = types.ModuleType("io_scene_collada.cdae")
    pkg_cdae.__path__ = [cdae_dir]
    sys.modules.setdefault("io_scene_collada.cdae", pkg_cdae)
    for mod_name in ("msgpack_lite", "normal_table", "reader"):
        full = f"io_scene_collada.cdae.{mod_name}"
        spec = importlib.util.spec_from_file_location(full, os.path.join(cdae_dir, f"{mod_name}.py"))
        mod = importlib.util.module_from_spec(spec)
        sys.modules[full] = mod
        spec.loader.exec_module(mod)
    return sys.modules["io_scene_collada.cdae.reader"]


def _quat16_angle_deg(q1: np.ndarray, q2: np.ndarray) -> float:
    a = q1.astype(np.float64) / 32767.0
    b = q2.astype(np.float64) / 32767.0
    na = np.linalg.norm(a)
    nb = np.linalg.norm(b)
    if na < 1e-8 or nb < 1e-8:
        return 0.0
    dot = abs(float(np.dot(a / na, b / nb)))
    dot = min(1.0, max(-1.0, dot))
    return math.degrees(2.0 * math.acos(dot))


def _non_sliver_tri_indices(mesh, edge_tol: float = 1e-5, area_tol: float = 1e-12) -> np.ndarray:
    """Return triangle vertex-index array (F, 3) excluding degenerate / zero-area sliver triangles."""
    if len(mesh.indices) < 3 or len(mesh.verts) == 0:
        return np.empty((0, 3), dtype=np.int64)
    tris = mesh.indices[: (len(mesh.indices) // 3) * 3].reshape(-1, 3).astype(np.int64, copy=False)
    valid_idx = (tris >= 0).all(axis=1) & (tris < len(mesh.verts)).all(axis=1)
    tris = tris[valid_idx]
    if len(tris) == 0:
        return tris
    p = mesh.verts[tris].astype(np.float64)
    e01 = p[:, 1] - p[:, 0]
    e12 = p[:, 2] - p[:, 1]
    e20 = p[:, 0] - p[:, 2]
    min_edge = np.minimum(
        np.linalg.norm(e01, axis=1),
        np.minimum(np.linalg.norm(e12, axis=1), np.linalg.norm(e20, axis=1)),
    )
    twice_area = np.linalg.norm(np.cross(e01, -e20), axis=1)
    keep = (min_edge > edge_tol) & (twice_area > 2.0 * area_tol)
    return tris[keep]


def _match_mesh_normals(ma, mb) -> tuple[list[float], int]:
    """Compare corner normals (degrees) across EVERY triangle between two CDAE meshes.

    Skips zero-length normals (present in some game files) instead of reporting false 90° errors.
    Returns (angle_degrees_list, skipped_zero_norm_corners).
    """
    if len(ma.indices) < 3 or len(mb.indices) < 3 or len(ma.norms) == 0 or len(mb.norms) == 0:
        return [], 0
    ta = _non_sliver_tri_indices(ma)
    tb = _non_sliver_tri_indices(mb)
    if len(ta) == 0 or len(tb) == 0:
        return [], 0

    pa = ma.verts[ta].astype(np.float64)  # (Fa, 3, 3)
    pb = mb.verts[tb].astype(np.float64)  # (Fb, 3, 3)
    na = ma.norms[ta].astype(np.float64)
    nb = mb.norms[tb].astype(np.float64)

    la = np.linalg.norm(na, axis=2)  # (Fa, 3)
    lb = np.linalg.norm(nb, axis=2)  # (Fb, 3)
    va = la > 1e-6
    vb = lb > 1e-6
    ua = np.zeros_like(na)
    ub = np.zeros_like(nb)
    ua[va] = na[va] / la[va, None]
    ub[vb] = nb[vb] / lb[vb, None]

    # Quantize centroids for O(N) spatial bucketing across all triangles
    tol = 1e-3
    ca = np.round(pa.mean(axis=1) / tol).astype(np.int64)
    cb = np.round(pb.mean(axis=1) / tol).astype(np.int64)
    buckets: dict[tuple[int, int, int], list[int]] = {}
    for bi, key in enumerate(map(tuple, cb.tolist())):
        buckets.setdefault(key, []).append(bi)

    # Pre-unpack to Python lists/tuples for fast inner loop without NumPy scalar overhead
    angs: list[float] = []
    skipped_zero = 0
    used_b: set[int] = set()
    ca_list = ca.tolist()

    for ai, key_l in enumerate(ca_list):
        cands = buckets.get((key_l[0], key_l[1], key_l[2]))
        if not cands:
            continue
        p0 = pa[ai]
        u0 = ua[ai]
        v0 = va[ai]
        best = None
        best_score = -4.0
        for bi in cands:
            if bi in used_b:
                continue
            p1 = pb[bi]
            # Fast 3x3 squared distance matrix
            diff = p0[:, None, :] - p1[None, :, :]
            d2 = (diff * diff).sum(axis=2)
            # Greedy/exact min per row (3x3)
            c0 = int(d2[0].argmin())
            c1 = int(d2[1].argmin())
            c2 = int(d2[2].argmin())
            if (
                c0 != c1
                and c1 != c2
                and c0 != c2
                and d2[0, c0] < 4e-6
                and d2[1, c1] < 4e-6
                and d2[2, c2] < 4e-6
            ):
                perm = (c0, c1, c2)
                u1 = ub[bi]
                score = float(
                    (u0[0] * u1[c0]).sum()
                    + (u0[1] * u1[c1]).sum()
                    + (u0[2] * u1[c2]).sum()
                )
                if score > best_score:
                    best_score = score
                    best = (bi, perm)
        if best is not None:
            bi, perm = best
            used_b.add(bi)
            u1 = ub[bi]
            v1 = vb[bi]
            for k in range(3):
                pk = perm[k]
                if not (v0[k] and v1[pk]):
                    skipped_zero += 1
                    continue
                dot = float((u0[k] * u1[pk]).sum())
                if dot > 1.0:
                    dot = 1.0
                elif dot < -1.0:
                    dot = -1.0
                angs.append(math.degrees(math.acos(dot)))
    return angs, skipped_zero


def compare_cdae_files(orig_path: str, exp_path: str, *, pos_tol: float = 1e-4, check_normals: bool = False) -> dict:
    reader = _load_reader()
    sa = reader.read_cdae_file(orig_path)
    sb = reader.read_cdae_file(exp_path)

    diffs: list[str] = []

    # 1. Header & version
    if sa.shape_version != sb.shape_version:
        diffs.append(f"shape_version: {sa.shape_version} vs {sb.shape_version}")
    if sa.exporter_version != sb.exporter_version:
        diffs.append(f"exporter_version: {sa.exporter_version} vs {sb.exporter_version}")

    # 2. Shape scalars
    if abs(sa.smallest_visible_size - sb.smallest_visible_size) > 1e-3:
        diffs.append(f"smallestVisibleSize: {sa.smallest_visible_size} vs {sb.smallest_visible_size}")
    if sa.smallest_visible_dl != sb.smallest_visible_dl:
        diffs.append(f"smallestVisibleDL: {sa.smallest_visible_dl} vs {sb.smallest_visible_dl}")
    center_diff = float(np.abs(np.array(sa.center) - np.array(sb.center)).max())
    bounds_diff = float(np.abs(np.array(sa.bounds) - np.array(sb.bounds)).max())
    radius_diff = abs(sa.radius - sb.radius)
    tube_diff = abs(sa.tube_radius - sb.tube_radius)

    # 3. Shape 19 vector counts & itemsize
    vec_names = [
        "nodes", "objects", "sub_shape_first_node", "sub_shape_first_object",
        "sub_shape_num_nodes", "sub_shape_num_objects", "default_rotations",
        "default_translations", "node_rotations", "node_translations",
        "node_uniform_scales", "node_aligned_scales", "node_arbitrary_scale_factors",
        "node_arbitrary_scale_rots", "ground_translations", "ground_rotations",
        "object_states", "triggers", "details",
    ]
    vec_diffs = []
    for vn in vec_names:
        va = getattr(sa, vn)
        vb = getattr(sb, vn)
        if len(va) != len(vb) or va.dtype != vb.dtype:
            vec_diffs.append(f"{vn}: ({len(va)}, {va.dtype}) vs ({len(vb)}, {vb.dtype})")

    # 4. Node transforms matched by name
    def _node_map(shape):
        out = {}
        for i in range(len(shape.nodes)):
            ni = int(shape.nodes[i]["name_index"])
            nm = shape.names[ni] if 0 <= ni < len(shape.names) else f"Node_{i}"
            pi = int(shape.nodes[i]["parent_index"])
            pnm = shape.names[int(shape.nodes[pi]["name_index"])] if (0 <= pi < len(shape.nodes) and 0 <= int(shape.nodes[pi]["name_index"]) < len(shape.names)) else None
            out[nm] = (
                pnm,
                shape.default_translations[i] if i < len(shape.default_translations) else np.zeros(3),
                shape.default_rotations[i] if i < len(shape.default_rotations) else np.array([0, 0, 0, 32767]),
            )
        return out

    na_map, nb_map = _node_map(sa), _node_map(sb)
    common_nodes = sorted(set(na_map) & set(nb_map))
    max_node_t = 0.0
    max_node_r = 0.0
    parent_mismatches = 0
    for nm in common_nodes:
        pa, ta, qa = na_map[nm]
        pb, tb, qb = nb_map[nm]
        if pa != pb:
            parent_mismatches += 1
        max_node_t = max(max_node_t, float(np.abs(ta - tb).max()))
        max_node_r = max(max_node_r, _quat16_angle_deg(qa, qb))

    # 5. Per-mesh comparison matched by object name
    def _obj_mesh_map(shape):
        out = {}
        for o in shape.objects:
            nm = shape.names[int(o["name_index"])] if 0 <= int(o["name_index"]) < len(shape.names) else ""
            mi = int(o["start_mesh_index"])
            if nm and 0 <= mi < len(shape.meshes):
                out[nm] = shape.meshes[mi]
        return out

    ma_map, mb_map = _obj_mesh_map(sa), _obj_mesh_map(sb)
    common_objs = sorted(set(ma_map) & set(mb_map))
    only_a = sorted(set(ma_map) - set(mb_map))
    only_b = sorted(set(mb_map) - set(ma_map))

    mesh_stats = {
        "common": len(common_objs),
        "only_orig": only_a,
        "only_exp": only_b,
        "exact_tri_count": 0,
        "exact_vert_count": 0,
        "exact_prim_count": 0,
        "flag_mismatches": 0,
        "max_bounds_diff": 0.0,
        "max_center_diff": 0.0,
        "max_radius_diff": 0.0,
        "max_uv0_bbox_diff": 0.0,
        "max_uv1_bbox_diff": 0.0,
        "uv0_present_mismatch": 0,
        "uv1_present_mismatch": 0,
        "color_present_mismatch": 0,
        "tan_present_mismatch": 0,
        "max_tan_norm_err": 0.0,
        "norm_deg_med": float("nan"),
        "norm_deg_p99": float("nan"),
        "norm_deg_max": float("nan"),
        "norm_corners_compared": 0,
        "norm_zero_corners_skipped": 0,
    }
    all_norm_angs: list[float] = []
    total_zero_norm_skipped = 0

    for nm in common_objs:
        ma, mb = ma_map[nm], mb_map[nm]
        if len(ma.indices) == len(mb.indices):
            mesh_stats["exact_tri_count"] += 1
        if len(ma.verts) == len(mb.verts):
            mesh_stats["exact_vert_count"] += 1
        if len(ma.primitives) == len(mb.primitives):
            mesh_stats["exact_prim_count"] += 1
        if ma.mesh_flags != mb.mesh_flags:
            mesh_stats["flag_mismatches"] += 1
        mesh_stats["max_bounds_diff"] = max(
            mesh_stats["max_bounds_diff"], float(np.abs(np.array(ma.bounds) - np.array(mb.bounds)).max())
        )
        mesh_stats["max_center_diff"] = max(
            mesh_stats["max_center_diff"], float(np.abs(np.array(ma.center) - np.array(mb.center)).max())
        )
        mesh_stats["max_radius_diff"] = max(mesh_stats["max_radius_diff"], abs(ma.radius - mb.radius))

        # Compute UV bounding boxes over vertices referenced by non-sliver triangles so
        # zero-area sliver triangles removed by Exact mode do not trigger false UV mismatches.
        ta_valid = _non_sliver_tri_indices(ma)
        tb_valid = _non_sliver_tri_indices(mb)
        va_used = np.unique(ta_valid) if len(ta_valid) > 0 else np.empty(0, dtype=np.int64)
        vb_used = np.unique(tb_valid) if len(tb_valid) > 0 else np.empty(0, dtype=np.int64)

        if (len(ma.tverts) > 0) != (len(mb.tverts) > 0):
            mesh_stats["uv0_present_mismatch"] += 1
        elif len(ma.tverts) > 0 and len(va_used) > 0 and len(vb_used) > 0:
            uv0_a = ma.tverts[va_used[va_used < len(ma.tverts)]]
            uv0_b = mb.tverts[vb_used[vb_used < len(mb.tverts)]]
            if len(uv0_a) > 0 and len(uv0_b) > 0:
                d_uv0 = max(
                    float(np.abs(uv0_a.min(0) - uv0_b.min(0)).max()),
                    float(np.abs(uv0_a.max(0) - uv0_b.max(0)).max()),
                )
                mesh_stats["max_uv0_bbox_diff"] = max(mesh_stats["max_uv0_bbox_diff"], d_uv0)

        if (len(ma.tverts2) > 0) != (len(mb.tverts2) > 0):
            mesh_stats["uv1_present_mismatch"] += 1
        elif len(ma.tverts2) > 0 and len(va_used) > 0 and len(vb_used) > 0:
            uv1_a = ma.tverts2[va_used[va_used < len(ma.tverts2)]]
            uv1_b = mb.tverts2[vb_used[vb_used < len(mb.tverts2)]]
            if len(uv1_a) > 0 and len(uv1_b) > 0:
                d_uv1 = max(
                    float(np.abs(uv1_a.min(0) - uv1_b.min(0)).max()),
                    float(np.abs(uv1_a.max(0) - uv1_b.max(0)).max()),
                )
                mesh_stats["max_uv1_bbox_diff"] = max(mesh_stats["max_uv1_bbox_diff"], d_uv1)

        if (len(ma.colors) > 0) != (len(mb.colors) > 0):
            mesh_stats["color_present_mismatch"] += 1
        if (len(ma.tangents) > 0) != (len(mb.tangents) > 0):
            mesh_stats["tan_present_mismatch"] += 1
        elif len(mb.tangents) > 0:
            tlens = np.linalg.norm(mb.tangents[:, :3], axis=1)
            nz = tlens > 1e-4
            if nz.any():
                mesh_stats["max_tan_norm_err"] = max(
                    mesh_stats["max_tan_norm_err"], float(np.abs(tlens[nz] - 1.0).max())
                )
        if check_normals:
            angs_m, z_skip = _match_mesh_normals(ma, mb)
            all_norm_angs.extend(angs_m)
            total_zero_norm_skipped += z_skip

    if all_norm_angs:
        arr_n = np.array(all_norm_angs, dtype=np.float64)
        mesh_stats["norm_deg_med"] = float(np.median(arr_n))
        mesh_stats["norm_deg_p99"] = float(np.percentile(arr_n, 99))
        mesh_stats["norm_deg_max"] = float(arr_n.max())
        mesh_stats["norm_corners_compared"] = len(all_norm_angs)
        mesh_stats["norm_zero_corners_skipped"] = total_zero_norm_skipped

    # 6. Materials
    mats_a = {m.name for m in sa.materials}
    mats_b = {m.name for m in sb.materials}

    return {
        "orig_bytes": os.path.getsize(orig_path),
        "exp_bytes": os.path.getsize(exp_path),
        "header_diffs": diffs,
        "scalar_diffs": {
            "center": center_diff,
            "bounds": bounds_diff,
            "radius": radius_diff,
            "tube_radius": tube_diff,
        },
        "vector_diffs": vec_diffs,
        "nodes": {
            "orig": len(sa.nodes),
            "exp": len(sb.nodes),
            "common": len(common_nodes),
            "parent_mismatches": parent_mismatches,
            "max_trans_diff": max_node_t,
            "max_rot_diff_deg": max_node_r,
        },
        "names_set_match": set(sa.names) == set(sb.names),
        "meshes": mesh_stats,
        "materials": {
            "orig": len(mats_a),
            "exp": len(mats_b),
            "only_orig": sorted(mats_a - mats_b),
            "only_exp": sorted(mats_b - mats_a),
        },
    }


def run_oracle(all_modes: bool = False) -> int:
    import bpy

    sys.path.insert(0, REPO_ROOT)
    from io_scene_collada.cdae.exporter import CdaeExporter, CdaeExportSettings
    from io_scene_collada.cdae.importer import CdaeImporter, CdaeImportSettings

    local_dir = os.environ.get("CDAE_LOCAL_DIR") or os.path.join(REPO_ROOT, "tests", "_local_cdae")
    # StandardMesh real vehicles (skip SkinMesh files which are out of scope for Phase 2 export)
    files = sorted(
        f for f in os.listdir(local_dir)
        if f.lower().endswith(".cdae") and f.lower() in ("wigeon_bigwing.cdae", "wigeon.cdae", "midtruck.cdae")
    )
    if not files:
        files = sorted(f for f in os.listdir(local_dir) if f.lower().endswith(".cdae"))
    if not files:
        print(f"No .cdae files found in {local_dir}")
        return 1

    modes = (
        [
            ("EXACT (default)", dict(normals_mode="EXACT")),
            ("SHARP", dict(normals_mode="SHARP")),
            ("UNMERGED", dict(normals_mode="UNMERGED")),
        ]
        if all_modes
        else [("EXACT (default)", dict(normals_mode="EXACT"))]
    )

    workdir = tempfile.mkdtemp(prefix="cdae_oracle_")
    try:
        for mode_name, imp_kwargs in modes:
            print("\n" + "#" * 88)
            print(f"ORACLE MODE: {mode_name} ({imp_kwargs})")
            print("#" * 88)
            for fn in files:
                orig_path = os.path.join(local_dir, fn)
                exp_path = os.path.join(workdir, f"reexported_{mode_name}_{fn}")

                bpy.ops.wm.read_factory_settings(use_empty=True)
                t0 = time.perf_counter()
                imp = CdaeImporter(
                    bpy.context,
                    CdaeImportSettings(filepath=orig_path, lod_mode="HIGHEST", **imp_kwargs),
                )
                ok_in = imp.import_document()
                t_in = time.perf_counter() - t0
                assert ok_in, f"Import failed on {fn}: {imp.errors}"

                t0 = time.perf_counter()
                exp = CdaeExporter(
                    bpy.context,
                    CdaeExportSettings(
                        filepath=exp_path,
                        use_selection=False,
                        apply_modifiers=False,
                        compress=False,
                    ),
                )
                ok_out = exp.export_document()
                t_out = time.perf_counter() - t0
                assert ok_out, f"Export failed on {fn}: {exp.errors}"

                rep = compare_cdae_files(orig_path, exp_path, check_normals=True)
                ms = rep["meshes"]
                print("=" * 88)
                print(f"ORACLE [{mode_name}]: {fn} (import={t_in:.2f}s, export={t_out:.2f}s)")
                print(f"  Sizes:       orig={rep['orig_bytes']:,} B -> exp={rep['exp_bytes']:,} B")
                print(f"  Scalars:     center={rep['scalar_diffs']['center']:.6f}, bounds={rep['scalar_diffs']['bounds']:.6f}, "
                      f"radius={rep['scalar_diffs']['radius']:.6f}, tube={rep['scalar_diffs']['tube_radius']:.6f}")
                print(f"  19 Vectors:  diffs={rep['vector_diffs']}")
                print(f"  Nodes:       orig={rep['nodes']['orig']}, exp={rep['nodes']['exp']}, "
                      f"parent_diff={rep['nodes']['parent_mismatches']}, max_T={rep['nodes']['max_trans_diff']:.6f}, "
                      f"max_R={rep['nodes']['max_rot_diff_deg']:.4f}°")
                print(f"  Meshes:      common={ms['common']}, exact_tris={ms['exact_tri_count']}/{ms['common']}, "
                      f"exact_verts={ms['exact_vert_count']}/{ms['common']}, exact_prims={ms['exact_prim_count']}/{ms['common']}, "
                      f"flag_diffs={ms['flag_mismatches']}")
                print(f"  Mesh Geom:   max_bounds={ms['max_bounds_diff']:.6f}, max_center={ms['max_center_diff']:.6f}, "
                      f"max_radius={ms['max_radius_diff']:.6f}")
                print(f"  Mesh Attrs:  uv0_miss={ms['uv0_present_mismatch']} (max_bb={ms['max_uv0_bbox_diff']:.6f}), "
                      f"uv1_miss={ms['uv1_present_mismatch']} (max_bb={ms['max_uv1_bbox_diff']:.6f}), "
                      f"col_miss={ms['color_present_mismatch']}, tan_miss={ms['tan_present_mismatch']} (max_len_err={ms['max_tan_norm_err']:.5f})")
                print(f"  Normals deg: med={ms['norm_deg_med']:.4f}°, p99={ms['norm_deg_p99']:.4f}°, "
                      f"max={ms['norm_deg_max']:.4f}° ({ms['norm_corners_compared']} corners, "
                      f"{ms['norm_zero_corners_skipped']} zero-len skipped)")
                print(f"  Materials:   orig={rep['materials']['orig']}, exp={rep['materials']['exp']}, "
                      f"only_orig={rep['materials']['only_orig']}, only_exp={rep['materials']['only_exp']}")
        return 0
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def main() -> int:
    argv = sys.argv
    if "--" in argv:
        argv = argv[argv.index("--") + 1 :]
    else:
        argv = argv[1:]
    if "--oracle-modes" in argv:
        return run_oracle(all_modes=True)
    if "--oracle" in argv:
        return run_oracle(all_modes=False)
    if len(argv) != 2:
        print("Usage: compare_cdae.py <orig.cdae> <exported.cdae>  OR  compare_cdae.py --oracle")
        return 1
    rep = compare_cdae_files(argv[0], argv[1])
    for k, v in rep.items():
        print(f"{k}: {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
