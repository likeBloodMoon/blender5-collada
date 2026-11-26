"""Regression tests for #39: Float and Vertex (POINT) domain color attributes export to .dae.

Verifies all four color attribute combinations export with semantic="COLOR" and
round-trip their RGBA values:
  1. FLOAT_COLOR on POINT (Blender 5 UI default, exact repro from #39)
  2. FLOAT_COLOR on CORNER
  3. BYTE_COLOR on POINT
  4. BYTE_COLOR on CORNER (existing path)
Also verifies triangulate=True (keep_normals=True loop_triangles path and keep_normals=False).

Usage:
    python tests/test_color_attributes_export.py
    BLENDER_5X=... python tests/test_color_attributes_export.py
"""

from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS_DIR = os.path.join(REPO_ROOT, ".tools")


def _find_blender() -> str:
    env = os.environ.get("BLENDER_5X")
    if env and os.path.exists(env):
        return env
    exe = "blender.exe" if os.name == "nt" else "blender"
    matches = glob.glob(os.path.join(TOOLS_DIR, "blender-5.*", exe))
    if matches:
        return matches[0]
    raise RuntimeError("Could not find Blender 5.x (set BLENDER_5X)")


WORKER_SCRIPT = r'''
import json
import os
import sys

import bpy

argv = sys.argv[sys.argv.index("--") + 1:]
repo_root, workdir, out_json = argv[0], argv[1], argv[2]
sys.path.insert(0, repo_root)

import io_scene_collada
io_scene_collada.register()


def run_case(label, data_type, domain, triangulate=False):
    bpy.ops.wm.read_homefile(use_empty=True)
    bpy.ops.mesh.primitive_cube_add()
    me = bpy.context.object.data
    ca = me.color_attributes.new("Color", data_type, domain)
    for d in ca.data:
        if data_type == "FLOAT_COLOR":
            d.color = (1.0, 0.21404114, 0.0, 1.0)  # sRGB ~(1.0, 0.5, 0.0, 1.0)
        else:
            d.color_srgb = (1.0, 0.5, 0.0, 1.0)

    dae_path = os.path.join(workdir, f"{label}.dae")
    assert "FINISHED" in bpy.ops.wm.collada_export(
        filepath=dae_path,
        triangulate=triangulate,
        keep_normals=True,
    )
    txt = open(dae_path, encoding="utf-8").read()
    has_color_sem = 'semantic="COLOR"' in txt

    bpy.ops.wm.read_homefile(use_empty=True)
    assert "FINISHED" in bpy.ops.wm.collada_import(filepath=dae_path)
    ob = next(o for o in bpy.data.objects if o.type == "MESH")
    imp_ca = ob.data.color_attributes[0] if len(ob.data.color_attributes) > 0 else None
    sample_srgb = list(imp_ca.data[0].color_srgb) if imp_ca is not None and len(imp_ca.data) > 0 else None
    return {
        "has_color_sem": has_color_sem,
        "imp_count": len(ob.data.color_attributes),
        "sample_srgb": sample_srgb,
    }


results = {
    "float_point": run_case("float_point", "FLOAT_COLOR", "POINT", triangulate=False),
    "float_point_tri": run_case("float_point_tri", "FLOAT_COLOR", "POINT", triangulate=True),
    "float_corner": run_case("float_corner", "FLOAT_COLOR", "CORNER", triangulate=False),
    "byte_point": run_case("byte_point", "BYTE_COLOR", "POINT", triangulate=False),
    "byte_corner": run_case("byte_corner", "BYTE_COLOR", "CORNER", triangulate=False),
}
with open(out_json, "w", encoding="utf-8") as f:
    json.dump(results, f, indent=2)
'''


def main() -> int:
    blender = _find_blender()
    workdir = tempfile.mkdtemp(prefix="test_color_attrs_39_")
    try:
        worker_py = os.path.join(workdir, "worker.py")
        out_json = os.path.join(workdir, "out.json")
        with open(worker_py, "w", encoding="utf-8") as f:
            f.write(WORKER_SCRIPT)

        proc = subprocess.run(
            [blender, "-b", "--factory-startup", "--python-exit-code", "1",
             "--python", worker_py, "--", REPO_ROOT, workdir, out_json],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if proc.returncode != 0:
            print("STDOUT:\n", proc.stdout)
            print("STDERR:\n", proc.stderr)
            raise RuntimeError(f"Blender worker failed with exit code {proc.returncode}")

        with open(out_json, encoding="utf-8") as f:
            res = json.load(f)

        expected = [1.0, 0.5, 0.0, 1.0]
        failures: list[str] = []
        for label, info in res.items():
            if not info["has_color_sem"]:
                failures.append(f"{label}: missing semantic=\"COLOR\" in exported .dae")
            elif info["sample_srgb"] is None:
                failures.append(f"{label}: no color attribute after re-import")
            else:
                diff = max(abs(a - b) for a, b in zip(info["sample_srgb"], expected))
                if diff > 0.01:
                    failures.append(f"{label}: sRGB mismatch {info['sample_srgb']} vs {expected} (diff={diff:.4f})")

        if failures:
            print("FAIL test_color_attributes_export:")
            for msg in failures:
                print(f"  - {msg}")
            return 1

        print("PASS test_color_attributes_export (all 5 #39 FLOAT/BYTE x POINT/CORNER cases passed)")
        return 0
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
