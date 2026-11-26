# SPDX-FileCopyrightText: 2009-2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Complete Collada (.dae) importer faithfully ported from Blender 4.5.1 for Blender 5.0+."""

from __future__ import annotations

import math
import os
import re
import traceback
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from typing import TYPE_CHECKING

import bpy
import numpy as np
from mathutils import Matrix, Vector

from .collada_utils import (
    check_target_collection_visibility,
    create_fcurve_on_channelbag,
    dae_flat_to_matrix,
    ensure_action_and_channelbag,
    fov_to_focallength,
    set_editbone_property_matrix,
)

from .importer.archive import ArchiveMixin
from .importer.materials import MaterialsMixin
from .importer.mesh import MeshMixin
from .importer.rig import MINIMUM_BONE_LENGTH, RigMixin, _MorphData, _SkinData
from .importer.animation import AnimationMixin, _AnimChannelData
if TYPE_CHECKING:
    from bpy.types import Context, Material, Mesh, Object, Scene


from .importer.xml_parse import (
    _find_child,
    _find_children,
    _norm_archive_path,
    _parse_extra_blender,
    _parse_f32_np,
    _parse_floats,
    _parse_i32_np,
    _parse_ints,
    _parse_strings,
    _parse_xml_safe,
    _safe_float,
    _safe_int,
    _slice_accessor_array,
    _strip_ns,
    _uri_to_local_id,
    _uri_to_native_path,
)


@dataclass
class ImportSettings:
    filepath: str = ""
    import_units: bool = False
    custom_normals: bool = True
    fast_exact_normals: bool = False
    sharp_edges_from_normals: bool = False
    sharp_angle_threshold: float = math.radians(1.0)
    archive_mode: str = "ALL"  # "ALL" (all .dae into collections) or "MAIN" (single main .dae)
    node_filter: set[str] | None = None
    archive_entry_filter: list[str] | None = None
    find_chains: bool = False
    auto_connect: bool = False
    fix_orientation: bool = False
    min_chain_length: int = 0
    keep_bind_info: bool = False



