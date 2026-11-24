"""Read-only scanner for all .cdae files across a local BeamNG.drive installation.

Walks BEAMNG_DIR/content and BEAMNG_DIR/levels (plus any loose .cdae files under
BEAMNG_DIR), opens every .zip IN PLACE without extracting anything to disk, parses
every .cdae with io_scene_collada.cdae.reader.read_cdae_bytes, and writes a CSV
report to tests/_local_cdae/beamng_cdae_scan.csv (git-ignored).

Usage:
    BEAMNG_DIR="C:\\Program Files (x86)\\Steam\\steamapps\\common\\BeamNG.drive" \\
        .tools\\blender-5.3.0-windows-x64\\blender.exe -b --factory-startup --python tests/scan_beamng_install.py

Or with any Python environment that has numpy + zstandard installed:
    BEAMNG_DIR="/path/to/BeamNG.drive" python tests/scan_beamng_install.py
"""

from __future__ import annotations

import csv
import importlib.util
import os
import sys
import time
import zipfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_cdae_reader():
    """Load reader.py directly without requiring bpy."""
    cdae_dir = os.path.join(REPO_ROOT, "io_scene_collada", "cdae")
    # Create lightweight package shims so relative imports inside cdae/ work without bpy
    import types

    pkg_root = types.ModuleType("io_scene_collada")
    pkg_root.__path__ = [os.path.join(REPO_ROOT, "io_scene_collada")]
    sys.modules.setdefault("io_scene_collada", pkg_root)

    pkg_cdae = types.ModuleType("io_scene_collada.cdae")
    pkg_cdae.__path__ = [cdae_dir]
    sys.modules.setdefault("io_scene_collada.cdae", pkg_cdae)

    for mod_name in ("msgpack_lite", "normal_table", "reader"):
        full = f"io_scene_collada.cdae.{mod_name}"
        spec = importlib.util.spec_from_file_location(full, os.path.join(cdae_dir, f"{mod_name}.py"))
        mod = importlib.util.module_from_spec(spec)
        sys.modules[full] = mod
        spec.loader.exec_module(mod)
    return sys.modules["io_scene_collada.cdae.reader"]


def _parse_one(reader_mod, source_label: str, raw: bytes) -> dict:
    t0 = time.perf_counter()
    row = {
        "source": source_label,
        "size_bytes": len(raw),
        "version": "",
        "exporter_version": "",
        "compressed": "",
        "parse_ms": 0.0,
        "num_meshes": 0,
        "type_0_standard": 0,
        "type_1_skin": 0,
        "type_2_decal": 0,
        "type_3_sorted": 0,
        "type_4_null": 0,
        "type_other": 0,
        "parent_mesh_ge_0": 0,
        "encoded_norms_meshes": 0,
        "num_details": 0,
        "num_sequences": 0,
        "num_materials": 0,
        "warnings": 0,
        "error": "",
    }
    try:
        shape = reader_mod.read_cdae_bytes(raw)
        row["version"] = shape.shape_version
        row["exporter_version"] = shape.exporter_version
        row["compressed"] = bool(shape.header_info.get("compression", False))
        row["num_meshes"] = len(shape.meshes)
        row["num_details"] = len(shape.details)
        row["num_sequences"] = shape.num_sequences
        row["num_materials"] = len(shape.materials)
        row["warnings"] = len(shape.warnings)
        for m in shape.meshes:
            mt = m.mesh_type
            if mt == 0:
                row["type_0_standard"] += 1
            elif mt == 1:
                row["type_1_skin"] += 1
            elif mt == 2:
                row["type_2_decal"] += 1
            elif mt == 3:
                row["type_3_sorted"] += 1
            elif mt == 4:
                row["type_4_null"] += 1
            else:
                row["type_other"] += 1
            if mt != 4:
                if m.parent_mesh >= 0:
                    row["parent_mesh_ge_0"] += 1
                if len(m.encoded_norms) > 0:
                    row["encoded_norms_meshes"] += 1
    except Exception as exc:
        row["error"] = f"{type(exc).__name__}: {exc}"
    row["parse_ms"] = round((time.perf_counter() - t0) * 1000.0, 2)
    return row


