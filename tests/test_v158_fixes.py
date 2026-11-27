#!/usr/bin/env python3
"""Regression tests for v1.5.8 fixes:
- #54: Importing a .dae mesh with >8 UV maps (falls back to FLOAT2 CORNER attributes)
- #59: Importing with a folder path reports "No file selected" instead of XML parse error
- #63: .cdae export picks render/active color attribute and warns on dropped UVs/colors
- #66: Selection Only on a child without its parent exports world-space transform
"""

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
import math
import os
import sys
import bpy
from mathutils import Vector

repo_root, tmpdir = sys.argv[-2], sys.argv[-1]
sys.path.insert(0, repo_root)
import io_scene_collada
io_scene_collada.register()
from io_scene_collada.cdae.exporter import CdaeExporter, CdaeExportSettings
from io_scene_collada.import_collada import DocumentImporter, ImportSettings

# --- 1. #54: .dae with 12 UV maps + second mesh imports cleanly ---
uv_sources = []
uv_inputs = []
for i in range(12):
    u0 = f"{0.05 * i:.3f}"
    uv_sources.append(
        f'<source id="M1-uv-{i}">'
        f'<float_array id="M1-uv-{i}-arr" count="6">{u0} 0 1 0 0 1</float_array>'
        f'<technique_common><accessor source="#M1-uv-{i}-arr" count="3" stride="2">'
        f'<param name="S" type="float"/><param name="T" type="float"/>'
        f'</accessor></technique_common></source>'
    )
    uv_inputs.append(f'<input semantic="TEXCOORD" source="#M1-uv-{i}" offset="0" set="{i}"/>')

dae_12uv = os.path.join(tmpdir, "many_uvs.dae")
with open(dae_12uv, "w", encoding="utf-8") as fh:
    fh.write(
        '<?xml version="1.0" encoding="utf-8"?>'
        '<COLLADA xmlns="http://www.collada.org/2005/11/COLLADASchema" version="1.4.1">'
        '<asset><up_axis>Z_UP</up_axis></asset>'
        '<library_geometries>'
        '<geometry id="M1-mesh" name="many_uvs"><mesh>'
        '<source id="M1-pos"><float_array id="M1-pos-arr" count="9">0 0 0 1 0 0 0 1 0</float_array>'
        '<technique_common><accessor source="#M1-pos-arr" count="3" stride="3">'
        '<param name="X" type="float"/><param name="Y" type="float"/><param name="Z" type="float"/>'
        '</accessor></technique_common></source>'
        + "".join(uv_sources)
        + '<vertices id="M1-verts"><input semantic="POSITION" source="#M1-pos"/></vertices>'
        '<triangles count="1"><input semantic="VERTEX" source="#M1-verts" offset="0"/>'
        + "".join(uv_inputs)
        + '<p>0 1 2</p></triangles></mesh></geometry>'
        '<geometry id="M2-mesh" name="sibling"><mesh>'
        '<source id="M2-pos"><float_array id="M2-pos-arr" count="9">2 0 0 3 0 0 2 1 0</float_array>'
        '<technique_common><accessor source="#M2-pos-arr" count="3" stride="3">'
        '<param name="X" type="float"/><param name="Y" type="float"/><param name="Z" type="float"/>'
        '</accessor></technique_common></source>'
        '<vertices id="M2-verts"><input semantic="POSITION" source="#M2-pos"/></vertices>'
        '<triangles count="1"><input semantic="VERTEX" source="#M2-verts" offset="0"/><p>0 1 2</p></triangles>'
        '</mesh></geometry>'
        '</library_geometries>'
        '<library_visual_scenes><visual_scene id="S">'
        '<node id="ManyUVs" name="many_uvs"><instance_geometry url="#M1-mesh"/></node>'
        '<node id="Sibling" name="sibling"><instance_geometry url="#M2-mesh"/></node>'
        '</visual_scene></library_visual_scenes>'
        '<scene><instance_visual_scene url="#S"/></scene></COLLADA>'
    )

