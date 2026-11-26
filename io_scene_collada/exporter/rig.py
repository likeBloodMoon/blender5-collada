# SPDX-FileCopyrightText: 2009-2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Rest-pose matrices, skin/morph controllers, bone hierarchy, and <instance_controller>."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from mathutils import Euler, Matrix, Vector

from ..collada_utils import (
    LIMITED_PRECISION,
    bc_add_global_transform_mat,
    bc_add_global_transform_vec,
    bc_apply_global_transform_mat,
    bc_decompose,
    get_assigned_armature,
    get_bone_property_matrix,
    get_controller_id,
    get_geometry_id,
    get_joint_sid,
    get_mesh_copy,
    get_morph_controller_id,
    is_leaf_bone,
    is_root_bone,
    matrix_to_dae_flat,
    sanitize_matrix,
)
from ..xml_writer import encode_xml, fmt_f32, fmt_float, fmt_int

if TYPE_CHECKING:
    from bpy.types import Bone, Object

def _get_object_local_matrix(ob: Object) -> Matrix:
    """Match BKE_object_matrix_local_get(ob, mat)."""
    return ob.matrix_local.copy()


def _create_restpose_mat(settings, bone: Bone, from_mat: Matrix, use_local_space: bool) -> Matrix:
    """Port of bc_create_restpose_mat()."""
    if not settings.keep_bind_info:
        return from_mat.copy()

    def has_prop_vec(b: Bone, key: str) -> bool:
        return (f"{key}_x" in b) or (f"{key}_y" in b) or (f"{key}_z" in b)

    if not (
        has_prop_vec(bone, "restpose_loc")
        or has_prop_vec(bone, "restpose_rot")
        or has_prop_vec(bone, "restpose_scale")
    ):
        return from_mat.copy()

    loc, rot, _, scale = bc_decompose(from_mat)
    loc = Vector((
        float(bone.get("restpose_loc_x", loc.x)),
        float(bone.get("restpose_loc_y", loc.y)),
        float(bone.get("restpose_loc_z", loc.z)),
    ))
    if use_local_space and bone.parent:
        b = bone.parent
        while b is not None:
            loc.x += float(b.get("restpose_loc_x", 0.0))
            loc.y += float(b.get("restpose_loc_y", 0.0))
            loc.z += float(b.get("restpose_loc_z", 0.0))
            b = b.parent

    if "restpose_rot_x" in bone:
        rot.x = math.radians(float(bone["restpose_rot_x"]))
    if "restpose_rot_y" in bone:
        rot.y = math.radians(float(bone["restpose_rot_y"]))
    if "restpose_rot_z" in bone:
        rot.z = math.radians(float(bone["restpose_rot_z"]))

    scale = Vector((
        float(bone.get("restpose_scale_x", scale.x)),
        float(bone.get("restpose_scale_y", scale.y)),
        float(bone.get("restpose_scale_z", scale.z)),
    ))

    return Matrix.LocRotScale(loc, rot, scale)


