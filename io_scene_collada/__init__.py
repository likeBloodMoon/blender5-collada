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

from .beamng.picker import execute_beamng_import, register_picker, unregister_picker
from .beamng.source import load_vehicle_source
from .cdae.exporter import CdaeExporter, CdaeExportSettings, zstd_available
from .cdae.importer import CdaeImporter, CdaeImportSettings
from .collada_utils import report_capped
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


def _has_nearby_beamng_pc(filepath: str) -> bool:
    """Fast check for sibling .pc configs next to a loose .dae/.cdae (avoids scanning unrelated folders)."""
    base_dir = os.path.dirname(os.path.abspath(filepath))
    if not base_dir or not os.path.isdir(base_dir):
        return False
    cur = base_dir
    for _ in range(4):
        try:
            with os.scandir(cur) as it:
                for entry in it:
                    if entry.is_file(follow_symlinks=False) and entry.name.lower().endswith(".pc"):
                        return True
        except OSError:
            return False
        parent = os.path.dirname(cur)
        if os.path.basename(parent).lower() == "vehicles":
            break
        if not parent or parent == cur:
            break
        cur = parent
    return False


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

    keep_quads: BoolProperty(
        name="Keep Quads",
        description=(
            "Triangulate n-gons only and export quads as 4-sided polygons. "
            "Note: non-planar quads are split by whichever program opens the file, so shading may differ slightly"
        ),
        default=False,
    )

    keep_normals: BoolProperty(
        name="Keep Normals",
        description="Preserve pre-triangulation corner normals (custom normals, sharp edges, and non-planar quads) when triangulating",
        default=True,
    )

    deduplicate_attributes: BoolProperty(
        name="Deduplicate Attributes",
        description="Deduplicate UV and vertex color source arrays on export",
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
            sub_kq = col.row(align=False)
            sub_kq.active = self.triangulate
            sub_kq.prop(self, "keep_quads")
            sub_kn = col.row(align=False)
            sub_kn.active = self.triangulate
            sub_kn.prop(self, "keep_normals")
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
            keep_quads=self.keep_quads,
            keep_normals=self.keep_normals,
            deduplicate_attributes=self.deduplicate_attributes,
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

    archive_mode: EnumProperty(
        name="Archive Models",
        description=(
            "How to handle archives (.zip/.zae/.kmz) containing multiple .dae files "
            "(manifest.xml is always honored when present)"
        ),
        items=(
            (
                "ALL",
                "All models (collections)",
                "Import every .dae file in the archive into its own collection",
            ),
            (
                "MAIN",
                "Main model only",
                "Import only the .dae matching the archive name (or shallowest) and warn about skipped models",
            ),
        ),
        default="ALL",
    )

    beamng_config: StringProperty(
        name="BeamNG Config",
        description="BeamNG .pc path (or name) inside the archive/folder to resolve and import",
        default="",
        options={"HIDDEN", "SKIP_SAVE"},
    )

    beamng_parts_mode: EnumProperty(
        name="BeamNG Parts Mode",
        description="Which parts to import when a BeamNG .pc config is specified",
        items=(
            ("CONFIG", "Only this config", "Import only meshes used by the resolved config"),
            (
                "CONFIG_PLUS_HIDDEN",
                "Config + other parts hidden",
                "Import all meshes, putting non-config meshes into excluded Alternatives collections",
            ),
            ("ALL", "Everything", "Import all selected models without config filtering"),
        ),
        default="ALL",
        options={"HIDDEN", "SKIP_SAVE"},
    )

    archive_models: StringProperty(
        name="Archive Models Filter",
        description="Semicolon-separated list of .dae paths inside the archive to import",
        default="",
        options={"HIDDEN", "SKIP_SAVE"},
    )

    use_common: BoolProperty(
        name="Use common.zip",
        description="Use sibling common.zip for BeamNG slot resolution and shared meshes",
        default=True,
        options={"HIDDEN", "SKIP_SAVE"},
    )

    spread_out: BoolProperty(
        name="Spread Out Models",
        description="Place multiple imported models side by side on a grid",
        default=False,
        options={"HIDDEN", "SKIP_SAVE"},
    )

    interactive_picker: BoolProperty(default=False, options={"HIDDEN", "SKIP_SAVE"})

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
        box.prop(self, "archive_mode")

        box = layout.box()
        box.label(text="Armature Options", icon="ARMATURE_DATA")
        col = box.column(align=False)
        col.prop(self, "fix_orientation")
        col.prop(self, "find_chains")
        col.prop(self, "auto_connect")
        col.prop(self, "min_chain_length")

        box = layout.box()
        box.prop(self, "keep_bind_info")

    def invoke(self, context, event):
        self.interactive_picker = True
        if self.properties.is_property_set("filepath") and os.path.isfile(self.filepath):
            return self.execute(context)
        return super().invoke(context, event)

    def execute(self, context):
        if not self.filepath or not os.path.exists(self.filepath):
            self.report({"ERROR"}, "No filepath given or file does not exist")
            return {"CANCELLED"}

        ext = os.path.splitext(self.filepath)[1].lower()
        is_archive = ext in (".zip", ".zae", ".kmz")

        # Interactive UI path: open BeamNG picker when archive has >1 .dae or any .pc,
        # or when a loose .dae sits inside a BeamNG vehicle folder containing .pc configs.
        if (
            self.interactive_picker
            and not bpy.app.background
            and not self.beamng_config
            and not self.archive_models
            and (is_archive or _has_nearby_beamng_pc(self.filepath))
        ):
            try:
                src = load_vehicle_source(self.filepath, index_dae_nodes=False)
                if src.has_picker_choices(cdae_mode=False):
                    return bpy.ops.wm.collada_beamng_picker(
                        "INVOKE_DEFAULT",
                        filepath=self.filepath,
                        cdae_mode=False,
                        import_units=self.import_units,
                        custom_normals=self.custom_normals,
                        fast_exact_normals=self.fast_exact_normals,
                        sharp_edges_from_normals=self.sharp_edges_from_normals,
                        sharp_angle_threshold=self.sharp_angle_threshold,
                    )
            except Exception as exc:
                self.report(
                    {"WARNING"},
                    f"Could not open BeamNG archive picker ({exc}); falling back to direct import",
                )

        settings = ImportSettings(
            filepath=self.filepath,
            import_units=self.import_units,
            custom_normals=self.custom_normals,
            fast_exact_normals=self.fast_exact_normals,
            sharp_edges_from_normals=self.sharp_edges_from_normals,
            sharp_angle_threshold=self.sharp_angle_threshold,
            archive_mode=self.archive_mode,
            find_chains=self.find_chains,
            auto_connect=self.auto_connect,
            fix_orientation=self.fix_orientation,
            min_chain_length=self.min_chain_length,
            keep_bind_info=self.keep_bind_info,
        )

        # Scripted BeamNG config or explicit archive_models / spread_out path
        if self.beamng_config or self.archive_models or self.spread_out:
            ok, summary, warnings, errors = execute_beamng_import(
                context,
                filepath=self.filepath,
                cdae_mode=False,
                beamng_config=self.beamng_config,
                beamng_parts_mode=self.beamng_parts_mode,
                archive_models=self.archive_models,
                use_common=self.use_common,
                spread_out=self.spread_out,
                base_dae_settings=settings,
            )
            report_capped(self, "WARNING", warnings)
            if ok:
                if summary:
                    self.report({"INFO"}, summary)
                return {"FINISHED"}
            report_capped(self, "ERROR", errors, fallback="BeamNG Collada import failed")
            return {"CANCELLED"}

        importer = DocumentImporter(context, settings)
        ok = importer.import_document()
        report_capped(self, "WARNING", importer.warnings)
        if ok:
            return {"FINISHED"}

        report_capped(self, "ERROR", importer.errors, fallback="Collada import failed")
        return {"CANCELLED"}


class IMPORT_SCENE_OT_beamng_cdae(bpy.types.Operator, ImportHelper):
    """Load a BeamNG Cached Collada (.cdae v31) file"""

    bl_idname = "import_scene.beamng_cdae"
    bl_label = "BeamNG Cached Collada (.cdae)"
    bl_options = {"PRESET", "UNDO"}

    filename_ext = ".cdae"
    filter_glob: StringProperty(
        default="*.cdae;*.zip",
        options={"HIDDEN"},
    )

    lod_mode: EnumProperty(
        name="Detail Level (LOD)",
        description="Which detail levels to import from the .cdae shape",
        items=(
            ("HIGHEST", "Highest detail only", "Import only the highest-detail visible LOD"),
            ("ALL", "All detail levels", "Import every detail level into its own collection"),
        ),
        default="HIGHEST",
    )
    import_collision: BoolProperty(
        name="Import Collision Meshes",
        description="Also import negative-size / collision detail levels",
        default=False,
    )
    normals_mode: EnumProperty(
        name="Normals",
        description=(
            "How to handle split vertices and vertex normals on import. "
            "Exact normals match the game but do not auto-update if you reshape the mesh; "
            "Sharp edges recompute shading from geometry and can differ from the game"
        ),
        items=(
            (
                "EXACT",
                "Exact (keep game normals)",
                "Merge split vertices and keep exact game custom normals, while also marking "
                "sharp edges so creases survive if custom normals are cleared later. "
                "Note: exact custom normals will not update automatically if you reshape the mesh",
            ),
            (
                "SHARP",
                "Sharp edges (easier to edit)",
                "Merge split vertices and convert normal discontinuities into sharp edges without "
                "locked custom normals. Easier to edit, but shading is recomputed from geometry "
                "and can differ from the game",
            ),
            (
                "UNMERGED",
                "Unmerged (raw GPU vertices)",
                "Keep raw unmerged GPU vertices and exact game custom normals (for debugging or "
                "byte-faithful round trips; exact normals will not update if you reshape the mesh)",
            ),
        ),
        default="EXACT",
    )
    merge_distance: FloatProperty(
        name="Merge Distance",
        description="Maximum distance between vertices to weld when merging",
        default=1e-5,
        min=0.0,
        max=0.1,
        precision=6,
    )
    sharp_angle_threshold: FloatProperty(
        name="Sharp Angle",
        description="Minimum normal angle across an edge to mark it sharp",
        default=math.radians(1.0),
        min=0.0,
        max=math.pi,
        subtype="ANGLE",
    )
    beamng_config: StringProperty(
        name="BeamNG Config",
        description="BeamNG .pc path (or name) inside the archive/folder to resolve and import",
        default="",
        options={"HIDDEN", "SKIP_SAVE"},
    )
    beamng_parts_mode: EnumProperty(
        name="BeamNG Parts Mode",
        description="Which parts to import when a BeamNG .pc config is specified",
        items=(
            ("CONFIG", "Only this config", "Import only meshes used by the resolved config"),
            (
                "CONFIG_PLUS_HIDDEN",
                "Config + other parts hidden",
                "Import all meshes, putting non-config meshes into excluded Alternatives collections",
            ),
            ("ALL", "Everything", "Import all selected models without config filtering"),
        ),
        default="ALL",
        options={"HIDDEN", "SKIP_SAVE"},
    )
    archive_models: StringProperty(
        name="Archive Models Filter",
        description="Semicolon-separated list of .cdae paths inside the archive to import",
        default="",
        options={"HIDDEN", "SKIP_SAVE"},
    )
    use_common: BoolProperty(
        name="Use common.zip",
        description="Use sibling common.zip for BeamNG slot resolution and shared meshes",
        default=True,
        options={"HIDDEN", "SKIP_SAVE"},
    )
    spread_out: BoolProperty(
        name="Spread Out Models",
        description="Place multiple imported models side by side on a grid",
        default=False,
        options={"HIDDEN", "SKIP_SAVE"},
    )
    interactive_picker: BoolProperty(default=False, options={"HIDDEN", "SKIP_SAVE"})

    def invoke(self, context, event):
        self.interactive_picker = True
        if self.properties.is_property_set("filepath") and os.path.isfile(self.filepath):
            return self.execute(context)
        return super().invoke(context, event)

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False

        box = layout.box()
        box.label(text="Detail Levels", icon="MESH_DATA")
        col = box.column(align=False)
        col.prop(self, "lod_mode")
        col.prop(self, "import_collision")

        box = layout.box()
        box.label(text="Geometry & Normals", icon="NORMALS_VERTEX_FACE")
        col = box.column(align=False)
        col.prop(self, "normals_mode")
        sub = col.column()
        sub.enabled = self.normals_mode != "UNMERGED"
        sub.prop(self, "merge_distance")
        sub.prop(self, "sharp_angle_threshold")

    def execute(self, context):
        if not self.filepath or not os.path.exists(self.filepath):
            self.report({"ERROR"}, "No filepath given or file does not exist")
            return {"CANCELLED"}

        ext = os.path.splitext(self.filepath)[1].lower()
        is_zip = ext == ".zip"

        if (
            self.interactive_picker
            and not bpy.app.background
            and not self.beamng_config
            and not self.archive_models
            and (is_zip or _has_nearby_beamng_pc(self.filepath))
        ):
            try:
                src = load_vehicle_source(self.filepath, index_dae_nodes=False)
                if src.has_picker_choices(cdae_mode=True):
                    return bpy.ops.wm.collada_beamng_picker(
                        "INVOKE_DEFAULT",
                        filepath=self.filepath,
                        cdae_mode=True,
                        cdae_lod_mode=self.lod_mode,
                        cdae_import_collision=self.import_collision,
                        cdae_normals_mode=self.normals_mode,
                        cdae_merge_distance=self.merge_distance,
                        sharp_angle_threshold=self.sharp_angle_threshold,
                    )
            except Exception as exc:
                self.report(
                    {"WARNING"},
                    f"Could not open BeamNG archive picker ({exc}); falling back to direct import",
                )

        settings = CdaeImportSettings(
            filepath=self.filepath,
            lod_mode=self.lod_mode,
            import_collision=self.import_collision,
            normals_mode=self.normals_mode,
            merge_distance=self.merge_distance,
            sharp_angle_threshold=self.sharp_angle_threshold,
        )

        # Scripted BeamNG config / archive_models path for .cdae (or .cdae inside .zip)
        if self.beamng_config or self.archive_models or self.spread_out:
            ok, summary, warnings, errors = execute_beamng_import(
                context,
                filepath=self.filepath,
                cdae_mode=True,
                beamng_config=self.beamng_config,
                beamng_parts_mode=self.beamng_parts_mode,
                archive_models=self.archive_models,
                use_common=self.use_common,
                spread_out=self.spread_out,
                base_cdae_settings=settings,
            )
            report_capped(self, "WARNING", warnings)
            if ok:
                if summary:
                    self.report({"INFO"}, summary)
                return {"FINISHED"}
            report_capped(self, "ERROR", errors, fallback="BeamNG .cdae import failed")
            return {"CANCELLED"}

        importer = CdaeImporter(context, settings)
        ok = importer.import_document()
        for info in importer.infos:
            self.report({"INFO"}, info)
        report_capped(self, "WARNING", importer.warnings)
        if ok:
            return {"FINISHED"}
        report_capped(self, "ERROR", importer.errors, fallback="BeamNG .cdae import failed")
        return {"CANCELLED"}


class EXPORT_SCENE_OT_beamng_cdae(bpy.types.Operator, ExportHelper):
    """Export mesh objects to BeamNG Cached Collada (.cdae v31, experimental, StandardMesh only)"""

    bl_idname = "export_scene.beamng_cdae"
    bl_label = "BeamNG Cached Collada (.cdae) [Experimental]"
    bl_options = {"PRESET"}

    filename_ext = ".cdae"
    filter_glob: StringProperty(
        default="*.cdae",
        options={"HIDDEN"},
    )

    use_selection: BoolProperty(
        name="Selection Only",
        description="Export selected mesh objects only",
        default=False,
    )
    apply_modifiers: BoolProperty(
        name="Apply Modifiers",
        description="Apply modifiers before exporting",
        default=True,
    )
    keep_quads: BoolProperty(
        name="Keep Quads",
        description="Not available for .cdae (Torque3D requires triangles)",
        default=False,
    )
    keep_normals: BoolProperty(
        name="Keep Normals",
        description="Preserve pre-triangulation corner normals (custom normals, sharp edges, and non-planar quads) when triangulating",
        default=True,
    )
    compress: BoolProperty(
        name="Compress (Zstandard)",
        description="Compress the .cdae body with Zstandard (requires zstandard in Blender's Python)",
        default=False,
    )

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False

        box = layout.box()
        box.label(text="BeamNG .cdae [Experimental]", icon="EXPORT")
        col = box.column(align=False)
        col.prop(self, "use_selection")
        col.prop(self, "apply_modifiers")
        sub_kq = col.row(align=False)
        sub_kq.enabled = False
        sub_kq.prop(self, "keep_quads")
        col.prop(self, "keep_normals")
        sub = col.column()
        sub.enabled = zstd_available()
        sub.prop(self, "compress")

    def execute(self, context):
        if not self.filepath:
            self.report({"ERROR"}, "No filepath given")
            return {"CANCELLED"}

        settings = CdaeExportSettings(
            filepath=self.filepath,
            use_selection=self.use_selection,
            apply_modifiers=self.apply_modifiers,
            keep_normals=self.keep_normals,
            compress=self.compress and zstd_available(),
        )
        exporter = CdaeExporter(context, settings)
        ok = exporter.export_document()
        report_capped(self, "WARNING", exporter.warnings)
        if ok:
            return {"FINISHED"}
        report_capped(self, "ERROR", exporter.errors, fallback="BeamNG .cdae export failed")
        return {"CANCELLED"}


class IO_FH_collada(bpy.types.FileHandler):
    bl_idname = "IO_FH_collada"
    bl_label = "Collada"
    bl_import_operator = "wm.collada_import"
    bl_export_operator = "wm.collada_export"
    bl_file_extensions = ".dae;.zae;.kmz;.zip"

    @classmethod
    def poll_drop(cls, context):
        return poll_file_object_drop(context)


class IO_FH_beamng_cdae(bpy.types.FileHandler):
    bl_idname = "IO_FH_beamng_cdae"
    bl_label = "BeamNG Cached Collada"
    bl_import_operator = "import_scene.beamng_cdae"
    bl_file_extensions = ".cdae"

    @classmethod
    def poll_drop(cls, context):
        return poll_file_object_drop(context)


def menu_func_import(self, context):
    self.layout.operator(WM_OT_collada_import.bl_idname, text="Collada (.dae, .zae, .kmz, .zip)")
    self.layout.operator(IMPORT_SCENE_OT_beamng_cdae.bl_idname, text="BeamNG Cached Collada (.cdae)")


def menu_func_export(self, context):
    self.layout.operator(WM_OT_collada_export.bl_idname, text="Collada (Default) (.dae)")
    self.layout.operator(
        EXPORT_SCENE_OT_beamng_cdae.bl_idname,
        text="BeamNG Cached Collada (.cdae) [Experimental]",
    )


classes = (
    WM_OT_collada_export,
    WM_OT_collada_import,
    IMPORT_SCENE_OT_beamng_cdae,
    EXPORT_SCENE_OT_beamng_cdae,
    IO_FH_collada,
    IO_FH_beamng_cdae,
)


def register():
    register_picker()
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.TOPBAR_MT_file_import.append(menu_func_import)
    bpy.types.TOPBAR_MT_file_export.append(menu_func_export)


def unregister():
    bpy.types.TOPBAR_MT_file_export.remove(menu_func_export)
    bpy.types.TOPBAR_MT_file_import.remove(menu_func_import)
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    unregister_picker()
