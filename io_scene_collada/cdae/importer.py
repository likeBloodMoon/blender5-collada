# SPDX-FileCopyrightText: 2012 GarageGames, LLC (Torque3D, MIT)
# SPDX-FileCopyrightText: 2026 Blender5-Collada Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Blender 5.0+ scene builder for BeamNG Cached Collada (.cdae) v31 shapes."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass

import bpy
import numpy as np
from mathutils import Matrix, Quaternion, Vector
from mathutils.kdtree import KDTree

from ..beamng_materials import (
    alt_image_refs,
    lookup_material_color_maps,
    parse_beamng_materials_json,
)
from ..import_collada import DocumentImporter
from .normal_table import U8_TO_NORMAL_TABLE
from .reader import (
    CdaeError,
    CdaeMesh,
    CdaeShape,
    MESH_TYPE_NULL,
    MESH_TYPE_SKIN,
    PRIM_INDEXED,
    PRIM_MATERIAL_MASK,
    PRIM_NO_MATERIAL,
    PRIM_TYPE_FAN,
    PRIM_TYPE_MASK,
    PRIM_TYPE_STRIP,
    PRIM_TYPE_TRIANGLES,
    read_cdae_file,
)


@dataclass
class CdaeImportSettings:
    filepath: str = ""
    lod_mode: str = "HIGHEST"  # "HIGHEST" or "ALL"
    import_collision: bool = False
    normals_mode: str = "EXACT"  # "EXACT", "SHARP", or "UNMERGED"
    merge_distance: float = 1e-5
    sharp_angle_threshold: float = math.radians(1.0)


def _quat16_to_blender(q16: np.ndarray) -> Quaternion:
    """Convert Torque3D Quat16 (x, y, z, w int16 / 32767) to Blender Quaternion(w, -x, -y, -z)."""
    qx = float(q16[0]) / 32767.0
    qy = float(q16[1]) / 32767.0
    qz = float(q16[2]) / 32767.0
    qw = float(q16[3]) / 32767.0
    q = Quaternion((qw, -qx, -qy, -qz))
    if q.magnitude > 0.0:
        q.normalize()
    else:
        q.identity()
    return q


def _safe_name(names: list[str], idx: int, fallback: str) -> str:
    if 0 <= idx < len(names) and names[idx]:
        return names[idx]
    return fallback


def _bounds_extent(bounds: tuple[float, ...] | list[float]) -> float:
    if len(bounds) < 6:
        return 0.0
    dx = float(bounds[3]) - float(bounds[0])
    dy = float(bounds[4]) - float(bounds[1])
    dz = float(bounds[5]) - float(bounds[2])
    if not (np.isfinite(dx) and np.isfinite(dy) and np.isfinite(dz)):
        return 0.0
    ext = max(dx, dy, dz)
    return ext if ext > 0.0 else 0.0


def _is_collision_detail(name: str, size: float) -> bool:
    if size < 0.0:
        return True
    low = name.lower()
    if size <= 0.0 and (low.startswith("col") or "collision" in low or low.startswith("loscol")):
        return True
    return False


def _primitives_to_triangles(
    mesh: CdaeMesh,
    num_verts: int,
    warnings: list[str],
    mesh_label: str,
) -> tuple[np.ndarray, np.ndarray]:
    """Convert TSDrawPrimitive list into triangle vertex indices (F, 3) and material indices (F,)."""
    tri_chunks: list[np.ndarray] = []
    mat_chunks: list[np.ndarray] = []
    indices = mesh.indices
    n_indices = len(indices)
    dropped = 0

    for prim in mesh.primitives:
        start = int(prim["start"])
        count = int(prim["num_elements"])
        mat_raw = int(prim["mat_index"]) & 0xFFFFFFFF

        if count < 3 or start < 0:
            continue

        prim_type = mat_raw & PRIM_TYPE_MASK
        is_indexed = bool(mat_raw & PRIM_INDEXED)
        no_mat = bool(mat_raw & PRIM_NO_MATERIAL)
        mat_idx = -1 if no_mat else int(mat_raw & PRIM_MATERIAL_MASK)

        if is_indexed:
            if start >= n_indices:
                dropped += 1
                continue
            end = min(start + count, n_indices)
            if end - start < 3:
                continue
            elem = indices[start:end].astype(np.int64, copy=False)
        else:
            elem = np.arange(start, start + count, dtype=np.int64)

        if prim_type == PRIM_TYPE_TRIANGLES:
            n_tris = len(elem) // 3
            if n_tris == 0:
                continue
            tris = elem[: n_tris * 3].reshape(n_tris, 3)
        elif prim_type == PRIM_TYPE_STRIP:
            n_tris = len(elem) - 2
            if n_tris <= 0:
                continue
            a = elem[:-2]
            b = elem[1:-1]
            c = elem[2:]
            tris = np.empty((n_tris, 3), dtype=np.int64)
            # Alternating winding matching Torque3D TSMesh::buildPolyList
            tris[0::2, 0] = a[0::2]
            tris[0::2, 1] = b[0::2]
            tris[0::2, 2] = c[0::2]
            tris[1::2, 0] = b[1::2]
            tris[1::2, 1] = a[1::2]
            tris[1::2, 2] = c[1::2]
            non_degen = (tris[:, 0] != tris[:, 1]) & (tris[:, 0] != tris[:, 2]) & (tris[:, 1] != tris[:, 2])
            tris = tris[non_degen]
        elif prim_type == PRIM_TYPE_FAN:
            n_tris = len(elem) - 2
            if n_tris <= 0:
                continue
            tris = np.empty((n_tris, 3), dtype=np.int64)
            tris[:, 0] = elem[0]
            tris[:, 1] = elem[1:-1]
            tris[:, 2] = elem[2:]
            non_degen = (tris[:, 0] != tris[:, 1]) & (tris[:, 0] != tris[:, 2]) & (tris[:, 1] != tris[:, 2])
            tris = tris[non_degen]
        else:
            warnings.append(f"Mesh '{mesh_label}': unsupported primitive type 0x{prim_type:X}, skipped")
            continue

        if len(tris) == 0:
            continue

        ok = (
            (tris[:, 0] >= 0) & (tris[:, 0] < num_verts)
            & (tris[:, 1] >= 0) & (tris[:, 1] < num_verts)
            & (tris[:, 2] >= 0) & (tris[:, 2] < num_verts)
        )
        bad_cnt = int(len(tris) - ok.sum())
        if bad_cnt > 0:
            dropped += bad_cnt
            tris = tris[ok]
        if len(tris) == 0:
            continue

        tri_chunks.append(tris.astype(np.int32, copy=False))
        mat_chunks.append(np.full(len(tris), mat_idx, dtype=np.int32))

    if dropped > 0:
        warnings.append(f"Mesh '{mesh_label}': dropped {dropped} triangle(s) with out-of-range vertex indices")

    if not tri_chunks:
        return np.empty((0, 3), dtype=np.int32), np.empty(0, dtype=np.int32)
    all_tris = np.vstack(tri_chunks) if len(tri_chunks) > 1 else tri_chunks[0]
    all_mats = np.concatenate(mat_chunks) if len(mat_chunks) > 1 else mat_chunks[0]
    # Torque3D / BeamNG stores front faces in CW winding (colladaAppMesh.cpp:609-614);
    # flip to Blender's CCW front-face winding.
    all_tris = np.ascontiguousarray(all_tris[:, ::-1])
    return all_tris, all_mats


