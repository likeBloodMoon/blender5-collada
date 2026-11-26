"""Synthetic regression test for #52: BeamNG v1.5 PBR materials, multi-layer compositing,
palette paints, AO, UV1, glass/alphaTest/carbonfibre transparency, and legacy fallback.

Verifies (using purely synthetic zips, no game files):
  1. Shared 'BeamNG v1.5 Material' node group is created once and shared by v1.5 materials.
  2. UV maps are named UV0 and UV1 on BeamNG PBR import; maps with <map>UseUV: 1 bind to UV1.
  3. Paints from .pc are stored on imported objects as beamng_paint1..3 (sRGB -> linear)
     plus metallic/roughness/clearcoat/clearcoat_roughness.
  4. Cycles EMIT bake of Base Color with Palette Mask (UV1) + AO (UV1) matches
     paint * mask * AO to within 1/255 for all 3 paint channels (R, G, B).
  5. Two objects sharing the exact same material with different beamng_paint1 values
     bake to different colors matching each object's own paint.
  6. Two-layer material shaped like covet_main (version 1.5, activeLayers=2):
     Layer 0 = instanceDiffuse (no baseColorMap, white factor) + AO;
     Layer 1 = baseColorMap + opacityMap mask + metallicMap + roughnessMap.
     Material stays opaque; black opacityMap -> Paint 1 * AO, white opacityMap -> Layer 1 texture.
  7. Legacy material (no version: 1.5, translucent=true, mapTo='*_old', colorMap in Stages[1])
     keeps 1.5.5 direct Principled BSDF behaviour with its texture connected.
  8. Translucent v1.5 material with no opacity source:
     - Name containing 'glass'/'windshield' -> 0.35 fallback, BLENDED, backface culling off, reported.
     - Other name ('covet_carbonfibre') -> stays opaque (Alpha=1.0, not BLENDED, not in glass report).
  9. Material with alphaTest ('etk800_grille') -> DITHERED (not BLENDED) with GREATER_THAN clip.
 10. beamng_materials='BASE_COLOR' produces 1.5.5-style direct Principled BSDF trees
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


def _make_split_png(left_rgba: tuple[int, int, int, int], right_rgba: tuple[int, int, int, int]) -> bytes:
    """8x4 PNG: left 4 columns = left_rgba, right 4 columns = right_rgba."""
    w, h = 8, 4
    row = bytes(left_rgba) * 4 + bytes(right_rgba) * 4
    raw = b"".join(b"\x00" + row for _ in range(h))

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
                ["covet_body", ["body"]],
                ["covet_interior_mesh", ["body"]],
                ["legacy_part", ["body"]],
                ["carbon_hood", ["body"]],
                ["grille_mesh", ["body"]],
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
            "version": 1.5,
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
            "version": 1.5,
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
            "version": 1.5,
            "Stages": [{
                "baseColorMap": "vehicles/pbrcar/white.png",
                "colorPaletteMap": "vehicles/pbrcar/mask_b.png",
                "colorPaletteMapUseUV": 1,
                "ambientOcclusionMap": "vehicles/pbrcar/ao_half.png",
                "ambientOcclusionMapUseUV": 1,
            }],
        },
        # Two-layer material shaped like covet_main:
        # Layer 0 = instanceDiffuse paint 1 (no baseColorMap) + AO
        # Layer 1 = covet_main_b texture masked by covet_main_o (black=L0, white=L1)
        "covet_main": {
            "name": "covet_main",
            "mapTo": "covet_main",
            "version": 1.5,
            "activeLayers": 2,
            "Stages": [
                {
                    "instanceDiffuse": True,
                    "ambientOcclusionMap": "vehicles/pbrcar/ao_half.png",
                },
                {
                    "baseColorMap": "vehicles/pbrcar/covet_main_b.png",
                    "opacityMap": "vehicles/pbrcar/covet_main_o.png",
                    "metallicMap": "vehicles/pbrcar/covet_main_m.png",
                    "roughnessMap": "vehicles/pbrcar/covet_main_r.png",
                },
            ],
        },
        # Solid material (translucent=False, alphaTest=False) with opacityMap & alpha in baseColorMap:
        # must stay OPAQUE and ignore layer 0 opacity
        "covet_interior": {
            "name": "covet_interior",
            "mapTo": "covet_interior",
            "version": 1.5,
            "Stages": [{
                "baseColorMap": "vehicles/pbrcar/grille_alpha.png",
                "opacityMap": "vehicles/pbrcar/covet_main_o.png",
                "opacityFactor": 0.5,
                "roughnessMap": "vehicles/pbrcar/covet_main_r.png",
            }],
        },
        # Legacy material (no version: 1.5) with translucent and mapTo *_old, colorMap in Stages[1]
        "covet_lights_old": {
            "name": "covet_lights_old",
            "mapTo": "covet_lights_old",
            "translucent": True,
            "Stages": [
                {},
                {"colorMap": "vehicles/pbrcar/legacy_col.png"},
            ],
        },
        # Translucent v1.5 material without glass/windshield in name and no opacity source
        "covet_carbonfibre": {
            "name": "covet_carbonfibre",
            "mapTo": "covet_carbonfibre",
            "version": 1.5,
            "translucent": True,
            "Stages": [{
                "baseColorMap": "vehicles/pbrcar/white.png",
            }],
        },
        # alphaTest material shaped like etk800_grille
        "etk800_grille": {
            "name": "etk800_grille",
            "mapTo": "etk800_grille",
            "version": 1.5,
            "alphaTest": True,
            "alphaRef": 128,
            "doubleSided": True,
            "Stages": [{
                "baseColorMap": "vehicles/pbrcar/grille_alpha.png",
            }],
        },
        "mat_glass_alpha": {
            "name": "mat_glass_alpha",
            "mapTo": "mat_glass_alpha",
            "version": 1.5,
            "translucent": True,
            "Stages": [{
                "baseColorMap": "vehicles/pbrcar/white.png",
                "opacityFactor": 0.4,
            }],
        },
        "mat_glass_opaque": {
            "name": "mat_glass_opaque",
            "mapTo": "mat_glass_opaque",
            "version": 1.5,
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
        zf.writestr("vehicles/pbrcar/covet_body.dae", _make_two_uv_dae("covet_body", "covet_main"))
        zf.writestr("vehicles/pbrcar/covet_interior_mesh.dae", _make_two_uv_dae("covet_interior_mesh", "covet_interior"))
        zf.writestr("vehicles/pbrcar/legacy_part.dae", _make_two_uv_dae("legacy_part", "covet_lights_old"))
        zf.writestr("vehicles/pbrcar/carbon_hood.dae", _make_two_uv_dae("carbon_hood", "covet_carbonfibre"))
        zf.writestr("vehicles/pbrcar/grille_mesh.dae", _make_two_uv_dae("grille_mesh", "etk800_grille"))
        zf.writestr("vehicles/pbrcar/glass_alpha.dae", _make_two_uv_dae("glass_alpha", "mat_glass_alpha"))
        zf.writestr("vehicles/pbrcar/glass_opaque.dae", _make_two_uv_dae("glass_opaque", "mat_glass_opaque"))
        zf.writestr("vehicles/pbrcar/white.png", _make_png((255, 255, 255, 255)))
        zf.writestr("vehicles/pbrcar/mask_r.png", _make_png((255, 0, 0, 255)))
        zf.writestr("vehicles/pbrcar/mask_g.png", _make_png((0, 255, 0, 255)))
        zf.writestr("vehicles/pbrcar/mask_b.png", _make_png((0, 0, 255, 255)))
        zf.writestr("vehicles/pbrcar/ao_half.png", _make_png((128, 128, 128, 255)))
        zf.writestr("vehicles/pbrcar/covet_main_b.png", _make_png((51, 153, 204, 255)))
        zf.writestr("vehicles/pbrcar/covet_main_o.png", _make_split_png((0, 0, 0, 255), (255, 255, 255, 255)))
        zf.writestr("vehicles/pbrcar/covet_main_m.png", _make_png((200, 200, 200, 255)))
        zf.writestr("vehicles/pbrcar/covet_main_r.png", _make_png((100, 100, 100, 255)))
        zf.writestr("vehicles/pbrcar/legacy_col.png", _make_png((200, 100, 50, 255)))
        zf.writestr("vehicles/pbrcar/grille_alpha.png", _make_split_png((40, 40, 40, 0), (40, 40, 40, 255)))
        # Config with NO paints section + info_defaultpaint.json + info.json for paint library test
        zf.writestr("vehicles/pbrcar/defaultpaint.pc", json.dumps({"format": 2, "parts": {"main": "pbrcar"}}))
        zf.writestr("vehicles/pbrcar/info_defaultpaint.json", json.dumps({
            "Config Type": "Factory",
            "defaultPaintName1": "Sea Blue",
            "defaultPaintName3": "NoSuchPaint",
        }))
        zf.writestr("vehicles/pbrcar/info.json", json.dumps({
            "defaultPaintName1": "ShouldNotOverride",
            "defaultPaintName2": "Fallback Red",
        }))

    common_zip = os.path.join(workdir, "common.zip")
    paint_lib = json.dumps({
        "paints": {
            "Sea Blue": {
                "name": "Sea Blue",
                "baseColor": [0.16, 0.28, 0.56, 1.2],
                "metallic": 0.25,
                "roughness": 0.07,
                "clearcoat": 1.0,
                "clearcoatRoughness": 0.06,
            },
            "Fallback Red": {
                "name": "Fallback Red",
                "baseColor": [0.7, 0.1, 0.1, 1.0],
                "metallic": 0.0,
                "roughness": 0.3,
                "clearcoat": 0.8,
                "clearcoatRoughness": 0.05,
            },
        }
    })
    with zipfile.ZipFile(common_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("vehicles/common/paintLibraries/default.paintLibrary.json", paint_lib)

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


def bake_base_color_pixels(ob, sample_coords):
    """Bake Cycles EMIT pass of the Base Color feeding Principled BSDF (both direct and inside node groups)."""
    scene = bpy.context.scene
    scene.render.engine = "CYCLES"
    scene.cycles.device = "CPU"
    scene.cycles.samples = 1
    scene.cycles.use_denoising = False

    ma = ob.data.materials[0]
    ntree = ma.node_tree

    # Temporarily swap every Principled BSDF (direct or inside a shared Group) with Emission(Base Color)
    restores = []

    def _patch_tree(tree):
        for n in list(tree.nodes):
            if n.type == "BSDF_PRINCIPLED":
                out_links = [(lk.to_socket,) for lk in n.outputs["BSDF"].links]
                if not out_links:
                    continue
                emit = tree.nodes.new("ShaderNodeEmission")
                emit.inputs["Strength"].default_value = 1.0
                bc_in = n.inputs["Base Color"]
                if bc_in.is_linked:
                    tree.links.new(bc_in.links[0].from_socket, emit.inputs["Color"])
                else:
                    emit.inputs["Color"].default_value = tuple(bc_in.default_value)
                for (to_sock,) in out_links:
                    tree.links.new(emit.outputs["Emission"], to_sock)
                restores.append((tree, n, emit, out_links))

    patched_trees = set()
    _patch_tree(ntree)
    patched_trees.add(ntree)
    for n in ntree.nodes:
        if n.type == "GROUP" and n.node_tree is not None and n.node_tree not in patched_trees:
            _patch_tree(n.node_tree)
            patched_trees.add(n.node_tree)

    w, h = 16, 8
    img = bpy.data.images.new(f"bake_{ob.name}", width=w, height=h, alpha=False, float_buffer=True)
    bake_node = ntree.nodes.new("ShaderNodeTexImage")
    bake_node.image = img
    ntree.nodes.active = bake_node

    for o in bpy.context.view_layer.objects:
        o.select_set(False)
    ob.select_set(True)
    bpy.context.view_layer.objects.active = ob

    try:
        bpy.ops.object.bake(type="EMIT", use_clear=True, margin=0)
        out = []
        for sx, sy in sample_coords:
            idx = (sy * w + sx) * 4
            out.append([float(img.pixels[idx]), float(img.pixels[idx + 1]), float(img.pixels[idx + 2])])
        return out
    finally:
        for tree, bsdf_node, emit_node, out_links in restores:
            for (to_sock,) in out_links:
                tree.links.new(bsdf_node.outputs["BSDF"], to_sock)
            tree.nodes.remove(emit_node)
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
ob_covet = bpy.data.objects.get("covet_body")
ob_legacy = bpy.data.objects.get("legacy_part")
ob_carbon = bpy.data.objects.get("carbon_hood")
ob_grille = bpy.data.objects.get("grille_mesh")
ob_ga = bpy.data.objects.get("glass_alpha")
ob_go = bpy.data.objects.get("glass_opaque")

uv_names_r = [u.name for u in ob_r.data.uv_layers] if ob_r else []

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

baked_r = bake_base_color_pixels(ob_r, [(8, 4)])[0]
baked_g = bake_base_color_pixels(ob_g, [(8, 4)])[0]
baked_b = bake_base_color_pixels(ob_b, [(8, 4)])[0]

# Two objects sharing the exact same material with different beamng_paint1
ob_r2 = ob_r.copy()
ob_r2.name = "panel_r_recolored"
ob_r2.data = ob_r.data.copy()
ob_r2.location = (3.0, 0.0, 0.0)
bpy.context.scene.collection.objects.link(ob_r2)
ob_r2["beamng_paint1"] = (
    srgb_to_linear(0.2),
    srgb_to_linear(0.9),
    srgb_to_linear(0.85),
    1.0,
)
baked_r2 = bake_base_color_pixels(ob_r2, [(8, 4)])[0]

# Two-layer covet_main: left (x=3 -> black opacityMap -> L0 paint 1 * AO), right (x=12 -> white -> L1 covet_main_b)
covet_left, covet_right = bake_base_color_pixels(ob_covet, [(3, 4), (12, 4)])
mat_covet = ob_covet.data.materials[0]
covet_layers = [n for n in mat_covet.node_tree.nodes if n.type in ("GROUP", "BSDF_PRINCIPLED")]
covet_mix_shaders = [n for n in mat_covet.node_tree.nodes if n.type == "MIX_SHADER"]
covet_l1_has_met = any(n.type == "TEX_IMAGE" and n.label == "L1 Metallic" for n in mat_covet.node_tree.nodes)
covet_l1_has_rgh = any(n.type == "TEX_IMAGE" and n.label == "L1 Roughness" for n in mat_covet.node_tree.nodes)

# Solid material with opacityMap & alpha in baseColorMap -> must stay OPAQUE & slim (0 Attribute nodes)
ob_int = bpy.data.objects.get("covet_interior_mesh")
mat_int = ob_int.data.materials[0]
int_bsdf = next(n for n in mat_int.node_tree.nodes if n.type in ("GROUP", "BSDF_PRINCIPLED"))
int_attr_count = sum(1 for n in mat_int.node_tree.nodes if n.type == "ATTRIBUTE")

# Legacy material (no version: 1.5, colorMap in Stages[1])
mat_legacy = ob_legacy.data.materials[0]
legacy_has_group = any(n.type == "GROUP" for n in mat_legacy.node_tree.nodes)
legacy_has_bsdf = any(n.type == "BSDF_PRINCIPLED" for n in mat_legacy.node_tree.nodes)
legacy_tex = None
for n in mat_legacy.node_tree.nodes:
    if n.type == "BSDF_PRINCIPLED":
        bc_sock = n.inputs.get("Base Color")
        if bc_sock and bc_sock.is_linked and bc_sock.links[0].from_node.type == "TEX_IMAGE":
            legacy_tex = bc_sock.links[0].from_node.image.name

# Translucent carbonfibre (no glass/windshield in name, no opacity source -> stays opaque)
mat_carbon = ob_carbon.data.materials[0]
carbon_shader = next(n for n in mat_carbon.node_tree.nodes if n.type in ("GROUP", "BSDF_PRINCIPLED"))

# alphaTest grille -> DITHERED + GREATER_THAN clip
mat_grille = ob_grille.data.materials[0]
grille_shader = next(n for n in mat_grille.node_tree.nodes if n.type in ("GROUP", "BSDF_PRINCIPLED"))
grille_alpha_clip = (
    grille_shader.inputs["Alpha"].is_linked
    and grille_shader.inputs["Alpha"].links[0].from_node.type == "MATH"
    and grille_shader.inputs["Alpha"].links[0].from_node.operation == "GREATER_THAN"
)

# Glass properties & report
mat_ga = ob_ga.data.materials[0]
mat_go = ob_go.data.materials[0]
ga_shader = next(n for n in mat_ga.node_tree.nodes if n.type in ("GROUP", "BSDF_PRINCIPLED"))
go_shader = next(n for n in mat_go.node_tree.nodes if n.type in ("GROUP", "BSDF_PRINCIPLED"))
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
    "covet_render_method": getattr(mat_covet, "surface_render_method", None),
    "covet_num_layers": len(covet_layers),
    "covet_num_mix": len(covet_mix_shaders),
    "covet_l1_has_met": covet_l1_has_met,
    "covet_l1_has_rgh": covet_l1_has_rgh,
    "covet_left": covet_left,
    "covet_right": covet_right,
    "int_render_method": getattr(mat_int, "surface_render_method", None),
    "int_alpha": float(int_bsdf.inputs["Alpha"].default_value),
    "int_alpha_linked": bool(int_bsdf.inputs["Alpha"].is_linked),
    "int_attr_count": int_attr_count,
    "legacy_has_group": legacy_has_group,
    "legacy_has_bsdf": legacy_has_bsdf,
    "legacy_tex": legacy_tex,
    "carbon_render_method": getattr(mat_carbon, "surface_render_method", None),
    "carbon_alpha": float(carbon_shader.inputs["Alpha"].default_value),
    "carbon_in_report": ("covet_carbonfibre" in report_text),
    "grille_render_method": getattr(mat_grille, "surface_render_method", None),
    "grille_alpha_clip": grille_alpha_clip,
    "grille_backface_culling": mat_grille.use_backface_culling,
    "ga_render_method": getattr(mat_ga, "surface_render_method", None),
    "ga_backface_culling": mat_ga.use_backface_culling,
    "ga_alpha": float(ga_shader.inputs["Alpha"].default_value),
    "go_render_method": getattr(mat_go, "surface_render_method", None),
    "go_backface_culling": mat_go.use_backface_culling,
    "go_alpha": float(go_shader.inputs["Alpha"].default_value),
    "report_mentions_glass_fallback": ("mat_glass_opaque" in report_text and "0.35" in report_text),
}

# ---- Part 2: Default paint library (no paints in .pc -> info_<config>.json -> info.json -> common.zip) ----
clear_source_cache()
bpy.ops.wm.read_homefile(use_empty=True)

ok_dp, _s_dp, warns_dp, errs_dp = execute_beamng_import(
    bpy.context,
    filepath=car_zip,
    cdae_mode=False,
    beamng_config="defaultpaint",
    beamng_parts_mode="CONFIG",
    use_common=True,
    beamng_materials="PBR",
)
ob_dp = bpy.data.objects.get("panel_r")
dp_res = {
    "ok": ok_dp,
    "errors": errs_dp,
    "warnings": warns_dp,
    "paint1": list(ob_dp["beamng_paint1"])[:3] if ob_dp else None,
    "paint1_roughness": float(ob_dp["beamng_paint1_roughness"]) if ob_dp else None,
    "paint2": list(ob_dp["beamng_paint2"])[:3] if ob_dp else None,
    "paint3": list(ob_dp["beamng_paint3"])[:3] if ob_dp else None,
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
    json.dump({"pbr": pbr_res, "default_paint": dp_res, "base_color": bc_res}, f, indent=2)
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
        dp = data["default_paint"]
        bc = data["base_color"]
        tol = 1.0 / 255.0
        ao = 128.0 / 255.0

        exp_r = [_srgb_to_lin(c) * ao for c in (0.8, 0.2, 0.1)]
        exp_g = [_srgb_to_lin(c) * ao for c in (0.15, 0.75, 0.25)]
        exp_b = [_srgb_to_lin(c) * ao for c in (0.1, 0.3, 0.9)]
        exp_r2 = [_srgb_to_lin(c) * ao for c in (0.2, 0.9, 0.85)]
        exp_covet_l1 = [_srgb_to_lin(c / 255.0) for c in (51, 153, 204)]

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
            ("covet_main L0 (black mask -> paint 1 * AO)", pbr["covet_left"], exp_r),
            ("covet_main L1 (white mask -> covet_main_b)", pbr["covet_right"], exp_covet_l1),
        ):
            diff = _max_rgb_diff(got, exp)
            if diff > tol:
                failures.append(f"{label}: max diff {diff:.5f} > 1/255 ({tol:.5f}); got={got} exp={exp}")

        if not pbr["shared_mat_same"]:
            failures.append("Recolored object did not share the same material")

        # Multi-layer covet_main checks
        if pbr["covet_render_method"] == "BLENDED":
            failures.append("covet_main should stay opaque (upper-layer opacityMap is a layer mask, not transparency)")
        if pbr["covet_num_layers"] != 2 or pbr["covet_num_mix"] != 1:
            failures.append(
                f"covet_main expected 2 layer shaders and 1 MixShader, got layers={pbr['covet_num_layers']} mix={pbr['covet_num_mix']}"
            )
        if not pbr["covet_l1_has_met"] or not pbr["covet_l1_has_rgh"]:
            failures.append(
                f"covet_main L1 missing metallic/roughness maps: met={pbr['covet_l1_has_met']} rgh={pbr['covet_l1_has_rgh']}"
            )

        # Solid covet_interior with opacityMap/alpha -> must stay opaque & slim (0 Attribute nodes)
        if pbr["int_render_method"] == "BLENDED" or pbr["int_alpha_linked"] or abs(pbr["int_alpha"] - 1.0) > 1e-4:
            failures.append(
                f"covet_interior (non-translucent) should stay opaque: method={pbr['int_render_method']} "
                f"alpha={pbr['int_alpha']} linked={pbr['int_alpha_linked']}"
            )
        if pbr["int_attr_count"] != 0:
            failures.append(f"Plain PBR material covet_interior should have 0 Attribute nodes, got {pbr['int_attr_count']}")

        # Default paint library checks (info_<config>.json -> info.json -> common.zip paintLibraries)
        exp_sea_blue = [_srgb_to_lin(c) for c in (0.16, 0.28, 0.56)]
        exp_fb_red = [_srgb_to_lin(c) for c in (0.7, 0.1, 0.1)]
        if not dp["ok"] or dp["errors"]:
            failures.append(f"Default paint import failed: {dp['errors']}")
        if dp["paint1"] is None or _max_rgb_diff(dp["paint1"], exp_sea_blue) > 1e-5:
            failures.append(f"Default paint1 expected Sea Blue {exp_sea_blue}, got {dp['paint1']}")
        if dp["paint1_roughness"] is None or abs(dp["paint1_roughness"] - 0.07) > 1e-5:
            failures.append(f"Default paint1 roughness expected 0.07, got {dp['paint1_roughness']}")
        if dp["paint2"] is None or _max_rgb_diff(dp["paint2"], exp_fb_red) > 1e-5:
            failures.append(f"Default paint2 (from info.json) expected Fallback Red {exp_fb_red}, got {dp['paint2']}")
        if dp["paint3"] is None or _max_rgb_diff(dp["paint3"], [1.0, 1.0, 1.0]) > 1e-5:
            failures.append(f"Missing default paint3 should fall back to white, got {dp['paint3']}")
        missing_paint_warns = [w for w in dp["warnings"] if "NoSuchPaint" in w]
        if len(missing_paint_warns) != 1:
            failures.append(f"Expected 1 warning for missing paint 'NoSuchPaint', got {dp['warnings']}")

        # Legacy *_old material checks
        if pbr["legacy_has_group"] or not pbr["legacy_has_bsdf"] or pbr["legacy_tex"] != "legacy_col.png":
            failures.append(
                f"Legacy material should keep 1.5.5 direct Principled BSDF + Stages[1] colorMap: "
                f"group={pbr['legacy_has_group']} bsdf={pbr['legacy_has_bsdf']} tex={pbr['legacy_tex']!r}"
            )

        # Translucent carbonfibre without opacity source -> stays opaque
        if pbr["carbon_render_method"] == "BLENDED" or abs(pbr["carbon_alpha"] - 1.0) > 1e-4:
            failures.append(
                f"covet_carbonfibre should stay opaque: method={pbr['carbon_render_method']} alpha={pbr['carbon_alpha']}"
            )
        if pbr["carbon_in_report"]:
            failures.append("covet_carbonfibre should NOT appear in glass fallback report")

        # alphaTest grille -> DITHERED + GREATER_THAN
        if pbr["grille_render_method"] != "DITHERED" or not pbr["grille_alpha_clip"]:
            failures.append(
                f"etk800_grille should use DITHERED + GREATER_THAN alpha clip: "
                f"method={pbr['grille_render_method']} clip={pbr['grille_alpha_clip']}"
            )
        if pbr["grille_backface_culling"] is not False:
            failures.append("etk800_grille (doubleSided=True) should have backface culling off")

        # Glass checks
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

        # Base color only checks
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

        print("PASS test_beamng_pbr_52 (all #52 PBR, multi-layer covet_main, legacy, carbonfibre, alphaTest, glass & BASE_COLOR checks passed)")
        print(
            f"  Max bake diffs: R={_max_rgb_diff(pbr['baked_r'], exp_r):.6f}, "
            f"G={_max_rgb_diff(pbr['baked_g'], exp_g):.6f}, "
            f"B={_max_rgb_diff(pbr['baked_b'], exp_b):.6f}, "
            f"Recolored={_max_rgb_diff(pbr['baked_r2'], exp_r2):.6f}, "
            f"CovetL0={_max_rgb_diff(pbr['covet_left'], exp_r):.6f}, "
            f"CovetL1={_max_rgb_diff(pbr['covet_right'], exp_covet_l1):.6f}"
        )
        return 0
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
