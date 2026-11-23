# SPDX-FileCopyrightText: 2012 GarageGames, LLC (Torque3D, MIT)
# SPDX-FileCopyrightText: 2026 Blender5-Collada Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Blender 5.0+ scene builder for BeamNG Cached Collada (.cdae) v31 shapes."""

from __future__ import annotations

import math
from dataclasses import dataclass

import bpy
import numpy as np
from mathutils import Matrix, Quaternion, Vector

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
    merge_by_distance: bool = True
    merge_distance: float = 1e-5
    sharp_edges_from_normals: bool = True
    sharp_angle_threshold: float = math.radians(1.0)


def _quat16_to_blender(q16: np.ndarray) -> Quaternion:
    """Convert Torque3D Quat16 (x, y, z, w int16 / 32767) to Blender Quaternion(w, -x, -y, -z)."""
    qx = float(q16[0]) / 32767.0
    qy = float(q16[1]) / 32767.0
    qz = float(q16[2]) / 32767.0
    qw = float(q16[3]) / 32767.0
    q = Quaternion((qw, -qx, -qy, -qz))
    if q.length_squared > 0.0:
        q.normalize()
    else:
        q.identity()
    return q


def _safe_name(names: list[str], idx: int, fallback: str) -> str:
    if 0 <= idx < len(names) and names[idx]:
        return names[idx]
    return fallback


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
    return all_tris, all_mats


