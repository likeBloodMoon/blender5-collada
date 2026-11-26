"""Synthetic regression test for BeamNG jbeam glowMap light material resolution.

Verifies (using purely synthetic zips, no game files):
  1. glowMap entries are collected with precedence: active config's parts first,
     then all jbeam files in the vehicle zip, then common.zip.
  2. Placeholder material names (covet_lowbeam, covet_headlightglass, covet_taillight,
     covet_signal_L, common_beacon) keep their placeholder Blender material names so
     .dae export writes game-expected names, and store the mapped real material in
     the custom property 'beamng_glow_source'.
  3. Lights='OFF' (default) resolves each placeholder to its 'off' material;
     Lights='ON' resolves to its 'on' material.
  4. Works in both Full PBR and Base color only modes.

Usage:
    python tests/test_beamng_glowmap.py
    BLENDER_5X=... python tests/test_beamng_glowmap.py
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
    w, h = 4, 4
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
    lib_effects = []
    lib_materials = []
    tri_blocks = []
    bind_mats = []
    coords = []
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
        bind_mats.append(f'              <instance_material symbol="{mid}" target="#{mid}"/>')
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


def _build_fixtures(workdir: str) -> str:
    car_zip = os.path.join(workdir, "glowcar.zip")
    common_zip = os.path.join(workdir, "common.zip")

    # Main active part defines glowMap for covet_lowbeam & covet_headlightglass (wins over unused_part!)
    main_jbeam = json.dumps({
        "glowcar": {
            "slotType": "main",
            "flexbodies": [
                ["mesh", "[group]:"],
                ["lights_mesh", ["body"]],
            ],
            "glowMap": {
                "covet_lowbeam": {
                    "simpleFunction": {"lowbeam": 0.49},
                    "off": "covet_lights",
                    "on": "covet_lights_on",
                },
                "covet_headlightglass": {
                    "simpleFunction": {"lowbeam": 0.49},
                    "off": "covet_glass",
                    "on": "covet_glass_on_intense",
                },
                "covet_abs": {
                    "simpleFunction": {"absActive": 1},
                    "off": "invis",
                    "on": "covet_lights_on",
                },
            },
        },
        # Unused part in same vehicle zip: defines covet_taillight & covet_signal_L,
        # and tries to override covet_lowbeam (active config part must win!)
        "glowcar_unused_trim": {
            "slotType": "glowcar_trim",
            "glowMap": {
                "covet_lowbeam": {"off": "wrong_lights", "on": "wrong_lights_on"},
                "covet_taillight": {"off": "covet_lights", "on": "covet_lights_on"},
                "covet_signal_L": {"off": "covet_lights", "on": "covet_lights_on"},
            },
        },
    })
    pc = json.dumps({
        "format": 2,
        "parts": {"main": "glowcar"},
    })
    car_mats = json.dumps({
        "covet_lights": {
            "name": "covet_lights",
            "mapTo": "covet_lights",
            "version": 1.5,
            "Stages": [{"baseColorMap": "vehicles/glowcar/covet_lights_d.png"}],
        },
        "covet_lights_on": {
            "name": "covet_lights_on",
            "mapTo": "covet_lights_on",
            "version": 1.5,
            "Stages": [{
                "baseColorMap": "vehicles/glowcar/covet_lights_on_d.png",
                "emissiveMap": "vehicles/glowcar/covet_lights_on_d.png",
                "emissiveFactor": [1.0, 1.0, 1.0],
            }],
        },
        "covet_glass": {
            "name": "covet_glass",
            "mapTo": "covet_glass",
            "version": 1.5,
            "translucent": True,
            "Stages": [{
                "baseColorMap": "vehicles/glowcar/covet_glass_d.png",
                "opacityFactor": 0.3,
            }],
        },
        "covet_glass_on_intense": {
            "name": "covet_glass_on_intense",
            "mapTo": "covet_glass_on_intense",
            "version": 1.5,
            "translucent": True,
            "Stages": [{
                "baseColorMap": "vehicles/glowcar/covet_glass_on_d.png",
                "opacityFactor": 0.5,
                "emissiveFactor": [1.0, 0.9, 0.7],
            }],
        },
        "wrong_lights": {
            "name": "wrong_lights",
            "mapTo": "wrong_lights",
            "version": 1.5,
            "Stages": [{"baseColorMap": "vehicles/glowcar/wrong.png"}],
        },
    })
    dae = _make_dae(
        "lights_mesh",
        ["covet_lowbeam", "covet_headlightglass", "covet_taillight", "covet_signal_L", "common_beacon", "covet_abs"],
    )

    with zipfile.ZipFile(car_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("vehicles/glowcar/glowcar.jbeam", main_jbeam)
        zf.writestr("vehicles/glowcar/base.pc", pc)
        zf.writestr("vehicles/glowcar/main.materials.json", car_mats)
        zf.writestr("vehicles/glowcar/glowcar.dae", dae)
        zf.writestr("vehicles/glowcar/covet_lights_d.png", _make_png((200, 200, 200, 255)))
        zf.writestr("vehicles/glowcar/covet_lights_on_d.png", _make_png((255, 240, 180, 255)))
        zf.writestr("vehicles/glowcar/covet_glass_d.png", _make_png((180, 210, 230, 255)))
        zf.writestr("vehicles/glowcar/covet_glass_on_d.png", _make_png((255, 255, 220, 255)))
        zf.writestr("vehicles/glowcar/wrong.png", _make_png((255, 0, 255, 255)))

    common_jbeam = json.dumps({
        "common_beacon_part": {
            "slotType": "beacon",
            "glowMap": {
                "common_beacon": {"off": "beacon_off_mat", "on": "beacon_on_mat"},
            },
        }
    })
    common_mats = json.dumps({
        "beacon_off_mat": {
            "name": "beacon_off_mat",
            "mapTo": "beacon_off_mat",
            "version": 1.5,
            "Stages": [{"baseColorMap": "vehicles/common/beacon_off.png"}],
        },
        "beacon_on_mat": {
            "name": "beacon_on_mat",
            "mapTo": "beacon_on_mat",
            "version": 1.5,
            "Stages": [{"baseColorMap": "vehicles/common/beacon_on.png"}],
        },
    })
    with zipfile.ZipFile(common_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("vehicles/common/beacon.jbeam", common_jbeam)
        zf.writestr("vehicles/common/main.materials.json", common_mats)
        zf.writestr("vehicles/common/beacon_off.png", _make_png((100, 100, 100, 255)))
        zf.writestr("vehicles/common/beacon_on.png", _make_png((255, 160, 0, 255)))

    return car_zip


WORKER_SCRIPT = r'''
import json
import sys

import bpy

argv = sys.argv[sys.argv.index("--") + 1:]
repo_root, car_zip, out_json = argv[0], argv[1], argv[2]
sys.path.insert(0, repo_root)

from io_scene_collada.beamng.picker import execute_beamng_import
from io_scene_collada.beamng.source import clear_source_cache


def inspect_mat(name):
    ma = bpy.data.materials.get(name)
    if ma is None:
        return None
    bc_img = None
    has_group = False
    has_bsdf = False
    alpha_val = None
    for n in ma.node_tree.nodes:
        if n.type == "GROUP":
            has_group = True
            sock = n.inputs.get("Base Color")
            if sock and sock.is_linked and sock.links[0].from_node.type == "TEX_IMAGE":
                bc_img = sock.links[0].from_node.image.name
            if "Alpha" in n.inputs:
                alpha_val = float(n.inputs["Alpha"].default_value)
        elif n.type == "BSDF_PRINCIPLED":
            has_bsdf = True
            sock = n.inputs.get("Base Color")
            if sock and sock.is_linked and sock.links[0].from_node.type == "TEX_IMAGE":
                bc_img = sock.links[0].from_node.image.name
            if "Alpha" in n.inputs:
                alpha_val = float(n.inputs["Alpha"].default_value)
    return {
        "name": ma.name,
        "glow_source": ma.get("beamng_glow_source"),
        "bc_img": bc_img,
        "has_group": has_group,
        "has_bsdf": has_bsdf,
        "alpha": alpha_val,
        "render_method": getattr(ma, "surface_render_method", None),
        "backface_culling": ma.use_backface_culling,
    }


def run_case(mat_mode, lights_mode):
    clear_source_cache()
    bpy.ops.wm.read_homefile(use_empty=True)
    ok, _s, warns, errs = execute_beamng_import(
        bpy.context,
        filepath=car_zip,
        cdae_mode=False,
        beamng_config="base",
        beamng_parts_mode="CONFIG",
        use_common=True,
        beamng_materials=mat_mode,
        beamng_lights=lights_mode,
    )
    return {
        "ok": ok,
        "errors": errs,
        "lowbeam": inspect_mat("covet_lowbeam"),
        "glass": inspect_mat("covet_headlightglass"),
        "taillight": inspect_mat("covet_taillight"),
        "signal": inspect_mat("covet_signal_L"),
        "beacon": inspect_mat("common_beacon"),
        "invis_lamp": inspect_mat("covet_abs"),
        "warnings": warns,
    }


out = {
    "pbr_off": run_case("PBR", "OFF"),
    "pbr_on": run_case("PBR", "ON"),
    "bc_off": run_case("BASE_COLOR", "OFF"),
}
with open(out_json, "w", encoding="utf-8") as f:
    json.dump(out, f, indent=2)
'''


def main() -> int:
    blender = _find_blender()
    workdir = tempfile.mkdtemp(prefix="test_beamng_glowmap_")
    try:
        car_zip = _build_fixtures(workdir)
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

        failures: list[str] = []

        # 1. PBR + Lights OFF
        po = data["pbr_off"]
        if not po["ok"] or po["errors"]:
            failures.append(f"pbr_off failed: {po['errors']}")
        for key, exp_src, exp_img in (
            ("lowbeam", "covet_lights", "covet_lights_d.png"),
            ("glass", "covet_glass", "covet_glass_d.png"),
            ("taillight", "covet_lights", "covet_lights_d.png"),
            ("signal", "covet_lights", "covet_lights_d.png"),
            ("beacon", "beacon_off_mat", "beacon_off.png"),
        ):
            m = po[key]
            if m is None or m["glow_source"] != exp_src or m["bc_img"] != exp_img:
                failures.append(f"pbr_off[{key}] mismatch: expected src={exp_src} img={exp_img}, got {m}")
        if po["glass"]["render_method"] != "BLENDED" or po["glass"]["backface_culling"] is not False:
            failures.append(f"pbr_off[glass] should be BLENDED + no backface culling: {po['glass']}")
        inv_po = po["invis_lamp"]
        if inv_po is None or inv_po["glow_source"] != "invis" or inv_po["alpha"] != 0.0 or inv_po["render_method"] != "BLENDED":
            failures.append(f"pbr_off[invis_lamp] should be fully transparent (alpha=0, BLENDED): {inv_po}")

        # 2. PBR + Lights ON
        pon = data["pbr_on"]
        if not pon["ok"] or pon["errors"]:
            failures.append(f"pbr_on failed: {pon['errors']}")
        for key, exp_src, exp_img in (
            ("lowbeam", "covet_lights_on", "covet_lights_on_d.png"),
            ("glass", "covet_glass_on_intense", "covet_glass_on_d.png"),
            ("beacon", "beacon_on_mat", "beacon_on.png"),
            ("invis_lamp", "covet_lights_on", "covet_lights_on_d.png"),
        ):
            m = pon[key]
            if m is None or m["glow_source"] != exp_src or m["bc_img"] != exp_img:
                failures.append(f"pbr_on[{key}] mismatch: expected src={exp_src} img={exp_img}, got {m}")

        # 3. BASE_COLOR + Lights OFF
        bc = data["bc_off"]
        if not bc["ok"] or bc["errors"]:
            failures.append(f"bc_off failed: {bc['errors']}")
        for key, exp_src, exp_img in (
            ("lowbeam", "covet_lights", "covet_lights_d.png"),
            ("glass", "covet_glass", "covet_glass_d.png"),
            ("taillight", "covet_lights", "covet_lights_d.png"),
            ("signal", "covet_lights", "covet_lights_d.png"),
            ("beacon", "beacon_off_mat", "beacon_off.png"),
        ):
            m = bc[key]
            if m is None or m["glow_source"] != exp_src or m["bc_img"] != exp_img or m["has_group"] or not m["has_bsdf"]:
                failures.append(f"bc_off[{key}] mismatch: expected src={exp_src} img={exp_img} BSDF, got {m}")
        inv_bc = bc["invis_lamp"]
        if inv_bc is None or inv_bc["glow_source"] != "invis" or inv_bc["alpha"] != 0.0 or inv_bc["render_method"] != "BLENDED":
            failures.append(f"bc_off[invis_lamp] should be fully transparent (alpha=0, BLENDED): {inv_bc}")

        if failures:
            print("FAIL test_beamng_glowmap:")
            for msg in failures:
                print(f"  - {msg}")
            return 1

        print("PASS test_beamng_glowmap (all glowMap precedence, OFF/ON, PBR & BASE_COLOR checks passed)")
        return 0
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
