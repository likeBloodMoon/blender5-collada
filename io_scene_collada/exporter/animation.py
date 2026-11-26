# SPDX-FileCopyrightText: 2009-2026 Blender Authors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Axis/channel maps, frame sampling, and object/bone animation export."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

import bpy
from mathutils import Matrix

from ..collada_utils import (
    assigned_action_has_keyframes,
    bc_add_global_transform_mat,
    bc_apply_global_transform_mat,
    bc_decompose,
    focallength_to_fov,
    get_action_id,
    get_assigned_fcurves,
    get_camera_id,
    get_light_id,
    matrix_to_dae_flat,
)
from ..xml_writer import encode_xml, fmt_f32, fmt_float, fmt_int
from .rig import _get_object_local_matrix

if TYPE_CHECKING:
    from bpy.types import Bone, Object

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


class AnimationMixin:
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

    def _collect_unassigned_action_jobs(self) -> list[tuple[Object, object, object | None]]:
        """Collect (object, action, slot) tuples for unassigned actions in bpy.data.actions (#44)."""
        assigned_ptrs: set[int] = set()
        for ob in self.export_set:
            if ob.animation_data and ob.animation_data.action:
                assigned_ptrs.add(ob.animation_data.action.as_pointer())
            if ob.data is not None:
                adt = getattr(ob.data, "animation_data", None)
                if adt and adt.action:
                    assigned_ptrs.add(adt.action.as_pointer())
                sk = getattr(ob.data, "shape_keys", None)
                if sk and sk.animation_data and sk.animation_data.action:
                    assigned_ptrs.add(sk.animation_data.action.as_pointer())
            for mslot in getattr(ob, "material_slots", ()):
                if mslot.material and mslot.material.animation_data and mslot.material.animation_data.action:
                    assigned_ptrs.add(mslot.material.animation_data.action.as_pointer())

        obj_by_name = {ob.name: ob for ob in self.export_set}
        armatures = [ob for ob in self.export_set if ob.type == "ARMATURE" and ob.data]
        jobs: list[tuple[Object, object, object | None]] = []

        for act in bpy.data.actions:
            if act.as_pointer() in assigned_ptrs:
                continue
            if hasattr(act, "layers"):
                from bpy_extras import anim_utils

                for slot in act.slots:
                    if getattr(slot, "target_id_type", "OBJECT") != "OBJECT":
                        continue
                    cbag = anim_utils.action_get_channelbag_for_slot(act, slot)
                    if cbag is None or not any(len(fcu.keyframe_points) > 0 for fcu in cbag.fcurves):
                        continue
                    target_ob = obj_by_name.get(getattr(slot, "name_display", ""))
                    if target_ob is None and armatures:
                        for fcu in cbag.fcurves:
                            dp = fcu.data_path
                            if dp.startswith('pose.bones["'):
                                end_q = dp.find('"]', 12)
                                if end_q > 12:
                                    bname = dp[12:end_q]
                                    for arm_ob in armatures:
                                        if bname in arm_ob.data.bones:
                                            target_ob = arm_ob
                                            break
                                if target_ob is not None:
                                    break
                    if target_ob is None and len(self.export_set) == 1:
                        target_ob = self.export_set[0]
                    if target_ob is not None:
                        jobs.append((target_ob, act, slot))
            else:
                fcurves = getattr(act, "fcurves", ())
                if any(len(fcu.keyframe_points) > 0 for fcu in fcurves) and len(self.export_set) == 1:
                    jobs.append((self.export_set[0], act, None))
        return jobs

    def _export_animations(self) -> None:
        animated_objs = [ob for ob in reversed(self.export_set) if self._object_has_animations(ob)]
        extra_jobs = self._collect_unassigned_action_jobs() if self.settings.include_all_actions else []
        if not animated_objs and not extra_jobs:
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

            # Export unassigned actions when include_all_actions is enabled (#44)
            for ob, act, slot in extra_jobs:
                had_adt = ob.animation_data is not None
                orig_act = ob.animation_data.action if had_adt else None
                orig_slot = getattr(ob.animation_data, "action_slot", None) if had_adt else None
                try:
                    if not had_adt:
                        ob.animation_data_create()
                    ob.animation_data.action = act
                    if slot is not None and hasattr(ob.animation_data, "action_slot"):
                        ob.animation_data.action_slot = slot

                    kfs: set[int] = set()
                    for fcu in get_assigned_fcurves(ob.animation_data):
                        for kp in fcu.keyframe_points:
                            kfs.add(round(kp.co.x))
                    job_frames_set = set(scene_sample_frames)
                    if s.keep_keyframes or s.export_animation_type == "keys":
                        job_frames_set.update(kfs)
                    job_frames = sorted(f for f in job_frames_set if sfra <= f <= efra)

                    job_obj_mats: dict[int, Matrix] = {}
                    job_bone_mats: dict[str, dict[int, Matrix]] = {}
                    for frame_idx in job_frames:
                        self.scene.frame_set(frame_idx)
                        job_obj_mats[frame_idx] = _get_object_local_matrix(ob)
                        if ob.type == "ARMATURE" and ob.pose:
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
                                job_bone_mats.setdefault(bone.name, {})[frame_idx] = bmat

                    ob_id = self.id_map.translate_id(ob.name)
                    act_id = self.id_map.translate_id(act.name)
                    self.sw.open_element(
                        "animation",
                        [("id", f"action_container-{ob_id}-{act_id}"), ("name", encode_xml(act.name))],
                    )
                    if export_as_matrix:
                        self._export_object_matrix_animation(ob, job_obj_mats)
                    self._export_object_curve_animations(ob, job_obj_mats, job_bone_mats, export_as_matrix)
                    if ob.type == "ARMATURE" and export_as_matrix:
                        for root_bone in ob.data.bones:
                            if root_bone.parent is None:
                                self._export_bone_matrix_animations_recursive(ob, root_bone, job_bone_mats)
                    self.sw.close_element()
                finally:
                    if had_adt:
                        ob.animation_data.action = orig_act
                        if hasattr(ob.animation_data, "action_slot"):
                            ob.animation_data.action_slot = orig_slot
                    else:
                        ob.animation_data_clear()

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
