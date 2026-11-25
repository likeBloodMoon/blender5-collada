"""Plain-Python unit tests for BeamNG lenient JSON parser and jbeam table parsing (Step 2).

Runs without bpy and without any game files (CI-safe).
"""

from __future__ import annotations

import importlib.util
import math
import os
import sys
import types

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _import_beamng_no_bpy():
    """Load io_scene_collada.beamng without triggering io_scene_collada/__init__.py (which imports bpy)."""
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
parse = beamng.parse
JbeamError = beamng.JbeamError
parse_jbeam_table = beamng.parse_jbeam_table
PartIndex = beamng.PartIndex
has_non_identity_modifiers = beamng.has_non_identity_modifiers


def test_comments_and_bom() -> None:
    text = (
        "\ufeff// leading comment\n"
        "{\n"
        "  /* block\n"
        "     comment */\n"
        "  # shell comment\n"
        '  "url": "http://example.com//keep/*ok*/#hash",\n'
        '  "ok": true,\n'
        "}\n"
    )
    d = parse(text)
    assert d["url"] == "http://example.com//keep/*ok*/#hash"
    assert d["ok"] is True


def test_trailing_and_missing_commas() -> None:
    # Missing commas on the same line AND across lines, plus trailing commas
    text = """{
        "a": 1 "b": 2
        "c": [1 2 3,]
        "rows": [
            ["mesh", "[group]:"]
            ["hood_a", ["hood"]]
            ["hood_b", ["hood"],]
        ],
    }"""
    d = parse(text)
    assert d["a"] == 1 and d["b"] == 2
    assert d["c"] == [1, 2, 3]
    assert len(d["rows"]) == 3 and d["rows"][2][0] == "hood_b"


def test_numbers_and_bare_constants() -> None:
    text = """{
        "dot_end": 1.,
        "dot_start": .5,
        "plus": +1,
        "plus_f": +2.5e2,
        "neg": -3.25,
        "fmax": FLT_MAX,
        "nfmax": -FLT_MAX,
        "n": nan,
        "infp": inf,
        "infn": -inf,
        "t": true,
        "f": false,
        "z": null
    }"""
    d = parse(text)
    assert d["dot_end"] == 1.0
    assert d["dot_start"] == 0.5
    assert d["plus"] == 1
    assert d["plus_f"] == 250.0
    assert d["neg"] == -3.25
    assert d["fmax"] > 1e38
    assert d["nfmax"] < -1e38
    assert math.isnan(d["n"])
    assert math.isinf(d["infp"]) and d["infp"] > 0
    assert math.isinf(d["infn"]) and d["infn"] < 0
    assert d["t"] is True and d["f"] is False and d["z"] is None


def test_unquoted_keys_equals_escapes_and_duplicate_keys() -> None:
    text = r"""{
        unquotedKey: "hello\n\t\"world\"\\/\u0041",
        eqKey = 42,
        dup: "first",
        dup: "second"
    }"""
    d = parse(text)
    assert d["unquotedKey"] == 'hello\n\t"world"\\/A'
    assert d["eqKey"] == 42
    assert d["dup"] == "second"


def test_syntax_errors_carry_line_and_col() -> None:
    # Unterminated string on line 2, col 8
    bad_str = '{\n  "k": "unterminated\n}'
    try:
        parse(bad_str)
        raise AssertionError("Expected JbeamError for unterminated string")
    except JbeamError as exc:
        assert exc.line == 2 and exc.col == 8, f"Got line={exc.line}, col={exc.col}: {exc}"

    # Unterminated array on line 3, col 3
    bad_arr = '{\n  "a":\n  [1, 2\n'
    try:
        parse(bad_arr)
        raise AssertionError("Expected JbeamError for unterminated array")
    except JbeamError as exc:
        assert exc.line == 3 and exc.col == 3, f"Got line={exc.line}, col={exc.col}: {exc}"

    # Missing colon/equals after key
    bad_colon = '{\n  "k" 123\n}'
    try:
        parse(bad_colon)
        raise AssertionError("Expected JbeamError for missing ':'")
    except JbeamError as exc:
        assert exc.line == 2, f"Got line={exc.line}: {exc}"

    # Trailing garbage
    try:
        parse('{"a": 1} extra')
        raise AssertionError("Expected JbeamError for trailing data")
    except JbeamError as exc:
        assert exc.line == 1 and exc.col == 10, f"Got line={exc.line}, col={exc.col}: {exc}"


