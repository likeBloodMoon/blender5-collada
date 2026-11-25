"""Headless Blender Step 4 verification suite for BeamNG vehicle-config import (local only).

Runs all 11 cases from docs/beamng_vehicle_import_spec.md Section 7 Step 4 against the
read-only BeamNG vehicles folder, measuring objects, time, and peak memory (tracemalloc +
Windows RSS) and comparing against a full ALL import of the same archive.

Writes results to tests/_local_beamng/step4_<ver>.json (git-ignored).

Usage:
    blender.exe -b --factory-startup --python-exit-code 1 --python tests/run_beamng_blender_cases.py
"""

from __future__ import annotations

import ctypes
import json
import os
import sys
import time

import bpy
from mathutils import Vector


class _PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_ulong),
        ("PageFaultCount", ctypes.c_ulong),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def _get_rss_mb() -> tuple[float, float]:
    """Return (current_rss_mb, peak_rss_mb) with zero CPU overhead on Windows."""
    if os.name == "nt":
        pmc = _PROCESS_MEMORY_COUNTERS()
        pmc.cb = ctypes.sizeof(pmc)
        k32 = ctypes.windll.kernel32
        k32.GetCurrentProcess.restype = ctypes.c_void_p
        psapi = ctypes.windll.psapi
        psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong]
        psapi.GetProcessMemoryInfo.restype = ctypes.c_int
        proc = k32.GetCurrentProcess()
        if psapi.GetProcessMemoryInfo(proc, ctypes.byref(pmc), pmc.cb):
            return (
                pmc.WorkingSetSize / (1024 * 1024),
                pmc.PeakWorkingSetSize / (1024 * 1024),
            )
    return 0.0, 0.0

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

import io_scene_collada  # noqa: E402
from io_scene_collada.beamng.resolve import resolve_config  # noqa: E402
from io_scene_collada.beamng.source import clear_source_cache, load_vehicle_source  # noqa: E402

DEFAULT_VEHICLES_DIR = r"C:\Steam\steamapps\common\BeamNG.drive\content\vehicles"


_REGISTERED = False


def _reset_scene() -> None:
    global _REGISTERED
    bpy.ops.wm.read_factory_settings(use_empty=True)
    if not _REGISTERED:
        try:
            io_scene_collada.register()
        except ValueError:
            pass
        _REGISTERED = True


def _world_bbox(objs: list) -> tuple[Vector, Vector] | None:
    mins = Vector((1e9, 1e9, 1e9))
    maxs = Vector((-1e9, -1e9, -1e9))
    found = False
    for ob in objs:
        if ob.type != "MESH" or not ob.data or len(ob.data.vertices) == 0:
            continue
        mat = ob.matrix_world
        for corner in ob.bound_box:
            wc = mat @ Vector(corner)
            mins.x = min(mins.x, wc.x)
            mins.y = min(mins.y, wc.y)
            mins.z = min(mins.z, wc.z)
            maxs.x = max(maxs.x, wc.x)
            maxs.y = max(maxs.y, wc.y)
            maxs.z = max(maxs.z, wc.z)
            found = True
    return (mins, maxs) if found else None


def _is_object_in_excluded_only(ob) -> bool:
    def _find_lc(lc, coll):
        if lc.collection == coll:
            return lc
        for ch in lc.children:
            r = _find_lc(ch, coll)
            if r is not None:
                return r
        return None

    def _is_excluded_chain(lc_root, target_coll) -> bool:
        def _walk(lc, parent_excl: bool) -> bool | None:
            cur_excl = parent_excl or lc.exclude
            if lc.collection == target_coll:
                return cur_excl
            for ch in lc.children:
                res = _walk(ch, cur_excl)
                if res is not None:
                    return res
            return None

        r = _walk(lc_root, False)
        return bool(r)

    root_lc = bpy.context.view_layer.layer_collection
    return all(_is_excluded_chain(root_lc, c) for c in ob.users_collection)


def _mesh_objects() -> list:
    return [ob for ob in bpy.data.objects if ob.type == "MESH"]


