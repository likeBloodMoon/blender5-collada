# SPDX-FileCopyrightText: 2009-2026 Blender Authors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Complete Collada (.dae) exporter faithfully ported from Blender 4.5.1."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import bpy
from mathutils import Matrix

from .xml_writer import (
    ColladaStreamWriter,
)
from .collada_utils import (
    GlobalIdMap,
    build_export_set,
    build_global_transform,
    flush_edit_mode_objects,
)

from .exporter.geometry import GeometryMixin
from .exporter.materials import MaterialsMixin
from .exporter.rig import RigMixin
from .exporter.animation import AnimationMixin
from .exporter.scene import SceneMixin
if TYPE_CHECKING:
    from bpy.types import Context, Object, Scene


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
class DocumentExporter(GeometryMixin, MaterialsMixin, RigMixin, AnimationMixin, SceneMixin):
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
        self._extra_ob_materials: dict[int, list[bpy.types.Material]] = {}

    def _collect_realized_gn_materials(self) -> None:
        """Collect materials used by Geometry Nodes instances so .dae effects/materials/bindings include them (#64)."""
        self._extra_ob_materials.clear()
        if not self.settings.apply_modifiers:
            return
        use_render = self.settings.export_mesh_type == "render"
        gn_eval_to_ob: dict[int, Object] = {}
        for ob in self.export_set:
            if ob.type != "MESH":
                continue
            if any(
                mod.type == "NODES" and (mod.show_render if use_render else mod.show_viewport)
                for mod in ob.modifiers
            ):
                gn_eval_to_ob[ob.evaluated_get(self.depsgraph).as_pointer()] = ob
        if not gn_eval_to_ob:
            return

        seen_by_ob: dict[int, set[int]] = {}
        for ob in gn_eval_to_ob.values():
            seen_by_ob[ob.as_pointer()] = {
                slot.material.as_pointer() for slot in ob.material_slots if slot.material is not None
            }

        for inst in self.depsgraph.object_instances:
            if not inst.is_instance or inst.parent is None:
                continue
            host_ob = gn_eval_to_ob.get(inst.parent.as_pointer())
            if host_ob is None:
                continue
            inst_ob = inst.object
            if inst_ob is None or inst_ob.type != "MESH":
                continue
            host_ptr = host_ob.as_pointer()
            seen = seen_by_ob[host_ptr]
            cand_mats: list[bpy.types.Material] = []
            for slot in getattr(inst_ob, "material_slots", ()):
                if slot.material is not None:
                    cand_mats.append(slot.material)
            if inst_ob.data is not None:
                for ma in getattr(inst_ob.data, "materials", ()):
                    if ma is not None:
                        cand_mats.append(ma)
            for ma in cand_mats:
                mp = ma.as_pointer()
                if mp not in seen:
                    seen.add(mp)
                    self._extra_ob_materials.setdefault(host_ptr, []).append(ma)

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
        self._collect_realized_gn_materials()

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

    # ------------------------------------------------------------------
    # <library_cameras>
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # <library_lights>
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # <library_visual_scenes>
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # <scene>
    # ------------------------------------------------------------------

