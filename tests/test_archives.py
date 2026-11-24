"""Tests for pure in-memory COLLADA archive (.zae / .kmz / .zip) import (Part B7).

Builds zip archives on the fly and verifies:
  1. manifest.xml + dae_root in a subfolder + texture in a sibling textures/ folder
  2. no manifest, two .dae files (deterministic sorted pick)
  3. texture referenced with Windows backslashes and different case
  4. texture referenced by an absolute path existing on disk -> must NOT be loaded
  5. entry names with "../../x" and leading "/" -> nothing written to disk, import succeeds
  6. missing texture -> warning ("Texture not found in archive: ..."), import succeeds
  7. oversized/high-ratio entry -> clear error, no memory blow-up
  8. packed images decode, materials link texture to Base Color, and survive
     save_as_mainfile + reopen.

Usage:
    python tests/test_archives.py
    BLENDER_5X=/path/to/blender python tests/test_archives.py
"""

from __future__ import annotations

import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import zipfile
import zlib

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _find_runner() -> list[str]:
    blender = os.environ.get("BLENDER_5X") or os.environ.get("BLENDER_EXE")
    if blender and os.path.isfile(blender):
        return [blender, "-b", "--factory-startup", "--python"]
    local_bl = os.path.join(REPO_ROOT, ".tools", "blender-5.3.0-windows-x64", "blender.exe")
    if os.path.isfile(local_bl):
        return [local_bl, "-b", "--factory-startup", "--python"]
    return [sys.executable]


def _make_png(width: int = 4, height: int = 4, rgba: tuple[int, int, int, int] = (255, 0, 0, 255)) -> bytes:
    """Generate a valid RGBA PNG in pure Python."""
    def chunk(ctype: bytes, data: bytes) -> bytes:
        c = ctype + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    row = b"\x00" + bytes(rgba) * width
    raw = row * height
    idat = zlib.compress(raw, 6)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


def _make_dds(width: int = 4, height: int = 4, rgba: tuple[int, int, int, int] = (255, 0, 0, 255)) -> bytes:
    """Generate a valid uncompressed 32-bit RGBA DDS image in pure Python."""
    r, g, b, a = rgba
    # DDS_HEADER (124 bytes): dwSize=124, dwFlags=CAPS|HEIGHT|WIDTH|PIXELFORMAT|PITCH (0x100F)
    # DDS_PIXELFORMAT (32 bytes): dwSize=32, dwFlags=DDPF_RGB|DDPF_ALPHAPIXELS (0x41), 32-bit RGBA masks
    pitch = width * 4
    ddspf = struct.pack(
        "<8I",
        32,            # dwSize
        0x41,          # DDPF_RGB | DDPF_ALPHAPIXELS
        0,             # dwFourCC
        32,            # dwRGBBitCount
        0x000000FF,    # dwRBitMask
        0x0000FF00,    # dwGBitMask
        0x00FF0000,    # dwBBitMask
        0xFF000000,    # dwABitMask
    )
    header = struct.pack(
        "<7I",
        124,           # dwSize
        0x0000100F,    # DDSD_CAPS | DDSD_HEIGHT | DDSD_WIDTH | DDSD_PITCH | DDSD_PIXELFORMAT
        height,
        width,
        pitch,
        0,             # dwDepth
        0,             # dwMipMapCount
    )
    header += b"\x00" * (11 * 4)  # dwReserved1[11]
    header += ddspf
    header += struct.pack("<5I", 0x1000, 0, 0, 0, 0)  # DDSCAPS_TEXTURE + caps2..4 + reserved2
    assert len(header) == 124
    pixels = bytes((r, g, b, a)) * (width * height)
    return b"DDS " + header + pixels


