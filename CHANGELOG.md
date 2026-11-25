# Changelog

All notable changes to **Collada (DAE) Import/Export for Blender 5.0+** are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

- Improved (#12): deduplicate UV and vertex color sources on `.dae` export (cuts `big_mesh` `.dae` size from `8.96 MB` to `< 6 MB` with byte-exact round-trip and faster export/import)

### Details
- **UV & vertex color deduplication** (`export_collada.py`, `__init__.py`): added exact float32 bit-level deduplication (`_dedup_float_rows`) preserving first-seen order for all UV maps and `BYTE_COLOR` vertex color attributes, assigning per-layer `<input>` offsets in `<triangles>`/`<polylist>` and indexing unique values in `<p>`. Pass `deduplicate_attributes=False` to `wm.collada_export` to keep the legacy per-corner layout for Blender 4.5.1 byte-parity testing.

## [1.5.1] - 2026-11-25

- Fixed (#10): preserve pre-triangulation corner normals (custom normals, sharp edges, and flat/non-planar quads) when triangulating on `.dae` and `.cdae` export
- Fixed (#1): catch unexpected `.dae` import exceptions on both disk and archive paths, clean up partially created datablocks, and report a single clean UI error instead of a traceback
- Fixed (#11): `.cdae` export now preserves child transforms, inherited/non-uniform/shear scale under scaled parents, and fixes mirrored (negative-scale) objects being exported inside-out

### Details
- **Scaled-parent hierarchies & mirrored winding in `.cdae` export** (`cdae/exporter.py`): build node local transforms from each object's rigid world transform (`Matrix.LocRotScale(loc, rot, None)`) relative to its parent's `Quat16`-quantized rigid world transform, and bake any non-rigid residual (`rigid_q.inverted() @ matrix_world`) into mesh vertices, normals, and tangents—reversing triangle winding and bitangent sign when the residual determinant is negative. Fixes misplaced/missized children under scaled parents (`hierarchy` error reduced from `3.35 m` to `0.00079 mm`) and fixes mirrored (negative-scale) objects being exported inside-out in `.cdae`.
- **Clean `.dae` import error handling & partial-datablock cleanup** (`import_collada.py`): wrapped `_import_dae_tree()` via `_safe_import_dae_tree()` on both disk and archive (`MAIN` / `ALL`) paths so unexpected errors log their traceback to the console only, remove any partially created objects/datablocks (and empty sub-collections in multi-DAE archives), and surface a clean `"Import failed: ..."` message in Blender's UI. Deduplicated invalid animation `stride` warnings in `_read_animation_elem()`.
- **Keep Normals through triangulation** (`collada_utils.py`, `export_collada.py`, `cdae/exporter.py`, `__init__.py`): added a **Keep Normals** (`keep_normals`, default `True`) option to both `wm.collada_export` (Geom tab and Collection Exporters, greyed out when **Triangulate** is off) and `export_scene.beamng_cdae`. Before `bmesh.ops.triangulate()`, pre-triangulation `corner_normals` are copied into a temporary `FLOAT_VECTOR`/`CORNER` attribute so bmesh carries them onto the split triangles, exported directly, and then removed.
- **Note for Python API callers**: because `keep_normals=True` is now the default, triangulated exports preserve viewport normals instead of recomputing them after triangulation; pass `keep_normals=False` to restore the `v1.5.0` / Blender 4.5.1 post-triangulation normal behaviour.

## [1.5.0] - 2026-11-25

- Added: Experimental BeamNG archive picker, `.pc` vehicle configuration import (`.dae` and `.cdae`), and automatic `common.zip` part lookup

### Details
- **BeamNG archive & folder picker `[Experimental]`** (`io_scene_collada/beamng/picker.py`, `__init__.py`): opening a multi-model `.zip`/`.zae`/`.kmz` archive, a vehicle archive with `.pc` configs, or a loose `.dae`/`.cdae` inside a vehicle folder with `.pc` configs interactively opens the **Import BeamNG Archive [Experimental]** dialog. Supports vehicle/config selection, **Only this config** / **Config + other parts hidden** / **Everything** part modes, model filtering & selection, optional **Spread selected models out side by side**, and per-archive remembered choices. Added `.zip` to `IO_FH_collada` drag-and-drop and the File → Import menu label (`Collada (.dae, .zae, .kmz, .zip)`).
- **BeamNG `.pc` configuration resolver & `common.zip` lookup** (`io_scene_collada/beamng/`): unified lenient JSON/jbeam parser (`lenient_json.py`, also shared by `beamng_materials.py`), table parser (`parts.py`, supporting both `slots` and `slots2`), slot-path & legacy `.pc` resolver (`resolve.py`), safe recursive-descent `$=` expression evaluator (`expr.py`), and session-cached archive/folder scanner (`source.py`) with automatic sibling `common.zip` / `vehicles/common/` discovery.
- **Selective node filtering & scene organization** (`import_collada.py`, `cdae/importer.py`, `picker.py`): added `node_filter` (pruning unreferenced nodes, geometries, materials, and images) and scriptable operator properties (`beamng_config`, `beamng_parts_mode`, `archive_models`, `use_common`, `spread_out`) to both `wm.collada_import` and `import_scene.beamng_cdae`. Imported config objects are tagged with `beamng_part`, `beamng_slot_path`, `beamng_config`, and `beamng_source` custom properties, parts requiring jbeam placement are grouped under `Needs placement`, and an import report text datablock is written to the `.blend`.
- **Hardening & tests**: added safe numeric/stride/skin `vertex_weights` guards in `import_collada.py`, expanded `tests/fuzz_import.py`, `tests/test_malformed.py`, and `tests/test_archives.py`, added synthetic unit tests (`tests/test_beamng_parse.py`, `tests/test_beamng_resolve.py`) to CI, and cleared stale `tests/_work` artifacts automatically in `tests/run_parity_tests.py`.

## [1.4.4] - 2026-11-24

- Fixed: multi-`.dae` zip archive import, preserving UVs/colors/normals when dropping duplicate or degenerate triangles, `.dds` fallback & BeamNG `main.materials.json` texture loading, clearer non-`.cdae` file errors, and `compare_cdae.py` oracle accuracy

### Details
- **Multi-`.dae` archive import** (`import_collada.py`, `__init__.py`): added **Archive Models** (`archive_mode`: **All models (collections)** *(default)* or **Main model only**) for `.zip`/`.zae`/`.kmz` archives. `manifest.xml` `<dae_root>` is still honored when present; otherwise `.dae` entries are ranked preferring the `.dae` matching the archive name and shallowest path (ignoring `__MACOSX` / `._*` junk). In **All models** mode, multi-`.dae` car archives import every `.dae` into its own collection under the active collection; in **Main model only** mode, the main `.dae` is imported and skipped `.dae` files are reported in a warning.
- **Preserve UVs, colors, and normals when dropping bad faces** (`import_collada.py`): `_read_mesh()` now filters degenerate faces (repeated vertex in a face) and duplicate faces (same sorted vertex set) together with their per-loop UV, vertex color, normal, and material-assignment arrays *before* constructing the Blender mesh, so one bad/duplicate triangle no longer causes `me.validate()` to discard all UVs, colors, and custom normals for the entire mesh.
- **`.dds` fallback & lenient `main.materials.json` texture loading** (`beamng_materials.py`, `import_collada.py`, `cdae/importer.py`): when a referenced `.png`/raster texture is missing, the importer now also checks for the same filename with `.dds` (and `.dds` → `.png`), both inside archives and on disk. Added a lenient JSON parser (supporting trailing commas and `//`/`/* */` comments) that reads `main.materials.json` and links `baseColorMap` / `colorMap` / `diffuseMap` textures to untextured materials without warning on external `common.zip` textures.
- **Clear non-`.cdae` file errors** (`cdae/reader.py`): handing a `.zip`, `.dae`/XML, `.png`, `.dds`, `.blend`, or other non-`.cdae` file to the `.cdae` importer now reports a clear format error instead of a misleading `"Unsupported .cdae version 80"`.
- **Oracle script accuracy** (`tests/compare_cdae.py`): removed the 2,000-triangle sampling cap in `_match_mesh_normals()` so every triangle is compared, skipped zero-length normals instead of reporting false `90°` errors, and computed UV0/UV1 bounding boxes over non-sliver triangles so zero-area slivers removed in `Exact` mode do not cause false UV mismatches.

## [1.4.3] - 2026-11-24

- Fixed: prevented transitive vertex-merge chaining in `.cdae` import and added real-file skinned rest-pose verification

### Details
- **Non-chaining KD-tree vertex merge** (`io_scene_collada/cdae/importer.py`): replaced union-find transitive clustering in `_merge_verts_by_distance()` with greedy first-vertex-bounded clustering so every vertex merged into a cluster stays strictly within `merge_distance` of the cluster's first vertex (eliminating 31 chained merges up to `1.8× tol` on `midtruck_windshield_dmg`).
- **Tests** (`tests/test_cdae_import.py`): added synthetic non-chaining merge regression test (`kdtree_merge_across_grid_boundary_no_chaining`) and depsgraph rest-pose vertex/normal verification against `initialVerts`/`initialNorms` for skinned `.cdae` meshes.

## [1.4.2] - 2026-11-24

- Fixed: active collection linking for `.dae` and `.cdae` imports, exact KD-tree vertex merging, skinned `.cdae` bind-pose bones with roll, explicit tangent/mesh-type diagnostics, and `.cdae` export warning summary

### Details
- **Active collection linking** (`import_collada.py`, `cdae/importer.py`): both `.dae` and `.cdae` importers now link newly created objects (meshes, armatures, empties, and `All detail levels` LOD sub-collections) into the active Outliner collection (`context.view_layer.active_layer_collection.collection`) instead of always linking to the root Scene Collection.
- **Skinned `.cdae` bind pose & bone roll** (`cdae/importer.py`): `_build_armature()` now builds skinned edit bones from each `SkinMesh`'s row-major `initialTransforms` inverse bind matrices (`inv_bind.inverted()`) with `Bone.AxisRollFromMatrix()` roll, falling back to default node transforms only for unskinned nodes.
- **Exact KD-tree vertex merge** (`cdae/importer.py`): replaced grid-rounding quantization in `_merge_verts_by_distance()` with exact float32 deduplication followed by `mathutils.kdtree.KDTree` Euclidean radius clustering + union-find, preventing close vertices on opposite sides of a grid line from failing to merge.
- **Unsupported `.cdae` mesh types rejected clearly** (`cdae/reader.py`): mesh types other than `StandardMesh (0)`, `SkinMesh (1)`, and `NullMesh (4)` (such as `SortedMesh (3)`, `DecalMesh (2)`, or `BillboardMesh (5)`) now raise a clear `CdaeError` instead of being read as `StandardMesh` and desynchronizing the stream.
- **Explicit tangent failure warnings & export warning cap** (`cdae/exporter.py`, `__init__.py`): `calc_tangents()` failures during `.cdae` export now append a warning instead of being silently swallowed, and `export_scene.beamng_cdae` reports `"... and N more warning(s)"` when more than 5 warnings occur (matching the import operators).

## [1.4.1] - 2026-11-24

- Performance: vectorized sharp-edge detection from normals with NumPy (`4×–5.7×` faster `.cdae` and `.dae` imports on large meshes)

### Details
- **Vectorized `_apply_sharp_edges_from_normals`** (`io_scene_collada/import_collada.py`): replaced per-polygon/per-edge Python dict and `Vector.angle()` loops with `foreach_get`, `np.argsort` grouping by `edge_index`, vectorized dot-product angle calculation for manifold edges, exact non-manifold (`3+` loops) fallback, and a single `foreach_set` write to `sharp_edge`. Benefits both BeamNG `.cdae` (`Exact` and `Sharp edges` modes) and COLLADA `.dae` (`Mark Sharp from Normals`).
- Verified **100% identical `sharp_edge` sets** across 22 snapshots (`1,308` meshes / `233,792` sharp edges: all synthetic fixtures, 6 local real `.cdae` files in all 3 Normals modes, `.dae` sharp-edge fixtures, and non-manifold edges).

### Performance (Blender 5.3.0, `.cdae` import)
| Mesh | Mode | Before (`v1.4.0`) | After (`v1.4.1`) | Speedup |
| --- | --- | ---: | ---: | ---: |
| `wigeon.cdae` (`83,935` tris) | Exact / Sharp | `1.85 s` / `1.84 s` | **`0.46 s`** / **`0.47 s`** | `4.1×` / `3.9×` |
| `midtruck.cdae` (`280,787` tris) | Exact / Sharp | `6.54 s` / `6.22 s` | **`1.62 s`** / **`1.55 s`** | `4.0×` / `4.0×` |
| Suzanne + Subsurf 5 (`503,808` quads / `1,007,616` tris) | Exact / Sharp | `23.51 s` / `23.15 s` | **`4.31 s`** / **`4.03 s`** | `5.5×` / `5.7×` |

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
- **COLLADA (`.dae`) importer hardening**:
  - Fixed a 32-bit `int32` sum/cumsum overflow on Windows when malformed `<polylist>` `<vcount>` values exceed `2^31-1`, added empty-array guards before normal/UV/color indexing, and skipped `<polylist>` / `<polygons>` faces with fewer than 3 vertices (`0`, `1`, or `2`) with a single per-mesh warning (`"N invalid faces with fewer than 3 vertices skipped"`).
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

[1.5.1]: https://github.com/likeBloodMoon/blender5-collada/compare/v1.5.0...v1.5.1
[1.5.0]: https://github.com/likeBloodMoon/blender5-collada/compare/v1.4.4...v1.5.0
[1.4.4]: https://github.com/likeBloodMoon/blender5-collada/compare/v1.4.3...v1.4.4
[1.4.3]: https://github.com/likeBloodMoon/blender5-collada/compare/v1.4.2...v1.4.3
[1.4.2]: https://github.com/likeBloodMoon/blender5-collada/compare/v1.4.1...v1.4.2
[1.4.1]: https://github.com/likeBloodMoon/blender5-collada/compare/v1.4.0...v1.4.1
[1.4.0]: https://github.com/likeBloodMoon/blender5-collada/compare/v1.3.1...v1.4.0
[1.3.1]: https://github.com/likeBloodMoon/blender5-collada/compare/v1.3.0...v1.3.1
[1.3.0]: https://github.com/likeBloodMoon/blender5-collada/compare/v1.2.0...v1.3.0
[1.2.0]: https://github.com/likeBloodMoon/blender5-collada/compare/v1.1.1...v1.2.0
[1.1.1]: https://github.com/likeBloodMoon/blender5-collada/compare/v1.1.0...v1.1.1
[1.1.0]: https://github.com/likeBloodMoon/blender5-collada/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/likeBloodMoon/blender5-collada/releases/tag/v1.0.0
