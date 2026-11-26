"""Regression tests for #46: jbeam expression candidate explosion and deep nesting guards.

Plain-Python test (no bpy required, CI-safe). Verifies:
  1. Combined random() expressions exceeding the total candidate cap fail fast
     with ExprError fallback and warning instead of hanging.
  2. Deeply nested expressions (e.g. 3000 parens / unary minus) do not raise
     RecursionError; eval_mesh_expr and eval_modifier_expr fall back cleanly.
  3. Deeply nested lenient JSON arrays/objects raise JbeamError instead of
     RecursionError.
  4. Normal random() expressions under the cap still evaluate cleanly.

Usage:
    python tests/test_jbeam_expr_limits.py
"""

from __future__ import annotations

import importlib.util
import os
import sys
import time
import types

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
from io_scene_collada.beamng import expr, lenient_json  # noqa: E402


def test_random_candidate_cap() -> None:
    # Small product under the cap still works
    cands, is_multi, warns = expr.eval_mesh_expr("$='m'..random(5)..random(5)", {}, {}, set())
    assert is_multi and not warns and len(cands) == 36, f"Unexpected small random: {len(cands)}, {warns}"

    # Huge product from #46 must fail fast (< 0.5s) with warning instead of hanging
    t0 = time.perf_counter()
    cands, is_multi, warns = expr.eval_mesh_expr(
        "$='m'..random(64)..random(64)..random(64)", {}, {}, {"m"}
    )
    elapsed = time.perf_counter() - t0
    assert elapsed < 0.5, f"Combined random() took too long: {elapsed:.2f}s"
    assert is_multi, "Expected multi-candidate fallback"
    assert warns, "Expected warning when candidate limit exceeded"
    assert cands == ["m"], f"Expected literal fallback ['m'], got {cands}"
    print(f"  PASS random_candidate_cap ({elapsed * 1000:.2f} ms, warn={warns[0]})")


def test_deep_expr_recursion_guard() -> None:
    deep_parens = "$=" + "(" * 3000 + "1" + ")" * 3000
    cands, is_multi, warns = expr.eval_mesh_expr(deep_parens, {}, {}, set())
    assert is_multi and warns, f"Expected fallback warning for deep parens, got {cands}, {warns}"

    deep_unary = "$=" + "-" * 3000 + "1"
    val, warn = expr.eval_modifier_expr(deep_unary, {}, {})
    assert val is None and warn, f"Expected modifier warning for deep unary, got {val}, {warn}"
    print("  PASS deep_expr_recursion_guard")


def test_deep_lenient_json_guard() -> None:
    deep_arr = "[" * 3000 + "]" * 3000
    try:
        lenient_json.parse(deep_arr)
        raise AssertionError("Expected JbeamError for deeply nested array")
    except lenient_json.JbeamError as exc:
        assert "depth" in str(exc).lower(), f"Unexpected error message: {exc}"

    deep_obj = '{"a":' * 3000 + "1" + "}" * 3000
    try:
        lenient_json.parse(deep_obj)
        raise AssertionError("Expected JbeamError for deeply nested object")
    except lenient_json.JbeamError as exc:
        assert "depth" in str(exc).lower(), f"Unexpected error message: {exc}"
    print("  PASS deep_lenient_json_guard")


def main() -> None:
    test_deep_expr_recursion_guard()
    test_deep_lenient_json_guard()
    test_random_candidate_cap()
    print("PASS: All #46 jbeam expression and lenient JSON guard tests passed!")


if __name__ == "__main__":
    main()
