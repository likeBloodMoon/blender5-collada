# SPDX-FileCopyrightText: 2009-2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Complete Collada (.dae) exporter faithfully ported from Blender 4.5.1."""

from __future__ import annotations

import datetime
import math
import os
import shutil
import urllib.parse
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import bpy
from mathutils import Euler, Matrix, Vector

from .xml_writer import (
    ColladaStreamWriter,
    encode_xml,
    f32,
    fmt_f32,
    fmt_f32_array,
    fmt_float,
    fmt_int,
    fmt_int_array,
)
from .collada_utils import (
    LIMITED_PRECISION,
    GlobalIdMap,
    assigned_action_has_keyframes,
    bc_add_global_transform_mat,
    bc_add_global_transform_vec,
    bc_apply_global_transform_mat,
    bc_decompose,
    build_export_set,
    build_global_transform,
    flush_edit_mode_objects,
    focallength_to_fov,
    get_action_id,
    get_assigned_armature,
    get_assigned_fcurves,
    get_bone_property_matrix,
    get_camera_id,
    get_controller_id,
    get_diffuse_image,
    get_effect_id,
    get_geometry_id,
    get_joint_sid,
    get_light_id,
    get_master_shader,
    get_material_id,
    get_mesh_copy,
    get_morph_controller_id,
    is_export_root,
    is_leaf_bone,
    is_root_bone,
    KEEP_NORMALS_ATTR,
    matrix_to_dae_flat,
    sanitize_matrix,
    sanitize_vec3,
)

if TYPE_CHECKING:
    from bpy.types import Bone, Context, Mesh, Object, Scene


@dataclass
class ExportSettings:
    filepath: str = ""
    apply_modifiers: bool = False
    export_mesh_type: str = "view"
    global_forward: str = "Y"
    global_up: str = "Z"
    apply_global_orientation: bool = False
    selected: bool = False
    include_children: bool = False
    include_armatures: bool = False
    include_shapekeys: bool = False
    deform_bones_only: bool = False
    include_animations: bool = True
    include_all_actions: bool = True
    export_animation_type: str = "sample"
    sampling_rate: int = 1
    keep_smooth_curves: bool = False
    keep_keyframes: bool = False
    keep_flat_curves: bool = False
    active_uv_only: bool = False
    use_texture_copies: bool = True
    triangulate: bool = True
    keep_quads: bool = False
    keep_normals: bool = True
    deduplicate_attributes: bool = True
    use_object_instantiation: bool = True
    use_blender_profile: bool = True
    sort_by_name: bool = False
    object_transformation_type: str = "matrix"
    animation_transformation_type: str = "matrix"
    open_sim: bool = False
    limit_precision: bool = False
    keep_bind_info: bool = False
    collection: str = ""
    global_transform: Matrix = field(default_factory=lambda: Matrix.Identity(4))


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


# Channel mapping tables from AnimationExporter.cpp
_AXIS_FROM_TYPE: dict[str, list[str]] = {
    "color": ["R", "G", "B"],
    "specular_color": ["R", "G", "B"],
    "diffuse_color": ["R", "G", "B"],
    "alpha": ["R", "G", "B"],
    "scale": ["X", "Y", "Z"],
    "location": ["X", "Y", "Z"],
    "rotation_euler": ["X", "Y", "Z"],
}

_CHANNEL_BLENDER_TO_COLLADA: dict[str, str] = {
    "rotation": "rotation",
    "rotation_euler": "rotation",
    "rotation_quaternion": "rotation",
    "scale": "scale",
    "location": "location",
    "specular_color": "specular",
    "diffuse_color": "diffuse",
    "ior": "index_of_refraction",
    "specular_hardness": "specular_hardness",
    "alpha": "alpha",
    "color": "color",
    "fall_off_angle": "falloff_angle",
    "spot_size": "falloff_angle",
    "fall_off_exponent": "falloff_exponent",
    "spot_blend": "falloff_exponent",
    "blender/blender_dist": "blender/blender_dist",
    "distance": "blender/blender_dist",
    "lens": "xfov",
    "xfov": "xfov",
    "xmag": "xmag",
    "zfar": "zfar",
    "znear": "znear",
    "ortho_scale": "xmag",
    "clip_end": "zfar",
    "clip_start": "znear",
}

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


def _native_path_to_uri(path: str) -> str:
    """Match COLLADABU::URI::nativePathToUri()."""
    if not path:
        return ""
    p = path.replace("\\", "/")
    if len(p) >= 2 and p[1] == ":":
        return "file:///" + urllib.parse.quote(p, safe="/:")
    return urllib.parse.quote(p, safe="/")


def _get_object_local_matrix(ob: Object) -> Matrix:
    """Match BKE_object_matrix_local_get(ob, mat)."""
    return ob.matrix_local.copy()


def _create_restpose_mat(settings: ExportSettings, bone: Bone, from_mat: Matrix, use_local_space: bool) -> Matrix:
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


