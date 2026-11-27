# Architecture

This document maps every module in `io_scene_collada/` to its responsibilities and, where applicable, the Blender 4.5.1 C++ Collada source file (`source/blender/io/collada/`) it was ported from.

## Design principles

- **Thin public entry points:** `import_collada.py` (`DocumentImporter`, `ImportSettings`) and `export_collada.py` (`DocumentExporter`, `ExportSettings`) remain the public API used by operators and tests.
- **Mixin composition, no circular imports:** `DocumentImporter` and `DocumentExporter` inherit domain-focused mixins from `importer/` and `exporter/`. Submodules share state only through `self` (the importer/exporter instance) and never import `import_collada.py` or `export_collada.py`.
- **Keep files small:** Every module stays under ~1,000 lines so bug fixes land in one focused place.

---

## Top-level entry points & shared utilities

| Module | Responsibilities | Blender 4.5.1 C++ origin |
| --- | --- | --- |
| `io_scene_collada/__init__.py` | Extension registration, Blender operators (`wm.collada_import`, `wm.collada_export`, `import_scene.beamng_cdae`, `export_scene.beamng_cdae`), FileHandler, Collection Exporters (`IO_FH_collada`, `IO_FH_beamng_cdae`) | `collada.cpp`, `io_collada.cc` |
| `io_scene_collada/collada_utils.py` | ID translation (`GlobalIdMap`), matrix/axis transforms, `build_export_set`, `flush_edit_mode_objects`, `get_mesh_copy` (modifier evaluation, Keep Normals, Keep Quads), Blender 5.0+ channelbag F-curve helpers, Principled BSDF socket helpers, `report_capped`, collection visibility checks | `collada_utils.cpp`, `collada_internal.cpp`, `BlenderContext.cpp` |
| `io_scene_collada/xml_writer.py` | Streaming XML writer (`ColladaStreamWriter`) matching OpenCOLLADA formatting and float32/int32 array formatters | OpenCOLLADA `COLLADASW::StreamWriter` |
| `io_scene_collada/beamng_materials.py` | BeamNG `main.materials.json` lookup and `.png`/`.dds` texture fallback helpers | *(new in blender5-collada)* |

---

## Collada Importer (`import_collada.py` + `importer/`)

`DocumentImporter` in `import_collada.py` composes six mixins (`ArchiveMixin`, `MaterialsMixin`, `MeshMixin`, `RigMixin`, `AnimationMixin`, `SceneMixin`) and owns `ImportSettings`, per-document state reset (`_reset_dae_state`), top-level file/archive dispatch (`import_document`), exception/cleanup guards (`_safe_import_dae_tree`), and library orchestration (`_import_dae_tree`).

| Module | Responsibilities | Blender 4.5.1 C++ origin |
| --- | --- | --- |
| `io_scene_collada/import_collada.py` | `ImportSettings`, `DocumentImporter` entry point, state reset, archive/disk dispatch, top-level tree orchestration | `DocumentImporter.cpp` |
| `io_scene_collada/importer/xml_parse.py` | Expat parser-level DTD/ENTITY rejection, `_parse_xml_safe`, XML namespace traversal, strict NumPy/text array parsers, `<accessor>` `offset`/`count` slicing, `<extra>` Blender profile parser, URI/path normalization | `DocumentImporter.cpp`, `MeshImporter.cpp`, `collada_internal.cpp` |
| `io_scene_collada/importer/archive.py` | `.zip`/`.zae`/`.kmz` indexing, zip-bomb & 50k entry-count caps, `manifest.xml` discovery, `main.materials.json` loading, archive image extraction & packing | *(new in blender5-collada)* |
| `io_scene_collada/importer/materials.py` | `<library_images>`, `<library_effects>`, `<library_materials>`, Principled BSDF material setup, BeamNG texture fallback, per-polygon material bindings, shared-mesh material slot optimization | `ImageImporter.cpp`, `MaterialExporter.cpp` / `DocumentImporter.cpp`, `MeshImporter.cpp` (`optimize_material_assignements`) |
| `io_scene_collada/importer/mesh.py` | `<library_geometries>`, `_read_mesh` (`<triangles>`, `<polylist>`, `<polygons>`/`<ph>` hole tessellation, `<tristrips>`, `<lines>`, UVs, vertex colors, custom split normals, duplicate/degenerate face validation), sharp edges from normals | `MeshImporter.cpp` |
| `io_scene_collada/importer/rig.py` | `_SkinData`, `_MorphData`, `<library_controllers>` (skin & morph), armature and edit-bone construction, bone collections, bone-parented child attachment, shape keys | `ArmatureImporter.cpp`, `SkinInfo.cpp`, `ExtraHandler.cpp` |
| `io_scene_collada/importer/animation.py` | `_AnimChannelData`, `<library_animations>`, F-curve creation/population, object & joint matrix animation, DCC SID target resolution, decomposed channel animation | `AnimationImporter.cpp` |
| `io_scene_collada/importer/scene.py` | `<asset>` units & up-axis conversion, `<library_cameras>`, `<library_lights>`, `<library_visual_scenes>` node transforms, scene/collection placement, `node_filter` dependency pruning | `DocumentImporter.cpp`, `CameraImporter.cpp`, `LightImporter.cpp`, `TransformReader.cpp` |