def main() -> int:
    # Accept BEAMNG_DIR env var or CLI arg (handling Blender's '--' separator)
    argv = sys.argv
    if "--" in argv:
        cli_args = argv[argv.index("--") + 1 :]
    elif os.path.basename(argv[0]).lower().startswith("blender"):
        cli_args = []
    else:
        cli_args = argv[1:]

    beamng_dir = cli_args[0] if cli_args else os.environ.get("BEAMNG_DIR", "")
    if not beamng_dir or not os.path.isdir(beamng_dir):
        print("ERROR: Set BEAMNG_DIR to a valid BeamNG.drive installation directory (or pass as CLI argument).")
        return 1

    reader_mod = _load_cdae_reader()
    scan_roots = []
    for sub in ("content", "levels"):
        p = os.path.join(beamng_dir, sub)
        if os.path.isdir(p):
            scan_roots.append(p)
    if not scan_roots:
        scan_roots = [beamng_dir]

    rows: list[dict] = []
    t_start = time.perf_counter()
    zip_count = 0

    for root_dir in scan_roots:
        for dirpath, _, filenames in os.walk(root_dir):
            for fn in sorted(filenames):
                low = fn.lower()
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, beamng_dir)
                if low.endswith(".cdae"):
                    try:
                        with open(full, "rb") as f:
                            raw = f.read()
                        rows.append(_parse_one(reader_mod, rel, raw))
                    except OSError as exc:
                        rows.append({"source": rel, "error": f"OSError: {exc}"})
                elif low.endswith(".zip"):
                    zip_count += 1
                    try:
                        with zipfile.ZipFile(full, "r") as zf:
                            for info in sorted(zf.infolist(), key=lambda i: i.filename):
                                if info.is_dir() or not info.filename.lower().endswith(".cdae"):
                                    continue
                                label = f"{rel}::{info.filename}"
                                try:
                                    raw = zf.read(info)
                                    rows.append(_parse_one(reader_mod, label, raw))
                                except Exception as exc:
                                    rows.append({"source": label, "error": f"{type(exc).__name__}: {exc}"})
                    except Exception as exc:
                        print(f"  WARN: could not open zip {rel}: {exc}", flush=True)

    total_s = time.perf_counter() - t_start
    out_dir = os.path.join(REPO_ROOT, "tests", "_local_cdae")
    os.makedirs(out_dir, exist_ok=True)
    out_csv = os.path.join(out_dir, "beamng_cdae_scan.csv")

    fieldnames = [
        "source",
        "size_bytes",
        "version",
        "exporter_version",
        "compressed",
        "parse_ms",
        "num_meshes",
        "type_0_standard",
        "type_1_skin",
        "type_2_decal",
        "type_3_sorted",
        "type_4_null",
        "type_other",
        "parent_mesh_ge_0",
        "encoded_norms_meshes",
        "num_details",
        "num_sequences",
        "num_materials",
        "warnings",
        "error",
    ]
    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            writer.writerow({k: r.get(k, "") for k in fieldnames})

    errors = [r for r in rows if r.get("error")]
    skins = [r for r in rows if r.get("type_1_skin", 0) > 0]
    sorted_m = [r for r in rows if r.get("type_3_sorted", 0) > 0]
    parents = [r for r in rows if r.get("parent_mesh_ge_0", 0) > 0]
    seqs = [r for r in rows if r.get("num_sequences", 0) > 0]
    enc = [r for r in rows if r.get("encoded_norms_meshes", 0) > 0]

    print(f"\nScanned {zip_count} zips, parsed {len(rows)} .cdae files in {total_s:.2f}s")
    print(f"CSV report written to: {out_csv}")
    print(f"  Parse errors:       {len(errors)}")
    print(f"  SkinMesh (type=1):  {len(skins)} files")
    print(f"  SortedMesh (type=3):{len(sorted_m)} files")
    print(f"  parentMesh >= 0:    {len(parents)} files")
    print(f"  sequences > 0:      {len(seqs)} files")
    print(f"  encodedNorms > 0:   {len(enc)} files")

    for title, subset in (
        ("Sample SkinMesh files", skins),
        ("Sample SortedMesh files", sorted_m),
        ("Sample parentMesh>=0 files", parents),
        ("Sample animated (sequences>0) files", seqs),
        ("Sample encodedNorms files", enc),
        ("Parse errors", errors),
    ):
        if subset:
            print(f"\n{title} (first 10):")
            for r in subset[:10]:
                print(
                    f"  {r['source']} | skin={r.get('type_1_skin',0)} sorted={r.get('type_3_sorted',0)} "
                    f"parent={r.get('parent_mesh_ge_0',0)} seq={r.get('num_sequences',0)} "
                    f"enc={r.get('encoded_norms_meshes',0)} err={r.get('error','')}"
                )

    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
