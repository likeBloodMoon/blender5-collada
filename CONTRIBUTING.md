# Contributing

Contributions, bug reports, and real-world `.dae` / `.cdae` test cases are welcome!

## Project Structure

- `io_scene_collada/` — Blender 5.0+ extension package (`GPL-3.0-or-later`):
  - `blender_manifest.toml` & `LICENSE` — Extension manifest and license text
  - `__init__.py` — Operator registration (`wm.collada_import`, `wm.collada_export`, `import_scene.beamng_cdae`, `export_scene.beamng_cdae`), UI panels, and FileHandlers (`IO_FH_collada`, `IO_FH_beamng_cdae`)
  - `import_collada.py` & `importer/` — COLLADA 1.4.1 importer split into focused mixins (`xml_parse.py`, `archive.py`, `materials.py`, `mesh.py`, `rig.py`, `animation.py`, `scene.py`)
  - `export_collada.py` & `exporter/` — COLLADA 1.4.1 exporter split into focused mixins (`geometry.py`, `materials.py`, `rig.py`, `animation.py`, `scene.py`)
  - `cdae/` — BeamNG Cached Collada (`.cdae` v31) reader, importer, exporter, MsgPack codec, and Torque3D normal table
  - `beamng/` & `beamng_materials.py` — BeamNG vehicle archive/folder scanner, `.pc` / `.jbeam` part & `glowMap` resolver, Full PBR v1.5 material builder (`pbr_nodes.py`, `materials_ctx.py`), and interactive picker
  - `collada_utils.py`, `xml_writer.py` — Shared math/ID/mesh/duplicate-face utilities and OpenCOLLADA-compatible XML writer
- `docs/` — Architecture map ([`docs/architecture.md`](docs/architecture.md)), `.cdae` format notes ([`docs/cdae_format.md`](docs/cdae_format.md)), and profiling report ([`docs/profiling.md`](docs/profiling.md))
- `tests/` — Automated parity, accuracy, fuzz, malformed-input, archive, `.cdae`, BeamNG, and feature regression test suites:
  - `run_parity_tests.py` — Export byte-parity + scene import parity against Blender 4.5.1 LTS (28 checks)
  - `run_deep_verification.py` — Per-element import diff, cross-version round-trips, import flags, third-party `.dae` fixtures (33 checks)
  - `test_accuracy_roundtrip.py` — Geometry & normal round-trip accuracy CI gate (`.dae` and `.cdae`)
  - `test_malformed.py`, `fuzz_import.py`, `test_archives.py`, `test_cdae_*.py`, `test_beamng_*.py`, and per-issue regression tests

## Local Development

1. Lint and type-check:
   ```bash
   ruff check io_scene_collada
   mypy --strict io_scene_collada/cdae/msgpack_lite.py io_scene_collada/cdae/reader.py
   ```
2. Validate and build the extension:
   ```bash
   blender --command extension validate io_scene_collada
   blender --command extension build --source-dir io_scene_collada --output-dir dist
   ```
3. Install into your Blender 5.x environment (restart Blender after installing a new `.zip` over an existing version so Python reloads all submodules):
   ```bash
   blender --command extension install-file -r user_default -e dist/collada_io-1.5.8.zip
   ```
4. Run parity and regression tests (parity suites require portable Blender 4.5.1 and 5.x in `.tools/`):
   ```bash
   python tests/run_parity_tests.py
   python tests/run_deep_verification.py
   python tests/test_accuracy_roundtrip.py
   ```

## Reporting Issues

When reporting an import or export discrepancy, please include:
- A minimal `.dae`, `.cdae`, or `.blend` file reproducing the issue (synthetic or permissively licensed — please don't upload proprietary game assets)
- Your add-on version and Blender version (e.g., `5.0.1`, `5.3.0`)
- How Blender 4.5.1 LTS handles the same file (if applicable)
