"""Regression test for #51: BeamNG vehicle import material and texture resolution.

Tests with a purely synthetic car zip + common.zip (no game files):
  1. Material defined in a non-main *.materials.json is textured.
  2. Texture that only exists in common.zip is resolved for a vehicle part.
  3. Material name shared by a car part and a common part is reused (one material,
     no .001 copy) and textured from common.zip.
  4. Vehicle *.materials.json wins over common.zip on a material name clash.
  5. Texture named in *.materials.json but missing from both zips produces
     exactly one summary warning per import (with count and name).
  6. A pre-existing unrelated material with the same name in the user's .blend
     is never reused by the import, while the import's own newly created
     material is still shared across car and common parts.

Usage:
    python tests/test_beamng_materials_51.py
    BLENDER_5X=... python tests/test_beamng_materials_51.py
"""

from __future__ import annotations

import glob
import json
import os
import shutil
import struct
import subprocess
import tempfile
import zipfile
import zlib

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


def _make_png(rgba: tuple[int, int, int, int]) -> bytes:
    """Generate a minimal valid 2x2 RGBA PNG."""
    w, h = 2, 2
    raw = b"".join(b"\x00" + bytes(rgba) * w for _ in range(h))

    def _chunk(tag: bytes, payload: bytes) -> bytes:
        crc = zlib.crc32(tag + payload) & 0xFFFFFFFF
        return struct.pack(">I", len(payload)) + tag + payload + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", zlib.compress(raw))
        + _chunk(b"IEND", b"")
    )


def _make_dae(node_name: str, mat_names: list[str]) -> str:
    """Build a minimal valid COLLADA 1.4.1 document with one non-overlapping triangle per material."""
    lib_effects = []
    lib_materials = []
    tri_blocks = []
    bind_mats = []
    coords: list[str] = []
    for i, mname in enumerate(mat_names):
        eid = f"{mname}-effect"
        mid = f"{mname}-material"
        x0 = i * 2
        coords.append(f"{x0} 0 0 {x0 + 1} 0 0 {x0} 1 0")
        v0 = i * 3
        lib_effects.append(
            f'    <effect id="{eid}"><profile_COMMON><technique sid="common">'
            f"<lambert><diffuse><color>0.5 0.5 0.5 1</color></diffuse></lambert>"
            f"</technique></profile_COMMON></effect>"
        )
        lib_materials.append(
            f'    <material id="{mid}" name="{mname}"><instance_effect url="#{eid}"/></material>'
        )
        tri_blocks.append(
            f'        <triangles material="{mid}" count="1">'
            f'<input semantic="VERTEX" source="#{node_name}-verts" offset="0"/>'
            f"<p>{v0} {v0 + 1} {v0 + 2}</p></triangles>"
        )
        bind_mats.append(
            f'              <instance_material symbol="{mid}" target="#{mid}"/>'
        )
    n_verts = len(mat_names) * 3
    coords_str = " ".join(coords)

    return "\n".join([
        '<?xml version="1.0" encoding="utf-8"?>',
        '<COLLADA xmlns="http://www.collada.org/2005/11/COLLADASchema" version="1.4.1">',
        "  <asset><up_axis>Z_UP</up_axis></asset>",
        "  <library_effects>",
        *lib_effects,
        "  </library_effects>",
        "  <library_materials>",
        *lib_materials,
        "  </library_materials>",
        "  <library_geometries>",
        f'    <geometry id="{node_name}-mesh" name="{node_name}">',
        "      <mesh>",
        f'        <source id="{node_name}-pos">',
        f'          <float_array id="{node_name}-pos-arr" count="{n_verts * 3}">{coords_str}</float_array>',
        "          <technique_common>",
        f'            <accessor source="#{node_name}-pos-arr" count="{n_verts}" stride="3">',
        '              <param name="X" type="float"/><param name="Y" type="float"/><param name="Z" type="float"/>',
        "            </accessor>",
        "          </technique_common>",
        "        </source>",
        f'        <vertices id="{node_name}-verts">',
        f'          <input semantic="POSITION" source="#{node_name}-pos"/>',
        "        </vertices>",
        *tri_blocks,
        "      </mesh>",
        "    </geometry>",
        "  </library_geometries>",
        "  <library_visual_scenes>",
        '    <visual_scene id="Scene" name="Scene">',
        f'      <node id="{node_name}" name="{node_name}" type="NODE">',
        f'        <instance_geometry url="#{node_name}-mesh">',
        "          <bind_material><technique_common>",
        *bind_mats,
        "          </technique_common></bind_material>",
        "        </instance_geometry>",
        "      </node>",
        "    </visual_scene>",
        "  </library_visual_scenes>",
        '  <scene><instance_visual_scene url="#Scene"/></scene>',
        "</COLLADA>",
    ])


