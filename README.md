# Blender Collada (DAE) Import/Export for Blender 5.0+

[![Build & Parity Tests](https://github.com/likeBloodMoon/blender5-collada/actions/workflows/build.yml/badge.svg)](https://github.com/likeBloodMoon/blender5-collada/actions/workflows/build.yml)
[![License: GPL v2+](https://img.shields.io/badge/License-GPLv2%2B-blue.svg)](LICENSE)
[![Blender 5.0+](https://img.shields.io/badge/Blender-5.0%2B-orange.svg)](https://www.blender.org)

A drop-in **Blender 5.0+ Extension** that restores full **COLLADA 1.4.1 (`.dae`)** import and export support after its removal in Blender 5.0, ported directly from **Blender 4.5.1 LTS** with verified byte-for-byte export parity and deep per-element import parity.

## Why This Exists

Blender 5.0 dropped built-in COLLADA (`.dae`) support, breaking pipelines for many video games, modding communities, and legacy 3D engines (e.g., BeamNG.drive, Second Life / OpenSim, Amnesia/HPL, Torque3D, Source/GoldSrc tools, Godot/WebGL workflows, etc.).

This extension implements the complete Blender 4.5.1 COLLADA importer and exporter in pure Python with zero external dependencies, updated for Blender 5.0+ APIs (layered Actions / Slots / Channelbags, `Mesh.corner_normals`, `bpy.types.FileHandler`, and the Blender Extension Manifest format).

## Features

- **Drop-in Operator Compatibility**: Registers the original `bpy.ops.wm.collada_import` and `bpy.ops.wm.collada_export` operators with all Blender 4.5.1 properties, defaults, and tabbed UI — existing scripts and pipelines work without modification.
- **Drag-and-Drop Support**: Registers `IO_FH_collada` (`bpy.types.FileHandler`) so `.dae` files can be dragged directly into the Blender viewport.
- **Meshes & Geometry**: Polylists, triangulated meshes (`SHORT_EDGE` / `EAR_CLIP`), n-gons, loose edges, custom split normals, multi-UV maps (`active_uv_only`), byte vertex colors, modifier application, and geometry instancing.
- **Materials & Textures**: Principled BSDF import/export, `<lambert>` / `<phong>` / `<blinn>` / `<constant>`, emission, IOR, alpha/transparency, diffuse/emission/normal textures, and texture copy export (`use_texture_copies`).
- **Cameras & Lights**: Perspective & orthographic cameras; Point, Sun, Spot, and Area lights with full Blender `<extra>` profile DNA preservation.
- **Armatures, Skinning & Morphing**: Skeleton hierarchies, vertex weights, `<bind_shape_matrix>`, `Name_array` and `IDREF_array` joints, bone collections, shape keys (`<morph>`), OpenSim / Second Life compatibility (`open_sim`, `deform_bones_only`), and armature import flags (`auto_connect`, `find_chains`, `fix_orientation`, `keep_bind_info`).
- **Animations**: Object and pose-bone animations (matrix sampled, decomposed sampled, and keyframe curves with smooth tangents), fully ported to Blender 5.0+'s layered Action / Slot / Channelbag system.
- **Coordinate Systems & Units**: `Z_UP` / `Y_UP` / `X_UP` axis conversion, global orientation export, and `import_units` scaling.

## Installation

### Option 1: Install from Release `.zip` (Recommended)
1. Download the latest `collada_io-*.zip` from the **Releases** page.
2. Open Blender 5.0+.
3. Go to **Edit → Preferences → Get Extensions** → click the top-right dropdown (`⌄`) → **Install from Disk…** and select `collada_io-*.zip` (or simply drag-and-drop the `.zip` onto the Blender window).

### Option 2: Install via Blender CLI
```bash
blender --command extension install-file -r user_default -e collada_io-4.5.1.zip
```

## Usage

- **UI**: **File → Import → Collada (Default) (.dae)** and **File → Export → Collada (Default) (.dae)**, or drag-and-drop a `.dae` file into Blender.
- **Python API** (identical to Blender 4.5.1):
  ```python
  import bpy

  bpy.ops.wm.collada_import(filepath="/path/to/model.dae")
  bpy.ops.wm.collada_export(filepath="/path/to/output.dae", triangulate=True)
  ```

## Parity Verification

This port is verified directly against **Blender 4.5.1 LTS** across **52 automated checks** (`tests/run_parity_tests.py` and `tests/run_deep_verification.py`):

| Test Suite | Coverage | Result |
| --- | --- | --- |
| **Export Byte-Parity** | 16 scenes/flag combos (primitives, multi-UV, vertex colors, modifiers, cameras, lights, materials, hierarchies, global orientation, armatures, skinning, shape keys, OpenSim, matrix/decomposed/curve animations) | **16 / 16 PASS** (exact XML structure, max float32 Δ ≤ `1e-5`) |
| **Import Scene Parity** | 9 full-scene imports compared against Blender 4.5.1 | **9 / 9 PASS** |
| **Deep Per-Element Import Parity** | Every vertex, corner normal, loop UV, color attribute, skin weight, shape key delta, BSDF socket/texture, light/camera param, bone matrix, and multi-frame animation | **9 / 9 PASS** |
| **Cross-Version Round-Trip** | `5.3 export → 4.5.1 import` & `4.5.1 → 5.3 → 5.3` vs `4.5.1 → 4.5.1 → 4.5.1` | **10 / 10 PASS** |
| **Import Flag Matrix** | `custom_normals=False`, `auto_connect`, `find_chains`, `fix_orientation`, `keep_bind_info` | **5 / 5 PASS** |
| **Third-Party `.dae` Stress Tests** | Non-Blender `Y_UP`, centimeter units (`import_units`), n-gon `<polylist>`, `<phong>`, non-identity `<bind_shape_matrix>` + `IDREF_array` | **3 / 3 PASS** |

## Building from Source

Requires Blender 5.0+:

```bash
blender --command extension validate io_scene_collada
blender --command extension build --source-dir io_scene_collada --output-dir dist
```

## License

Licensed under **GPL-2.0-or-later**, matching Blender 4.5.1 LTS. See [LICENSE](LICENSE) for details.
