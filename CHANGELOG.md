# Changelog

All notable changes to **Collada (DAE) Import/Export for Blender 5.0+** are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.4.0] - 2026-11-24

- Added: BeamNG .cdae (v31) import; experimental .cdae export

### Details
- **BeamNG `.cdae` (v31) Importer** (`import_scene.beamng_cdae` + `IO_FH_beamng_cdae` drag-and-drop):
  - Pure-Python bounds-checked MsgPack reader (`io_scene_collada/cdae/msgpack_lite.py`), optional Zstandard decompression via Blender's bundled `zstandard` at runtime (clean error if unavailable), strict `elemSize` validation, and rejection of non-v31 files (such as legacy v26) with a clear version error.
  - **Normals modes**: **Exact (keep game normals)** *(default — merges split vertices, writes exact `custom_normal` values, and marks `sharp_edge` hints so creases survive if custom normals are cleared)*, **Sharp edges (easier to edit)**, and **Unmerged (raw GPU vertices)**.
  - **Detail levels & collision**: supports **Highest detail only** or **All detail levels** (into per-detail collections) and an optional **Import Collision Meshes** toggle.
  - **Skinned meshes**: imports `SkinMesh` armatures, vertex groups, and Armature modifiers using verified row-major `initialTransforms` (animation sequences are not imported yet and are skipped with an info notice).
  - **Double-sided, layered, and duplicate faces**: preserves opposite-winding double-sided faces and differing-material/attribute overlay/decal layers on dedicated unwelded vertex layers, dropping only truly identical duplicates (same vertices, winding, material, UVs, color, and normals). Tiny faces collapsed during vertex merging are reported as `INFO` (`"Mesh '<name>': N tiny faces (under X mm) merged away"`).
  - **Front-face winding fix**: converts Torque3D/BeamNG CW front-face triangle winding to Blender CCW on import (and back to CW on export).
  - **Empty cleanup & display size**: single-mesh 1-to-1 nodes import directly as Mesh objects without redundant Empties in both **Highest** and **All detail levels** modes; Empties created for real hierarchies or multi-LOD nodes use model-relative `empty_display_size` (2% of bounding-box extent, clamped to `0.01–0.10 m`, `0.05 m` fallback) instead of `1.0 m`.
- **BeamNG `.cdae` (v31) Exporter [Experimental]** (`export_scene.beamng_cdae`):
  - Scoped to static meshes (`StandardMesh`) only and labeled `[Experimental]` (not yet tested in-game), with CW winding conversion, exact float32 GPU vertex deduplication, `calc_tangents()`, selection-only & modifier-application options, and optional Zstandard compression via Blender's bundled `zstandard`.
- **Docs, Verification & CI**:
  - Added update restart note to `README.md` and `CONTRIBUTING.md` (restart Blender after installing a new version over an existing one) and documented `.cdae` known limitations.
  - Verified by parsing all `6,319` v31 `.cdae` files shipped with BeamNG.drive `0.40.6.0` (0 parse errors), paired `.dae` vs `.cdae` checks across 3 vehicles, 3-mode export round-trip oracle (`0.0000°` median / `0.0280°` p99 normal delta in default **Exact** mode), and positional triangle / longest-edge checks across all detail levels and all 3 Normals modes.
  - Wired synthetic `.cdae` import, export round-trip, and fuzz tests (`tests/test_cdae_import.py`, `tests/test_cdae_export.py`) into GitHub Actions across Blender `5.0.1` and `5.3.0`. No game files are committed or used in CI.

## [1.3.1] - 2026-11-23

- Fixed: malformed files could create invalid meshes that crash Blender; archive textures are now read directly from the zip

