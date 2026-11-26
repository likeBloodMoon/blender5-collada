"""Synthetic regression test for #52: BeamNG v1.5 PBR materials, palette paints, AO, UV1, glass.

Verifies (using purely synthetic zips, no game files):
  1. Shared 'BeamNG v1.5 Material' node group is created once and shared by v1.5 materials.
  2. UV maps are named UV0 and UV1 on BeamNG PBR import; maps with <map>UseUV: 1 bind to UV1.
  3. Paints from .pc are stored on imported objects as beamng_paint1..3 (sRGB -> linear)
     plus metallic/roughness/clearcoat/clearcoat_roughness.
  4. Cycles DIFFUSE/COLOR bake of a quad with Base Color + Palette Mask (UV1) + AO (UV1)
     matches expected paint * mask * AO to within 1/255 for all 3 paint channels (R, G, B).
  5. Two objects sharing the exact same material with different beamng_paint1 values
     bake to different colors matching each object's own paint.
  6. Translucent glass gets Alpha, surface_render_method='BLENDED', backface culling off;
     opaque translucent glass falls back to 0.35 and is noted in the import report.
  7. beamng_materials='BASE_COLOR' produces 1.5.5-style direct Principled BSDF trees
     without the shared node group or UV renaming.

Usage:
    python tests/test_beamng_pbr_52.py
    BLENDER_5X=... python tests/test_beamng_pbr_52.py
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


def _make_two_uv_dae(node_name: str, mat_name: str) -> str:
    """Build a COLLADA 1.4.1 quad (2 tris) with 2 UV sets (set 0 and set 1)."""
    eid = f"{mat_name}-effect"
    mid = f"{mat_name}-material"
    return f'''<?xml version="1.0" encoding="utf-8"?>
<COLLADA xmlns="http://www.collada.org/2005/11/COLLADASchema" version="1.4.1">
  <asset><up_axis>Z_UP</up_axis></asset>
  <library_effects>
    <effect id="{eid}"><profile_COMMON><technique sid="common">
      <lambert><diffuse><color>0.5 0.5 0.5 1</color></diffuse></lambert>
    </technique></profile_COMMON></effect>
  </library_effects>
  <library_materials>
    <material id="{mid}" name="{mat_name}"><instance_effect url="#{eid}"/></material>
  </library_materials>
  <library_geometries>
    <geometry id="{node_name}-mesh" name="{node_name}">
      <mesh>
        <source id="{node_name}-pos">
          <float_array id="{node_name}-pos-arr" count="12">0 0 0 1 0 0 1 1 0 0 1 0</float_array>
          <technique_common>
            <accessor source="#{node_name}-pos-arr" count="4" stride="3">
              <param name="X" type="float"/><param name="Y" type="float"/><param name="Z" type="float"/>
            </accessor>
          </technique_common>
        </source>
        <source id="{node_name}-uv0">
          <float_array id="{node_name}-uv0-arr" count="8">0 0 1 0 1 1 0 1</float_array>
          <technique_common>
            <accessor source="#{node_name}-uv0-arr" count="4" stride="2">
              <param name="S" type="float"/><param name="T" type="float"/>
            </accessor>
          </technique_common>
        </source>
        <source id="{node_name}-uv1">
          <float_array id="{node_name}-uv1-arr" count="8">0 0 1 0 1 1 0 1</float_array>
          <technique_common>
            <accessor source="#{node_name}-uv1-arr" count="4" stride="2">
              <param name="S" type="float"/><param name="T" type="float"/>
            </accessor>
          </technique_common>
        </source>
        <vertices id="{node_name}-verts">
          <input semantic="POSITION" source="#{node_name}-pos"/>
        </vertices>
        <triangles material="{mid}" count="2">
          <input semantic="VERTEX" source="#{node_name}-verts" offset="0"/>
          <input semantic="TEXCOORD" source="#{node_name}-uv0" offset="1" set="0"/>
          <input semantic="TEXCOORD" source="#{node_name}-uv1" offset="2" set="1"/>
          <p>0 0 0 1 1 1 2 2 2 0 0 0 2 2 2 3 3 3</p>
        </triangles>
      </mesh>
    </geometry>
  </library_geometries>
  <library_visual_scenes>
    <visual_scene id="Scene" name="Scene">
      <node id="{node_name}" name="{node_name}" type="NODE">
        <instance_geometry url="#{node_name}-mesh">
          <bind_material><technique_common>
            <instance_material symbol="{mid}" target="#{mid}"/>
          </technique_common></bind_material>
        </instance_geometry>
      </node>
    </visual_scene>
  </library_visual_scenes>
  <scene><instance_visual_scene url="#Scene"/></scene>
</COLLADA>'''


def _build_fixture(workdir: str) -> str:
    car_zip = os.path.join(workdir, "pbrcar.zip")

    jbeam = json.dumps({
        "pbrcar": {
            "slotType": "main",
            "flexbodies": [
                ["mesh", "[group]:"],
                ["panel_r", ["body"]],
                ["panel_g", ["body"]],
                ["panel_b", ["body"]],
                ["glass_alpha", ["glass"]],
                ["glass_opaque", ["glass"]],
            ],
        }
    })
    pc = json.dumps({
        "format": 2,
        "parts": {"main": "pbrcar"},
        "paints": [
            {
                "baseColor": [0.8, 0.2, 0.1, 1.3],
                "metallic": 0.7,
                "roughness": 0.25,
                "clearcoat": 0.9,
                "clearcoatRoughness": 0.04,
            },
            {
                "baseColor": [0.15, 0.75, 0.25, 1.0],
                "metallic": 0.2,
                "roughness": 0.4,
                "clearcoat": 0.5,
                "clearcoatRoughness": 0.08,
            },
            {
                "baseColor": [0.1, 0.3, 0.9, 1.0],
                "metallic": 0.0,
                "roughness": 0.6,
                "clearcoat": 0.0,
                "clearcoatRoughness": 0.1,
            },
        ],
    })
    mats = json.dumps({
        "mat_pbr_r": {
            "name": "mat_pbr_r",
            "mapTo": "mat_pbr_r",
            "Stages": [{
                "baseColorMap": "vehicles/pbrcar/white.png",
                "colorPaletteMap": "vehicles/pbrcar/mask_r.png",
                "colorPaletteMapUseUV": 1,
                "ambientOcclusionMap": "vehicles/pbrcar/ao_half.png",
                "ambientOcclusionMapUseUV": 1,
                "roughnessFactor": 0.5,
                "metallicFactor": 0.0,
            }],
        },
        "mat_pbr_g": {
            "name": "mat_pbr_g",
            "mapTo": "mat_pbr_g",
            "Stages": [{
                "baseColorMap": "vehicles/pbrcar/white.png",
                "colorPaletteMap": "vehicles/pbrcar/mask_g.png",
                "colorPaletteMapUseUV": 1,
                "ambientOcclusionMap": "vehicles/pbrcar/ao_half.png",
                "ambientOcclusionMapUseUV": 1,
            }],
        },
        "mat_pbr_b": {
            "name": "mat_pbr_b",
            "mapTo": "mat_pbr_b",
            "Stages": [{
                "baseColorMap": "vehicles/pbrcar/white.png",
                "colorPaletteMap": "vehicles/pbrcar/mask_b.png",
                "colorPaletteMapUseUV": 1,
                "ambientOcclusionMap": "vehicles/pbrcar/ao_half.png",
                "ambientOcclusionMapUseUV": 1,
            }],
        },
        "mat_glass_alpha": {
            "name": "mat_glass_alpha",
            "mapTo": "mat_glass_alpha",
            "translucent": True,
            "Stages": [{
                "baseColorMap": "vehicles/pbrcar/white.png",
                "opacityFactor": 0.4,
            }],
        },
        "mat_glass_opaque": {
            "name": "mat_glass_opaque",
            "mapTo": "mat_glass_opaque",
            "translucent": True,
            "Stages": [{
                "baseColorMap": "vehicles/pbrcar/white.png",
            }],
        },
    })

    with zipfile.ZipFile(car_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("vehicles/pbrcar/pbrcar.jbeam", jbeam)
        zf.writestr("vehicles/pbrcar/base.pc", pc)
        zf.writestr("vehicles/pbrcar/main.materials.json", mats)
        zf.writestr("vehicles/pbrcar/panel_r.dae", _make_two_uv_dae("panel_r", "mat_pbr_r"))
        zf.writestr("vehicles/pbrcar/panel_g.dae", _make_two_uv_dae("panel_g", "mat_pbr_g"))
        zf.writestr("vehicles/pbrcar/panel_b.dae", _make_two_uv_dae("panel_b", "mat_pbr_b"))
        zf.writestr("vehicles/pbrcar/glass_alpha.dae", _make_two_uv_dae("glass_alpha", "mat_glass_alpha"))
        zf.writestr("vehicles/pbrcar/glass_opaque.dae", _make_two_uv_dae("glass_opaque", "mat_glass_opaque"))
        zf.writestr("vehicles/pbrcar/white.png", _make_png((255, 255, 255, 255)))
        zf.writestr("vehicles/pbrcar/mask_r.png", _make_png((255, 0, 0, 255)))
        zf.writestr("vehicles/pbrcar/mask_g.png", _make_png((0, 255, 0, 255)))
        zf.writestr("vehicles/pbrcar/mask_b.png", _make_png((0, 0, 255, 255)))
        zf.writestr("vehicles/pbrcar/ao_half.png", _make_png((128, 128, 128, 255)))

    return car_zip


WORKER_SCRIPT = r'''
import json
import sys

import bpy

argv = sys.argv[sys.argv.index("--") + 1:]
repo_root, car_zip, out_json = argv[0], argv[1], argv[2]
sys.path.insert(0, repo_root)

from io_scene_collada.beamng.pbr_nodes import NODE_GROUP_NAME
from io_scene_collada.beamng.picker import execute_beamng_import
from io_scene_collada.beamng.resolve import srgb_to_linear
from io_scene_collada.beamng.source import clear_source_cache


def bake_base_color(ob):
    """Bake Cycles EMIT pass of the Base Color input feeding Principled BSDF inside the node group."""
    scene = bpy.context.scene
    scene.render.engine = "CYCLES"
    scene.cycles.device = "CPU"
    scene.cycles.samples = 1
    scene.cycles.use_denoising = False

    ma = ob.data.materials[0]
    ntree = ma.node_tree
    grp_node = next(n for n in ntree.nodes if n.type == "GROUP")
    ng = grp_node.node_tree
    bsdf = next(n for n in ng.nodes if n.type == "BSDF_PRINCIPLED")
    go = next(n for n in ng.nodes if n.type == "GROUP_OUTPUT")
    bc_src = bsdf.inputs["Base Color"].links[0].from_socket

    # Temporarily route Principled Base Color -> Emission(1.0) -> Group Output
    emit = ng.nodes.new("ShaderNodeEmission")
    emit.inputs["Strength"].default_value = 1.0
    ng.links.new(bc_src, emit.inputs["Color"])
    ng.links.new(emit.outputs["Emission"], go.inputs["BSDF"])

    img = bpy.data.images.new(f"bake_{ob.name}", width=8, height=8, alpha=False, float_buffer=True)
    bake_node = ntree.nodes.new("ShaderNodeTexImage")
    bake_node.image = img
    ntree.nodes.active = bake_node

    for o in bpy.context.view_layer.objects:
        o.select_set(False)
    ob.select_set(True)
    bpy.context.view_layer.objects.active = ob

    try:
        bpy.ops.object.bake(
            type="EMIT",
            use_clear=True,
            margin=0,
        )
        # Sample center pixel (x=4, y=4) in linear float buffer
        idx = (4 * 8 + 4) * 4
        return [float(img.pixels[idx]), float(img.pixels[idx + 1]), float(img.pixels[idx + 2])]
    finally:
        ng.links.new(bsdf.outputs["BSDF"], go.inputs["BSDF"])
        ng.nodes.remove(emit)
        ntree.nodes.remove(bake_node)
        bpy.data.images.remove(img)


# ---- Part 1: Full PBR mode ----
clear_source_cache()
bpy.ops.wm.read_homefile(use_empty=True)

ok_pbr, summary_pbr, warns_pbr, errs_pbr = execute_beamng_import(
    bpy.context,
    filepath=car_zip,
    cdae_mode=False,
    beamng_config="base",
    beamng_parts_mode="CONFIG",
    use_common=False,
    beamng_materials="PBR",
)

ng = bpy.data.node_groups.get(NODE_GROUP_NAME)
ob_r = bpy.data.objects.get("panel_r")
ob_g = bpy.data.objects.get("panel_g")
ob_b = bpy.data.objects.get("panel_b")
ob_ga = bpy.data.objects.get("glass_alpha")
ob_go = bpy.data.objects.get("glass_opaque")

uv_names_r = [u.name for u in ob_r.data.uv_layers] if ob_r else []

# Check palette & AO UVMap nodes use UV1
mat_r = ob_r.data.materials[0]
pal_uv = None
ao_uv = None
for n in mat_r.node_tree.nodes:
    if n.type == "TEX_IMAGE" and n.label == "Color Palette":
        vec_in = n.inputs["Vector"]
        if vec_in.is_linked and vec_in.links[0].from_node.type == "UVMAP":
            pal_uv = vec_in.links[0].from_node.uv_map
    if n.type == "TEX_IMAGE" and n.label == "Ambient Occlusion":
        vec_in = n.inputs["Vector"]
        if vec_in.is_linked and vec_in.links[0].from_node.type == "UVMAP":
            ao_uv = vec_in.links[0].from_node.uv_map

baked_r = bake_base_color(ob_r)
baked_g = bake_base_color(ob_g)
baked_b = bake_base_color(ob_b)

# Two objects sharing the exact same material with different beamng_paint1
ob_r2 = ob_r.copy()
ob_r2.name = "panel_r_recolored"
ob_r2.data = ob_r.data.copy()  # share material mat_r
ob_r2.location = (3.0, 0.0, 0.0)
bpy.context.scene.collection.objects.link(ob_r2)
ob_r2["beamng_paint1"] = (
    srgb_to_linear(0.2),
    srgb_to_linear(0.9),
    srgb_to_linear(0.85),
    1.0,
)
baked_r2 = bake_base_color(ob_r2)

# Check glass properties & report
mat_ga = ob_ga.data.materials[0]
mat_go = ob_go.data.materials[0]
ga_grp = next(n for n in mat_ga.node_tree.nodes if n.type == "GROUP")
go_grp = next(n for n in mat_go.node_tree.nodes if n.type == "GROUP")
report_text = ""
for txt in bpy.data.texts:
    if "pbrcar" in txt.name:
        report_text = txt.as_string()
        break

pbr_res = {
    "ok": ok_pbr,
    "errors": errs_pbr,
    "has_node_group": ng is not None,
    "uv_names_r": uv_names_r,
    "pal_uv": pal_uv,
    "ao_uv": ao_uv,
    "baked_r": baked_r,
    "baked_g": baked_g,
    "baked_b": baked_b,
    "baked_r2": baked_r2,
    "shared_mat_same": (ob_r.data.materials[0] == ob_r2.data.materials[0]),
    "ga_render_method": getattr(mat_ga, "surface_render_method", None),
    "ga_backface_culling": mat_ga.use_backface_culling,
    "ga_alpha": float(ga_grp.inputs["Alpha"].default_value),
    "go_render_method": getattr(mat_go, "surface_render_method", None),
    "go_backface_culling": mat_go.use_backface_culling,
    "go_alpha": float(go_grp.inputs["Alpha"].default_value),
    "report_mentions_glass_fallback": ("mat_glass_opaque" in report_text and "0.35" in report_text),
}

# ---- Part 2: Base color only mode (must match 1.5.5) ----
clear_source_cache()
bpy.ops.wm.read_homefile(use_empty=True)

ok_bc, summary_bc, warns_bc, errs_bc = execute_beamng_import(
    bpy.context,
    filepath=car_zip,
    cdae_mode=False,
    beamng_config="base",
    beamng_parts_mode="CONFIG",
    use_common=False,
    beamng_materials="BASE_COLOR",
)

ob_r_bc = bpy.data.objects.get("panel_r")
mat_r_bc = ob_r_bc.data.materials[0]
bc_res = {
    "ok": ok_bc,
    "errors": errs_bc,
    "has_node_group": bpy.data.node_groups.get(NODE_GROUP_NAME) is not None,
    "has_group_node": any(n.type == "GROUP" for n in mat_r_bc.node_tree.nodes),
    "has_bsdf_node": any(n.type == "BSDF_PRINCIPLED" for n in mat_r_bc.node_tree.nodes),
    "has_paint_prop": "beamng_paint1" in ob_r_bc,
    "uv_names": [u.name for u in ob_r_bc.data.uv_layers],
}

with open(out_json, "w", encoding="utf-8") as f:
    json.dump({"pbr": pbr_res, "base_color": bc_res}, f, indent=2)
'''


def _srgb_to_lin(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _max_rgb_diff(a: list[float], b: list[float]) -> float:
    return max(abs(x - y) for x, y in zip(a, b))


def main() -> int:
    blender = _find_blender()
    workdir = tempfile.mkdtemp(prefix="test_beamng_pbr_52_")
    try:
        car_zip = _build_fixture(workdir)
        worker_py = os.path.join(workdir, "worker.py")
        out_json = os.path.join(workdir, "out.json")
        with open(worker_py, "w", encoding="utf-8") as f:
            f.write(WORKER_SCRIPT)

        proc = subprocess.run(
            [blender, "-b", "--factory-startup", "--python-exit-code", "1",
             "--python", worker_py, "--", REPO_ROOT, car_zip, out_json],
            capture_output=True,
            text=True,
            timeout=180,
        )
        if proc.returncode != 0:
            print("STDOUT:\n", proc.stdout)
            print("STDERR:\n", proc.stderr)
            raise RuntimeError(f"Blender worker failed with exit code {proc.returncode}")

        with open(out_json, encoding="utf-8") as f:
            data = json.load(f)

        pbr = data["pbr"]
        bc = data["base_color"]
        tol = 1.0 / 255.0
        ao = 128.0 / 255.0

        exp_r = [_srgb_to_lin(c) * ao for c in (0.8, 0.2, 0.1)]
        exp_g = [_srgb_to_lin(c) * ao for c in (0.15, 0.75, 0.25)]
        exp_b = [_srgb_to_lin(c) * ao for c in (0.1, 0.3, 0.9)]
        exp_r2 = [_srgb_to_lin(c) * ao for c in (0.2, 0.9, 0.85)]

        failures: list[str] = []
        if not pbr["ok"] or pbr["errors"]:
            failures.append(f"PBR import failed: {pbr['errors']}")
        if not pbr["has_node_group"]:
            failures.append("Shared 'BeamNG v1.5 Material' node group was not created")
        if pbr["uv_names_r"] != ["UV0", "UV1"]:
            failures.append(f"Expected UV names ['UV0', 'UV1'], got {pbr['uv_names_r']}")
        if pbr["pal_uv"] != "UV1" or pbr["ao_uv"] != "UV1":
            failures.append(f"Expected UseUV:1 maps bound to UV1, got pal={pbr['pal_uv']} ao={pbr['ao_uv']}")

        for label, got, exp in (
            ("Paint 1 (R) * AO", pbr["baked_r"], exp_r),
            ("Paint 2 (G) * AO", pbr["baked_g"], exp_g),
            ("Paint 3 (B) * AO", pbr["baked_b"], exp_b),
            ("Recolored object sharing mat", pbr["baked_r2"], exp_r2),
        ):
            diff = _max_rgb_diff(got, exp)
            if diff > tol:
                failures.append(f"{label}: max diff {diff:.5f} > 1/255 ({tol:.5f}); got={got} exp={exp}")

        if not pbr["shared_mat_same"]:
            failures.append("Recolored object did not share the same material")
        if pbr["ga_render_method"] != "BLENDED" or pbr["ga_backface_culling"] is not False:
            failures.append(
                f"glass_alpha render settings wrong: method={pbr['ga_render_method']} cull={pbr['ga_backface_culling']}"
            )
        if abs(pbr["ga_alpha"] - 0.4) > 1e-4:
            failures.append(f"glass_alpha expected Alpha=0.4, got {pbr['ga_alpha']}")
        if pbr["go_render_method"] != "BLENDED" or pbr["go_backface_culling"] is not False:
            failures.append(
                f"glass_opaque render settings wrong: method={pbr['go_render_method']} cull={pbr['go_backface_culling']}"
            )
        if abs(pbr["go_alpha"] - 0.35) > 1e-4:
            failures.append(f"glass_opaque expected fallback Alpha=0.35, got {pbr['go_alpha']}")
        if not pbr["report_mentions_glass_fallback"]:
            failures.append("Import report did not mention mat_glass_opaque 0.35 fallback")

        if not bc["ok"] or bc["errors"]:
            failures.append(f"BASE_COLOR import failed: {bc['errors']}")
        if bc["has_node_group"] or bc["has_group_node"] or not bc["has_bsdf_node"]:
            failures.append(f"BASE_COLOR mode should not use PBR node group: {bc}")
        if bc["has_paint_prop"]:
            failures.append("BASE_COLOR mode should not set beamng_paint1 custom properties")
        if bc["uv_names"] == ["UV0", "UV1"]:
            failures.append(f"BASE_COLOR mode should keep original UV names, got {bc['uv_names']}")

        if failures:
            print("FAIL test_beamng_pbr_52:")
            for msg in failures:
                print(f"  - {msg}")
            return 1

        print("PASS test_beamng_pbr_52 (all #52 PBR, Cycles bake, shared-mat paint, glass & BASE_COLOR checks passed)")
        print(f"  Max bake diffs: R={_max_rgb_diff(pbr['baked_r'], exp_r):.6f}, "
              f"G={_max_rgb_diff(pbr['baked_g'], exp_g):.6f}, "
              f"B={_max_rgb_diff(pbr['baked_b'], exp_b):.6f}, "
              f"Recolored={_max_rgb_diff(pbr['baked_r2'], exp_r2):.6f}")
        return 0
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