class DocumentExporter:
    """Complete Collada 1.4.1 DocumentExporter matching Blender 4.5.1."""

    def __init__(self, context: Context, settings: ExportSettings) -> None:
        self.context = context
        self.scene: Scene = context.scene
        self.view_layer = context.view_layer
        self.depsgraph = context.evaluated_depsgraph_get()
        self.settings = settings
        self.settings.global_transform = build_global_transform(
            settings.global_forward, settings.global_up
        )
        self.id_map = GlobalIdMap()
        self.sw = ColladaStreamWriter()
        self.export_set: list[Object] = []
        self.export_set_ptrs: set[int] = set()
        self.key_image_map: dict[str, bpy.types.Image] = {}
        self.exported_geometries: set[str] = set()
        self.marked_ptrs: set[int] = set()
        self._temp_meshes: list[bpy.types.Mesh] = []
        self._children_by_parent_ptr: dict[int, list[Object]] = {}

    def export_current_scene(self) -> int:
        # Match io_collada.cc option normalization
        s = self.settings
        sample_animations = s.export_animation_type == "sample"
        if not sample_animations:
            s.sampling_rate = 0
            s.animation_transformation_type = "decomposed"
        s.keep_keyframes = s.keep_keyframes or (s.sampling_rate < 1)
        if s.animation_transformation_type != "decomposed":
            s.keep_smooth_curves = False
        if s.include_animations:
            s.object_transformation_type = s.animation_transformation_type

        self.id_map.clear()
        flush_edit_mode_objects(self.view_layer, self.depsgraph)
        self.export_set = build_export_set(
            self.scene,
            self.view_layer,
            s.selected,
            s.include_children,
            s.include_armatures,
            s.sort_by_name,
            s.collection,
        )
        self.export_set_ptrs = {ob.as_pointer() for ob in self.export_set}
        self._temp_meshes.clear()
        self._children_by_parent_ptr.clear()
        for ob in self.view_layer.objects:
            par = ob.parent
            if par is not None:
                self._children_by_parent_ptr.setdefault(par.as_pointer(), []).append(ob)

        try:
            self.sw.start_document()
            self._export_asset()

            if any(ob.type == "CAMERA" and ob.data for ob in self.export_set):
                self._export_cameras()

            if any(ob.type == "LIGHT" and ob.data for ob in self.export_set):
                self._export_lights()

            self._export_effects()
            self._export_images()
            self._export_materials()

            if any(ob.type == "MESH" and ob.data for ob in self.export_set):
                self._export_geometries()

            if any(ob.type == "ARMATURE" and ob.data for ob in self.export_set) or s.include_shapekeys:
                self._export_controllers()

            orig_frame = self.scene.frame_current
            orig_subframe = self.scene.frame_subframe
            try:
                if s.include_animations:
                    self._export_animations()

                self._export_visual_scene()
                self._export_scene()
            finally:
                self.scene.frame_set(orig_frame, subframe=orig_subframe)
        finally:
            if self._temp_meshes:
                bpy.data.batch_remove(self._temp_meshes)
                self._temp_meshes.clear()

        self.sw.end_document()
        os.makedirs(os.path.dirname(os.path.abspath(self.settings.filepath)), exist_ok=True)
        self.sw.write_to_file(self.settings.filepath)
        return len(self.export_set)

    # ------------------------------------------------------------------
    # <asset>
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # <library_cameras>
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # <library_lights>
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # <library_effects>
    # ------------------------------------------------------------------

    def _iter_materials_in_export_set(self):
        seen: set[int] = set()
        for ob in self.export_set:
            if not hasattr(ob, "material_slots"):
                continue
            for slot in ob.material_slots:
                ma = slot.material
                if ma is None:
                    continue
                ptr = ma.as_pointer()
                if ptr not in seen:
                    seen.add(ptr)
                    yield ma, ob

    def _export_effects(self) -> None:
        has_any = any(
            slot.material is not None
            for ob in self.scene.objects
            for slot in getattr(ob, "material_slots", ())
        )
        if not has_any:
            return

        self.sw.open_element("library_effects")
        for ma, ob in self._iter_materials_in_export_set():
            eff_id = get_effect_id(self.id_map, ma)
            self.sw.open_element("effect", [("id", eff_id)])
            self.sw.open_element("profile_COMMON")

            shader = get_master_shader(ma)
            image = get_diffuse_image(ma)
            img_key = ""
            if image is not None:
                img_key = self.id_map.translate_id(image.name)
                if img_key not in self.key_image_map:
                    self.key_image_map[img_key] = image

                # <newparam> for surface and sampler2D
                surf_sid = f"{img_key}-surface"
                samp_sid = f"{img_key}-sampler"
                self.sw.open_element("newparam", [("sid", surf_sid)])
                self.sw.open_element("surface", [("type", "2D")])
                self.sw.write_element("init_from", img_key)
                self.sw.close_element()
                self.sw.close_element()

                self.sw.open_element("newparam", [("sid", samp_sid)])
                self.sw.open_element("sampler2D")
                self.sw.write_element("source", surf_sid)
                self.sw.close_element()
                self.sw.close_element()

            # Collect material values matching collada_utils.cpp
            # Base color
            diff_col = [ma.diffuse_color[0], ma.diffuse_color[1], ma.diffuse_color[2], 1.0]
            if shader is not None:
                bc_sock = shader.inputs.get("Base Color")
                if bc_sock is not None:
                    dv = bc_sock.default_value
                    diff_col = [dv[0], dv[1], dv[2], 1.0]

            # Emission
            em_col = [0.0, 0.0, 0.0, 1.0]
            if shader is not None:
                es_sock = shader.inputs.get("Emission Strength")
                ec_sock = shader.inputs.get("Emission Color")
                em_strength = float(es_sock.default_value) if es_sock is not None else 0.0
                if em_strength != 0.0 and ec_sock is not None:
                    dv = ec_sock.default_value
                    fc = [dv[0] * em_strength, dv[1] * em_strength, dv[2] * em_strength]
                    max_c = max(fc[0], fc[1], fc[2])
                    if max_c > 1.0:
                        fc = [c / max_c for c in fc]
                    em_col = [fc[0], fc[1], fc[2], dv[3]]

            # IOR
            ior = -1.0
            if shader is not None:
                ior_sock = shader.inputs.get("IOR")
                if ior_sock is not None:
                    ior = float(ior_sock.default_value)

            # Reflectivity (Metallic)
            reflectivity = float(ma.specular_intensity)
            if shader is not None:
                met_sock = shader.inputs.get("Metallic")
                if met_sock is not None:
                    reflectivity = float(met_sock.default_value)

            # Alpha
            alpha = float(ma.diffuse_color[3])
            if shader is not None:
                a_sock = shader.inputs.get("Alpha")
                if a_sock is not None:
                    alpha = float(a_sock.default_value)

            # Active UV layer name on ob
            active_uv = ""
            if ob.type == "MESH" and ob.data and len(ob.data.uv_layers) > 0:
                uv_act = ob.data.uv_layers.active
                active_uv = uv_act.name if uv_act else ob.data.uv_layers[0].name

            self.sw.open_element("technique", [("sid", "common")])
            self.sw.open_element("lambert")

            # Emission (OpenCOLLADA writes emission, ambient, diffuse, specular, shininess, reflective, reflectivity, transparent, transparency, index_of_refraction)
            self.sw.open_element("emission")
            self.sw.write_element(
                "color",
                f"{fmt_f32(em_col[0])} {fmt_f32(em_col[1])} {fmt_f32(em_col[2])} {fmt_f32(em_col[3])}",
                [("sid", "emission")],
            )
            self.sw.close_element()

            # Diffuse
            self.sw.open_element("diffuse")
            if image is not None:
                self.sw.write_element(
                    "texture",
                    None,
                    [("texture", f"{img_key}-sampler"), ("texcoord", encode_xml(active_uv))],
                )
            else:
                self.sw.write_element(
                    "color",
                    f"{fmt_f32(diff_col[0])} {fmt_f32(diff_col[1])} {fmt_f32(diff_col[2])} {fmt_f32(diff_col[3])}",
                    [("sid", "diffuse")],
                )
            self.sw.close_element()

            # Reflectivity
            if reflectivity > 0.0:
                self.sw.open_element("reflectivity")
                self.sw.write_element("float", fmt_f32(reflectivity), [("sid", "specular")])
                self.sw.close_element()

            # Transparent
            if alpha < 1.0:
                self.sw.open_element("transparent", [("opaque", "A_ONE")])
                self.sw.write_element(
                    "color", f"0 0 0 {fmt_f32(alpha)}", [("sid", "alpha")]
                )
                self.sw.close_element()

            # Index of refraction
            if ior >= 0.0:
                self.sw.open_element("index_of_refraction")
                self.sw.write_element("float", fmt_f32(ior), [("sid", "ior")])
                self.sw.close_element()

            self.sw.close_element()  # lambert
            self.sw.close_element()  # technique
            self.sw.close_element()  # profile_COMMON
            self.sw.close_element()  # effect

        self.sw.close_element()

    # ------------------------------------------------------------------
    # <library_images>
    # ------------------------------------------------------------------

    def _export_images(self) -> None:
        self.sw.open_element("library_images")
        export_dir = os.path.dirname(os.path.abspath(self.settings.filepath))
        use_copies = self.settings.use_texture_copies

        for img_key, image in self.key_image_map.items():
            is_generated = image.source == "GENERATED"
            is_dirty = image.is_dirty
            is_packed = image.packed_file is not None

            export_file = image.name
            if not os.path.splitext(export_file)[1]:
                ext_map = {
                    "PNG": ".png",
                    "JPEG": ".jpg",
                    "TARGA": ".tga",
                    "BMP": ".bmp",
                    "TIFF": ".tif",
                    "OPEN_EXR": ".exr",
                }
                export_file += ext_map.get(image.file_format, ".png")

            export_path = os.path.join(export_dir, export_file)

            if is_generated or is_dirty or is_packed:
                os.makedirs(export_dir, exist_ok=True)
                try:
                    image.save_render(export_path, scene=self.scene)
                except Exception:
                    try:
                        orig = image.filepath_raw
                        image.filepath_raw = export_path
                        image.save()
                        image.filepath_raw = orig
                    except Exception:
                        continue
                ref_path = export_file
            else:
                src_path = bpy.path.abspath(image.filepath, library=image.library)
                src_path = os.path.normpath(src_path)
                if not os.path.exists(src_path):
                    continue
                if use_copies:
                    os.makedirs(export_dir, exist_ok=True)
                    if os.path.normcase(os.path.abspath(src_path)) != os.path.normcase(
                        os.path.abspath(export_path)
                    ):
                        shutil.copy2(src_path, export_path)
                    ref_path = export_file
                else:
                    ref_path = src_path

            self.sw.open_element("image", [("id", img_key), ("name", img_key)])
            self.sw.write_element("init_from", _native_path_to_uri(ref_path))
            self.sw.close_element()

        self.sw.close_element()

    # ------------------------------------------------------------------
    # <library_materials>
    # ------------------------------------------------------------------

    def _export_materials(self) -> None:
        mats = list(self._iter_materials_in_export_set())
        if not mats:
            return
        self.sw.open_element("library_materials")
        for ma, _ob in mats:
            mat_id = get_material_id(self.id_map, ma)
            mat_name = encode_xml(ma.name)
            eff_id = get_effect_id(self.id_map, ma)
            self.sw.open_element("material", [("id", mat_id), ("name", mat_name)])
            self.sw.write_element("instance_effect", None, [("url", f"#{eff_id}")])
            self.sw.close_element()
        self.sw.close_element()

    # ------------------------------------------------------------------
    # <library_geometries>
    # ------------------------------------------------------------------

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

        # Byte color attributes on CORNER domain (matching CD_PROP_BYTE_COLOR)
        byte_color_attrs = [
            attr for attr in mesh.color_attributes
            if attr.domain == "CORNER" and attr.data_type == "BYTE_COLOR"
        ]
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
                col_arr = np.empty(n_loops * 4, dtype=np.float32)
                if n_loops > 0:
                    attr.data.foreach_get("color_srgb", col_arr)
                col_rows = col_arr.reshape(n_loops, 4)
                if tri_loops is not None:
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

    # ------------------------------------------------------------------
    # <library_controllers>
    # ------------------------------------------------------------------

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

        use_inst = self.settings.use_object_instantiation
        controller_id = get_controller_id(self.id_map, ob_arm, ob)
        controller_name = encode_xml(ob_arm.name)
        geom_id = get_geometry_id(self.id_map, ob, use_inst)

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
        kbs = list(key.key_blocks)[1:]
        if not kbs:
            return
        use_inst = self.settings.use_object_instantiation
        controller_id = get_morph_controller_id(self.id_map, ob)
        controller_name = encode_xml(f"{ob.name}-morph")
        geom_id = get_geometry_id(self.id_map, ob, use_inst)

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

    # ------------------------------------------------------------------
    # <library_animations>
    # ------------------------------------------------------------------

    def _object_has_animations(self, ob: Object) -> bool:
        if assigned_action_has_keyframes(ob.animation_data):
            return True
        if ob.data is not None and ob.type in ("CAMERA", "LIGHT"):
            if assigned_action_has_keyframes(ob.data.animation_data):
                return True
        for slot in getattr(ob, "material_slots", ()):
            if slot.material and assigned_action_has_keyframes(slot.material.animation_data):
                return True
        if ob.type == "MESH" and ob.data and ob.data.shape_keys:
            if assigned_action_has_keyframes(ob.data.shape_keys.animation_data):
                return True
        return False

    def _fra2time(self, frame: float) -> float:
        fps = self.scene.render.fps / self.scene.render.fps_base
        return frame / fps

    def _export_animations(self) -> None:
        animated_objs = [ob for ob in reversed(self.export_set) if self._object_has_animations(ob)]
        if not animated_objs:
            return

        if True:
            self.sw.open_element("library_animations")
            s = self.settings
            export_as_matrix = s.animation_transformation_type == "matrix"

            # Determine scene sample frames
            scene_sample_frames: set[int] = set()
            if s.sampling_rate >= 1:
                sfra = self.scene.frame_start
                efra = self.scene.frame_end
                fi = sfra
                while fi < efra:
                    scene_sample_frames.add(fi)
                    fi += s.sampling_rate
                if fi >= efra:
                    scene_sample_frames.add(efra)

            # Collect keyframes per object
            obj_keyframes: dict[int, set[int]] = {}
            all_frames_needed: set[int] = set(scene_sample_frames)
            for ob in animated_objs:
                kfs: set[int] = set()
                for adt in self._iter_object_anim_datas(ob):
                    for fcu in get_assigned_fcurves(adt):
                        for kp in fcu.keyframe_points:
                            kfs.add(round(kp.co.x))
                obj_keyframes[ob.as_pointer()] = kfs
                if s.keep_keyframes or s.export_animation_type == "keys":
                    all_frames_needed.update(kfs)

            # Sample all needed frames in range [frame_start, frame_end]
            sfra = self.scene.frame_start
            efra = self.scene.frame_end
            frames_sorted = sorted(f for f in all_frames_needed if sfra <= f <= efra)

            # Per-object sampled matrices & bone matrices
            obj_mat_samples: dict[int, dict[int, Matrix]] = {ob.as_pointer(): {} for ob in animated_objs}
            bone_mat_samples: dict[int, dict[str, dict[int, Matrix]]] = {
                ob.as_pointer(): {} for ob in animated_objs
            }

            for frame_idx in frames_sorted:
                is_scene_sample = frame_idx in scene_sample_frames
                need_set = False
                for ob in animated_objs:
                    if is_scene_sample or frame_idx in obj_keyframes[ob.as_pointer()]:
                        need_set = True
                        break
                if not need_set:
                    continue

                self.scene.frame_set(frame_idx)
                for ob in animated_objs:
                    ptr = ob.as_pointer()
                    if not (is_scene_sample or frame_idx in obj_keyframes[ptr]):
                        continue
                    obj_mat_samples[ptr][frame_idx] = _get_object_local_matrix(ob)
                    if ob.type == "ARMATURE" and ob.pose:
                        bmap = bone_mat_samples[ptr]
                        for pchan in ob.pose.bones:
                            bone = pchan.bone
                            if pchan.parent:
                                bmat = pchan.parent.matrix.inverted() @ pchan.matrix
                            else:
                                bmat = pchan.matrix.copy()
                            if s.open_sim:
                                temp = bone.matrix_local.to_3x3().to_4x4().inverted()
                                bmat = bmat @ temp
                                if bone.parent:
                                    ptemp = bone.parent.matrix_local.to_3x3().to_4x4()
                                    bmat = ptemp @ bmat
                            bmap.setdefault(bone.name, {})[frame_idx] = bmat

            for ob in animated_objs:
                ptr = ob.as_pointer()
                ob_id = self.id_map.translate_id(ob.name)
                ob_name_xml = encode_xml(ob.name)
                self.sw.open_element(
                    "animation", [("id", f"action_container-{ob_id}"), ("name", ob_name_xml)]
                )

                if export_as_matrix:
                    self._export_object_matrix_animation(ob, obj_mat_samples[ptr])

                self._export_object_curve_animations(ob, obj_mat_samples[ptr], bone_mat_samples[ptr], export_as_matrix)

                if ob.type == "ARMATURE" and export_as_matrix:
                    for root_bone in ob.data.bones:
                        if root_bone.parent is None:
                            self._export_bone_matrix_animations_recursive(
                                ob, root_bone, bone_mat_samples[ptr]
                            )

                self.sw.close_element()  # action_container

            self.sw.close_element()  # library_animations

    def _iter_object_anim_datas(self, ob: Object):
        if ob.animation_data:
            yield ob.animation_data
        if ob.data and ob.type in ("CAMERA", "LIGHT") and ob.data.animation_data:
            yield ob.data.animation_data
        for slot in getattr(ob, "material_slots", ()):
            if slot.material and slot.material.animation_data:
                yield slot.material.animation_data

    def _is_matrix_samples_animated(self, samples: dict[int, Matrix]) -> bool:
        if len(samples) < 2:
            return False
        ref = None
        for f in sorted(samples.keys()):
            m = samples[f]
            if ref is None:
                ref = m
                continue
            for r in range(4):
                for c in range(4):
                    if abs(m[r][c] - ref[r][c]) > 0.00001:
                        return True
        return False

    def _export_object_matrix_animation(self, ob: Object, samples: dict[int, Matrix]) -> None:
        if not samples:
            return
        if not self.settings.keep_flat_curves and not self._is_matrix_samples_animated(samples):
            return
        action = ob.animation_data.action if ob.animation_data else None
        name_xml = encode_xml(ob.name)
        action_name = action.name if action else f"{name_xml}-action"
        anim_id = get_action_id(self.id_map, action_name, name_xml, "transform", "")
        target = f"{self.id_map.translate_id(name_xml)}/transform"
        self._write_matrix_animation_xml(anim_id, name_xml, target, samples)

    def _export_bone_matrix_animations_recursive(
        self, ob: Object, bone: Bone, bmap: dict[str, dict[int, Matrix]]
    ) -> None:
        samples = bmap.get(bone.name, {})
        if samples and (self.settings.keep_flat_curves or self._is_matrix_samples_animated(samples)):
            action = ob.animation_data.action if ob.animation_data else None
            action_name = action.name if action else f"{ob.name}-action"
            name_xml = encode_xml(ob.name)
            anim_id = get_action_id(self.id_map, action_name, name_xml, bone.name, "pose_matrix")
            target = f"{self.id_map.translate_id(f'{ob.name}_{bone.name}')}/transform"
            self._write_matrix_animation_xml(anim_id, name_xml, target, samples)

        for child in bone.children:
            self._export_bone_matrix_animations_recursive(ob, child, bmap)

    def _write_matrix_animation_xml(
        self, anim_id: str, name_xml: str, target: str, samples: dict[int, Matrix]
    ) -> None:
        frames = sorted(samples.keys())
        self.sw.open_element("animation", [("id", anim_id), ("name", name_xml)])

        input_id = f"{anim_id}-input"
        self._write_float_source(
            input_id,
            f"{input_id}-array",
            len(frames),
            1,
            ("TIME",),
            [fmt_f32(self._fra2time(f)) for f in frames],
        )

        output_id = f"{anim_id}-output"
        gt = self.settings.global_transform
        apply_glob = self.settings.apply_global_orientation
        mat_vals: list[str] = []
        for f in frames:
            m = samples[f]
            if apply_glob:
                m = bc_apply_global_transform_mat(m, gt)
            else:
                m = bc_add_global_transform_mat(m, gt)
            flat = matrix_to_dae_flat(m, self.settings.limit_precision)
            for v in flat:
                mat_vals.append(fmt_float(v))
        self._write_float_source(
            output_id,
            f"{output_id}-array",
            len(frames),
            16,
            ("TRANSFORM",),
            mat_vals,
            param_type="float4x4",
        )

        interp_id = f"{anim_id}-interpolation"
        self._write_name_source(interp_id, f"{interp_id}-array", "INTERPOLATION", ["LINEAR"] * len(frames))

        sampler_id = f"{anim_id}-sampler"
        self.sw.open_element("sampler", [("id", sampler_id)])
        self.sw.write_element("input", None, [("semantic", "INPUT"), ("source", f"#{input_id}")])
        self.sw.write_element("input", None, [("semantic", "OUTPUT"), ("source", f"#{output_id}")])
        self.sw.write_element("input", None, [("semantic", "INTERPOLATION"), ("source", f"#{interp_id}")])
        self.sw.close_element()

        self.sw.write_element("channel", None, [("source", f"#{sampler_id}"), ("target", target)])
        self.sw.close_element()

    def _write_name_source(
        self, source_id: str, array_id: str, param_name: str, values: list[str]
    ) -> None:
        self.sw.open_element("source", [("id", source_id)])
        self.sw.open_element("Name_array", [("id", array_id), ("count", fmt_int(len(values)))])
        self.sw.append_values(values)
        self.sw.close_element()
        self.sw.open_element("technique_common")
        self.sw.open_element(
            "accessor",
            [("source", f"#{array_id}"), ("count", fmt_int(len(values))), ("stride", "1")],
        )
        self.sw.write_element("param", None, [("name", param_name), ("type", "name")])
        self.sw.close_element()
        self.sw.close_element()
        self.sw.close_element()

    def _export_object_curve_animations(
        self,
        ob: Object,
        obj_samples: dict[int, Matrix],
        bone_samples: dict[str, dict[int, Matrix]],
        export_as_matrix: bool,
    ) -> None:
        action = ob.animation_data.action if ob.animation_data else None
        action_name = action.name if action else "constraint_anim"
        keep_flat = self.settings.keep_flat_curves
        keep_smooth = self.settings.keep_smooth_curves and (self.settings.export_animation_type == "keys")

        # Export decomposed transform curves if not exporting as matrix
        if not export_as_matrix and obj_samples:
            frames = sorted(obj_samples.keys())
            fcurve_lookup = {
                (fcu.data_path, fcu.array_index): fcu
                for fcu in get_assigned_fcurves(ob.animation_data)
            }
            for channel_type in ("location", "rotation_euler", "scale"):
                axes = _AXIS_FROM_TYPE[channel_type]
                for idx, axis in enumerate(axes):
                    fcu = fcurve_lookup.get((channel_type, idx))
                    if self.settings.export_animation_type == "keys" and fcu is not None:
                        c_frames = [kp.co.x for kp in fcu.keyframe_points]
                        c_vals = [kp.co.y for kp in fcu.keyframe_points]
                    else:
                        c_frames = [float(f) for f in frames]
                        c_vals = []
                        for f in frames:
                            loc, rot, _, scale = bc_decompose(obj_samples[f])
                            if channel_type == "location":
                                c_vals.append(loc[idx])
                            elif channel_type == "rotation_euler":
                                c_vals.append(rot[idx])
                            else:
                                c_vals.append(scale[idx])

                    if not c_vals:
                        continue
                    is_anim = len(c_vals) >= 2 and any(abs(v - c_vals[0]) > 1e-5 for v in c_vals[1:])
                    if not keep_flat and not is_anim:
                        continue

                    name_xml = encode_xml(ob.name)
                    anim_id = get_action_id(self.id_map, action_name, name_xml, channel_type, axis, ".")
                    tm_name = _CHANNEL_BLENDER_TO_COLLADA.get(channel_type, channel_type)
                    is_rot = channel_type == "rotation_euler"
                    sid = f"{tm_name}{axis}.ANGLE" if is_rot else f"{tm_name}.{axis}"
                    target = f"{self.id_map.translate_id(name_xml)}/{sid}"
                    self._write_curve_animation_xml(
                        anim_id, name_xml, target, axis, c_frames, c_vals, is_rot, fcu if keep_smooth else None
                    )

        # Export bone decomposed transform curves if armature & not exporting as matrix
        if not export_as_matrix and ob.type == "ARMATURE" and bone_samples:
            fcurve_lookup = {
                (fcu.data_path, fcu.array_index): fcu
                for fcu in get_assigned_fcurves(ob.animation_data)
            }
            for bone in ob.data.bones:
                bsamps = bone_samples.get(bone.name)
                if not bsamps:
                    continue
                bframes = sorted(bsamps.keys())
                bone_prefix = f'pose.bones["{bone.name}"].'
                curve_name = encode_xml(f"{ob.name}_{bone.name}")
                for channel_type in ("location", "rotation_euler", "scale"):
                    axes = _AXIS_FROM_TYPE[channel_type]
                    for idx, axis in enumerate(axes):
                        fcu = fcurve_lookup.get((bone_prefix + channel_type, idx))
                        if self.settings.export_animation_type == "keys" and fcu is not None:
                            c_frames = [kp.co.x for kp in fcu.keyframe_points]
                            c_vals = [kp.co.y for kp in fcu.keyframe_points]
                        else:
                            c_frames = [float(f) for f in bframes]
                            c_vals = []
                            for f in bframes:
                                loc, rot, _, scale = bc_decompose(bsamps[f])
                                if channel_type == "location":
                                    c_vals.append(loc[idx])
                                elif channel_type == "rotation_euler":
                                    c_vals.append(rot[idx])
                                else:
                                    c_vals.append(scale[idx])
                        if not c_vals:
                            continue
                        is_anim = len(c_vals) >= 2 and any(abs(v - c_vals[0]) > 1e-5 for v in c_vals[1:])
                        if not keep_flat and not is_anim:
                            continue
                        channel_target = f'["{bone.name}"].{channel_type}'
                        anim_id = get_action_id(self.id_map, action_name, curve_name, channel_target, axis, ".")
                        tm_name = _CHANNEL_BLENDER_TO_COLLADA.get(channel_type, channel_type)
                        is_rot = channel_type == "rotation_euler"
                        sid = f"{tm_name}{axis}.ANGLE" if is_rot else f"{tm_name}.{axis}"
                        target = f"{self.id_map.translate_id(curve_name)}/{sid}"
                        self._write_curve_animation_xml(
                            anim_id,
                            curve_name,
                            target,
                            axis,
                            c_frames,
                            c_vals,
                            is_rot,
                            fcu if keep_smooth else None,
                            tangent_is_angle=False,
                        )

        # Export camera / light / material property FCurves
        if ob.type == "CAMERA" and ob.data and ob.data.animation_data:
            cam_fcurves = get_assigned_fcurves(ob.data.animation_data)
            cam_id = get_camera_id(self.id_map, ob)
            for fcu in cam_fcurves:
                dp = fcu.data_path
                tm_name = _CHANNEL_BLENDER_TO_COLLADA.get(dp)
                if not tm_name:
                    continue
                c_frames = [kp.co.x for kp in fcu.keyframe_points]
                c_vals = [kp.co.y for kp in fcu.keyframe_points]
                if dp == "lens":
                    c_vals = [math.degrees(focallength_to_fov(v, ob.data.sensor_width)) for v in c_vals]
                elif dp == "ortho_scale":
                    c_vals = [v / 2.0 for v in c_vals]
                if not c_vals:
                    continue
                is_anim = len(c_vals) >= 2 and any(abs(v - c_vals[0]) > 1e-5 for v in c_vals[1:])
                if not keep_flat and not is_anim:
                    continue
                anim_id = get_action_id(self.id_map, action_name, encode_xml(ob.name), tm_name, "", ".")
                target = f"{cam_id}/{tm_name}"
                self._write_curve_animation_xml(
                    anim_id, encode_xml(ob.name), target, "", c_frames, c_vals, False, fcu if keep_smooth else None
                )

        if ob.type == "LIGHT" and ob.data and ob.data.animation_data:
            la_fcurves = get_assigned_fcurves(ob.data.animation_data)
            la_id = get_light_id(self.id_map, ob)
            for fcu in la_fcurves:
                dp = fcu.data_path
                tm_name = _CHANNEL_BLENDER_TO_COLLADA.get(dp)
                if not tm_name:
                    continue
                axis = _AXIS_FROM_TYPE.get(dp, [""])[fcu.array_index] if dp in _AXIS_FROM_TYPE else ""
                c_frames = [kp.co.x for kp in fcu.keyframe_points]
                c_vals = [kp.co.y for kp in fcu.keyframe_points]
                if not c_vals:
                    continue
                is_anim = len(c_vals) >= 2 and any(abs(v - c_vals[0]) > 1e-5 for v in c_vals[1:])
                if not keep_flat and not is_anim:
                    continue
                is_angle = dp == "spot_size"
                anim_id = get_action_id(self.id_map, action_name, encode_xml(ob.name), dp, axis, ".")
                sid = f"{tm_name}.{axis}" if axis else tm_name
                target = f"{la_id}/{sid}"
                self._write_curve_animation_xml(
                    anim_id, encode_xml(ob.name), target, axis, c_frames, c_vals, is_angle, fcu if keep_smooth else None
                )

    def _write_curve_animation_xml(
        self,
        anim_id: str,
        name_xml: str,
        target: str,
        axis: str,
        frames: list[float],
        values: list[float],
        is_angle: bool,
        smooth_fcu=None,
        tangent_is_angle: bool | None = None,
    ) -> None:
        if tangent_is_angle is None:
            tangent_is_angle = is_angle
        self.sw.open_element("animation", [("id", anim_id), ("name", name_xml)])

        input_id = f"{anim_id}-input"
        self._write_float_source(
            input_id,
            f"{input_id}-array",
            len(frames),
            1,
            ("TIME",),
            [fmt_f32(self._fra2time(f)) for f in frames],
        )

        output_id = f"{anim_id}-output"
        out_param = "ANGLE" if is_angle else (axis if axis else "X")
        out_vals = [fmt_f32(math.degrees(v) if is_angle else v) for v in values]
        self._write_float_source(output_id, f"{output_id}-array", len(values), 1, (out_param,), out_vals)

        interp_id = f"{anim_id}-interpolation"
        has_tangents = False
        if smooth_fcu is not None and len(smooth_fcu.keyframe_points) == len(frames):
            interps: list[str] = []
            for kp in smooth_fcu.keyframe_points:
                if kp.interpolation == "BEZIER":
                    interps.append("BEZIER")
                    has_tangents = True
                elif kp.interpolation == "CONSTANT":
                    interps.append("STEP")
                else:
                    interps.append("LINEAR")
            self._write_name_source(interp_id, f"{interp_id}-array", "INTERPOLATION", interps)
        else:
            self._write_name_source(interp_id, f"{interp_id}-array", "INTERPOLATION", ["LINEAR"] * len(frames))

        in_tan_id = f"{anim_id}-intangent"
        out_tan_id = f"{anim_id}-outtangent"
        if has_tangents and smooth_fcu is not None:
            in_vals: list[str] = []
            out_vals_t: list[str] = []
            for kp in smooth_fcu.keyframe_points:
                in_vals.append(fmt_f32(self._fra2time(kp.handle_left.x)))
                in_vals.append(fmt_f32(math.degrees(kp.handle_left.y) if tangent_is_angle else kp.handle_left.y))
                out_vals_t.append(fmt_f32(self._fra2time(kp.handle_right.x)))
                out_vals_t.append(fmt_f32(math.degrees(kp.handle_right.y) if tangent_is_angle else kp.handle_right.y))
            self._write_float_source(in_tan_id, f"{in_tan_id}-array", len(frames), 2, ("X", "Y"), in_vals)
            self._write_float_source(out_tan_id, f"{out_tan_id}-array", len(frames), 2, ("X", "Y"), out_vals_t)

        sampler_id = f"{anim_id}-sampler"
        self.sw.open_element("sampler", [("id", sampler_id)])
        self.sw.write_element("input", None, [("semantic", "INPUT"), ("source", f"#{input_id}")])
        self.sw.write_element("input", None, [("semantic", "OUTPUT"), ("source", f"#{output_id}")])
        self.sw.write_element("input", None, [("semantic", "INTERPOLATION"), ("source", f"#{interp_id}")])
        if has_tangents:
            self.sw.write_element("input", None, [("semantic", "IN_TANGENT"), ("source", f"#{in_tan_id}")])
            self.sw.write_element("input", None, [("semantic", "OUT_TANGENT"), ("source", f"#{out_tan_id}")])
        self.sw.close_element()

        self.sw.write_element("channel", None, [("source", f"#{sampler_id}"), ("target", target)])
        self.sw.close_element()

    # ------------------------------------------------------------------
    # <library_visual_scenes>
    # ------------------------------------------------------------------

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
        child_objects = self._children_by_parent_ptr.get(ob.as_pointer(), ())
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

    # ------------------------------------------------------------------
    # <scene>
    # ------------------------------------------------------------------

    def _export_scene(self) -> None:
        scene_id = self.id_map.translate_id(self.scene.name)
        self.sw.open_element("scene")
        self.sw.write_element("instance_visual_scene", None, [("url", f"#{scene_id}")])
        self.sw.close_element()