class RigMixin:
    def _export_controllers(self) -> None:
        self.sw.open_element("library_controllers")
        for ob in self.export_set:
            if ob.type != "MESH" or not ob.data:
                continue
            ob_arm = get_assigned_armature(ob)
            if ob_arm is not None:
                self._export_skin_controller(ob, ob_arm)
            if self.settings.include_shapekeys and ob.data.shape_keys is not None:
                if len(ob.data.shape_keys.key_blocks) > 1:
                    self._export_morph_controller(ob)
        self.sw.close_element()

    def _export_skin_controller(self, ob: Object, ob_arm: Object) -> None:
        # Check if mesh has any vertex group weights
        if len(ob.vertex_groups) == 0:
            return
        mesh, needs_free = get_mesh_copy(
            self.depsgraph,
            ob,
            self.settings.apply_modifiers,
            self.settings.triangulate,
            keep_normals=False,
            keep_quads=self.settings.keep_quads,
            eval_mode=self.settings.export_mesh_type,
            readonly=True,
        )
        if needs_free:
            self._temp_meshes.append(mesh)
        has_any_weights = any(len(v.groups) > 0 for v in mesh.vertices)
        if not has_any_weights:
            return

        controller_id = get_controller_id(self.id_map, ob_arm, ob)
        controller_name = encode_xml(ob_arm.name)
        geom_id, _ = self._get_object_geometry_id(ob)

        self.sw.open_element("controller", [("id", controller_id), ("name", controller_name)])
        self.sw.open_element("skin", [("source", f"#{geom_id}")])

        # <bind_shape_matrix>
        f_obmat = _get_object_local_matrix(ob)
        if not self.settings.apply_global_orientation:
            f_obmat = bc_add_global_transform_mat(f_obmat, self.settings.global_transform)
        bvals = matrix_to_dae_flat(f_obmat, self.settings.limit_precision)
        self.sw.open_element("bind_shape_matrix")
        self.sw.append_values(fmt_float(v) for v in bvals)
        self.sw.close_element()

        # Map vertex groups -> bones
        arm = ob_arm.data
        bone_vgroups: list[tuple[int, Bone]] = []
        joint_index_by_vg: list[int] = []
        for vg in ob.vertex_groups:
            bone = arm.bones.get(vg.name)
            if bone is not None:
                joint_index_by_vg.append(len(bone_vgroups))
                bone_vgroups.append((vg.index, bone))
            else:
                joint_index_by_vg.append(-1)

        # 1. Joints Name_array source
        joints_src_id = f"{controller_id}-joints"
        joints_arr_id = f"{joints_src_id}-array"
        self.sw.open_element("source", [("id", joints_src_id)])
        self.sw.open_element("Name_array", [("id", joints_arr_id), ("count", fmt_int(len(bone_vgroups)))])
        self.sw.append_values(get_joint_sid(self.id_map, b.name) for _, b in bone_vgroups)
        self.sw.close_element()
        self.sw.open_element("technique_common")
        self.sw.open_element("accessor", [("source", f"#{joints_arr_id}"), ("count", fmt_int(len(bone_vgroups))), ("stride", "1")])
        self.sw.write_element("param", None, [("name", "JOINT"), ("type", "name")])
        self.sw.close_element()
        self.sw.close_element()
        self.sw.close_element()

        # 2. Inverse bind matrices source
        bind_src_id = f"{controller_id}-bind_poses"
        bind_arr_id = f"{bind_src_id}-array"
        bind_vals: list[str] = []
        arm_world = ob_arm.matrix_world
        for _, bone in bone_vgroups:
            bind_mat = get_bone_property_matrix(bone, "bind_mat")
            has_bindmat = bind_mat is not None
            if not has_bindmat:
                bind_mat = _create_restpose_mat(self.settings, bone, bone.matrix_local, True)
                if self.settings.open_sim:
                    loc, _, _, scale = bc_decompose(bind_mat)
                    bind_mat = Matrix.LocRotScale(loc, Euler((0.0, 0.0, 0.0), "XYZ"), scale)

            world = arm_world @ bind_mat
            if not has_bindmat and self.settings.apply_global_orientation:
                world = bc_apply_global_transform_mat(world, self.settings.global_transform)
            inv_world = world.inverted()
            flat = matrix_to_dae_flat(inv_world, self.settings.limit_precision)
            for fv in flat:
                bind_vals.append(fmt_f32(fv))

        self._write_float_source(
            bind_src_id,
            bind_arr_id,
            len(bone_vgroups),
            16,
            ("TRANSFORM",),
            bind_vals,
            param_type="float4x4",
        )

        # 3. Vertex weights collection (normalized per vertex)
        vcounts: list[int] = []
        joints_out: list[int] = []
        weights_out: list[float] = []
        for v in mesh.vertices:
            jw: dict[int, float] = {}
            sumw = 0.0
            for g in v.groups:
                idx = g.group
                if 0 <= idx < len(joint_index_by_vg):
                    ji = joint_index_by_vg[idx]
                    if ji != -1 and g.weight > 0.0:
                        jw[ji] = jw.get(ji, 0.0) + g.weight
                        sumw += g.weight
            if sumw > 0.0:
                invsum = 1.0 / sumw
                vcounts.append(len(jw))
                for ji in sorted(jw.keys()):
                    joints_out.append(ji)
                    weights_out.append(invsum * jw[ji])
            else:
                vcounts.append(0)

        weights_src_id = f"{controller_id}-weights"
        weights_arr_id = f"{weights_src_id}-array"
        self._write_float_source(
            weights_src_id,
            weights_arr_id,
            len(weights_out),
            1,
            ("WEIGHT",),
            [fmt_f32(w) for w in weights_out],
        )

        # <joints>
        self.sw.open_element("joints")
        self.sw.write_element("input", None, [("semantic", "JOINT"), ("source", f"#{joints_src_id}")])
        self.sw.write_element("input", None, [("semantic", "INV_BIND_MATRIX"), ("source", f"#{bind_src_id}")])
        self.sw.close_element()

        # <vertex_weights>
        self.sw.open_element("vertex_weights", [("count", fmt_int(len(vcounts)))])
        self.sw.write_element("input", None, [("semantic", "JOINT"), ("source", f"#{joints_src_id}"), ("offset", "0")])
        self.sw.write_element("input", None, [("semantic", "WEIGHT"), ("source", f"#{weights_src_id}"), ("offset", "1")])
        self.sw.open_element("vcount")
        self.sw.append_text(" ".join(fmt_int(vc) for vc in vcounts) + " ")
        self.sw.close_element()
        self.sw.open_element("v")
        v_pairs: list[str] = []
        for wi, ji in enumerate(joints_out):
            v_pairs.append(fmt_int(ji))
            v_pairs.append(fmt_int(wi))
        self.sw.append_values(v_pairs)
        self.sw.close_element()
        self.sw.close_element()

        self.sw.close_element()  # skin
        self.sw.close_element()  # controller

    def _export_morph_controller(self, ob: Object) -> None:
        key = ob.data.shape_keys
        skipped = getattr(self, "_skipped_morph_keys", {}).get(ob.name, ())
        kbs = [kb for kb in list(key.key_blocks)[1:] if kb.name not in skipped]
        if not kbs:
            return
        controller_id = get_morph_controller_id(self.id_map, ob)
        controller_name = encode_xml(f"{ob.name}-morph")
        geom_id, _ = self._get_object_geometry_id(ob)

        self.sw.open_element("controller", [("id", controller_id), ("name", controller_name)])
        self.sw.open_element("morph", [("source", f"#{geom_id}"), ("method", "NORMALIZED")])

        # Targets
        targets_id = f"{self.id_map.translate_id(ob.name)}-targets"
        targets_arr_id = f"{targets_id}-array"
        self.sw.open_element("source", [("id", targets_id)])
        self.sw.open_element("IDREF_array", [("id", targets_arr_id), ("count", fmt_int(len(kbs)))])
        self.sw.append_values(
            f"{get_geometry_id(self.id_map, ob, False)}_morph_{self.id_map.translate_id(kb.name)}"
            for kb in kbs
        )
        self.sw.close_element()
        self.sw.open_element("technique_common")
        self.sw.open_element("accessor", [("source", f"#{targets_arr_id}"), ("count", fmt_int(len(kbs))), ("stride", "1")])
        self.sw.write_element("param", None, [("name", "IDREF"), ("type", "IDREF")])
        self.sw.close_element()
        self.sw.close_element()
        self.sw.close_element()

        # Weights
        weights_id = f"{self.id_map.translate_id(ob.name)}-weights"
        weights_arr_id = f"{weights_id}-array"
        self._write_float_source(
            weights_id,
            weights_arr_id,
            len(kbs),
            1,
            ("MORPH_WEIGHT",),
            [fmt_f32(kb.value) for kb in kbs],
        )

        # <targets>
        self.sw.open_element("targets")
        self.sw.write_element("input", None, [("semantic", "MORPH_TARGET"), ("source", f"#{targets_id}")])
        self.sw.write_element("input", None, [("semantic", "MORPH_WEIGHT"), ("source", f"#{weights_id}")])
        self.sw.close_element()

        self.sw.close_element()  # morph
        self.sw.close_element()  # controller

    def _write_instance_controller(self, ob: Object, ob_arm: Object) -> bool:
        if len(ob.vertex_groups) == 0:
            return False
        if not any(len(v.groups) > 0 for v in ob.data.vertices):
            return False
        controller_id = get_controller_id(self.id_map, ob_arm, ob)
        self.sw.open_element("instance_controller", [("url", f"#{controller_id}")])
        for bone in ob_arm.data.bones:
            if bone.parent is None:
                self._write_skeleton_urls(ob_arm, bone)
        self._write_material_bindings(ob)
        self.sw.close_element()
        return True

    def _write_skeleton_urls(self, ob_arm: Object, bone: Bone) -> None:
        if is_root_bone(bone, self.settings.deform_bones_only):
            joint_id = self.id_map.translate_id(f"{ob_arm.name}_{bone.name}")
            self.sw.write_element("skeleton", f"#{joint_id}")
        else:
            for child in bone.children:
                self._write_skeleton_urls(ob_arm, child)

    def _collect_armature_collection_extras(self, ob_arm: Object) -> list[tuple[str, str | None, str | None, str]]:
        arm = ob_arm.data
        params: list[tuple[str, str | None, str | None, str]] = []
        all_names = [bc.name for bc in arm.collections]
        vis_names = [bc.name for bc in arm.collections if bc.is_visible]
        if all_names:
            params.append(("collections", "collections", "string", encode_xml("\n".join(all_names))))
        if vis_names:
            params.append(("visible_collections", "visible_collections", "string", encode_xml("\n".join(vis_names))))
        if arm.collections.active is not None:
            params.append(("active_collection", "active_collection", "string", encode_xml(arm.collections.active.name)))
        return params

    def _can_export_bone(self, bone: Bone) -> bool:
        if not self.settings.deform_bones_only:
            return True
        if bone.use_deform:
            return True
        return any(self._can_export_bone(c) for c in bone.children)

    def _is_bone_export_root(self, bone: Bone) -> bool:
        p = bone.parent
        while p is not None:
            if self._can_export_bone(p):
                return False
            p = p.parent
        return self._can_export_bone(bone)

    def _write_armature_bones(self, ob_arm: Object, child_objects: list[Object]) -> None:
        arm = ob_arm.data
        for bone in arm.bones:
            if bone.parent is None:
                self._write_bone_node(bone, ob_arm, child_objects)

    def _write_bone_node(self, bone: Bone, ob_arm: Object, child_objects: list[Object]) -> None:
        if not self._can_export_bone(bone):
            for child in bone.children:
                self._write_bone_node(child, ob_arm, child_objects)
            return

        node_id = self.id_map.translate_id(f"{ob_arm.name}_{bone.name}")
        node_name = encode_xml(bone.name)
        node_sid = get_joint_sid(self.id_map, bone.name)

        self.sw.open_element("node", [("id", node_id), ("name", node_name), ("sid", node_sid), ("type", "JOINT")])

        # Bone transform
        mat = get_bone_property_matrix(bone, "rest_mat")
        has_restmat = mat is not None
        if not has_restmat:
            bone_rest = _create_restpose_mat(self.settings, bone, bone.matrix_local, True)
            if self._is_bone_export_root(bone):
                mat = bone_rest.copy()
            else:
                parent_rest = _create_restpose_mat(self.settings, bone.parent, bone.parent.matrix_local, True)
                mat = parent_rest.inverted() @ bone_rest

            if self.settings.open_sim:
                work = bone_rest.to_3x3().to_4x4().inverted()
                mat = mat @ work
                if not self._is_bone_export_root(bone):
                    pwork = parent_rest.to_3x3().to_4x4()
                    mat = pwork @ mat

        if not has_restmat and self.settings.apply_global_orientation:
            mat = bc_apply_global_transform_mat(mat, self.settings.global_transform)

        if self.settings.limit_precision:
            mat = sanitize_matrix(mat, LIMITED_PRECISION)

        if self.settings.object_transformation_type == "matrix":
            vals = matrix_to_dae_flat(mat, False)
            self.sw.open_element("matrix", [("sid", "transform")])
            self.sw.append_values(fmt_float(v) for v in vals)
            self.sw.close_element()
        else:
            loc, rot, _, scale = bc_decompose(mat)
            self._write_transform_decomposed(loc, rot, scale)

        # Bone-parented child objects
        remaining: list[Object] = []
        for child_ob in child_objects:
            if child_ob.parent_type == "BONE" and child_ob.parent_bone == bone.name:
                self._write_node(child_ob)
            else:
                remaining.append(child_ob)
        child_objects[:] = remaining

        # Child bones
        for child in bone.children:
            self._write_bone_node(child, ob_arm, child_objects)

        # Blender profile extra
        if self.settings.use_blender_profile:
            extras: list[tuple[str, str | None, str | None, str]] = []
            if not self._is_bone_export_root(bone) and bone.use_connect:
                extras.append(("connect", "connect", "bool", "1"))
            bcolls = [bc.name for bc in bone.collections]
            if bcolls:
                extras.append(("collections", None, "string", encode_xml("\n".join(bcolls))))
            # Roll from bone local matrix
            _, roll = bone.AxisRollFromMatrix(bone.matrix_local.to_3x3())
            if abs(roll) > 1e-7:
                extras.append(("roll", "roll", "float", fmt_f32(roll)))
            if is_leaf_bone(bone):
                head = bone.head_local.copy()
                tail = bone.tail_local.copy()
                if self.settings.apply_global_orientation:
                    head = bc_add_global_transform_vec(head, self.settings.global_transform)
                    tail = bc_add_global_transform_vec(tail, self.settings.global_transform)
                tip = tail - head
                extras.append(("tip_x", "tip_x", "float", fmt_f32(tip.x)))
                extras.append(("tip_y", "tip_y", "float", fmt_f32(tip.y)))
                extras.append(("tip_z", "tip_z", "float", fmt_f32(tip.z)))
            if extras:
                self.sw.write_extra_technique("blender", extras)

        self.sw.close_element()  # node
