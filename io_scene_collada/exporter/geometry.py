# SPDX-FileCopyrightText: 2009-2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Geometry export, float-row deduplication, normals, XML sources, and primitive lists."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..collada_utils import (
    KEEP_NORMALS_ATTR,
    get_geometry_id,
    get_material_id,
    get_mesh_copy,
)
from ..xml_writer import (
    encode_xml,
    fmt_f32_array,
    fmt_int,
    fmt_int_array,
)

if TYPE_CHECKING:
    from bpy.types import Mesh, Object

def _dedup_float_rows(arr_2d):
    """Exact float32 bit-level row deduplication preserving first-seen order."""
    import numpy as np

    n, stride = arr_2d.shape
    if n == 0:
        return arr_2d, np.empty(0, dtype=np.int32)
    a = np.ascontiguousarray(arr_2d, dtype="<f4")
    if (a == 0.0).any():
        a = a.copy()
        a[a == 0.0] = 0.0
    if n <= 256:
        if stride == 2:
            keys = a.view(np.uint64).ravel().tolist()
        else:
            raw = a.tobytes()
            rb = stride * 4
            keys = [raw[off : off + rb] for off in range(0, n * rb, rb)]
        seen: dict = {}
        first_idx: list[int] = []
        loop_idx = [0] * n
        for i, k in enumerate(keys):
            idx = seen.get(k)
            if idx is None:
                idx = len(first_idx)
                seen[k] = idx
                first_idx.append(i)
            loop_idx[i] = idx
        return np.ascontiguousarray(a[first_idx], dtype=np.float32), np.array(loop_idx, dtype=np.int32)
    if stride == 2:
        keys = a.view(np.uint64).ravel()
    else:
        keys = a.view(np.dtype((np.void, stride * 4))).ravel()
    _, first_idx, inv = np.unique(keys, return_index=True, return_inverse=True)
    order = np.argsort(first_idx)
    inv_map = np.empty_like(order)
    inv_map[order] = np.arange(len(order), dtype=order.dtype)
    uniq_vals = np.ascontiguousarray(a[first_idx[order]], dtype=np.float32)
    loop_idx = inv_map[inv].astype(np.int32)
    return uniq_vals, loop_idx


