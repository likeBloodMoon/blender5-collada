# SPDX-FileCopyrightText: 2012 GarageGames, LLC (Torque3D, MIT)
# SPDX-FileCopyrightText: 2026 Blender5-Collada Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Experimental BeamNG Cached Collada (.cdae v31) exporter (StandardMesh only).

Builds a Torque3D/BeamNG v31 TSShape binary matching verified real .cdae files:
  - Raw little-endian <II header (version=31 | (124 << 16), headerSize)
  - MsgPack header dict with info, compression, bodysize, objectNames
  - Sequential MsgPack body (optionally Zstandard-compressed)
  - GPU-split vertices per unique (pos, normal, uv0, uv1, color), flipped UV V,
    sRGB ColorI bytes, MikkTSpace tangents, Quat16 conjugate node rotations, and
    per-material TSDrawPrimitive triangle lists.
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass

import bpy
import numpy as np
from mathutils import Matrix, Quaternion

from ..collada_utils import get_mesh_copy
from .msgpack_lite import MsgPackWriter
from .reader import (
    DTYPE_DETAIL,
    DTYPE_NODE,
    DTYPE_OBJECT,
    DTYPE_OBJECT_STATE,
    DTYPE_PRIMITIVE,
    MESH_TYPE_STANDARD,
    PRIM_INDEXED,
    PRIM_NO_MATERIAL,
    PRIM_TYPE_TRIANGLES,
    SUPPORTED_CDAE_VERSION,
)

EXPORTER_VERSION = 124
_HEADER_INFO_BANNER = (
    "\r\n\r\n\r\nWelcome! This is a binary file :D\r\n"
    "Please read the docs at https://go.beamng.com/shapeMessagepackFileformat\r\n\r\n\r\n"
)
_NO_MAP = 0xFFFFFFFF
_DEFAULT_MAT_FLAGS = 3  # S_Wrap | T_Wrap


def zstd_available() -> bool:
    try:
        import zstandard  # noqa: F401
        return True
    except ImportError:
        return False


@dataclass
class CdaeExportSettings:
    filepath: str = ""
    use_selection: bool = False
    apply_modifiers: bool = True
    compress: bool = False


def _blender_quat_to_quat16(q: Quaternion) -> tuple[int, int, int, int]:
    """Convert Blender Quaternion(w, x, y, z) to Torque3D conjugate Quat16 (x, y, z, w int16)."""
    qn = q.normalized()
    # Torque3D QuatF is the conjugate of Blender's quaternion convention
    vals = (-qn.x, -qn.y, -qn.z, qn.w)
    return tuple(max(-32767, min(32767, int(round(v * 32767.0)))) for v in vals)  # type: ignore[return-value]


def _pack_vec(w: MsgPackWriter, count: int, elem_size: int, raw: bytes) -> None:
    w.write_int(count)
    w.write_int(elem_size)
    w.write_bin(raw)


def _pack_point3f(w: MsgPackWriter, pt: tuple[float, float, float]) -> None:
    w.write_array_header(3)
    w.write_float32(pt[0])
    w.write_float32(pt[1])
    w.write_float32(pt[2])


def _pack_box3f(w: MsgPackWriter, box: tuple[float, float, float, float, float, float]) -> None:
    w.write_array_header(6)
    for v in box:
        w.write_float32(v)


