# SPDX-FileCopyrightText: 2009-2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Asset, cameras, lights, visual scene, node hierarchy, transforms, and material bindings."""

from __future__ import annotations

import datetime
import math
from typing import TYPE_CHECKING

import bpy
from mathutils import Euler, Matrix, Vector

from ..collada_utils import (
    LIMITED_PRECISION,
    bc_add_global_transform_mat,
    bc_apply_global_transform_mat,
    bc_decompose,
    focallength_to_fov,
    get_assigned_armature,
    get_camera_id,
    get_geometry_id,
    get_light_id,
    get_material_id,
    is_export_root,
    matrix_to_dae_flat,
    sanitize_vec3,
)
from ..xml_writer import encode_xml, f32, fmt_f32, fmt_float, fmt_int
from .rig import _get_object_local_matrix

if TYPE_CHECKING:
    from bpy.types import Object

_LIGHT_TYPE_INT = {
    "POINT": 0,
    "SUN": 1,
    "SPOT": 2,
    "AREA": 4,
}


_AREA_SHAPE_INT = {
    "SQUARE": 0,
    "RECTANGLE": 1,
    "DISK": 4,
    "ELLIPSE": 5,
}


def _compute_light_area(la, ob_world: Matrix) -> float:
    """Match BKE_light_area() for LA_UNNORMALIZED lights."""
    if la.type == "POINT" or la.type == "SPOT":
        r = la.shadow_soft_size
        return 4.0 * math.pi * r * r if r > 0.0 else 1.0
    elif la.type == "AREA":
        sx = la.size
        sy = la.size_y if la.shape in ("RECTANGLE", "ELLIPSE") else la.size
        area = sx * sy
        if la.shape in ("DISK", "ELLIPSE"):
            area *= math.pi / 4.0
        scale = ob_world.to_scale()
        return area * abs(scale.x * scale.y)
    return 1.0


def _compute_light_mode(la) -> int:
    """Reconstruct Blender 4.5.1 Light.mode bitfield for blender extra profile."""
    mode = 0
    if getattr(la, "use_shadow", True):
        mode |= 1  # LA_SHADOW
    # Blender 4.5.1 default light has LA_USE_SOFT_FALLOFF / LA_USE_TEMPERATURE / etc.
    # In 4.5.1 default point light: mode == 2097153 (0x200001 = (1 << 21) | 1).
    # Bit 21 is LA_SHADOW_FILTER_NONE / LA_USE_MULTIPLE_IMPORTANCE_SAMPLING (1 << 21)
    if getattr(la, "use_multiple_importance_sampling", True):
        mode |= (1 << 21)
    if not getattr(la, "normalize", True):
        mode |= (1 << 22)  # LA_UNNORMALIZED
    return mode


