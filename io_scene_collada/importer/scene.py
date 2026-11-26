# SPDX-FileCopyrightText: 2009-2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Asset/units/up-axis, cameras, lights, node transforms, collections, and node filtering."""

from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from typing import TYPE_CHECKING

import bpy
from mathutils import Matrix, Vector

from ..collada_utils import dae_flat_to_matrix, fov_to_focallength
from .xml_parse import (
    _find_child,
    _find_children,
    _parse_extra_blender,
    _parse_floats,
    _safe_float,
    _safe_int,
    _strip_ns,
    _uri_to_local_id,
)

if TYPE_CHECKING:
    from bpy.types import Object


class SceneMixin:
    def _compute_node_filter_plan(self, root: ET.Element, node_filter: set[str]) -> None:
        """Pre-scan visual scenes to determine kept nodes, geometries, materials, and images."""
        lib_nodes: dict[str, ET.Element] = {}
        for lib in _find_children(root, "library_nodes"):
            for n in _find_children(lib, "node"):
                nid = n.attrib.get("id", "")
                if nid:
                    lib_nodes[nid] = n

        kept_nodes: set[int] = set()
        kept_geoms: set[str] = set()
        kept_ctrls: set[str] = set()
        kept_mats: set[str] = set()

        def _collect_bindings(inst_el: ET.Element) -> None:
            bm = _find_child(inst_el, "bind_material")
            if bm is None:
                return
            tc = _find_child(bm, "technique_common")
            if tc is None:
                return
            for im in _find_children(tc, "instance_material"):
                tid = _uri_to_local_id(im.attrib.get("target"))
                if tid:
                    kept_mats.add(tid)

        def _walk_node(node: ET.Element, visiting: set[int]) -> bool:
            key = id(node)
            if key in visiting:
                return False
            visiting.add(key)
            try:
                node_name = node.attrib.get("name") or node.attrib.get("id") or ""
                node_id = node.attrib.get("id", "")
                direct_match = (node_name in node_filter) or (bool(node_id) and node_id in node_filter)
                if not direct_match:
                    for ig in _find_children(node, "instance_geometry"):
                        iname = ig.attrib.get("name", "")
                        if iname and iname in node_filter:
                            direct_match = True
                            break

                child_match = False
                for ch in _find_children(node, "node"):
                    if _walk_node(ch, visiting):
                        child_match = True
                for ino in _find_children(node, "instance_node"):
                    src_n = lib_nodes.get(_uri_to_local_id(ino.attrib.get("url")))
                    if src_n is not None and _walk_node(src_n, visiting):
                        child_match = True

                if direct_match or child_match:
                    kept_nodes.add(key)
                    if direct_match:
                        for ig in _find_children(node, "instance_geometry"):
                            gid = _uri_to_local_id(ig.attrib.get("url"))
                            if gid:
                                kept_geoms.add(gid)
                            _collect_bindings(ig)
                        for ic in _find_children(node, "instance_controller"):
                            cid = _uri_to_local_id(ic.attrib.get("url"))
                            if cid:
                                kept_ctrls.add(cid)
                            _collect_bindings(ic)
                    return True
                return False
            finally:
                visiting.discard(key)

        for lib in _find_children(root, "library_visual_scenes"):
            for vscene in _find_children(lib, "visual_scene"):
                for node in _find_children(vscene, "node"):
                    _walk_node(node, set())

        # Resolve geometry IDs referenced by kept controllers (skin / morph)
        if kept_ctrls:
            for lib in _find_children(root, "library_controllers"):
                for ctrl in _find_children(lib, "controller"):
                    cid = ctrl.attrib.get("id", "")
                    if cid not in kept_ctrls:
                        continue
                    skin = _find_child(ctrl, "skin")
                    if skin is not None:
                        gid = _uri_to_local_id(skin.attrib.get("source"))
                        if gid:
                            kept_geoms.add(gid)
                    morph = _find_child(ctrl, "morph")
                    if morph is not None:
                        gid = _uri_to_local_id(morph.attrib.get("source"))
                        if gid:
                            kept_geoms.add(gid)

        # Resolve image IDs referenced by kept materials -> effects
        kept_effs: set[str] = set()
        for lib in _find_children(root, "library_materials"):
            for mat_elem in _find_children(lib, "material"):
                if mat_elem.attrib.get("id", "") in kept_mats:
                    ie = _find_child(mat_elem, "instance_effect")
                    if ie is not None:
                        eid = _uri_to_local_id(ie.attrib.get("url"))
                        if eid:
                            kept_effs.add(eid)
        kept_imgs: set[str] = set()
        for lib in _find_children(root, "library_effects"):
            for eff_elem in _find_children(lib, "effect"):
                if eff_elem.attrib.get("id", "") not in kept_effs:
                    continue
                profile = _find_child(eff_elem, "profile_COMMON")
                if profile is None:
                    continue
                surface_to_img: dict[str, str] = {}
                sampler_to_surface: dict[str, str] = {}
                for np_el in _find_children(profile, "newparam"):
                    sid = np_el.attrib.get("sid", "")
                    surf = _find_child(np_el, "surface")
                    if surf is not None:
                        init_f = _find_child(surf, "init_from")
                        if init_f is not None and init_f.text:
                            img_ref = init_f.text.strip()
                            kept_imgs.add(img_ref)
                            if sid:
                                surface_to_img[sid] = img_ref
                    samp = _find_child(np_el, "sampler2D")
                    if samp is not None:
                        src = _find_child(samp, "source")
                        if src is not None and src.text and sid:
                            sampler_to_surface[sid] = src.text.strip()
                # Also keep images referenced directly via <texture texture="img_id"/>
                for el in profile.iter():
                    if _strip_ns(el.tag) == "texture":
                        tex_ref = el.attrib.get("texture", "").strip()
                        if tex_ref:
                            surf_ref = sampler_to_surface.get(tex_ref, tex_ref)
                            img_ref = surface_to_img.get(surf_ref, surf_ref)
                            if img_ref:
                                kept_imgs.add(img_ref)

        self._kept_nodes = kept_nodes
        self._allowed_geom_ids = kept_geoms
        self._allowed_mat_ids = kept_mats
        self._allowed_img_ids = kept_imgs

    def _read_asset(self, asset: ET.Element) -> None:
        unit = _find_child(asset, "unit")
        if unit is not None:
            self.unit_name = unit.attrib.get("name", "meter").lower()
            try:
                self.unit_meter = float(unit.attrib.get("meter", "1.0"))
            except ValueError:
                self.unit_meter = 1.0
        up = _find_child(asset, "up_axis")
        if up is not None and up.text:
            self.up_axis = up.text.strip().upper()
        contrib = _find_child(asset, "contributor")
        if contrib is not None:
            tool = _find_child(contrib, "authoring_tool")
            if tool is not None and tool.text and tool.text.startswith("Blender "):
                parts = tool.text[len("Blender "):].split(" ", 1)
                if parts:
                    self.import_from_version = parts[0]

    def _get_up_axis_matrix(self) -> Matrix:
        if self.up_axis == "X_UP":
            return Matrix.Rotation(-math.pi / 2.0, 4, "Y")
        if self.up_axis == "Y_UP":
            return Matrix.Rotation(math.pi / 2.0, 4, "X")
        return Matrix.Identity(4)

    def _get_unit_scale_matrix(self) -> Matrix:
        us = self.scene.unit_settings
        bl_scale = 1.0 if us.system == "NONE" else us.scale_length
        factor = self.unit_meter / bl_scale if bl_scale else 1.0
        return Matrix.Diagonal((factor, factor, factor, 1.0))

    def _apply_unit_and_up_axis(self) -> None:
        rot_m = self._get_up_axis_matrix()
        scale_m = self._get_unit_scale_matrix() if not self.settings.import_units else Matrix.Identity(4)
        if rot_m == Matrix.Identity(4) and scale_m == Matrix.Identity(4):
            return
        for ob in self.root_objects:
            if ob.parent is None:
                mat = rot_m @ scale_m @ ob.matrix_basis
                ob.matrix_basis = mat

    def _read_library_cameras(self, lib: ET.Element) -> None:
        for cam_elem in _find_children(lib, "camera"):
            cam_id = cam_elem.attrib.get("id", "")
            cam_name = cam_elem.attrib.get("name") or cam_id
            cam = bpy.data.cameras.new(name=cam_name)

            extras = _parse_extra_blender(cam_elem)
            if "shiftx" in extras:
                cam.shift_x = _safe_float(extras["shiftx"])
            if "shifty" in extras:
                cam.shift_y = _safe_float(extras["shifty"])
            if "dof_distance" in extras:
                cam.dof.focus_distance = _safe_float(extras["dof_distance"])

            optics = _find_child(cam_elem, "optics")
            if optics is not None:
                tc = _find_child(optics, "technique_common")
                if tc is not None:
                    persp = _find_child(tc, "perspective")
                    ortho = _find_child(tc, "orthographic")
                    if persp is not None:
                        cam.type = "PERSP"
                        zn = _find_child(persp, "znear")
                        zf = _find_child(persp, "zfar")
                        if zn is not None and zn.text:
                            cam.clip_start = max(1e-6, _safe_float(zn.text, cam.clip_start))
                        if zf is not None and zf.text:
                            cam.clip_end = max(1e-6, _safe_float(zf.text, cam.clip_end))
                        xfov_el = _find_child(persp, "xfov")
                        yfov_el = _find_child(persp, "yfov")
                        ar_el = _find_child(persp, "aspect_ratio")
                        if xfov_el is not None and xfov_el.text:
                            xfv = _safe_float(xfov_el.text, 0.0)
                            if 0.0 < xfv < 180.0:
                                cam.lens = fov_to_focallength(math.radians(xfv), cam.sensor_width)
                        elif yfov_el is not None and yfov_el.text:
                            yfv = _safe_float(yfov_el.text, 0.0)
                            if 0.0 < yfv < 180.0:
                                yfov = math.radians(yfv)
                                if ar_el is not None and ar_el.text:
                                    aspect = _safe_float(ar_el.text, 1.0)
                                    if aspect > 0.0:
                                        xfov = 2.0 * math.atan(aspect * math.tan(yfov * 0.5))
                                        cam.lens = fov_to_focallength(xfov, cam.sensor_width)
                                else:
                                    cam.lens = fov_to_focallength(yfov, cam.sensor_width)
                    elif ortho is not None:
                        cam.type = "ORTHO"
                        zn = _find_child(ortho, "znear")
                        zf = _find_child(ortho, "zfar")
                        if zn is not None and zn.text:
                            cam.clip_start = max(1e-6, _safe_float(zn.text, cam.clip_start))
                        if zf is not None and zf.text:
                            cam.clip_end = max(1e-6, _safe_float(zf.text, cam.clip_end))
                        xmag_el = _find_child(ortho, "xmag")
                        ymag_el = _find_child(ortho, "ymag")
                        ar_el = _find_child(ortho, "aspect_ratio")
                        if xmag_el is not None and xmag_el.text:
                            cam.ortho_scale = max(1e-6, _safe_float(xmag_el.text, 3.0) * 2.0)
                        elif ymag_el is not None and ymag_el.text:
                            ymag = _safe_float(ymag_el.text, 3.0)
                            if ar_el is not None and ar_el.text:
                                cam.ortho_scale = max(1e-6, 2.0 * ymag * _safe_float(ar_el.text, 1.0))
                            else:
                                cam.ortho_scale = max(1e-6, ymag)

            self.cameras_by_id[cam_id] = cam

    def _read_library_lights(self, lib: ET.Element) -> None:
        int_to_type = {0: "POINT", 1: "SUN", 2: "SPOT", 4: "AREA"}
        int_to_shape = {0: "SQUARE", 1: "RECTANGLE", 4: "DISK", 5: "ELLIPSE"}

        for la_elem in _find_children(lib, "light"):
            la_id = la_elem.attrib.get("id", "")
            la_name = la_elem.attrib.get("name") or la_id
            extras = _parse_extra_blender(la_elem)

            if extras and "type" in extras:
                la_type = int_to_type.get(_safe_int(extras["type"], 0), "POINT")
                la = bpy.data.lights.new(name=la_name, type=la_type)
                la_key = la.name
                la.color = (
                    _safe_float(extras.get("red"), 1.0),
                    _safe_float(extras.get("green"), 1.0),
                    _safe_float(extras.get("blue"), 1.0),
                )
                if "energy" in extras:
                    la.energy = _safe_float(extras["energy"], la.energy)
                if "mode" in extras:
                    mode = _safe_int(extras["mode"], 0)
                    la.use_shadow = bool(mode & 1)
                    if hasattr(la, "normalize"):
                        la.normalize = not bool(mode & (1 << 22))
                if "radius" in extras and hasattr(la, "shadow_soft_size"):
                    la.shadow_soft_size = _safe_float(extras["radius"], la.shadow_soft_size)
                if "clipend" in extras and hasattr(la, "cutoff_distance"):
                    la.cutoff_distance = _safe_float(extras["clipend"], la.cutoff_distance)
                # Match DocumentImporter.cpp:999-1008: write spot & area DNA fields
                # regardless of final light type so round-trips preserve DNA values.
                if "spotsize" in extras or "spotblend" in extras:
                    if la.type != "SPOT":
                        la.type = "SPOT"
                        la = bpy.data.lights[la_key]
                    if "spotsize" in extras:
                        la.spot_size = math.radians(_safe_float(extras["spotsize"], 45.0))
                    if "spotblend" in extras:
                        la.spot_blend = _safe_float(extras["spotblend"], la.spot_blend)
                if "area_shape" in extras or "area_size" in extras or "area_sizey" in extras:
                    if la.type != "AREA":
                        la.type = "AREA"
                        la = bpy.data.lights[la_key]
                    if "area_shape" in extras:
                        la.shape = int_to_shape.get(_safe_int(extras["area_shape"], 1), "RECTANGLE")
                    if "area_size" in extras:
                        la.size = _safe_float(extras["area_size"], la.size)
                    if "area_sizey" in extras and la.shape in ("RECTANGLE", "ELLIPSE"):
                        la.size_y = _safe_float(extras["area_sizey"], la.size_y)
                if la.type != la_type:
                    la.type = la_type
                    la = bpy.data.lights[la_key]
            else:
                tc = _find_child(la_elem, "technique_common")
                la_type = "POINT"
                sub = None
                if tc is not None:
                    for tag, btype in (
                        ("directional", "SUN"),
                        ("spot", "SPOT"),
                        ("point", "POINT"),
                        ("ambient", "SUN"),
                    ):
                        sub = _find_child(tc, tag)
                        if sub is not None:
                            la_type = btype
                            break
                la = bpy.data.lights.new(name=la_name, type=la_type)
                la.energy = 1.0
                if sub is not None:
                    col_el = _find_child(sub, "color")
                    if col_el is not None:
                        c = _parse_floats(col_el.text)
                        if len(c) >= 3:
                            la.color = (c[0], c[1], c[2])
                    if la_type == "SPOT":
                        fa = _find_child(sub, "falloff_angle")
                        fe = _find_child(sub, "falloff_exponent")
                        if fa is not None and fa.text:
                            la.spot_size = math.radians(_safe_float(fa.text, 45.0))
                        if fe is not None and fe.text:
                            la.spot_blend = _safe_float(fe.text, la.spot_blend)

            self.lights_by_id[la_id] = la

    def _read_node_matrix(self, node: ET.Element) -> Matrix:
        mat = Matrix.Identity(4)
        for ch in node:
            tag = _strip_ns(ch.tag)
            if tag == "matrix":
                vals = _parse_floats(ch.text)
                if len(vals) == 16:
                    return dae_flat_to_matrix(vals)
            elif tag == "translate":
                vals = _parse_floats(ch.text)
                if len(vals) >= 3:
                    mat = mat @ Matrix.Translation((vals[0], vals[1], vals[2]))
            elif tag == "rotate":
                vals = _parse_floats(ch.text)
                if len(vals) >= 4:
                    axis = Vector((vals[0], vals[1], vals[2]))
                    if axis.length > 0.0:
                        mat = mat @ Matrix.Rotation(math.radians(vals[3]), 4, axis)
            elif tag == "scale":
                vals = _parse_floats(ch.text)
                if len(vals) >= 3:
                    mat = mat @ Matrix.Diagonal((vals[0], vals[1], vals[2], 1.0))
        return mat

    def _node_is_decomposed(self, node: ET.Element) -> bool:
        for ch in node:
            if _strip_ns(ch.tag) == "matrix":
                return False
        return True

    def _add_object_to_scene(self, name: str, data) -> Object:
        ob = bpy.data.objects.new(name=name, object_data=data)
        self.target_collection.objects.link(ob)
        ob.select_set(True)
        self.view_layer.objects.active = ob
        return ob

    def _process_node(
        self,
        node: ET.Element,
        parent_node: ET.Element | None,
        parent_ob: Object | None,
        parent_bone: str | None = None,
    ) -> None:
        if self._kept_nodes is not None and id(node) not in self._kept_nodes:
            return
        node_id = node.attrib.get("id", "")
        node_name = node.attrib.get("name") or node_id or "Node"
        node_type = node.attrib.get("type", "NODE").upper()
        if node_id:
            self.node_elem_by_id[node_id] = node

        child_nodes = _find_children(node, "node")

        if node_type == "JOINT":
            if parent_node is None:
                arm_data = bpy.data.armatures.new(name="Armature")
                parent_ob = self._add_object_to_scene("Armature", arm_data)
                self.root_objects.append(parent_ob)
            if parent_node is None or parent_node.attrib.get("type", "NODE").upper() != "JOINT":
                if parent_ob is not None and parent_ob.type == "ARMATURE":
                    self.root_joint_nodes.append((node, parent_ob))
            return

        # When node_filter is active, only instantiate geometries on directly matched nodes
        # (ancestor nodes that were kept only for hierarchy become Empties carrying transforms).
        nf = self.settings.node_filter
        direct_node_match = (
            nf is None
            or (node_name in nf)
            or (bool(node_id) and node_id in nf)
        )
        inst_geoms = _find_children(node, "instance_geometry")
        if nf is not None and not direct_node_match:
            inst_geoms = [ig for ig in inst_geoms if ig.attrib.get("name", "") in nf]
        inst_ctrls = _find_children(node, "instance_controller") if direct_node_match else []
        inst_cams = _find_children(node, "instance_camera") if nf is None else []
        inst_lights = _find_children(node, "instance_light") if nf is None else []
        inst_nodes = _find_children(node, "instance_node")

        created: list[Object] = []

        for ig in inst_geoms:
            gid = _uri_to_local_id(ig.attrib.get("url"))
            me = self.meshes_by_id.get(gid)
            if me is not None:
                ob = self._add_object_to_scene(node_name, me)
                self._apply_material_bindings(ig, ob, gid)
                self.object_by_geom_id[gid] = ob
                self.imported_mesh_objects.append(ob)
                self.imported_objects_by_node_name.setdefault(node_name, []).append(ob)
                created.append(ob)

        for ic in inst_ctrls:
            cid = _uri_to_local_id(ic.attrib.get("url"))
            gid = ""
            if cid in self.skins_by_id:
                gid = self.skins_by_id[cid].geometry_id
            elif cid in self.morphs_by_id:
                gid = self.morphs_by_id[cid].geometry_id
            me = self.meshes_by_id.get(gid)
            if me is not None:
                ob = self._add_object_to_scene(node_name, me)
                self._apply_material_bindings(ic, ob, gid)
                self.object_by_geom_id[gid] = ob
                self.imported_mesh_objects.append(ob)
                self.imported_objects_by_node_name.setdefault(node_name, []).append(ob)
                created.append(ob)

        for icam in inst_cams:
            cam_id = _uri_to_local_id(icam.attrib.get("url"))
            cam = self.cameras_by_id.get(cam_id)
            if cam is not None:
                created.append(self._add_object_to_scene(node_name, cam))

        for ila in inst_lights:
            la_id = _uri_to_local_id(ila.attrib.get("url"))
            la = self.lights_by_id.get(la_id)
            if la is not None:
                created.append(self._add_object_to_scene(node_name, la))

        for ino in inst_nodes:
            target_id = _uri_to_local_id(ino.attrib.get("url"))
            src_node = self.lib_nodes_by_id.get(target_id)
            if src_node is not None:
                self._process_node(src_node, parent_node, parent_ob)

        if not created and not inst_nodes:
            is_arm = any(c.attrib.get("type", "NODE").upper() == "JOINT" for c in child_nodes)
            if is_arm:
                arm_data = bpy.data.armatures.new(name=node_name)
                ob = self._add_object_to_scene(node_name, arm_data)
                self._apply_armature_collections(node, arm_data)
            else:
                ob = self._add_object_to_scene(node_name, None)
            created.append(ob)

        mat = self._read_node_matrix(node)
        for ob in created:
            if parent_ob is not None:
                ob.parent = parent_ob
                if parent_bone and parent_ob.type == "ARMATURE" and parent_bone in parent_ob.data.bones:
                    ob.parent_type = "BONE"
                    ob.parent_bone = parent_bone
            else:
                self.root_objects.append(ob)
            ob.matrix_basis = mat
            if node_id:
                self.object_by_node_id[node_id] = ob

        primary_ob = created[0] if created else parent_ob
        for ch in child_nodes:
            self._process_node(ch, node, primary_ob)