### Details
- **Native & Python array parsing**: `native/fastfmt.cpp` (`parse_f32` / `parse_i32`) now accepts a single leading `+` and reports incomplete parses via an `int* ok` out-param so `_native_loader.py` falls back to Python. Python parsing wraps `np.fromstring`, replaces legacy 3ds Max `1.#QNAN`/`#IND`/`#INF`/`#SNAN` tokens with `0.0` (+ warning), and skips broken geometries with clear warnings instead of tracebacks.
- **Mesh safety & validation**: Vectorized NumPy bounds checks drop only faces with out-of-range/negative `VERTEX`/`NORMAL`/`TEXCOORD`/`COLOR` indices and replace non-finite positions/attributes with `0.0`. Every imported mesh runs `me.validate(clean_customdata=False)` before applying per-loop UVs/colors/normals and shape keys, and `wm.collada_import` caps UI warnings to the first 5 plus an `"and N more"` summary.
- **In-memory archive import (`.zae`/`.kmz`/`.zip`)**: Reads the root `.dae` (`manifest.xml` `<dae_root>` first, else deterministic sorted `.dae`) and all textures directly from `zipfile.ZipFile` in memory without temporary directories, never reads textures from the real filesystem, packs & verifies decoded textures via `img.pack(data=..., data_len=...)`, and enforces zip-bomb limits (1 GiB single entry, 4 GiB total, >200× ratio above 50 MB).
- **Tests**: Added `tests/test_malformed.py`, `tests/fuzz_import.py` (200 seeded iterations), and `tests/test_archives.py` to CI across Blender 5.0.1 and 5.3.0 with `COLLADA_NATIVE=1` and `COLLADA_NATIVE=0`.

## [1.3.0] - 2026-11-23

### Added
- **Fast Exact Normals** import option (`fast_exact_normals`, default `False`): writes COLLADA normals directly to Blender's `custom_normal` `FLOAT_VECTOR` `CORNER` attribute via `foreach_set`, avoiding `short2` compression and cutting large-mesh import time dramatically.
- **Optional C++17 `fastfmt` native accelerator** (`native/fastfmt.cpp` + `io_scene_collada/_native_loader.py`): plain C ABI loaded via `ctypes` (`fmt_f32_array_mt`, `fmt_i32_array_mt`, `parse_f32`, `parse_i32`) with silent pure-Python fallback and `COLLADA_NATIVE=0` switch.
- **Multi-platform CI & Releases**: GitHub Actions now runs `ruff` linting, builds native binaries for `windows-x64`, `linux-x64`, `macos-arm64`, and `macos-x64`, fuzz-tests 10M random `float32` values + edge cases, verifies byte-identical output between `COLLADA_NATIVE=1` and `COLLADA_NATIVE=0` across **Blender 5.0.1 and 5.3.0**, and packages both per-platform accelerated zips (`--split-platforms`) and a pure-Python universal zip.

### Performance (Suzanne + Subdivision Level 5: 503,808 faces / 2,015,232 loops, Blender 5.3.0)
| Operation | Before (`v1.2.0`) | After (`v1.3.0`, pure-Python) |
| --- | --- | --- |
| **Import (`custom_normals=True`, default)** | `9.47 s` | **`5.08 s`** (`all_loop_normals.tolist()` to `normals_split_custom_set`) |
| **Import (`fast_exact_normals=True`)** | `9.47 s` | **`3.87 s`** (`custom_normal` `FLOAT_VECTOR` attribute via `foreach_set`) |

### Fixed
- Archive (`.zae`/`.kmz`/`.zip`) images are now packed immediately on load so mid-import errors never leave dangling temp-folder image paths.
- Updated `get_mesh_copy()` docstring (`BEAUTY` n-gon method) and removed remaining unused imports/locals (`fmt_bool`, `double_round`, `Material`, `Action`, `fps`, `es_sock`, `irest_dae`).
- Added `08_ngon7_bevel_mirror_tri` (7-gon + Bevel & Mirror modifiers with `triangulate=True`) to CI parity tests (now **61 / 61** checks).

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

[1.4.0]: https://github.com/likeBloodMoon/blender5-collada/compare/v1.3.1...v1.4.0
[1.3.1]: https://github.com/likeBloodMoon/blender5-collada/compare/v1.3.0...v1.3.1
[1.3.0]: https://github.com/likeBloodMoon/blender5-collada/compare/v1.2.0...v1.3.0
[1.2.0]: https://github.com/likeBloodMoon/blender5-collada/compare/v1.1.1...v1.2.0
[1.1.1]: https://github.com/likeBloodMoon/blender5-collada/compare/v1.1.0...v1.1.1
[1.1.0]: https://github.com/likeBloodMoon/blender5-collada/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/likeBloodMoon/blender5-collada/releases/tag/v1.0.0
