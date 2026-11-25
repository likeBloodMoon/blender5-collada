"""Plain-Python unit tests for BeamNG config resolver, expression evaluator, and source scanner (Step 2).

Runs without bpy and without any game files (CI-safe).
"""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import types
import zipfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


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


beamng = _import_beamng_no_bpy()
PartIndex = beamng.PartIndex
parse_pc = beamng.parse_pc
resolve_config = beamng.resolve_config
eval_mesh_expr = beamng.eval_mesh_expr
eval_modifier_expr = beamng.eval_modifier_expr
extract_dae_node_names = beamng.extract_dae_node_names
load_vehicle_source = beamng.load_vehicle_source
clear_source_cache = beamng.clear_source_cache


def _build_synthetic_pools() -> tuple[PartIndex, PartIndex]:
    common = PartIndex()
    common.add_jbeam_text(
        """{
          "common_seat_race": {
            "slotType": "car_seat_FL",
            "slots2": [
              ["name", "allowTypes", "denyTypes", "default", "description"],
              ["seat_harness", ["harness"], [], "common_harness_red", "Harness"]
            ],
            "flexbodies": [
              ["mesh", "[group]:"],
              ["seat_race_mesh", ["seat"]]
            ]
          },
          "common_harness_red": {
            "slotType": "harness",
            "flexbodies": [
              ["mesh", "[group]:"],
              ["harness_red_mesh", ["seat"]]
            ]
          },
          "common_harness_blue": {
            "slotType": "harness",
            "flexbodies": [
              ["mesh", "[group]:"],
              ["harness_blue_mesh", ["seat"]]
            ]
          }
        }""",
        "vehicles/common/seats.jbeam",
    )

    veh = PartIndex()
    veh.add_jbeam_text(
        """{
          "testcar": {
            "slotType": "main",
            "components": {
              "bodyType": {"hatch": true},
              "driveshaftMeshA": "ds_short"
            },
            "variables": [
              ["name", "type", "unit", "category", "default", "min", "max"],
              ["$scale", "range", "", "Size", 1.2, 0.5, 2.0]
            ],
            "slots2": [
              ["name", "allowTypes", "denyTypes", "default", "description"],
              ["car_hood", ["car_hood"], [], "car_hood_dup_ignored", "Duplicate Hood First"],
              ["car_hood", ["car_hood"], [], "car_hood_std", "Hood"],
              ["car_seat_FL", ["car_seat_FL"], [], "common_seat_race", "Driver Seat"],
              ["car_bumper", ["car_bumper"], [], "car_bumper_std", "Bumper"],
              ["car_extra", ["car_extra"], [], "car_extra_a", "Extra"]
            ],
            "flexbodies": [
              ["mesh", "[group]:"],
              ["body_mesh", ["body"]],
              ["$=$components.driveshaftMeshA", ["body"]]
            ]
          },
          "testcar_alt_main": {
            "slotType": ["main", "alt_main"],
            "flexbodies": [
              ["mesh", "[group]:"],
              ["alt_body_mesh", ["body"]]
            ]
          },
          "car_hood_std": {
            "slotType": "car_hood",
            "flexbodies": [
              ["mesh", "[group]:"],
              ["hood_std_mesh", ["hood"]]
            ]
          },
          "car_hood_sport": {
            "slotType": "car_hood",
            "flexbodies": [
              ["mesh", "[group]:"],
              ["hood_sport_mesh", ["hood"]],
              {"pos": {"x": 0.0, "y": "$=$scale * 0.5", "z": 0.0}},
              ["hood_scoop_moved", ["hood"]]
            ]
          },
          "car_bumper_std": {
            "slotType": "car_bumper",
            "flexbodies": [
              ["mesh", "[group]:"],
              ["bumper_std_mesh", ["bumper"]]
            ]
          },
          "car_extra_a": {
            "slotType": ["car_extra", "other_slot"],
            "flexbodies": [
              ["mesh", "[group]:"],
              ["extra_a_mesh", ["extra"]]
            ]
          },
          "cycle_part_a": {
            "slotType": "cycle_slot",
            "slots": [
              ["type", "default", "description"],
              ["cycle_slot", "cycle_part_a", "Self Cycle"]
            ],
            "flexbodies": [
              ["mesh", "[group]:"],
              ["cycle_mesh", ["c"]]
            ]
          }
        }""",
        "vehicles/testcar/testcar.jbeam",
    )
    return common, veh


