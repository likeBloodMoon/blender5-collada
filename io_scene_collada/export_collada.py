# SPDX-FileCopyrightText: 2009-2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Complete Collada (.dae) exporter faithfully ported from Blender 4.5.1."""

from __future__ import annotations

import datetime
import math
import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import bpy
from mathutils import Euler, Matrix, Vector

from .xml_writer import (
    ColladaStreamWriter,
    encode_xml,
    f32,
    fmt_f32,
    fmt_float,
    fmt_int,
)
from .collada_utils import (
    LIMITED_PRECISION,
    GlobalIdMap,
    assigned_action_has_keyframes,
    bc_add_global_transform_mat,
    bc_apply_global_transform_mat,
    bc_decompose,
    build_export_set,
    build_global_transform,
    flush_edit_mode_objects,
    focallength_to_fov,
    get_action_id,
    get_assigned_armature,
    get_assigned_fcurves,
    get_camera_id,
    get_geometry_id,
    get_light_id,
    get_material_id,
    is_export_root,
    matrix_to_dae_flat,
    sanitize_vec3,
)

from .exporter.geometry import GeometryMixin
from .exporter.materials import MaterialsMixin
from .exporter.rig import RigMixin
if TYPE_CHECKING:
    from bpy.types import Bone, Context, Object, Scene


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


def _get_object_local_matrix(ob: Object) -> Matrix:
    """Match BKE_object_matrix_local_get(ob, mat)."""
    return ob.matrix_local.copy()


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


class DocumentExporter(GeometryMixin, MaterialsMixin, RigMixin):
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

    # ------------------------------------------------------------------
    # <library_images>
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # <library_materials>
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # <library_geometries>
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # <library_controllers>
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # <scene>
    # ------------------------------------------------------------------

    def _export_scene(self) -> None:
        scene_id = self.id_map.translate_id(self.scene.name)
        self.sw.open_element("scene")
        self.sw.write_element("instance_visual_scene", None, [("url", f"#{scene_id}")])
        self.sw.close_element()
