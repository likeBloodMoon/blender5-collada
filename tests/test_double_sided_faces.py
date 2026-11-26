#!/usr/bin/env python3
"""Regression tests for #26: double-sided faces option (KEEP / MERGE / REMOVE),
exact duplicate filtering (winding + material + UV + normal), and double_sided export
for both .dae and .cdae."""

from __future__ import annotations

import glob
import os
import shutil
import subprocess
import sys
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS_DIR = os.path.join(REPO_ROOT, ".tools")


def find_blender5() -> str:
    env = os.environ.get("BLENDER_5X")
    if env and os.path.exists(env):
        return env
    for pat in (
        os.path.join(TOOLS_DIR, "blender-5.*", "blender.exe"),
        os.path.join(TOOLS_DIR, "blender-5.*", "blender"),
    ):
        hits = sorted(glob.glob(pat))
        if hits:
            return hits[-1]
    which = shutil.which("blender")
    if which:
        return which
    raise RuntimeError("Blender 5.x executable not found; set BLENDER_5X")


WORKER = r"""
import os
import sys
import xml.etree.ElementTree as ET
import bpy
import numpy as np

tmpdir = sys.argv[-1]


def write_test_dae(path: str) -> None:
    # 5 disjoint triangles (15 vertices) -> 5 double-sided pairs (front + back) + 3 exact duplicates of front tris 0,1,2
    verts = []
    for i in range(5):
        x = float(i * 2)
        verts.extend([
            (x, 0.0, 0.0),
            (x + 1.0, 0.0, 0.0),
            (x, 1.0, 0.0),
        ])
    pos_str = " ".join(f"{c:.6f}" for v in verts for c in v)
    # Normals: 0 = +Z, 1 = -Z
    norm_str = "0 0 1 0 0 -1"
    # UVs: 0=(0,0), 1=(1,0), 2=(0,1)
    uv_str = "0 0 1 0 0 1"

    p_tokens = []
    # 5 front faces (+Z)
    for i in range(5):
        v0, v1, v2 = i * 3, i * 3 + 1, i * 3 + 2
        p_tokens.extend([v0, 0, 0, v1, 0, 1, v2, 0, 2])
    # 5 back faces (-Z, reversed winding)
    for i in range(5):
        v0, v1, v2 = i * 3, i * 3 + 1, i * 3 + 2
        p_tokens.extend([v0, 1, 0, v2, 1, 2, v1, 1, 1])
    # 3 exact duplicates of front faces 0, 1, 2
    for i in range(3):
        v0, v1, v2 = i * 3, i * 3 + 1, i * 3 + 2
        p_tokens.extend([v0, 0, 0, v1, 0, 1, v2, 0, 2])

    p_str = " ".join(str(x) for x in p_tokens)
    n_tris = 13

    dae = f'''<?xml version="1.0" encoding="utf-8"?>
<COLLADA xmlns="http://www.collada.org/2005/11/COLLADASchema" version="1.4.1">
  <asset><up_axis>Z_UP</up_axis></asset>
  <library_materials>
    <material id="MatA-material" name="MatA"><instance_effect url="#MatA-effect"/></material>
  </library_materials>
  <library_effects>
    <effect id="MatA-effect"><profile_COMMON><technique sid="common"><lambert><diffuse><color>0.8 0.2 0.2 1</color></diffuse></lambert></technique></profile_COMMON></effect>
  </library_effects>
  <library_geometries>
    <geometry id="DSMesh-mesh" name="DSMesh">
      <mesh>
        <source id="DSMesh-pos">
          <float_array id="DSMesh-pos-arr" count="{len(verts) * 3}">{pos_str}</float_array>
          <technique_common><accessor source="#DSMesh-pos-arr" count="{len(verts)}" stride="3">
            <param name="X" type="float"/><param name="Y" type="float"/><param name="Z" type="float"/>
          </accessor></technique_common>
        </source>
        <source id="DSMesh-norm">
          <float_array id="DSMesh-norm-arr" count="6">{norm_str}</float_array>
          <technique_common><accessor source="#DSMesh-norm-arr" count="2" stride="3">
            <param name="X" type="float"/><param name="Y" type="float"/><param name="Z" type="float"/>
          </accessor></technique_common>
        </source>
        <source id="DSMesh-uv">
          <float_array id="DSMesh-uv-arr" count="6">{uv_str}</float_array>
          <technique_common><accessor source="#DSMesh-uv-arr" count="3" stride="2">
            <param name="S" type="float"/><param name="T" type="float"/>
          </accessor></technique_common>
        </source>
        <vertices id="DSMesh-verts"><input semantic="POSITION" source="#DSMesh-pos"/></vertices>
        <triangles material="MatA-material" count="{n_tris}">
          <input semantic="VERTEX" source="#DSMesh-verts" offset="0"/>
          <input semantic="NORMAL" source="#DSMesh-norm" offset="1"/>
          <input semantic="TEXCOORD" source="#DSMesh-uv" offset="2" set="0"/>
          <p>{p_str}</p>
        </triangles>
      </mesh>
    </geometry>
  </library_geometries>
  <library_visual_scenes>
    <visual_scene id="Scene" name="Scene">
      <node id="DSObj" name="DSObj" type="NODE">
        <instance_geometry url="#DSMesh-mesh">
          <bind_material><technique_common>
            <instance_material symbol="MatA-material" target="#MatA-material"/>
          </technique_common></bind_material>
        </instance_geometry>
      </node>
    </visual_scene>
  </library_visual_scenes>
  <scene><instance_visual_scene url="#Scene"/></scene>
</COLLADA>'''
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(dae)


def write_diff_attr_dae(path: str) -> None:
    # Tri 0: same vertices (0,1,2), same winding, DIFFERENT material (Mat0 vs Mat1)
    # Tri 1: same vertices (3,4,5), same winding, same material (Mat0), DIFFERENT UVs
    verts = [
        (0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0),
        (2.0, 0.0, 0.0), (3.0, 0.0, 0.0), (2.0, 1.0, 0.0),
    ]
    pos_str = " ".join(f"{c:.6f}" for v in verts for c in v)
    dae = f'''<?xml version="1.0" encoding="utf-8"?>
<COLLADA xmlns="http://www.collada.org/2005/11/COLLADASchema" version="1.4.1">
  <asset><up_axis>Z_UP</up_axis></asset>
  <library_materials>
    <material id="Mat0-material" name="Mat0"><instance_effect url="#Mat0-effect"/></material>
    <material id="Mat1-material" name="Mat1"><instance_effect url="#Mat1-effect"/></material>
  </library_materials>
  <library_effects>
    <effect id="Mat0-effect"><profile_COMMON><technique sid="common"><lambert><diffuse><color>1 0 0 1</color></diffuse></lambert></technique></profile_COMMON></effect>
    <effect id="Mat1-effect"><profile_COMMON><technique sid="common"><lambert><diffuse><color>0 1 0 1</color></diffuse></lambert></technique></profile_COMMON></effect>
  </library_effects>
  <library_geometries>
    <geometry id="Overlay-mesh" name="Overlay">
      <mesh>
        <source id="Overlay-pos">
          <float_array id="Overlay-pos-arr" count="18">{pos_str}</float_array>
          <technique_common><accessor source="#Overlay-pos-arr" count="6" stride="3">
            <param name="X" type="float"/><param name="Y" type="float"/><param name="Z" type="float"/>
          </accessor></technique_common>
        </source>
        <source id="Overlay-norm">
          <float_array id="Overlay-norm-arr" count="3">0 0 1</float_array>
          <technique_common><accessor source="#Overlay-norm-arr" count="1" stride="3">
            <param name="X" type="float"/><param name="Y" type="float"/><param name="Z" type="float"/>
          </accessor></technique_common>
        </source>
        <source id="Overlay-uv">
          <float_array id="Overlay-uv-arr" count="12">0 0 1 0 0 1 0.5 0.5 1 0.5 0.5 1</float_array>
          <technique_common><accessor source="#Overlay-uv-arr" count="6" stride="2">
            <param name="S" type="float"/><param name="T" type="float"/>
          </accessor></technique_common>
        </source>
        <vertices id="Overlay-verts"><input semantic="POSITION" source="#Overlay-pos"/></vertices>
        <triangles material="Mat0-material" count="3">
          <input semantic="VERTEX" source="#Overlay-verts" offset="0"/>
          <input semantic="NORMAL" source="#Overlay-norm" offset="1"/>
          <input semantic="TEXCOORD" source="#Overlay-uv" offset="2" set="0"/>
          <p>0 0 0 1 0 1 2 0 2  3 0 0 4 0 1 5 0 2  3 0 3 4 0 4 5 0 5</p>
        </triangles>
        <triangles material="Mat1-material" count="1">
          <input semantic="VERTEX" source="#Overlay-verts" offset="0"/>
          <input semantic="NORMAL" source="#Overlay-norm" offset="1"/>
          <input semantic="TEXCOORD" source="#Overlay-uv" offset="2" set="0"/>
          <p>0 0 0 1 0 1 2 0 2</p>
        </triangles>
      </mesh>
    </geometry>
  </library_geometries>
  <library_visual_scenes>
    <visual_scene id="Scene" name="Scene">
      <node id="OverlayObj" name="OverlayObj" type="NODE">
        <instance_geometry url="#Overlay-mesh">
          <bind_material><technique_common>
            <instance_material symbol="Mat0-material" target="#Mat0-material"/>
            <instance_material symbol="Mat1-material" target="#Mat1-material"/>
          </technique_common></bind_material>
        </instance_geometry>
      </node>
    </visual_scene>
  </library_visual_scenes>
  <scene><instance_visual_scene url="#Scene"/></scene>
</COLLADA>'''
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(dae)


def tri_signature_set(me) -> set:
    sigs = set()
    for poly in me.polygons:
        pts = tuple(
            tuple(round(c, 4) for c in me.vertices[me.loops[li].vertex_index].co)
            for li in poly.loop_indices
        )
        # Rotate to canonical start (preserve winding)
        min_i = min(range(len(pts)), key=lambda idx: pts[idx])
        rot = pts[min_i:] + pts[:min_i]
        nz = round(poly.normal.z, 2)
        sigs.add((rot, nz))
    return sigs


dae_path = os.path.join(tmpdir, "ds.dae")
write_test_dae(dae_path)

# 1. DAE import: KEEP -> 10 faces (5 front + 5 back), no double_sided attr
bpy.ops.wm.read_homefile(use_empty=True)
res = bpy.ops.wm.collada_import(filepath=dae_path, double_sided_faces="KEEP")
assert res == {"FINISHED"}
me_keep = bpy.data.objects["DSObj"].data
assert len(me_keep.polygons) == 10, f"KEEP expected 10 faces, got {len(me_keep.polygons)}"
assert me_keep.attributes.get("double_sided") is None, "KEEP should not set double_sided attr"
orig_sigs = tri_signature_set(me_keep)
assert len(orig_sigs) == 10, f"expected 10 distinct oriented tris, got {len(orig_sigs)}"

# Export to .cdae from KEEP so we have a .cdae with 5 double-sided pairs
cdae_orig = os.path.join(tmpdir, "ds_orig.cdae")
res = bpy.ops.export_scene.beamng_cdae(filepath=cdae_orig)
assert res == {"FINISHED"}

# 2. DAE import: REMOVE -> 5 faces, no double_sided attr
bpy.ops.wm.read_homefile(use_empty=True)
res = bpy.ops.wm.collada_import(filepath=dae_path, double_sided_faces="REMOVE")
assert res == {"FINISHED"}
me_rem = bpy.data.objects["DSObj"].data
assert len(me_rem.polygons) == 5, f"REMOVE expected 5 faces, got {len(me_rem.polygons)}"
assert me_rem.attributes.get("double_sided") is None, "REMOVE should not set double_sided attr"

# 3. DAE import: MERGE -> 5 faces with double_sided=True -> export DAE -> re-import KEEP -> 10 tris matching orig
bpy.ops.wm.read_homefile(use_empty=True)
res = bpy.ops.wm.collada_import(filepath=dae_path, double_sided_faces="MERGE")
assert res == {"FINISHED"}
me_merge = bpy.data.objects["DSObj"].data
assert len(me_merge.polygons) == 5, f"MERGE expected 5 faces, got {len(me_merge.polygons)}"
ds_attr = me_merge.attributes.get("double_sided")
assert ds_attr is not None and ds_attr.domain == "FACE" and ds_attr.data_type == "BOOLEAN"
ds_vals = [d.value for d in ds_attr.data]
assert all(ds_vals) and len(ds_vals) == 5, f"expected all 5 faces double_sided=True, got {ds_vals}"

# Re-export DAE (Keep Normals=True)
dae_rt = os.path.join(tmpdir, "ds_rt.dae")
res = bpy.ops.wm.collada_export(filepath=dae_rt, triangulate=True, keep_normals=True)
assert res == {"FINISHED"}
# Re-export CDAE from MERGE mesh
cdae_rt = os.path.join(tmpdir, "ds_rt.cdae")
res = bpy.ops.export_scene.beamng_cdae(filepath=cdae_rt)
assert res == {"FINISHED"}

# Re-import DAE with KEEP -> must match orig_sigs (10 oriented tris)
bpy.ops.wm.read_homefile(use_empty=True)
res = bpy.ops.wm.collada_import(filepath=dae_rt, double_sided_faces="KEEP")
assert res == {"FINISHED"}
me_dae_rt = bpy.data.objects["DSObj"].data
assert len(me_dae_rt.polygons) == 10, f"DAE round-trip expected 10 faces, got {len(me_dae_rt.polygons)}"
assert tri_signature_set(me_dae_rt) == orig_sigs, "DAE MERGE->export->KEEP triangle set mismatch"

# 4. CDAE import: KEEP, MERGE, REMOVE all match DAE behavior
bpy.ops.wm.read_homefile(use_empty=True)
res = bpy.ops.import_scene.beamng_cdae(filepath=cdae_rt, double_sided_faces="KEEP")
assert res == {"FINISHED"}
me_cdae_keep = bpy.data.objects["DSObj"].data
assert len(me_cdae_keep.polygons) == 10, f"CDAE KEEP expected 10 faces, got {len(me_cdae_keep.polygons)}"
assert tri_signature_set(me_cdae_keep) == orig_sigs, "CDAE MERGE->export->KEEP triangle set mismatch"

bpy.ops.wm.read_homefile(use_empty=True)
res = bpy.ops.import_scene.beamng_cdae(filepath=cdae_rt, double_sided_faces="MERGE")
assert res == {"FINISHED"}
me_cdae_merge = bpy.data.objects["DSObj"].data
assert len(me_cdae_merge.polygons) == 5, f"CDAE MERGE expected 5 faces, got {len(me_cdae_merge.polygons)}"
ds_c = me_cdae_merge.attributes.get("double_sided")
assert ds_c is not None and all(d.value for d in ds_c.data)

bpy.ops.wm.read_homefile(use_empty=True)
res = bpy.ops.import_scene.beamng_cdae(filepath=cdae_rt, double_sided_faces="REMOVE")
assert res == {"FINISHED"}
me_cdae_rem = bpy.data.objects["DSObj"].data
assert len(me_cdae_rem.polygons) == 5, f"CDAE REMOVE expected 5 faces, got {len(me_cdae_rem.polygons)}"
assert me_cdae_rem.attributes.get("double_sided") is None

# 5. Keep Quads + double_sided export on a quad mesh
bpy.ops.wm.read_homefile(use_empty=True)
bpy.ops.mesh.primitive_plane_add()
qob = bpy.context.object
qob.name = "DSQuad"
qds = qob.data.attributes.new(name="double_sided", type="BOOLEAN", domain="FACE")
qds.data[0].value = True
dae_quad = os.path.join(tmpdir, "ds_quad.dae")
res = bpy.ops.wm.collada_export(filepath=dae_quad, triangulate=True, keep_quads=True)
assert res == {"FINISHED"}
bpy.ops.wm.read_homefile(use_empty=True)
res = bpy.ops.wm.collada_import(filepath=dae_quad, double_sided_faces="KEEP")
assert res == {"FINISHED"}
me_q = bpy.data.objects["DSQuad"].data
assert len(me_q.polygons) == 2 and all(len(p.vertices) == 4 for p in me_q.polygons), (
    f"Keep Quads double_sided expected 2 quads, got {[len(p.vertices) for p in me_q.polygons]}"
)

# 6. Different-material pair and different-UV pair preserved with KEEP
diff_dae = os.path.join(tmpdir, "diff_attrs.dae")
write_diff_attr_dae(diff_dae)
bpy.ops.wm.read_homefile(use_empty=True)
res = bpy.ops.wm.collada_import(filepath=diff_dae, double_sided_faces="KEEP")
assert res == {"FINISHED"}
me_diff = bpy.data.objects["OverlayObj"].data
assert len(me_diff.polygons) == 4, f"diff material/UV expected 4 faces, got {len(me_diff.polygons)}"
mat_names = sorted(me_diff.materials[p.material_index].name for p in me_diff.polygons)
assert "Mat0" in mat_names and "Mat1" in mat_names, f"lost material: {mat_names}"

print("PASS test_double_sided_faces (#26: DAE/CDAE KEEP/MERGE/REMOVE, round-trip, Keep Quads, diff-mat/UV)")
"""


def main() -> int:
    blender = find_blender5()
    with tempfile.TemporaryDirectory(prefix="collada_test_ds_") as tmpdir:
        worker_py = os.path.join(tmpdir, "worker.py")
        with open(worker_py, "w", encoding="utf-8") as fh:
            fh.write(WORKER)
        proc = subprocess.run(
            [blender, "-b", "--python-exit-code", "1", "--python", worker_py, "--", tmpdir],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            sys.stdout.write(proc.stdout)
            sys.stderr.write(proc.stderr)
            return proc.returncode
        for line in proc.stdout.splitlines():
            if line.startswith("PASS "):
                print(line)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