def test_path_beats_name_and_deep_common_slots() -> None:
    common, veh = _build_synthetic_pools()
    pc = parse_pc(
        """{
          "format": 2,
          "parts": {
            "car_hood": "car_hood_sport",
            "seat_harness": "common_harness_red",
            "/car_seat_FL/seat_harness/": "common_harness_blue",
            "car_bumper": "none",
            "car_extra": ""
          },
          "vars": {"$scale": 1.6}
        }""",
        "vehicles/testcar/sport.pc",
    )
    own = {"body_mesh", "ds_short", "hood_sport_mesh", "hood_scoop_moved"}
    com = {"seat_race_mesh", "harness_blue_mesh"}
    res = resolve_config(pc, veh.parts, common.parts, own_meshes=own, common_meshes=com)

    assert res.main_part == "testcar"
    pnames = [p.name for p in res.parts]
    assert "car_hood_sport" in pnames
    assert "common_seat_race" in pnames
    assert "common_harness_blue" in pnames and "common_harness_red" not in pnames  # path key beat name key!
    assert "car_bumper_std" not in pnames  # "none" left empty
    assert "car_extra_a" not in pnames     # "" left empty
    assert res.path_keys_used == 1 and res.path_keys_total == 1 and not res.unused_path_keys
    assert len(res.duplicate_slot_warnings) == 1  # duplicate car_hood logged, last kept
    assert "kept last" in res.duplicate_slot_warnings[0]
    mnames = res.primary_mesh_names()
    assert mnames == own | com
    # Check moved flexbody modifier expression evaluated ($scale=1.6 -> y=0.8) and flagged needs_placement
    scoop = next(m for m in res.meshes if m.name == "hood_scoop_moved")
    assert scoop.needs_placement is True
    assert abs(scoop.evaluated_modifiers["pos"]["y"] - 0.8) < 1e-6


def test_duplicate_slot_last_wins() -> None:
    """Duplicate slot names inside one part keep the last definition (matching BeamNG slotSystem.lua)."""
    common, veh = _build_synthetic_pools()
    pc = parse_pc('{"format": 2, "parts": {}}', "vehicles/testcar/default.pc")
    res = resolve_config(pc, veh.parts, common.parts)
    pnames = [p.name for p in res.parts]
    assert "car_hood_std" in pnames
    assert "car_hood_dup_ignored" not in pnames
    assert any("car_hood" in w and "kept last" in w for w in res.duplicate_slot_warnings)