def _build_fixtures(workdir: str) -> tuple[str, str]:
    car_zip = os.path.join(workdir, "testcar.zip")
    common_zip = os.path.join(workdir, "common.zip")

    car_jbeam = json.dumps({
        "testcar": {
            "slotType": "main",
            "slots": [
                ["type", "default", "description"],
                ["testcar_wheel", "common_wheel_part", "Wheel"],
            ],
            "flexbodies": [
                ["mesh", "[group]:"],
                ["car_body", ["body"]],
            ],
        }
    })
    car_pc = json.dumps({
        "format": 2,
        "parts": {
            "main": "testcar",
            "testcar_wheel": "common_wheel_part",
        },
    })
    car_main_mats = json.dumps({
        "mat_common_tex": {
            "name": "mat_common_tex",
            "mapTo": "mat_common_tex",
            "Stages": [{"baseColorMap": "vehicles/common/textures/chrome_d.png"}],
        },
        "mat_missing": {
            "name": "mat_missing",
            "mapTo": "mat_missing",
            "Stages": [{"baseColorMap": "vehicles/testcar/does_not_exist.png"}],
        },
    })
    car_extra_mats = json.dumps({
        "mat_nonmain": {
            "name": "mat_nonmain",
            "mapTo": "mat_nonmain",
            "Stages": [{"baseColorMap": "vehicles/testcar/extra/paint_d.png"}],
        },
        "mat_clash": {
            "name": "mat_clash",
            "mapTo": "mat_clash",
            "Stages": [{"baseColorMap": "vehicles/testcar/veh_win.png"}],
        },
    })
    car_dae = _make_dae(
        "car_body",
        ["mat_nonmain", "mat_common_tex", "mat_shared", "mat_missing", "mat_clash"],
    )

    with zipfile.ZipFile(car_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("vehicles/testcar/testcar.jbeam", car_jbeam)
        zf.writestr("vehicles/testcar/base.pc", car_pc)
        zf.writestr("vehicles/testcar/main.materials.json", car_main_mats)
        zf.writestr("vehicles/testcar/extra/paint.materials.json", car_extra_mats)
        zf.writestr("vehicles/testcar/testcar.dae", car_dae)
        zf.writestr("vehicles/testcar/extra/paint_d.png", _make_png((255, 0, 0, 255)))
        zf.writestr("vehicles/testcar/veh_win.png", _make_png((0, 255, 0, 255)))

    common_jbeam = json.dumps({
        "common_wheel_part": {
            "slotType": "testcar_wheel",
            "flexbodies": [
                ["mesh", "[group]:"],
                ["common_wheel", ["wheel"]],
            ],
        }
    })
    common_mats = json.dumps({
        "mat_shared": {
            "name": "mat_shared",
            "mapTo": "mat_shared",
            "Stages": [{"baseColorMap": "vehicles/common/textures/shared_d.png"}],
        },
        "mat_clash": {
            "name": "mat_clash",
            "mapTo": "mat_clash",
            "Stages": [{"baseColorMap": "vehicles/common/textures/common_lose.png"}],
        },
    })
    common_dae = _make_dae("common_wheel", ["mat_shared"])

    with zipfile.ZipFile(common_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("vehicles/common/wheel.jbeam", common_jbeam)
        zf.writestr("vehicles/common/common.materials.json", common_mats)
        zf.writestr("vehicles/common/wheel.dae", common_dae)
        zf.writestr("vehicles/common/textures/chrome_d.png", _make_png((0, 0, 255, 255)))
        zf.writestr("vehicles/common/textures/shared_d.png", _make_png((255, 255, 0, 255)))
        zf.writestr("vehicles/common/textures/common_lose.png", _make_png((128, 0, 128, 255)))

    return car_zip, common_zip


WORKER_SCRIPT = r'''
import json
import os
import sys

import bpy

argv = sys.argv[sys.argv.index("--") + 1:]
repo_root, car_zip, out_json = argv[0], argv[1], argv[2]
sys.path.insert(0, repo_root)

from io_scene_collada.beamng.picker import execute_beamng_import
from io_scene_collada.beamng.source import clear_source_cache


def base_color_image_name(ma):
    if ma is None or ma.node_tree is None:
        return None
    for node in ma.node_tree.nodes:
        if node.type in ("BSDF_PRINCIPLED", "GROUP"):
            sock = node.inputs.get("Base Color")
            if sock is not None and sock.is_linked:
                src_node = sock.links[0].from_node
                if src_node.type == "TEX_IMAGE" and src_node.image is not None:
                    return src_node.image.name
    return None


def obj_mat_by_prefix(ob, prefix):
    if ob is None or ob.data is None:
        return None
    for ma in ob.data.materials:
        if ma is not None and (ma.name == prefix or ma.name.startswith(prefix + ".")):
            return ma
    return None


# ---- Case A: Clean scene ----
clear_source_cache()
bpy.ops.wm.read_homefile(use_empty=True)

ok_a, summary_a, warnings_a, errors_a = execute_beamng_import(
    bpy.context,
    filepath=car_zip,
    cdae_mode=False,
    beamng_config="base",
    beamng_parts_mode="CONFIG",
    use_common=True,
)

car_ob = bpy.data.objects.get("car_body")
wheel_ob = bpy.data.objects.get("common_wheel")

m_nonmain = obj_mat_by_prefix(car_ob, "mat_nonmain")
m_common_tex = obj_mat_by_prefix(car_ob, "mat_common_tex")
m_shared_car = obj_mat_by_prefix(car_ob, "mat_shared")
m_shared_wheel = obj_mat_by_prefix(wheel_ob, "mat_shared")
m_clash = obj_mat_by_prefix(car_ob, "mat_clash")
m_missing = obj_mat_by_prefix(car_ob, "mat_missing")

shared_mat_names_a = sorted(
    m.name for m in bpy.data.materials
    if m.name == "mat_shared" or m.name.startswith("mat_shared.")
)

case_a = {
    "ok": ok_a,
    "errors": errors_a,
    "warnings": warnings_a,
    "car_found": car_ob is not None,
    "wheel_found": wheel_ob is not None,
    "nonmain_tex": base_color_image_name(m_nonmain),
    "common_tex": base_color_image_name(m_common_tex),
    "shared_tex": base_color_image_name(m_shared_car),
    "shared_same_datablock": (m_shared_car is not None and m_shared_car == m_shared_wheel),
    "shared_mat_names": shared_mat_names_a,
    "clash_tex": base_color_image_name(m_clash),
    "missing_tex": base_color_image_name(m_missing),
}

# ---- Case B: Pre-existing unrelated user material named "mat_shared" ----
clear_source_cache()
bpy.ops.wm.read_homefile(use_empty=True)
user_mat = bpy.data.materials.new("mat_shared")
user_mat["user_owned"] = True

ok_b, summary_b, warnings_b, errors_b = execute_beamng_import(
    bpy.context,
    filepath=car_zip,
    cdae_mode=False,
    beamng_config="base",
    beamng_parts_mode="CONFIG",
    use_common=True,
)

car_ob_b = bpy.data.objects.get("car_body")
wheel_ob_b = bpy.data.objects.get("common_wheel")
m_shared_car_b = obj_mat_by_prefix(car_ob_b, "mat_shared")
m_shared_wheel_b = obj_mat_by_prefix(wheel_ob_b, "mat_shared")
shared_mat_names_b = sorted(
    m.name for m in bpy.data.materials
    if m.name == "mat_shared" or m.name.startswith("mat_shared.")
)

case_b = {
    "ok": ok_b,
    "errors": errors_b,
    "reused_user_mat": (m_shared_car_b == user_mat or m_shared_wheel_b == user_mat),
    "shared_same_datablock": (m_shared_car_b is not None and m_shared_car_b == m_shared_wheel_b),
    "shared_mat_names": shared_mat_names_b,
    "shared_tex": base_color_image_name(m_shared_car_b),
}

with open(out_json, "w", encoding="utf-8") as f:
    json.dump({"case_a": case_a, "case_b": case_b}, f, indent=2)
'''


def main() -> int:
    blender = _find_blender()
    workdir = tempfile.mkdtemp(prefix="test_beamng_materials_51_")
    try:
        car_zip, _common_zip = _build_fixtures(workdir)
        worker_py = os.path.join(workdir, "worker.py")
        out_json = os.path.join(workdir, "out.json")
        with open(worker_py, "w", encoding="utf-8") as f:
            f.write(WORKER_SCRIPT)

        proc = subprocess.run(
            [blender, "-b", "--factory-startup", "--python-exit-code", "1",
             "--python", worker_py, "--", REPO_ROOT, car_zip, out_json],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if proc.returncode != 0:
            print("STDOUT:\n", proc.stdout)
            print("STDERR:\n", proc.stderr)
            raise RuntimeError(f"Blender worker failed with exit code {proc.returncode}")

        with open(out_json, encoding="utf-8") as f:
            data = json.load(f)

        ca = data["case_a"]
        cb = data["case_b"]
        failures: list[str] = []

        if not ca["ok"] or ca["errors"]:
            failures.append(f"Case A import failed: ok={ca['ok']} errors={ca['errors']}")
        if not ca["car_found"] or not ca["wheel_found"]:
            failures.append(f"Case A missing objects: car={ca['car_found']} wheel={ca['wheel_found']}")
        if ca["nonmain_tex"] != "paint_d.png":
            failures.append(f"Non-main *.materials.json texture expected 'paint_d.png', got {ca['nonmain_tex']!r}")
        if ca["common_tex"] != "chrome_d.png":
            failures.append(f"common.zip texture expected 'chrome_d.png', got {ca['common_tex']!r}")
        if ca["shared_tex"] != "shared_d.png":
            failures.append(f"Shared material texture expected 'shared_d.png', got {ca['shared_tex']!r}")
        if not ca["shared_same_datablock"]:
            failures.append(f"Shared material was NOT reused across car and common parts: {ca['shared_mat_names']}")
        if ca["shared_mat_names"] != ["mat_shared"]:
            failures.append(f"Expected single material ['mat_shared'] with no .001 copy, got {ca['shared_mat_names']}")
        if ca["clash_tex"] != "veh_win.png":
            failures.append(f"Vehicle clash priority expected 'veh_win.png', got {ca['clash_tex']!r}")
        if ca["missing_tex"] is not None:
            failures.append(f"Missing texture material unexpectedly got texture {ca['missing_tex']!r}")

        missing_warns = [w for w in ca["warnings"] if "does_not_exist.png" in w]
        if len(missing_warns) != 1:
            failures.append(
                f"Expected exactly 1 warning naming 'does_not_exist.png', got {len(missing_warns)}: {ca['warnings']}"
            )

        if not cb["ok"] or cb["errors"]:
            failures.append(f"Case B import failed: ok={cb['ok']} errors={cb['errors']}")
        if cb["reused_user_mat"]:
            failures.append("Case B erroneously reused pre-existing user material 'mat_shared'")
        if not cb["shared_same_datablock"]:
            failures.append(f"Case B did not share newly created material across passes: {cb['shared_mat_names']}")
        if len(cb["shared_mat_names"]) != 2:
            failures.append(
                f"Case B expected exactly 2 materials (user + 1 shared import mat), got {cb['shared_mat_names']}"
            )
        if cb["shared_tex"] != "shared_d.png":
            failures.append(f"Case B shared material texture expected 'shared_d.png', got {cb['shared_tex']!r}")

        if failures:
            print("FAIL test_beamng_materials_51:")
            for msg in failures:
                print(f"  - {msg}")
            return 1

        print("PASS test_beamng_materials_51 (all 6 #51 checks passed)")
        print(f"  Missing-texture warning: {missing_warns[0]}")
        return 0
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
