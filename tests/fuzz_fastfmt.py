"""Fuzz & parity test for native/fastfmt.cpp vs pure-Python xml_writer / NumPy.

Tests:
  - 10,000,000 random float32 values + edge cases (+/-0, NaN, +/-inf, 1e-4, 9.9999e6,
    1e7, 3.4e38, 1.4e-45, denormals) comparing native fmt_f32_array_mt against Python.
  - int32 formatting (fmt_i32_array_mt) and parsing (parse_f32, parse_i32).
"""
from __future__ import annotations

import importlib.util
import os
import sys
import time

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_mod(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


_nl = _load_mod("_native_loader", os.path.join(ROOT, "io_scene_collada", "_native_loader.py"))
get_native_lib = _nl.get_native_lib
native_fmt_f32_array = _nl.native_fmt_f32_array
native_fmt_i32_array = _nl.native_fmt_i32_array
native_parse_f32 = _nl.native_parse_f32
native_parse_i32 = _nl.native_parse_i32


def _fmt_f32_sanitized(v: float) -> str:
    if v == 0.0:
        return "0"
    av = -v if v < 0.0 else v
    if av < 1e-4 or av >= 1e7:
        s = f"{v:.6g}"
        if "e-0" in s:
            return s.replace("e-0", "e-")
        if "e+0" in s:
            return s.replace("e+0", "e+")
        return s
    return f"{v:.7g}"


def py_fmt_f32_array(arr: np.ndarray) -> str:
    a = np.asarray(arr, dtype=np.float32).ravel()
    if a.size == 0:
        return ""
    if not np.isfinite(a).all():
        a = np.where(np.isfinite(a), a, np.float32(0.0))
    if (a == 0.0).any():
        a = a.copy()
        a[a == 0.0] = 0.0
    return " ".join(map(_fmt_f32_sanitized, a.tolist()))


def main() -> int:
    lib = get_native_lib()
    if lib is None:
        print("ERROR: native fastfmt library not loaded (build it into io_scene_collada/_native/ first)")
        return 1

    # 1. Edge cases
    edges = np.array(
        [
            0.0,
            -0.0,
            float("nan"),
            float("inf"),
            float("-inf"),
            1e-4,
            -1e-4,
            9.99999e-5,
            9.9999e6,
            9.999999e6,
            1e7,
            -1e7,
            3.4028235e38,
            -3.4028235e38,
            1.4e-45,
            -1.4e-45,
            1.1754942e-38,  # largest subnormal
            1.1754944e-38,  # smallest normal
            1.0,
            -1.0,
            0.1234567,
            123456.7,
        ],
        dtype=np.float32,
    )
    nat_e = native_fmt_f32_array(edges)
    py_e = py_fmt_f32_array(edges)
    if nat_e != py_e:
        print(f"EDGE MISMATCH:\n  NAT: {nat_e}\n  PY : {py_e}")
        return 1
    print(f"[PASS] Edge cases ({len(edges)} values) exact byte match")

    # 2. 10,000,000 random float32 bit patterns (in 1M chunks)
    rng = np.random.default_rng(42)
    total = 10_000_000
    chunk = 1_000_000
    t_nat = 0.0
    t_py = 0.0
    for c in range(total // chunk):
        bits = rng.integers(0, 2**32, size=chunk, dtype=np.uint32)
        arr = bits.view(np.float32)
        t0 = time.perf_counter()
        s_nat = native_fmt_f32_array(arr)
        t_nat += time.perf_counter() - t0
        t1 = time.perf_counter()
        s_py = py_fmt_f32_array(arr)
        t_py += time.perf_counter() - t1
        if s_nat != s_py:
            ntoks = s_nat.split(" ")
            ptoks = s_py.split(" ")
            for i, (a, b) in enumerate(zip(ntoks, ptoks)):
                if a != b:
                    print(f"FUZZ MISMATCH at chunk {c} idx {i} val={arr[i]!r} (bits=0x{int(bits[i]):08x}): nat={a!r} vs py={b!r}")
                    return 1
            print(f"FUZZ LENGTH MISMATCH at chunk {c}: {len(ntoks)} vs {len(ptoks)}")
            return 1
    print(f"[PASS] 10,000,000 random float32 exact byte match (native={t_nat:.2f}s vs python={t_py:.2f}s, {t_py/max(t_nat,1e-6):.1f}x faster)")

    # 3. parse_f32 & parse_i32 + fmt_i32_array
    f_arr = rng.uniform(-1000.0, 1000.0, size=500_000).astype(np.float32)
    f_txt = native_fmt_f32_array(f_arr)
    f_parsed = native_parse_f32(f_txt)
    f_ref = np.fromstring(f_txt, sep=" ", dtype=np.float32)
    assert f_parsed.shape == f_ref.shape and np.allclose(f_parsed, f_ref, rtol=1e-6, atol=1e-6)
    print(f"[PASS] parse_f32 ({len(f_parsed)} floats)")

    i_arr = rng.integers(-100_000, 10_000_000, size=1_000_000, dtype=np.int32)
    i_nat = native_fmt_i32_array(i_arr)
    i_py = " ".join(map(str, i_arr.tolist()))
    assert i_nat == i_py
    i_parsed = native_parse_i32(i_nat)
    assert np.array_equal(i_parsed, i_arr)
    print(f"[PASS] fmt_i32_array & parse_i32 ({len(i_parsed)} ints)")

    # 4. Leading '+' and ok==0 fallback on invalid tokens
    fp_plus = native_parse_f32("+1 +0 +2.5 -3.5  \n")
    assert fp_plus is not None and np.allclose(fp_plus, [1.0, 0.0, 2.5, -3.5])
    ip_plus = native_parse_i32("+0 +1 +42 -7  \t")
    assert ip_plus is not None and np.array_equal(ip_plus, [0, 1, 42, -7])
    assert native_parse_f32("1 2 x 4") is None
    assert native_parse_f32("1.#QNAN 2") is None
    assert native_parse_f32("++1 2") is None
    assert native_parse_i32("0 1 x 3") is None
    assert native_parse_i32("0 1.5 2") is None
    print("[PASS] parse_f32 / parse_i32 leading '+' and invalid-token fallback")
    return 0


if __name__ == "__main__":
    sys.exit(main())