def test_two_slot_paths_same_slot_name() -> None:
    """Two branches sharing a slot name resolve independently via /branchA/slot/ vs /branchB/slot/."""
    veh = PartIndex()
    veh.add_jbeam_text(
        """{
          "truck": {
            "slotType": "main",
            "slots2": [
              ["name", "allowTypes", "denyTypes", "default", "description"],
              ["axle_F", ["axle_F"], [], "axle_front", "Front Axle"],
              ["axle_R", ["axle_R"], [], "axle_rear", "Rear Axle"],
              ["axle_M", ["axle_M"], [], "axle_mid", "Mid Axle"]
            ]
          },
          "axle_front": {
            "slotType": "axle_F",
            "slots2": [
              ["name", "allowTypes", "denyTypes", "default", "description"],
              ["brake_pad", ["brake_pad"], [], "pad_std", "Front Brake Pad"]
            ]
          },
          "axle_rear": {
            "slotType": "axle_R",
            "slots2": [
              ["name", "allowTypes", "denyTypes", "default", "description"],
              ["brake_pad", ["brake_pad"], [], "pad_std", "Rear Brake Pad"]
            ]
          },
          "axle_mid": {
            "slotType": "axle_M",
            "slots2": [
              ["name", "allowTypes", "denyTypes", "default", "description"],
              ["brake_pad", ["brake_pad"], [], "pad_std", "Mid Brake Pad"]
            ]
          },
          "pad_std": {"slotType": "brake_pad", "flexbodies": [["mesh", "[group]:"], ["pad_std_m", ["b"]]]},
          "pad_sport": {"slotType": "brake_pad", "flexbodies": [["mesh", "[group]:"], ["pad_sport_m", ["b"]]]},
          "pad_race": {"slotType": "brake_pad", "flexbodies": [["mesh", "[group]:"], ["pad_race_m", ["b"]]]},
          "pad_hd": {"slotType": "brake_pad", "flexbodies": [["mesh", "[group]:"], ["pad_hd_m", ["b"]]]}
        }""",
        "vehicles/truck/truck.jbeam",
    )
    pc = parse_pc(
        """{
          "format": 2,
          "parts": {
            "brake_pad": "pad_hd",
            "/axle_F/brake_pad/": "pad_sport",
            "/axle_R/brake_pad/": "pad_race"
          }
        }""",
        "vehicles/truck/custom.pc",
    )
    own = {"pad_std_m", "pad_sport_m", "pad_race_m", "pad_hd_m"}
    res = resolve_config(pc, veh.parts, {}, own_meshes=own)
    by_path = {p.slot_path: p.name for p in res.parts}
    assert by_path["/axle_F/brake_pad/"] == "pad_sport"
    assert by_path["/axle_R/brake_pad/"] == "pad_race"
    assert by_path["/axle_M/brake_pad/"] == "pad_hd"
    assert res.path_keys_used == 2 and res.path_keys_total == 2 and not res.unused_path_keys
    assert res.primary_mesh_names() == {"pad_sport_m", "pad_race_m", "pad_hd_m"}


def test_legacy_pc_and_missing_part_fallback() -> None:
    common, veh = _build_synthetic_pools()
    # Legacy .pc: no "format", no "parts" key
    pc = parse_pc(
        """{
          "car_hood": "nonexistent_hood_part",
          "car_bumper": "none",
          "vars": {"$scale": 1.0}
        }""",
        "vehicles/testcar/legacy.pc",
    )
    assert pc.is_legacy is True
    own = {"body_mesh", "ds_short", "hood_std_mesh", "extra_a_mesh"}
    com = {"seat_race_mesh", "harness_red_mesh"}
    res = resolve_config(pc, veh.parts, common.parts, own_meshes=own, common_meshes=com)
    assert "nonexistent_hood_part" in res.missing_parts
    assert "car_hood_std" in [p.name for p in res.parts]  # fell back to slot default
    assert "car_bumper_std" not in [p.name for p in res.parts]


def test_main_part_name_override_and_cycle_guard() -> None:
    common, veh = _build_synthetic_pools()
    pc = parse_pc(
        '{"format": 2, "mainPartName": "testcar_alt_main", "parts": {}}',
        "vehicles/testcar/alt.pc",
    )
    res = resolve_config(pc, veh.parts, common.parts, own_meshes={"alt_body_mesh"})
    assert res.main_part == "testcar_alt_main"
    assert res.primary_mesh_names() == {"alt_body_mesh"}

    # Cycle guard test: cycle_part_a references itself infinitely along /cycle_slot/cycle_slot/...
    # Guard triggers on repeated part at same path; also verify max depth does not crash
    cyc_veh = PartIndex()
    cyc_veh.add_jbeam_text(
        """{
          "root": {
            "slotType": "main",
            "slots": [["type", "default", "description"], ["s", "root", "Self"]]
          }
        }""",
        "vehicles/cyc/cyc.jbeam",
    )
    # Use a slot path revisiting same (part, path) via empty slot name wouldn't happen;
    # test direct self-slot recursion terminates cleanly without stack overflow
    # Let's ensure depth guard or cycle detection handles self-referencing slots safely
    pc_c = parse_pc('{"format": 2, "parts": {}}', "vehicles/cyc/c.pc")
    # Note: path changes each step (/s/, /s/s/, ...) so we must guard depth or ancestor part!
    # Verify resolve_config handles self-loop without RecursionError!
    try:
        res_c = resolve_config(pc_c, cyc_veh.parts, {})
        assert res_c.main_part == "root"
        assert res_c.cycles >= 1
        assert any("depth limit" in w.lower() for w in res_c.warnings), (
            f"Expected depth-limit warning, got {res_c.warnings}"
        )
    except RecursionError:
        raise AssertionError("Self-referencing slot caused RecursionError!")


