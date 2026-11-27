#!/usr/bin/env python3
"""Regression test for #45: Geometry Nodes instances export with apply_modifiers=True."""

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
import bpy

repo_root, tmpdir = sys.argv[-2], sys.argv[-1]
sys.path.insert(0, repo_root)
import io_scene_collada
io_scene_collada.register()

# Exact repro from #45: 3x3 Grid -> Instance on Points of Cube = 9 cubes = 72 vertices
bpy.ops.wm.read_homefile(use_empty=True)
bpy.ops.mesh.primitive_cube_add()
ob = bpy.context.object
ob.name = "GN"
ng = bpy.data.node_groups.new("Scatter", "GeometryNodeTree")
ng.interface.new_socket("Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
ng.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
gi = ng.nodes.new("NodeGroupInput")
go = ng.nodes.new("NodeGroupOutput")
grid = ng.nodes.new("GeometryNodeMeshGrid")
grid.inputs["Vertices X"].default_value = 3
grid.inputs["Vertices Y"].default_value = 3
iop = ng.nodes.new("GeometryNodeInstanceOnPoints")
ng.links.new(grid.outputs["Mesh"], iop.inputs["Points"])
ng.links.new(gi.outputs[0], iop.inputs["Instance"])
ng.links.new(iop.outputs["Instances"], go.inputs[0])
ob.modifiers.new("GN", "NODES").node_group = ng

dae_path = os.path.join(tmpdir, "gn.dae")
res = bpy.ops.wm.collada_export(filepath=dae_path, apply_modifiers=True)
assert res == {"FINISHED"}

cdae_path = os.path.join(tmpdir, "gn.cdae")
res = bpy.ops.export_scene.beamng_cdae(filepath=cdae_path, apply_modifiers=True)
assert res == {"FINISHED"}

# Verify .dae import has all 72 vertices (9 cubes)
bpy.ops.wm.read_homefile(use_empty=True)
res = bpy.ops.wm.collada_import(filepath=dae_path)
assert res == {"FINISHED"}
n_verts_dae = len(bpy.data.objects["GN"].data.vertices)
assert n_verts_dae == 72, f"#45 DAE failed: expected 72 vertices (9 cubes), got {n_verts_dae}"

# Verify .cdae import also has all 9 cubes (108 triangles)
bpy.ops.wm.read_homefile(use_empty=True)
res = bpy.ops.import_scene.beamng_cdae(filepath=cdae_path)
assert res == {"FINISHED"}
n_polys_cdae = len(bpy.data.objects["GN"].data.polygons)
assert n_polys_cdae == 108, f"#45 CDAE failed: expected 108 triangles (9 cubes), got {n_polys_cdae}"

# #64: Realized Geometry Nodes instances must preserve their own materials (.dae and .cdae)
bpy.ops.wm.read_homefile(use_empty=True)
host_mat = bpy.data.materials.new("HostMat")
proto_mat = bpy.data.materials.new("ProtoMat")

bpy.ops.mesh.primitive_cube_add(size=0.4)
proto = bpy.context.object
proto.name = "Proto"
proto.data.materials.append(proto_mat)

bpy.ops.mesh.primitive_grid_add(x_subdivisions=2, y_subdivisions=2, size=4.0)
host = bpy.context.object
host.name = "HostGN"
host.data.materials.append(host_mat)

ng2 = bpy.data.node_groups.new("ScatterMat", "GeometryNodeTree")
ng2.interface.new_socket("Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
ng2.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
gi2 = ng2.nodes.new("NodeGroupInput")
go2 = ng2.nodes.new("NodeGroupOutput")
obj_info = ng2.nodes.new("GeometryNodeObjectInfo")
obj_info.inputs["Object"].default_value = proto
iop2 = ng2.nodes.new("GeometryNodeInstanceOnPoints")
join2 = ng2.nodes.new("GeometryNodeJoinGeometry")
ng2.links.new(gi2.outputs[0], iop2.inputs["Points"])
ng2.links.new(obj_info.outputs["Geometry"], iop2.inputs["Instance"])
ng2.links.new(gi2.outputs[0], join2.inputs["Geometry"])
ng2.links.new(iop2.outputs["Instances"], join2.inputs["Geometry"])
ng2.links.new(join2.outputs["Geometry"], go2.inputs[0])
host.modifiers.new("GN", "NODES").node_group = ng2

# Export only HostGN (Proto is not selected, so ProtoMat is only on the realized instances)
bpy.ops.object.select_all(action="DESELECT")
host.select_set(True)
bpy.context.view_layer.objects.active = host

dae_mat = os.path.join(tmpdir, "gn_mat.dae")
res = bpy.ops.wm.collada_export(filepath=dae_mat, selected=True, apply_modifiers=True)
assert res == {"FINISHED"}

cdae_mat = os.path.join(tmpdir, "gn_mat.cdae")
res = bpy.ops.export_scene.beamng_cdae(filepath=cdae_mat, use_selection=True, apply_modifiers=True)
assert res == {"FINISHED"}


def _mat_face_counts(obj_name: str) -> dict[str, int]:
    me = bpy.data.objects[obj_name].data
    counts: dict[str, int] = {}
    for p in me.polygons:
        ma = me.materials[p.material_index] if p.material_index < len(me.materials) else None
        nm = ma.name if ma is not None else "<None>"
        counts[nm] = counts.get(nm, 0) + 1
    return counts


bpy.ops.wm.read_homefile(use_empty=True)
assert bpy.ops.wm.collada_import(filepath=dae_mat) == {"FINISHED"}
dae_counts = _mat_face_counts("HostGN")
assert dae_counts == {"HostMat": 8, "ProtoMat": 108}, f"#64 DAE material counts wrong: {dae_counts}"

bpy.ops.wm.read_homefile(use_empty=True)
assert bpy.ops.import_scene.beamng_cdae(filepath=cdae_mat) == {"FINISHED"}
cdae_counts = _mat_face_counts("HostGN")
assert cdae_counts == {"HostMat": 8, "ProtoMat": 108}, f"#64 CDAE material counts wrong: {cdae_counts}"

print(f"PASS test_geonodes_instances (#45 + #64: DAE={dae_counts}, CDAE={cdae_counts})")
"""


def main() -> int:
    blender = find_blender5()
    with tempfile.TemporaryDirectory(prefix="collada_test_gn_") as tmpdir:
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