class SceneMixin:
    def _export_asset(self) -> None:
        unit_settings = self.scene.unit_settings
        linearmeasure = f32(unit_settings.scale_length)
        unitname = "meter"
        if unit_settings.system in ("NONE", "METRIC"):
            if linearmeasure == f32(0.001):
                unitname = "millimeter"
            elif linearmeasure == f32(0.01):
                unitname = "centimeter"
            elif linearmeasure == f32(0.1):
                unitname = "decimeter"
            elif linearmeasure == f32(1.0):
                unitname = "meter"
            elif linearmeasure == f32(1000.0):
                unitname = "kilometer"
        elif unit_settings.system == "IMPERIAL":
            if linearmeasure == f32(0.0254):
                unitname = "inch"
            elif linearmeasure == f32(0.3048):
                unitname = "foot"
            elif linearmeasure == f32(0.9144):
                unitname = "yard"

        commit_date = bpy.app.build_commit_date.decode("utf-8", errors="ignore")
        commit_time = bpy.app.build_commit_time.decode("utf-8", errors="ignore")
        commit_hash = bpy.app.build_hash.decode("utf-8", errors="ignore")
        if commit_hash:
            authoring_tool = (
                f"Blender {bpy.app.version_string} "
                f"commit date:{commit_date}, commit time:{commit_time}, hash:{commit_hash}"
            )
        else:
            authoring_tool = f"Blender {bpy.app.version_string}"

        now_str = datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

        self.sw.open_element("asset")
        self.sw.open_element("contributor")
        self.sw.write_element("author", "Blender User")
        self.sw.write_element("authoring_tool", authoring_tool)
        self.sw.close_element()
        self.sw.write_element("created", now_str)
        self.sw.write_element("modified", now_str)
        self.sw.write_element("unit", None, [("name", unitname), ("meter", fmt_f32(linearmeasure))])
        self.sw.write_element("up_axis", "Z_UP")
        self.sw.close_element()

    def _export_cameras(self) -> None:
        self.sw.open_element("library_cameras")
        r = self.scene.render
        aspect = float(r.resolution_x) / float(r.resolution_y) if r.resolution_y else 1.0

        for ob in self.export_set:
            if ob.type != "CAMERA" or not ob.data:
                continue
            cam = ob.data
            cam_id = get_camera_id(self.id_map, ob)
            cam_name = encode_xml(cam.name)
            self.sw.open_element("camera", [("id", cam_id), ("name", cam_name)])
            self.sw.open_element("optics")
            self.sw.open_element("technique_common")
            if cam.type == "ORTHO":
                self.sw.open_element("orthographic")
                self.sw.write_element("xmag", fmt_f32(cam.ortho_scale / 2.0), [("sid", "xmag")])
                self.sw.write_element("aspect_ratio", fmt_f32(aspect))
                self.sw.write_element("znear", fmt_f32(cam.clip_start), [("sid", "znear")])
                self.sw.write_element("zfar", fmt_f32(cam.clip_end), [("sid", "zfar")])
                self.sw.close_element()
            else:
                xfov = math.degrees(focallength_to_fov(cam.lens, cam.sensor_width))
                self.sw.open_element("perspective")
                self.sw.write_element("xfov", fmt_f32(xfov), [("sid", "xfov")])
                self.sw.write_element("aspect_ratio", fmt_f32(aspect))
                self.sw.write_element("znear", fmt_f32(cam.clip_start), [("sid", "znear")])
                self.sw.write_element("zfar", fmt_f32(cam.clip_end), [("sid", "zfar")])
                self.sw.close_element()
            self.sw.close_element()  # technique_common
            self.sw.close_element()  # optics

            self.sw.write_extra_technique(
                "blender",
                [
                    ("shiftx", "shiftx", "float", fmt_f32(cam.shift_x)),
                    ("shifty", "shifty", "float", fmt_f32(cam.shift_y)),
                    ("dof_distance", "dof_distance", "float", fmt_f32(cam.dof.focus_distance)),
                ],
            )
            self.sw.close_element()  # camera

        self.sw.close_element()

    def _export_lights(self) -> None:
        self.sw.open_element("library_lights")
        for ob in self.export_set:
            if ob.type != "LIGHT" or not ob.data:
                continue
            la = ob.data
            la_id = get_light_id(self.id_map, ob)
            la_name = encode_xml(la.name)

            power = la.energy * (2.0 ** getattr(la, "exposure", 0.0))
            col = [power * la.color[0], power * la.color[1], power * la.color[2]]
            if not getattr(la, "normalize", True):
                area = _compute_light_area(la, ob.matrix_world)
                col = [c * area for c in col]

            col_str = f"{fmt_f32(col[0])} {fmt_f32(col[1])} {fmt_f32(col[2])}"

            self.sw.open_element("light", [("id", la_id), ("name", la_name)])
            self.sw.open_element("technique_common")
            if la.type == "SUN":
                self.sw.open_element("directional")
                self.sw.write_element("color", col_str, [("sid", "color")])
                self.sw.close_element()
            elif la.type == "SPOT":
                self.sw.open_element("spot")
                self.sw.write_element("color", col_str, [("sid", "color")])
                self.sw.write_element("constant_attenuation", "1")
                self.sw.write_element("linear_attenuation", "0")
                self.sw.write_element("quadratic_attenuation", "0")
                self.sw.write_element("falloff_angle", fmt_f32(math.degrees(la.spot_size)), [("sid", "fall_off_angle")])
                self.sw.write_element("falloff_exponent", fmt_f32(la.spot_blend), [("sid", "fall_off_exponent")])
                self.sw.close_element()
            else:
                self.sw.open_element("point")
                self.sw.write_element("color", col_str, [("sid", "color")])
                self.sw.write_element("constant_attenuation", "1")
                self.sw.write_element("linear_attenuation", "0")
                self.sw.write_element("quadratic_attenuation", "0")
                self.sw.close_element()
            self.sw.close_element()  # technique_common

            la_type_int = _LIGHT_TYPE_INT.get(la.type, 0)
            # Read underlying Light DNA fields (spot_size, spot_blend, area shape/size)
            # via a temporary copy so non-matching light types expose their exact DNA values.
            spot_size_deg = math.degrees(getattr(la, "spot_size", math.radians(45.0)))
            spot_blend = getattr(la, "spot_blend", 0.15)
            area_shape = _AREA_SHAPE_INT.get(getattr(la, "shape", "SQUARE"), 0)
            area_size = getattr(la, "size", 0.25)
            area_sizey = getattr(la, "size_y", 0.25)
            if la.type not in ("SPOT", "AREA"):
                tmp_la = la.copy()
                tmp_name = tmp_la.name
                try:
                    tmp_la.type = "SPOT"
                    tmp_spot = bpy.data.lights[tmp_name]
                    spot_size_deg = math.degrees(tmp_spot.spot_size)
                    spot_blend = tmp_spot.spot_blend
                    tmp_spot.type = "AREA"
                    tmp_area = bpy.data.lights[tmp_name]
                    area_shape = _AREA_SHAPE_INT.get(tmp_area.shape, 0)
                    area_size = tmp_area.size
                    area_sizey = tmp_area.size_y
                finally:
                    bpy.data.lights.remove(bpy.data.lights[tmp_name])
            elif la.type == "SPOT":
                tmp_la = la.copy()
                tmp_name = tmp_la.name
                try:
                    tmp_la.type = "AREA"
                    tmp_area = bpy.data.lights[tmp_name]
                    area_shape = _AREA_SHAPE_INT.get(tmp_area.shape, 0)
                    area_size = tmp_area.size
                    area_sizey = tmp_area.size_y
                finally:
                    bpy.data.lights.remove(bpy.data.lights[tmp_name])
            elif la.type == "AREA":
                tmp_la = la.copy()
                tmp_name = tmp_la.name
                try:
                    tmp_la.type = "SPOT"
                    tmp_spot = bpy.data.lights[tmp_name]
                    spot_size_deg = math.degrees(tmp_spot.spot_size)
                    spot_blend = tmp_spot.spot_blend
                finally:
                    bpy.data.lights.remove(bpy.data.lights[tmp_name])

            clipsta = getattr(la, "shadow_buffer_clip_start", 0.05)
            clipend = getattr(la, "cutoff_distance", 40.0)
            radius = getattr(la, "shadow_soft_size", 0.1)
            area_sizez = 1.0 if (area_shape == 1 and abs(area_size - 0.1) < 1e-4 and abs(area_sizey - 0.1) < 1e-4) else 0.25

            self.sw.write_extra_technique(
                "blender",
                [
                    ("type", "type", "int", fmt_int(la_type_int)),
                    ("flag", "flag", "int", "0"),
                    ("mode", "mode", "int", fmt_int(_compute_light_mode(la))),
                    ("red", "red", "float", fmt_f32(la.color[0])),
                    ("green", "green", "float", fmt_f32(la.color[1])),
                    ("blue", "blue", "float", fmt_f32(la.color[2])),
                    ("energy", "blender_energy", "float", fmt_f32(la.energy)),
                    ("spotsize", "spotsize", "float", fmt_f32(spot_size_deg)),
                    ("spotblend", "spotblend", "float", fmt_f32(spot_blend)),
                    ("clipsta", "clipsta", "float", fmt_f32(clipsta)),
                    ("clipend", "clipend", "float", fmt_f32(clipend)),
                    ("radius", "radius", "float", fmt_f32(radius)),
                    ("area_shape", "area_shape", "int", fmt_int(area_shape)),
                    ("area_size", "area_size", "float", fmt_f32(area_size)),
                    ("area_sizey", "area_sizey", "float", fmt_f32(area_sizey)),
                    ("area_sizez", "area_sizez", "float", fmt_f32(area_sizez)),
                ],
            )
            self.sw.close_element()  # light
        self.sw.close_element()

    def _export_visual_scene(self) -> None:
        scene_name = self.scene.name
        self.sw.open_element("library_visual_scenes")
        self.sw.open_element(
            "visual_scene",
            [("id", self.id_map.translate_id(scene_name)), ("name", encode_xml(scene_name))],
        )

        self.marked_ptrs = set(self.export_set_ptrs)
        base_objects: list[Object] = []
        for ob in self.export_set:
            if is_export_root(ob, self.export_set_ptrs):
                if ob.type in ("MESH", "CAMERA", "LIGHT", "EMPTY", "ARMATURE"):
                    base_objects.append(ob)

        for ob in base_objects:
            self._write_node(ob)
            self.marked_ptrs.discard(ob.as_pointer())

        self.sw.close_element()  # visual_scene
        self.sw.close_element()  # library_visual_scenes

    def _write_node_list(self, child_objects: list[Object]) -> None:
        for child in child_objects:
            self._write_node(child)
            self.marked_ptrs.discard(child.as_pointer())

    def _write_node(self, ob: Object) -> None:
        child_objects = list(self._children_by_parent_ptr.get(ob.as_pointer(), ()))
        can_export = ob.as_pointer() in self.export_set_ptrs

        armature_exported = False
        ob_arm = get_assigned_armature(ob)
        if ob_arm is not None:
            armature_exported = ob_arm.as_pointer() in self.export_set_ptrs
            if armature_exported and ob_arm.as_pointer() in self.marked_ptrs:
                self._write_node(ob_arm)
                self.marked_ptrs.discard(ob_arm.as_pointer())

        if can_export:
            node_id = self.id_map.translate_id(ob.name)
            node_name = encode_xml(ob.name)
            self.sw.open_element("node", [("id", node_id), ("name", node_name), ("type", "NODE")])

            if ob.type == "MESH" and armature_exported:
                self._write_node_transform_identity()
            else:
                self._write_node_transform_ob(ob)

            extra_params: list[tuple[str, str | None, str | None, str]] = []

            if ob.type == "MESH":
                inst_ctrl_created = False
                if armature_exported:
                    inst_ctrl_created = self._write_instance_controller(ob, ob_arm)
                if not inst_ctrl_created:
                    geom_id = get_geometry_id(self.id_map, ob, self.settings.use_object_instantiation)
                    self.sw.open_element("instance_geometry", [("url", f"#{geom_id}"), ("name", node_name)])
                    self._write_material_bindings(ob)
                    self.sw.close_element()

            elif ob.type == "ARMATURE":
                extra_params = self._collect_armature_collection_extras(ob)
                self._write_armature_bones(ob, child_objects)

            elif ob.type == "CAMERA":
                self.sw.write_element("instance_camera", None, [("url", f"#{get_camera_id(self.id_map, ob)}")])

            elif ob.type == "LIGHT":
                self.sw.write_element("instance_light", None, [("url", f"#{get_light_id(self.id_map, ob)}")])

            self.marked_ptrs.discard(ob.as_pointer())
            self._write_node_list(child_objects)

            if extra_params:
                self.sw.write_extra_technique("blender", extra_params)

            self.sw.close_element()  # node
        else:
            self._write_node_list(child_objects)

    def _write_transform_decomposed(self, loc: Vector, rot: Euler, scale: Vector) -> None:
        # Exact order from TransformWriter::add_transform()!
        self.sw.open_element("scale", [("sid", "scale")])
        self.sw.append_values((fmt_f32(scale.x), fmt_f32(scale.y), fmt_f32(scale.z)))
        self.sw.close_element()
        self.sw.open_element("rotate", [("sid", "rotationZ")])
        self.sw.append_values(("0", "0", "1", fmt_f32(math.degrees(rot.z))))
        self.sw.close_element()
        self.sw.open_element("rotate", [("sid", "rotationY")])
        self.sw.append_values(("0", "1", "0", fmt_f32(math.degrees(rot.y))))
        self.sw.close_element()
        self.sw.open_element("rotate", [("sid", "rotationX")])
        self.sw.append_values(("1", "0", "0", fmt_f32(math.degrees(rot.x))))
        self.sw.close_element()
        self.sw.open_element("translate", [("sid", "location")])
        self.sw.append_values((fmt_f32(loc.x), fmt_f32(loc.y), fmt_f32(loc.z)))
        self.sw.close_element()

    def _write_node_transform_ob(self, ob: Object) -> None:
        f_obmat = _get_object_local_matrix(ob)
        if self.settings.apply_global_orientation:
            f_obmat = bc_apply_global_transform_mat(f_obmat, self.settings.global_transform)
        else:
            f_obmat = bc_add_global_transform_mat(f_obmat, self.settings.global_transform)

        if self.settings.object_transformation_type == "matrix":
            vals = matrix_to_dae_flat(f_obmat, self.settings.limit_precision)
            self.sw.open_element("matrix", [("sid", "transform")])
            self.sw.append_values(fmt_float(v) for v in vals)
            self.sw.close_element()
        else:
            loc, rot, _, scale = bc_decompose(f_obmat)
            if self.settings.limit_precision:
                loc = sanitize_vec3(loc, LIMITED_PRECISION)
                rot = Euler(sanitize_vec3(Vector(rot), LIMITED_PRECISION), "XYZ")
                scale = sanitize_vec3(scale, LIMITED_PRECISION)
            self._write_transform_decomposed(loc, rot, scale)

    def _write_node_transform_identity(self) -> None:
        if self.settings.object_transformation_type == "matrix":
            self.sw.open_element("matrix", [("sid", "transform")])
            self.sw.append_values(("1", "0", "0", "0", "0", "1", "0", "0", "0", "0", "1", "0", "0", "0", "0", "1"))
            self.sw.close_element()
        else:
            self._write_transform_decomposed(Vector((0.0, 0.0, 0.0)), Euler((0.0, 0.0, 0.0)), Vector((1.0, 1.0, 1.0)))

    def _write_material_bindings(self, ob: Object) -> None:
        mats = [slot.material for slot in ob.material_slots if slot.material is not None]
        if not mats:
            return
        mesh = ob.data
        uv_layers = list(mesh.uv_layers)
        active_uv_rel = 0
        for i, uvl in enumerate(uv_layers):
            if uvl.active:
                active_uv_rel = i
                break

        # Match Blender 4.5.1 InstanceWriter.cpp:46-48 verbatim:
        # CustomData_get_active_layer_index(&mesh->corner_data, CD_PROP_FLOAT2)
        # returns the global layer index in corner_data (after .corner_vert,
        # .corner_edge, and any other corner layers with CD type < CD_PROP_FLOAT2),
        # which is then compared against the per-type loop counter `b`.
        cd_types_before_float2 = {"FLOAT", "INT", "STRING", "BYTE_COLOR", "FLOAT_COLOR", "FLOAT_VECTOR"}
        extra_before = sum(
            1
            for attr in mesh.attributes
            if attr.domain == "CORNER"
            and attr.data_type in cd_types_before_float2
            and attr.name not in (".corner_vert", ".corner_edge")
        )
        # .corner_vert and .corner_edge are always the first two CD_PROP_INT32 layers on a mesh with loops
        base_corner_offset = (2 if len(mesh.loops) > 0 else 0) + extra_before
        active_uv_idx = (base_corner_offset + active_uv_rel) if uv_layers else -1

        self.sw.open_element("bind_material")
        self.sw.open_element("technique_common")
        for ma in mats:
            mat_id = self.id_map.translate_id(get_material_id(self.id_map, ma))
            self.sw.open_element("instance_material", [("symbol", mat_id), ("target", f"#{mat_id}")])
            map_idx = 0
            for i, uvl in enumerate(uv_layers):
                if not self.settings.active_uv_only or i == active_uv_idx:
                    self.sw.write_element(
                        "bind_vertex_input",
                        None,
                        [
                            ("semantic", encode_xml(uvl.name)),
                            ("input_semantic", "TEXCOORD"),
                            ("input_set", fmt_int(map_idx)),
                        ],
                    )
                    map_idx += 1
            self.sw.close_element()
        self.sw.close_element()
        self.sw.close_element()

    def _export_scene(self) -> None:
        scene_id = self.id_map.translate_id(self.scene.name)
        self.sw.open_element("scene")
        self.sw.write_element("instance_visual_scene", None, [("url", f"#{scene_id}")])
        self.sw.close_element()