def test_all_observed_expression_shapes() -> None:
    known = {
        "log_01", "log_02", "log_03", "log_04",
        "diffuser_yes", "diffuser_no",
        "prop_1", "prop_2", "prop_3",
        "hatch_trim", "sedan_trim",
        "ds_short",
        "small_mesh", "big_mesh",
        "fallback_a", "fallback_b",
    }

    # 1. $='S'..(round(random(4)))
    c1, multi1, w1 = eval_mesh_expr("$='log_0'..(round(random(4)))", {}, {}, known)
    assert c1 == ["log_01", "log_02", "log_03", "log_04"] and multi1 is True and not w1

    # 2. $= $components.hasDiffuserCutout ~= true and 'S' or 'S'
    c2a, m2a, w2a = eval_mesh_expr(
        "$= $components.hasDiffuserCutout ~= true and 'diffuser_no' or 'diffuser_yes'",
        {},
        {"hasDiffuserCutout": True},
        known,
    )
    assert c2a == ["diffuser_yes"] and m2a is False and not w2a
    c2b, _, _ = eval_mesh_expr(
        "$= $components.hasDiffuserCutout ~= true and 'diffuser_no' or 'diffuser_yes'",
        {},
        {},
        known,
    )
    assert c2b == ["diffuser_no"]

    # 3. $= 'S' .. random(1,16)
    c3, m3, w3 = eval_mesh_expr("$= 'prop_' .. random(1, 16)", {}, {}, known)
    assert c3 == ["prop_1", "prop_2", "prop_3"] and m3 is True and not w3

    # 4. $=$components.bodyType.hatch == true and 'S' or 'S'
    c4, m4, w4 = eval_mesh_expr(
        "$=$components.bodyType.hatch == true and 'hatch_trim' or 'sedan_trim'",
        {},
        {"bodyType": {"hatch": True}},
        known,
    )
    assert c4 == ["hatch_trim"] and m4 is False and not w4

    # 5. $=$components.driveshaftMeshA
    c5, m5, w5 = eval_mesh_expr("$=$components.driveshaftMeshA", {}, {"driveshaftMeshA": "ds_short"}, known)
    assert c5 == ["ds_short"] and m5 is False and not w5

    # 6. $= $scale < 1.5 and 'S' or 'S'
    c6a, _, _ = eval_mesh_expr("$= $scale < 1.5 and 'small_mesh' or 'big_mesh'", {"$scale": 1.2}, {}, known)
    c6b, _, _ = eval_mesh_expr("$= $scale < 1.5 and 'small_mesh' or 'big_mesh'", {"$scale": 1.8}, {}, known)
    assert c6a == ["small_mesh"] and c6b == ["big_mesh"]

    # 7. Failure fallback extracts known string literals and warns with expression text
    bad_expr = "$= unknownFunc('fallback_a', 'not_in_mesh', 'fallback_b')"
    c7, m7, w7 = eval_mesh_expr(bad_expr, {}, {}, known)
    assert c7 == ["fallback_a", "fallback_b"] and m7 is True
    assert len(w7) == 1 and "unknownFunc" in w7[0]

    # 7b. random() matching zero known meshes emits a warning
    c7b, m7b, w7b = eval_mesh_expr("$= 'missing_variant_' .. random(1, 3)", {}, {}, known)
    assert c7b == [] and m7b is True
    assert len(w7b) == 1 and "no matching mesh" in w7b[0].lower()

    # 8. Modifier expression success & failure
    v_ok, w_ok = eval_modifier_expr("$= $offset + 0.25", {"$offset": 0.5}, {})
    assert v_ok == 0.75 and w_ok is None
    v_bad, w_bad = eval_modifier_expr("$= badFunc(1)", {}, {})
    assert v_bad is None and w_bad is not None


