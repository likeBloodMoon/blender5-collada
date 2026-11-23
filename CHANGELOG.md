# Changelog

All notable changes to **Collada (DAE) Import/Export for Blender 5.0+** are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.2.0] - 2026-11-23

### Added
- **Collection Exporters support**: added hidden `collection` property to `wm.collada_export` so Blender 5.0+'s per-collection Exporters panel exports only objects belonging to that collection (ideal for exporting individual vehicle/game parts in one click).
- **UI error and warning reporting**: `wm.collada_import` now collects exact diagnostics (e.g., `"Root element is not <COLLADA>"`, `"Missing texture: wood.png"`, XML parse errors, archive errors) and surfaces them directly via `self.report()` instead of hiding them in the system console.

### Fixed
- **Archive (`.zae` / `.kmz` / `.zip`) texture security**: confined archive image resolution strictly to the extracted temporary directory so an archive cannot read or pack arbitrary files from outside the archive on disk.
- **Archive partial-failure cleanup**: if an archive import fails partway, any newly loaded temporary images are removed from `bpy.data.images` before the temp directory is deleted.
- Removed duplicate `node_elem_by_id` initialization/assignment and unused imports (`Euler`, `Quaternion`, `field`) in `import_collada.py`.

### Tests
- Added `pro_zae_blocks_external_disk_image`, `pro_collection_exporter`, and `pro_error_reporting` to CI deep verification (now **60 / 60** checks).

## [1.1.1] - 2026-11-23

### Fixed
- **Hard edges / Shade Auto Smooth smoothed over on export**: `_create_normals()` now uses `mesh.corner_normals` whenever `mesh.has_custom_normals or mesh.normals_domain == "CORNER"` (matching Blender 4.5.1's `GeometryExporter.cpp:635`), preserving sharp rim/crease normals on meshes with `sharp_edge` or mixed flat/smooth faces.
- **N-gon triangulation parity**: switched `bmesh.ops.triangulate()` in `collada_utils.py` from `ngon_method="EAR_CLIP"` to `ngon_method="BEAUTY"` and added `bm.normal_update()` before triangulation to match Blender 4.5.1's `bc_triangulate_mesh()` (`calc_face_normal = true`, `use_beauty = false` = `MOD_TRIANGULATE_NGON_BEAUTY`).

### Tests
- Added `07_sharp_autosmooth_polylist` and `07_sharp_autosmooth_triangulated` (32-sided cylinder with 64 sharp rim edges and n-gon caps) to CI parity tests (now **57 / 57** checks).

## [1.1.0] - 2026-11-23

### Performance
- **3.4× faster large-mesh export** (`14.53 s → 4.24 s` on a 502k-triangle mesh): replaced per-vertex/per-polygon/per-loop Python loops in `export_collada.py` with `foreach_get` + NumPy for positions, normals, UVs, byte vertex colors, loose edges, and `<p>` index assembly.
- Added batch float32 and integer array formatters (`fmt_f32_array`, `fmt_int_array`) in `xml_writer.py` preserving exact OpenCOLLADA formatting and byte-parity.
- **Faster large-mesh import** (`8.79 s → 6.97 s` on a 502k-triangle mesh): switched `<float_array>`, `<vcount>`, and `<p>` parsing to C-speed `np.fromstring()`, vectorized primitive/normal/UV/color/material indexing, and switched mesh construction to direct `foreach_set` (`vertices`, `loops`, `polygons`, `use_smooth`, `uv`, `color_srgb`, `material_index`) + `me.update(calc_edges=True)`.

## [1.0.0] - 2026-11-23

### Added
- **Drop-in Blender 5.0+ Collada Extension** ported from Blender 4.5.1 LTS with identical `wm.collada_import` / `wm.collada_export` operator IDs, properties, defaults, and tabbed UI.
- **Full COLLADA 1.4.1 Import & Export**:
  - Meshes (`<triangles>`, `<polylist>`, `<polygons>` including `<ph>`, `<trifans>`, `<tristrips>`, `<lines>`), custom split normals, multi-UV maps (`active_uv_only`), byte vertex colors, modifier application, and geometry instancing.
  - Principled BSDF materials (`<lambert>`, `<phong>`, `<blinn>`, `<constant>`), textures, `use_texture_copies`, and smart cross-platform texture path fallback (`textures/`, `images/`, `maps/`, basename lookup).
  - Perspective & orthographic cameras; Point, Sun, Spot, and Area lights with Blender `<extra>` profile DNA preservation.
  - Armatures, skinning (`<skin>`, vertex weights, `<bind_shape_matrix>`, `Name_array` & `IDREF_array` joints), bone collections, morph targets / shape keys (`<morph>`), OpenSim / Second Life compatibility (`open_sim`, `deform_bones_only`), and armature import options (`auto_connect`, `find_chains`, `fix_orientation`, `keep_bind_info`).
  - Object and pose-bone animations (matrix sampled, decomposed sampled, keyframe curves with smooth tangents, plus third-party DCC transform `sid` resolution) ported to Blender 5.0+'s layered Action / Slot / Channelbag API.
- **Mark Sharp from Normals** import option (`sharp_edges_from_normals` + `sharp_angle_threshold`): converts COLLADA normals directly into clean `sharp_edge` markings on smooth faces, preventing flat triangles from hiding sharp creases and eliminating false sharp-edge noise.
- **Archive & Drag-and-Drop Support**: `.zae`, `.kmz`, and `.zip` archive import with `manifest.xml` `<dae_root>` resolution, zip-slip protection, automatic image packing, and `IO_FH_collada` FileHandler drag-and-drop.
- **Automated CI & Verification**: GitHub Actions workflow running all 55 parity & deep verification checks against Blender 4.5.1 LTS on every push, PR, and release tag.

[1.2.0]: https://github.com/likeBloodMoon/blender5-collada/compare/v1.1.1...v1.2.0
[1.1.1]: https://github.com/likeBloodMoon/blender5-collada/compare/v1.1.0...v1.1.1
[1.1.0]: https://github.com/likeBloodMoon/blender5-collada/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/likeBloodMoon/blender5-collada/releases/tag/v1.0.0