def _make_textured_dae(geom_name: str, image_uri: str | None, mat_name: str = "Mat") -> str:
    img_lib = ""
    mat_diff = "<color>0.5 0.5 0.5 1</color>"
    newparams = ""
    if image_uri is not None:
        img_lib = f"""
  <library_images>
    <image id="Tex-img" name="Tex-img"><init_from>{image_uri}</init_from></image>
  </library_images>"""
        newparams = """
        <newparam sid="Tex-surf"><surface type="2D"><init_from>Tex-img</init_from></surface></newparam>
        <newparam sid="Tex-samp"><sampler2D><source>Tex-surf</source></sampler2D></newparam>"""
        mat_diff = '<texture texture="Tex-samp" texcoord="UVMap"/>'

    return f"""<?xml version="1.0" encoding="utf-8"?>
<COLLADA xmlns="http://www.collada.org/2005/11/COLLADASchema" version="1.4.1">
  <asset><up_axis>Z_UP</up_axis></asset>{img_lib}
  <library_effects>
    <effect id="{mat_name}-fx">
      <profile_COMMON>{newparams}
        <technique sid="common">
          <lambert><diffuse>{mat_diff}</diffuse></lambert>
        </technique>
      </profile_COMMON>
    </effect>
  </library_effects>
  <library_materials>
    <material id="{mat_name}-mat" name="{mat_name}"><instance_effect url="#{mat_name}-fx"/></material>
  </library_materials>
  <library_geometries>
    <geometry id="{geom_name}-mesh" name="{geom_name}">
      <mesh>
        <source id="{geom_name}-pos">
          <float_array id="{geom_name}-pos-arr" count="12">0 0 0 1 0 0 1 1 0 0 1 0</float_array>
          <technique_common>
            <accessor source="#{geom_name}-pos-arr" count="4" stride="3">
              <param name="X" type="float"/><param name="Y" type="float"/><param name="Z" type="float"/>
            </accessor>
          </technique_common>
        </source>
        <source id="{geom_name}-uv">
          <float_array id="{geom_name}-uv-arr" count="8">0 0 1 0 1 1 0 1</float_array>
          <technique_common>
            <accessor source="#{geom_name}-uv-arr" count="4" stride="2">
              <param name="S" type="float"/><param name="T" type="float"/>
            </accessor>
          </technique_common>
        </source>
        <vertices id="{geom_name}-verts">
          <input semantic="POSITION" source="#{geom_name}-pos"/>
        </vertices>
        <triangles material="{mat_name}-mat" count="2">
          <input semantic="VERTEX" source="#{geom_name}-verts" offset="0"/>
          <input semantic="TEXCOORD" source="#{geom_name}-uv" offset="1" set="0"/>
          <p>0 0 1 1 2 2 0 0 2 2 3 3</p>
        </triangles>
      </mesh>
    </geometry>
  </library_geometries>
  <library_visual_scenes>
    <visual_scene id="Scene">
      <node id="{geom_name}" name="{geom_name}" type="NODE">
        <instance_geometry url="#{geom_name}-mesh">
          <bind_material><technique_common>
            <instance_material symbol="{mat_name}-mat" target="#{mat_name}-mat"/>
          </technique_common></bind_material>
        </instance_geometry>
      </node>
    </visual_scene>
  </library_visual_scenes>
  <scene><instance_visual_scene url="#Scene"/></scene>
</COLLADA>
"""


