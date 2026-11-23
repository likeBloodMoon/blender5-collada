# SPDX-FileCopyrightText: 2008-2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Collada (.dae) Import/Export Extension for Blender 5.0+.

Faithfully ported from Blender 4.5.1's native Collada (io_collada.cc + source/blender/io/collada/)
with identical operator IDs (wm.collada_export / wm.collada_import), properties, defaults, and UI
for drop-in compatibility.
"""

from __future__ import annotations

import math
import os
import bpy
from bpy.props import (
    BoolProperty,
    EnumProperty,
    FloatProperty,
    IntProperty,
    StringProperty,
)
from bpy_extras.io_utils import (
    ExportHelper,
    ImportHelper,
    poll_file_object_drop,
)

from .export_collada import DocumentExporter, ExportSettings
from .import_collada import DocumentImporter, ImportSettings


_AXIS_ITEMS = (
    ("X", "X", "Positive X Axis"),
    ("Y", "Y", "Positive Y Axis"),
    ("Z", "Z", "Positive Z Axis"),
    ("-X", "-X", "Negative X Axis"),
    ("-Y", "-Y", "Negative Y Axis"),
    ("-Z", "-Z", "Negative Z Axis"),
)


class WM_OT_collada_export(bpy.types.Operator, ExportHelper):
    """Save a Collada file"""

    bl_idname = "wm.collada_export"
    bl_label = "Export COLLADA"
    bl_options = {"PRESET"}

    filename_ext = ".dae"

    filter_glob: StringProperty(
        default="*.dae",
        options={"HIDDEN"},
    )

    collection: StringProperty(
        name="Collection",
        description="Export only objects in this collection (used by Blender Collection Exporters)",
        default="",
        options={"HIDDEN", "SKIP_SAVE"},
    )

    prop_bc_export_ui_section: EnumProperty(
        name="Export Section",
        description="Only for User Interface organization",
        items=(
            ("main", "Main", "Data export section"),
            ("geometry", "Geom", "Geometry export section"),
            ("armature", "Arm", "Armature export section"),
            ("animation", "Anim", "Animation export section"),
            ("collada", "Extra", "Collada export section"),
        ),
        default="main",
    )

    apply_modifiers: BoolProperty(
        name="Apply Modifiers",
        description="Apply modifiers to exported mesh (non destructive)",
        default=False,
    )

    export_mesh_type: IntProperty(
        name="Resolution",
        description="Modifier resolution for export",
        default=0,
    )

    export_mesh_type_selection: EnumProperty(
        name="Resolution",
        description="Modifier resolution for export",
        items=(
            ("view", "Viewport", "Apply modifier's viewport settings"),
            ("render", "Render", "Apply modifier's render settings"),
        ),
        default="view",
    )

    export_global_forward_selection: EnumProperty(
        name="Global Forward Axis",
        description="Global Forward axis for export",
        items=_AXIS_ITEMS,
        default="Y",
    )

    export_global_up_selection: EnumProperty(
        name="Global Up Axis",
        description="Global Up axis for export",
        items=_AXIS_ITEMS,
        default="Z",
    )

    apply_global_orientation: BoolProperty(
        name="Apply Global Orientation",
        description="Rotate all root objects to match the global orientation settings otherwise set the global orientation per Collada asset",
        default=False,
    )

    selected: BoolProperty(
        name="Selection Only",
        description="Export only selected elements",
        default=False,
    )

    include_children: BoolProperty(
        name="Include Children",
        description="Export all children of selected objects (even if not selected)",
        default=False,
    )

    include_armatures: BoolProperty(
        name="Include Armatures",
        description="Export related armatures (even if not selected)",
        default=False,
    )

    include_shapekeys: BoolProperty(
        name="Include Shape Keys",
        description="Export all Shape Keys from Mesh Objects",
        default=False,
    )

    deform_bones_only: BoolProperty(
        name="Deform Bones Only",
        description="Only export deforming bones with armatures",
        default=False,
    )

    include_animations: BoolProperty(
        name="Include Animations",
        description="Export animations if available",
        default=True,
    )

    include_all_actions: BoolProperty(
        name="Include all Actions",
        description="Export also unassigned actions",
        default=True,
    )

    export_animation_type_selection: EnumProperty(
        name="Key Type",
        description="Type for exported animations (use sample keys or Curve keys)",
        items=(
            ("sample", "Samples", "Export Sampled points guided by sampling rate"),
            ("keys", "Curves", "Export Curves (note: guided by curve keys)"),
        ),
        default="sample",
    )

    sampling_rate: IntProperty(
        name="Sampling Rate",
        description="The distance between 2 keyframes (1 to key every frame)",
        default=1,
        min=1,
    )

    keep_smooth_curves: BoolProperty(
        name="Keep Smooth curves",
        description="Export also the curve handles (if available)",
        default=False,
    )

    keep_keyframes: BoolProperty(
        name="Keep Keyframes",
        description="Use existing keyframes as additional sample points",
        default=False,
    )

    keep_flat_curves: BoolProperty(
        name="All Keyed Curves",
        description="Export also curves which have only one key or are totally flat",
        default=False,
    )

    active_uv_only: BoolProperty(
        name="Only Selected UV Map",
        description="Export only the selected UV Map",
        default=False,
    )

    use_texture_copies: BoolProperty(
        name="Copy",
        description="Copy textures to same folder where the .dae file is exported",
        default=True,
    )

    triangulate: BoolProperty(
        name="Triangulate",
        description="Export polygons (quads and n-gons) as triangles",
        default=True,
    )

    use_object_instantiation: BoolProperty(
        name="Use Object Instances",
        description="Instantiate multiple Objects from same Data",
        default=True,
    )

    use_blender_profile: BoolProperty(
        name="Use Blender Profile",
        description="Export additional Blender specific information (for material, shaders, bones, etc.)",
        default=True,
    )

    sort_by_name: BoolProperty(
        name="Sort by Object name",
        description="Sort exported data by Object name",
        default=False,
    )

    export_object_transformation_type: IntProperty(
        name="Transform",
        description="Object Transformation type for translation, scale and rotation",
        default=0,
    )

    export_object_transformation_type_selection: EnumProperty(
        name="Transform",
        description="Object Transformation type for translation, scale and rotation",
        items=(
            ("matrix", "Matrix", "Use <matrix> representation for exported transformations"),
            ("decomposed", "Decomposed", "Use <rotate>, <translate> and <scale> representation for exported transformations"),
        ),
        default="matrix",
    )

    export_animation_transformation_type: IntProperty(
        name="Transform",
        description="Transformation type for translation, scale and rotation",
        default=0,
    )

    export_animation_transformation_type_selection: EnumProperty(
        name="Transform",
        description="Transformation type for translation, scale and rotation",
        items=(
            ("matrix", "Matrix", "Use <matrix> representation for exported transformations"),
            ("decomposed", "Decomposed", "Use <rotate>, <translate> and <scale> representation for exported transformations"),
        ),
        default="matrix",
    )

    open_sim: BoolProperty(
        name="Export to SL/OpenSim",
        description="Compatibility mode for Second Life, OpenSimulator and other compatible online worlds",
        default=False,
    )

    limit_precision: BoolProperty(
        name="Limit Precision",
        description="Reduce the precision of the exported data to 6 digits",
        default=False,
    )

    keep_bind_info: BoolProperty(
        name="Keep Bind Info",
        description="Store Bindpose information in custom bone properties for later use during Collada export",
        default=False,
    )

    def draw(self, context):
        layout = self.layout
        row = layout.row(align=False)
        row.prop(self, "prop_bc_export_ui_section", expand=True)

        layout.use_property_split = True
        layout.use_property_decorate = False

        sec = self.prop_bc_export_ui_section
        if sec == "main":
            box = layout.box()
            col = box.column(align=False)
            col.prop(self, "selected")
            sub = col.column(align=False)
            sub.enabled = self.selected
            sub.prop(self, "include_children")
            sub.prop(self, "include_armatures")
            sub.prop(self, "include_shapekeys")

            box = layout.box()
            box.label(text="Global Orientation", icon="ORIENTATION_GLOBAL")
            box.prop(self, "apply_global_orientation", text="Apply")
            box.prop(self, "export_global_forward_selection", text="Forward Axis")
            box.prop(self, "export_global_up_selection", text="Up Axis")

            box = layout.box()
            box.label(text="Texture Options", icon="TEXTURE_DATA")
            col = box.column(align=False)
            col.prop(self, "use_texture_copies")
            col.prop(self, "active_uv_only", text="Only Selected Map")

        elif sec == "geometry":
            box = layout.box()
            box.label(text="Export Data Options", icon="MESH_DATA")
            col = box.column(align=False)
            col.prop(self, "triangulate")
            row = col.row(align=True)
            row.prop(self, "apply_modifiers")
            sub = row.column(align=False)
            sub.active = self.apply_modifiers
            sub.prop(self, "export_mesh_type_selection", text="")
            if self.include_animations:
                col.prop(self, "export_animation_transformation_type_selection")
            else:
                col.prop(self, "export_object_transformation_type_selection")

        elif sec == "armature":
            box = layout.box()
            box.label(text="Armature Options", icon="ARMATURE_DATA")
            col = box.column(align=False)
            col.prop(self, "deform_bones_only")
            col.prop(self, "open_sim")

        elif sec == "animation":
            box = layout.box()
            box.prop(self, "include_animations")
            col = box.column(align=False)
            col.active = self.include_animations
            row = col.row(align=False)
            row.prop(self, "export_animation_type_selection", expand=True)
            if self.include_animations:
                box.prop(self, "export_animation_transformation_type_selection")
            else:
                box.prop(self, "export_object_transformation_type_selection")
            col.prop(self, "keep_smooth_curves")
            sub = col.column(align=False)
            sub.active = self.include_animations and self.export_animation_type_selection == "sample"
            sub.prop(self, "sampling_rate")
            sub.prop(self, "keep_keyframes")
            col.prop(self, "keep_flat_curves")
            col.prop(self, "include_all_actions")

        elif sec == "collada":
            box = layout.box()
            box.label(text="Collada Options", icon="MODIFIER")
            col = box.column(align=False)
            col.prop(self, "use_object_instantiation")
            col.prop(self, "use_blender_profile")
            col.prop(self, "sort_by_name")
            col.prop(self, "keep_bind_info")
            col.prop(self, "limit_precision")

    def execute(self, context):
        if not self.filepath:
            self.report({"ERROR"}, "No filepath given")
            return {"CANCELLED"}

        filepath = bpy.path.ensure_ext(self.filepath, ".dae")

        settings = ExportSettings(
            filepath=filepath,
            apply_modifiers=self.apply_modifiers,
            export_mesh_type=self.export_mesh_type_selection,
            global_forward=self.export_global_forward_selection,
            global_up=self.export_global_up_selection,
            apply_global_orientation=self.apply_global_orientation,
            selected=self.selected,
            include_children=self.include_children,
            include_armatures=self.include_armatures,
            include_shapekeys=self.include_shapekeys,
            deform_bones_only=self.deform_bones_only,
            include_animations=self.include_animations,
            include_all_actions=self.include_all_actions,
            export_animation_type=self.export_animation_type_selection,
            sampling_rate=self.sampling_rate,
            keep_smooth_curves=self.keep_smooth_curves,
            keep_keyframes=self.keep_keyframes,
            keep_flat_curves=self.keep_flat_curves,
            active_uv_only=self.active_uv_only,
            use_texture_copies=self.use_texture_copies,
            triangulate=self.triangulate,
            use_object_instantiation=self.use_object_instantiation,
            use_blender_profile=self.use_blender_profile,
            sort_by_name=self.sort_by_name,
            object_transformation_type=self.export_object_transformation_type_selection,
            animation_transformation_type=self.export_animation_transformation_type_selection,
            open_sim=self.open_sim,
            limit_precision=self.limit_precision,
            keep_bind_info=self.keep_bind_info,
            collection=self.collection,
        )

        exporter = DocumentExporter(context, settings)
        count = exporter.export_current_scene()
        if count == 0:
            self.report({"WARNING"}, "No objects selected -- Created empty export file")
            return {"CANCELLED"}

        self.report({"INFO"}, f"Exported {count} Objects")
        return {"FINISHED"}


class WM_OT_collada_import(bpy.types.Operator, ImportHelper):
    """Load a Collada file"""

    bl_idname = "wm.collada_import"
    bl_label = "Import COLLADA"
    bl_options = {"UNDO", "PRESET"}

    filename_ext = ".dae"

    filter_glob: StringProperty(
        default="*.dae;*.zae;*.kmz;*.zip",
        options={"HIDDEN"},
    )

    import_units: BoolProperty(
        name="Import Units",
        description="If disabled match import to Blender's current Unit settings, otherwise use the settings from the Imported scene",
        default=False,
    )

    custom_normals: BoolProperty(
        name="Custom Normals",
        description="Import custom normals, if available (otherwise Blender will compute them)",
        default=True,
    )

    fast_exact_normals: BoolProperty(
        name="Fast Exact Normals",
        description=(
            "Write COLLADA normals directly to Blender's custom_normal FLOAT_VECTOR attribute "
            "(much faster on large meshes and avoids short2 compression; normals stay locked "
            "across Edit Mode edits until cleared via Clear Custom Split Normals Data)"
        ),
        default=False,
    )

    sharp_edges_from_normals: BoolProperty(
        name="Mark Sharp from Normals",
        description=(
            "Convert COLLADA normals into clean Sharp Edge markings with smooth faces "
            "(prevents flat-face triangles from hiding sharp edges and eliminates false sharp edges)"
        ),
        default=False,
    )

    sharp_angle_threshold: FloatProperty(
        name="Sharp Threshold",
        description="Minimum normal angle discontinuity across an edge to mark it sharp",
        default=math.radians(1.0),
        min=0.0,
        max=math.pi,
        subtype="ANGLE",
    )

    fix_orientation: BoolProperty(
        name="Fix Leaf Bones",
        description="Fix Orientation of Leaf Bones (Collada does only support Joints)",
        default=False,
    )

    find_chains: BoolProperty(
        name="Find Bone Chains",
        description="Find best matching Bone Chains and ensure bones in chain are connected",
        default=False,
    )

    auto_connect: BoolProperty(
        name="Auto Connect",
        description="Set use_connect for parent bones which have exactly one child bone",
        default=False,
    )

    min_chain_length: IntProperty(
        name="Minimum Chain Length",
        description="When searching Bone Chains disregard chains of length below this value",
        default=0,
        min=0,
    )

    keep_bind_info: BoolProperty(
        name="Keep Bind Info",
        description="Store Bindpose information in custom bone properties for later use during Collada export",
        default=False,
    )

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False

        box = layout.box()
        box.label(text="Import Data Options", icon="MESH_DATA")
        box.prop(self, "import_units")
        box.prop(self, "custom_normals")
        if self.custom_normals and not self.sharp_edges_from_normals:
            box.prop(self, "fast_exact_normals")
        box.prop(self, "sharp_edges_from_normals")
        if self.sharp_edges_from_normals:
            box.prop(self, "sharp_angle_threshold")

        box = layout.box()
        box.label(text="Armature Options", icon="ARMATURE_DATA")
        col = box.column(align=False)
        col.prop(self, "fix_orientation")
        col.prop(self, "find_chains")
        col.prop(self, "auto_connect")
        col.prop(self, "min_chain_length")

        box = layout.box()
        box.prop(self, "keep_bind_info")

    def execute(self, context):
        if not self.filepath or not os.path.exists(self.filepath):
            self.report({"ERROR"}, "No filepath given or file does not exist")
            return {"CANCELLED"}

        settings = ImportSettings(
            filepath=self.filepath,
            import_units=self.import_units,
            custom_normals=self.custom_normals,
            fast_exact_normals=self.fast_exact_normals,
            sharp_edges_from_normals=self.sharp_edges_from_normals,
            sharp_angle_threshold=self.sharp_angle_threshold,
            find_chains=self.find_chains,
            auto_connect=self.auto_connect,
            fix_orientation=self.fix_orientation,
            min_chain_length=self.min_chain_length,
            keep_bind_info=self.keep_bind_info,
        )

        importer = DocumentImporter(context, settings)
        ok = importer.import_document()
        for warn in importer.warnings:
            self.report({"WARNING"}, warn)
        if ok:
            return {"FINISHED"}

        if importer.errors:
            self.report({"ERROR"}, "; ".join(importer.errors))
        else:
            self.report({"ERROR"}, "Collada import failed")
        return {"CANCELLED"}


class IO_FH_collada(bpy.types.FileHandler):
    bl_idname = "IO_FH_collada"
    bl_label = "Collada"
    bl_import_operator = "wm.collada_import"
    bl_export_operator = "wm.collada_export"
    bl_file_extensions = ".dae;.zae;.kmz"

    @classmethod
    def poll_drop(cls, context):
        return poll_file_object_drop(context)


def menu_func_import(self, context):
    self.layout.operator(WM_OT_collada_import.bl_idname, text="Collada (Default) (.dae, .zae, .kmz)")


def menu_func_export(self, context):
    self.layout.operator(WM_OT_collada_export.bl_idname, text="Collada (Default) (.dae)")


classes = (
    WM_OT_collada_export,
    WM_OT_collada_import,
    IO_FH_collada,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.TOPBAR_MT_file_import.append(menu_func_import)
    bpy.types.TOPBAR_MT_file_export.append(menu_func_export)


def unregister():
    bpy.types.TOPBAR_MT_file_export.remove(menu_func_export)
    bpy.types.TOPBAR_MT_file_import.remove(menu_func_import)
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
