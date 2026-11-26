# SPDX-FileCopyrightText: 2009-2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Complete Collada (.dae) importer faithfully ported from Blender 4.5.1 for Blender 5.0+."""

from __future__ import annotations

import math
import os
import traceback
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from typing import TYPE_CHECKING

import bpy
import numpy as np

from .collada_utils import (
    check_target_collection_visibility,
)

from .importer.archive import ArchiveMixin
from .importer.materials import MaterialsMixin
from .importer.mesh import MeshMixin
from .importer.rig import RigMixin, _MorphData, _SkinData
from .importer.animation import AnimationMixin, _AnimChannelData
from .importer.scene import SceneMixin
if TYPE_CHECKING:
    from bpy.types import Context, Material, Mesh, Object, Scene


from .importer.xml_parse import (
    _find_child,
    _find_children,
    _norm_archive_path,
    _parse_xml_safe,
    _strip_ns,
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



class DocumentImporter(ArchiveMixin, MaterialsMixin, MeshMixin, RigMixin, AnimationMixin, SceneMixin):
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
        self._beamng_mat_ctx = None
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
            self._beamng_mat_maps = self._beamng_mat_ctx.mat_maps if self._beamng_mat_ctx is not None else {}
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
                    if self._beamng_mat_ctx is None:
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
        if self._beamng_mat_ctx is not None:
            self._beamng_mat_maps = self._beamng_mat_ctx.mat_maps
        else:
            self._beamng_mat_maps = {}
            self._load_disk_materials_json(self.dae_dir)
        try:
            tree = _parse_xml_safe(dae_path)
        except Exception as e:
            self.errors.append(f"Failed to parse XML: {e}")
            return False
        return self._safe_import_dae_tree(tree)

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

    # ------------------------------------------------------------------
    # <library_images>
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # <library_effects> & <library_materials>
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # <library_cameras>
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # <library_lights>
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # <library_geometries>
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # <library_controllers>
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # <library_visual_scenes> Node Processing
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Armatures, Skinning & Shape Keys
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # <library_animations>
    # ------------------------------------------------------------------

