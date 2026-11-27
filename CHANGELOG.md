# Changelog

All notable changes to **Collada (DAE) Import/Export for Blender 5.0+** are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.5.8] - 2026-11-27

- Changed: Removed the local version-check / update-restart popup and the optional `fastfmt` native DLL loader (`_native_loader.py` and `native/fastfmt.cpp`) so the extension is 100% pure Python and meets `extensions.blender.org` guidelines
- Fixed (#54): Importing a `.dae` mesh with more than 8 UV maps now falls back to `FLOAT2` `CORNER` attributes instead of crashing when `mesh.uv_layers.new()` returns `None` (fixes BeamNG `police_parts.dae` and 6 vanilla police configs)
- Fixed (#53): **Include all Actions** no longer crashes when an object has `animation_data` with `action = None`
- Fixed (#63): `.cdae` export now selects the render/active color attribute instead of hard-coding `color_attributes[0]`, converts `FLOAT_COLOR` to sRGB cleanly, and warns when extra UV maps (>2) or color attributes (>1) are skipped
- Fixed (#64): Realized Geometry Nodes instances now preserve their own materials and face `material_index` mappings on both `.dae` and `.cdae` export
- Fixed (#66): Exporting a selected child without its parent (`Selection Only`) now writes its world-space transform (or transform relative to its nearest exported ancestor) instead of its parent-local transform, for both static nodes and animations
- Fixed (#59): Importing with no file selected (a folder path) now reports `"No file selected"` instead of failing in the XML parser
- Fixed (#79): Updated the 8 regression tests from 1.5.7 to run under `--factory-startup` and register `io_scene_collada` directly from the checkout instead of testing an installed extension

## [1.5.7] - 2026-11-26

- Changed: License changed from GPL-2.0-or-later to GPL-3.0-or-later, as extensions.blender.org requires; allowed by the 'or later' clause of the original Blender code. Torque3D-derived data stays MIT. Shortened extension tagline to "Collada (.dae) and BeamNG (.cdae) import and export".
- Fixed (#39): export `FLOAT_COLOR` (linear → sRGB) and `POINT`-domain color attributes to `.dae`, warning on unsupported types/domains; on import, create `FLOAT_COLOR` (sRGB → linear) when a `COLOR` source has HDR values above `1.0` instead of clamping to `BYTE_COLOR`
- Fixed (#38): resolve skin controller joint SIDs/IDs within each controller's `<skeleton>` subtree before falling back to global lookup so two armatures sharing bone names bind to their own rigs
- Fixed (#42): keep sanitized bone IDs and SIDs unique in `GlobalIdMap` (e.g. `Bone L`, `Bone.L`, `Bone_L` → `Bone_L`, `Bone_L2`, `Bone_L3`) so bone names and vertex groups round-trip without collisions
- Fixed (#40, #41): evaluate each shape key separately through the depsgraph when `apply_modifiers` changes vertex count (Subsurf, Array, Mirror), and write `Basis + (key - relative_key)` for chained relative shape keys
- Added (#26): **Double-Sided Faces** import option (`Keep (separate vertices)` / `Merge into one face` / `Remove back faces`) consistent across `.dae` and `.cdae` import, exact-duplicate filtering on winding + material + UVs + colors + corner normals, and double-sided export (`double_sided = True` face attribute written twice with reversed winding and flipped normals in `.dae` and `.cdae`, including `Keep Normals` and `Keep Quads`)
- Fixed (#43): give linked duplicates with active modifiers distinct geometry IDs when `apply_modifiers` and `use_object_instantiation` are both enabled, while keeping unmodified linked duplicates shared
- Fixed (#47): warn when exporting skinned meshes to `.cdae` (StandardMesh-only) and temporarily switch referenced armatures to rest pose by default
- Fixed (#48): refuse collinear Forward/Up axes from Python (`CANCELLED` + error report) when `apply_global_orientation` is enabled, and auto-swap the other axis in the export UI
- Added (#50): register `.cdae` export (`bl_export_operator = "export_scene.beamng_cdae"`) on `IO_FH_beamng_cdae` with a hidden `collection` property so `.cdae` works as a Blender Collection Exporter
- Fixed (#44): export unassigned (fake-user) actions when **Include all Actions** is enabled by temporarily assigning each matching Blender 5 action/slot, sampling it, and restoring original `animation_data`
- Fixed (#45): realize Geometry Nodes instances from `depsgraph.object_instances` into the exported mesh copy when `apply_modifiers` is enabled (`.dae` and `.cdae`)
- Docs: added `docs/profiling.md` (`cProfile` breakdown on a 1M-tri mesh, 4,000-object scene, and BeamNG Covet `DXi_M` config import)
- CI (#6): added weekly/manual allow-failure workflow (`.github/workflows/daily-blender.yml`) testing against the latest Blender daily build

## [1.5.6] - 2026-11-26

- Added (#52): full BeamNG v1.5 PBR material import (`"version": 1.5`) with multi-layer (`activeLayers`) compositing, using upper-layer `opacityMap × opacityFactor` as layer masks over lower layers while preserving 1.5.5 base-colour behaviour for legacy materials
- Added (#52): palette-mask paint blending (`colorPaletteMap` R/G/B and `instanceDiffuse`) driven by per-object `beamng_paint1..3` custom properties (sRGB → linear) read via Object `Attribute` nodes, loaded from `.pc` `paints` or resolved via `defaultPaintName1..3` (`info_<config>.json` → `info.json` → `vehicles/common/paintLibraries/*.paintLibrary.json`)
- Added (#52): ambient occlusion, metallic, roughness, clearcoat, normal, emissive, and `UV0`/`UV1` naming on BeamNG PBR imports (`<map>UseUV: 1` binds to `UV1`)
- Added (#52): glass and `alphaTest` rules — only layer 0 controls transparency on `translucent`/`alphaTest` materials (solid parts stay opaque), translucent `glass`/`windshield` materials without an opacity source fall back to `0.35` (noted in the import report), and `alphaTest` materials use `DITHERED` with alpha clip instead of `BLENDED`
- Added (#52): light placeholder material resolution via jbeam `glowMap` (active config parts → vehicle zip → `common.zip`), keeping placeholder Blender material names for `.dae` export (`beamng_glow_source` custom property), with a **Lights: Off / On** import option and fully transparent rendering for `invis` dashboard warning lamps
- Improved (#52): slimmed per-layer PBR node graphs to build only the paint mixes, attributes, and maps each layer actually uses — cutting first EEVEE render time on the Covet `DXi_M` from about 4 minutes down to 8 seconds with identical Cycles bake results
- Added (#52): **BeamNG Materials: Full PBR / Base color only** import option (`Base color only` matches 1.5.5; plain `.dae`/`.zip` imports and 4.5.1 parity exports remain byte-identical)

## [1.5.5] - 2026-11-26

- Fixed (#51): BeamNG vehicle/config import now builds one shared material table from every `*.materials.json` in the vehicle archive/folder and `common.zip` (vehicle entries win on name clashes), resolves textures vehicle-first then `common.zip`, reuses materials created within the same import without creating `.001` duplicates (tagged with a per-import custom property so pre-existing user materials are never reused), and emits one summary warning per import listing textures missing from both sources. Plain `.dae` and `.zip` imports remain unchanged
- Refactored: split `import_collada.py` and `export_collada.py` into focused mixin modules under `io_scene_collada/importer/` (`xml_parse`, `archive`, `materials`, `mesh`, `rig`, `animation`, `scene`) and `io_scene_collada/exporter/` (`geometry`, `materials`, `rig`, `animation`, `scene`) while keeping `DocumentImporter` and `DocumentExporter` as thin entry points with byte-identical 4.5.1 parity output; added `.git-blame-ignore-revs` and `docs/architecture.md`
- Improved (#14): expanded `ruff` lint coverage (`E, F, W, B, UP`) across `io_scene_collada` and added `mypy --strict` type-checking in CI for `cdae/msgpack_lite.py` and `cdae/reader.py` via `pyproject.toml`

## [1.5.4] - 2026-11-26

- Fixed (#35): flush unsaved Edit Mode edits (`update_from_editmode()`) before `.dae` and `.cdae` export so changes made without leaving Edit Mode are exported
- Fixed (#36): export viewport-hidden objects (`H` / eye icon and `Disable in Viewports` / monitor icon) on `.dae` export, matching Blender 4.5.1 while still skipping excluded collections
- Fixed (#37): eliminate O(N²) export scaling on scenes with thousands of objects (`.dae` and `.cdae`) by batch-removing temporary meshes once per export, skipping unnecessary copies for unmodified read-only meshes, and building a single `parent → children` map (4,000 cubes: `13.76 s → 1.43 s` `.dae`, `10.49 s → 1.69 s` `.cdae`, byte-identical output)
- Fixed (#46): cap combined `random()` candidates at 256 with O(1) set deduplication and catch `RecursionError` in `beamng/expr.py`, and add a nesting depth guard (`MAX_NESTING_DEPTH = 256`) in `beamng/lenient_json.py`
- Fixed (#34): import non-JOINT child objects under armature bones with `parent_type = "BONE"` and `parent_bone` set after armatures are built, preserving exact world transforms

## [1.5.3] - 2026-11-26

- Fixed (#19): **Resolution → Render** (`export_mesh_type_selection='render'`) now actually evaluates modifiers at render settings (render levels and `show_render` visibility, non-destructively restored after export) on `.dae` export, and added the matching **Apply Modifiers + Resolution** (`Viewport` / `Render`) pair to `.cdae` export
- Docs: clarified in `docs/cdae_format.md` that pure `int16` `Quat16` quantization is at most `~0.0035°` and the extra up to `0.0396°` in paired verification comes from BeamNG's shipped game files themselves

## [1.5.2] - 2026-11-26

- Added (#18): **Keep Quads** export option (`keep_quads`, default `False`) under **Triangulate** on `.dae` export to triangulate n-gons only and preserve 4-sided quads in `<polylist>` (greyed out for `.cdae`)
- Improved (#12): deduplicate UV and vertex color sources on `.dae` export (cuts `big_mesh` `.dae` size from `8.96 MB` to `< 6 MB` with byte-exact round-trip and faster export/import)
- Fixed (#17): use Blender's viewport `mesh.loop_triangles` split on the default triangulated export path for `.dae` and `.cdae` (keeping `bmesh` `SHORT_EDGE` behind `keep_normals=False` for 4.5.1 parity), fixing opposite-winding co-vertex triangles on `monkey_sharp_edges`/`big_mesh` and cutting `nonplanar_quads` area error from `-0.32%` to `~0%`
- Fixed (#16): eliminate `ERROR: Image "" not available. Keeping packed image` stderr noise when importing packed archive textures on Blender 5.0+ by setting `img.filepath` before `img.pack()` and `img.source = "FILE"`
- Fixed (#27): respect `<accessor>` `offset` and `count` across mesh, skin controller, and animation source readers (including `Name_array` / `IDREF_array`), preventing shifted vertices and stray loose vertices
- Fixed (#28): tessellate `<polygons>/<ph>` hole outlines (`<h>`) using `mathutils.geometry.tessellate_polygon` while preserving per-corner normals, UVs, and vertex colors (with warning and outer-polygon fallback on degenerate holes)
- Fixed (#2): raise `MsgPackError` for unhashable MsgPack map keys and replace NumPy `DeprecationWarning` control flow in `.dae` array parsing with explicit token-count validation
- Improved (#3): harden archive and XML imports by capping zip archives at 50,000 entries and rejecting XML `<!DOCTYPE>` / `<!ENTITY>` declarations at the expat parser level
- Improved (#7, #8): factor operator warning/error capping into shared `report_capped()` helper and warn after `.dae`/`.cdae` import when the active target collection is excluded or hidden in the current view layer
- Improved (#9): make the "restart Blender after updating" requirement visible in extension metadata, add-on Preferences, and a one-time popup when an update is detected in-session
- Added (#22): automated `.dae` and `.cdae` round-trip accuracy CI gate (`tests/test_accuracy_roundtrip.py`) covering 11 synthetic scenes (all 22 `.dae`/`.cdae` checks passing strictly with 0 xfails)
- Improved (#4, #5): smoke-test native `fastfmt` binaries on Windows, macOS, and Linux in CI (`tests/smoke_fastfmt.py`), pin all GitHub Actions to commit SHAs with Dependabot, verify downloaded Blender tarballs with `sha256sum`, and publish `SHA256SUMS.txt` with release artifacts

### Details
- **Parser & archive hardening** (`cdae/msgpack_lite.py`, `import_collada.py`, `beamng/source.py`): reject unhashable MsgPack map keys (`list`, `dict`, etc.) with `MsgPackError` instead of `TypeError`; replace `warnings.simplefilter("error", DeprecationWarning)` around `np.fromstring` with explicit token-count and last-token validation (`_fromstring_f32_strict` / `_fromstring_i32_strict`) that behaves identically on NumPy 1.26 and 2.x; enforce a 50,000-entry limit on `.zip`/`.zae`/`.kmz` archives in both `DocumentImporter` and `VehicleSource`; reject `<!DOCTYPE>` / `<!ENTITY>` declarations before `ET.parse` in `.dae` and archive `manifest.xml` files.
- **Keep Quads export option** (`collada_utils.py`, `export_collada.py`, `__init__.py`): added `keep_quads: BoolProperty(default=False)` to `wm.collada_export` (under **Triangulate**, greyed out when **Triangulate** is off) and greyed out on `export_scene.beamng_cdae` (Torque3D requires triangles). When enabled with **Triangulate**, `get_mesh_copy()` saves pre-triangulation corner normals (when **Keep Normals** is on) and triangulates only faces with more than 4 vertices (`len(f.verts) > 4`), writing mixed 3- and 4-sided `<polylist>` entries. Note: non-planar quads are split by whichever program opens the file, so shading may differ slightly.
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