def _merge_verts_by_distance(
    verts: np.ndarray,
    tris: np.ndarray,
    tol: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Merge duplicate positions within a single mesh using quantized coordinates.

    Returns (merged_verts, remapped_tris, kept_tri_mask, old_to_new_vert_idx).
    """
    if len(verts) == 0 or tol <= 0.0:
        old_to_new = np.arange(len(verts), dtype=np.int32)
        return verts, tris, np.ones(len(tris), dtype=bool), old_to_new

    quant = np.round(verts.astype(np.float64) / tol).astype(np.int64)
    _, unique_idx, inverse = np.unique(quant, axis=0, return_index=True, return_inverse=True)
    # Preserve deterministic vertex order
    order = np.argsort(unique_idx)
    inv_order = np.empty_like(order)
    inv_order[order] = np.arange(len(order))
    merged_verts = verts[unique_idx[order]]
    old_to_new = inv_order[inverse].astype(np.int32)

    remapped = old_to_new[tris]
    kept = (remapped[:, 0] != remapped[:, 1]) & (remapped[:, 0] != remapped[:, 2]) & (remapped[:, 1] != remapped[:, 2])
    return merged_verts, remapped[kept], kept, old_to_new


class CdaeImporter:
    """Imports a BeamNG .cdae v31 file into the active Blender scene."""

    def __init__(self, context, settings: CdaeImportSettings) -> None:
        self.context = context
        self.scene = context.scene
        self.view_layer = context.view_layer
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

        # Create/reuse materials by name
        materials: list = []
        for mat in shape.materials:
            ma = bpy.data.materials.get(mat.name)
            if ma is None:
                ma = bpy.data.materials.new(name=mat.name)
            materials.append(ma)

        # Select detail levels to import
        selected_details = self._select_details(shape)

        # Create node hierarchy (Empties) in scene collection
        node_empties: list = []
        for i in range(n_nodes):
            node_name = _safe_name(shape.names, int(shape.nodes[i]["name_index"]), f"Node_{i}")
            ob_empty = bpy.data.objects.new(name=node_name, object_data=None)
            self.scene.collection.objects.link(ob_empty)
            ob_empty.matrix_basis = local_mats[i]
            ob_empty.select_set(True)
            node_empties.append(ob_empty)

        for i in range(n_nodes):
            parent_idx = int(shape.nodes[i]["parent_index"])
            if 0 <= parent_idx < n_nodes and parent_idx != i:
                node_empties[i].parent = node_empties[parent_idx]

        # Build an Armature only if any selected mesh is a SkinMesh
        has_skin = any(m.mesh_type == MESH_TYPE_SKIN for m in shape.meshes)
        ob_armature = self._build_armature(shape, world_mats) if (has_skin and n_nodes > 0) else None

        # Import objects per selected detail level
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
                    self.scene.collection.children.link(target_coll)
                else:
                    target_coll = self.scene.collection

                for obj_i in range(first_obj, min(first_obj + num_objs, len(shape.objects))):
                    self._import_object_at_detail(
                        shape, obj_i, obj_det, materials, node_empties, ob_armature, target_coll
                    )
        else:
            # Fallback if shape has no details table: import first mesh of every object
            for obj_i in range(len(shape.objects)):
                self._import_object_at_detail(
                    shape, obj_i, 0, materials, node_empties, ob_armature, self.scene.collection
                )

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
        node_empties: list,
        ob_armature,
        target_coll,
    ) -> None:
        obj = shape.objects[obj_i]
        num_meshes = int(obj["num_meshes"])
        start_mesh = int(obj["start_mesh_index"])
        if obj_det < 0 or obj_det >= num_meshes:
            return
        mesh_idx = start_mesh + obj_det
        if mesh_idx < 0 or mesh_idx >= len(shape.meshes):
            return
        c_mesh = shape.meshes[mesh_idx]
        if c_mesh.mesh_type == MESH_TYPE_NULL:
            return

        obj_name = _safe_name(shape.names, int(obj["name_index"]), f"Object_{obj_i}")
        me, old_to_new = self._build_blender_mesh(c_mesh, obj_name, materials)
        if me is None:
            return

        ob = bpy.data.objects.new(name=obj_name, object_data=me)
        target_coll.objects.link(ob)
        ob.select_set(True)
        self.view_layer.objects.active = ob

        node_idx = int(obj["node_index"])
        if c_mesh.mesh_type == MESH_TYPE_SKIN and ob_armature is not None:
            ob.parent = ob_armature
            self._apply_skin_weights(shape, c_mesh, ob, ob_armature, old_to_new)
        elif 0 <= node_idx < len(node_empties):
            ob.parent = node_empties[node_idx]

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

        # Merge vertices by position within this mesh (required for sharp_edges_from_normals on GPU-split meshes)
        if self.settings.merge_by_distance and self.settings.merge_distance > 0.0:
            verts, tris, kept, old_to_new = _merge_verts_by_distance(verts, tris, self.settings.merge_distance)
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
                if self.settings.sharp_edges_from_normals:
                    DocumentImporter._apply_sharp_edges_from_normals(
                        me, norm_loops, self.settings.sharp_angle_threshold
                    )
                else:
                    try:
                        me.normals_split_custom_set(norm_loops.tolist())
                    except Exception as exc:
                        self.warnings.append(f"Could not set custom normals on '{mesh_name}': {exc}")

        me.update()
        return me, old_to_new

    def _build_armature(self, shape: CdaeShape, world_mats: list[Matrix | None]):
        arm_data = bpy.data.armatures.new(name="Armature")
        ob_arm = bpy.data.objects.new(name="Armature", object_data=arm_data)
        self.scene.collection.objects.link(ob_arm)
        self.view_layer.objects.active = ob_arm
        ob_arm.select_set(True)

        bpy.ops.object.mode_set(mode="EDIT")
        edit_bones: list = []
        n_nodes = len(shape.nodes)
        for i in range(n_nodes):
            bname = _safe_name(shape.names, int(shape.nodes[i]["name_index"]), f"Bone_{i}")
            eb = arm_data.edit_bones.new(bname)
            wm = world_mats[i] or Matrix.Identity(4)
            head = wm.translation.copy()
            eb.head = head
            eb.tail = head + (wm.to_3x3() @ Vector((0.0, 0.2, 0.0)))
            if (eb.tail - eb.head).length < 1e-4:
                eb.tail = head + Vector((0.0, 0.2, 0.0))
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
