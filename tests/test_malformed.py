"""Tests for malformed COLLADA (.dae) files — crash & invalid-mesh prevention (Part A7).

Generates small malformed .dae files and runs each import in an isolated subprocess
so that any segfault / Blender crash is caught as a test failure. For every imported
mesh, runs bmesh.from_mesh() and asserts me.validate() == False. Runs all cases with
both COLLADA_NATIVE=1 and COLLADA_NATIVE=0.

Usage:
    python tests/test_malformed.py
    BLENDER_5X=/path/to/blender python tests/test_malformed.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _find_runner() -> list[str]:
    """Return command prefix to run a Python script with bpy available."""
    blender = os.environ.get("BLENDER_5X") or os.environ.get("BLENDER_EXE")
    if blender and os.path.isfile(blender):
        return [blender, "-b", "--factory-startup", "--python"]
    # Try local portable Blender in .tools/
    local_bl = os.path.join(REPO_ROOT, ".tools", "blender-5.3.0-windows-x64", "blender.exe")
    if os.path.isfile(local_bl):
        return [local_bl, "-b", "--factory-startup", "--python"]
    # Fall back to current Python interpreter (e.g. PyPI bpy wheel venv)
    return [sys.executable]


def _make_dae(
    pos_text: str = "0 0 0  1 0 0  1 1 0  0 1 0",
    pos_count: int | None = None,
    pos_accessor_count: int | None = None,
    pos_stride: int = 3,
    prim_tag: str = "triangles",
    prim_count: int = 2,
    p_text: str = "0 1 2  0 2 3",
    vcount_text: str | None = None,
    extra_sources: str = "",
    extra_inputs: str = "",
) -> str:
    tokens = pos_text.split()
    if pos_count is None:
        pos_count = len(tokens)
    if pos_accessor_count is None:
        pos_accessor_count = (len(tokens) // pos_stride) if pos_stride > 0 else 0

    vcount_xml = f"<vcount>{vcount_text}</vcount>" if vcount_text is not None else ""
    return f"""<?xml version="1.0" encoding="utf-8"?>
<COLLADA xmlns="http://www.collada.org/2005/11/COLLADASchema" version="1.4.1">
  <asset><up_axis>Z_UP</up_axis></asset>
  <library_geometries>
    <geometry id="TestMesh" name="TestMesh">
      <mesh>
        <source id="pos-src">
          <float_array id="pos-arr" count="{pos_count}">{pos_text}</float_array>
          <technique_common>
            <accessor source="#pos-arr" count="{pos_accessor_count}" stride="{pos_stride}">
              <param name="X" type="float"/>
              <param name="Y" type="float"/>
              <param name="Z" type="float"/>
            </accessor>
          </technique_common>
        </source>
        {extra_sources}
        <vertices id="verts">
          <input semantic="POSITION" source="#pos-src"/>
        </vertices>
        <{prim_tag} count="{prim_count}">
          <input semantic="VERTEX" source="#verts" offset="0"/>
          {extra_inputs}
          {vcount_xml}
          <p>{p_text}</p>
        </{prim_tag}>
      </mesh>
    </geometry>
  </library_geometries>
  <library_visual_scenes>
    <visual_scene id="Scene">
      <node id="TestNode" name="TestNode" type="NODE">
        <instance_geometry url="#TestMesh"/>
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

# Strip args before '--' when invoked via Blender
argv = sys.argv
if "--" in argv:
    argv = argv[argv.index("--") + 1:]
else:
    argv = argv[1:]

repo_root, dae_path, out_json = argv[0], argv[1], argv[2]
sys.path.insert(0, repo_root)

import bpy
import bmesh
import io_scene_collada

bpy.ops.wm.read_factory_settings(use_empty=True)
if not hasattr(bpy.ops.wm, "collada_import"):
    io_scene_collada.register()

from io_scene_collada.import_collada import DocumentImporter, ImportSettings

settings = ImportSettings(filepath=dae_path)
importer = DocumentImporter(bpy.context, settings)
ok = importer.import_document()

meshes_info = []
for me in bpy.data.meshes:
    bm = bmesh.new()
    bm.from_mesh(me)
    bm.free()
    still_invalid = bool(me.validate(verbose=False, clean_customdata=False))
    verts = [[round(v.co.x, 5), round(v.co.y, 5), round(v.co.z, 5)] for v in me.vertices]
    polys = [list(p.vertices) for p in me.polygons]
    meshes_info.append({
        "name": me.name,
        "num_verts": len(me.vertices),
        "num_polys": len(me.polygons),
        "still_invalid": still_invalid,
        "verts": verts,
        "polys": polys,
        "uv_layers": [u.name for u in me.uv_layers],
        "color_attrs": [c.name for c in me.color_attributes],
        "has_custom_normals": bool(me.has_custom_normals),
    })