WORKER_SCRIPT = r'''
import json
import os
import sys

argv = sys.argv
if "--" in argv:
    argv = argv[argv.index("--") + 1:]
else:
    argv = argv[1:]

repo_root, archive_path, out_json, save_blend = argv[0], argv[1], argv[2], argv[3]
archive_mode = argv[4] if len(argv) > 4 else ""
sys.path.insert(0, repo_root)

import bpy
import io_scene_collada
from io_scene_collada.import_collada import DocumentImporter, ImportSettings

bpy.ops.wm.read_factory_settings(use_empty=True)
if not hasattr(bpy.ops.wm, "collada_import"):
    io_scene_collada.register()

kwargs = {"filepath": archive_path}
if archive_mode:
    kwargs["archive_mode"] = archive_mode
settings = ImportSettings(**kwargs)
importer = DocumentImporter(bpy.context, settings)
ok = importer.import_document()


def snapshot_state():
    imgs = []
    for img in bpy.data.images:
        px = list(img.pixels[:4]) if len(img.pixels) >= 4 else []
        imgs.append({
            "name": img.name,
            "filepath": img.filepath,
            "packed": img.packed_file is not None,
            "has_data": bool(img.has_data),
            "size": [int(img.size[0]), int(img.size[1])],
            "first_pixel": [round(float(c), 3) for c in px],
        })
    linked_base_colors = []
    mat_links = {}
    for ma in bpy.data.materials:
        if ma.node_tree:
            for n in ma.node_tree.nodes:
                if n.type == "BSDF_PRINCIPLED":
                    sock = n.inputs.get("Base Color")
                    if sock and sock.is_linked:
                        from_node = sock.links[0].from_node
                        if from_node.type == "TEX_IMAGE" and from_node.image is not None:
                            linked_base_colors.append(from_node.image.name)
                            mat_links[ma.name] = from_node.image.name
    colls = {c.name: sorted(o.name for o in c.objects) for c in bpy.data.collections}
    return {
        "objects": sorted(ob.name for ob in bpy.data.objects),
        "collections": colls,
        "images": imgs,
        "linked_base_colors": linked_base_colors,
        "mat_links": mat_links,
    }


state_before = snapshot_state()
state_after = None
if save_blend and ok:
    bpy.ops.wm.save_as_mainfile(filepath=save_blend)
    bpy.ops.wm.open_mainfile(filepath=save_blend)
    state_after = snapshot_state()

result = {
    "ok": bool(ok),
    "warnings": list(importer.warnings),
    "errors": list(importer.errors),
    "before": state_before,
    "after": state_after,
}
with open(out_json, "w", encoding="utf-8") as f:
    json.dump(result, f)
'''


def _run_archive(
    runner: list[str],
    worker_path: str,
    archive_path: str,
    save_blend: str = "",
    archive_mode: str = "",
) -> dict:
    suffix = f".{archive_mode}" if archive_mode else ""
    out_json = archive_path + suffix + ".result.json"
    if runner[0] == sys.executable:
        cmd = [runner[0], worker_path, REPO_ROOT, archive_path, out_json, save_blend, archive_mode]
    else:
        cmd = [*runner, worker_path, "--", REPO_ROOT, archive_path, out_json, save_blend, archive_mode]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise AssertionError(
            f"Subprocess failed (rc={proc.returncode}) on {os.path.basename(archive_path)}\n"
            f"STDOUT:\n{proc.stdout[-1500:]}\nSTDERR:\n{proc.stderr[-1500:]}"
        )
    if "Traceback (most recent call last)" in proc.stderr or "Traceback (most recent call last)" in proc.stdout:
        raise AssertionError(
            f"Unexpected traceback on {os.path.basename(archive_path)}\n"
            f"STDOUT:\n{proc.stdout[-1500:]}\nSTDERR:\n{proc.stderr[-1500:]}"
        )
    with open(out_json, "r", encoding="utf-8") as f:
        return json.load(f)


