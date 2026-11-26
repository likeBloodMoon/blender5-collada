# SPDX-FileCopyrightText: 2009-2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Animation library reading, F-curve population, and object/joint animation import."""

from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import TYPE_CHECKING

from mathutils import Matrix

from ..collada_utils import (
    create_fcurve_on_channelbag,
    dae_flat_to_matrix,
    ensure_action_and_channelbag,
)
from .xml_parse import (
    _find_child,
    _find_children,
    _parse_floats,
    _parse_strings,
    _safe_int,
    _slice_accessor_array,
    _strip_ns,
    _uri_to_local_id,
)

if TYPE_CHECKING:
    from bpy.types import Object

@dataclass
class _AnimChannelData:
    target: str
    times: list[float]
    values: list[float]
    stride: int
    interps: list[str]
    in_tangents: list[float]
    out_tangents: list[float]


class AnimationMixin:
    def _read_library_animations(self, lib: ET.Element) -> None:
        for anim in _find_children(lib, "animation"):
            self._read_animation_elem(anim)

    def _read_animation_elem(self, anim: ET.Element) -> None:
        # Recurse nested <animation> elements
        for sub in _find_children(anim, "animation"):
            self._read_animation_elem(sub)

        float_sources: dict[str, tuple[list[float], int]] = {}
        name_sources: dict[str, list[str]] = {}
        for src in _find_children(anim, "source"):
            sid = src.attrib.get("id", "")
            stride = 1
            acc = None
            tc = _find_child(src, "technique_common")
            if tc is not None:
                acc = _find_child(tc, "accessor")
                if acc is not None and "stride" in acc.attrib:
                    try:
                        stride = int(acc.attrib["stride"])
                    except ValueError:
                        self.warnings.append(
                            f"Invalid stride '{acc.attrib['stride']}' in animation source '{sid}'"
                        )
                        continue
            if stride <= 0:
                self.warnings.append(f"Invalid stride {stride} in animation source '{sid}'")
                continue
            fa = _find_child(src, "float_array")
            if fa is not None:
                floats_raw = _parse_floats(fa.text)
                float_sources[sid] = (
                    _slice_accessor_array(floats_raw, acc, stride, self.warnings, f"animation source '{sid}'"),
                    stride,
                )
            na = _find_child(src, "Name_array")
            if na is None:
                na = _find_child(src, "IDREF_array")
            if na is not None:
                names_raw = _parse_strings(na.text)
                name_sources[sid] = _slice_accessor_array(
                    names_raw, acc, stride, self.warnings, f"animation source '{sid}'"
                )

        samplers: dict[str, dict[str, str]] = {}
        for samp in _find_children(anim, "sampler"):
            sid = samp.attrib.get("id", "")
            smap: dict[str, str] = {}
            for inp in _find_children(samp, "input"):
                smap[inp.attrib.get("semantic", "")] = _uri_to_local_id(inp.attrib.get("source"))
            samplers[sid] = smap

        for ch in _find_children(anim, "channel"):
            samp_id = _uri_to_local_id(ch.attrib.get("source"))
            target = ch.attrib.get("target", "")
            smap = samplers.get(samp_id)
            if not smap or not target:
                continue
            times, _ = float_sources.get(smap.get("INPUT", ""), ([], 1))
            values, stride = float_sources.get(smap.get("OUTPUT", ""), ([], 1))
            interps = name_sources.get(smap.get("INTERPOLATION", ""), [])
            in_tan, _ = float_sources.get(smap.get("IN_TANGENT", ""), ([], 2))
            out_tan, _ = float_sources.get(smap.get("OUT_TANGENT", ""), ([], 2))
            if times and values:
                self.animations.append(
                    _AnimChannelData(
                        target=target,
                        times=times,
                        values=values,
                        stride=stride,
                        interps=interps,
                        in_tangents=in_tan,
                        out_tangents=out_tan,
                    )
                )

    def _apply_animations(self) -> None:
        if not self.animations:
            return
        fps = self.scene.render.fps / self.scene.render.fps_base

        for ch in self.animations:
            if "/" not in ch.target:
                continue
            target_id, prop_part = ch.target.split("/", 1)
            axis_part = ""
            if "." in prop_part:
                prop_name, axis_part = prop_part.split(".", 1)
            else:
                prop_name = prop_part

            # Check if target is a joint node
            if target_id in self.joint_arm_by_id:
                ob_arm = self.joint_arm_by_id[target_id]
                bname = self.joint_bone_name_by_id[target_id]
                jnode = self.joint_elem_by_id[target_id]
                if ch.stride == 16:
                    self._apply_joint_matrix_animation(ob_arm, bname, jnode, ch, fps)
                else:
                    self._apply_decomposed_animation(
                        ob_arm, f'pose.bones["{bname}"].', prop_name, axis_part, ch, fps, node_elem=jnode
                    )
                continue

            # Check if target is an object node
            ob = self.object_by_node_id.get(target_id)
            if ob is not None:
                if ch.stride == 16:
                    self._apply_object_matrix_animation(ob, ch, fps)
                else:
                    self._apply_decomposed_animation(
                        ob, "", prop_name, axis_part, ch, fps, node_elem=self.node_elem_by_id.get(target_id)
                    )
                continue

            # Check camera
            if target_id in self.cameras_by_id:
                cam = self.cameras_by_id[target_id]
                _, cbag = ensure_action_and_channelbag(cam)
                if prop_name == "xfov":
                    fcu = create_fcurve_on_channelbag(cbag, "lens", 0)
                    vals = [fov_to_focallength(math.radians(v), cam.sensor_width) for v in ch.values]
                    self._populate_fcurve(fcu, ch.times, vals, ch.interps, fps)
                elif prop_name == "znear":
                    fcu = create_fcurve_on_channelbag(cbag, "clip_start", 0)
                    self._populate_fcurve(fcu, ch.times, ch.values, ch.interps, fps)
                elif prop_name == "zfar":
                    fcu = create_fcurve_on_channelbag(cbag, "clip_end", 0)
                    self._populate_fcurve(fcu, ch.times, ch.values, ch.interps, fps)
                elif prop_name == "xmag":
                    fcu = create_fcurve_on_channelbag(cbag, "ortho_scale", 0)
                    self._populate_fcurve(fcu, ch.times, [v * 2.0 for v in ch.values], ch.interps, fps)
                continue

            # Check light
            if target_id in self.lights_by_id:
                la = self.lights_by_id[target_id]
                _, cbag = ensure_action_and_channelbag(la)
                if prop_name == "color":
                    axis_idx = {"R": 0, "G": 1, "B": 2}.get(axis_part.upper(), -1)
                    if axis_idx >= 0:
                        fcu = create_fcurve_on_channelbag(cbag, "color", axis_idx)
                        self._populate_fcurve(fcu, ch.times, ch.values, ch.interps, fps)
                elif prop_name in ("falloff_angle", "fall_off_angle"):
                    fcu = create_fcurve_on_channelbag(cbag, "spot_size", 0)
                    self._populate_fcurve(fcu, ch.times, [math.radians(v) for v in ch.values], ch.interps, fps)
                elif prop_name in ("falloff_exponent", "fall_off_exponent"):
                    fcu = create_fcurve_on_channelbag(cbag, "spot_blend", 0)
                    self._populate_fcurve(fcu, ch.times, ch.values, ch.interps, fps)

    def _populate_fcurve(
        self,
        fcu,
        times: list[float],
        values: list[float],
        interps: list[str],
        fps: float,
        in_tan: list[float] | None = None,
        out_tan: list[float] | None = None,
        is_angle: bool = False,
    ) -> None:
        count = min(len(times), len(values))
        fcu.keyframe_points.add(count)
        for i in range(count):
            kp = fcu.keyframe_points[i]
            kp.co = (times[i] * fps, values[i])
            interp = interps[i] if i < len(interps) else "LINEAR"
            if interp == "BEZIER":
                kp.interpolation = "BEZIER"
                if in_tan and 2 * i + 1 < len(in_tan):
                    hy = math.radians(in_tan[2 * i + 1]) if is_angle else in_tan[2 * i + 1]
                    kp.handle_left = (in_tan[2 * i] * fps, hy)
                if out_tan and 2 * i + 1 < len(out_tan):
                    hy = math.radians(out_tan[2 * i + 1]) if is_angle else out_tan[2 * i + 1]
                    kp.handle_right = (out_tan[2 * i] * fps, hy)
            elif interp == "STEP":
                kp.interpolation = "CONSTANT"
            else:
                kp.interpolation = "LINEAR"
        fcu.update()

    def _apply_object_matrix_animation(self, ob: Object, ch: _AnimChannelData, fps: float) -> None:
        count = min(len(ch.times), len(ch.values) // 16)
        if count == 0:
            return
        _, cbag = ensure_action_and_channelbag(ob)
        ob.rotation_mode = "QUATERNION"
        locs: list[list[float]] = [[], [], []]
        quats: list[list[float]] = [[], [], [], []]
        scales: list[list[float]] = [[], [], []]
        times = ch.times[:count]
        for i in range(count):
            m = dae_flat_to_matrix(ch.values[i * 16:(i + 1) * 16])
            loc, quat, scale = m.decompose()
            for a in range(3):
                locs[a].append(loc[a])
                scales[a].append(scale[a])
            for a in range(4):
                quats[a].append(quat[a])

        for a in range(4):
            fcu = create_fcurve_on_channelbag(cbag, "rotation_quaternion", a)
            self._populate_fcurve(fcu, times, quats[a], ch.interps, fps)
        for a in range(3):
            fcu = create_fcurve_on_channelbag(cbag, "location", a)
            self._populate_fcurve(fcu, times, locs[a], ch.interps, fps)
        for a in range(3):
            fcu = create_fcurve_on_channelbag(cbag, "scale", a)
            self._populate_fcurve(fcu, times, scales[a], ch.interps, fps)

    def _apply_joint_matrix_animation(
        self, ob_arm: Object, bname: str, jnode: ET.Element, ch: _AnimChannelData, fps: float
    ) -> None:
        count = min(len(ch.times), len(ch.values) // 16)
        if count == 0:
            return
        bone = ob_arm.data.bones.get(bname)
        pchan = ob_arm.pose.bones.get(bname)
        if bone is None or pchan is None:
            return
        _, cbag = ensure_action_and_channelbag(ob_arm)
        pchan.rotation_mode = "QUATERNION"

        # Compute rest matrices (AnimationImporter::apply_matrix_curves formula: iR @ M @ iR_dae @ R)
        rest_local = self._read_node_matrix(jnode)
        rest = bone.matrix_local.copy()
        parent_rest = bone.parent.matrix_local.copy() if bone.parent else Matrix.Identity(4)
        try:
            irest = rest.inverted()
            iparent_rest_local = (parent_rest @ rest_local).inverted()
        except ValueError:
            self.warnings.append(f"Bone '{bname}': singular rest matrix in matrix animation; skipped")
            return

        prefix = f'pose.bones["{bname}"].'
        locs: list[list[float]] = [[], [], []]
        quats: list[list[float]] = [[], [], [], []]
        scales: list[list[float]] = [[], [], []]
        times = ch.times[:count]

        for i in range(count):
            matfra = dae_flat_to_matrix(ch.values[i * 16:(i + 1) * 16])
            temp = parent_rest @ matfra
            mat = irest @ temp @ iparent_rest_local @ rest
            loc, quat, scale = mat.decompose()
            for a in range(3):
                locs[a].append(loc[a])
                scales[a].append(scale[a])
            for a in range(4):
                quats[a].append(quat[a])

        for a in range(4):
            fcu = create_fcurve_on_channelbag(cbag, f"{prefix}rotation_quaternion", a)
            self._populate_fcurve(fcu, times, quats[a], ch.interps, fps)
        for a in range(3):
            fcu = create_fcurve_on_channelbag(cbag, f"{prefix}location", a)
            self._populate_fcurve(fcu, times, locs[a], ch.interps, fps)
        for a in range(3):
            fcu = create_fcurve_on_channelbag(cbag, f"{prefix}scale", a)
            self._populate_fcurve(fcu, times, scales[a], ch.interps, fps)

    def _resolve_transform_sid(self, node_elem: ET.Element | None, prop_name: str) -> tuple[str, int]:
        """Resolve a transform sid on node_elem to (kind, rot_axis_idx) for third-party DCCs."""
        if node_elem is not None:
            for child in node_elem:
                if child.attrib.get("sid") == prop_name:
                    tag = _strip_ns(child.tag)
                    if tag == "translate":
                        return ("location", -1)
                    if tag == "scale":
                        return ("scale", -1)
                    if tag == "rotate":
                        vals = _parse_floats(child.text)
                        if len(vals) >= 3:
                            ax = Vector((abs(vals[0]), abs(vals[1]), abs(vals[2])))
                            if ax.x >= ax.y and ax.x >= ax.z and ax.x > 0.5:
                                return ("rotation", 0)
                            if ax.y >= ax.x and ax.y >= ax.z and ax.y > 0.5:
                                return ("rotation", 1)
                            if ax.z > 0.5:
                                return ("rotation", 2)
        if prop_name in ("location", "translate"):
            return ("location", -1)
        if prop_name == "scale":
            return ("scale", -1)
        if prop_name in ("rotationX", "rotateX"):
            return ("rotation", 0)
        if prop_name in ("rotationY", "rotateY"):
            return ("rotation", 1)
        if prop_name in ("rotationZ", "rotateZ"):
            return ("rotation", 2)
        return ("", -1)

    def _apply_decomposed_animation(
        self,
        ob: Object,
        prefix: str,
        prop_name: str,
        axis_part: str,
        ch: _AnimChannelData,
        fps: float,
        node_elem: ET.Element | None = None,
    ) -> None:
        _, cbag = ensure_action_and_channelbag(ob)
        axis_map = {"X": 0, "Y": 1, "Z": 2}
        kind, rot_axis = self._resolve_transform_sid(node_elem, prop_name)
        if kind == "location":
            if ch.stride == 3:
                count3 = min(len(ch.times), len(ch.values) // 3)
                times3 = ch.times[:count3]
                for a in range(3):
                    vals = [ch.values[i * 3 + a] for i in range(count3)]
                    fcu = create_fcurve_on_channelbag(cbag, f"{prefix}location", a)
                    self._populate_fcurve(fcu, times3, vals, ch.interps, fps)
            else:
                a = axis_map.get(axis_part.upper(), 0)
                fcu = create_fcurve_on_channelbag(cbag, f"{prefix}location", a)
                self._populate_fcurve(
                    fcu, ch.times, ch.values, ch.interps, fps, ch.in_tangents, ch.out_tangents
                )
        elif kind == "scale":
            if ch.stride == 3:
                count3 = min(len(ch.times), len(ch.values) // 3)
                times3 = ch.times[:count3]
                for a in range(3):
                    vals = [ch.values[i * 3 + a] for i in range(count3)]
                    fcu = create_fcurve_on_channelbag(cbag, f"{prefix}scale", a)
                    self._populate_fcurve(fcu, times3, vals, ch.interps, fps)
            else:
                a = axis_map.get(axis_part.upper(), 0)
                fcu = create_fcurve_on_channelbag(cbag, f"{prefix}scale", a)
                self._populate_fcurve(
                    fcu, ch.times, ch.values, ch.interps, fps, ch.in_tangents, ch.out_tangents
                )
        elif kind == "rotation" and rot_axis >= 0:
            vals = [math.radians(v) for v in ch.values]
            fcu = create_fcurve_on_channelbag(cbag, f"{prefix}rotation_euler", rot_axis)
            self._populate_fcurve(
                fcu, ch.times, vals, ch.interps, fps, ch.in_tangents, ch.out_tangents, is_angle=True
            )