result = {
    "ok": bool(ok),
    "warnings": list(importer.warnings),
    "errors": list(importer.errors),
    "meshes": meshes_info,
}
with open(out_json, "w", encoding="utf-8") as f:
    json.dump(result, f)
'''


def _run_case(runner: list[str], worker_path: str, dae_path: str, native: int) -> dict:
    out_json = dae_path + f".native{native}.json"
    env = os.environ.copy()
    env["COLLADA_NATIVE"] = str(native)
    if runner[0] == sys.executable:
        cmd = [runner[0], worker_path, REPO_ROOT, dae_path, out_json]
    else:
        cmd = [*runner, worker_path, "--", REPO_ROOT, dae_path, out_json]
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise AssertionError(
            f"Subprocess crashed (rc={proc.returncode}, COLLADA_NATIVE={native}) on {os.path.basename(dae_path)}\n"
            f"STDOUT:\n{proc.stdout[-1500:]}\nSTDERR:\n{proc.stderr[-1500:]}"
        )
    if "Traceback (most recent call last)" in proc.stderr or "Traceback (most recent call last)" in proc.stdout:
        raise AssertionError(
            f"Unexpected traceback (COLLADA_NATIVE={native}) on {os.path.basename(dae_path)}\n"
            f"STDOUT:\n{proc.stdout[-1500:]}\nSTDERR:\n{proc.stderr[-1500:]}"
        )
    with open(out_json, "r", encoding="utf-8") as f:
        data = json.load(f)
    for m in data["meshes"]:
        if m["still_invalid"]:
            raise AssertionError(
                f"Mesh '{m['name']}' still failed me.validate() after import "
                f"(COLLADA_NATIVE={native}, file={os.path.basename(dae_path)})"
            )
    return data


def build_cases() -> dict[str, tuple[str, dict]]:
    """Return mapping of case_name -> (dae_xml, expectations)."""
    norm_source = """
        <source id="norm-src">
          <float_array id="norm-arr" count="6">0 0 1  0 0 1</float_array>
          <technique_common>
            <accessor source="#norm-arr" count="2" stride="3">
              <param name="X" type="float"/><param name="Y" type="float"/><param name="Z" type="float"/>
            </accessor>
          </technique_common>
        </source>
    """
    norm_input = '<input semantic="NORMAL" source="#norm-src" offset="1"/>'
    full_sources = norm_source + """
        <source id="uv-src">
          <float_array id="uv-arr" count="8">0 0  1 0  1 1  0 1</float_array>
          <technique_common>
            <accessor source="#uv-arr" count="4" stride="2">
              <param name="S" type="float"/><param name="T" type="float"/>
            </accessor>
          </technique_common>
        </source>
        <source id="col-src">
          <float_array id="col-arr" count="6">1 0 0  0 1 0</float_array>
          <technique_common>
            <accessor source="#col-arr" count="2" stride="3">
              <param name="R" type="float"/><param name="G" type="float"/><param name="B" type="float"/>
            </accessor>
          </technique_common>
        </source>
    """
    full_inputs = (
        '<input semantic="NORMAL" source="#norm-src" offset="1"/>'
        '<input semantic="TEXCOORD" source="#uv-src" offset="2" set="0"/>'
        '<input semantic="COLOR" source="#col-src" offset="3" set="0"/>'
    )

    return {
        "leading_plus": (
            _make_dae(
                pos_text="+0 +0 +0  +1.5 +0 +0  +1.5 +2.0 +0  +0 +2.0 +0",
                p_text="+0 +1 +2  +0 +2 +3",
            ),
            {"expect_verts": 4, "expect_polys": 2, "compare_native": True},
        ),
        "scientific_and_nan_inf": (
            _make_dae(
                pos_text="0 0 0  1e+0 0 0  1E+00 1 0  nan inf 0",
                p_text="0 1 2  0 2 3",
            ),
            {"expect_verts": 4, "expect_polys": 2, "expect_warning": True},
        ),
        "stray_x_in_float_array": (
            _make_dae(
                pos_text="0 0 0  1 x 0  1 1 0  0 1 0",
                p_text="0 1 2  0 2 3",
            ),
            {"expect_warning": True, "expect_polys": 0},
        ),
        "legacy_3dsmax_qnan": (
            _make_dae(
                pos_text="0 0 0  1 0 0  1 1 0  1.#QNAN 1 0",
                p_text="0 1 2  0 2 3",
            ),
            {"expect_verts": 4, "expect_polys": 2, "expect_warning": True},
        ),
        "p_out_of_range": (
            _make_dae(
                p_text="0 1 2  0 2 99",
            ),
            {"expect_verts": 4, "expect_polys": 1, "expect_warning": True},
        ),
        "p_negative_index": (
            _make_dae(
                p_text="0 1 2  0 -1 3",
            ),
            {"expect_verts": 4, "expect_polys": 1, "expect_warning": True},
        ),
        "normal_index_out_of_range": (
            _make_dae(
                extra_sources=norm_source,
                extra_inputs=norm_input,
                p_text="0 0 1 0 2 0  0 0 2 0 3 99",
            ),
            {"expect_verts": 4, "expect_polys": 1, "expect_warning": True},
        ),
        "vcount_sum_larger_than_p": (
            _make_dae(
                prim_tag="polylist",
                prim_count=3,
                vcount_text="3 3 4",
                p_text="0 1 2  0 2 3  0 1",
            ),
            {"expect_verts": 4, "expect_polys": 2, "expect_warning": True},
        ),
        "lying_counts_too_large": (
            _make_dae(
                pos_count=999,
                pos_accessor_count=333,
                prim_count=50,
                p_text="0 1 2  0 2 3",
            ),
            {"expect_verts": 4, "expect_polys": 2},
        ),
        "lying_counts_too_small": (
            _make_dae(
                pos_count=1,
                pos_accessor_count=1,
                prim_count=1,
                p_text="0 1 2  0 2 3",
            ),
            {"expect_verts": 4},
        ),
        "empty_p": (
            _make_dae(p_text=""),
            {"expect_verts": 4, "expect_polys": 0},
        ),
        "empty_float_array": (
            _make_dae(pos_text="", pos_count=0, pos_accessor_count=0, p_text="0 1 2"),
            {"expect_verts": 0, "expect_polys": 0, "expect_warning": True},
        ),
        "stride_zero": (
            _make_dae(pos_stride=0, p_text="0 1 2"),
            {"expect_polys": 0, "expect_warning": True},
        ),
        "duplicate_vertex_face_validate": (
            _make_dae(p_text="0 1 2  0 0 2"),
            {"expect_verts": 4, "expect_polys": 1, "expect_warning": True},
        ),
        "duplicate_triangle_preserves_attrs": (
            _make_dae(
                extra_sources=full_sources,
                extra_inputs=full_inputs,
                prim_count=3,
                # Tri 0 (0,1,2), Tri 1 exact duplicate (0,1,2), Tri 2 valid (0,2,3)
                p_text=(
                    "0 0 0 0  1 0 1 1  2 0 2 0  "
                    "0 0 0 0  1 0 1 1  2 0 2 0  "
                    "0 0 0 0  2 0 2 0  3 0 3 1"
                ),
            ),
            {
                "expect_verts": 4,
                "expect_polys": 2,
                "expect_warning": True,
                "expect_warning_substr": "1 duplicate",
                "expect_uvs": True,
                "expect_colors": True,
                "expect_custom_normals": True,
            },
        ),
        "degenerate_triangle_preserves_attrs": (
            _make_dae(
                extra_sources=full_sources,
                extra_inputs=full_inputs,
                prim_count=3,
                # Tri 0 valid (0,1,2), Tri 1 degenerate (0,0,2), Tri 2 valid (0,2,3)
                p_text=(
                    "0 0 0 0  1 0 1 1  2 0 2 0  "
                    "0 0 0 0  0 0 0 0  2 0 2 0  "
                    "0 0 0 0  2 0 2 0  3 0 3 1"
                ),
            ),
            {
                "expect_verts": 4,
                "expect_polys": 2,
                "expect_warning": True,
                "expect_warning_substr": "1 degenerate",
                "expect_uvs": True,
                "expect_colors": True,
                "expect_custom_normals": True,
            },
        ),
        "polylist_mixed_vcount_0_1_2": (
            _make_dae(
                extra_sources=full_sources,
                extra_inputs=full_inputs,
                prim_tag="polylist",
                prim_count=5,
                vcount_text="0 3 1 4 2",
                # vcount=0 (0 tuples), vcount=3 (valid tri: 0,1,2), vcount=1 (1 tuple),
                # vcount=4 (valid quad: 0,1,2,3), vcount=2 (2 tuples)
                p_text=(
                    "0 0 0 0  1 0 1 1  2 0 2 0  "
                    "0 0 0 0  "
                    "0 0 0 0  1 0 1 1  2 0 2 0  3 0 3 1  "
                    "0 0 0 0  1 0 1 1"
                ),
            ),
            {
                "expect_verts": 4,
                "expect_polys": 2,
                "expect_warning": True,
                "expect_warning_substr": "3 invalid faces with fewer than 3 vertices skipped",
            },
        ),
        "polylist_all_zero_vcount": (
            _make_dae(
                extra_sources=full_sources,
                extra_inputs=full_inputs,
                prim_tag="polylist",
                prim_count=3,
                vcount_text="0 0 0",
                p_text="0 0 0 0  1 0 1 1  2 0 2 0",
            ),
            {
                "expect_verts": 4,
                "expect_polys": 0,
                "expect_warning": True,
                "expect_warning_substr": "3 invalid faces with fewer than 3 vertices skipped",
            },
        ),
        "polylist_empty_p_nonzero_vcount": (
            _make_dae(
                extra_sources=full_sources,
                extra_inputs=full_inputs,
                prim_tag="polylist",
                prim_count=2,
                vcount_text="3 4",
                p_text="",
            ),
            {"expect_verts": 4, "expect_polys": 0, "expect_warning": True},
        ),
        "polylist_vcount_int32_overflow": (
            _make_dae(
                extra_sources=full_sources,
                extra_inputs=full_inputs,
                prim_tag="polylist",
                prim_count=4,
                vcount_text="2147483647 4 99999 4",
                p_text="0 0 0 0  1 0 1 1  2 0 2 0  3 0 3 1",
            ),
            {"expect_verts": 4, "expect_polys": 0, "expect_warning": True},
        ),
        "normals_stride_1": (
            _make_dae(
                extra_sources="""
        <source id="norm-src">
          <float_array id="norm-arr" count="6">0 0 1  0 0 1</float_array>
          <technique_common>
            <accessor source="#norm-arr" count="6" stride="1">
              <param name="X" type="float"/>
            </accessor>
          </technique_common>
        </source>
""",
                extra_inputs='<input semantic="NORMAL" source="#norm-src" offset="1"/>',
                p_text="0 0 1 0 2 0  0 1 2 1 3 1",
            ),
            {
                "expect_verts": 4,
                "expect_polys": 2,
                "expect_warning": True,
                "expect_warning_substr": "NORMAL source",
            },
        ),
        "uv_stride_1": (
            _make_dae(
                extra_sources="""
        <source id="uv-src">
          <float_array id="uv-arr" count="4">0 1 0 1</float_array>
          <technique_common>
            <accessor source="#uv-arr" count="4" stride="1">
              <param name="S" type="float"/>
            </accessor>
          </technique_common>
        </source>
""",
                extra_inputs='<input semantic="TEXCOORD" source="#uv-src" offset="1" set="0"/>',
                p_text="0 0 1 1 2 2  0 0 2 2 3 3",
            ),
            {
                "expect_verts": 4,
                "expect_polys": 2,
                "expect_warning": True,
                "expect_warning_substr": "TEXCOORD source",
            },
        ),
        "color_stride_2": (
            _make_dae(
                extra_sources="""
        <source id="col-src">
          <float_array id="col-arr" count="4">1 0  0 1</float_array>
          <technique_common>
            <accessor source="#col-arr" count="2" stride="2">
              <param name="R" type="float"/><param name="G" type="float"/>
            </accessor>
          </technique_common>
        </source>
""",
                extra_inputs='<input semantic="COLOR" source="#col-src" offset="1" set="0"/>',
                p_text="0 0 1 1 2 0  0 0 2 0 3 1",
            ),
            {
                "expect_verts": 4,
                "expect_polys": 2,
                "expect_warning": True,
                "expect_warning_substr": "COLOR source",
            },
        ),
        "skin_vcount_int32_overflow": (
            """<?xml version="1.0" encoding="utf-8"?>
<COLLADA xmlns="http://www.collada.org/2005/11/COLLADASchema" version="1.4.1">
  <asset><up_axis>Z_UP</up_axis></asset>
  <library_geometries>
    <geometry id="SkinMesh" name="SkinMesh">
      <mesh>
        <source id="pos-src">
          <float_array id="pos-arr" count="12">0 0 0 1 0 0 1 1 0 0 1 0</float_array>
          <technique_common><accessor source="#pos-arr" count="4" stride="3"><param name="X" type="float"/><param name="Y" type="float"/><param name="Z" type="float"/></accessor></technique_common>
        </source>
        <vertices id="verts"><input semantic="POSITION" source="#pos-src"/></vertices>
        <triangles count="2"><input semantic="VERTEX" source="#verts" offset="0"/><p>0 1 2 0 2 3</p></triangles>
      </mesh>
    </geometry>
  </library_geometries>
  <library_controllers>
    <controller id="Arm-Skin">
      <skin source="#SkinMesh">
        <bind_shape_matrix>1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1</bind_shape_matrix>
        <source id="j-src"><Name_array id="j-arr" count="1">BoneA</Name_array><technique_common><accessor source="#j-arr" count="1" stride="1"><param name="JOINT" type="name"/></accessor></technique_common></source>
        <source id="ibm-src"><float_array id="ibm-arr" count="16">1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1</float_array><technique_common><accessor source="#ibm-arr" count="1" stride="16"><param name="TRANSFORM" type="float4x4"/></accessor></technique_common></source>
        <source id="w-src"><float_array id="w-arr" count="1">1.0</float_array><technique_common><accessor source="#w-arr" count="1" stride="1"><param name="WEIGHT" type="float"/></accessor></technique_common></source>
        <joints><input semantic="JOINT" source="#j-src"/><input semantic="INV_BIND_MATRIX" source="#ibm-src"/></joints>
        <vertex_weights count="4">
          <input semantic="JOINT" source="#j-src" offset="0"/>
          <input semantic="WEIGHT" source="#w-src" offset="1"/>
          <vcount>1 2147483647 1 1</vcount>
          <v>0 0 0 0</v>
        </vertex_weights>
      </skin>
    </controller>
  </library_controllers>
  <library_visual_scenes>
    <visual_scene id="Scene">
      <node id="Arm" name="Arm" type="NODE">
        <node id="BoneA" name="BoneA" sid="BoneA" type="JOINT"><matrix sid="transform">1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1</matrix></node>
        <node id="SkinNode" name="SkinNode" type="NODE"><instance_controller url="#Arm-Skin"/></node>
      </node>
    </visual_scene>
  </library_visual_scenes>
  <scene><instance_visual_scene url="#Scene"/></scene>
</COLLADA>
""",
            {
                "expect_verts": 4,
                "expect_polys": 2,
                "expect_warning": True,
                "expect_warning_substr": "vertex_weights",
            },
        ),
        "anim_accessor_stride_oops": (
            """<?xml version="1.0" encoding="utf-8"?>
<COLLADA xmlns="http://www.collada.org/2005/11/COLLADASchema" version="1.4.1">
  <asset><up_axis>Z_UP</up_axis></asset>
  <library_geometries>
    <geometry id="TriMesh" name="TriMesh">
      <mesh>
        <source id="pos-src">
          <float_array id="pos-arr" count="9">0 0 0 1 0 0 0 1 0</float_array>
          <technique_common><accessor source="#pos-arr" count="3" stride="3"><param name="X" type="float"/><param name="Y" type="float"/><param name="Z" type="float"/></accessor></technique_common>
        </source>
        <vertices id="verts"><input semantic="POSITION" source="#pos-src"/></vertices>
        <triangles count="1"><input semantic="VERTEX" source="#verts" offset="0"/><p>0 1 2</p></triangles>
      </mesh>
    </geometry>
  </library_geometries>
  <library_animations>
    <animation id="Anim1">
      <source id="Anim1-in">
        <float_array id="Anim1-in-arr" count="2">0.0 1.0</float_array>
        <technique_common><accessor source="#Anim1-in-arr" count="2" stride="oops"><param name="TIME" type="float"/></accessor></technique_common>
      </source>
      <source id="Anim1-out">
        <float_array id="Anim1-out-arr" count="2">0.0 5.0</float_array>
        <technique_common><accessor source="#Anim1-out-arr" count="2" stride="1"><param name="X" type="float"/></accessor></technique_common>
      </source>
      <sampler id="Anim1-samp">
        <input semantic="INPUT" source="#Anim1-in"/>
        <input semantic="OUTPUT" source="#Anim1-out"/>
      </sampler>
      <channel source="#Anim1-samp" target="TriNode/location.X"/>
    </animation>
  </library_animations>
  <library_visual_scenes>
    <visual_scene id="Scene">
      <node id="TriNode" name="TriNode" type="NODE">
        <translate sid="location">0 0 0</translate>
        <instance_geometry url="#TriMesh"/>
      </node>
    </visual_scene>
  </library_visual_scenes>
  <scene><instance_visual_scene url="#Scene"/></scene>
</COLLADA>
""",
            {
                "expect_verts": 3,
                "expect_polys": 1,
                "expect_warning": True,
                "expect_warning_substr": "animation source",
            },
        ),
        "trailing_garbage_last_token": (
            _make_dae(
                pos_text="0 0 0 1 0 0 0 1 0x",
                p_text="0 1 2",
                prim_count=1,
            ),
            {"expect_warning": True, "compare_native": True},
        ),
        "dtd_rejected": (
            """<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE COLLADA [<!ENTITY lol "lol">]>
<COLLADA xmlns="http://www.collada.org/2005/11/COLLADASchema" version="1.4.1">
  <asset><up_axis>Z_UP</up_axis></asset>
  <library_visual_scenes><visual_scene id="Scene"/></library_visual_scenes>
  <scene><instance_visual_scene url="#Scene"/></scene>
</COLLADA>
""",
            {"expect_ok": False, "expect_error_substr": "DTDs are not supported"},
        ),
    }


EXCEPTION_GUARD_WORKER = r'''
import json
import os
import sys

