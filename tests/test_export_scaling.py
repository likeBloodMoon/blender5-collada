"""Regression tests for #37: export scales linearly with object count (.dae & .cdae).

Verifies that exporting 500, 2,000, and 4,000 small cube objects scales roughly
linearly (no O(N^2) blowup from per-object meshes.remove() or view-layer scans)
and completes well under the quadratic regression threshold.

Usage:
    python tests/test_export_scaling.py
    BLENDER_5X=... python tests/test_export_scaling.py
"""

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
import time

import bmesh
import bpy

argv = sys.argv[sys.argv.index("--") + 1:]
repo_root, workdir, out_json = argv[0], argv[1], argv[2]
sys.path.insert(0, repo_root)

import io_scene_collada
io_scene_collada.register()

timings = {}
for n in (500, 2000, 4000):
    bpy.ops.wm.read_homefile(use_empty=True)
    me = bpy.data.meshes.new("m")
    bm = bmesh.new()
    bmesh.ops.create_cube(bm, size=0.2)
    bm.to_mesh(me)
    bm.free()
    for i in range(n):
        ob = bpy.data.objects.new(f"O{i}", me.copy())
        bpy.context.scene.collection.objects.link(ob)
    bpy.data.meshes.remove(me)

    dae_p = os.path.join(workdir, f"many_{n}.dae")
    t0 = time.perf_counter()
    res = bpy.ops.wm.collada_export(filepath=dae_p)
    t_dae = time.perf_counter() - t0
    assert "FINISHED" in res, f"DAE export failed at N={n}: {res}"

    cdae_p = os.path.join(workdir, f"many_{n}.cdae")
    t0 = time.perf_counter()
    res = bpy.ops.export_scene.beamng_cdae(filepath=cdae_p)
    t_cdae = time.perf_counter() - t0
    assert "FINISHED" in res, f"CDAE export failed at N={n}: {res}"

    # Verify no leaked temporary meshes
    assert len(bpy.data.meshes) == n, f"Leaked meshes at N={n}: {len(bpy.data.meshes)}"

    timings[str(n)] = {"dae": round(t_dae, 4), "cdae": round(t_cdae, 4)}

with open(out_json, "w", encoding="utf-8") as f:
    json.dump(timings, f, indent=2)
'''


def main() -> None:
    blender = _find_blender()
    workdir = tempfile.mkdtemp(prefix="collada_scaling_")
    try:
        worker_py = os.path.join(workdir, "worker.py")
        out_json = os.path.join(workdir, "results.json")
        with open(worker_py, "w", encoding="utf-8") as f:
            f.write(WORKER_SCRIPT)
        proc = subprocess.run(
            [blender, "--background", "--factory-startup", "--python", worker_py, "--", REPO_ROOT, workdir, out_json],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            print(proc.stdout)
            print(proc.stderr)
            raise SystemExit(f"Blender worker failed (rc={proc.returncode})")

        with open(out_json, encoding="utf-8") as f:
            timings = json.load(f)

        print("Export scaling timings (#37):", json.dumps(timings, indent=2))
        for fmt in ("dae", "cdae"):
            t500 = timings["500"][fmt]
            t4000 = timings["4000"][fmt]
            ratio = t4000 / max(t500, 0.05)
            # Linear scaling for 8x objects is ~8x; quadratic was ~35-40x (>10-15s).
            assert ratio < 16.0, f"{fmt}: quadratic scaling detected (500={t500}s, 4000={t4000}s, ratio={ratio:.1f}x)"
            assert t4000 < 6.0, f"{fmt}: 4000-object export too slow ({t4000:.2f}s)"
        print("PASS: Export scaling (#37) is linear for .dae and .cdae")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    main()
