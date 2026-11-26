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

MINIMUM_BONE_LENGTH = 0.000001

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



@dataclass
class _SkinData:
    controller_id: str
    geometry_id: str
    bind_shape_matrix: Matrix
    joint_names: list[str]
    inv_bind_matrices: list[Matrix]
    vcounts: list[int]
    v_pairs: list[tuple[int, float]]  # flattened (joint_idx, weight)
    ob_arm: Object | None = None


@dataclass
class _MorphData:
    controller_id: str
    geometry_id: str
    target_geom_ids: list[str]
    weights: list[float]


@dataclass
class _AnimChannelData:
    target: str
    times: list[float]
    values: list[float]
    stride: int
    interps: list[str]
    in_tangents: list[float]
    out_tangents: list[float]


class DocumentImporter(ArchiveMixin, MaterialsMixin, MeshMixin):
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

    def _read_library_controllers(self, lib: ET.Element) -> None:
        for ctrl in _find_children(lib, "controller"):
            cid = ctrl.attrib.get("id", "")
            skin = _find_child(ctrl, "skin")
            if skin is not None:
                self._read_skin_controller(cid, skin)
            morph = _find_child(ctrl, "morph")
            if morph is not None:
                self._read_morph_controller(cid, morph)

    def _read_skin_controller(self, cid: str, skin: ET.Element) -> None:
        geom_id = _uri_to_local_id(skin.attrib.get("source"))
        bsm_el = _find_child(skin, "bind_shape_matrix")
        bsm_vals = _parse_floats(bsm_el.text) if bsm_el is not None else []
        bsm = dae_flat_to_matrix(bsm_vals) if len(bsm_vals) >= 16 else Matrix.Identity(4)

        name_sources: dict[str, list[str]] = {}
        float_sources: dict[str, tuple[list[float], int | None]] = {}
        for src in _find_children(skin, "source"):
            sid = src.attrib.get("id", "")
            stride_val: int | None = None
            acc = None
            tc = _find_child(src, "technique_common")
            if tc is not None:
                acc = _find_child(tc, "accessor")
                if acc is not None and "stride" in acc.attrib:
                    try:
                        stride_val = int(acc.attrib["stride"])
                    except ValueError:
                        self.warnings.append(
                            f"Invalid stride '{acc.attrib['stride']}' in skin '{cid}' source '{sid}'"
                        )
                        stride_val = 0
            eff_stride = stride_val if (stride_val is not None and stride_val > 0) else 1
            na = _find_child(src, "Name_array")
            if na is None:
                na = _find_child(src, "IDREF_array")
            if na is not None:
                names_raw = _parse_strings(na.text)
                name_sources[sid] = _slice_accessor_array(
                    names_raw, acc, eff_stride, self.warnings, f"skin '{cid}' source '{sid}'"
                )
            fa = _find_child(src, "float_array")
            if fa is not None:
                floats_raw = _parse_floats(fa.text)
                floats_sliced = _slice_accessor_array(
                    floats_raw, acc, eff_stride, self.warnings, f"skin '{cid}' source '{sid}'"
                )
                float_sources[sid] = (floats_sliced, stride_val)

        joint_names: list[str] = []
        inv_bind_mats: list[Matrix] = []
        joints_el = _find_child(skin, "joints")
        if joints_el is not None:
            for inp in _find_children(joints_el, "input"):
                sem = inp.attrib.get("semantic", "")
                sid = _uri_to_local_id(inp.attrib.get("source"))
                if sem == "JOINT":
                    joint_names = name_sources.get(sid, [])
                elif sem == "INV_BIND_MATRIX":
                    floats, s_stride = float_sources.get(sid, ([], None))
                    if s_stride is not None and s_stride < 16:
                        self.warnings.append(
                            f"Skin '{cid}': INV_BIND_MATRIX source '{sid}' has stride {s_stride} < 16; skipping bind matrices"
                        )
                    else:
                        step = s_stride if (s_stride is not None and s_stride >= 16) else 16
                        for i in range(len(floats) // step):
                            inv_bind_mats.append(dae_flat_to_matrix(floats[i * step:i * step + 16]))

        vcounts: list[int] = []
        v_pairs: list[tuple[int, float]] = []
        vw_el = _find_child(skin, "vertex_weights")
        if vw_el is not None:
            j_off = -1
            w_off = -1
            w_src = ""
            max_off = -1
            for inp in _find_children(vw_el, "input"):
                sem = inp.attrib.get("semantic", "")
                raw_off = inp.attrib.get("offset", "0")
                try:
                    off = int(raw_off)
                except ValueError:
                    self.warnings.append(f"Skin '{cid}': invalid vertex_weights offset '{raw_off}'")
                    continue
                if off < 0:
                    self.warnings.append(f"Skin '{cid}': negative vertex_weights offset {off}")
                    continue
                if off > max_off:
                    max_off = off
                if sem == "JOINT":
                    j_off = off
                elif sem == "WEIGHT":
                    w_off = off
                    w_src = _uri_to_local_id(inp.attrib.get("source"))
            stride = max_off + 1 if max_off >= 0 else 0
            weights, w_stride = float_sources.get(w_src, ([], None))
            if w_stride is not None and w_stride <= 0:
                self.warnings.append(
                    f"Skin '{cid}': WEIGHT source '{w_src}' has invalid stride {w_stride}; skipping weights"
                )
                weights = []
            elif w_stride is not None and w_stride > 1 and weights:
                weights = weights[::w_stride]
            vc_el = _find_child(vw_el, "vcount")
            v_el = _find_child(vw_el, "v")
            raw_vcounts = _parse_ints(vc_el.text) if vc_el is not None else []
            v_ints = _parse_ints(v_el.text) if v_el is not None else []
            if stride > 0 and j_off >= 0 and w_off >= 0:
                max_pairs = len(v_ints) // stride
                neg_vc = 0
                truncated = False
                used_pairs = 0
                for vc in raw_vcounts:
                    if vc < 0:
                        neg_vc += 1
                        vcounts.append(0)
                        continue
                    rem = max_pairs - used_pairs
                    if rem <= 0:
                        if vc > 0:
                            truncated = True
                        break
                    if vc > rem:
                        vcounts.append(rem)
                        used_pairs += rem
                        truncated = True
                        break
                    vcounts.append(vc)
                    used_pairs += vc
                if neg_vc > 0:
                    self.warnings.append(f"Skin '{cid}': clamped {neg_vc} negative <vcount> entries to 0")
                if truncated:
                    self.warnings.append(
                        f"Skin '{cid}': <vertex_weights>/<vcount> exceeds <v> pairs ({max_pairs}); truncated"
                    )
                for i in range(used_pairs):
                    base = i * stride
                    ji = v_ints[base + j_off]
                    wi = v_ints[base + w_off]
                    w = weights[wi] if 0 <= wi < len(weights) else 0.0
                    v_pairs.append((ji, w))

        self.skins_by_id[cid] = _SkinData(
            controller_id=cid,
            geometry_id=geom_id,
            bind_shape_matrix=bsm,
            joint_names=joint_names,
            inv_bind_matrices=inv_bind_mats,
            vcounts=vcounts,
            v_pairs=v_pairs,
        )

    def _read_morph_controller(self, cid: str, morph: ET.Element) -> None:
        geom_id = _uri_to_local_id(morph.attrib.get("source"))
        idref_sources: dict[str, list[str]] = {}
        float_sources: dict[str, list[float]] = {}
        for src in _find_children(morph, "source"):
            sid = src.attrib.get("id", "")
            ia = _find_child(src, "IDREF_array")
            if ia is None:
                ia = _find_child(src, "Name_array")
            if ia is not None:
                idref_sources[sid] = _parse_strings(ia.text)
            fa = _find_child(src, "float_array")
            if fa is not None:
                float_sources[sid] = _parse_floats(fa.text)

        targets_el = _find_child(morph, "targets")
        target_ids: list[str] = []
        weights: list[float] = []
        if targets_el is not None:
            for inp in _find_children(targets_el, "input"):
                sem = inp.attrib.get("semantic", "")
                sid = _uri_to_local_id(inp.attrib.get("source"))
                if sem == "MORPH_TARGET":
                    target_ids = idref_sources.get(sid, [])
                elif sem == "MORPH_WEIGHT":
                    weights = float_sources.get(sid, [])

        self.morphs_by_id[cid] = _MorphData(
            controller_id=cid,
            geometry_id=geom_id,
            target_geom_ids=target_ids,
            weights=weights,
        )

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

    def _apply_armature_collections(self, node: ET.Element, arm_data) -> None:
        extras = _parse_extra_blender(node)
        if not extras:
            return
        all_colls = [c for c in extras.get("collections", "").split("\n") if c]
        vis_colls = set(c for c in extras.get("visible_collections", "").split("\n") if c)
        for name in all_colls:
            bc = arm_data.collections.new(name)
            bc.is_visible = name in vis_colls
        active_name = extras.get("active_collection", "")
        if active_name and active_name in arm_data.collections:
            arm_data.collections.active = arm_data.collections[active_name]

    # ------------------------------------------------------------------
    # Armatures, Skinning & Shape Keys
    # ------------------------------------------------------------------

    def _make_armatures(self) -> None:
        # Index all joint nodes under root_joint_nodes
        def index_joints(jnode: ET.Element, ob_arm: Object, parent_j: ET.Element | None) -> None:
            jid = jnode.attrib.get("id", "")
            jsid = jnode.attrib.get("sid", "")
            jname = jnode.attrib.get("name") or jid or jsid
            if jid:
                self.joint_elem_by_id[jid] = jnode
                self.joint_arm_by_id[jid] = ob_arm
                self.joint_bone_name_by_id[jid] = jname
                self.joint_parent_elem_by_id[jid] = parent_j
            if jsid:
                self.joint_elem_by_sid[jsid] = jnode
            for ch in _find_children(jnode, "node"):
                if ch.attrib.get("type", "NODE").upper() == "JOINT":
                    index_joints(ch, ob_arm, jnode)

        for rj, ob_arm in self.root_joint_nodes:
            index_joints(rj, ob_arm, None)

        # Build lookup from joint_elem -> inv_bind_mat across all skins
        joint_inv_bind: dict[int, Matrix] = {}
        for skin in self.skins_by_id.values():
            for jname, ibm in zip(skin.joint_names, skin.inv_bind_matrices):
                je = self.joint_elem_by_sid.get(jname) or self.joint_elem_by_id.get(jname)
                if je is not None:
                    joint_inv_bind[id(je)] = ibm
                    if skin.ob_arm is None:
                        jid = je.attrib.get("id", "")
                        skin.ob_arm = self.joint_arm_by_id.get(jid)

        # Group root joints by armature object
        arms_to_roots: dict[int, tuple[Object, list[ET.Element]]] = {}
        for rj, ob_arm in self.root_joint_nodes:
            entry = arms_to_roots.get(ob_arm.as_pointer())
            if entry is None:
                arms_to_roots[ob_arm.as_pointer()] = (ob_arm, [rj])
            else:
                entry[1].append(rj)

        for ob_arm, rjoints in arms_to_roots.values():
            self._build_armature_edit_bones(ob_arm, rjoints, joint_inv_bind)

        # Create non-JOINT child objects parented to armature bones (#34)
        n_mesh_before = len(self.imported_mesh_objects)

        def attach_bone_children(jnode: ET.Element, ob_arm: Object) -> None:
            jid = jnode.attrib.get("id", "")
            bname = (
                self.joint_bone_name_by_id.get(jid)
                or jnode.attrib.get("name")
                or jid
                or jnode.attrib.get("sid", "")
            )
            for ch in _find_children(jnode, "node"):
                if ch.attrib.get("type", "NODE").upper() == "JOINT":
                    attach_bone_children(ch, ob_arm)
                else:
                    self._process_node(ch, jnode, ob_arm, parent_bone=bname)

        for ob_arm, rjoints in arms_to_roots.values():
            for rj in rjoints:
                attach_bone_children(rj, ob_arm)

        if len(self.imported_mesh_objects) > n_mesh_before:
            self._optimize_material_assignments()

        # Link skins to meshes
        for skin in self.skins_by_id.values():
            ob_mesh = self.object_by_geom_id.get(skin.geometry_id)
            ob_arm = skin.ob_arm
            if ob_mesh is None or ob_arm is None:
                continue
            mod = ob_mesh.modifiers.new(name="Armature", type="ARMATURE")
            mod.object = ob_arm
            mod.use_vertex_groups = True
            if ob_mesh.parent != ob_arm:
                ob_mesh.parent = ob_arm
                if ob_mesh in self.root_objects:
                    self.root_objects.remove(ob_mesh)
            ob_mesh.matrix_basis = skin.bind_shape_matrix

            # Create vertex groups
            vgroups = []
            for jname in skin.joint_names:
                je = self.joint_elem_by_sid.get(jname) or self.joint_elem_by_id.get(jname)
                bname = (je.attrib.get("name") or je.attrib.get("id") or jname) if je is not None else jname
                vgroups.append(ob_mesh.vertex_groups.new(name=bname))

            n_mesh_verts = len(ob_mesh.data.vertices)
            cursor = 0
            for vi, vc in enumerate(skin.vcounts):
                if vi >= n_mesh_verts or cursor >= len(skin.v_pairs):
                    break
                for _ in range(vc):
                    if cursor < len(skin.v_pairs):
                        ji, w = skin.v_pairs[cursor]
                        if 0 <= ji < len(vgroups) and math.isfinite(w) and w > 0.0:
                            vgroups[ji].add([vi], w, "REPLACE")
                    cursor += 1

    def _build_armature_edit_bones(
        self, ob_arm: Object, root_joints: list[ET.Element], joint_inv_bind: dict[int, Matrix]
    ) -> None:
        self.view_layer.objects.active = ob_arm
        bpy.ops.object.mode_set(mode="EDIT")
        arm = ob_arm.data
        try:
            arm_world_inv = ob_arm.matrix_world.inverted()
        except ValueError:
            arm_world_inv = Matrix.Identity(4)

        leaf_bone_length = float("inf")
        ebone_extras: dict[str, dict[str, str]] = {}
        ebone_has_tail: set[str] = set()
        ebone_chain_len: dict[str, int] = {}

        def create_bone_rec(jnode: ET.Element, parent_eb, parent_mat: Matrix | None, sibcount: int) -> int:
            nonlocal leaf_bone_length
            bname = jnode.attrib.get("name") or jnode.attrib.get("id") or "Bone"
            eb = arm.edit_bones.new(bname)
            extras = _parse_extra_blender(jnode)
            ebone_extras[eb.name] = extras

            ibm = joint_inv_bind.get(id(jnode))
            bone_is_skinned = False
            if ibm is not None:
                try:
                    joint_bind_mat = ibm.inverted()
                    mat = arm_world_inv @ joint_bind_mat
                    bone_is_skinned = True
                except ValueError:
                    self.warnings.append(f"Bone '{bname}': singular inverse bind matrix; falling back to node transform")
                    ibm = None
            if ibm is None:
                local_m = self._read_node_matrix(jnode)
                mat = (parent_mat @ local_m) if parent_mat is not None else local_m
                joint_bind_mat = mat

            if parent_eb is not None:
                eb.parent = parent_eb

            # Collections
            for cname in (c for c in extras.get("collections", "").split("\n") if c):
                if cname in arm.collections:
                    arm.collections[cname].assign(eb)

            # Connect (must be set BEFORE eb.head so setting eb.use_connect=True
            # does not snap eb.head to the parent's placeholder tail!)
            has_tail = "tip_x" in extras and "tip_y" in extras and "tip_z" in extras
            has_connect = "connect" in extras
            connect_type = extras.get("connect", "") in ("1", "true", "True") if has_connect else False
            if has_tail and not has_connect:
                has_connect = True
                connect_type = False
            if not has_connect and self.settings.auto_connect:
                connect_type = sibcount == 1
            eb.use_connect = connect_type

            # Head & Tail
            eb.head = mat.translation
            if has_tail:
                tip = Vector((
                    _safe_float(extras["tip_x"]),
                    _safe_float(extras["tip_y"]),
                    _safe_float(extras["tip_z"]),
                ))
                if tip.length_squared > 0.0:
                    eb.tail = eb.head + tip
                    ebone_has_tail.add(eb.name)
                else:
                    eb.tail = eb.head + Vector((0.0, 0.5, 0.0))
            else:
                eb.tail = eb.head + Vector((0.0, 0.5, 0.0))

            # Roll
            if "roll" in extras:
                eb.roll = _safe_float(extras["roll"])
            else:
                _, roll = bpy.types.Bone.AxisRollFromMatrix(mat.to_3x3())
                eb.roll = roll

            if bone_is_skinned and self.settings.keep_bind_info:
                rest_m = self._read_node_matrix(jnode)
                set_editbone_property_matrix(eb, "bind_mat", joint_bind_mat)
                set_editbone_property_matrix(eb, "rest_mat", rest_m)

            if parent_eb is not None:
                if eb.use_connect:
                    parent_eb.tail = eb.head.copy()
                    ebone_has_tail.add(parent_eb.name)
                dist = (eb.head - parent_eb.head).length
                if dist < leaf_bone_length and dist > MINIMUM_BONE_LENGTH:
                    leaf_bone_length = dist

            child_joints = [
                ch for ch in _find_children(jnode, "node")
                if ch.attrib.get("type", "NODE").upper() == "JOINT"
            ]
            max_cl = 0
            for ch in child_joints:
                cl = create_bone_rec(ch, eb, mat, len(child_joints))
                if cl > max_cl:
                    max_cl = cl
            chain_len = max_cl + 1
            ebone_chain_len[eb.name] = chain_len
            return chain_len

        for rj in root_joints:
            create_bone_rec(rj, None, None, len(root_joints))

        # Connect bone chains if find_chains is requested (ArmatureImporter.cpp:280-358)
        if self.settings.find_chains:
            UNLIMITED_CHAIN_MAX = 10**9

            def connect_bone_chains(parent_eb, max_chain_len: int) -> None:
                children = list(parent_eb.children)
                dominant_child = None
                maxlen = 0
                if children:
                    for ch in children:
                        cl = ebone_chain_len.get(ch.name, 1)
                        if cl <= max_chain_len:
                            if cl > maxlen:
                                dominant_child = ch
                                maxlen = cl
                            elif cl == maxlen:
                                dominant_child = None
                if dominant_child is not None:
                    if not dominant_child.use_connect:
                        vec = dominant_child.head - parent_eb.head
                        if vec.length_squared > MINIMUM_BONE_LENGTH:
                            parent_eb.tail = dominant_child.head.copy()
                            ebone_has_tail.add(parent_eb.name)
                            if ebone_chain_len.get(parent_eb.name, 1) >= self.settings.min_chain_length:
                                dominant_child.use_connect = True
                    for ch in children:
                        connect_bone_chains(ch, UNLIMITED_CHAIN_MAX)
                elif maxlen > 1 and maxlen > self.settings.min_chain_length:
                    connect_bone_chains(parent_eb, maxlen - 1)
                else:
                    for ch in children:
                        connect_bone_chains(ch, UNLIMITED_CHAIN_MAX)

            for eb in list(arm.edit_bones):
                if eb.parent is None:
                    connect_bone_chains(eb, UNLIMITED_CHAIN_MAX)

        # Fix leaf bones
        default_leaf_len = 1.0 if leaf_bone_length == float("inf") else leaf_bone_length
        for eb in arm.edit_bones:
            is_leaf = not any(c.use_connect for c in eb.children)
            if is_leaf and eb.name not in ebone_has_tail:
                if self.settings.fix_orientation and eb.parent is not None:
                    vec = eb.head - eb.parent.head
                    if vec.length_squared < MINIMUM_BONE_LENGTH:
                        vec = eb.parent.tail - eb.parent.head
                else:
                    vec = eb.tail - eb.head
                if vec.length > 0.0:
                    vec.normalize()
                    eb.tail = eb.head + vec * default_leaf_len

        bpy.ops.object.mode_set(mode="OBJECT")

        # Set pose bone rotation mode (EUL if decomposed else QUAT)
        def set_rotmode_rec(jnode: ET.Element) -> None:
            bname = jnode.attrib.get("name") or jnode.attrib.get("id") or "Bone"
            pchan = ob_arm.pose.bones.get(bname)
            if pchan is not None:
                pchan.rotation_mode = "XYZ" if self._node_is_decomposed(jnode) else "QUATERNION"
            for ch in _find_children(jnode, "node"):
                if ch.attrib.get("type", "NODE").upper() == "JOINT":
                    set_rotmode_rec(ch)

        for rj in root_joints:
            set_rotmode_rec(rj)

    def _make_shape_keys(self) -> None:
        for morph in self.morphs_by_id.values():
            ob = self.object_by_geom_id.get(morph.geometry_id)
            if ob is None or ob.type != "MESH":
                continue
            if ob.data.shape_keys is None:
                ob.shape_key_add(name="Basis", from_mix=False)
            for idx, tid in enumerate(morph.target_geom_ids):
                tmesh = self.meshes_by_id.get(tid)
                if tmesh is None or len(tmesh.vertices) != len(ob.data.vertices):
                    continue
                sk = ob.shape_key_add(name=tmesh.name, from_mix=False)
                for vi, v in enumerate(tmesh.vertices):
                    sk.data[vi].co = v.co
                if idx < len(morph.weights):
                    sk.value = morph.weights[idx]

    # ------------------------------------------------------------------
    # <library_animations>
    # ------------------------------------------------------------------

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