argv = sys.argv
argv = argv[argv.index("--") + 1:] if "--" in argv else argv[1:]
repo_root, dae_path, zip_main_path, zip_all_path, out_json = argv[0], argv[1], argv[2], argv[3], argv[4]
sys.path.insert(0, repo_root)

import bpy
import io_scene_collada
from io_scene_collada.import_collada import DocumentImporter, ImportSettings

if not hasattr(bpy.ops.wm, "collada_import"):
    io_scene_collada.register()


def _reset_with_existing_cube():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.mesh.primitive_cube_add()
    bpy.context.active_object.name = "ExistingCube"
    bpy.context.active_object.data.name = "ExistingCubeMesh"


out = {}

# 1. Disk .dae path: mid-import exception must return False, record clean error, and remove partial objects
_reset_with_existing_cube()
imp = DocumentImporter(bpy.context, ImportSettings(filepath=dae_path))
def _boom_disk(lib):
    raise RuntimeError("boom_disk")
imp._read_library_animations = _boom_disk
try:
    ok = imp.import_document()
    out["disk"] = {
        "raised": False,
        "ok": bool(ok),
        "errors": list(imp.errors),
        "objects": sorted(o.name for o in bpy.data.objects),
        "meshes": sorted(m.name for m in bpy.data.meshes),
    }