def main() -> int:
    runner = _find_runner()
    print(f"Running archive tests using: {' '.join(runner)}")
    workdir = tempfile.mkdtemp(prefix="collada_archives_")
    red_png = _make_png(4, 4, (255, 0, 0, 255))
    green_png = _make_png(4, 4, (0, 255, 0, 255))

    try:
        worker_path = os.path.join(workdir, "_archive_worker.py")
        with open(worker_path, "w", encoding="utf-8") as f:
            f.write(WORKER_SCRIPT)

        # Case 1: manifest.xml + dae_root in a subfolder + texture in sibling textures/ folder + save & reopen
        c1_zae = os.path.join(workdir, "case1_manifest.zae")
        c1_blend = os.path.join(workdir, "case1_reopen.blend")
        with zipfile.ZipFile(c1_zae, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(
                "manifest.xml",
                '<?xml version="1.0"?><dae_manifest><dae_root>models/hero.dae</dae_root></dae_manifest>',
            )
            zf.writestr("a_other.dae", _make_textured_dae("WrongMesh", None))
            zf.writestr("models/hero.dae", _make_textured_dae("HeroMesh", "../textures/albedo.png"))
            zf.writestr("textures/albedo.png", red_png)
        r1 = _run_archive(runner, worker_path, c1_zae, save_blend=c1_blend)
        assert r1["ok"], f"Case 1 failed: {r1['errors']}"
        assert "HeroMesh" in r1["before"]["objects"] and "WrongMesh" not in r1["before"]["objects"], r1["before"]
        for label, snap in (("before", r1["before"]), ("after reopen", r1["after"])):
            assert snap is not None and len(snap["images"]) == 1, f"Case 1 ({label}): {snap}"
            im = snap["images"][0]
            assert im["packed"], f"Case 1 ({label}): image not packed: {im}"
            assert im["has_data"] and im["size"] == [4, 4], f"Case 1 ({label}): bad decode: {im}"
            assert im["first_pixel"][0] > 0.9 and im["first_pixel"][1] < 0.1, f"Case 1 ({label}): wrong pixel: {im}"
            assert len(snap["linked_base_colors"]) == 1, f"Case 1 ({label}): Base Color not linked: {snap}"
        print("  PASS case1_manifest_and_reopen")

        # Case 2: multi-.dae archive without manifest (BeamNG covet.zip layout: 3wheel/ sorts before covet.dae)
        c2_zip = os.path.join(workdir, "covet.zip")
        with zipfile.ZipFile(c2_zip, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("vehicles/covet/3wheel/covet_3wheel.dae", _make_textured_dae("ThreeWheelMesh", None))
            zf.writestr("vehicles/covet/covet.dae", _make_textured_dae("CovetMainMesh", None))
            zf.writestr("__MACOSX/vehicles/covet/._covet.dae", "not xml")
        # 2a. Default ALL mode: imports every .dae into its own collection
        r2_all = _run_archive(runner, worker_path, c2_zip, archive_mode="ALL")
        assert r2_all["ok"], f"Case 2 (ALL) failed: {r2_all['errors']}"
        assert r2_all["before"]["objects"] == ["CovetMainMesh", "ThreeWheelMesh"], r2_all["before"]
        assert r2_all["before"]["collections"].get("covet") == ["CovetMainMesh"], r2_all["before"]["collections"]
        assert r2_all["before"]["collections"].get("covet_3wheel") == ["ThreeWheelMesh"], r2_all["before"]["collections"]
        # 2b. MAIN mode: prefers covet.dae (matches zip name, shallower) over 3wheel/covet_3wheel.dae and warns
        r2_main = _run_archive(runner, worker_path, c2_zip, archive_mode="MAIN")
        assert r2_main["ok"], f"Case 2 (MAIN) failed: {r2_main['errors']}"
        assert r2_main["before"]["objects"] == ["CovetMainMesh"], f"Case 2 (MAIN) wrong pick: {r2_main['before']}"
        assert any("skipped 1 other" in w and "covet_3wheel.dae" in w for w in r2_main["warnings"]), r2_main["warnings"]
        print("  PASS case2_multi_dae_all_and_main_ranking")

        # Case 3: texture referenced with Windows backslashes and different case
        c3_kmz = os.path.join(workdir, "case3_win_case.kmz")
        with zipfile.ZipFile(c3_kmz, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("scene/model.dae", _make_textured_dae("WinMesh", r"..\Textures\Wood_DIFF.PNG"))
            zf.writestr("textures/wood_diff.png", green_png)
        r3 = _run_archive(runner, worker_path, c3_kmz)
        assert r3["ok"], f"Case 3 failed: {r3['errors']}"
        assert len(r3["before"]["images"]) == 1, r3["before"]
        im3 = r3["before"]["images"][0]
        assert im3["packed"] and im3["has_data"] and im3["first_pixel"][1] > 0.9, im3
        assert len(r3["before"]["linked_base_colors"]) == 1, r3["before"]
        print("  PASS case3_windows_backslashes_and_case")

        # Case 4: texture referenced by an absolute path that exists on disk -> must NOT be loaded
        secret_png = os.path.join(workdir, "secret_on_disk.png")
        with open(secret_png, "wb") as f:
            f.write(red_png)
        c4_zae = os.path.join(workdir, "case4_abs_disk_blocked.zae")
        with zipfile.ZipFile(c4_zae, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("model.dae", _make_textured_dae("DiskTest", secret_png))
        r4 = _run_archive(runner, worker_path, c4_zae)
        assert r4["ok"], f"Case 4 failed: {r4['errors']}"
        assert len(r4["before"]["images"]) == 0, f"Case 4 LEAKED disk image: {r4['before']['images']}"
        assert any("not found in archive" in w.lower() for w in r4["warnings"]), f"Case 4 missing warning: {r4['warnings']}"
        print("  PASS case4_disk_texture_blocked")

        # Case 5: entry names with "../../x" and absolute "/abs/..." -> nothing written anywhere, import works
        escape_marker = os.path.join(os.path.dirname(workdir), "collada_escape_probe.txt")
        if os.path.exists(escape_marker):
            os.remove(escape_marker)
        c5_zae = os.path.join(workdir, "case5_traversal_entries.zae")
        with zipfile.ZipFile(c5_zae, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("../../collada_escape_probe.txt", "should never touch disk")
            zf.writestr("/abs/model.dae", _make_textured_dae("SafeMesh", "tex.png"))
            zf.writestr("/abs/tex.png", red_png)
        r5 = _run_archive(runner, worker_path, c5_zae)
        assert r5["ok"], f"Case 5 failed: {r5['errors']}"
        assert not os.path.exists(escape_marker), "Case 5 wrote file outside archive!"
        assert "SafeMesh" in r5["before"]["objects"], r5["before"]
        assert len(r5["before"]["images"]) == 1 and r5["before"]["images"][0]["packed"], r5["before"]
        print("  PASS case5_traversal_and_absolute_entries")

        # Case 6: missing texture -> warning, import succeeds
        c6_zae = os.path.join(workdir, "case6_missing_tex.zae")
        with zipfile.ZipFile(c6_zae, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("model.dae", _make_textured_dae("NoTexMesh", "does_not_exist.png"))
        r6 = _run_archive(runner, worker_path, c6_zae)
        assert r6["ok"], f"Case 6 failed: {r6['errors']}"
        assert "NoTexMesh" in r6["before"]["objects"], r6["before"]
        assert len(r6["before"]["images"]) == 0, r6["before"]
        assert any("not found in archive" in w.lower() for w in r6["warnings"]), r6["warnings"]
        print("  PASS case6_missing_texture_warning")

        # Case 7: high compression ratio (>200x, >50 MB) zip-bomb guard -> clear error, no memory blow-up
        c7_zae = os.path.join(workdir, "case7_zipbomb.zae")
        with zipfile.ZipFile(c7_zae, "w", zipfile.ZIP_DEFLATED) as zf:
            # 60 MB of spaces compresses to ~60 KB (~1000x ratio)
            bomb_dae = b" " * (60 * 1024 * 1024)
            zf.writestr("bomb.dae", bomb_dae)
        r7 = _run_archive(runner, worker_path, c7_zae)
        assert not r7["ok"], "Case 7 should have refused zip-bomb archive"
        assert r7["errors"] and any(
            ("ratio" in e.lower() or "size" in e.lower() or "bomb" in e.lower() or "exceeds" in e.lower())
            for e in r7["errors"]
        ), f"Case 7 expected clear zip-bomb error, got: {r7['errors']}"
        print(f"  PASS case7_zipbomb_guard (error: {r7['errors'][0]})")

        # Case 8: .png reference falls back to .dds shipped inside the archive (BeamNG Barstow case)
        red_dds = _make_dds(4, 4, (255, 0, 0, 255))
        blue_dds = _make_dds(4, 4, (0, 0, 255, 255))
        c8_zip = os.path.join(workdir, "barstow_dds_fallback.zip")
        with zipfile.ZipFile(c8_zip, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(
                "vehicles/barstow/barstow.dae",
                _make_textured_dae("BarstowInterior", r"D:\BeamNG\barstow_interior_b.color.png", mat_name="barstow_interior"),
            )
            zf.writestr("vehicles/barstow/barstow_interior_b.color.dds", red_dds)
        r8 = _run_archive(runner, worker_path, c8_zip)
        assert r8["ok"], f"Case 8 failed: {r8['errors']}"
        assert len(r8["before"]["images"]) == 1, f"Case 8 missing DDS fallback image: {r8['before']}"
        im8 = r8["before"]["images"][0]
        assert im8["packed"] and im8["has_data"] and im8["size"] == [4, 4], im8
        assert im8["first_pixel"][0] > 0.9 and im8["first_pixel"][2] < 0.1, im8
        assert r8["before"]["mat_links"].get("barstow_interior") == "barstow_interior_b.color.dds", r8["before"]
        print("  PASS case8_png_to_dds_fallback")

        # Case 9: BeamNG main.materials.json (with trailing commas + comments) fills untextured .dae materials
        c9_zip = os.path.join(workdir, "wigeon.zip")
        main_mats_json = """{
          // Base vehicle material using PBR baseColorMap referencing .png while .dds is in zip
          "wigeon_body": {
            "name": "wigeon_body",
            "mapTo": "wigeon_body",
            "Stages": [
              {
                "baseColorMap": "/vehicles/wigeon/wigeon_body_b.color.png",
              },
              {},
            ],
          },
          // Material whose texture lives in common.zip (missing here -> must NOT warn or error)
          "common_rubber": {
            "name": "common_rubber",
            "mapTo": "common_rubber",
            "Stages": [
              {
                "colorMap": "/vehicles/common/tires/rubber_d.dds",
              }
            ]
          },
        }"""
        decal_mats_json = """{
          /* dynamicDecals with trailing comma */
          "wigeon_decal": {
            "name": "wigeon_decal",
            "mapTo": "wigeon_decal",
            "Stages": [
              {
                "colorMap": "vehicles/wigeon/dynamicDecals/decal_d.png",
              },
            ],
          },
        }"""
        with zipfile.ZipFile(c9_zip, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("vehicles/wigeon/wigeon.dae", _make_textured_dae("WigeonBody", None, mat_name="wigeon_body"))
            zf.writestr("vehicles/wigeon/decals.dae", _make_textured_dae("WigeonDecal", None, mat_name="wigeon_decal"))
            zf.writestr("vehicles/wigeon/common_part.dae", _make_textured_dae("CommonPart", None, mat_name="common_rubber"))
            zf.writestr("vehicles/wigeon/main.materials.json", main_mats_json)
            zf.writestr("vehicles/wigeon/dynamicDecals/main.materials.json", decal_mats_json)
            zf.writestr("vehicles/wigeon/wigeon_body_b.color.dds", blue_dds)
            zf.writestr("vehicles/wigeon/dynamicDecals/decal_d.png", green_png)
        r9 = _run_archive(runner, worker_path, c9_zip)
        assert r9["ok"], f"Case 9 failed: {r9['errors']}"
        assert not r9["warnings"], f"Case 9 unexpected warnings (trailing comma or common.zip): {r9['warnings']}"
        assert r9["before"]["mat_links"].get("wigeon_body") == "wigeon_body_b.color.dds", r9["before"]
        assert r9["before"]["mat_links"].get("wigeon_decal") == "decal_d.png", r9["before"]
        assert "common_rubber" not in r9["before"]["mat_links"], r9["before"]
        assert set(r9["before"]["collections"].keys()) == {"wigeon", "decals", "common_part"}, r9["before"]["collections"]
        print("  PASS case9_beamng_materials_json_lenient_and_dds")

        print("\nAll 9 archive tests passed!")
        return 0
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