def _merge_verts_by_distance(
    verts: np.ndarray,
    tris: np.ndarray,
    tol: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Merge duplicate positions within Euclidean distance <= tol using exact dedup + KD-tree.

    Returns (merged_verts, remapped_tris, kept_tri_mask, old_to_new_vert_idx).
    """
    n = len(verts)
    if n == 0 or tol <= 0.0:
        old_to_new = np.arange(n, dtype=np.int32)
        return verts, tris, np.ones(len(tris), dtype=bool), old_to_new

    # Stage 1: Exact float32 dedup collapses identical GPU-split seam vertices in C
    v_f32 = np.ascontiguousarray(verts, dtype="<f4")
    if (v_f32 == 0.0).any():
        v_f32 = v_f32.copy()
        v_f32[v_f32 == 0.0] = 0.0
    v_keys = v_f32.view(np.dtype((np.void, 12))).ravel()
    _, unique_idx, inv_exact = np.unique(v_keys, return_index=True, return_inverse=True)
    order = np.argsort(unique_idx)
    inv_order = np.empty_like(order)
    inv_order[order] = np.arange(len(order))
    u_verts = verts[unique_idx[order]]
    stage1_map = inv_order[inv_exact].astype(np.int32)
    n_u = len(u_verts)

    # Stage 2: Exact Euclidean <= tol clustering via KDTree without transitive chaining.
    # Every vertex in a cluster is guaranteed to be within <= tol of the cluster's first vertex.
    if n_u > 1:
        kd = KDTree(n_u)
        u_list = u_verts.tolist()
        for i, co in enumerate(u_list):
            kd.insert(co, i)
        kd.balance()

        u_to_new = np.full(n_u, -1, dtype=np.int32)
        keep_u: list[int] = []
        for i, co in enumerate(u_list):
            if u_to_new[i] != -1:
                continue
            nid = len(keep_u)
            keep_u.append(i)
            u_to_new[i] = nid
            hits = kd.find_range(co, tol)
            if len(hits) > 1:
                for _hco, j, _dist in hits:
                    if u_to_new[j] == -1:
                        u_to_new[j] = nid

        if len(keep_u) < n_u:
            merged_verts = u_verts[np.asarray(keep_u, dtype=np.int32)]
            old_to_new = u_to_new[stage1_map]
        else:
            merged_verts = u_verts
            old_to_new = stage1_map
    else:
        merged_verts = u_verts
        old_to_new = stage1_map

    remapped = old_to_new[tris]
    kept = (remapped[:, 0] != remapped[:, 1]) & (remapped[:, 0] != remapped[:, 2]) & (remapped[:, 1] != remapped[:, 2])
    return merged_verts, remapped[kept], kept, old_to_new


def _resolve_duplicate_faces(
    verts: np.ndarray,
    tris: np.ndarray,
    tri_mats: np.ndarray,
    uv0_loops: np.ndarray | None,
    uv1_loops: np.ndarray | None,
    col_loops: np.ndarray | None,
    norm_loops: np.ndarray | None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, int, int, int, np.ndarray]:
    """Classify triangles sharing the same 3 vertices into double-sided, layered, or truly identical.

    - Opposite winding (same vertex set, reversed cyclic order): intentional double-sided
      geometry -> kept with dedicated unwelded vertex copies.
    - Same winding, different material or differing per-corner UV0/UV1/color/normal:
      overlay/decal layer -> kept with dedicated unwelded vertex copies.
    - Same winding AND identical material, UV0, UV1, color, and normal (within tolerance):
      truly identical duplicate junk -> dropped along with its loop data.

    Returns (verts, tris, kept_mask, double_sided_kept, layered_kept, exact_dups_removed, extra_vert_sources).
    """
    n_tris = len(tris)
    empty_src = np.empty(0, dtype=np.int32)
    if n_tris <= 1:
        return verts, tris, np.ones(n_tris, dtype=bool), 0, 0, 0, empty_src

    sorted_tris = np.sort(tris, axis=1)
    _, inv, counts = np.unique(sorted_tris, axis=0, return_inverse=True, return_counts=True)
    if not (counts > 1).any():
        return verts, tris, np.ones(n_tris, dtype=bool), 0, 0, 0, empty_src

    a, b, c = tris[:, 0], tris[:, 1], tris[:, 2]
    parity = (a < b).astype(np.int8) + (b < c).astype(np.int8) + (c < a).astype(np.int8)

    dup_face_idx = np.flatnonzero(counts[inv] > 1)
    order = np.argsort(inv[dup_face_idx], kind="stable")
    dup_face_idx = dup_face_idx[order]
    dup_groups = inv[dup_face_idx]
    split_pts = np.flatnonzero(np.diff(dup_groups)) + 1
    groups = np.split(dup_face_idx, split_pts)

    uv0_f = uv0_loops.reshape(n_tris, 3, 2) if uv0_loops is not None else None
    uv1_f = uv1_loops.reshape(n_tris, 3, 2) if uv1_loops is not None else None
    col_f = col_loops.reshape(n_tris, 3, 4) if col_loops is not None else None
    norm_f = norm_loops.reshape(n_tris, 3, 3) if norm_loops is not None else None

    def _same_face_attrs(f0: int, fk: int) -> bool:
        if int(tri_mats[f0]) != int(tri_mats[fk]):
            return False
        t0 = tris[f0].tolist()
        tk = tris[fk].tolist()
        # Same winding implies cyclic rotation aligns the 3 corners
        shift = tk.index(t0[0])
        perm = [shift, (shift + 1) % 3, (shift + 2) % 3]
        if uv0_f is not None and float(np.abs(uv0_f[f0] - uv0_f[fk][perm]).max()) > 1e-4:
            return False
        if uv1_f is not None and float(np.abs(uv1_f[f0] - uv1_f[fk][perm]).max()) > 1e-4:
            return False
        if col_f is not None and float(np.abs(col_f[f0] - col_f[fk][perm]).max()) > 2e-3:
            return False
        if norm_f is not None and float(np.abs(norm_f[f0] - norm_f[fk][perm]).max()) > 1e-3:
            return False
        return True

    keep_mask = np.ones(n_tris, dtype=bool)
    # layer_faces[layer_idx] = list of face indices assigned to unwelded vertex layer (layer_idx >= 1)
    layer_faces: list[list[int]] = []
    double_sided_kept = 0
    layered_kept = 0
    exact_dups_removed = 0

    for grp in groups:
        # Track kept faces in this vertex-set group: list of (face_idx, winding)
        kept_in_grp: list[tuple[int, int]] = []
        for fi in grp.tolist():
            w = int(parity[fi])
            is_exact_dup = False
            has_same_winding = False
            for prev_fi, prev_w in kept_in_grp:
                if prev_w == w:
                    has_same_winding = True
                    if _same_face_attrs(prev_fi, fi):
                        is_exact_dup = True
                        break
            if is_exact_dup:
                keep_mask[fi] = False
                exact_dups_removed += 1
                continue

            layer_slot = len(kept_in_grp)
            kept_in_grp.append((fi, w))
            if layer_slot == 0:
                continue  # stays on base merged vertex layer 0
            if not has_same_winding:
                double_sided_kept += 1
            else:
                layered_kept += 1
            while len(layer_faces) < layer_slot:
                layer_faces.append([])
            layer_faces[layer_slot - 1].append(fi)

    out_tris = tris.copy()
    out_verts = verts
    extra_src_parts: list[np.ndarray] = []

    if layer_faces:
        vert_parts = [verts]
        cur_base = len(verts)
        for lf in layer_faces:
            lf_arr = np.array(lf, dtype=np.int32)
            lf_verts = np.unique(out_tris[lf_arr])
            vert_parts.append(verts[lf_verts].copy())
            remap = np.arange(len(verts), dtype=np.int32)
            remap[lf_verts] = np.arange(cur_base, cur_base + len(lf_verts), dtype=np.int32)
            out_tris[lf_arr] = remap[out_tris[lf_arr]]
            extra_src_parts.append(lf_verts.astype(np.int32, copy=False))
            cur_base += len(lf_verts)
        out_verts = np.vstack(vert_parts)

    extra_src = np.concatenate(extra_src_parts) if extra_src_parts else empty_src
    if exact_dups_removed > 0:
        out_tris = out_tris[keep_mask]

    return out_verts, out_tris, keep_mask, double_sided_kept, layered_kept, exact_dups_removed, extra_src


class CdaeImporter:
    """Imports a BeamNG .cdae v31 file into the active Blender scene."""

    def __init__(self, context, settings: CdaeImportSettings) -> None:
        self.context = context
        self.scene = context.scene
        self.view_layer = context.view_layer
        alc = context.view_layer.active_layer_collection
        self.target_collection = alc.collection if alc is not None else context.scene.collection
        self.settings = settings
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.infos: list[str] = []

    def import_document(self) -> bool:
        try:
            shape = read_cdae_file(self.settings.filepath)
        except CdaeError as exc:
            self.errors.append(str(exc))
            return False
        except Exception as exc:
            self.errors.append(f"Failed to read .cdae file: {exc}")
            return False

        self.warnings.extend(shape.warnings)
        if shape.num_sequences > 0:
            self.infos.append(
                f"Skipped {shape.num_sequences} animation sequence(s) (sequences are not imported)"
            )

        # Deselect existing objects
        for ob in self.view_layer.objects:
            ob.select_set(False)

        # Compute node local & world matrices
        n_nodes = len(shape.nodes)
        local_mats: list[Matrix] = []
        for i in range(n_nodes):
            t = Vector(shape.default_translations[i].tolist()) if i < len(shape.default_translations) else Vector((0, 0, 0))
            q = _quat16_to_blender(shape.default_rotations[i]) if i < len(shape.default_rotations) else Quaternion()
            local_mats.append(Matrix.Translation(t) @ q.to_matrix().to_4x4())

        world_mats: list[Matrix | None] = [None] * n_nodes

        def _get_world_mat(idx: int, depth: int = 0) -> Matrix:
            if idx < 0 or idx >= n_nodes or depth > n_nodes:
                return Matrix.Identity(4)
            cached = world_mats[idx]
            if cached is not None:
                return cached
            parent_idx = int(shape.nodes[idx]["parent_index"])
            if 0 <= parent_idx < n_nodes and parent_idx != idx:
                wm = _get_world_mat(parent_idx, depth + 1) @ local_mats[idx]
            else:
                wm = local_mats[idx]
            world_mats[idx] = wm
            return wm

        for i in range(n_nodes):
            _get_world_mat(i)

        # Optionally read sibling/parent main.materials.json for loose .cdae files on disk
        cdae_dir = os.path.dirname(os.path.abspath(self.settings.filepath)) if self.settings.filepath else ""
        beamng_mat_maps: dict[str, list[str]] = {}
        if cdae_dir:
            mat_candidates = [os.path.join(cdae_dir, "main.materials.json")]
            parent_dir = os.path.dirname(cdae_dir)
            if parent_dir and parent_dir != cdae_dir:
                mat_candidates.append(os.path.join(parent_dir, "main.materials.json"))
            for mpath in mat_candidates:
                if not os.path.isfile(mpath):
                    continue
                try:
                    with open(mpath, "r", encoding="utf-8", errors="replace") as mf:
                        parsed = parse_beamng_materials_json(mf.read())
                    for k, mlist in parsed.items():
                        cur = beamng_mat_maps.setdefault(k, [])
                        for item in mlist:
                            if item not in cur:
                                cur.append(item)
                except Exception as exc:
                    self.warnings.append(f"Could not parse '{os.path.basename(mpath)}': {exc}")

        def _resolve_disk_tex(raw_ref: str) -> str | None:
            if not cdae_dir:
                return None
            pdir = os.path.dirname(cdae_dir)
            for ref in alt_image_refs(raw_ref):
                norm = ref.replace("\\", os.sep).lstrip(os.sep)
                bname = os.path.basename(norm)
                cands = [
                    os.path.join(cdae_dir, norm),
                    os.path.join(cdae_dir, bname),
                    os.path.join(cdae_dir, "textures", bname),
                ]
                if pdir and pdir != cdae_dir:
                    cands.extend((
                        os.path.join(pdir, norm),
                        os.path.join(pdir, bname),
                        os.path.join(pdir, "textures", bname),
                    ))
                for c in cands:
                    if c and os.path.isfile(c):
                        return c
            return None

        # Create/reuse materials by name
        materials: list = []
        for mat in shape.materials:
            ma = bpy.data.materials.get(mat.name)
            if ma is None:
                ma = bpy.data.materials.new(name=mat.name)
            if beamng_mat_maps:
                refs = lookup_material_color_maps(beamng_mat_maps, mat.name)
                if refs:
                    ntree = ma.node_tree
                    shader_node = (
                        next((n for n in ntree.nodes if n.type == "BSDF_PRINCIPLED"), None)
                        if ntree is not None
                        else None
                    )
                    bc_sock = shader_node.inputs.get("Base Color") if shader_node is not None else None
                    if bc_sock is None or not bc_sock.is_linked:
                        ima = None
                        for ref in refs:
                            fp = _resolve_disk_tex(ref)
                            if fp is not None:
                                try:
                                    ima = bpy.data.images.load(fp, check_existing=True)
                                    break
                                except Exception:
                                    ima = None
                        if ima is not None:
                            if ntree is None:
                                ma.use_nodes = True
                                ntree = ma.node_tree
                            if shader_node is None:
                                shader_node = next((n for n in ntree.nodes if n.type == "BSDF_PRINCIPLED"), None)
                            if shader_node is None:
                                shader_node = ntree.nodes.new("ShaderNodeBsdfPrincipled")
                                shader_node.location = (0, 300)
                                out_node = next((n for n in ntree.nodes if n.type == "OUTPUT_MATERIAL"), None)
                                if out_node is None:
                                    out_node = ntree.nodes.new("ShaderNodeOutputMaterial")
                                    out_node.location = (300, 300)
                                ntree.links.new(shader_node.outputs[0], out_node.inputs[0])
                            bc_sock = shader_node.inputs.get("Base Color")
                            if bc_sock is not None and not bc_sock.is_linked:
                                tn = ntree.nodes.new("ShaderNodeTexImage")
                                tn.label = "Base Color"
                                tn.location = (-300, 300)
                                tn.image = ima
                                ntree.links.new(tn.outputs[0], bc_sock)
            materials.append(ma)

        # Select detail levels to import
        selected_details = self._select_details(shape)

        # Build an Armature only if any selected mesh is a SkinMesh
        has_skin = any(m.mesh_type == MESH_TYPE_SKIN for m in shape.meshes)
        ob_armature = self._build_armature(shape, world_mats) if (has_skin and n_nodes > 0) else None

        # Pre-collect (obj_i, obj_det, target_coll) pairs to import so 1-to-1 node+mesh
        # pairs become a single mesh Object (matching .dae naming without .001 suffixes).
        import_jobs: list[tuple[int, int, object]] = []
        if selected_details:
            for det_idx, det in selected_details:
                det_name = _safe_name(shape.names, int(det["name_index"]), f"Detail_{det_idx}")
                sub_idx = int(det["sub_shape_num"])
                obj_det = int(det["object_detail_num"])
                if sub_idx < 0 or sub_idx >= len(shape.sub_shape_first_object):
                    continue
                first_obj = int(shape.sub_shape_first_object[sub_idx])
                num_objs = int(shape.sub_shape_num_objects[sub_idx])

                if self.settings.lod_mode == "ALL":
                    target_coll = bpy.data.collections.new(det_name)
                    self.target_collection.children.link(target_coll)
                else:
                    target_coll = self.target_collection

                for obj_i in range(first_obj, min(first_obj + num_objs, len(shape.objects))):
                    import_jobs.append((obj_i, obj_det, target_coll))
        else:
            for obj_i in range(len(shape.objects)):
                import_jobs.append((obj_i, 0, self.target_collection))

        # Count how many valid non-skin jobs attach to each node and track mesh extents
        node_job_counts: dict[int, int] = {}
        node_extents: dict[int, float] = {}
        shape_extent = _bounds_extent(shape.bounds)
        for obj_i, obj_det, _target_coll in import_jobs:
            obj = shape.objects[obj_i]
            if 0 <= obj_det < int(obj["num_meshes"]):
                mi = int(obj["start_mesh_index"]) + obj_det
                if 0 <= mi < len(shape.meshes):
                    cm = shape.meshes[mi]
                    ni = int(obj["node_index"])
                    if 0 <= ni < n_nodes:
                        ext = _bounds_extent(cm.bounds)
                        if ext <= 0.0 and len(cm.verts) > 0:
                            ext = float(np.ptp(cm.verts, axis=0).max())
                        if ext > node_extents.get(ni, 0.0):
                            node_extents[ni] = ext
                        if cm.mesh_type not in (MESH_TYPE_NULL, MESH_TYPE_SKIN):
                            node_job_counts[ni] = node_job_counts.get(ni, 0) + 1

        def _empty_display_size(node_idx: int) -> float:
            ext = node_extents.get(node_idx, 0.0) or shape_extent
            if ext > 0.0:
                return max(0.01, min(0.1, 0.02 * ext))
            return 0.05

        # Create node objects: defer creating an Empty whenever a single non-skin mesh
        # occupies this node directly with the same name (in both HIGHEST and ALL modes).
        node_objects: list = [None] * n_nodes
        direct_node_jobs: set[tuple[int, int]] = set()
        direct_node_indices: set[int] = set()
        for obj_i, obj_det, _target_coll in import_jobs:
            obj = shape.objects[obj_i]
            if not (0 <= obj_det < int(obj["num_meshes"])):
                continue
            mi = int(obj["start_mesh_index"]) + obj_det
            if not (0 <= mi < len(shape.meshes)):
                continue
            if shape.meshes[mi].mesh_type in (MESH_TYPE_NULL, MESH_TYPE_SKIN):
                continue
            ni = int(obj["node_index"])
            if 0 <= ni < n_nodes and node_job_counts.get(ni) == 1:
                node_name = _safe_name(shape.names, int(shape.nodes[ni]["name_index"]), f"Node_{ni}")
                obj_name = _safe_name(shape.names, int(obj["name_index"]), f"Object_{obj_i}")
                if obj_name == node_name:
                    direct_node_jobs.add((obj_i, obj_det))
                    direct_node_indices.add(ni)

        for i in range(n_nodes):
            # Will be filled by mesh object if claimed in direct_node_jobs
            if i in direct_node_indices:
                continue
            node_name = _safe_name(shape.names, int(shape.nodes[i]["name_index"]), f"Node_{i}")
            ob_empty = bpy.data.objects.new(name=node_name, object_data=None)
            ob_empty.empty_display_size = _empty_display_size(i)
            self.target_collection.objects.link(ob_empty)
            ob_empty.matrix_basis = local_mats[i]
            ob_empty.select_set(True)
            node_objects[i] = ob_empty

        # Build mesh objects
        deferred_children: list[tuple[object, int]] = []
        for obj_i, obj_det, target_coll in import_jobs:
            is_direct = (obj_i, obj_det) in direct_node_jobs
            ob = self._import_object_at_detail(
                shape,
                obj_i,
                obj_det,
                materials,
                node_objects,
                ob_armature,
                target_coll,
                local_mats=local_mats,
                direct_node=is_direct,
            )
            if ob is not None and not is_direct:
                obj = shape.objects[obj_i]
                mi = int(obj["start_mesh_index"]) + obj_det
                if shape.meshes[mi].mesh_type != MESH_TYPE_SKIN:
                    ni = int(obj["node_index"])
                    if 0 <= ni < n_nodes:
                        deferred_children.append((ob, ni))

        # Ensure any direct node whose mesh was empty still gets an Empty so parenting works
        for i in range(n_nodes):
            if node_objects[i] is None:
                node_name = _safe_name(shape.names, int(shape.nodes[i]["name_index"]), f"Node_{i}")
                ob_empty = bpy.data.objects.new(name=node_name, object_data=None)
                ob_empty.empty_display_size = _empty_display_size(i)
                self.target_collection.objects.link(ob_empty)
                ob_empty.matrix_basis = local_mats[i]
                ob_empty.select_set(True)
                node_objects[i] = ob_empty

        # Hook up node hierarchy parenting
        for i in range(n_nodes):
            parent_idx = int(shape.nodes[i]["parent_index"])
            if 0 <= parent_idx < n_nodes and parent_idx != i:
                node_objects[i].parent = node_objects[parent_idx]

        for ob, ni in deferred_children:
            ob.parent = node_objects[ni]

        self.view_layer.update()
        return True

    def _select_details(self, shape: CdaeShape) -> list[tuple[int, np.void]]:
        if len(shape.details) == 0:
            return []
        visible: list[tuple[int, np.void]] = []
        collision: list[tuple[int, np.void]] = []
        for i, det in enumerate(shape.details):
            sub_idx = int(det["sub_shape_num"])
            obj_det = int(det["object_detail_num"])
            if sub_idx < 0 or obj_det < 0:
                continue
            det_name = _safe_name(shape.names, int(det["name_index"]), f"Detail_{i}")
            size = float(det["size"])
            if _is_collision_detail(det_name, size):
                collision.append((i, det))
            else:
                visible.append((i, det))

        if self.settings.lod_mode == "ALL":
            out = list(visible)
            if self.settings.import_collision:
                out.extend(collision)
            return out

        # HIGHEST detail only: highest pixel size, tie-break by lowest object_detail_num
        out = []
        if visible:
            best = max(visible, key=lambda pair: (float(pair[1]["size"]), -int(pair[1]["object_detail_num"])))
            out.append(best)
        if self.settings.import_collision:
            out.extend(collision)
        return out

    def _import_object_at_detail(
        self,
        shape: CdaeShape,
        obj_i: int,
        obj_det: int,
        materials: list,
        node_objects: list,
        ob_armature,
        target_coll,
        *,
        local_mats: list[Matrix] | None = None,
        direct_node: bool = False,
    ):
        obj = shape.objects[obj_i]
        num_meshes = int(obj["num_meshes"])
        start_mesh = int(obj["start_mesh_index"])
        if obj_det < 0 or obj_det >= num_meshes:
            return None
        mesh_idx = start_mesh + obj_det
        if mesh_idx < 0 or mesh_idx >= len(shape.meshes):
            return None
        c_mesh = shape.meshes[mesh_idx]
        if c_mesh.mesh_type == MESH_TYPE_NULL:
            return None

        obj_name = _safe_name(shape.names, int(obj["name_index"]), f"Object_{obj_i}")
        me, old_to_new = self._build_blender_mesh(c_mesh, obj_name, materials)
        if me is None:
            return None

        ob = bpy.data.objects.new(name=obj_name, object_data=me)
        target_coll.objects.link(ob)
        ob.select_set(True)
        self.view_layer.objects.active = ob

        node_idx = int(obj["node_index"])
        if c_mesh.mesh_type == MESH_TYPE_SKIN and ob_armature is not None:
            ob.parent = ob_armature
            self._apply_skin_weights(shape, c_mesh, ob, ob_armature, old_to_new)
        elif direct_node and 0 <= node_idx < len(node_objects) and local_mats is not None:
            ob.matrix_basis = local_mats[node_idx]
            node_objects[node_idx] = ob
        return ob

    def _build_blender_mesh(
        self,
        c_mesh: CdaeMesh,
        mesh_name: str,
        materials: list,
    ) -> tuple[object | None, np.ndarray]:
        verts_src = c_mesh.verts
        if c_mesh.mesh_type == MESH_TYPE_SKIN and len(verts_src) == 0 and len(c_mesh.initial_verts) > 0:
            verts_src = c_mesh.initial_verts

        if len(verts_src) == 0:
            return None, np.empty(0, dtype=np.int32)

        # First frame only when num_frames > 1
        n_verts = len(verts_src)
        if c_mesh.verts_per_frame > 0 and c_mesh.verts_per_frame < n_verts:
            n_verts = c_mesh.verts_per_frame
        verts = verts_src[:n_verts].astype(np.float32, copy=True)

        if not np.isfinite(verts).all():
            verts = np.where(np.isfinite(verts), verts, np.float32(0.0))
            self.warnings.append(f"Mesh '{mesh_name}': replaced non-finite vertex positions with 0.0")

        tris, tri_mats = _primitives_to_triangles(c_mesh, n_verts, self.warnings, mesh_name)
        if len(tris) == 0:
            return None, np.empty(0, dtype=np.int32)

        # Extract per-corner (loop) attributes BEFORE merging vertices so UV/color/normal seams survive
        flat_orig_idx = tris.ravel()

        uv0_loops: np.ndarray | None = None
        if len(c_mesh.tverts) >= n_verts:
            uv0 = c_mesh.tverts[:n_verts].astype(np.float32, copy=True)
            if not np.isfinite(uv0).all():
                uv0 = np.where(np.isfinite(uv0), uv0, np.float32(0.0))
            uv0[:, 1] = 1.0 - uv0[:, 1]  # Torque3D top-left V -> Blender bottom-left V
            uv0_loops = uv0[flat_orig_idx]

        uv1_loops: np.ndarray | None = None
        if len(c_mesh.tverts2) >= n_verts:
            uv1 = c_mesh.tverts2[:n_verts].astype(np.float32, copy=True)
            if not np.isfinite(uv1).all():
                uv1 = np.where(np.isfinite(uv1), uv1, np.float32(0.0))
            uv1[:, 1] = 1.0 - uv1[:, 1]
            uv1_loops = uv1[flat_orig_idx]

        col_loops: np.ndarray | None = None
        if len(c_mesh.colors) >= n_verts:
            cols = c_mesh.colors[:n_verts].astype(np.float32) * (1.0 / 255.0)
            col_loops = cols[flat_orig_idx]

        norm_src = c_mesh.norms
        if c_mesh.mesh_type == MESH_TYPE_SKIN and len(norm_src) == 0 and len(c_mesh.initial_norms) > 0:
            norm_src = c_mesh.initial_norms

        norm_loops: np.ndarray | None = None
        if len(norm_src) >= n_verts:
            norms = norm_src[:n_verts].astype(np.float32, copy=True)
            if not np.isfinite(norms).all():
                norms = np.where(np.isfinite(norms), norms, np.float32(0.0))
            norm_loops = norms[flat_orig_idx]
        elif len(c_mesh.encoded_norms) >= n_verts:
            norm_loops = U8_TO_NORMAL_TABLE[c_mesh.encoded_norms[:n_verts]][flat_orig_idx]

        # Merge vertices by position within this mesh unless UNMERGED mode is selected
        do_merge = self.settings.normals_mode != "UNMERGED"
        if do_merge and self.settings.merge_distance > 0.0:
            n_before_merge = len(tris)
            verts, tris, kept, old_to_new = _merge_verts_by_distance(verts, tris, self.settings.merge_distance)
            n_degen = n_before_merge - len(tris)
            if n_degen > 0:
                merge_mm = self.settings.merge_distance * 1000.0
                self.infos.append(
                    f"Mesh '{mesh_name}': {n_degen} tiny faces (under {merge_mm:g} mm) merged away"
                )
            if not kept.all():
                tri_mats = tri_mats[kept]
                kept_loops = np.repeat(kept, 3)
                if uv0_loops is not None:
                    uv0_loops = uv0_loops[kept_loops]
                if uv1_loops is not None:
                    uv1_loops = uv1_loops[kept_loops]
                if col_loops is not None:
                    col_loops = col_loops[kept_loops]
                if norm_loops is not None:
                    norm_loops = norm_loops[kept_loops]
        else:
            old_to_new = np.arange(len(verts), dtype=np.int32)

        # Classify duplicate faces sharing identical vertices: keep opposite-winding double-sided
        # faces and differing-material/attribute overlay layers unwelded; drop only truly identical
        # duplicate junk before me.validate().
        (
            verts,
            tris,
            dup_kept,
            n_double_sided,
            n_layered,
            n_exact_dups,
            _extra_src,
        ) = _resolve_duplicate_faces(verts, tris, tri_mats, uv0_loops, uv1_loops, col_loops, norm_loops)
        if n_double_sided > 0 or n_layered > 0 or n_exact_dups > 0:
            self.infos.append(
                f"Mesh '{mesh_name}': {n_double_sided} double-sided and {n_layered} layered faces "
                f"kept unwelded, {n_exact_dups} exact duplicates removed"
            )
            if n_exact_dups > 0:
                tri_mats = tri_mats[dup_kept]
                kept_loops = np.repeat(dup_kept, 3)
                if uv0_loops is not None:
                    uv0_loops = uv0_loops[kept_loops]
                if uv1_loops is not None:
                    uv1_loops = uv1_loops[kept_loops]
                if col_loops is not None:
                    col_loops = col_loops[kept_loops]
                if norm_loops is not None:
                    norm_loops = norm_loops[kept_loops]

        total_faces = len(tris)
        if total_faces == 0:
            return None, old_to_new
        total_loops = total_faces * 3

        me = bpy.data.meshes.new(name=mesh_name)
        me.vertices.add(len(verts))
        me.vertices.foreach_set("co", verts.ravel())
        me.loops.add(total_loops)
        me.loops.foreach_set("vertex_index", tris.ravel())
        me.polygons.add(total_faces)
        me.polygons.foreach_set("loop_start", np.arange(0, total_loops, 3, dtype=np.int32))
        me.polygons.foreach_set("loop_total", np.full(total_faces, 3, dtype=np.int32))
        me.update(calc_edges=True)

        if me.validate(clean_customdata=False):
            self.warnings.append(f"Mesh '{mesh_name}' had invalid data and was repaired")

        cur_n_loops = len(me.loops)
        cur_n_polys = len(me.polygons)

        # Assign materials referenced by primitives
        used_mat_indices = sorted({int(m) for m in np.unique(tri_mats) if 0 <= int(m) < len(materials)})
        mat_remap: dict[int, int] = {}
        for slot_i, src_m in enumerate(used_mat_indices):
            me.materials.append(materials[src_m])
            mat_remap[src_m] = slot_i
        if used_mat_indices and cur_n_polys == total_faces:
            slot_arr = np.zeros(total_faces, dtype=np.int32)
            for src_m, slot_i in mat_remap.items():
                slot_arr[tri_mats == src_m] = slot_i
            me.polygons.foreach_set("material_index", slot_arr)

        loops_match = (cur_n_loops == total_loops)
        if not loops_match and (
            uv0_loops is not None or uv1_loops is not None or col_loops is not None or norm_loops is not None
        ):
            self.warnings.append(
                f"Mesh '{mesh_name}': skipped per-loop UVs/colors/normals after mesh repair "
                f"(loop count changed from {total_loops} to {cur_n_loops})"
            )

        if loops_match:
            if uv0_loops is not None:
                uv_layer = me.uv_layers.new(name="UVMap")
                uv_layer.data.foreach_set("uv", uv0_loops.ravel())
            if uv1_loops is not None:
                uv_layer2 = me.uv_layers.new(name="UVMap.001")
                uv_layer2.data.foreach_set("uv", uv1_loops.ravel())
            if col_loops is not None:
                cattr = me.color_attributes.new(name="Color", type="BYTE_COLOR", domain="CORNER")
                cattr.data.foreach_set("color_srgb", col_loops.ravel())
            if norm_loops is not None and len(norm_loops) == cur_n_loops:
                if cur_n_polys > 0:
                    me.polygons.foreach_set("use_smooth", np.ones(cur_n_polys, dtype=bool))
                mode = self.settings.normals_mode
                if mode in ("EXACT", "SHARP"):
                    # Mark sharp edges wherever corner normals differ across an edge.
                    # In EXACT mode this keeps hard edges ready if custom normals are cleared later.
                    DocumentImporter._apply_sharp_edges_from_normals(
                        me, norm_loops, self.settings.sharp_angle_threshold
                    )
                if mode in ("EXACT", "UNMERGED"):
                    try:
                        cn = me.attributes.get("custom_normal")
                        if cn is not None and (cn.data_type != "FLOAT_VECTOR" or cn.domain != "CORNER"):
                            me.attributes.remove(cn)
                            cn = None
                        if cn is None:
                            cn = me.attributes.new(name="custom_normal", type="FLOAT_VECTOR", domain="CORNER")
                        cn.data.foreach_set("vector", norm_loops.ravel())
                    except Exception as exc:
                        self.warnings.append(f"Fast exact normals failed on '{mesh_name}', falling back: {exc}")
                        me.normals_split_custom_set(norm_loops.tolist())

        me.update()
        return me, old_to_new

    def _build_armature(self, shape: CdaeShape, world_mats: list[Matrix | None]):
        arm_data = bpy.data.armatures.new(name="Armature")
        ob_arm = bpy.data.objects.new(name="Armature", object_data=arm_data)
        self.target_collection.objects.link(ob_arm)
        self.view_layer.objects.active = ob_arm
        ob_arm.select_set(True)

        # Build world bind-pose matrices from SkinMesh initialTransforms (row-major inverse bind matrices)
        n_nodes = len(shape.nodes)
        node_bind_mats: dict[int, Matrix] = {}
        for c_mesh in shape.meshes:
            if c_mesh.mesh_type != MESH_TYPE_SKIN:
                continue
            n_xforms = len(c_mesh.initial_transforms)
            for bi, ni in enumerate(c_mesh.node_index.tolist()):
                if 0 <= ni < n_nodes and bi < n_xforms and ni not in node_bind_mats:
                    raw_m = c_mesh.initial_transforms[bi]
                    if np.isfinite(raw_m).all():
                        inv_bind = Matrix(raw_m.tolist())
                        if abs(inv_bind.determinant()) > 1e-10:
                            node_bind_mats[ni] = inv_bind.inverted()

        bpy.ops.object.mode_set(mode="EDIT")
        edit_bones: list = []
        for i in range(n_nodes):
            bname = _safe_name(shape.names, int(shape.nodes[i]["name_index"]), f"Bone_{i}")
            eb = arm_data.edit_bones.new(bname)
            mat = node_bind_mats.get(i) or world_mats[i] or Matrix.Identity(4)
            head = mat.translation.copy()
            try:
                rot3 = mat.to_3x3().normalized()
                axis, roll = bpy.types.Bone.AxisRollFromMatrix(rot3)
            except Exception:
                axis, roll = Vector((0.0, 1.0, 0.0)), 0.0
            if axis.length < 1e-4:
                axis = Vector((0.0, 1.0, 0.0))
            eb.head = head
            eb.tail = head + axis.normalized() * 0.2
            eb.roll = roll
            edit_bones.append(eb)

        for i in range(n_nodes):
            parent_idx = int(shape.nodes[i]["parent_index"])
            if 0 <= parent_idx < n_nodes and parent_idx != i:
                edit_bones[i].parent = edit_bones[parent_idx]

        bpy.ops.object.mode_set(mode="OBJECT")
        return ob_arm

    def _apply_skin_weights(
        self,
        shape: CdaeShape,
        c_mesh: CdaeMesh,
        ob_mesh,
        ob_armature,
        old_to_new: np.ndarray,
    ) -> None:
        mod = ob_mesh.modifiers.new(name="Armature", type="ARMATURE")
        mod.object = ob_armature
        mod.use_vertex_groups = True

        n_old = len(old_to_new)
        n_new = len(ob_mesh.data.vertices)
        node_map = c_mesh.node_index
        v_idx = c_mesh.vertex_index
        b_idx = c_mesh.bone_index
        wts = c_mesh.weight
        count = min(len(v_idx), len(b_idx), len(wts))
        if count == 0 or len(node_map) == 0:
            return

        # Pre-create vertex groups indexed by bone_index
        vgroups: list = []
        n_nodes = len(shape.nodes)
        for bi, ni in enumerate(node_map.tolist()):
            if 0 <= ni < n_nodes:
                gname = _safe_name(shape.names, int(shape.nodes[ni]["name_index"]), f"Bone_{ni}")
            else:
                gname = f"Bone_{bi}"
            vg = ob_mesh.vertex_groups.get(gname) or ob_mesh.vertex_groups.new(name=gname)
            vgroups.append(vg)

        # Accumulate weights per (bone, merged_vert) so duplicate split verts don't overwrite each other
        for vi, bi, w in zip(v_idx[:count].tolist(), b_idx[:count].tolist(), wts[:count].tolist()):
            if w <= 0.0 or not math.isfinite(w):
                continue
            if bi < 0 or bi >= len(vgroups) or vi < 0 or vi >= n_old:
                continue
            new_vi = int(old_to_new[vi])
            if 0 <= new_vi < n_new:
                vgroups[bi].add([new_vi], float(w), "REPLACE")