def _build_gpu_mesh_data(
    me,
    scale_mat: Matrix | None,
    global_mat_indices: dict[str, int],
    material_list: list[str],
) -> dict | None:
    """Extract triangulated GPU-split vertex buffers, tangents, and TSDrawPrimitives from a Blender Mesh."""
    n_polys = len(me.polygons)
    if n_polys == 0:
        return None

    me.calc_loop_triangles()
    n_tris = len(me.loop_triangles)
    if n_tris == 0:
        return None

    n_verts = len(me.vertices)
    n_loops = len(me.loops)

    co = np.empty(n_verts * 3, dtype=np.float32)
    me.vertices.foreach_get("co", co)
    co = co.reshape(n_verts, 3)

    # Corner normals (respects custom normals, sharp edges, flat/smooth faces)
    lnorms = np.empty(n_loops * 3, dtype=np.float32)
    me.corner_normals.foreach_get("vector", lnorms)
    lnorms = lnorms.reshape(n_loops, 3)

    if scale_mat is not None:
        s_np = np.array(scale_mat.to_3x3(), dtype=np.float64)
        co = (co.astype(np.float64) @ s_np.T).astype(np.float32)
        n_mat = np.array(scale_mat.to_3x3().inverted_safe().transposed(), dtype=np.float64)
        ln = lnorms.astype(np.float64) @ n_mat.T
        nlen = np.linalg.norm(ln, axis=1, keepdims=True)
        lnorms = np.where(nlen > 1e-8, ln / np.maximum(nlen, 1e-8), ln).astype(np.float32)

    tri_loops = np.empty(n_tris * 3, dtype=np.int32)
    me.loop_triangles.foreach_get("loops", tri_loops)
    tri_verts = np.empty(n_tris * 3, dtype=np.int32)
    me.loop_triangles.foreach_get("vertices", tri_verts)
    tri_mat_slots = np.empty(n_tris, dtype=np.int32)
    me.loop_triangles.foreach_get("material_index", tri_mat_slots)

    # Map local material slots to global shape material list
    slot_to_global: dict[int, int] = {}
    for slot_i, ma in enumerate(me.materials):
        if ma is not None:
            mname = ma.name
            if mname not in global_mat_indices:
                global_mat_indices[mname] = len(material_list)
                material_list.append(mname)
            slot_to_global[slot_i] = global_mat_indices[mname]

    # UV0 & UV1 (flip V back to Torque3D top-left convention: 1.0 - v)
    has_uv0 = len(me.uv_layers) > 0
    has_uv1 = len(me.uv_layers) > 1
    uv0_loops = np.zeros((n_loops, 2), dtype=np.float32)
    uv1_loops: np.ndarray | None = None
    tangents_loops = np.zeros((n_loops, 4), dtype=np.float32)
    tangents_loops[:, 0] = 1.0
    tangents_loops[:, 3] = 1.0

    if has_uv0:
        uv0_layer = me.uv_layers[0]
        uv0_raw = np.empty(n_loops * 2, dtype=np.float32)
        uv0_layer.data.foreach_get("uv", uv0_raw)
        uv0_loops = uv0_raw.reshape(n_loops, 2)
        uv0_loops[:, 1] = 1.0 - uv0_loops[:, 1]
        try:
            me.calc_tangents(uvmap=uv0_layer.name)
            t_vec = np.empty(n_loops * 3, dtype=np.float32)
            t_sign = np.empty(n_loops, dtype=np.float32)
            me.loops.foreach_get("tangent", t_vec)
            me.loops.foreach_get("bitangent_sign", t_sign)
            tangents_loops[:, :3] = t_vec.reshape(n_loops, 3)
            tangents_loops[:, 3] = t_sign
            me.free_tangents()
        except Exception:
            pass

    if has_uv1:
        uv1_raw = np.empty(n_loops * 2, dtype=np.float32)
        me.uv_layers[1].data.foreach_get("uv", uv1_raw)
        uv1_loops = uv1_raw.reshape(n_loops, 2)
        uv1_loops[:, 1] = 1.0 - uv1_loops[:, 1]

    # Vertex colors (sRGB uint8 bytes matching Blender BYTE_COLOR color_srgb)
    col_loops: np.ndarray | None = None
    if len(me.color_attributes) > 0:
        ca = me.color_attributes[0]
        if ca.domain == "CORNER":
            c_raw = np.empty(n_loops * 4, dtype=np.float32)
            ca.data.foreach_get("color_srgb", c_raw)
            col_loops = np.clip(np.round(c_raw.reshape(n_loops, 4) * 255.0), 0, 255).astype(np.uint8)
        elif ca.domain == "POINT":
            c_raw = np.empty(n_verts * 4, dtype=np.float32)
            ca.data.foreach_get("color_srgb", c_raw)
            c_u8 = np.clip(np.round(c_raw.reshape(n_verts, 4) * 255.0), 0, 255).astype(np.uint8)
            lv = np.empty(n_loops, dtype=np.int32)
            me.loops.foreach_get("vertex_index", lv)
            col_loops = c_u8[lv]

    # Gather per-triangle-corner arrays (length n_tris * 3)
    c_pos = co[tri_verts]
    c_norm = lnorms[tri_loops]
    c_uv0 = uv0_loops[tri_loops]
    c_tan = tangents_loops[tri_loops]
    c_uv1 = uv1_loops[tri_loops] if uv1_loops is not None else None
    c_col = col_loops[tri_loops] if col_loops is not None else None

    # Exact float32 bit-level GPU vertex deduplication on (pos, normal, uv0, uv1, color),
    # matching Torque3D's ColladaAppMesh VertTupleMap equality (canonicalize -0.0 -> +0.0).
    def _canon_f32(arr: np.ndarray) -> np.ndarray:
        out = np.ascontiguousarray(arr, dtype="<f4")
        if (out == 0.0).any():
            out = out.copy()
            out[out == 0.0] = 0.0
        return out

    n_corners = n_tris * 3
    p_c = _canon_f32(c_pos)
    n_c = _canon_f32(c_norm)
    u0_c = _canon_f32(c_uv0)
    u1_c = _canon_f32(c_uv1) if c_uv1 is not None else None
    col_c = np.ascontiguousarray(c_col, dtype="<u1") if c_col is not None else None

    key_parts = [
        p_c.view(np.uint8).reshape(n_corners, 12),
        n_c.view(np.uint8).reshape(n_corners, 12),
        u0_c.view(np.uint8).reshape(n_corners, 8),
    ]
    if u1_c is not None:
        key_parts.append(u1_c.view(np.uint8).reshape(n_corners, 8))
    if col_c is not None:
        key_parts.append(col_c.reshape(n_corners, 4))

    key_bytes = np.ascontiguousarray(np.hstack(key_parts))
    row_width = key_bytes.shape[1]
    keys = key_bytes.view(np.dtype((np.void, row_width))).ravel()

    _, uniq_idx, inverse = np.unique(keys, return_index=True, return_inverse=True)
    # Preserve original appearance order for deterministic output
    order = np.argsort(uniq_idx)
    inv_order = np.empty_like(order)
    inv_order[order] = np.arange(len(order))
    keep_corners = uniq_idx[order]
    corner_to_gpu = inv_order[inverse].astype(np.uint32)

    gpu_verts = np.ascontiguousarray(c_pos[keep_corners], dtype="<f4")
    gpu_norms = np.ascontiguousarray(c_norm[keep_corners], dtype="<f4")
    gpu_uv0 = np.ascontiguousarray(c_uv0[keep_corners], dtype="<f4")
    gpu_tan = np.ascontiguousarray(c_tan[keep_corners], dtype="<f4")
    gpu_uv1 = np.ascontiguousarray(c_uv1[keep_corners], dtype="<f4") if c_uv1 is not None else np.empty((0, 2), dtype="<f4")
    gpu_col = np.ascontiguousarray(c_col[keep_corners], dtype="<u1") if c_col is not None else np.empty((0, 4), dtype="<u1")

    # Group triangles by material slot into TSDrawPrimitives.
    # Flip Blender CCW front-face winding to Torque3D / BeamNG CW winding (colladaAppMesh.cpp:609-614).
    tri_gpu = np.ascontiguousarray(corner_to_gpu.reshape(n_tris, 3)[:, ::-1])
    unique_slots = np.unique(tri_mat_slots)
    prim_rows: list[tuple[int, int, int]] = []
    idx_chunks: list[np.ndarray] = []
    cur_start = 0

    for slot_val in unique_slots.tolist():
        mask = (tri_mat_slots == slot_val)
        slot_tris = tri_gpu[mask].ravel()
        if len(slot_tris) == 0:
            continue
        g_mat = slot_to_global.get(slot_val, -1)
        if g_mat >= 0:
            mat_bits = PRIM_TYPE_TRIANGLES | PRIM_INDEXED | g_mat
        else:
            mat_bits = PRIM_TYPE_TRIANGLES | PRIM_INDEXED | PRIM_NO_MATERIAL
        # Pack as signed int32 matching DTYPE_PRIMITIVE
        mat_i32 = struct.unpack("<i", struct.pack("<I", mat_bits & 0xFFFFFFFF))[0]
        prim_rows.append((cur_start, len(slot_tris), mat_i32))
        idx_chunks.append(slot_tris)
        cur_start += len(slot_tris)

    all_indices = np.concatenate(idx_chunks) if len(idx_chunks) > 1 else (idx_chunks[0] if idx_chunks else np.empty(0, dtype="<u4"))
    prims_arr = np.array(prim_rows, dtype=DTYPE_PRIMITIVE)

    bmin = gpu_verts.min(axis=0)
    bmax = gpu_verts.max(axis=0)
    center = (bmin + bmax) * 0.5
    radius = float(np.linalg.norm(gpu_verts - center, axis=1).max()) if len(gpu_verts) > 0 else 0.0

    return {
        "verts": gpu_verts,
        "tverts": gpu_uv0,
        "tverts2": gpu_uv1,
        "colors": gpu_col,
        "norms": gpu_norms,
        "tangents": gpu_tan,
        "primitives": prims_arr,
        "indices": np.ascontiguousarray(all_indices, dtype="<u4"),
        "bounds": (float(bmin[0]), float(bmin[1]), float(bmin[2]), float(bmax[0]), float(bmax[1]), float(bmax[2])),
        "center": (float(center[0]), float(center[1]), float(center[2])),
        "radius": radius,
        "num_tris": n_tris,
    }