bpy.ops.wm.read_homefile(use_empty=True)
res = bpy.ops.wm.collada_import(filepath=dae_12uv)
assert res == {"FINISHED"}, f"#54 failed: import returned {res}"
assert "many_uvs" in bpy.data.objects and "sibling" in bpy.data.objects, (
    f"#54 failed: objects={list(bpy.data.objects.keys())}"
)
me_uv = bpy.data.objects["many_uvs"].data
assert len(me_uv.uv_layers) == 12, f"#54 failed: expected 12 UV maps, got {len(me_uv.uv_layers)}"
# Verify 12th UV map actually got its coordinate data (u0 = 0.55)
u11 = me_uv.uv_layers[11].data[0].uv[0]
assert abs(u11 - 0.55) < 1e-4, f"#54 failed: 12th UV map u0={u11}, expected 0.55"

# --- 2. #59: Importing a folder path reports "No file selected" ---
bpy.ops.wm.read_homefile(use_empty=True)
imp_dir = DocumentImporter(bpy.context, ImportSettings(filepath=tmpdir))
assert imp_dir.import_document() is False
assert any("No file selected" in e for e in imp_dir.errors), f"#59 DocumentImporter errors={imp_dir.errors}"

try:
    res = bpy.ops.wm.collada_import(filepath=tmpdir)
    raise AssertionError(f"#59 failed: expected RuntimeError/CANCELLED for folder, got {res}")
except RuntimeError as exc:
    assert "no file selected" in str(exc).lower(), f"#59 unexpected .dae error: {exc}"

try:
    res = bpy.ops.import_scene.beamng_cdae(filepath=tmpdir)
    raise AssertionError(f"#59 failed: expected RuntimeError/CANCELLED for .cdae folder, got {res}")
except RuntimeError as exc:
    assert "no file selected" in str(exc).lower(), f"#59 unexpected .cdae error: {exc}"

# --- 3. #63: .cdae picks render/active color attribute & warns on skipped UVs/colors ---
bpy.ops.wm.read_homefile(use_empty=True)
bpy.ops.mesh.primitive_cube_add()
cob = bpy.context.object
cob.name = "MultiAttr"
cme = cob.data
for i in range(4):
    cme.uv_layers.new(name=f"UVExtra{i}")
assert len(cme.uv_layers) == 5

ca_red = cme.color_attributes.new(name="first_red", type="BYTE_COLOR", domain="CORNER")
for item in ca_red.data:
    item.color_srgb = (1.0, 0.0, 0.0, 1.0)
ca_blue = cme.color_attributes.new(name="paint", type="BYTE_COLOR", domain="CORNER")
for item in ca_blue.data:
    item.color_srgb = (0.0, 0.0, 1.0, 1.0)
cme.color_attributes.render_color_index = 1
cme.color_attributes.active_color_index = 1

cdae_multi = os.path.join(tmpdir, "multi_attr.cdae")
cexp = CdaeExporter(bpy.context, CdaeExportSettings(filepath=cdae_multi))
assert cexp.export_document(), f"#63 cdae export failed: {cexp.errors}"
assert any("3 extra UV" in w for w in cexp.warnings), f"#63 missing UV warning: {cexp.warnings}"
assert any("paint" in w and "1 other color" in w for w in cexp.warnings), (
    f"#63 missing color warning: {cexp.warnings}"
)

bpy.ops.wm.read_homefile(use_empty=True)
assert bpy.ops.import_scene.beamng_cdae(filepath=cdae_multi) == {"FINISHED"}
imp_cme = bpy.data.objects["MultiAttr"].data
assert len(imp_cme.color_attributes) == 1
c0 = imp_cme.color_attributes[0].data[0].color_srgb
assert c0[0] < 0.05 and c0[2] > 0.95, f"#63 failed: expected blue 'paint' layer, got {tuple(c0)}"

# --- 4. #66: Exporting selected child without parent preserves world transform ---
bpy.ops.wm.read_homefile(use_empty=True)
bpy.ops.object.empty_add(location=(1.0, 2.0, 3.0))
par = bpy.context.object
par.name = "Parent"
par.rotation_euler = (0.0, math.radians(30.0), math.radians(45.0))

