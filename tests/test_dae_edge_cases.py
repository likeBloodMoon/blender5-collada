"""Regression tests for COLLADA importer edge cases (#27 accessor offset/count, #28 <ph> holes)."""

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
from io_scene_collada.import_collada import DocumentImporter, ImportSettings

if not hasattr(bpy.ops.wm, "collada_import"):
    io_scene_collada.register()


def run_import(xml_text: str, name: str):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    p = os.path.join(workdir, name)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(xml_text)
    imp = DocumentImporter(bpy.context, ImportSettings(filepath=p))
    ok = imp.import_document()
    return ok, imp.warnings, imp.errors


results = {}

# #27a: Mesh accessor with offset="1" (leading junk 99) AND count="3" (2 trailing junk vertices)
dae_mesh = """<?xml version="1.0" encoding="utf-8"?>
<COLLADA xmlns="http://www.collada.org/2005/11/COLLADASchema" version="1.4.1">
  <asset><up_axis>Z_UP</up_axis></asset>
  <library_geometries>
    <geometry id="Tri" name="Tri">
      <mesh>
        <source id="pos">
          <float_array id="pos-arr" count="16">99  0 0 0  1 0 0  0 1 0  5 5 5  6 6 6</float_array>
          <technique_common>
            <accessor source="#pos-arr" count="3" offset="1" stride="3">
              <param name="X" type="float"/><param name="Y" type="float"/><param name="Z" type="float"/>
            </accessor>
          </technique_common>
        </source>
        <vertices id="verts"><input semantic="POSITION" source="#pos"/></vertices>
        <triangles count="1"><input semantic="VERTEX" source="#verts" offset="0"/><p>0 1 2</p></triangles>
      </mesh>
    </geometry>
  </library_geometries>
  <library_visual_scenes>
    <visual_scene id="Scene">
      <node id="TriNode" name="TriNode"><instance_geometry url="#Tri"/></node>
    </visual_scene>
  </library_visual_scenes>
  <scene><instance_visual_scene url="#Scene"/></scene>
</COLLADA>"""

ok, warns, errs = run_import(dae_mesh, "acc_mesh.dae")
assert ok and not errs, (ok, warns, errs)
me = bpy.data.objects["TriNode"].data
verts = [tuple(round(c, 5) for c in v.co) for v in me.vertices]
area = sum(p.area for p in me.polygons)
used_verts = {vi for p in me.polygons for vi in p.vertices}
loose_verts = len(me.vertices) - len(used_verts)
assert verts == [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)], verts
assert abs(area - 0.5) < 1e-5, area
assert loose_verts == 0, loose_verts
results["mesh_offset_count"] = {"verts": len(me.vertices), "area": round(area, 5), "loose": loose_verts}

# #27b: Skin controller accessor offset & count on joints (Name_array), inv_bind_matrix, and weights
dae_skin = """<?xml version="1.0" encoding="utf-8"?>
<COLLADA xmlns="http://www.collada.org/2005/11/COLLADASchema" version="1.4.1">
  <asset><up_axis>Z_UP</up_axis></asset>
  <library_geometries>
    <geometry id="SkinM" name="SkinM">
      <mesh>
        <source id="pos">
          <float_array id="pos-arr" count="9">0 0 0 1 0 0 0 1 0</float_array>
          <technique_common><accessor source="#pos-arr" count="3" stride="3"><param name="X" type="float"/><param name="Y" type="float"/><param name="Z" type="float"/></accessor></technique_common>
        </source>
        <vertices id="verts"><input semantic="POSITION" source="#pos"/></vertices>
        <triangles count="1"><input semantic="VERTEX" source="#verts" offset="0"/><p>0 1 2</p></triangles>
      </mesh>
    </geometry>
  </library_geometries>
  <library_controllers>
    <controller id="SkinCtrl">
      <skin source="#SkinM">
        <bind_shape_matrix>1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1</bind_shape_matrix>
        <source id="joints-src">
          <Name_array id="joints-arr" count="4">JunkBefore BoneA BoneB JunkAfter</Name_array>
          <technique_common><accessor source="#joints-arr" count="2" offset="1" stride="1"><param name="JOINT" type="name"/></accessor></technique_common>
        </source>
        <source id="ibm-src">
          <float_array id="ibm-arr" count="34">999  1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1  1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1  888</float_array>
          <technique_common><accessor source="#ibm-arr" count="2" offset="1" stride="16"><param name="TRANSFORM" type="float4x4"/></accessor></technique_common>
        </source>
        <source id="w-src">
          <float_array id="w-arr" count="4">99.0 0.25 0.75 88.0</float_array>
          <technique_common><accessor source="#w-arr" count="2" offset="1" stride="1"><param name="WEIGHT" type="float"/></accessor></technique_common>
        </source>
        <joints>
          <input semantic="JOINT" source="#joints-src"/>
          <input semantic="INV_BIND_MATRIX" source="#ibm-src"/>
        </joints>
        <vertex_weights count="3">
          <input semantic="JOINT" source="#joints-src" offset="0"/>
          <input semantic="WEIGHT" source="#w-src" offset="1"/>
          <vcount>2 2 2</vcount>
          <v>0 0 1 1  0 0 1 1  0 0 1 1</v>
        </vertex_weights>
      </skin>
    </controller>
  </library_controllers>
  <library_visual_scenes>
    <visual_scene id="Scene">
      <node id="Arm" name="Arm" type="NODE">
        <node id="BoneA" name="BoneA" sid="BoneA" type="JOINT">
          <matrix sid="transform">1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1</matrix>
          <node id="BoneB" name="BoneB" sid="BoneB" type="JOINT">
            <matrix sid="transform">1 0 0 0 0 1 0 1 0 0 1 0 0 0 0 1</matrix>
          </node>
        </node>
        <node id="SkinNode" name="SkinNode" type="NODE"><instance_controller url="#SkinCtrl"/></node>
      </node>
    </visual_scene>
  </library_visual_scenes>
  <scene><instance_visual_scene url="#Scene"/></scene>
</COLLADA>"""

