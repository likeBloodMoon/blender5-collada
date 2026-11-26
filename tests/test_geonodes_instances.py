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

tmpdir = sys.argv[-1]

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

print(f"PASS test_geonodes_instances (#45: DAE verts={n_verts_dae}, CDAE tris={n_polys_cdae})")
"""


def main() -> int:
    blender = find_blender5()
    with tempfile.TemporaryDirectory(prefix="collada_test_gn_") as tmpdir:
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