bpy.ops.mesh.primitive_cube_add(location=(0.0, 0.0, 0.0))
ch = bpy.context.object
ch.name = "Child"
ch.parent = par
ch.location = (-3.0, 0.0, 0.0)
bpy.context.view_layer.update()
expected_world_pos = ch.matrix_world.translation.copy()
expected_world_verts = sorted(
    (ch.matrix_world @ vert.co for vert in ch.data.vertices),
    key=lambda v: (round(v.x, 3), round(v.y, 3), round(v.z, 3)),
)

bpy.ops.object.select_all(action="DESELECT")
ch.select_set(True)
bpy.context.view_layer.objects.active = ch

dae_child = os.path.join(tmpdir, "child_only.dae")
assert bpy.ops.wm.collada_export(filepath=dae_child, selected=True) == {"FINISHED"}

bpy.ops.wm.read_homefile(use_empty=True)
assert bpy.ops.wm.collada_import(filepath=dae_child) == {"FINISHED"}
imp_ch = bpy.data.objects["Child"]
actual_world_pos = imp_ch.matrix_world.translation.copy()
actual_world_verts = sorted(
    (imp_ch.matrix_world @ vert.co for vert in imp_ch.data.vertices),
    key=lambda v: (round(v.x, 3), round(v.y, 3), round(v.z, 3)),
)
pos_err = (actual_world_pos - expected_world_pos).length
assert pos_err < 1e-4, (
    f"#66 failed: expected world pos {tuple(expected_world_pos)}, got {tuple(actual_world_pos)} (err={pos_err:.5f})"
)
max_vert_err = max((a - b).length for a, b in zip(actual_world_verts, expected_world_verts))
assert max_vert_err < 1e-4, f"#66 failed: max world-space vertex error={max_vert_err:.6f}"

# Also check skipped intermediate parent (GrandParent selected, Mid unselected, Child selected)
bpy.ops.wm.read_homefile(use_empty=True)
bpy.ops.object.empty_add(location=(5.0, 0.0, 0.0))
gp = bpy.context.object
gp.name = "GrandParent"
gp.rotation_euler = (0.0, 0.0, math.radians(90.0))

bpy.ops.object.empty_add(location=(0.0, 0.0, 0.0))
mid = bpy.context.object
mid.name = "Mid"
mid.parent = gp
mid.location = (2.0, 0.0, 0.0)

bpy.ops.mesh.primitive_cube_add(location=(0.0, 0.0, 0.0))
leaf = bpy.context.object
leaf.name = "Leaf"
leaf.parent = mid
leaf.location = (0.0, 3.0, 0.0)
bpy.context.view_layer.update()
exp_leaf_pos = leaf.matrix_world.translation.copy()

bpy.ops.object.select_all(action="DESELECT")
gp.select_set(True)
leaf.select_set(True)
dae_skip = os.path.join(tmpdir, "skip_mid.dae")
assert bpy.ops.wm.collada_export(filepath=dae_skip, selected=True) == {"FINISHED"}

bpy.ops.wm.read_homefile(use_empty=True)
assert bpy.ops.wm.collada_import(filepath=dae_skip) == {"FINISHED"}
imp_leaf = bpy.data.objects["Leaf"]
leaf_err = (imp_leaf.matrix_world.translation - exp_leaf_pos).length
assert leaf_err < 1e-4, (
    f"#66 skip-mid failed: expected {tuple(exp_leaf_pos)}, got {tuple(imp_leaf.matrix_world.translation)}"
)

print("PASS test_v158_fixes (#54 12-UV import, #59 folder check, #63 cdae render color + warnings, #66 child world transform)")
"""


def main() -> int:
    blender = find_blender5()
    with tempfile.TemporaryDirectory(prefix="collada_test_v158_") as tmpdir:
        worker_py = os.path.join(tmpdir, "worker.py")
        with open(worker_py, "w", encoding="utf-8") as fh:
            fh.write(WORKER)
        proc = subprocess.run(
            [blender, "-b", "--factory-startup", "--python-exit-code", "1", "--python", worker_py, "--", REPO_ROOT, tmpdir],
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