except Exception as e:
    out["disk"] = {"raised": True, "exc": repr(e)}

# 2. Archive MAIN mode: mid-import exception must return False, record clean error, and remove partial objects
_reset_with_existing_cube()
imp2 = DocumentImporter(bpy.context, ImportSettings(filepath=zip_main_path, archive_mode="MAIN"))
def _boom_zip_main(lib):
    raise RuntimeError("boom_zip_main")
imp2._read_library_animations = _boom_zip_main
try:
    ok2 = imp2.import_document()
    out["zip_main"] = {
        "raised": False,
        "ok": bool(ok2),
        "errors": list(imp2.errors),
        "objects": sorted(o.name for o in bpy.data.objects),
        "meshes": sorted(m.name for m in bpy.data.meshes),
    }
except Exception as e:
    out["zip_main"] = {"raised": True, "exc": repr(e)}

# 3. Archive ALL mode (good.dae + bad.dae): bad.dae failure cleaned up (including sub-collection), good.dae succeeds
_reset_with_existing_cube()
imp3 = DocumentImporter(bpy.context, ImportSettings(filepath=zip_all_path, archive_mode="ALL"))
orig_read_anim = imp3._read_library_animations
def _boom_on_bad(lib):
    if any(a.attrib.get("id") == "BadAnim" for a in lib):
        raise RuntimeError("boom_zip_all")
    return orig_read_anim(lib)
