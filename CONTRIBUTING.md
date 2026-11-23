# Contributing

Contributions, bug reports, and real-world `.dae` test cases are welcome!

## Project Structure

- `io_scene_collada/` — Blender 5.0+ extension package:
  - `blender_manifest.toml` — Extension manifest
  - `__init__.py` — Operator registration (`wm.collada_import`, `wm.collada_export`), UI panels, menu hooks, and `IO_FH_collada` FileHandler
  - `export_collada.py` — COLLADA 1.4.1 exporter
  - `import_collada.py` — COLLADA 1.4.1 importer
  - `collada_utils.py` — ID translation, matrix/axis utilities, Blender 5.0+ Action/Channelbag helpers
  - `xml_writer.py` — OpenCOLLADA-compatible XML serializer
- `tests/` — Automated parity test suites comparing Blender 5.0+ against Blender 4.5.1 LTS:
  - `run_parity_tests.py` — Export byte-parity + scene import parity (25 checks)
  - `run_deep_verification.py` — Per-element import diff, cross-version round-trips, import flags, third-party `.dae` fixtures (27 checks)

## Local Development

1. Validate and build the extension:
   ```bash
   blender --command extension validate io_scene_collada
   blender --command extension build --source-dir io_scene_collada --output-dir dist
   ```
2. Install into your Blender 5.x environment:
   ```bash
   blender --command extension install-file -r user_default -e dist/collada_io-1.1.0.zip
   ```
3. Run parity tests (requires portable Blender 4.5.1 and 5.x in `.tools/`):
   ```bash
   python tests/run_parity_tests.py
   python tests/run_deep_verification.py
   ```

## Reporting Issues

When reporting an import or export discrepancy, please include:
- A minimal `.dae` or `.blend` file reproducing the issue
- Your Blender version (e.g., `5.0.0`, `5.3.0`)
- How Blender 4.5.1 LTS handles the same file (if applicable)
