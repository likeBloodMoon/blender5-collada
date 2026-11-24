# Collada (DAE) for Blender 5.0+

[![Build & Parity Tests](https://github.com/likeBloodMoon/blender5-collada/actions/workflows/build.yml/badge.svg)](https://github.com/likeBloodMoon/blender5-collada/actions/workflows/build.yml)
[![License: GPL v2+](https://img.shields.io/badge/License-GPLv2%2B-blue.svg)](LICENSE)
[![Blender 5.0+](https://img.shields.io/badge/Blender-5.0%2B-orange.svg)](https://www.blender.org)

Blender 5.0 removed built-in COLLADA (`.dae`) support, which broke a lot of game modding and legacy engine pipelines (BeamNG.drive, Second Life / OpenSim, Torque3D, Amnesia, etc.).

This extension brings back the full Blender 4.5.1 COLLADA importer and exporter for **Blender 5.0+** in pure Python — no external wheels or dependencies, and using the exact same `wm.collada_import` / `wm.collada_export` operators so existing scripts and workflows keep working.

---

## Install

1. Grab the zip for your OS from [**Releases**](https://github.com/likeBloodMoon/blender5-collada/releases) (e.g., `collada_io-1.4.2-windows_x64.zip`), or `collada_io-1.4.2-pure-python.zip` if you prefer no compiled binary.
2. Drag and drop the `.zip` onto Blender, **or** go to **Edit → Preferences → Get Extensions → ⌄ → Install from Disk…** and pick the `.zip`.
3. **Updating**: After installing a new version over an existing one, restart Blender. Blender keeps the old add-on code loaded until restart.

CLI install:
```bash
blender --command extension install-file -r user_default -e collada_io-1.4.2-windows_x64.zip
```

### About the optional native binary (`fastfmt.dll` / `libfastfmt.so` / `libfastfmt.dylib`)

The platform-specific release zips include a small, optional C++17 helper library (`io_scene_collada/_native/fastfmt.dll` on Windows) that speeds up formatting and parsing large mesh number arrays (`<float_array>` and `<p>`).

- **100% open source & auditable**: Built directly from the ~125-line [`native/fastfmt.cpp`](native/fastfmt.cpp) in this repo — it only converts float/int arrays to and from text in memory (no file I/O, no networking, no system calls).
- **Built transparently by GitHub Actions**: Every release binary is compiled on GitHub's CI runners directly from this repository (see the **Actions** tab for full build logs).
- **Completely optional**: Don't want a `.dll` at all? Download **`collada_io-*-pure-python.zip`** instead — it contains only `.py` files and produces byte-identical output. You can also force the pure-Python path at any time by setting `COLLADA_NATIVE=0`.

---

## Usage

- **Menus**: **File → Import → Collada (Default) (.dae, .zae, .kmz)** and **File → Export → Collada (Default) (.dae)**
- **Drag & Drop**: Drop `.dae`, `.zae`, or `.kmz` files straight into the viewport.
- **Python**:
  ```python
  import bpy

  bpy.ops.wm.collada_import(filepath="/path/to/model.dae")
  bpy.ops.wm.collada_export(filepath="/path/to/output.dae", triangulate=True)
  ```

### Tip: Clean Sharp Edges on Import

Blender's old Collada importer often leaves meshes with missing sharp edges (where a flat triangle sits next to a smooth face) and random cyan edges from float rounding noise.

If you're importing game meshes (like car bodies) and want clean, editable sharp edges, turn on **Mark Sharp from Normals** under **Import Data Options** and adjust **Sharp Threshold** (default `1.0°`).

| Default Import (missed creases & stray sharp edges) | With **Mark Sharp from Normals** |
| :---: | :---: |
| ![Before: missed sharp edges on grille](docs/images/sharp_before_missed.jpg) | ![After: clean sharp edges on grille](docs/images/sharp_after_grille.jpg) |

Automatic sharp-edge reconstruction on a vehicle fender imported from `.dae` (louvers, headlight recess, wheel-arch lip, and panel seams):

![Imported vehicle fender with clean sharp edges](docs/images/sharp_after_fender.jpg)

---

## What's Supported

- **Geometry**: `<triangles>`, `<polylist>`, `<polygons>` (including `<ph>`), `<trifans>`, `<tristrips>`, `<lines>`, n-gons, custom split normals, multiple UV maps, byte vertex colors, modifiers, and instancing.
- **Materials & Textures**: Principled BSDF, `<lambert>` / `<phong>` / `<blinn>` / `<constant>`, texture copying on export, and automatic fallback lookup for relative/subfolder textures (`textures/`, `images/`, `maps/`) on import.
- **Rigs & Morphs**: Armatures, skinning (`<skin>`, vertex weights, `<bind_shape_matrix>`, `Name_array` and `IDREF_array`), bone collections, shape keys (`<morph>`), OpenSim export, and armature import options (`auto_connect`, `find_chains`, `fix_orientation`, `keep_bind_info`).
- **Animation**: Object and pose-bone animations (sampled matrices, decomposed transforms, and keyframe curves) using Blender 5.0+'s layered Action / Slot / Channelbag API.
- **Cameras & Lights**: Perspective/ortho cameras and Point, Sun, Spot, and Area lights with Blender `<extra>` profile data.
- **Archives**: Direct import of `.zae`, `.kmz`, and `.zip` archives with `manifest.xml` lookup and automatic texture packing.
- **Performance**: Mesh export and import use `foreach_get` / `foreach_set` with NumPy (~4.2 s export / ~7.0 s import on a 500k-triangle mesh).

---

## BeamNG `.cdae` (v31)

In addition to `.dae`, the extension includes native **BeamNG Cached Collada (`.cdae`)** support (format **version 31 only**; legacy v26 and other versions are rejected with a clear error):

- **Import** (`import_scene.beamng_cdae` — **File → Import → BeamNG Cached Collada (.cdae)**, plus drag-and-drop):
  - Pure-Python bounds-checked MsgPack reader (`io_scene_collada/cdae/msgpack_lite.py`), optional Zstandard decompression (uses Blender's bundled `zstandard` at runtime, reporting a clear error if unavailable), strict `elemSize` validation, and `me.validate()` mesh safety.
  - Supports `StandardMesh` and `SkinMesh` (armature + vertex groups + Armature modifier), node hierarchies (`Quat16`), `Triangles` / `Strip` / `Fan` primitives, `UV0` / `UV1` (V-flipped), sRGB `ColorI` vertex colors, explicit or encoded normals, material name preservation (`main.materials.json`), LOD selection (**Highest detail only** or **All detail levels** into per-detail collections), and optional collision meshes.
  - **Normals** mode (`normals_mode`):
    - **Exact (keep game normals)** *(default)*: welds GPU-split vertices by position (preserving double-sided/layered faces, UV seams, and corner colors), writes exact game normals to Blender's `custom_normal` attribute (`0.0000°` median / `0.0280°` p99 round-trip error), and also marks hard edges as `sharp_edge` so clearing custom normals later still leaves crisp creases. Note: custom normals do not auto-update if you reshape the mesh.
    - **Sharp edges (easier to edit)**: welds vertices and converts hard creases into `sharp_edge` markers so shading updates automatically as you edit (recomputed geometry normals can differ from game shading on curved panels).
    - **Unmerged (raw GPU vertices)**: keeps raw GPU-split vertices and exact normals for debugging or byte-faithful round-trips.
  - Animation sequences are skipped with an info notice.
- **Export [Experimental]** (`export_scene.beamng_cdae` — **File → Export → BeamNG Cached Collada (.cdae) [Experimental]**):
  - Currently scoped to **`StandardMesh` only** (skinned export waits on in-game verification) and labeled `[Experimental]` until in-game loading is confirmed across all test cases (see [`docs/cdae_manual_test.md`](docs/cdae_manual_test.md)).
  - Note: BeamNG.drive normally regenerates `.cdae` files automatically from `.dae` on first load, so `.dae` remains the recommended production export format for BeamNG mods.
- **Known limitations**:
  - `.cdae` **v31 only** (BeamNG.drive `0.40.x`); older versions (such as legacy v26) are rejected with a clear message.
  - Animations (sequences) are not imported yet; meshes, armatures, and skin weights are.
  - Export is **`[Experimental]`**: static meshes (`StandardMesh`) only, not yet tested in-game.
  - Tested by parsing all **6,319 `.cdae` files** shipped with BeamNG.drive `0.40.6.0` (0 parse errors).
- **Verification**:
  - **Tested against all 6,319 v31 `.cdae` files shipped with BeamNG.drive `0.40.6.0`** (`172` archives scanned read-only in `16.0 s`: `6,319 / 6,319` v31 files parsed cleanly with 0 errors; `39 / 39` animated files import cleanly; `2` legacy v26 files rejected cleanly).
  - **Paired `.dae` vs `.cdae` verification** (`wigeon_bigwing`, `wigeon`, `midtruck` — `396` objects): `0.000000 m` location error, `max 0.0396°` `Quat16` rotation error, `0.000000` median UV0/UV1 error, `≤ 1/255` sRGB vertex color diff, `100.00%` per-face material match.
  - **Export round-trip oracle** (`import → export → compare_cdae.py` on `wigeon_bigwing`, `wigeon`, `midtruck`): `0.000000` diff on all shape scalars, all 19 shape vectors, and all `396/396` mesh bounds/centers/radii/UVs/materials; default **Exact** mode achieves `0.0000°` median / `0.0280°` p99 / `≤ 0.1545°` max normal delta across `646,632` corners (`Unmerged`: `0.0000°` median / `0.0280°` p99 / `≤ 0.0442°` max).
  - Real game files are never committed to the repository; CI runs synthetic unit, round-trip, and fuzz tests only. See [`docs/cdae_format.md`](docs/cdae_format.md) and [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).

---

## Parity Testing

Every push and PR is tested in CI against **Blender 4.5.1 LTS** (on both **Blender 5.0.1** and **5.3.0**) across 61 automated checks:

- **Export byte-parity** (`tests/run_parity_tests.py`): 19 export configs checked for identical XML structure and float32 precision (`max Δ ≤ 1e-5`), verified byte-identical with both native and pure-Python fallback.
- **Deep import & round-trip** (`tests/run_deep_verification.py`): per-vertex/normal/UV/weight/keyframe comparisons, `5.3 → 4.5.1` cross-version imports, `import → re-export` round-trips, import flag matrix, and third-party `Y_UP` / `.zae` / `<tristrips>` fixtures.

---

## Building from Source

```bash
blender --command extension validate io_scene_collada
blender --command extension build --source-dir io_scene_collada --output-dir dist
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for dev setup and [CHANGELOG.md](CHANGELOG.md) for release notes.

## License

GPL-2.0-or-later — see [LICENSE](LICENSE). Based on Blender 4.5.1 LTS Collada I/O.