imp3._read_library_animations = _boom_on_bad
try:
    ok3 = imp3.import_document()
    out["zip_all"] = {
        "raised": False,
        "ok": bool(ok3),
        "errors": list(imp3.errors),
        "warnings": list(imp3.warnings),
        "objects": sorted(o.name for o in bpy.data.objects),
        "meshes": sorted(m.name for m in bpy.data.meshes),
        "collections": sorted(c.name for c in bpy.data.collections),
    }
except Exception as e:
    out["zip_all"] = {"raised": True, "exc": repr(e)}

with open(out_json, "w", encoding="utf-8") as fh:
    json.dump(out, fh)
'''


def _run_exception_guard_tests(runner: list[str], workdir: str) -> None:
    good_dae = """<?xml version="1.0" encoding="utf-8"?>
<COLLADA xmlns="http://www.collada.org/2005/11/COLLADASchema" version="1.4.1">
  <asset><up_axis>Z_UP</up_axis></asset>
  <library_geometries>
    <geometry id="GoodMesh" name="GoodMesh">
      <mesh>
        <source id="p"><float_array id="pa" count="9">0 0 0 1 0 0 0 1 0</float_array>
          <technique_common><accessor source="#pa" count="3" stride="3"><param name="X" type="float"/><param name="Y" type="float"/><param name="Z" type="float"/></accessor></technique_common></source>
        <vertices id="v"><input semantic="POSITION" source="#p"/></vertices>
        <triangles count="1"><input semantic="VERTEX" source="#v" offset="0"/><p>0 1 2</p></triangles>
      </mesh>
    </geometry>
  </library_geometries>
  <library_visual_scenes><visual_scene id="Scene">
    <node id="GoodNode" name="GoodNode" type="NODE"><instance_geometry url="#GoodMesh"/></node>
  </visual_scene></library_visual_scenes>
  <scene><instance_visual_scene url="#Scene"/></scene>
</COLLADA>"""
    bad_dae = """<?xml version="1.0" encoding="utf-8"?>
<COLLADA xmlns="http://www.collada.org/2005/11/COLLADASchema" version="1.4.1">
  <asset><up_axis>Z_UP</up_axis></asset>
  <library_geometries>
    <geometry id="BadMesh" name="BadMesh">
      <mesh>
        <source id="p"><float_array id="pa" count="9">0 0 0 1 0 0 0 1 0</float_array>
          <technique_common><accessor source="#pa" count="3" stride="3"><param name="X" type="float"/><param name="Y" type="float"/><param name="Z" type="float"/></accessor></technique_common></source>
        <vertices id="v"><input semantic="POSITION" source="#p"/></vertices>
        <triangles count="1"><input semantic="VERTEX" source="#v" offset="0"/><p>0 1 2</p></triangles>
      </mesh>
    </geometry>
  </library_geometries>
  <library_animations><animation id="BadAnim"/></library_animations>
  <library_visual_scenes><visual_scene id="Scene">
    <node id="BadNode" name="BadNode" type="NODE"><instance_geometry url="#BadMesh"/></node>
  </visual_scene></library_visual_scenes>
  <scene><instance_visual_scene url="#Scene"/></scene>
</COLLADA>"""
    dae_p = os.path.join(workdir, "guard_bad.dae")
    with open(dae_p, "w", encoding="utf-8") as fh:
        fh.write(bad_dae)
    zip_main = os.path.join(workdir, "guard_main.zip")
    with zipfile.ZipFile(zip_main, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("bad.dae", bad_dae)
    zip_all = os.path.join(workdir, "guard_all.zip")
    with zipfile.ZipFile(zip_all, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("good.dae", good_dae)
        zf.writestr("bad.dae", bad_dae)

    wpath = os.path.join(workdir, "_guard_worker.py")
    out_json = os.path.join(workdir, "_guard_out.json")
    with open(wpath, "w", encoding="utf-8") as fh:
        fh.write(EXCEPTION_GUARD_WORKER)

    cmd = (
        [runner[0], wpath, REPO_ROOT, dae_p, zip_main, zip_all, out_json]
        if runner[0] == sys.executable
        else [*runner, wpath, "--", REPO_ROOT, dae_p, zip_main, zip_all, out_json]
    )
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, f"Guard worker failed:\n{proc.stderr[-1500:]}"
    res = json.load(open(out_json, "r", encoding="utf-8"))

    # Disk check
    d = res["disk"]
    assert d["raised"] is False and d["ok"] is False, d
    assert d["errors"] == ["Import failed: boom_disk"], d
    assert d["objects"] == ["ExistingCube"] and d["meshes"] == ["ExistingCubeMesh"], d

    # Archive MAIN check
    zm = res["zip_main"]
    assert zm["raised"] is False and zm["ok"] is False, zm
    assert zm["errors"] == ["Import failed: boom_zip_main"], zm
    assert zm["objects"] == ["ExistingCube"] and zm["meshes"] == ["ExistingCubeMesh"], zm

    # Archive ALL check
    za = res["zip_all"]
    assert za["raised"] is False and za["ok"] is True, za
    assert any("boom_zip_all" in w for w in za["warnings"]), za
    assert za["objects"] == ["ExistingCube", "GoodNode"], za
    assert za["meshes"] == ["ExistingCubeMesh", "GoodMesh"], za
    assert za["collections"] == ["good"], za
    print("  PASS import_tree_exception_guard_and_cleanup (disk, zip MAIN, zip ALL)")


def main() -> int:
    runner = _find_runner()
    print(f"Running malformed .dae tests using: {' '.join(runner)}")
    workdir = tempfile.mkdtemp(prefix="collada_malformed_")
    try:
        worker_path = os.path.join(workdir, "_worker.py")
        with open(worker_path, "w", encoding="utf-8") as f:
            f.write(WORKER_SCRIPT)

        cases = build_cases()
        failures: list[str] = []

        for name, (dae_xml, exp) in cases.items():
            dae_path = os.path.join(workdir, f"{name}.dae")
            with open(dae_path, "w", encoding="utf-8") as f:
                f.write(dae_xml)

            results = {}
            for native in (1, 0):
                try:
                    res = _run_case(runner, worker_path, dae_path, native)
                    results[native] = res
                    if "expect_ok" in exp and res["ok"] is not exp["expect_ok"]:
                        raise AssertionError(
                            f"{name} (native={native}): expected ok={exp['expect_ok']}, got {res['ok']} (errors={res['errors']})"
                        )
                    if "expect_error_substr" in exp:
                        esub = exp["expect_error_substr"]
                        if not any(esub in e for e in res["errors"]):
                            raise AssertionError(
                                f"{name} (native={native}): expected error containing {esub!r}, got {res['errors']}"
                            )
                    if exp.get("expect_warning") and not res["warnings"]:
                        raise AssertionError(f"Expected warning for {name} (native={native}), got none")
                    if "expect_warning_substr" in exp:
                        sub = exp["expect_warning_substr"]
                        if not any(sub in w for w in res["warnings"]):
                            raise AssertionError(
                                f"{name} (native={native}): expected warning containing {sub!r}, got {res['warnings']}"
                            )
                    if "expect_verts" in exp:
                        got_v = res["meshes"][0]["num_verts"] if res["meshes"] else 0
                        if got_v != exp["expect_verts"]:
                            raise AssertionError(
                                f"{name} (native={native}): expected {exp['expect_verts']} verts, got {got_v}"
                            )
                    if "expect_polys" in exp:
                        got_p = res["meshes"][0]["num_polys"] if res["meshes"] else 0
                        if got_p != exp["expect_polys"]:
                            raise AssertionError(
                                f"{name} (native={native}): expected {exp['expect_polys']} polys, got {got_p}"
                            )
                    if exp.get("expect_uvs"):
                        if not res["meshes"] or not res["meshes"][0]["uv_layers"]:
                            raise AssertionError(
                                f"{name} (native={native}): UVs were lost! warnings={res['warnings']}"
                            )
                    if exp.get("expect_colors"):
                        if not res["meshes"] or not res["meshes"][0]["color_attrs"]:
                            raise AssertionError(
                                f"{name} (native={native}): vertex colors were lost! warnings={res['warnings']}"
                            )
                    if exp.get("expect_custom_normals"):
                        if not res["meshes"] or not res["meshes"][0]["has_custom_normals"]:
                            raise AssertionError(
                                f"{name} (native={native}): custom normals were lost! warnings={res['warnings']}"
                            )
                except Exception as exc:
                    failures.append(f"FAIL {name} (native={native}): {exc}")

            if exp.get("compare_native") and 1 in results and 0 in results:
                if results[1]["meshes"] != results[0]["meshes"]:
                    failures.append(
                        f"FAIL {name}: native=1 and native=0 produced different meshes!\n"
                        f"  native=1: {results[1]['meshes']}\n  native=0: {results[0]['meshes']}"
                    )

            if not any(f.startswith(f"FAIL {name}") for f in failures):
                w_count = len(results.get(0, {}).get("warnings", []))
                print(f"  PASS {name} (warnings={w_count})")

        if failures:
            print("\n".join(failures))
            return 1
        _run_exception_guard_tests(runner, workdir)
        print(f"\nAll {len(cases)} malformed .dae test cases + exception guard passed (COLLADA_NATIVE=1 & 0)!")
        return 0
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
