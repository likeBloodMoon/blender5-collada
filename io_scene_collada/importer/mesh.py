# SPDX-FileCopyrightText: 2009-2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Geometry reading, mesh construction (<triangles>, <polylist>, <polygons>/<ph>), and sharp edges."""

from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from typing import TYPE_CHECKING

import bpy
import numpy as np
from mathutils import Vector

from .xml_parse import (
    _find_child,
    _find_children,
    _parse_f32_np,
    _parse_floats,
    _parse_i32_np,
    _parse_ints,
    _safe_float,
    _safe_int,
    _slice_accessor_array,
    _strip_ns,
    _uri_to_local_id,
)

if TYPE_CHECKING:
    from bpy.types import Mesh


class MeshMixin:
    def _read_library_geometries(self, lib: ET.Element) -> None:
        for geom_elem in _find_children(lib, "geometry"):
            geom_id = geom_elem.attrib.get("id", "")
            if self._allowed_geom_ids is not None and geom_id not in self._allowed_geom_ids:
                continue
            geom_name = geom_elem.attrib.get("name") or geom_id
            mesh_elem = _find_child(geom_elem, "mesh")
            if mesh_elem is None:
                continue
            self._read_mesh(geom_id, geom_name, mesh_elem)

    def _read_mesh(self, geom_id: str, geom_name: str, mesh_elem: ET.Element) -> None:
        import numpy as np

        # Parse all <source> elements with C-speed parser
        sources: dict[str, tuple[np.ndarray, int, str]] = {}
        for src in _find_children(mesh_elem, "source"):
            sid = src.attrib.get("id", "")
            sname = src.attrib.get("name") or sid
            fa = _find_child(src, "float_array")
            if fa is not None:
                fa_id = fa.attrib.get("id", sid)
                vals = _parse_f32_np(
                    fa.text,
                    self.warnings,
                    f"<float_array id='{fa_id}'> of geometry '{geom_name}'",
                )
                if vals is None:
                    return
            else:
                vals = np.empty(0, dtype=np.float32)
            stride = 3
            acc = None
            tc = _find_child(src, "technique_common")
            if tc is not None:
                acc = _find_child(tc, "accessor")
                if acc is not None and "stride" in acc.attrib:
                    try:
                        stride = int(acc.attrib["stride"])
                    except ValueError:
                        self.warnings.append(
                            f"Invalid stride '{acc.attrib['stride']}' in source '{sid}' of geometry '{geom_name}'"
                        )
                        stride = 0
            if stride <= 0:
                self.warnings.append(
                    f"Invalid stride {stride} in source '{sid}' of geometry '{geom_name}'"
                )
                vals = np.empty(0, dtype=np.float32)
                stride = 3
            else:
                vals = _slice_accessor_array(
                    vals, acc, stride, self.warnings, f"source '{sid}' of geometry '{geom_name}'"
                )
            sources[sid] = (vals, stride, sname)

        # Parse <vertices>
        verts_elem = _find_child(mesh_elem, "vertices")
        pos_source_id = ""
        vert_normal_src_id = ""
        if verts_elem is not None:
            for inp in _find_children(verts_elem, "input"):
                sem = inp.attrib.get("semantic", "")
                if sem == "POSITION":
                    pos_source_id = _uri_to_local_id(inp.attrib.get("source"))
                elif sem == "NORMAL":
                    vert_normal_src_id = _uri_to_local_id(inp.attrib.get("source"))

        pos_vals, pos_stride, _ = sources.get(pos_source_id, (np.empty(0, dtype=np.float32), 3, ""))
        if pos_stride <= 0:
            pos_stride = 3
        elif pos_vals.size > 0 and pos_stride < 3:
            self.warnings.append(
                f"Geometry '{geom_name}': POSITION source '{pos_source_id}' has stride {pos_stride} < 3; zero-padding missing axes"
            )
        num_verts = pos_vals.size // pos_stride
        if pos_vals.size > 0 and not np.isfinite(pos_vals).all():
            pos_vals = np.where(np.isfinite(pos_vals), pos_vals, np.float32(0.0))
            self.warnings.append(f"Geometry '{geom_name}': replaced non-finite vertex positions with 0.0")
        warned_bad_strides: set[tuple[str, str]] = set()

        # Collect per-primitive vectorized chunks
        vcount_chunks: list[np.ndarray] = []
        loop_vert_chunks: list[np.ndarray] = []
        face_sharp_chunks: list[np.ndarray] = []
        loop_norm_chunks: list[np.ndarray] = []
        has_all_loop_normals = True
        uv_chunks: dict[str, list[np.ndarray]] = {}
        uv_set_order: list[str] = []
        col_chunks: dict[str, list[np.ndarray]] = {}
        col_set_order: list[str] = []
        loose_edges: list[tuple[int, int]] = []
        mat_sym_order: list[str] = []
        mat_sym_to_id: dict[str, int] = {}
        face_mat_chunks: list[np.ndarray] = []
        total_faces = 0
        total_loops = 0
        dropped_faces_total = 0
        invalid_vcount_total = 0

        def extract_vcolname(src_id: str, src_name: str) -> str:
            if src_name and src_name != src_id:
                return src_name
            idx = src_id.find("-mesh-colors-")
            if idx != -1:
                return src_id[idx + 13:]
            return src_id

        for child in mesh_elem:
            tag = _strip_ns(child.tag)
            if tag not in ("triangles", "polylist", "polygons", "trifans", "tristrips", "lines"):
                continue

            inputs: list[tuple[str, str, int, int]] = []
            max_offset = 0
            for inp in _find_children(child, "input"):
                sem = inp.attrib.get("semantic", "")
                src_id = _uri_to_local_id(inp.attrib.get("source"))
                try:
                    off = int(inp.attrib.get("offset", "0"))
                except ValueError:
                    self.warnings.append(
                        f"Invalid input offset '{inp.attrib.get('offset')}' in geometry '{geom_name}'"
                    )
                    continue
                if off < 0:
                    self.warnings.append(
                        f"Negative input offset {off} in geometry '{geom_name}'"
                    )
                    continue
                try:
                    iset = int(inp.attrib.get("set", "0"))
                except ValueError:
                    iset = 0
                inputs.append((sem, src_id, off, iset))
                if off > max_offset:
                    max_offset = off
            num_inputs = max_offset + 1

            vert_off = -1
            norm_off = -1
            norm_src_id = vert_normal_src_id
            uv_inputs: list[tuple[str, int]] = []
            col_inputs: list[tuple[str, str, int]] = []

            for sem, src_id, off, iset in inputs:
                if sem == "VERTEX":
                    vert_off = off
                elif sem == "NORMAL":
                    norm_off = off
                    norm_src_id = src_id
                elif sem == "TEXCOORD":
                    if src_id not in uv_chunks:
                        uv_chunks[src_id] = (
                            [np.zeros((total_loops, 2), dtype=np.float32)] if total_loops > 0 else []
                        )
                        uv_set_order.append(src_id)
                    uv_inputs.append((src_id, off))
                elif sem == "COLOR":
                    sname = sources[src_id][2] if src_id in sources else src_id
                    cname = extract_vcolname(src_id, sname)
                    if cname not in col_chunks:
                        col_chunks[cname] = (
                            [np.ones((total_loops, 4), dtype=np.float32)] if total_loops > 0 else []
                        )
                        col_set_order.append(cname)
                    col_inputs.append((cname, src_id, off))

            if vert_off < 0:
                continue

            if tag == "lines":
                p_el = _find_child(child, "p")
                if p_el is not None and p_el.text:
                    p_arr = _parse_i32_np(p_el.text, self.warnings, f"<lines>/<p> of geometry '{geom_name}'")
                    if p_arr is None:
                        continue
                    cnt = p_arr.size // (2 * num_inputs)
                    if cnt > 0 and num_verts > 0:
                        p2 = p_arr[:cnt * 2 * num_inputs].reshape(cnt, 2, num_inputs)
                        e0 = p2[:, 0, vert_off]
                        e1 = p2[:, 1, vert_off]
                        e_ok = (e0 >= 0) & (e0 < num_verts) & (e1 >= 0) & (e1 < num_verts) & (e0 != e1)
                        if e_ok.any():
                            loose_edges.extend(zip(e0[e_ok].tolist(), e1[e_ok].tolist()))
                continue

            # Parse primitive into (prim_vcounts, prim_p[n_loops, num_inputs])
            prim_vcounts: np.ndarray | None = None
            prim_p: np.ndarray | None = None

            if tag == "triangles":
                p_el = _find_child(child, "p")
                if p_el is not None and p_el.text:
                    p_raw = _parse_i32_np(p_el.text, self.warnings, f"<triangles>/<p> of geometry '{geom_name}'")
                    if p_raw is None:
                        continue
                    tri_count = p_raw.size // (3 * num_inputs)
                    if tri_count > 0:
                        prim_p = p_raw[:tri_count * 3 * num_inputs].reshape(tri_count * 3, num_inputs)
                        prim_vcounts = np.full(tri_count, 3, dtype=np.int32)
            elif tag == "polylist":
                vc_el = _find_child(child, "vcount")
                p_el = _find_child(child, "p")
                if vc_el is not None and vc_el.text:
                    vc_raw = _parse_i32_np(vc_el.text, self.warnings, f"<polylist>/<vcount> of geometry '{geom_name}'")
                    if vc_raw is None or vc_raw.size == 0:
                        continue
                    if p_el is None or not p_el.text:
                        n_lt3 = int((vc_raw < 3).sum())
                        invalid_vcount_total += n_lt3
                        if n_lt3 < vc_raw.size:
                            self.warnings.append(
                                f"Geometry '{geom_name}': <polylist> has empty <p> with non-zero <vcount>; skipped"
                            )
                        continue
                    p_raw = _parse_i32_np(p_el.text, self.warnings, f"<polylist>/<p> of geometry '{geom_name}'")
                    if p_raw is None:
                        continue
                    n_tuples = p_raw.size // num_inputs
                    p_2d = p_raw[:n_tuples * num_inputs].reshape(n_tuples, num_inputs)
                    if bool((vc_raw >= 3).all()):
                        need = int(vc_raw.sum(dtype=np.int64))
                        if need <= n_tuples:
                            prim_vcounts = vc_raw
                            prim_p = p_2d[:need]
                        else:
                            # vcount sum exceeds <p>; keep only complete polygons
                            cs = np.cumsum(vc_raw, dtype=np.int64)
                            valid_n = int(np.searchsorted(cs, n_tuples, side="right"))
                            if valid_n > 0:
                                prim_vcounts = vc_raw[:valid_n]
                                prim_p = p_2d[:int(cs[valid_n - 1])]
                            self.warnings.append(
                                f"Geometry '{geom_name}': <polylist> vcount sum ({need}) exceeds <p> tuples ({n_tuples}); truncated"
                            )
                    else:
                        valid_vc: list[int] = []
                        valid_rows: list[np.ndarray] = []
                        cursor = 0
                        truncated = False
                        for vc in vc_raw.tolist():
                            if vc < 3:
                                invalid_vcount_total += 1
                                cursor += max(vc, 0)
                            elif cursor + vc <= n_tuples:
                                valid_vc.append(vc)
                                valid_rows.append(p_2d[cursor:cursor + vc])
                                cursor += vc
                            else:
                                truncated = True
                                cursor += vc
                        if truncated:
                            self.warnings.append(
                                f"Geometry '{geom_name}': <polylist> vcount exceeds <p> tuples ({n_tuples}); truncated"
                            )
                        if valid_vc:
                            prim_vcounts = np.asarray(valid_vc, dtype=np.int32)
                            prim_p = np.vstack(valid_rows)
            elif tag == "polygons":
                from mathutils.geometry import normal as _poly_normal
                from mathutils.geometry import tessellate_polygon as _tessellate_polygon

                p_list: list[np.ndarray] = []
                vc_list: list[int] = []
                for p_el in _find_children(child, "p"):
                    if not p_el.text:
                        invalid_vcount_total += 1
                        continue
                    pr = _parse_i32_np(p_el.text, self.warnings, f"<polygons>/<p> of geometry '{geom_name}'")
                    if pr is None:
                        continue
                    vc = pr.size // num_inputs
                    if vc >= 3:
                        vc_list.append(vc)
                        p_list.append(pr[:vc * num_inputs].reshape(vc, num_inputs))
                    else:
                        invalid_vcount_total += 1

                ph_elems = _find_children(child, "ph")
                if ph_elems:
                    pos_2d = (
                        pos_vals[:num_verts * pos_stride].reshape(num_verts, pos_stride)
                        if num_verts > 0
                        else np.empty((0, 3), dtype=np.float32)
                    )

                    def _vert_vec(vi: int) -> Vector:
                        row = pos_2d[vi]
                        if pos_stride >= 3:
                            return Vector((float(row[0]), float(row[1]), float(row[2])))
                        x = float(row[0]) if pos_stride >= 1 else 0.0
                        y = float(row[1]) if pos_stride >= 2 else 0.0
                        return Vector((x, y, 0.0))

                    for ph_el in ph_elems:
                        pe = _find_child(ph_el, "p")
                        if pe is None or not pe.text:
                            invalid_vcount_total += 1
                            continue
                        pr = _parse_i32_np(pe.text, self.warnings, f"<polygons>/<ph>/<p> of geometry '{geom_name}'")
                        if pr is None:
                            continue
                        vc = pr.size // num_inputs
                        if vc < 3:
                            invalid_vcount_total += 1
                            continue
                        outer_rows = pr[:vc * num_inputs].reshape(vc, num_inputs)

                        hole_rows_list: list[np.ndarray] = []
                        for h_el in _find_children(ph_el, "h"):
                            if not h_el.text:
                                self.warnings.append(f"Geometry '{geom_name}': empty <ph>/<h> hole skipped")
                                continue
                            hr = _parse_i32_np(h_el.text, self.warnings, f"<polygons>/<ph>/<h> of geometry '{geom_name}'")
                            if hr is None:
                                continue
                            hvc = hr.size // num_inputs
                            if hvc < 3:
                                self.warnings.append(
                                    f"Geometry '{geom_name}': <ph>/<h> hole has fewer than 3 vertices; skipped"
                                )
                                continue
                            h_rows = hr[:hvc * num_inputs].reshape(hvc, num_inputs)
                            if vert_off >= 0:
                                hv = h_rows[:, vert_off]
                                if int(hv.min()) < 0 or int(hv.max()) >= num_verts:
                                    self.warnings.append(
                                        f"Geometry '{geom_name}': <ph>/<h> hole vertex index out of range; skipped"
                                    )
                                    continue
                            hole_rows_list.append(h_rows)

                        if not hole_rows_list or vert_off < 0:
                            vc_list.append(vc)
                            p_list.append(outer_rows)
                            continue

                        ov = outer_rows[:, vert_off]
                        if int(ov.min()) < 0 or int(ov.max()) >= num_verts:
                            vc_list.append(vc)
                            p_list.append(outer_rows)
                            continue

                        polylines: list[list[Vector]] = []
                        flat_pts: list[Vector] = []
                        combined_rows: list[np.ndarray] = [outer_rows, *hole_rows_list]
                        for rows in combined_rows:
                            pts = [_vert_vec(int(vi)) for vi in rows[:, vert_off].tolist()]
                            polylines.append(pts)
                            flat_pts.extend(pts)
                        all_rows = np.vstack(combined_rows)

                        try:
                            tris = _tessellate_polygon(polylines)
                        except Exception:
                            tris = []

                        if not tris:
                            self.warnings.append(
                                f"Geometry '{geom_name}': failed to tessellate <ph> holes; importing outer polygon"
                            )
                            vc_list.append(vc)
                            p_list.append(outer_rows)
                            continue

                        outer_n = _poly_normal(polylines[0])
                        tri_idx_flat: list[int] = []
                        for i0, i1, i2 in tris:
                            if outer_n.length > 1e-12:
                                tn = (flat_pts[i1] - flat_pts[i0]).cross(flat_pts[i2] - flat_pts[i0])
                                if tn.dot(outer_n) < 0.0:
                                    i1, i2 = i2, i1
                            tri_idx_flat.extend((i0, i1, i2))
                        idx_arr = np.asarray(tri_idx_flat, dtype=np.int32)
                        p_list.append(all_rows[idx_arr])
                        vc_list.extend([3] * len(tris))

                if vc_list:
                    prim_vcounts = np.asarray(vc_list, dtype=np.int32)
                    prim_p = np.vstack(p_list)
            elif tag == "trifans":
                tri_rows: list[np.ndarray] = []
                tri_total = 0
                for p_el in _find_children(child, "p"):
                    if p_el.text:
                        pr = _parse_i32_np(p_el.text, self.warnings, f"<trifans>/<p> of geometry '{geom_name}'")
                        if pr is None:
                            continue
                        vc = pr.size // num_inputs
                        if vc >= 3:
                            p2 = pr[:vc * num_inputs].reshape(vc, num_inputs)
                            idx = np.empty((vc - 2) * 3, dtype=np.int32)
                            idx[0::3] = 0
                            idx[1::3] = np.arange(1, vc - 1, dtype=np.int32)
                            idx[2::3] = np.arange(2, vc, dtype=np.int32)
                            tri_rows.append(p2[idx])
                            tri_total += vc - 2
                if tri_total > 0:
                    prim_vcounts = np.full(tri_total, 3, dtype=np.int32)
                    prim_p = np.vstack(tri_rows)
            elif tag == "tristrips":
                tri_rows = []
                tri_total = 0
                for p_el in _find_children(child, "p"):
                    if p_el.text:
                        pr = _parse_i32_np(p_el.text, self.warnings, f"<tristrips>/<p> of geometry '{geom_name}'")
                        if pr is None:
                            continue
                        vc = pr.size // num_inputs
                        if vc >= 3:
                            p2 = pr[:vc * num_inputs].reshape(vc, num_inputs)
                            nt = vc - 2
                            ks = np.arange(2, vc, dtype=np.int32)
                            even = (ks % 2) == 0
                            idx = np.empty(nt * 3, dtype=np.int32)
                            idx[0::3] = np.where(even, ks - 2, ks - 1)
                            idx[1::3] = np.where(even, ks - 1, ks - 2)
                            idx[2::3] = ks
                            tri_rows.append(p2[idx])
                            tri_total += nt
                if tri_total > 0:
                    prim_vcounts = np.full(tri_total, 3, dtype=np.int32)
                    prim_p = np.vstack(tri_rows)

            if (
                prim_vcounts is None
                or prim_p is None
                or prim_vcounts.size == 0
                or prim_p.shape[0] == 0
                or int(prim_vcounts.sum(dtype=np.int64)) != int(prim_p.shape[0])
            ):
                continue

            # Vectorized bounds check: drop only faces with out-of-range VERTEX/NORMAL/TEXCOORD/COLOR indices
            norm_vals, norm_stride, _ = sources.get(norm_src_id, (np.empty(0, dtype=np.float32), 3, ""))
            if norm_stride < 3:
                if (norm_off >= 0 or vert_normal_src_id) and ("NORMAL", norm_src_id) not in warned_bad_strides:
                    warned_bad_strides.add(("NORMAL", norm_src_id))
                    self.warnings.append(
                        f"Geometry '{geom_name}': NORMAL source '{norm_src_id}' has stride {norm_stride} < 3; skipping normals"
                    )
                n_norm_elems = 0
                has_norm_src = False
            else:
                n_norm_elems = norm_vals.size // norm_stride
                has_norm_src = (norm_off >= 0 or bool(vert_normal_src_id)) and n_norm_elems > 0

            v_col = prim_p[:, vert_off]
            loop_ok = (v_col >= 0) & (v_col < num_verts)
            if has_norm_src:
                n_col = prim_p[:, norm_off] if norm_off >= 0 else v_col
                loop_ok &= (n_col >= 0) & (n_col < n_norm_elems)
            for u_id, u_off in uv_inputs:
                u_vals_chk, u_stride_chk, _ = sources.get(u_id, (np.empty(0, dtype=np.float32), 2, ""))
                if u_stride_chk < 2:
                    if ("TEXCOORD", u_id) not in warned_bad_strides:
                        warned_bad_strides.add(("TEXCOORD", u_id))
                        self.warnings.append(
                            f"Geometry '{geom_name}': TEXCOORD source '{u_id}' has stride {u_stride_chk} < 2; skipping UV values"
                        )
                    continue
                n_u_chk = u_vals_chk.size // u_stride_chk
                if n_u_chk > 0:
                    u_col = prim_p[:, u_off]
                    loop_ok &= (u_col >= 0) & (u_col < n_u_chk)
            for _cn, c_src_id, c_off in col_inputs:
                c_vals_chk, c_stride_chk, _ = sources.get(c_src_id, (np.empty(0, dtype=np.float32), 3, ""))
                if c_stride_chk < 3:
                    if ("COLOR", c_src_id) not in warned_bad_strides:
                        warned_bad_strides.add(("COLOR", c_src_id))
                        self.warnings.append(
                            f"Geometry '{geom_name}': COLOR source '{c_src_id}' has stride {c_stride_chk} < 3; skipping color values"
                        )
                    continue
                n_c_chk = c_vals_chk.size // c_stride_chk
                if n_c_chk > 0:
                    c_col = prim_p[:, c_off]
                    loop_ok &= (c_col >= 0) & (c_col < n_c_chk)

            if not bool(loop_ok.all()):
                if tag in ("triangles", "trifans", "tristrips") or bool((prim_vcounts == 3).all()):
                    face_ok = loop_ok.reshape(-1, 3).all(axis=1)
                else:
                    starts_chk = np.cumsum(prim_vcounts, dtype=np.int64) - prim_vcounts
                    face_ok = np.logical_and.reduceat(loop_ok, starts_chk)
                dropped = int((~face_ok).sum())
                dropped_faces_total += dropped
                if not bool(face_ok.any()):
                    continue
                loop_keep = np.repeat(face_ok, prim_vcounts)
                prim_vcounts = prim_vcounts[face_ok]
                prim_p = prim_p[loop_keep]
                if prim_vcounts.size == 0 or prim_p.shape[0] == 0:
                    continue

            prim_face_count = int(prim_vcounts.size)
            prim_loop_count = int(prim_p.shape[0])
            mat_sym = child.attrib.get("material", "")
            sym_id = mat_sym_to_id.get(mat_sym)
            if sym_id is None:
                sym_id = len(mat_sym_order)
                mat_sym_to_id[mat_sym] = sym_id
                mat_sym_order.append(mat_sym)
            face_mat_chunks.append(np.full(prim_face_count, sym_id, dtype=np.int32))

            l_verts = prim_p[:, vert_off]
            vcount_chunks.append(prim_vcounts)
            loop_vert_chunks.append(l_verts)

            # Normals (vectorized)
            if has_norm_src:
                n_src = norm_vals[:n_norm_elems * norm_stride].reshape(n_norm_elems, norm_stride)[:, :3]
                n_idx = prim_p[:, norm_off] if norm_off >= 0 else l_verts
                p_norms = n_src[n_idx].astype(np.float64)
                if not np.isfinite(p_norms).all():
                    p_norms = np.where(np.isfinite(p_norms), p_norms, 0.0)
                nlens = np.linalg.norm(p_norms, axis=1, keepdims=True)
                nmask = nlens[:, 0] > 0.0
                p_norms[nmask] /= nlens[nmask]
                # Determine flat faces (all loop normals in face have dot product ~1.0 with loop 0)
                if tag in ("triangles", "trifans", "tristrips") or bool((prim_vcounts == 3).all()):
                    n3 = p_norms.reshape(prim_face_count, 3, 3)
                    dp1 = (n3[:, 0] * n3[:, 1]).sum(axis=1)
                    dp2 = (n3[:, 0] * n3[:, 2]).sum(axis=1)
                    is_flat = (dp1 >= 0.99999) & (dp1 <= 1.00001) & (dp2 >= 0.99999) & (dp2 <= 1.00001)
                else:
                    starts = np.cumsum(prim_vcounts, dtype=np.int64) - prim_vcounts
                    if p_norms.shape[0] > 0 and starts.size > 0:
                        first_n = np.repeat(p_norms[starts], prim_vcounts, axis=0)
                        dots = (p_norms * first_n).sum(axis=1)
                        ok = (dots >= 0.99999) & (dots <= 1.00001)
                        is_flat = np.logical_and.reduceat(ok, starts)
                    else:
                        is_flat = np.ones(prim_face_count, dtype=bool)
                face_sharp_chunks.append(is_flat)
                loop_norm_chunks.append(p_norms.astype(np.float32))
            else:
                face_sharp_chunks.append(np.ones(prim_face_count, dtype=bool))
                has_all_loop_normals = False

            # UVs (vectorized)
            cur_uv_srcs = {s for s, _ in uv_inputs}
            for u_id in uv_set_order:
                if u_id not in cur_uv_srcs:
                    uv_chunks[u_id].append(np.zeros((prim_loop_count, 2), dtype=np.float32))
            for u_id, u_off in uv_inputs:
                u_vals, u_stride, _ = sources.get(u_id, (np.empty(0, dtype=np.float32), 2, ""))
                if u_stride <= 0:
                    u_stride = 2
                n_u = u_vals.size // u_stride
                if n_u > 0 and u_stride >= 2:
                    u_src = u_vals[:n_u * u_stride].reshape(n_u, u_stride)[:, :2]
                    u_idx = np.clip(prim_p[:, u_off], 0, n_u - 1)
                    uv_chunks[u_id].append(u_src[u_idx])
                else:
                    uv_chunks[u_id].append(np.zeros((prim_loop_count, 2), dtype=np.float32))

            # Colors (vectorized)
            cur_col_names = {cn for cn, _, _ in col_inputs}
            for cn in col_set_order:
                if cn not in cur_col_names:
                    col_chunks[cn].append(np.ones((prim_loop_count, 4), dtype=np.float32))
            for cn, c_src_id, c_off in col_inputs:
                c_vals, c_stride, _ = sources.get(c_src_id, (np.empty(0, dtype=np.float32), 3, ""))
                if c_stride <= 0:
                    c_stride = 3
                n_c = c_vals.size // c_stride
                if n_c > 0 and c_stride >= 3:
                    c_src = c_vals[:n_c * c_stride].reshape(n_c, c_stride)
                    c_idx = np.clip(prim_p[:, c_off], 0, n_c - 1)
                    if c_stride >= 4:
                        col_chunks[cn].append(c_src[c_idx, :4])
                    else:
                        rgb = c_src[c_idx, :3]
                        rgba = np.ones((prim_loop_count, 4), dtype=np.float32)
                        rgba[:, :3] = rgb
                        col_chunks[cn].append(rgba)
                else:
                    col_chunks[cn].append(np.ones((prim_loop_count, 4), dtype=np.float32))

            total_faces += prim_face_count
            total_loops += prim_loop_count

        if invalid_vcount_total > 0:
            self.warnings.append(
                f"Geometry '{geom_name}': {invalid_vcount_total} invalid faces with fewer than 3 vertices skipped"
            )
        if dropped_faces_total > 0:
            self.warnings.append(
                f"Geometry '{geom_name}': dropped {dropped_faces_total} face(s) with out-of-range indices"
            )

        # Concatenate per-face and per-loop arrays and drop degenerate/duplicate faces
        # BEFORE building the Blender mesh so UVs, vertex colors, and custom normals
        # on all remaining faces are preserved instead of wiped by me.validate().
        all_vcounts: np.ndarray | None = None
        all_loop_verts: np.ndarray | None = None
        all_face_mats: np.ndarray | None = None
        all_face_sharp: np.ndarray | None = None
        all_loop_normals = (
            (np.vstack(loop_norm_chunks) if len(loop_norm_chunks) > 1 else loop_norm_chunks[0])
            if (has_all_loop_normals and loop_norm_chunks)
            else None
        )
        uv_arrays: dict[str, np.ndarray] = {}
        for u_id in uv_set_order:
            chunks = uv_chunks[u_id]
            if chunks:
                uv_arrays[u_id] = np.vstack(chunks) if len(chunks) > 1 else chunks[0]
        col_arrays: dict[str, np.ndarray] = {}
        for cn in col_set_order:
            chunks = col_chunks[cn]
            if chunks:
                col_arrays[cn] = np.vstack(chunks) if len(chunks) > 1 else chunks[0]

        if total_faces > 0:
            all_vcounts = np.concatenate(vcount_chunks) if len(vcount_chunks) > 1 else vcount_chunks[0]
            all_loop_verts = np.concatenate(loop_vert_chunks) if len(loop_vert_chunks) > 1 else loop_vert_chunks[0]
            all_face_mats = np.concatenate(face_mat_chunks) if len(face_mat_chunks) > 1 else face_mat_chunks[0]
            if face_sharp_chunks:
                all_face_sharp = np.concatenate(face_sharp_chunks) if len(face_sharp_chunks) > 1 else face_sharp_chunks[0]

            keep_face: np.ndarray | None = None
            n_degen = 0
            n_dups = 0
            if bool((all_vcounts == 3).all()):
                tris = all_loop_verts.reshape(total_faces, 3)
                non_degen = (tris[:, 0] != tris[:, 1]) & (tris[:, 1] != tris[:, 2]) & (tris[:, 0] != tris[:, 2])
                n_degen = int((~non_degen).sum())
                valid_idx = np.flatnonzero(non_degen)
                if valid_idx.size > 0:
                    s_tris = np.ascontiguousarray(np.sort(tris[valid_idx], axis=1))
                    s_view = s_tris.view(np.dtype((np.void, s_tris.dtype.itemsize * 3))).ravel()
                    _, first_pos = np.unique(s_view, return_index=True)
                    n_dups = int(valid_idx.size - first_pos.size)
                    if n_degen > 0 or n_dups > 0:
                        keep_face = np.zeros(total_faces, dtype=bool)
                        keep_face[valid_idx[first_pos]] = True
                elif n_degen > 0:
                    keep_face = np.zeros(total_faces, dtype=bool)
            elif bool((all_vcounts == 4).all()):
                quads = all_loop_verts.reshape(total_faces, 4)
                s_all = np.ascontiguousarray(np.sort(quads, axis=1))
                non_degen = (
                    (s_all[:, 0] != s_all[:, 1])
                    & (s_all[:, 1] != s_all[:, 2])
                    & (s_all[:, 2] != s_all[:, 3])
                )
                n_degen = int((~non_degen).sum())
                valid_idx = np.flatnonzero(non_degen)
                if valid_idx.size > 0:
                    s_valid = np.ascontiguousarray(s_all[valid_idx])
                    s_view = s_valid.view(np.dtype((np.void, s_valid.dtype.itemsize * 4))).ravel()
                    _, first_pos = np.unique(s_view, return_index=True)
                    n_dups = int(valid_idx.size - first_pos.size)
                    if n_degen > 0 or n_dups > 0:
                        keep_face = np.zeros(total_faces, dtype=bool)
                        keep_face[valid_idx[first_pos]] = True
                elif n_degen > 0:
                    keep_face = np.zeros(total_faces, dtype=bool)
            else:
                kf = np.ones(total_faces, dtype=bool)
                lv_list_chk = all_loop_verts.tolist()
                seen_faces: set[tuple[int, ...]] = set()
                cur_chk = 0
                for fi, vc in enumerate(all_vcounts.tolist()):
                    sv = tuple(sorted(lv_list_chk[cur_chk : cur_chk + vc]))
                    cur_chk += vc
                    if any(sv[k] == sv[k + 1] for k in range(vc - 1)):
                        kf[fi] = False
                        n_degen += 1
                    elif sv in seen_faces:
                        kf[fi] = False
                        n_dups += 1
                    else:
                        seen_faces.add(sv)
                if n_degen > 0 or n_dups > 0:
                    keep_face = kf

            if keep_face is not None:
                keep_loop = np.repeat(keep_face, all_vcounts)
                all_vcounts = all_vcounts[keep_face]
                all_loop_verts = all_loop_verts[keep_loop]
                all_face_mats = all_face_mats[keep_face]
                if all_face_sharp is not None:
                    all_face_sharp = all_face_sharp[keep_face]
                if all_loop_normals is not None and len(all_loop_normals) == keep_loop.size:
                    all_loop_normals = all_loop_normals[keep_loop]
                for u_id in list(uv_arrays.keys()):
                    if len(uv_arrays[u_id]) == keep_loop.size:
                        uv_arrays[u_id] = uv_arrays[u_id][keep_loop]
                for cn in list(col_arrays.keys()):
                    if len(col_arrays[cn]) == keep_loop.size:
                        col_arrays[cn] = col_arrays[cn][keep_loop]
                total_faces = int(all_vcounts.size)
                total_loops = int(all_loop_verts.size)
                parts = []
                if n_dups > 0:
                    parts.append(f"{n_dups} duplicate")
                if n_degen > 0:
                    parts.append(f"{n_degen} degenerate")
                self.warnings.append(
                    f"Geometry '{geom_name}': dropped {' and '.join(parts)} face(s)"
                )

        mat_prims: dict[str, np.ndarray] = {}
        if total_faces > 0 and all_face_mats is not None:
            for sym_id, sym in enumerate(mat_sym_order):
                f_ids = np.flatnonzero(all_face_mats == sym_id)
                if f_ids.size > 0:
                    mat_prims[sym] = f_ids

        # Construct Blender Mesh with foreach_set (fast path when no loose edges)
        me = bpy.data.meshes.new(name=geom_name)
        if num_verts > 0:
            if pos_stride == 3:
                pos_flat = pos_vals[:num_verts * 3]
            else:
                p_mat = np.zeros((num_verts, 3), dtype=np.float32)
                cols_k = min(pos_stride, 3)
                p_mat[:, :cols_k] = pos_vals[:num_verts * pos_stride].reshape(num_verts, pos_stride)[:, :cols_k]
                pos_flat = p_mat.ravel()
        else:
            pos_flat = np.empty(0, dtype=np.float32)

        if not loose_edges and total_faces > 0 and all_vcounts is not None and all_loop_verts is not None:
            loop_starts = np.empty(total_faces, dtype=np.int32)
            loop_starts[0] = 0
            if total_faces > 1:
                np.cumsum(all_vcounts[:-1], out=loop_starts[1:])

            me.vertices.add(num_verts)
            me.vertices.foreach_set("co", pos_flat)
            me.loops.add(total_loops)
            me.loops.foreach_set("vertex_index", all_loop_verts)
            me.polygons.add(total_faces)
            me.polygons.foreach_set("loop_start", loop_starts)
            me.polygons.foreach_set("loop_total", all_vcounts)
            me.update(calc_edges=True)
        else:
            verts_co = pos_flat.reshape(num_verts, 3).tolist() if num_verts > 0 else []
            face_vert_indices: list[list[int]] = []
            if total_faces > 0 and all_vcounts is not None and all_loop_verts is not None:
                lv_list = all_loop_verts.tolist()
                cur = 0
                for vc in all_vcounts.tolist():
                    face_vert_indices.append(lv_list[cur:cur + vc])
                    cur += vc
            me.from_pydata(verts_co, loose_edges, face_vert_indices)

        if me.validate(clean_customdata=False):
            self.warnings.append(f"Geometry '{geom_name}' had invalid data and was repaired")

        # Smooth/flat face flags (foreach_set)
        cur_n_loops = len(me.loops)
        cur_n_polys = len(me.polygons)
        if total_faces > 0 and cur_n_polys == total_faces:
            if self.settings.sharp_edges_from_normals and all_loop_normals is not None and len(all_loop_normals) == cur_n_loops:
                me.polygons.foreach_set("use_smooth", np.ones(total_faces, dtype=bool))
            elif all_face_sharp is not None:
                me.polygons.foreach_set("use_smooth", np.logical_not(all_face_sharp))

        loops_match = (cur_n_loops == total_loops)
        if not loops_match and (uv_set_order or col_set_order or all_loop_normals is not None):
            self.warnings.append(
                f"Geometry '{geom_name}': skipped per-loop UVs/colors/normals after mesh repair "
                f"(loop count changed from {total_loops} to {cur_n_loops})"
            )

        if loops_match:
            # UVs (foreach_set)
            for u_id in uv_set_order:
                uv_arr = uv_arrays.get(u_id)
                if uv_arr is None:
                    continue
                if len(uv_arr) == cur_n_loops:
                    if not np.isfinite(uv_arr).all():
                        uv_arr = np.where(np.isfinite(uv_arr), uv_arr, np.float32(0.0))
                    uv_layer = me.uv_layers.new(name=u_id)
                    uv_layer.data.foreach_set("uv", uv_arr.ravel())

            # Byte Color Attributes (foreach_set)
            for cn in col_set_order:
                c_arr = col_arrays.get(cn)
                if c_arr is None:
                    continue
                if len(c_arr) == cur_n_loops:
                    if not np.isfinite(c_arr).all():
                        c_arr = np.where(np.isfinite(c_arr), c_arr, np.float32(0.0))
                    cattr = me.color_attributes.new(name=cn, type="BYTE_COLOR", domain="CORNER")
                    cattr.data.foreach_set("color_srgb", c_arr.ravel())

            # Custom normals / Clean sharp edges from normals
            if all_loop_normals is not None and len(all_loop_normals) == cur_n_loops and len(all_loop_normals) > 0:
                if not np.isfinite(all_loop_normals).all():
                    all_loop_normals = np.where(np.isfinite(all_loop_normals), all_loop_normals, np.float32(0.0))
                if self.settings.sharp_edges_from_normals:
                    self._apply_sharp_edges_from_normals(me, all_loop_normals, self.settings.sharp_angle_threshold)
                elif self.settings.custom_normals:
                    if self.settings.fast_exact_normals:
                        try:
                            cn = me.attributes.get("custom_normal")
                            if cn is not None and (cn.data_type != "FLOAT_VECTOR" or cn.domain != "CORNER"):
                                me.attributes.remove(cn)
                                cn = None
                            if cn is None:
                                cn = me.attributes.new(name="custom_normal", type="FLOAT_VECTOR", domain="CORNER")
                            cn.data.foreach_set("vector", all_loop_normals.ravel())
                        except Exception as e:
                            self.warnings.append(f"Fast exact normals failed on {geom_name}, falling back: {e}")
                            me.normals_split_custom_set(all_loop_normals.tolist())
                    else:
                        try:
                            # Passing a Python list is ~2.5x faster than passing an ndarray to Blender's RNA wrapper
                            me.normals_split_custom_set(all_loop_normals.tolist())
                        except Exception as e:
                            self.warnings.append(f"Could not set custom normals on {geom_name}: {e}")

        me.update()
        self.meshes_by_id[geom_id] = me
        self.mesh_mat_prims[geom_id] = mat_prims

    @staticmethod
    def _apply_sharp_edges_from_normals(
        me: Mesh, loop_normals: list[tuple[float, float, float]] | np.ndarray, threshold_rad: float
    ) -> None:
        """Mark sharp_edge directly from loop normal discontinuities across each edge (vectorized)."""
        n_edges = len(me.edges)
        if n_edges == 0:
            return

        sharp_flags = np.zeros(n_edges, dtype=bool)
        n_loops = len(me.loops)
        n_polys = len(me.polygons)

        if n_loops > 0 and n_polys > 0:
            loop_verts = np.empty(n_loops, dtype=np.int32)
            me.loops.foreach_get("vertex_index", loop_verts)
            loop_edges = np.empty(n_loops, dtype=np.int32)
            me.loops.foreach_get("edge_index", loop_edges)

            poly_starts = np.empty(n_polys, dtype=np.int32)
            me.polygons.foreach_get("loop_start", poly_starts)
            poly_totals = np.empty(n_polys, dtype=np.int32)
            me.polygons.foreach_get("loop_total", poly_totals)

            arange_l = np.arange(n_loops, dtype=np.int32)
            next_loop = arange_l + 1
            poly_ends = poly_starts + poly_totals - 1
            next_loop[poly_ends] = poly_starts

            # Canonicalize per-loop endpoint order by vertex index so both sides of an edge pair matching ends
            v0 = loop_verts
            v1 = loop_verts[next_loop]
            swap = v0 > v1
            l_lo = np.where(swap, next_loop, arange_l)
            l_hi = np.where(swap, arange_l, next_loop)

            norms = np.asarray(loop_normals, dtype=np.float64).reshape(n_loops, 3)
            lens_sq = np.einsum("ij,ij->i", norms, norms)
            valid = lens_sq > 0.0
            unit = np.zeros_like(norms)
            unit[valid] = norms[valid] / np.sqrt(lens_sq[valid])[:, None]

            order = np.argsort(loop_edges, kind="stable")
            e_sorted = loop_edges[order]
            counts = np.bincount(e_sorted, minlength=n_edges)
            e_starts = np.empty(n_edges, dtype=np.int32)
            e_starts[0] = 0
            if n_edges > 1:
                np.cumsum(counts[:-1], out=e_starts[1:])

            # Fast path: manifold edges (exactly 2 loops)
            m2 = np.flatnonzero(counts == 2)
            if m2.size > 0:
                s2 = e_starts[m2]
                la = order[s2]
                lb = order[s2 + 1]
                ia0 = l_lo[la]
                ia1 = l_hi[la]
                ib0 = l_lo[lb]
                ib1 = l_hi[lb]
                dot0 = np.clip(np.einsum("ij,ij->i", unit[ia0], unit[ib0]), -1.0, 1.0)
                dot1 = np.clip(np.einsum("ij,ij->i", unit[ia1], unit[ib1]), -1.0, 1.0)
                ang0 = np.where(valid[ia0] & valid[ib0], np.arccos(dot0), 0.0)
                ang1 = np.where(valid[ia1] & valid[ib1], np.arccos(dot1), 0.0)
                sharp_flags[m2] = (ang0 + ang1) * 0.5 > threshold_rad

            # Non-manifold edges (3+ loops): compare first face pair against remaining pairs
            m3 = np.flatnonzero(counts >= 3)
            if m3.size > 0:
                for ei in m3.tolist():
                    st = int(e_starts[ei])
                    cnt = int(counts[ei])
                    la_i = int(order[st])
                    ia0_i = int(l_lo[la_i])
                    ia1_i = int(l_hi[la_i])
                    ua0 = unit[ia0_i]
                    ua1 = unit[ia1_i]
                    va0 = bool(valid[ia0_i])
                    va1 = bool(valid[ia1_i])
                    for k in range(1, cnt):
                        lb_i = int(order[st + k])
                        ib0_i = int(l_lo[lb_i])
                        ib1_i = int(l_hi[lb_i])
                        ang0_f = (
                            math.acos(max(-1.0, min(1.0, float(np.dot(ua0, unit[ib0_i])))))
                            if (va0 and valid[ib0_i])
                            else 0.0
                        )
                        ang1_f = (
                            math.acos(max(-1.0, min(1.0, float(np.dot(ua1, unit[ib1_i])))))
                            if (va1 and valid[ib1_i])
                            else 0.0
                        )
                        if (ang0_f + ang1_f) * 0.5 > threshold_rad:
                            sharp_flags[ei] = True
                            break

        se = me.attributes.get("sharp_edge")
        if se is None:
            se = me.attributes.new(name="sharp_edge", type="BOOLEAN", domain="EDGE")
        se.data.foreach_set("value", sharp_flags)