class DocumentImporter(ArchiveMixin, MaterialsMixin, MeshMixin, RigMixin, AnimationMixin):
    """Complete Collada (.dae) DocumentImporter ported from Blender 4.5.1."""

    def __init__(self, context: Context, settings: ImportSettings) -> None:
        self.context = context
        self.scene: Scene = context.scene
        self.view_layer = context.view_layer
        alc = context.view_layer.active_layer_collection
        self.target_collection = alc.collection if alc is not None else context.scene.collection
        self.settings = settings
        self.dae_dir = os.path.dirname(os.path.abspath(settings.filepath))

        self._archive_zf: zipfile.ZipFile | None = None
        self._archive_basename: str = ""
        self._archive_dae_dir: str = ""
        self._archive_entries: dict[str, zipfile.ZipInfo] = {}
        self._archive_entries_ci: dict[str, tuple[str, zipfile.ZipInfo]] = {}
        self._archive_by_basename: dict[str, list[tuple[str, zipfile.ZipInfo]]] = {}
        self._archive_images_by_entry: dict[str, object] = {}
        self._archive_bytes_read: int = 0
        self._beamng_mat_maps: dict[str, list[str]] = {}
        self.errors: list[str] = []
        self.warnings: list[str] = []

        self._reset_dae_state()

    def _reset_dae_state(self) -> None:
        """Reset per-DAE parsing and scene instantiation state (preserves archive caches)."""
        # Asset info
        self.up_axis: str = "Z_UP"
        self.unit_name: str = "meter"
        self.unit_meter: float = 1.0
        self.import_from_version: str = ""

        # Parsed library maps
        self.images_by_id: dict[str, bpy.types.Image] = {}
        self.effects_by_id: dict[str, ET.Element] = {}
        self.materials_by_id: dict[str, Material] = {}
        self.cameras_by_id: dict[str, bpy.types.Camera] = {}
        self.lights_by_id: dict[str, bpy.types.Light] = {}
        self.meshes_by_id: dict[str, Mesh] = {}
        self.mesh_mat_prims: dict[str, dict[str, np.ndarray]] = {}
        self.skins_by_id: dict[str, _SkinData] = {}
        self.morphs_by_id: dict[str, _MorphData] = {}
        self.lib_nodes_by_id: dict[str, ET.Element] = {}
        self.animations: list[_AnimChannelData] = []

        # Scene instantiation state
        self.node_elem_by_id: dict[str, ET.Element] = {}
        self.object_by_node_id: dict[str, Object] = {}
        self.object_by_geom_id: dict[str, Object] = {}
        self.imported_mesh_objects: list[Object] = []
        self.imported_objects_by_node_name: dict[str, list[Object]] = {}
        self.root_objects: list[Object] = []
        self._kept_nodes: set[int] | None = None
        self._allowed_geom_ids: set[str] | None = None
        self._allowed_mat_ids: set[str] | None = None
        self._allowed_img_ids: set[str] | None = None

        # Armature tracking
        self.root_joint_nodes: list[tuple[ET.Element, Object]] = []  # (joint_elem, ob_arm)
        self.joint_elem_by_sid: dict[str, ET.Element] = {}
        self.joint_elem_by_id: dict[str, ET.Element] = {}
        self.joint_arm_by_id: dict[str, Object] = {}
        self.joint_bone_name_by_id: dict[str, str] = {}
        self.joint_parent_elem_by_id: dict[str, ET.Element | None] = {}

    def import_document(self) -> bool:
        ext = os.path.splitext(self.settings.filepath)[1].lower()
        if ext in (".zae", ".kmz", ".zip"):
            self._archive_basename = os.path.basename(self.settings.filepath)
            self._archive_images_by_entry = {}
            self._archive_bytes_read = 0
            self._beamng_mat_maps = {}
            base_collection = self.target_collection
            try:
                with zipfile.ZipFile(self.settings.filepath, "r") as zf:
                    self._archive_zf = zf
                    self._build_archive_index(zf)
                    dae_entries, from_manifest = self._find_archive_daes(zf)
                    if not dae_entries:
                        self.errors.append(
                            f"No .dae file found inside archive: {self._archive_basename}"
                        )
                        return False
                    self._load_archive_materials_json(zf)

                    if self.settings.archive_entry_filter is not None:
                        want_ci = {_norm_archive_path(p).lower() for p in self.settings.archive_entry_filter}
                        dae_entries = [e for e in dae_entries if e[0].lower() in want_ci]
                        from_manifest = False
                        if not dae_entries:
                            self.errors.append(
                                f"None of the selected .dae entries were found in '{self._archive_basename}'"
                            )
                            return False

                    if from_manifest or len(dae_entries) == 1 or self.settings.archive_mode == "MAIN":
                        if not from_manifest and len(dae_entries) > 1:
                            skipped = [n for n, _ in dae_entries[1:]]
                            preview = ", ".join(skipped[:4])
                            if len(skipped) > 4:
                                preview += f", ... (+{len(skipped) - 4} more)"
                            self.warnings.append(
                                f"Archive '{self._archive_basename}' contains {len(dae_entries)} .dae files; "
                                f"imported '{dae_entries[0][0]}' and skipped {len(skipped)} other(s) ({preview}). "
                                f"Set Archive Models to 'All models (collections)' to import all"
                            )
                        dae_norm, dae_info = dae_entries[0]
                        self._archive_dae_dir = dae_norm.rsplit("/", 1)[0] if "/" in dae_norm else ""
                        dae_bytes = self._read_zip_entry(zf, dae_info)
                        try:
                            tree = _parse_xml_safe(dae_bytes)
                        except Exception as e:
                            self.errors.append(f"Failed to parse XML: {e}")
                            return False
                        ok_main = self._safe_import_dae_tree(tree)
                        if ok_main:
                            vis_warn = check_target_collection_visibility(self.view_layer, base_collection)
                            if vis_warn:
                                self.warnings.append(vis_warn)
                        return ok_main

                    # Multi-DAE import ("ALL"): import every .dae into its own sub-collection
                    for ob in self.view_layer.objects:
                        ob.select_set(False)

                    stem_counts: dict[str, int] = {}
                    for dae_norm, _ in dae_entries:
                        st = os.path.splitext(dae_norm.rsplit("/", 1)[-1])[0].lower()
                        stem_counts[st] = stem_counts.get(st, 0) + 1

                    any_ok = False
                    for dae_norm, dae_info in dae_entries:
                        self._reset_dae_state()
                        self._archive_dae_dir = dae_norm.rsplit("/", 1)[0] if "/" in dae_norm else ""
                        dae_bytes = self._read_zip_entry(zf, dae_info)
                        try:
                            tree = _parse_xml_safe(dae_bytes)
                        except Exception as e:
                            self.warnings.append(f"Failed to parse XML in '{dae_norm}': {e}")
                            continue
                        bname = dae_norm.rsplit("/", 1)[-1]
                        stem = os.path.splitext(bname)[0] or "Model"
                        if stem_counts.get(stem.lower(), 0) > 1 and "/" in dae_norm:
                            parent_seg = dae_norm.rsplit("/", 2)[-2]
                            coll_name = f"{parent_seg}_{stem}"
                        else:
                            coll_name = stem
                        sub_coll = bpy.data.collections.new(coll_name)
                        base_collection.children.link(sub_coll)
                        self.target_collection = sub_coll
                        if self._safe_import_dae_tree(
                            tree,
                            deselect_existing=False,
                            entry_label=dae_norm,
                            as_warning=True,
                        ):
                            any_ok = True
                        elif len(sub_coll.objects) == 0 and len(sub_coll.children) == 0:
                            base_collection.children.unlink(sub_coll)
                            bpy.data.collections.remove(sub_coll)
                    if any_ok:
                        vis_warn = check_target_collection_visibility(self.view_layer, base_collection)
                        if vis_warn:
                            self.warnings.append(vis_warn)
                    return any_ok
            except Exception as e:
                self.errors.append(f"Archive import failed: {e}")
                return False
            finally:
                self.target_collection = base_collection
                self._archive_zf = None
                self._archive_entries = {}
                self._archive_entries_ci = {}
                self._archive_by_basename = {}
                self._archive_images_by_entry = {}
                self._archive_dae_dir = ""
                self._beamng_mat_maps = {}
        ok = self._import_dae_file(self.settings.filepath)
        if ok:
            vis_warn = check_target_collection_visibility(self.view_layer, self.target_collection)
            if vis_warn:
                self.warnings.append(vis_warn)
        return ok

    def _safe_import_dae_tree(
        self,
        tree: ET.ElementTree,
        *,
        deselect_existing: bool = True,
        entry_label: str | None = None,
        as_warning: bool = False,
    ) -> bool:
        """Run ``_import_dae_tree`` with clean error reporting and partial-datablock cleanup."""
        pre_objects = set(bpy.data.objects)
        pre_meshes = set(bpy.data.meshes)
        pre_armatures = set(bpy.data.armatures)
        pre_cameras = set(bpy.data.cameras)
        pre_lights = set(bpy.data.lights)
        pre_materials = set(bpy.data.materials)
        pre_images = set(bpy.data.images)
        pre_actions = set(bpy.data.actions)
        pre_collections = set(bpy.data.collections)
        try:
            return self._import_dae_tree(tree, deselect_existing=deselect_existing)
        except Exception as exc:
            traceback.print_exc()
            # Clean up any objects / datablocks partially created before the failure
            for ob in list(bpy.data.objects):
                if ob not in pre_objects:
                    try:
                        bpy.data.objects.remove(ob, do_unlink=True)
                    except Exception:
                        pass
            for data_coll, pre_set in (
                (bpy.data.meshes, pre_meshes),
                (bpy.data.armatures, pre_armatures),
                (bpy.data.cameras, pre_cameras),
                (bpy.data.lights, pre_lights),
                (bpy.data.materials, pre_materials),
                (bpy.data.images, pre_images),
                (bpy.data.actions, pre_actions),
                (bpy.data.collections, pre_collections),
            ):
                for item in list(data_coll):
                    if item not in pre_set:
                        try:
                            data_coll.remove(item)
                        except Exception:
                            pass
            msg = f"Import failed for '{entry_label}': {exc}" if entry_label else f"Import failed: {exc}"
            if as_warning:
                self.warnings.append(msg)
            else:
                self.errors.append(msg)
            return False

    def _import_dae_file(self, dae_path: str) -> bool:
        self._beamng_mat_maps = {}
        self._load_disk_materials_json(self.dae_dir)
        try:
            tree = _parse_xml_safe(dae_path)
        except Exception as e:
            self.errors.append(f"Failed to parse XML: {e}")
            return False
        return self._safe_import_dae_tree(tree)

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

    def _import_dae_tree(self, tree: ET.ElementTree, *, deselect_existing: bool = True) -> bool:

        root = tree.getroot()
        if _strip_ns(root.tag) != "COLLADA":
            self.errors.append("Root element is not <COLLADA>")
            return False

        # Deselect all existing objects
        if deselect_existing:
            for ob in self.view_layer.objects:
                ob.select_set(False)

        if self.settings.node_filter is not None:
            self._compute_node_filter_plan(root, self.settings.node_filter)
        else:
            self._kept_nodes = None
            self._allowed_geom_ids = None
            self._allowed_mat_ids = None
            self._allowed_img_ids = None

        # 1. Asset
        asset_elem = _find_child(root, "asset")
        if asset_elem is not None:
            self._read_asset(asset_elem)

        # 2. Libraries
        for lib in _find_children(root, "library_images"):
            self._read_library_images(lib)
        for lib in _find_children(root, "library_effects"):
            self._read_library_effects(lib)
        for lib in _find_children(root, "library_materials"):
            self._read_library_materials(lib)
        for lib in _find_children(root, "library_cameras"):
            self._read_library_cameras(lib)
        for lib in _find_children(root, "library_lights"):
            self._read_library_lights(lib)
        for lib in _find_children(root, "library_geometries"):
            self._read_library_geometries(lib)
        for lib in _find_children(root, "library_controllers"):
            self._read_library_controllers(lib)
        for lib in _find_children(root, "library_nodes"):
            for n in _find_children(lib, "node"):
                nid = n.attrib.get("id", "")
                if nid:
                    self.lib_nodes_by_id[nid] = n
        for lib in _find_children(root, "library_animations"):
            self._read_library_animations(lib)

        # 3. Apply units if requested
        if self.settings.import_units:
            us = self.scene.unit_settings
            if self.unit_name in ("millimeter", "centimeter", "decimeter", "meter", "kilometer"):
                us.system = "METRIC"
            elif self.unit_name in ("inch", "foot", "yard"):
                us.system = "IMPERIAL"
            else:
                us.system = "NONE"
            us.scale_length = self.unit_meter

        # 4. Visual Scenes
        for lib in _find_children(root, "library_visual_scenes"):
            for vscene in _find_children(lib, "visual_scene"):
                for node in _find_children(vscene, "node"):
                    self._process_node(node, None, None)

        # 5. Optimize material assignments (move identical object mats to mesh data)
        self._optimize_material_assignments()

        # 6. Build Armatures, link skins, build shape keys
        self._make_armatures()
        self._make_shape_keys()

        # 7. Import Animations
        self._apply_animations()

        # 8. Match scale & up-axis rotation on root objects
        self._apply_unit_and_up_axis()

        self.view_layer.update()
        return True

    # ------------------------------------------------------------------
    # <asset>
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # <library_images>
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # <library_effects> & <library_materials>
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # <library_cameras>
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # <library_lights>
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # <library_geometries>
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # <library_controllers>
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # <library_visual_scenes> Node Processing
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Armatures, Skinning & Shape Keys
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # <library_animations>
    # ------------------------------------------------------------------