---

## Collada Exporter (`export_collada.py` + `exporter/`)

`DocumentExporter` in `export_collada.py` composes five mixins (`GeometryMixin`, `MaterialsMixin`, `RigMixin`, `AnimationMixin`, `SceneMixin`) and owns `ExportSettings`, option normalization, Edit Mode flushing, export-set construction, `parent -> children` indexing, batch temp-mesh cleanup, and top-level document serialization (`export_current_scene`).

| Module | Responsibilities | Blender 4.5.1 C++ origin |
| --- | --- | --- |
| `io_scene_collada/export_collada.py` | `ExportSettings`, `DocumentExporter` entry point, option normalization, export lifecycle & batch temp-mesh cleanup | `DocumentExporter.cpp` |
| `io_scene_collada/exporter/geometry.py` | Float-row deduplication (`_dedup_float_rows`), `<library_geometries>`, normal generation, XML `<source>` writers, `<triangles>`/`<polylist>`/`<lines>` primitive lists, morph target geometry export | `GeometryExporter.cpp` |
| `io_scene_collada/exporter/materials.py` | Native path to `file://` URI conversion, export-set material collection, `<library_effects>`, `<library_images>` (with texture copy), `<library_materials>` | `EffectExporter.cpp`, `ImageExporter.cpp`, `MaterialExporter.cpp` |
| `io_scene_collada/exporter/rig.py` | Rest-pose matrix synthesis, `<library_controllers>` (skin & morph), bone filtering & hierarchy XML, armature bone collection extras, `<instance_controller>` & `<skeleton>` URLs | `ArmatureExporter.cpp`, `ControllerExporter.cpp`, `SkinInfo.cpp` |
| `io_scene_collada/exporter/animation.py` | Axis/channel mapping tables, frame-to-time conversion, matrix sampling & F-curve keyframe export for objects and armature bones | `AnimationExporter.cpp`, `AnimationClipExporter.cpp` |
| `io_scene_collada/exporter/scene.py` | `<asset>` metadata & units, `<library_cameras>`, `<library_lights>`, `<library_visual_scenes>`, node hierarchy & matrix/decomposed transforms, `<bind_material>`, `<scene>` | `DocumentExporter.cpp`, `CameraExporter.cpp`, `LightExporter.cpp`, `SceneExporter.cpp`, `TransformWriter.cpp`, `ExtraTags.cpp` |

---

## BeamNG `.cdae` & Vehicle Config Packages

*(All new in blender5-collada; no Blender 4.5.1 C++ counterpart.)*

| Module | Responsibilities |
| --- | --- |
| `io_scene_collada/cdae/msgpack_lite.py` | Pure-Python MsgPack reader/writer used for BeamNG `.cdae` v31 headers & bodies |
| `io_scene_collada/cdae/reader.py` | Binary `.cdae` v31 (`TSShape`) parser, Zstandard decompression, vertex/normal/UV/tangent/node/primitive unpacking |
| `io_scene_collada/cdae/importer.py` | `.cdae` to Blender mesh/node/material importer |
| `io_scene_collada/cdae/exporter.py` | Blender scene to `.cdae` v31 exporter (GPU vertex splitting, MikkTSpace tangents, `Quat16` rigid hierarchy, non-rigid residual baking) |
| `io_scene_collada/beamng/lenient_json.py` | Single lenient JSON / JBeam parser (comments, missing/trailing commas, bare constants, nesting depth guard) |
| `io_scene_collada/beamng/expr.py` | Safe BeamNG `$=` mesh/modifier expression evaluator with candidate & recursion caps |
| `io_scene_collada/beamng/parts.py` | JBeam table & part index extraction across vehicle and `common.zip` |
| `io_scene_collada/beamng/resolve.py` | `.pc` config slot-tree resolution & mesh candidate collection |
| `io_scene_collada/beamng/source.py` | Unified vehicle folder / zip / `common.zip` reader with archive safety limits |
| `io_scene_collada/beamng/picker.py` | Blender UI picker operator for BeamNG `.pc` vehicle configs |