def test_dae_node_name_any_attr_order_and_source_cache() -> None:
    # Bollard-style: name before id, plus node with only id
    dae = b'<node name="bollard_a" id="node_1" type="NODE"/><node id="only_id" type="NODE"/>'
    names = extract_dae_node_names(dae)
    assert names == {"bollard_a", "only_id"}

    # Zip scanning + common.zip discovery + cache hit
    clear_source_cache()
    with tempfile.TemporaryDirectory() as td:
        com_zip = os.path.join(td, "common.zip")
        with zipfile.ZipFile(com_zip, "w") as zf:
            zf.writestr("vehicles/common/w.jbeam", '{"w1": {"slotType": "wheel"}}')
            zf.writestr("vehicles/common/wheels/w1.dae", b'<node name="w1_mesh" id="w1_mesh"/>')
        car_zip = os.path.join(td, "car.zip")
        with zipfile.ZipFile(car_zip, "w") as zf:
            zf.writestr("vehicles/car/info.json", '{"default_pc": "base"}')
            zf.writestr("vehicles/car/info_base.json", '{"Config Type": "Factory"}')
            zf.writestr("vehicles/car/base.pc", '{"format": 2, "parts": {}}')
            zf.writestr("vehicles/car/car.jbeam", '{"car": {"slotType": "main"}}')
            zf.writestr("vehicles/car/car.dae", b'<node name="car_body" id="car_body"/>')

        s1 = load_vehicle_source(car_zip)
        s2 = load_vehicle_source(car_zip)
        assert s1 is s2  # cache hit
        assert s1.common_path == com_zip
        assert "car" in s1.vehicles and s1.vehicles["car"].default_pc == "base"
        assert s1.default_config_for_vehicle("car").name == "base"
        assert s1.vehicles["car"].configs[0].config_type == "Factory"
        assert "car_body" in s1.all_dae_mesh_names()

        # Non-recursive loose-folder scan outside a `vehicles/<model>/` tree
        loose_dir = os.path.join(td, "desktop_like")
        sub_dir = os.path.join(loose_dir, "nested_stuff")
        os.makedirs(sub_dir, exist_ok=True)
        root_dae = os.path.join(loose_dir, "root.dae")
        with open(root_dae, "wb") as f:
            f.write(b'<node name="root_mesh" id="root_mesh"/>')
        with open(os.path.join(sub_dir, "nested.dae"), "wb") as f:
            f.write(b'<node name="nested_mesh" id="nested_mesh"/>')
        with open(os.path.join(sub_dir, "nested.jbeam"), "w", encoding="utf-8") as f:
            f.write('{"nested_part": {"slotType": "main"}}')

        s_loose = load_vehicle_source(root_dae)
        assert "root_mesh" in s_loose.all_dae_mesh_names()
        assert "nested_mesh" not in s_loose.all_dae_mesh_names()
        assert "nested_part" not in s_loose.part_index.parts

        # Inside `vehicles/<model>/`, bounded recursion DOES pick up variant subdirs
        veh_model_dir = os.path.join(td, "vehicles", "mycar")
        veh_sub = os.path.join(veh_model_dir, "3wheel")
        os.makedirs(veh_sub, exist_ok=True)
        veh_dae = os.path.join(veh_model_dir, "mycar.dae")
        with open(veh_dae, "wb") as f:
            f.write(b'<node name="mycar_body" id="mycar_body"/>')
        with open(os.path.join(veh_sub, "mycar_3wheel.dae"), "wb") as f:
            f.write(b'<node name="mycar_3w" id="mycar_3w"/>')
        s_veh = load_vehicle_source(veh_dae)
        assert {"mycar_body", "mycar_3w"} <= s_veh.all_dae_mesh_names()
    clear_source_cache()


def main() -> int:
    tests = [
        test_path_beats_name_and_deep_common_slots,
        test_duplicate_slot_last_wins,
        test_two_slot_paths_same_slot_name,
        test_legacy_pc_and_missing_part_fallback,
        test_main_part_name_override_and_cycle_guard,
        test_all_observed_expression_shapes,
        test_dae_node_name_any_attr_order_and_source_cache,
    ]
    for fn in tests:
        fn()
        print(f"  PASS {fn.__name__}")
    print(f"\nAll {len(tests)} BeamNG resolver & expression unit tests passed!")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
