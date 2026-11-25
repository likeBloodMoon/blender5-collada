"""Real-data BeamNG resolution check over all 1,767 configs (Step 3, local only).

Reads C:\\Steam\\steamapps\\common\\BeamNG.drive\\content\\vehicles strictly read-only.
Skips cleanly when the game folder is not present (e.g. in CI).
Never commits any output; writes summary JSON to tests/_local_beamng/.

Usage:
    python tests/run_beamng_real.py
    BEAMNG_VEHICLES_DIR=... python tests/run_beamng_real.py
"""

from __future__ import annotations

import collections
import importlib.util
import json
import os
import sys
import time
import types

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_VEHICLES_DIR = r"C:\Steam\steamapps\common\BeamNG.drive\content\vehicles"


def _import_beamng_no_bpy():
    if "io_scene_collada.beamng" in sys.modules:
        return sys.modules["io_scene_collada.beamng"]
    pkg_root = types.ModuleType("io_scene_collada")
    pkg_root.__path__ = [os.path.join(REPO_ROOT, "io_scene_collada")]
    sys.modules["io_scene_collada"] = pkg_root
    beamng_dir = os.path.join(REPO_ROOT, "io_scene_collada", "beamng")
    spec = importlib.util.spec_from_file_location(
        "io_scene_collada.beamng",
        os.path.join(beamng_dir, "__init__.py"),
        submodule_search_locations=[beamng_dir],
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["io_scene_collada.beamng"] = mod
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    vdir = os.environ.get("BEAMNG_VEHICLES_DIR") or DEFAULT_VEHICLES_DIR
    if not os.path.isdir(vdir):
        print(f"SKIP: BeamNG vehicles directory not found: {vdir}")
        return 0

    beamng = _import_beamng_no_bpy()
    t0 = time.perf_counter()

    zips = sorted(f for f in os.listdir(vdir) if f.lower().endswith(".zip"))
    sources = {}
    for zname in zips:
        sources[zname] = beamng.load_vehicle_source(os.path.join(vdir, zname), index_dae_nodes=True)
    t_scan = time.perf_counter() - t0

    common_src = sources.get("common.zip")
    common_parts = common_src.part_index.parts if common_src is not None else {}
    common_meshes = common_src.all_dae_mesh_names() if common_src is not None else set()

    # Cross-vehicle mesh index across all zips
    all_mesh_owners: dict[str, set[str]] = collections.defaultdict(set)
    for zname, src in sources.items():
        for nm in src.all_dae_mesh_names():
            all_mesh_owners[nm].add(zname)

    agg = collections.Counter()
    mesh_counts = collections.Counter()
    raw_non_expr_counts = collections.Counter()
    exceptions: list[str] = []

    t1 = time.perf_counter()
    for zname, src in sorted(sources.items()):
        agg["zips"] += 1
        agg["jbeam_ok"] += src.part_index.jbeam_ok
        agg["jbeam_fail"] += len(src.part_index.jbeam_fail)
        agg["parts"] += len(src.part_index.parts)
        agg["dae"] += len(src.dae_models)
        agg["cdae"] += len(src.cdae_models)
        if zname == "common.zip":
            continue

        own_meshes = src.all_dae_mesh_names()
        other_meshes = {m for m, owners in all_mesh_owners.items() if (owners - {zname, "common.zip"})}

        for ce in src.all_configs:
            assert ce.pc is not None
            try:
                # Run with fallback_on_missing_part=False for exact apples-to-apples comparison
                # against Step 1 reference resolve.py (which did not substitute slot.default),
                # AND also run with default fallback_on_missing_part=True to verify zero crashes.
                res = beamng.resolve_config(
                    ce.pc,
                    src.part_index.parts,
                    common_parts,
                    model_dir=ce.vehicle or None,
                    own_meshes=own_meshes,
                    common_meshes=common_meshes,
                    other_zip_meshes=other_meshes,
                    fallback_on_missing_part=True,
                )
                res_nofb = beamng.resolve_config(
                    ce.pc,
                    src.part_index.parts,
                    common_parts,
                    model_dir=ce.vehicle or None,
                    own_meshes=own_meshes,
                    common_meshes=common_meshes,
                    other_zip_meshes=other_meshes,
                    fallback_on_missing_part=False,
                )
            except Exception as exc:
                exceptions.append(f"{zname}:{ce.path}: {type(exc).__name__}: {exc}")
                continue

            agg["configs"] += 1
            agg["no_main"] += int(res.main_part is None)
            agg["with_expr_meshes"] += int(res.expr_count > 0)
            agg["with_missing_parts_nofb"] += int(bool(res_nofb.missing_parts))
            agg["with_missing_parts"] += int(bool(res.missing_parts))
            agg["path_keys_total"] += res_nofb.path_keys_total
            agg["path_keys_used"] += res_nofb.path_keys_used
            agg["with_unused_path_keys"] += int(bool(res_nofb.unused_path_keys))
            agg["cycles"] += res.cycles

            # Non-expression unique mesh classification (matching Step 1 resolve.py)
            non_expr = {m.name: m.source_category for m in res_nofb.meshes if not m.from_expression}
            for cat in non_expr.values():
                raw_non_expr_counts[cat] += 1
            if any(c == "missing" for c in non_expr.values()):
                agg["with_nowhere_meshes_nofb"] += 1

            # Full resolved (including evaluated expressions) unique mesh classification
            all_res = {m.name: m.source_category for m in res.meshes}
            for cat in all_res.values():
                mesh_counts[cat] += 1
            if any(c == "missing" for c in all_res.values()):
                agg["with_nowhere_meshes"] += 1

    t_res = time.perf_counter() - t1

    out_dir = os.path.join(REPO_ROOT, "tests", "_local_beamng")
    os.makedirs(out_dir, exist_ok=True)
    summary = {
        "scan_sec": round(t_scan, 2),
        "resolve_sec": round(t_res, 2),
        "agg": dict(agg),
        "non_expr_mesh_refs": dict(raw_non_expr_counts),
        "all_resolved_mesh_refs": dict(mesh_counts),
        "exceptions": exceptions,
    }
    with open(os.path.join(out_dir, "step3_summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    print(json.dumps(summary, indent=2))
    assert not exceptions, f"Exceptions during resolution: {exceptions[:5]}"
    assert agg["zips"] == 126
    assert agg["jbeam_ok"] == 5294 and agg["jbeam_fail"] == 0
    assert agg["configs"] == 1767 and agg["no_main"] == 0
    assert agg["path_keys_total"] == 2044 and agg["path_keys_used"] == 2038
    assert agg["with_missing_parts_nofb"] == 72
    assert agg["with_nowhere_meshes_nofb"] == 113
    assert agg["with_expr_meshes"] == 400
    assert raw_non_expr_counts["own"] == 167017
    assert raw_non_expr_counts["common"] == 27306
    assert raw_non_expr_counts["other_zip"] == 47
    assert raw_non_expr_counts["missing"] == 387
    print("\nStep 3 real-data check PASSED (exact match with Step 1 baseline across all 1,767 configs)!")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
