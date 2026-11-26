#!/usr/bin/env python3
"""Regression tests for #40 (shape keys + vertex-count-changing modifiers)
and #41 (chained relative_key shape keys)."""

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

# --- Case 1 (#40): Shape key with Subsurf modifier (changes vertex count) ---
bpy.ops.wm.read_homefile(use_empty=True)
bpy.ops.mesh.primitive_cube_add()
ob = bpy.context.object
ob.name = "SKM"
ob.shape_key_add(name="Basis")
k = ob.shape_key_add(name="Grow")
for d in k.data:
    d.co *= 2.0
ob.modifiers.new("Subdiv", "SUBSURF").levels = 1

dae_mod = os.path.join(tmpdir, "skmod.dae")
res = bpy.ops.wm.collada_export(
    filepath=dae_mod,
    include_shapekeys=True,
    apply_modifiers=True,
)
assert res == {"FINISHED"}, f"export skmod failed: {res}"

bpy.ops.wm.read_homefile(use_empty=True)
res = bpy.ops.wm.collada_import(filepath=dae_mod)
assert res == {"FINISHED"}, f"import skmod failed: {res}"

imp_ob = bpy.data.objects["SKM"]
kb = imp_ob.data.shape_keys.key_blocks
basis_max = max(v.co.length for v in kb["Basis"].data)
grow_max = max(v.co.length for v in kb["Grow"].data)
ratio = grow_max / basis_max
assert abs(ratio - 2.0) < 1e-4, f"#40 failed: Grow/Basis ratio={ratio:.6f}, expected 2.0"

# --- Case 2 (#41): Chained relative_key (B relative to A) ---
bpy.ops.wm.read_homefile(use_empty=True)
bpy.ops.mesh.primitive_cube_add()
ob = bpy.context.object
ob.name = "SKR"
ob.shape_key_add(name="Basis")
a = ob.shape_key_add(name="A", from_mix=False)
for d in a.data:
    d.co.y += 0.5
b = ob.shape_key_add(name="B", from_mix=False)
b.relative_key = a
for d in b.data:
    d.co.y += 1.0
a.value = 0.0
b.value = 1.0

dg = bpy.context.evaluated_depsgraph_get()
me_ref = ob.evaluated_get(dg).to_mesh()
expected_max_y = max(v.co.y for v in me_ref.vertices)
ob.evaluated_get(dg).to_mesh_clear()
assert abs(expected_max_y - 1.5) < 1e-5, f"unexpected Blender ref max_y={expected_max_y}"

dae_chain = os.path.join(tmpdir, "skchain.dae")
res = bpy.ops.wm.collada_export(filepath=dae_chain, include_shapekeys=True)
assert res == {"FINISHED"}, f"export skchain failed: {res}"

bpy.ops.wm.read_homefile(use_empty=True)
res = bpy.ops.wm.collada_import(filepath=dae_chain)
assert res == {"FINISHED"}, f"import skchain failed: {res}"

imp_ob2 = bpy.data.objects["SKR"]
kb2 = imp_ob2.data.shape_keys.key_blocks
kb2["A"].value = 0.0
kb2["B"].value = 1.0
dg2 = bpy.context.evaluated_depsgraph_get()
me_imp = imp_ob2.evaluated_get(dg2).to_mesh()
actual_max_y = max(v.co.y for v in me_imp.vertices)
imp_ob2.evaluated_get(dg2).to_mesh_clear()
assert abs(actual_max_y - expected_max_y) < 1e-4, (
    f"#41 failed: actual_max_y={actual_max_y:.6f}, expected {expected_max_y:.6f}"
)

# --- Case 3 (#40 + #41 combined): Chained relative_key + Subsurf modifier ---
bpy.ops.wm.read_homefile(use_empty=True)
bpy.ops.mesh.primitive_cube_add()
ob = bpy.context.object
ob.name = "SKCombo"
ob.shape_key_add(name="Basis")
a = ob.shape_key_add(name="A", from_mix=False)
for d in a.data:
    d.co.y += 0.5
b = ob.shape_key_add(name="B", from_mix=False)
b.relative_key = a
for d in b.data:
    d.co.y += 1.0
ob.modifiers.new("Subdiv", "SUBSURF").levels = 1
a.value = 0.0
b.value = 1.0

dg = bpy.context.evaluated_depsgraph_get()
me_ref = ob.evaluated_get(dg).to_mesh()
expected_combo_y = max(v.co.y for v in me_ref.vertices)
ob.evaluated_get(dg).to_mesh_clear()

dae_combo = os.path.join(tmpdir, "skcombo.dae")
res = bpy.ops.wm.collada_export(
    filepath=dae_combo,
    include_shapekeys=True,
    apply_modifiers=True,
)
assert res == {"FINISHED"}, f"export skcombo failed: {res}"
# Verify exporter restored original key values
assert abs(a.value - 0.0) < 1e-6 and abs(b.value - 1.0) < 1e-6, (
    f"key values not restored: A={a.value}, B={b.value}"
)

bpy.ops.wm.read_homefile(use_empty=True)
res = bpy.ops.wm.collada_import(filepath=dae_combo)
assert res == {"FINISHED"}, f"import skcombo failed: {res}"

imp_ob3 = bpy.data.objects["SKCombo"]
kb3 = imp_ob3.data.shape_keys.key_blocks
kb3["A"].value = 0.0
kb3["B"].value = 1.0
dg3 = bpy.context.evaluated_depsgraph_get()
me_imp3 = imp_ob3.evaluated_get(dg3).to_mesh()
actual_combo_y = max(v.co.y for v in me_imp3.vertices)
imp_ob3.evaluated_get(dg3).to_mesh_clear()
assert abs(actual_combo_y - expected_combo_y) < 1e-4, (
    f"#40+#41 combo failed: actual={actual_combo_y:.6f}, expected={expected_combo_y:.6f}"
)

print(
    f"PASS test_shapekeys_export (#40 ratio={ratio:.4f}, #41 y={actual_max_y:.4f}, "
    f"combo y={actual_combo_y:.4f})"
)
"""


def main() -> int:
    blender = find_blender5()
    with tempfile.TemporaryDirectory(prefix="collada_test_sk_") as tmpdir:
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