class GeometryMixin:
    def _export_geometries(self) -> None:
        self.sw.open_element("library_geometries")
        for ob in self.export_set:
            if ob.type != "MESH" or not ob.data:
                continue
            self._export_geometry_object(ob)
        self.sw.close_element()

    def _create_normals(
        self,
        mesh: Mesh,
        loop_starts,
        loop_totals,
        use_smooth,
        loop_vidx,
    ):
        """Vectorized exact port of GeometryExporter::create_normals()."""
        import numpy as np

        n_polys = len(loop_starts)
        n_loops = len(loop_vidx)
        if n_polys == 0:
            return np.empty((0, 3), dtype=np.float32), np.empty((0,), dtype=np.int32)

        keep_attr = mesh.attributes.get(KEEP_NORMALS_ATTR)
        use_kept_normals = (
            keep_attr is not None
            and keep_attr.domain == "CORNER"
            and keep_attr.data_type == "FLOAT_VECTOR"
            and len(keep_attr.data) == n_loops
        )
        use_corner_normals = use_kept_normals or mesh.has_custom_normals or mesh.normals_domain == "CORNER"
        all_vert_normals = use_corner_normals or bool(use_smooth.all())
        all_flat = (not use_corner_normals) and (not bool(use_smooth.any()))

        # Fast path 1: all flat faces (no corner normals) -> 1 normal per polygon, no dedup
        if all_flat:
            poly_normals = np.empty(n_polys * 3, dtype=np.float32)
            mesh.polygons.foreach_get("normal", poly_normals)
            normals_arr = poly_normals.reshape(n_polys, 3)
            loop_norm_idx = np.repeat(np.arange(n_polys, dtype=np.int32), loop_totals)
            return normals_arr, loop_norm_idx

        # Fast path 2: corner normals or all smooth faces -> deduplicate in first-seen order
        if all_vert_normals:
            if use_kept_normals:
                raw_n = np.empty(n_loops * 3, dtype=np.float32)
                keep_attr.data.foreach_get("vector", raw_n)
                raw_n = raw_n.reshape(n_loops, 3)
                mesh.attributes.remove(keep_attr)
            elif use_corner_normals:
                raw_n = np.empty(n_loops * 3, dtype=np.float32)
                mesh.corner_normals.foreach_get("vector", raw_n)
                raw_n = raw_n.reshape(n_loops, 3)
            else:
                v_n = np.empty(len(mesh.vertices) * 3, dtype=np.float32)
                mesh.vertices.foreach_get("normal", v_n)
                raw_n = v_n.reshape(-1, 3)[loop_vidx]

            # Normalize in float64 then cast to float32 (matches Vector.normalized() + f32())
            raw64 = raw_n.astype(np.float64)
            lens = np.linalg.norm(raw64, axis=1, keepdims=True)
            mask = lens[:, 0] > 0.0
            raw64[mask] /= lens[mask]
            n32 = raw64.astype(np.float32)
            # Normalize -0.0 to +0.0 so dedup keys match Python float dict keys
            n32[n32 == 0.0] = 0.0

            # Deduplicate preserving first-seen order (matches shared_normal_indices dict)
            uniq, first_idx, inv = np.unique(n32, axis=0, return_index=True, return_inverse=True)
            order = np.argsort(first_idx)
            inv_map = np.empty_like(order)
            inv_map[order] = np.arange(len(order), dtype=order.dtype)
            normals_arr = uniq[order]
            loop_norm_idx = inv_map[inv].astype(np.int32)
            return normals_arr, loop_norm_idx

        # Mixed flat + smooth faces: batch-fetch all arrays, single pass over polygons
        poly_normals = np.empty(n_polys * 3, dtype=np.float32)
        mesh.polygons.foreach_get("normal", poly_normals)
        poly_normals = poly_normals.reshape(n_polys, 3)

        v_n = np.empty(len(mesh.vertices) * 3, dtype=np.float32)
        mesh.vertices.foreach_get("normal", v_n)
        v_n64 = v_n.reshape(-1, 3).astype(np.float64)
        vlens = np.linalg.norm(v_n64, axis=1, keepdims=True)
        vmask = vlens[:, 0] > 0.0
        v_n64[vmask] /= vlens[vmask]
        v_n32 = v_n64.astype(np.float32)
        v_n32[v_n32 == 0.0] = 0.0
        v_n_list = v_n32.tolist()
        p_n_list = poly_normals.tolist()
        l_vidx_list = loop_vidx.tolist()
        l_starts_list = loop_starts.tolist()
        l_totals_list = loop_totals.tolist()
        smooth_list = use_smooth.tolist()

        normals: list[tuple[float, float, float]] = []
        loop_norm_idx = np.empty(n_loops, dtype=np.int32)
        shared_normal_indices: dict[tuple[float, float, float], int] = {}
        last_normal_index = -1

        for pi in range(n_polys):
            ls = l_starts_list[pi]
            lt = l_totals_list[pi]
            if not smooth_list[pi]:
                pn = p_n_list[pi]
                normals.append((pn[0], pn[1], pn[2]))
                last_normal_index += 1
                loop_norm_idx[ls:ls + lt] = last_normal_index
            else:
                for li in range(ls, ls + lt):
                    vn = v_n_list[l_vidx_list[li]]
                    nt = (vn[0], vn[1], vn[2])
                    idx = shared_normal_indices.get(nt)
                    if idx is not None:
                        loop_norm_idx[li] = idx
                    else:
                        last_normal_index += 1
                        shared_normal_indices[nt] = last_normal_index
                        normals.append(nt)
                        loop_norm_idx[li] = last_normal_index

        return np.asarray(normals, dtype=np.float32), loop_norm_idx

    def _write_mesh_xml(
        self,
        ob: Object,
        mesh: Mesh,
        geom_id: str,
        geom_name: str,
        include_loose_edges: bool = True,
        use_loop_triangles: bool = False,
    ) -> None:
        import numpy as np

        n_loops = len(mesh.loops)
        vcount = len(mesh.vertices)

        if use_loop_triangles:
            mesh.calc_loop_triangles()
            n_polys = len(mesh.loop_triangles)
            n_emit_loops = n_polys * 3
            tri_loops = np.empty(n_emit_loops, dtype=np.int32)
            loop_vidx = np.empty(n_emit_loops, dtype=np.int32)
            mat_indices = np.empty(n_polys, dtype=np.int32)
            if n_polys > 0:
                mesh.loop_triangles.foreach_get("loops", tri_loops)
                mesh.loop_triangles.foreach_get("vertices", loop_vidx)
                mesh.loop_triangles.foreach_get("material_index", mat_indices)
            loop_totals = np.full(n_polys, 3, dtype=np.int32)
            if n_emit_loops > 0:
                cn = np.empty(n_loops * 3, dtype=np.float32)
                mesh.corner_normals.foreach_get("vector", cn)
                raw64 = cn.reshape(n_loops, 3)[tri_loops].astype(np.float64)
                lens = np.linalg.norm(raw64, axis=1, keepdims=True)
                mask = lens[:, 0] > 0.0
                raw64[mask] /= lens[mask]
                normals_arr, loop_norm_idx = _dedup_float_rows(raw64.astype(np.float32))
            else:
                normals_arr = np.empty((0, 3), dtype=np.float32)
                loop_norm_idx = np.empty(0, dtype=np.int32)
        else:
            tri_loops = None
            n_polys = len(mesh.polygons)
            n_emit_loops = n_loops
            loop_starts = np.empty(n_polys, dtype=np.int32)
            loop_totals = np.empty(n_polys, dtype=np.int32)
            use_smooth = np.empty(n_polys, dtype=bool)
            mat_indices = np.empty(n_polys, dtype=np.int32)
            if n_polys > 0:
                mesh.polygons.foreach_get("loop_start", loop_starts)
                mesh.polygons.foreach_get("loop_total", loop_totals)
                mesh.polygons.foreach_get("use_smooth", use_smooth)
                mesh.polygons.foreach_get("material_index", mat_indices)

            loop_vidx = np.empty(n_loops, dtype=np.int32)
            if n_loops > 0:
                mesh.loops.foreach_get("vertex_index", loop_vidx)

            normals_arr, loop_norm_idx = self._create_normals(
                mesh, loop_starts, loop_totals, use_smooth, loop_vidx
            )

        # Color attributes on CORNER or POINT domain (BYTE_COLOR or FLOAT_COLOR)
        byte_color_attrs = []
        for attr in mesh.color_attributes:
            if attr.domain in ("CORNER", "POINT") and attr.data_type in ("BYTE_COLOR", "FLOAT_COLOR"):
                byte_color_attrs.append(attr)
            else:
                self.warnings.append(
                    f"Mesh '{geom_name}': skipped unsupported color attribute '{attr.name}' "
                    f"(domain={attr.domain}, type={attr.data_type})"
                )
        has_color = len(byte_color_attrs) > 0
        has_uvs = len(mesh.uv_layers) > 0

        self.sw.open_element("geometry", [("id", geom_id), ("name", geom_name)])
        self.sw.open_element("mesh")

        apply_glob = self.settings.apply_global_orientation
        gt = self.settings.global_transform
        if apply_glob:
            gt_rot = np.array(gt.to_3x3(), dtype=np.float64)
            gt_trans = np.array(gt.translation, dtype=np.float64)

        # 1. Positions source (foreach_get)
        pos_id = f"{geom_id}-positions"
        pos_arr_id = f"{pos_id}-array"
        pos_arr = np.empty(vcount * 3, dtype=np.float32)
        if vcount > 0:
            mesh.vertices.foreach_get("co", pos_arr)
            if apply_glob:
                p64 = pos_arr.reshape(vcount, 3).astype(np.float64)
                pos_arr = (p64 @ gt_rot.T + gt_trans).astype(np.float32).ravel()
        self._write_float_source_raw(pos_id, pos_arr_id, vcount, 3, ("X", "Y", "Z"), fmt_f32_array(pos_arr))

        # 2. Normals source
        nor_id = f"{geom_id}-normals"
        nor_arr_id = f"{nor_id}-array"
        if apply_glob and len(normals_arr) > 0:
            n64 = normals_arr.astype(np.float64)
            normals_arr = (n64 @ gt_rot.T + gt_trans).astype(np.float32)
        self._write_float_source_raw(
            nor_id, nor_arr_id, len(normals_arr), 3, ("X", "Y", "Z"), fmt_f32_array(normals_arr)
        )

        dedupe_attrs = self.settings.deduplicate_attributes

        # 3. UV sources (foreach_get)
        active_uv_idx = 0
        uv_layer_indices: list[tuple[str, int, np.ndarray]] = []
        if has_uvs:
            for i, uvl in enumerate(mesh.uv_layers):
                if uvl.active:
                    active_uv_idx = i
                    break
            for i, uvl in enumerate(mesh.uv_layers):
                if self.settings.active_uv_only and i != active_uv_idx:
                    continue
                layer_id = f"{geom_id}-map" if self.settings.active_uv_only else f"{geom_id}-map-{i}"
                set_idx = 0 if self.settings.active_uv_only else i
                uv_arr = np.empty(n_loops * 2, dtype=np.float32)
                if n_loops > 0:
                    uvl.data.foreach_get("uv", uv_arr)
                uv_rows = uv_arr.reshape(n_loops, 2)
                if tri_loops is not None:
                    uv_rows = uv_rows[tri_loops] if n_emit_loops > 0 else np.empty((0, 2), dtype=np.float32)
                if dedupe_attrs and n_emit_loops > 0:
                    uniq_uv, uv_idx = _dedup_float_rows(uv_rows)
                    self._write_float_source_raw(
                        layer_id, f"{layer_id}-array", len(uniq_uv), 2, ("S", "T"), fmt_f32_array(uniq_uv)
                    )
                    uv_layer_indices.append((layer_id, set_idx, uv_idx))
                else:
                    self._write_float_source_raw(
                        layer_id, f"{layer_id}-array", n_emit_loops, 2, ("S", "T"), fmt_f32_array(uv_rows)
                    )

        # 4. Vertex color sources (foreach_get)
        col_layer_indices: list[tuple[str, int, np.ndarray]] = []
        if has_color:
            for idx, attr in enumerate(byte_color_attrs):
                layer_id = f"{geom_id}-colors-{attr.name}"
                is_point = attr.domain == "POINT"
                src_count = vcount if is_point else n_loops
                col_arr = np.empty(src_count * 4, dtype=np.float32)
                if src_count > 0:
                    if attr.data_type == "BYTE_COLOR":
                        attr.data.foreach_get("color_srgb", col_arr)
                    else:
                        attr.data.foreach_get("color", col_arr)
                        rgb = np.maximum(col_arr.reshape(src_count, 4)[:, :3], np.float32(0.0))
                        srgb = np.where(
                            rgb <= 0.0031308,
                            rgb * 12.92,
                            1.055 * np.power(rgb, 1.0 / 2.4) - 0.055,
                        )
                        col_arr.reshape(src_count, 4)[:, :3] = srgb.astype(np.float32, copy=False)
                col_rows = col_arr.reshape(src_count, 4)
                if is_point:
                    col_rows = col_rows[loop_vidx] if n_emit_loops > 0 else np.empty((0, 4), dtype=np.float32)
                elif tri_loops is not None:
                    col_rows = col_rows[tri_loops] if n_emit_loops > 0 else np.empty((0, 4), dtype=np.float32)
                if dedupe_attrs and n_emit_loops > 0:
                    uniq_col, col_idx = _dedup_float_rows(col_rows)
                    self._write_float_source_raw(
                        layer_id,
                        f"{layer_id}-array",
                        len(uniq_col),
                        4,
                        ("R", "G", "B", "A"),
                        fmt_f32_array(uniq_col),
                        source_name=attr.name,
                    )
                    col_layer_indices.append((layer_id, idx, col_idx))
                else:
                    self._write_float_source_raw(
                        layer_id,
                        f"{layer_id}-array",
                        n_emit_loops,
                        4,
                        ("R", "G", "B", "A"),
                        fmt_f32_array(col_rows),
                        source_name=attr.name,
                    )

        # 5. <vertices>
        verts_id = f"{geom_id}-vertices"
        self.sw.open_element("vertices", [("id", verts_id)])
        self.sw.write_element("input", None, [("semantic", "POSITION"), ("source", f"#{pos_id}")])
        self.sw.close_element()

        # 6. Loose edges (<lines>)
        if include_loose_edges and len(mesh.edges) > 0:
            n_edges = len(mesh.edges)
            is_loose = np.empty(n_edges, dtype=bool)
            mesh.edges.foreach_get("is_loose", is_loose)
            if is_loose.any():
                edge_verts = np.empty(n_edges * 2, dtype=np.int32)
                mesh.edges.foreach_get("vertices", edge_verts)
                # Match GeometryExporter.cpp:245-246 (edge[1] then edge[0])
                loose_v = edge_verts.reshape(n_edges, 2)[is_loose][:, ::-1]
                self.sw.open_element("lines", [("count", fmt_int(len(loose_v)))])
                self.sw.write_element(
                    "input", None, [("semantic", "VERTEX"), ("source", f"#{verts_id}"), ("offset", "0")]
                )
                self.sw.open_element("p")
                self.sw.append_text(fmt_int_array(loose_v))
                self.sw.close_element()
                self.sw.close_element()

        # 7. Primitive lists (<triangles> or <polylist>)
        if n_polys > 0:
            mat_count = len(ob.material_slots)
            loop_mat = np.repeat(mat_indices, loop_totals)
            tex_indices = None if dedupe_attrs else np.arange(n_emit_loops, dtype=np.int32)
            if mat_count > 0:
                for a in range(mat_count):
                    self._write_primitive_list(
                        a,
                        has_uvs,
                        active_uv_idx,
                        byte_color_attrs,
                        ob,
                        mesh,
                        geom_id,
                        loop_totals,
                        mat_indices,
                        loop_mat,
                        loop_vidx,
                        loop_norm_idx,
                        tex_indices,
                        uv_layer_indices if dedupe_attrs else None,
                        col_layer_indices if dedupe_attrs else None,
                    )
            else:
                self._write_primitive_list(
                    0,
                    has_uvs,
                    active_uv_idx,
                    byte_color_attrs,
                    ob,
                    mesh,
                    geom_id,
                    loop_totals,
                    mat_indices,
                    loop_mat,
                    loop_vidx,
                    loop_norm_idx,
                    tex_indices,
                    uv_layer_indices if dedupe_attrs else None,
                    col_layer_indices if dedupe_attrs else None,
                )

        self.sw.close_element()  # mesh
        self.sw.close_element()  # geometry

    def _write_float_source_raw(
        self,
        source_id: str,
        array_id: str,
        count: int,
        stride: int,
        params: tuple[str, ...],
        values_text: str,
        source_name: str | None = None,
        param_type: str = "float",
    ) -> None:
        attrs = [("id", source_id)]
        if source_name:
            attrs.append(("name", encode_xml(source_name)))
        self.sw.open_element("source", attrs)
        self.sw.open_element("float_array", [("id", array_id), ("count", fmt_int(count * stride))])
        if values_text:
            self.sw.append_text(values_text)
        self.sw.close_element()
        self.sw.open_element("technique_common")
        self.sw.open_element(
            "accessor",
            [("source", f"#{array_id}"), ("count", fmt_int(count)), ("stride", fmt_int(stride))],
        )
        for p in params:
            self.sw.write_element("param", None, [("name", p), ("type", param_type)])
        self.sw.close_element()
        self.sw.close_element()
        self.sw.close_element()

    def _write_float_source(
        self,
        source_id: str,
        array_id: str,
        count: int,
        stride: int,
        params: tuple[str, ...],
        values: list[str],
        source_name: str | None = None,
        param_type: str = "float",
    ) -> None:
        self._write_float_source_raw(
            source_id, array_id, count, stride, params, " ".join(values), source_name, param_type
        )

    def _write_primitive_list(
        self,
        material_index: int,
        has_uvs: bool,
        active_uv_idx: int,
        byte_color_attrs: list,
        ob: Object,
        mesh: Mesh,
        geom_id: str,
        loop_totals,
        mat_indices,
        loop_mat,
        loop_vidx,
        loop_norm_idx,
        tex_indices,
        uv_layer_indices=None,
        col_layer_indices=None,
    ) -> None:
        import numpy as np

        poly_mask = mat_indices == material_index
        if not poly_mask.any():
            return
        sel_vcounts = loop_totals[poly_mask]
        is_triangulated = bool((sel_vcounts == 3).all())

        ma = ob.material_slots[material_index].material if len(ob.material_slots) > material_index else None
        prim_tag = "triangles" if is_triangulated else "polylist"
        attrs: list[tuple[str, str]] = []
        if ma is not None:
            mat_id = self.id_map.translate_id(get_material_id(self.id_map, ma))
            attrs.append(("material", mat_id))
        attrs.append(("count", fmt_int(len(sel_vcounts))))

        self.sw.open_element(prim_tag, attrs)
        self.sw.write_element(
            "input", None, [("semantic", "VERTEX"), ("source", f"#{geom_id}-vertices"), ("offset", "0")]
        )
        self.sw.write_element(
            "input", None, [("semantic", "NORMAL"), ("source", f"#{geom_id}-normals"), ("offset", "1")]
        )

        use_dedup_cols = uv_layer_indices is not None or col_layer_indices is not None
        if use_dedup_cols:
            cur_offset = 2
            for layer_id, set_idx, _idx_arr in (uv_layer_indices or ()):
                self.sw.write_element(
                    "input",
                    None,
                    [
                        ("semantic", "TEXCOORD"),
                        ("source", f"#{layer_id}"),
                        ("offset", fmt_int(cur_offset)),
                        ("set", fmt_int(set_idx)),
                    ],
                )
                cur_offset += 1
            for layer_id, set_idx, _idx_arr in (col_layer_indices or ()):
                self.sw.write_element(
                    "input",
                    None,
                    [
                        ("semantic", "COLOR"),
                        ("source", f"#{layer_id}"),
                        ("offset", fmt_int(cur_offset)),
                        ("set", fmt_int(set_idx)),
                    ],
                )
                cur_offset += 1
        else:
            if has_uvs:
                for i in range(len(mesh.uv_layers)):
                    if self.settings.active_uv_only and i != active_uv_idx:
                        continue
                    layer_id = f"{geom_id}-map" if self.settings.active_uv_only else f"{geom_id}-map-{i}"
                    set_idx = 0 if self.settings.active_uv_only else i
                    self.sw.write_element(
                        "input",
                        None,
                        [
                            ("semantic", "TEXCOORD"),
                            ("source", f"#{layer_id}"),
                            ("offset", "2"),
                            ("set", fmt_int(set_idx)),
                        ],
                    )

            has_color = len(byte_color_attrs) > 0
            if has_color:
                col_offset = "3" if has_uvs else "2"
                for idx, attr in enumerate(byte_color_attrs):
                    layer_id = f"{geom_id}-colors-{attr.name}"
                    self.sw.write_element(
                        "input",
                        None,
                        [
                            ("semantic", "COLOR"),
                            ("source", f"#{layer_id}"),
                            ("offset", col_offset),
                            ("set", fmt_int(idx)),
                        ],
                    )

        if not is_triangulated:
            self.sw.open_element("vcount")
            self.sw.append_text(fmt_int_array(sel_vcounts) + " ")
            self.sw.close_element()

        # Vectorized <p> assembly
        if poly_mask.all():
            cols = [loop_vidx, loop_norm_idx]
            if use_dedup_cols:
                for _lid, _sidx, idx_arr in (uv_layer_indices or ()):
                    cols.append(idx_arr)
                for _lid, _sidx, idx_arr in (col_layer_indices or ()):
                    cols.append(idx_arr)
            else:
                if has_uvs:
                    cols.append(tex_indices)
                if len(byte_color_attrs) > 0:
                    cols.append(tex_indices)
        else:
            lmask = loop_mat == material_index
            cols = [loop_vidx[lmask], loop_norm_idx[lmask]]
            if use_dedup_cols:
                for _lid, _sidx, idx_arr in (uv_layer_indices or ()):
                    cols.append(idx_arr[lmask])
                for _lid, _sidx, idx_arr in (col_layer_indices or ()):
                    cols.append(idx_arr[lmask])
            else:
                if has_uvs:
                    cols.append(tex_indices[lmask])
                if len(byte_color_attrs) > 0:
                    cols.append(tex_indices[lmask])
        p_arr = np.column_stack(cols)

        self.sw.open_element("p")
        self.sw.append_text(fmt_int_array(p_arr))
        self.sw.close_element()
        self.sw.close_element()

    def _export_geometry_object(self, ob: Object) -> None:
        use_inst = self.settings.use_object_instantiation
        geom_id = get_geometry_id(self.id_map, ob, use_inst)
        if use_inst and geom_id in self.exported_geometries:
            return
        self.exported_geometries.add(geom_id)
        geom_name = encode_xml(ob.data.name if use_inst else ob.name)

        use_loop_tris = self.settings.triangulate and not self.settings.keep_quads and self.settings.keep_normals
        has_morphs = (
            self.settings.include_shapekeys
            and ob.data.shape_keys is not None
            and len(ob.data.shape_keys.key_blocks) > 1
        )
        mesh, needs_free = get_mesh_copy(
            self.depsgraph,
            ob,
            self.settings.apply_modifiers,
            False if use_loop_tris else self.settings.triangulate,
            keep_normals=self.settings.keep_normals,
            keep_quads=self.settings.keep_quads,
            eval_mode=self.settings.export_mesh_type,
            readonly=not has_morphs,
        )
        if needs_free:
            self._temp_meshes.append(mesh)
        self._write_mesh_xml(
            ob,
            mesh,
            geom_id,
            geom_name,
            include_loose_edges=True,
            use_loop_triangles=use_loop_tris,
        )

        # Shape keys
        if has_morphs:
            key_blocks = ob.data.shape_keys.key_blocks
            for kb in key_blocks[1:]:
                morph_id = f"{get_geometry_id(self.id_map, ob, False)}_morph_{self.id_map.translate_id(kb.name)}"
                if morph_id in self.exported_geometries:
                    continue
                self.exported_geometries.add(morph_id)
                if len(kb.data) == len(mesh.vertices):
                    for vi, kd in enumerate(kb.data):
                        mesh.vertices[vi].co = kd.co
                    mesh.update()
                self._write_mesh_xml(
                    ob,
                    mesh,
                    morph_id,
                    encode_xml(kb.name),
                    include_loose_edges=False,
                    use_loop_triangles=use_loop_tris,
                )