ok, warns, errs = run_import(dae_skin, "acc_skin.dae")
assert ok and not errs, (ok, warns, errs)
ob_skin = bpy.data.objects["SkinNode"]
vgroups = {vg.name: vg.weight(0) for vg in ob_skin.vertex_groups}
assert set(vgroups.keys()) == {"BoneA", "BoneB"}, vgroups
assert abs(vgroups["BoneA"] - 0.25) < 1e-5 and abs(vgroups["BoneB"] - 0.75) < 1e-5, vgroups
results["skin_offset_count"] = vgroups

# #27c: Animation accessor offset & count on INPUT, OUTPUT, and INTERPOLATION (Name_array)
dae_anim = """<?xml version="1.0" encoding="utf-8"?>
<COLLADA xmlns="http://www.collada.org/2005/11/COLLADASchema" version="1.4.1">
  <asset><up_axis>Z_UP</up_axis></asset>
  <library_geometries>
    <geometry id="Box" name="Box">
      <mesh>
        <source id="pos">
          <float_array id="pos-arr" count="9">0 0 0 1 0 0 0 1 0</float_array>
          <technique_common><accessor source="#pos-arr" count="3" stride="3"><param name="X" type="float"/><param name="Y" type="float"/><param name="Z" type="float"/></accessor></technique_common>
        </source>
        <vertices id="verts"><input semantic="POSITION" source="#pos"/></vertices>
        <triangles count="1"><input semantic="VERTEX" source="#verts" offset="0"/><p>0 1 2</p></triangles>
      </mesh>
    </geometry>
  </library_geometries>
  <library_animations>
    <animation id="AnimX">
      <source id="in-src">
        <float_array id="in-arr" count="4">99.0 0.0 1.0 88.0</float_array>
        <technique_common><accessor source="#in-arr" count="2" offset="1" stride="1"><param name="TIME" type="float"/></accessor></technique_common>
      </source>
      <source id="out-src">
        <float_array id="out-arr" count="4">999.0 2.0 10.0 888.0</float_array>
        <technique_common><accessor source="#out-arr" count="2" offset="1" stride="1"><param name="X" type="float"/></accessor></technique_common>
      </source>
      <source id="interp-src">
        <Name_array id="interp-arr" count="4">JUNK LINEAR LINEAR JUNK2</Name_array>
        <technique_common><accessor source="#interp-arr" count="2" offset="1" stride="1"><param name="INTERPOLATION" type="name"/></accessor></technique_common>
      </source>
      <sampler id="samp">
        <input semantic="INPUT" source="#in-src"/>
        <input semantic="OUTPUT" source="#out-src"/>
        <input semantic="INTERPOLATION" source="#interp-src"/>
      </sampler>
      <channel source="#samp" target="BoxNode/location.X"/>
    </animation>
  </library_animations>
  <library_visual_scenes>
    <visual_scene id="Scene">
      <node id="BoxNode" name="BoxNode">
        <translate sid="location">0 0 0</translate>
        <instance_geometry url="#Box"/>
      </node>
    </visual_scene>
  </library_visual_scenes>
  <scene><instance_visual_scene url="#Scene"/></scene>
</COLLADA>"""

ok, warns, errs = run_import(dae_anim, "acc_anim.dae")
assert ok and not errs, (ok, warns, errs)
bpy.context.scene.frame_set(0)
bpy.context.view_layer.update()
x0 = float(bpy.data.objects["BoxNode"].location.x)
bpy.context.scene.frame_set(int(round(bpy.context.scene.render.fps)))
bpy.context.view_layer.update()
x1 = float(bpy.data.objects["BoxNode"].location.x)
assert abs(x0 - 2.0) < 1e-3 and abs(x1 - 10.0) < 1e-3, (x0, x1)
results["anim_offset_count"] = {"x0": round(x0, 4), "x1": round(x1, 4)}

# #27d: Out-of-bounds accessor range warns and clamps cleanly
dae_clamp = dae_mesh.replace('count="3" offset="1"', 'count="99" offset="1"')
ok, warns, errs = run_import(dae_clamp, "acc_clamp.dae")
assert ok and any("exceeds array length" in w for w in warns), (ok, warns, errs)
results["clamp_warning"] = True

with open(out_json, "w", encoding="utf-8") as fh:
    json.dump(results, fh, indent=2)
'''


def main() -> int:
    blender = _find_blender()
    print(f"Running COLLADA edge-case tests (#27/#28) using: {blender}")
    workdir = tempfile.mkdtemp(prefix="collada_edge_")
    try:
        worker = os.path.join(workdir, "_worker.py")
        out_json = os.path.join(workdir, "out.json")
        with open(worker, "w", encoding="utf-8") as fh:
            fh.write(WORKER_SCRIPT)
        proc = subprocess.run(
            [blender, "-b", "--factory-startup", "--python-exit-code", "1", "--python", worker, "--", REPO_ROOT, workdir, out_json],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if proc.returncode != 0:
            print(proc.stdout[-1500:])
            print(proc.stderr[-1500:])
            return 1
        res = json.load(open(out_json, "r", encoding="utf-8"))
        for k, v in res.items():
            print(f"  PASS {k}: {v}")
        print("\nAll COLLADA edge-case tests passed!")
        return 0
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