class CdaeExporter:
    """Exports Blender scene mesh objects to BeamNG .cdae v31 (StandardMesh only)."""

    def __init__(self, context, settings: CdaeExportSettings) -> None:
        self.context = context
        self.scene = context.scene
        self.settings = settings
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def export_document(self) -> bool:
        if self.settings.use_selection:
            mesh_obs = [ob for ob in self.context.selected_objects if ob.type == "MESH"]
        else:
            mesh_obs = [ob for ob in self.scene.objects if ob.type == "MESH"]

        if not mesh_obs:
            self.errors.append("No mesh objects to export")
            return False

        # Collect ancestor hierarchy nodes (Empty / Mesh parents)
        node_obs_set = set(mesh_obs)
        for ob in list(mesh_obs):
            p = ob.parent
            while p is not None and p not in node_obs_set:
                if p.type in ("MESH", "EMPTY"):
                    node_obs_set.add(p)
                p = p.parent

        # Deterministic topological order (parents before children, alphabetical tie-break)
        def _depth(ob) -> int:
            d = 0
            p = ob.parent
            while p is not None and p in node_obs_set:
                d += 1
                p = p.parent
            return d

        node_obs = sorted(node_obs_set, key=lambda o: (_depth(o), o.name))
        ob_to_node_idx = {ob: i for i, ob in enumerate(node_obs)}

        names: list[str] = []
        name_to_idx: dict[str, int] = {}

        def _add_name(nm: str) -> int:
            idx = name_to_idx.get(nm)
            if idx is None:
                idx = len(names)
                names.append(nm)
                name_to_idx[nm] = idx
            return idx

        # Build nodes, default rotations/translations
        n_nodes = len(node_obs)
        nodes_arr = np.empty(n_nodes, dtype=DTYPE_NODE)
        def_rots = np.empty((n_nodes, 4), dtype="<i2")
        def_trans = np.empty((n_nodes, 3), dtype="<f4")
        node_scales: dict[int, Matrix | None] = {}

        # Pre-compute parent/child/sibling links
        children_by_parent: dict[int, list[int]] = {}
        for i, ob in enumerate(node_obs):
            p_idx = ob_to_node_idx[ob.parent] if (ob.parent in ob_to_node_idx) else -1
            children_by_parent.setdefault(p_idx, []).append(i)

        first_child = [-1] * n_nodes
        next_sibling = [-1] * n_nodes
        for p_idx, ch_list in children_by_parent.items():
            if p_idx >= 0 and ch_list:
                first_child[p_idx] = ch_list[0]
            for k in range(len(ch_list) - 1):
                next_sibling[ch_list[k]] = ch_list[k + 1]

        for i, ob in enumerate(node_obs):
            p_idx = ob_to_node_idx[ob.parent] if (ob.parent in ob_to_node_idx) else -1
            mat = ob.matrix_local if p_idx >= 0 else ob.matrix_world
            loc, rot, scl = mat.decompose()
            def_trans[i] = (loc.x, loc.y, loc.z)
            def_rots[i] = _blender_quat_to_quat16(rot)
            if any(abs(s - 1.0) > 1e-5 for s in scl):
                node_scales[i] = Matrix.Diagonal((scl.x, scl.y, scl.z, 1.0))
            else:
                node_scales[i] = None

            nodes_arr[i] = (
                _add_name(ob.name),
                p_idx,
                -1,  # first_object filled below
                first_child[i],
                next_sibling[i],
            )

        # Build meshes & objects
        depsgraph = self.context.evaluated_depsgraph_get()
        global_mat_indices: dict[str, int] = {}
        material_list: list[str] = []
        exported_meshes: list[dict] = []
        obj_rows: list[tuple[int, int, int, int, int, int]] = []
        obj_names: list[str] = []
        world_mins: list[np.ndarray] = []
        world_maxs: list[np.ndarray] = []
        total_tris = 0

        for ob in node_obs:
            if ob.type != "MESH" or ob not in mesh_obs:
                continue
            ni = ob_to_node_idx[ob]
            needs_tri = self.settings.apply_modifiers or any(p.loop_total > 3 for p in ob.data.polygons)
            me_copy, should_remove = get_mesh_copy(
                depsgraph,
                ob,
                apply_modifiers=self.settings.apply_modifiers,
                triangulate=needs_tri,
            )
            try:
                mdata = _build_gpu_mesh_data(me_copy, node_scales[ni], global_mat_indices, material_list)
            finally:
                if should_remove:
                    bpy.data.meshes.remove(me_copy)

            if mdata is None:
                continue

            mesh_idx = len(exported_meshes)
            obj_idx = len(obj_rows)
            exported_meshes.append(mdata)
            obj_names.append(ob.name)
            total_tris += mdata["num_tris"]

            # Link object onto its node
            prev_first = int(nodes_arr[ni]["first_object"])
            nodes_arr[ni]["first_object"] = obj_idx
            obj_rows.append((_add_name(ob.name), 1, mesh_idx, ni, prev_first, -1))

            # Track world-space shape bounds
            mw = np.array(ob.matrix_world, dtype=np.float64)
            # Undo local scale if already baked into verts
            if node_scales[ni] is not None:
                loc_w, rot_w, _ = ob.matrix_world.decompose()
                mw = np.array(Matrix.Translation(loc_w) @ rot_w.to_matrix().to_4x4(), dtype=np.float64)
            w_pts = (mdata["verts"].astype(np.float64) @ mw[:3, :3].T) + mw[:3, 3]
            world_mins.append(w_pts.min(axis=0))
            world_maxs.append(w_pts.max(axis=0))

        if not exported_meshes:
            self.errors.append("No non-empty mesh geometry to export")
            return False

        detail_name_idx = _add_name("detail2")
        s_bmin = np.min(np.vstack(world_mins), axis=0)
        s_bmax = np.max(np.vstack(world_maxs), axis=0)
        s_center = (s_bmin + s_bmax) * 0.5
        s_radius = float(np.linalg.norm(s_bmax - s_bmin) * 0.5)
        s_bounds = (
            float(s_bmin[0]), float(s_bmin[1]), float(s_bmin[2]),
            float(s_bmax[0]), float(s_bmax[1]), float(s_bmax[2]),
        )

        objects_arr = np.array(obj_rows, dtype=DTYPE_OBJECT)
        obj_states_arr = np.array([(1.0, 0, 0)] * len(obj_rows), dtype=DTYPE_OBJECT_STATE)
        details_arr = np.array(
            [(detail_name_idx, 0, 0, 2.0, -1.0, -1.0, total_tris, 0, 0, 0, 0, 0.0, 0)],
            dtype=DTYPE_DETAIL,
        )

        # Serialize MsgPack body
        w = MsgPackWriter()
        w.write_float32(2.0)  # mSmallestVisibleSize
        w.write_int(0)        # mSmallestVisibleDL
        w.write_float32(s_radius)
        w.write_float32(s_radius)  # tubeRadius
        _pack_point3f(w, (float(s_center[0]), float(s_center[1]), float(s_center[2])))
        _pack_box3f(w, s_bounds)

        # 19 shape pack_vectors
        _pack_vec(w, len(nodes_arr), 20, nodes_arr.tobytes())
        _pack_vec(w, len(objects_arr), 24, objects_arr.tobytes())
        _pack_vec(w, 1, 4, struct.pack("<i", 0))
        _pack_vec(w, 1, 4, struct.pack("<i", 0))
        _pack_vec(w, 1, 4, struct.pack("<i", len(nodes_arr)))
        _pack_vec(w, 1, 4, struct.pack("<i", len(objects_arr)))
        _pack_vec(w, len(def_rots), 8, def_rots.tobytes())
        _pack_vec(w, len(def_trans), 12, def_trans.tobytes())
        # Empty animation vectors
        _pack_vec(w, 0, 8, b"")
        _pack_vec(w, 0, 12, b"")
        _pack_vec(w, 0, 4, b"")
        _pack_vec(w, 0, 12, b"")
        _pack_vec(w, 0, 12, b"")
        _pack_vec(w, 0, 8, b"")
        _pack_vec(w, 0, 12, b"")
        _pack_vec(w, 0, 8, b"")
        _pack_vec(w, len(obj_states_arr), 12, obj_states_arr.tobytes())
        _pack_vec(w, 0, 8, b"")  # triggers
        _pack_vec(w, len(details_arr), 52, details_arr.tobytes())

        # Names
        w.write_int(len(names))
        for nm in names:
            w.write_str(nm)

        # Meshes
        w.write_int(len(exported_meshes))
        for m in exported_meshes:
            w.write_int(MESH_TYPE_STANDARD)
            w.write_int(1)   # numFrames
            w.write_int(1)   # numMatFrames
            w.write_int(-1)  # parentMesh
            _pack_box3f(w, m["bounds"])
            _pack_point3f(w, m["center"])
            w.write_float32(m["radius"])
            _pack_vec(w, len(m["verts"]), 12, m["verts"].tobytes())
            _pack_vec(w, len(m["tverts"]), 8, m["tverts"].tobytes())
            _pack_vec(w, len(m["tverts2"]), 8, m["tverts2"].tobytes())
            _pack_vec(w, len(m["colors"]), 4, m["colors"].tobytes())
            _pack_vec(w, len(m["norms"]), 12, m["norms"].tobytes())
            _pack_vec(w, 0, 1, b"")  # encodedNorms (unused in real BeamNG v31 files)
            _pack_vec(w, len(m["primitives"]), 12, m["primitives"].tobytes())
            _pack_vec(w, len(m["indices"]), 4, m["indices"].tobytes())
            _pack_vec(w, len(m["tangents"]), 16, m["tangents"].tobytes())
            w.write_int(len(m["verts"]))  # vertsPerFrame
            w.write_int(0)                # meshFlags

        # Sequences (0)
        w.write_int(0)

        # Material list
        w.write_int(len(material_list))
        for mname in material_list:
            w.write_str(mname)
            w.write_int(_DEFAULT_MAT_FLAGS)
            w.write_int(0)        # reflectMap
            w.write_int(_NO_MAP)  # bumpMap
            w.write_int(_NO_MAP)  # detailMap
            w.write_float32(1.0)  # detailScale
            w.write_float32(1.0)  # reflectionAmount

        body_raw = w.getvalue()
        use_compression = False
        if self.settings.compress:
            try:
                import zstandard as zstd  # type: ignore[import-not-found]
                body_stored = zstd.ZstdCompressor(level=3).compress(body_raw)
                use_compression = True
            except ImportError:
                self.warnings.append("zstandard is unavailable; writing uncompressed .cdae")
                body_stored = body_raw
        else:
            body_stored = body_raw

        hw = MsgPackWriter()
        hw.write(
            {
                "info": _HEADER_INFO_BANNER,
                "compression": use_compression,
                "bodysize": len(body_stored),
                "objectNames": obj_names,
            }
        )
        header_bytes = hw.getvalue()
        version_val = (SUPPORTED_CDAE_VERSION & 0xFF) | ((EXPORTER_VERSION & 0xFFFF) << 16)
        file_bytes = struct.pack("<II", version_val, len(header_bytes)) + header_bytes + body_stored

        try:
            with open(self.settings.filepath, "wb") as f:
                f.write(file_bytes)
        except OSError as exc:
            self.errors.append(f"Failed to write .cdae file: {exc}")
            return False

        return True