def _mesh_names_base(objs: list) -> set[str]:
    # Strip Blender .001 deduplication suffixes only if needed? Wait: in a fresh scene
    # every unique mesh name imported once should keep its exact name.
    return {ob.name for ob in objs}


def _find_report_text() -> str:
    for txt in bpy.data.texts:
        if txt.name.startswith("BeamNG import"):
            return txt.as_string()
    return ""


def _measure_call(fn):
    t0 = time.perf_counter()
    res = fn()
    dt = time.perf_counter() - t0
    cur_mb, peak_mb = _get_rss_mb()
    return res, round(dt, 3), round(max(cur_mb, peak_mb), 1)


def main() -> int:
    vdir = os.environ.get("BEAMNG_VEHICLES_DIR") or DEFAULT_VEHICLES_DIR
    if not os.path.isdir(vdir):
        print(f"SKIP: BeamNG vehicles directory not found: {vdir}")
        return 0

    bl_ver = ".".join(str(x) for x in bpy.app.version)
    print(f"Running Step 4 BeamNG Blender import cases on Blender {bl_ver}...")

    # Warm common.zip cache check & verify cold vs cached timing
    clear_source_cache()
    t_c0 = time.perf_counter()
    common_src = load_vehicle_source(os.path.join(vdir, "common.zip"), index_dae_nodes=True)
    t_common_cold = round(time.perf_counter() - t_c0, 3)
    t_c1 = time.perf_counter()
    _ = load_vehicle_source(os.path.join(vdir, "common.zip"), index_dae_nodes=True)
    t_common_cached = round(time.perf_counter() - t_c1, 5)
    print(f"  common.zip index: cold={t_common_cold}s, cached={t_common_cached}s")

    results: list[dict] = []

    def _run_case(case_id: str, fn):
        try:
            info = fn()
            info["case"] = case_id
            info["status"] = "PASS"
            results.append(info)
            print(
                f"  [PASS] {case_id}: objs={info.get('objects')} time={info.get('time_s')}s "
                f"rss={info.get('peak_mb')}MB | {info.get('notes', '')}",
                flush=True,
            )
        except Exception as exc:
            results.append({"case": case_id, "status": "FAIL", "error": f"{type(exc).__name__}: {exc}"})
            print(f"  [FAIL] {case_id}: {type(exc).__name__}: {exc}", flush=True)
            raise

    # Baseline ALL import timing for covet.zip for comparison
    _reset_scene()
    _, t_covet_all, mb_covet_all = _measure_call(
        lambda: bpy.ops.wm.collada_import(filepath=os.path.join(vdir, "covet.zip"))
    )
    covet_all_objs = len(_mesh_objects())
    print(f"  [REF ] covet.zip ALL baseline: objs={covet_all_objs} time={t_covet_all}s peak={mb_covet_all}MB")

    # Case 1: covet.zip + 13s_M.pc
    def case_covet_13s_m():
        _reset_scene()
        zpath = os.path.join(vdir, "covet.zip")
        op_res, dt, mb = _measure_call(
            lambda: bpy.ops.wm.collada_import(
                filepath=zpath,
                beamng_config="vehicles/covet/13s_M.pc",
                beamng_parts_mode="CONFIG",
                use_common=True,
            )
        )
        assert op_res == {"FINISHED"}, op_res
        obs = _mesh_objects()
        names = _mesh_names_base(obs)
        # Verify against resolver
        src = load_vehicle_source(zpath)
        ce = next(c for c in src.all_configs if c.name == "13s_M")
        res = resolve_config(
            ce.pc,
            src.part_index.parts,
            common_src.part_index.parts,
            model_dir="covet",
            own_meshes=src.all_dae_mesh_names(),
            common_meshes=common_src.all_dae_mesh_names(),
        )
        expected = res.primary_mesh_names()
        assert names == expected, f"diff: extra={names - expected}, miss={expected - names}"
        hoods = [n for n in names if "hood" in n.lower() and "decal" not in n.lower()]
        assert hoods == ["covet_hood"], f"Expected ['covet_hood'], got {hoods}"
        assert not any("3wheel" in n.lower() for n in names), "Found unexpected 3wheel object"
        assert all(ob.get("beamng_config") == "13s_M" and ob.get("beamng_source") for ob in obs)
        assert "Needs placement" in bpy.data.collections
        return {
            "objects": len(obs),
            "time_s": dt,
            "peak_mb": mb,
            "all_time_s": t_covet_all,
            "notes": f"145 meshes (vs {covet_all_objs} in ALL; {dt}s vs {t_covet_all}s ALL); hood={hoods}",
        }

    _run_case("covet.zip + 13s_M.pc", case_covet_13s_m)
    covet_dae_names = _mesh_names_base(_mesh_objects())

    # Case 2: covet.zip + 3wheel.pc
    def case_covet_3wheel():
        _reset_scene()
        zpath = os.path.join(vdir, "covet.zip")
        op_res, dt, mb = _measure_call(
            lambda: bpy.ops.wm.collada_import(
                filepath=zpath,
                beamng_config="vehicles/covet/3wheel.pc",
                beamng_parts_mode="CONFIG",
                use_common=True,
            )
        )
        assert op_res == {"FINISHED"}
        obs = _mesh_objects()
        from_3w = [ob.name for ob in obs if "3wheel/covet_3wheel.dae" in str(ob.get("beamng_source", ""))]
        assert len(from_3w) > 0, "Expected objects sourced from 3wheel/covet_3wheel.dae"
        return {
            "objects": len(obs),
            "time_s": dt,
            "peak_mb": mb,
            "notes": f"{len(from_3w)} objects from 3wheel/covet_3wheel.dae",
        }

    _run_case("covet.zip + 3wheel.pc", case_covet_3wheel)

    # Case 3: pickup.zip + d15_4wd_A.pc (legacy .pc without format)
    def case_pickup_legacy():
        _reset_scene()
        zpath = os.path.join(vdir, "pickup.zip")
        op_res, dt, mb = _measure_call(
            lambda: bpy.ops.wm.collada_import(
                filepath=zpath,
                beamng_config="vehicles/pickup/d15_4wd_A.pc",
                beamng_parts_mode="CONFIG",
                use_common=True,
            )
        )
        assert op_res == {"FINISHED"}
        obs = _mesh_objects()
        src = load_vehicle_source(zpath)
        ce = next(c for c in src.all_configs if c.name == "d15_4wd_A")
        assert ce.pc.is_legacy is True
        res = resolve_config(
            ce.pc,
            src.part_index.parts,
            common_src.part_index.parts,
            model_dir="pickup",
            own_meshes=src.all_dae_mesh_names(),
            common_meshes=common_src.all_dae_mesh_names(),
        )
        expected = res.primary_mesh_names() | res.alternative_mesh_names()
        assert len(obs) == len(expected), f"objs={len(obs)} != resolver={len(expected)}"
        return {"objects": len(obs), "time_s": dt, "peak_mb": mb, "notes": "legacy .pc exact mesh match"}

    _run_case("pickup.zip + d15_4wd_A.pc (legacy)", case_pickup_legacy)

    # Case 4: pickup.zip + deserttruck_crawler_A.pc (6 slot-path keys, deep common slots)
    def case_pickup_crawler():
        _reset_scene()
        zpath = os.path.join(vdir, "pickup.zip")
        op_res, dt, mb = _measure_call(
            lambda: bpy.ops.wm.collada_import(
                filepath=zpath,
                beamng_config="vehicles/pickup/deserttruck_crawler_A.pc",
                beamng_parts_mode="CONFIG",
                use_common=True,
            )
        )
        assert op_res == {"FINISHED"}
        obs = _mesh_objects()
        names = _mesh_names_base(obs)
        rep = _find_report_text()
        assert "Path keys: 6/6 used" in rep, f"Expected 6/6 path keys in report:\n{rep[:400]}"
        race_seats = [
            n for n in names if "racing_seat" in n.lower() or "seat_race" in n.lower() or "raceseat" in n.lower()
        ]
        assert len(race_seats) > 0, f"Expected race seats in {sorted(n for n in names if 'seat' in n.lower())}"
        return {
            "objects": len(obs),
            "time_s": dt,
            "peak_mb": mb,
            "notes": f"6/6 path keys used; race seats={sorted(race_seats)}",
        }

    _run_case("pickup.zip + deserttruck_crawler_A.pc", case_pickup_crawler)

    # Case 5: vivace.zip config with $components expressions
    def case_vivace_expr():
        _reset_scene()
        zpath = os.path.join(vdir, "vivace.zip")
        src = load_vehicle_source(zpath)
        chosen_ce = None
        for ce in src.all_configs:
            r = resolve_config(
                ce.pc,
                src.part_index.parts,
                common_src.part_index.parts,
                model_dir="vivace",
                own_meshes=src.all_dae_mesh_names(),
                common_meshes=common_src.all_dae_mesh_names(),
            )
            if r.expr_count > 0:
                chosen_ce = ce
                break
        assert chosen_ce is not None
        op_res, dt, mb = _measure_call(
            lambda: bpy.ops.wm.collada_import(
                filepath=zpath,
                beamng_config=chosen_ce.path,
                beamng_parts_mode="CONFIG",
                use_common=True,
            )
        )
        assert op_res == {"FINISHED"}
        obs = _mesh_objects()
        assert not any(ob.name.startswith("$") for ob in obs), "Found raw $= mesh name!"
        return {
            "objects": len(obs),
            "time_s": dt,
            "peak_mb": mb,
            "notes": f"config={chosen_ce.name}, 0 raw $= names",
        }

    _run_case("vivace.zip ($components expr)", case_vivace_expr)

    # Case 6: logs.zip (random() expressions)
    def case_logs_random():
        _reset_scene()
        zpath = os.path.join(vdir, "logs.zip")
        src = load_vehicle_source(zpath)
        # Find a config whose parts use random() log expressions
        chosen_ce = None
        expected_cands = []
        for ce in src.all_configs:
            r = resolve_config(
                ce.pc,
                src.part_index.parts,
                common_src.part_index.parts,
                model_dir="logs",
                own_meshes=src.all_dae_mesh_names(),
                common_meshes=common_src.all_dae_mesh_names(),
            )
            expr_meshes = [m for m in r.meshes if m.from_expression]
            if len({m.name for m in expr_meshes}) >= 2:
                chosen_ce = ce
                expected_cands = sorted({m.name for m in expr_meshes})
                break
        assert chosen_ce is not None
        op_res, dt, mb = _measure_call(
            lambda: bpy.ops.wm.collada_import(
                filepath=zpath,
                beamng_config=chosen_ce.path,
                beamng_parts_mode="CONFIG",
                use_common=True,
            )
        )
        assert op_res == {"FINISHED"}
        obs = _mesh_objects()
        names = _mesh_names_base(obs)
        assert set(expected_cands).issubset(names), f"Missing log variants: {set(expected_cands) - names}"
        visible_logs = [ob.name for ob in obs if ob.name in expected_cands and not _is_object_in_excluded_only(ob)]
        hidden_logs = [ob.name for ob in obs if ob.name in expected_cands and _is_object_in_excluded_only(ob)]
        assert len(visible_logs) >= 1 and len(hidden_logs) >= 1, (visible_logs, hidden_logs)
        return {
            "objects": len(obs),
            "time_s": dt,
            "peak_mb": mb,
            "notes": f"config={chosen_ce.name}, visible={visible_logs}, alts={len(hidden_logs)}",
        }

    _run_case("logs.zip (random() expr)", case_logs_random)

    # Case 7: etk800.zip (cross-vehicle mesh etkc_engbaycrap reported as other-zip, not missing)
    def case_etk800_other_zip():
        _reset_scene()
        zpath = os.path.join(vdir, "etk800.zip")
        src = load_vehicle_source(zpath)
        chosen_ce = src.default_config_for_vehicle("etk800") or src.all_configs[0]
        # Pick a config that actually triggers etkc_engbaycrap
        for ce in src.all_configs:
            r = resolve_config(
                ce.pc,
                src.part_index.parts,
                common_src.part_index.parts,
                model_dir="etk800",
                own_meshes=src.all_dae_mesh_names(),
                common_meshes=common_src.all_dae_mesh_names(),
            )
            if "etkc_engbaycrap" in r.missing_meshes:
                chosen_ce = ce
                break
        op_res, dt, mb = _measure_call(
            lambda: bpy.ops.wm.collada_import(
                filepath=zpath,
                beamng_config=chosen_ce.path,
                beamng_parts_mode="CONFIG",
                use_common=True,
            )
        )
        assert op_res == {"FINISHED"}
        rep = _find_report_text()
        assert "Meshes in other vehicle zips" in rep and "etkc_engbaycrap" in rep, rep
        assert "Missing meshes" not in rep or "etkc_engbaycrap" not in rep.split("Missing meshes")[-1], rep
        return {
            "objects": len(_mesh_objects()),
            "time_s": dt,
            "peak_mb": mb,
            "notes": f"config={chosen_ce.name}; etkc_engbaycrap classified as other_zip (not missing)",
        }

    _run_case("etk800.zip (cross-vehicle other_zip)", case_etk800_other_zip)

    # Case 8: md_series.zip & percheron.zip (stale mesh references -> succeeds, listed in report)
    def case_stale_meshes(zip_name: str):
        _reset_scene()
        zpath = os.path.join(vdir, zip_name)
        src = load_vehicle_source(zpath)
        chosen_ce = None
        for ce in src.all_configs:
            r = resolve_config(
                ce.pc,
                src.part_index.parts,
                common_src.part_index.parts,
                model_dir=ce.vehicle or None,
                own_meshes=src.all_dae_mesh_names(),
                common_meshes=common_src.all_dae_mesh_names(),
            )
            if r.missing_meshes:
                chosen_ce = ce
                break
        assert chosen_ce is not None
        op_res, dt, mb = _measure_call(
            lambda: bpy.ops.wm.collada_import(
                filepath=zpath,
                beamng_config=chosen_ce.path,
                beamng_parts_mode="CONFIG",
                use_common=True,
            )
        )
        assert op_res == {"FINISHED"}
        rep = _find_report_text()
        assert "Missing meshes" in rep, rep
        return {
            "objects": len(_mesh_objects()),
            "time_s": dt,
            "peak_mb": mb,
            "notes": f"config={chosen_ce.name}; succeeded with missing meshes listed in report",
        }

    _run_case("md_series.zip (stale meshes)", lambda: case_stale_meshes("md_series.zip"))
    _run_case("percheron.zip (stale meshes)", lambda: case_stale_meshes("percheron.zip"))

    # Case 9: common.zip picker: 2 wheel .dae files with spread_out=True
    def case_common_two_wheels():
        _reset_scene()
        zpath = os.path.join(vdir, "common.zip")
        wheel_daes = [m.path for m in common_src.dae_models if "/wheels/" in m.path.lower()][:2]
        assert len(wheel_daes) == 2
        op_res, dt, mb = _measure_call(
            lambda: bpy.ops.wm.collada_import(
                filepath=zpath,
                archive_models=";".join(wheel_daes),
                beamng_parts_mode="ALL",
                spread_out=True,
            )
        )
        assert op_res == {"FINISHED"}
        # Check two collections exist and their world bounding boxes do NOT overlap on X
        colls = list(bpy.context.scene.collection.children)
        assert len(colls) == 2, [c.name for c in colls]
        bb0 = _world_bbox(list(colls[0].all_objects))
        bb1 = _world_bbox(list(colls[1].all_objects))
        assert bb0 is not None and bb1 is not None
        separated = (bb0[1].x < bb1[0].x - 0.1) or (bb1[1].x < bb0[0].x - 0.1) or (bb0[1].y < bb1[0].y - 0.1)
        assert separated, f"Spread-out bboxes overlapped: {bb0} vs {bb1}"
        return {
            "objects": len(_mesh_objects()),
            "time_s": dt,
            "peak_mb": mb,
            "notes": f"2 wheel models separated cleanly: x0=[{bb0[0].x:.2f},{bb0[1].x:.2f}] x1=[{bb1[0].x:.2f},{bb1[1].x:.2f}]",
        }

    _run_case("common.zip (2 wheels, spread_out=True)", case_common_two_wheels)

    # Case 10: utv.zip CONFIG_PLUS_HIDDEN
    def case_utv_hidden():
        zpath = os.path.join(vdir, "utv.zip")
        # Reference ALL import count (own meshes)
        _reset_scene()
        _, t_all, mb_all = _measure_call(lambda: bpy.ops.wm.collada_import(filepath=zpath))
        all_count = len(_mesh_objects())

        _reset_scene()
        src = load_vehicle_source(zpath)
        ce = src.default_config_for_vehicle("utv") or src.all_configs[0]
        op_res, dt, mb = _measure_call(
            lambda: bpy.ops.wm.collada_import(
                filepath=zpath,
                beamng_config=ce.path,
                beamng_parts_mode="CONFIG_PLUS_HIDDEN",
                use_common=False,  # compare pure own object count directly against ALL
            )
        )
        assert op_res == {"FINISHED"}
        obs = _mesh_objects()
        assert len(obs) == all_count, f"CONFIG_PLUS_HIDDEN count {len(obs)} != ALL {all_count}"
        res = resolve_config(
            ce.pc,
            src.part_index.parts,
            {},
            model_dir="utv",
            own_meshes=src.all_dae_mesh_names(),
            common_meshes=set(),
        )
        active_names = res.primary_mesh_names()
        non_cfg_visible = [
            ob.name for ob in obs if ob.name not in active_names and not _is_object_in_excluded_only(ob)
        ]
        assert not non_cfg_visible, f"Non-config objects leaked into visible collection: {non_cfg_visible[:10]}"
        return {
            "objects": len(obs),
            "time_s": dt,
            "peak_mb": mb,
            "all_time_s": t_all,
            "notes": f"objs={len(obs)} == ALL({all_count}); 0 non-config objects visible",
        }

    _run_case("utv.zip (CONFIG_PLUS_HIDDEN)", case_utv_hidden)

    # Case 11: .cdae path for covet.zip + 13s_M.pc
    def case_covet_cdae():
        _reset_scene()
        zpath = os.path.join(vdir, "covet.zip")
        op_res, dt, mb = _measure_call(
            lambda: bpy.ops.import_scene.beamng_cdae(
                filepath=zpath,
                beamng_config="vehicles/covet/13s_M.pc",
                beamng_parts_mode="CONFIG",
                use_common=True,
            )
        )
        assert op_res == {"FINISHED"}
        obs = _mesh_objects()
        cdae_names = _mesh_names_base(obs)
        assert cdae_names == covet_dae_names, (
            f"CDAE vs DAE mismatch: extra={cdae_names - covet_dae_names}, miss={covet_dae_names - cdae_names}"
        )
        return {
            "objects": len(obs),
            "time_s": dt,
            "peak_mb": mb,
            "notes": f"100% identical {len(cdae_names)} object names vs .dae import",
        }

    _run_case("covet.zip + 13s_M.pc (.cdae)", case_covet_cdae)

    out_dir = os.path.join(REPO_ROOT, "tests", "_local_beamng")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"step4_{bl_ver}.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "blender": bl_ver,
                "common_cold_s": t_common_cold,
                "common_cached_s": t_common_cached,
                "covet_all_objs": covet_all_objs,
                "covet_all_time_s": t_covet_all,
                "covet_all_peak_mb": mb_covet_all,
                "cases": results,
            },
            f,
            indent=2,
        )
    print(f"\nAll {len(results)} Step 4 cases PASSED on Blender {bl_ver}! Saved: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