def test_beamng_materials_uses_single_parser() -> None:
    spec = importlib.util.spec_from_file_location(
        "io_scene_collada.beamng_materials",
        os.path.join(REPO_ROOT, "io_scene_collada", "beamng_materials.py"),
    )
    assert spec is not None and spec.loader is not None
    bmat = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bmat)
    assert bmat.loads_lenient_json is parse
    # Verify trailing comma + missing comma in main.materials.json parses cleanly
    mats = bmat.parse_beamng_materials_json(
        '{"car_body": {"mapTo": "car_body" "Stages": [{"baseColorMap": "/vehicles/car/body.png",},]}}'
    )
    assert mats["car_body"] == ["/vehicles/car/body.png"]


def test_table_parsing_modifiers_and_reordered_headers() -> None:
    # Header with reordered columns (mesh is NOT column 0!), dict modifier rows carried forward,
    # and trailing dict inline modifier applying to one row only.
    tbl = [
        ["[group]:", "mesh", "nonFlexMaterials"],
        {"pos": {"x": 0.0, "y": 1.0, "z": 0.0}},
        [["g1"], "mesh_moved", []],
        [["g1"], "mesh_inline", [], {"rot": {"x": 0.0, "y": 0.0, "z": 90.0}}],
        [["g1"], "mesh_after_inline", []],
        {"pos": {"x": 0.0, "y": 0.0, "z": 0.0}},
        [["g1"], "mesh_identity", []],
    ]
    rows = parse_jbeam_table(tbl)
    assert [r["mesh"] for r, _ in rows] == ["mesh_moved", "mesh_inline", "mesh_after_inline", "mesh_identity"]
    assert has_non_identity_modifiers(rows[0][1]) is True
    assert rows[1][1]["rot"]["z"] == 90.0
    assert "rot" not in rows[2][1]  # inline modifier did not leak
    assert rows[2][1]["pos"]["y"] == 1.0
    assert has_non_identity_modifiers(rows[3][1]) is False


def test_slots_v1_and_slots2_and_spotlight_skip() -> None:
    jbeam = """{
      "car_main": {
        "slotType": "main",
        "information": {"name": "Main Body"},
        "slots": [
          ["description", "default", "type"],
          ["Hood Slot", "car_hood_a", "car_hood"],
        ],
        "flexbodies": [
          ["mesh", "[group]:"],
          ["car_body", ["body"]],
        ],
        "props": [
          ["func", "mesh", "idRef:", "idX:", "idY:", "baseRotation", "rotation", "translation"],
          ["lowbeam", "SPOTLIGHT", "n0", "n1", "n2", {"x":0,"y":0,"z":0}, {"x":0,"y":0,"z":0}, {"x":0,"y":0,"z":0}],
          ["steering", "car_steer", "n0", "n1", "n2", {"x":0,"y":0,"z":0}, {"x":0,"y":0,"z":0}, {"x":0,"y":0,"z":0}],
        ],
      },
      "car_hood_a": {
        "slotType": ["car_hood", "car_hood_alt"],
        "slots2": [
          ["description", "name", "allowTypes", "denyTypes", "default"],
          ["Scoop", "hood_scoop", ["scoop"], [], ""],
        ],
        "flexbodies": [
          ["mesh", "[group]:"],
          ["car_hood_mesh", ["hood"]],
        ],
      }
    }"""
    idx = PartIndex()
    assert idx.add_jbeam_text(jbeam, "vehicles/car/car.jbeam")
    assert idx.jbeam_ok == 1 and not idx.jbeam_fail
    main = idx.parts["car_main"]
    assert main.slots[0].name == "car_hood" and main.slots[0].default == "car_hood_a" and main.slots[0].fmt == 1
    assert [m.mesh for m in main.meshes] == ["car_body", "car_steer"]  # SPOTLIGHT skipped, prop dict col kept
    hood = idx.parts["car_hood_a"]
    assert hood.slot_types == ["car_hood", "car_hood_alt"]
    assert hood.slots[0].name == "hood_scoop" and hood.slots[0].fmt == 2


def main() -> int:
    tests = [
        test_comments_and_bom,
        test_trailing_and_missing_commas,
        test_numbers_and_bare_constants,
        test_unquoted_keys_equals_escapes_and_duplicate_keys,
        test_syntax_errors_carry_line_and_col,
        test_beamng_materials_uses_single_parser,
        test_table_parsing_modifiers_and_reordered_headers,
        test_slots_v1_and_slots2_and_spotlight_skip,
    ]
    for fn in tests:
        fn()
        print(f"  PASS {fn.__name__}")
    print(f"\nAll {len(tests)} BeamNG parser & table unit tests passed!")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
