# Collada (DAE) for Blender 5.0+

[![Build & Parity Tests](https://github.com/likeBloodMoon/blender5-collada/actions/workflows/build.yml/badge.svg)](https://github.com/likeBloodMoon/blender5-collada/actions/workflows/build.yml)
[![License: GPL v2+](https://img.shields.io/badge/License-GPLv2%2B-blue.svg)](LICENSE)
[![Blender 5.0+](https://img.shields.io/badge/Blender-5.0%2B-orange.svg)](https://www.blender.org)

Blender 5.0 removed built-in COLLADA (`.dae`) support, which broke a lot of game modding and legacy engine pipelines (BeamNG.drive, Second Life / OpenSim, Torque3D, Amnesia, etc.).

This extension brings back the full Blender 4.5.1 COLLADA importer and exporter for **Blender 5.0+** in pure Python — no external wheels or dependencies, and using the exact same `wm.collada_import` / `wm.collada_export` operators so existing scripts and workflows keep working.

---

## Install

1. Grab the latest `collada_io-*.zip` from [**Releases**](https://github.com/likeBloodMoon/blender5-collada/releases).
2. Drag and drop the `.zip` onto Blender, **or** go to **Edit → Preferences → Get Extensions → ⌄ → Install from Disk…** and pick the `.zip`.

CLI install:
```bash
blender --command extension install-file -r user_default -e collada_io-1.1.0.zip
```

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

## Parity Testing

Every push and PR is tested in CI against **Blender 4.5.1 LTS** across 55 automated checks:

- **Export byte-parity** (`tests/run_parity_tests.py`): 16 export configs checked for identical XML structure and float32 precision (`max Δ ≤ 1e-5`).
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
