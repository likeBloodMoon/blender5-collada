"""Seeded fuzzer for COLLADA (.dae) import crash & invalid-mesh detection (Part A8).

Uses a deterministic built-in seed corpus covering triangles/normals/UVs, polylists/
vertex colors/materials, skinned armatures (<vertex_weights>), morph targets, and
animations/cameras/lights, randomly corrupts numbers, indices, attributes, and
structure, and runs imports inside subprocesses. On every imported mesh it runs
bmesh.from_mesh() and asserts me.validate() == False.

Batches iterations per subprocess for speed; if a batch crashes, automatically
re-runs each file in that batch individually to pinpoint the exact failing seed.

Usage:
    python tests/fuzz_import.py
    FUZZ_ITERS=200 FUZZ_SEED=42 COLLADA_NATIVE=1 python tests/fuzz_import.py
"""

from __future__ import annotations

import os
import random
import re
import shutil
import subprocess
import sys
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _find_runner() -> list[str]:
    blender = os.environ.get("BLENDER_5X") or os.environ.get("BLENDER_EXE")
    if blender and os.path.isfile(blender):
        return [blender, "-b", "--factory-startup", "--python"]
    local_bl = os.path.join(REPO_ROOT, ".tools", "blender-5.3.0-windows-x64", "blender.exe")
    if os.path.isfile(local_bl):
        return [local_bl, "-b", "--factory-startup", "--python"]
    return [sys.executable]


BUILTIN_SEEDS: list[str] = [
    # Triangles with normals and UVs
    """<?xml version="1.0" encoding="utf-8"?>
<COLLADA xmlns="http://www.collada.org/2005/11/COLLADASchema" version="1.4.1">
  <asset><up_axis>Z_UP</up_axis></asset>
  <library_geometries>
    <geometry id="Cube-mesh" name="Cube">
      <mesh>
        <source id="Cube-pos">
          <float_array id="Cube-pos-arr" count="12">0 0 0 1 0 0 1 1 0 0 1 0</float_array>
          <technique_common>
            <accessor source="#Cube-pos-arr" count="4" stride="3">
              <param name="X" type="float"/><param name="Y" type="float"/><param name="Z" type="float"/>
            </accessor>
          </technique_common>
        </source>
        <source id="Cube-norm">
          <float_array id="Cube-norm-arr" count="6">0 0 1 0 0 -1</float_array>
          <technique_common>
            <accessor source="#Cube-norm-arr" count="2" stride="3">
              <param name="X" type="float"/><param name="Y" type="float"/><param name="Z" type="float"/>
            </accessor>
          </technique_common>
        </source>
        <source id="Cube-uv">
          <float_array id="Cube-uv-arr" count="8">0 0 1 0 1 1 0 1</float_array>
          <technique_common>
            <accessor source="#Cube-uv-arr" count="4" stride="2">
              <param name="S" type="float"/><param name="T" type="float"/>
            </accessor>
          </technique_common>
        </source>
        <vertices id="Cube-verts">
          <input semantic="POSITION" source="#Cube-pos"/>
        </vertices>
        <triangles count="2">
          <input semantic="VERTEX" source="#Cube-verts" offset="0"/>
          <input semantic="NORMAL" source="#Cube-norm" offset="1"/>
          <input semantic="TEXCOORD" source="#Cube-uv" offset="2" set="0"/>
          <p>0 0 0 1 0 1 2 0 2 0 1 0 2 1 2 3 1 3</p>
        </triangles>
      </mesh>
    </geometry>
  </library_geometries>
  <library_visual_scenes>
    <visual_scene id="Scene">
      <node id="Cube" name="Cube" type="NODE">
        <matrix sid="transform">1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1</matrix>
        <instance_geometry url="#Cube-mesh"/>
      </node>
    </visual_scene>
  </library_visual_scenes>
  <scene><instance_visual_scene url="#Scene"/></scene>
</COLLADA>
""",
    # Polylist with vertex colors and material
    """<?xml version="1.0" encoding="utf-8"?>
<COLLADA xmlns="http://www.collada.org/2005/11/COLLADASchema" version="1.4.1">
  <asset><up_axis>Z_UP</up_axis></asset>
  <library_effects>
    <effect id="Mat-fx"><profile_COMMON><technique sid="common"><lambert><diffuse><color>0.8 0.2 0.1 1</color></diffuse></lambert></technique></profile_COMMON></effect>
  </library_effects>
  <library_materials>
    <material id="Mat-mat" name="Mat"><instance_effect url="#Mat-fx"/></material>
  </library_materials>
  <library_geometries>
    <geometry id="Poly-mesh" name="Poly">
      <mesh>
        <source id="Poly-pos">
          <float_array id="Poly-pos-arr" count="15">0 0 0 1 0 0 2 1 0 1 2 0 0 1 0</float_array>
          <technique_common>
            <accessor source="#Poly-pos-arr" count="5" stride="3">
              <param name="X" type="float"/><param name="Y" type="float"/><param name="Z" type="float"/>
            </accessor>
          </technique_common>
        </source>
        <source id="Poly-col">
          <float_array id="Poly-col-arr" count="12">1 0 0 1 0 1 0 1 0 0 1 1</float_array>
          <technique_common>
            <accessor source="#Poly-col-arr" count="3" stride="4">
              <param name="R" type="float"/><param name="G" type="float"/><param name="B" type="float"/><param name="A" type="float"/>
            </accessor>
          </technique_common>
        </source>
        <vertices id="Poly-verts">
          <input semantic="POSITION" source="#Poly-pos"/>
        </vertices>
        <polylist material="Mat-mat" count="2">
          <input semantic="VERTEX" source="#Poly-verts" offset="0"/>
          <input semantic="COLOR" source="#Poly-col" offset="1" set="0"/>
          <vcount>4 3</vcount>
          <p>0 0 1 1 2 2 3 0 0 0 3 1 4 2</p>
        </polylist>
      </mesh>
    </geometry>
  </library_geometries>
  <library_visual_scenes>
    <visual_scene id="Scene">
      <node id="Poly" name="Poly" type="NODE">
        <instance_geometry url="#Poly-mesh">
          <bind_material><technique_common>
            <instance_material symbol="Mat-mat" target="#Mat-mat"/>
          </technique_common></bind_material>
        </instance_geometry>
      </node>
    </visual_scene>
  </library_visual_scenes>
  <scene><instance_visual_scene url="#Scene"/></scene>
</COLLADA>
""",
    # Skinned mesh with armature, inv_bind_matrix, and vertex_weights
    """<?xml version="1.0" encoding="utf-8"?>
<COLLADA xmlns="http://www.collada.org/2005/11/COLLADASchema" version="1.4.1">
  <asset><up_axis>Z_UP</up_axis></asset>
  <library_geometries>
    <geometry id="Skin-mesh" name="Skin">
      <mesh>
        <source id="Skin-pos">
          <float_array id="Skin-pos-arr" count="12">0 0 0 1 0 0 1 1 0 0 1 0</float_array>
          <technique_common>
            <accessor source="#Skin-pos-arr" count="4" stride="3">
              <param name="X" type="float"/><param name="Y" type="float"/><param name="Z" type="float"/>
            </accessor>
          </technique_common>
        </source>
        <source id="Skin-norm">
          <float_array id="Skin-norm-arr" count="6">0 0 1 0 0 1</float_array>
          <technique_common>
            <accessor source="#Skin-norm-arr" count="2" stride="3">
              <param name="X" type="float"/><param name="Y" type="float"/><param name="Z" type="float"/>
            </accessor>
          </technique_common>
        </source>
        <vertices id="Skin-verts"><input semantic="POSITION" source="#Skin-pos"/></vertices>
        <triangles count="2">
          <input semantic="VERTEX" source="#Skin-verts" offset="0"/>
          <input semantic="NORMAL" source="#Skin-norm" offset="1"/>
          <p>0 0 1 0 2 0 0 1 2 1 3 1</p>
        </triangles>
      </mesh>
    </geometry>
  </library_geometries>
  <library_controllers>
    <controller id="Arm-Skin" name="Arm">
      <skin source="#Skin-mesh">
        <bind_shape_matrix>1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1</bind_shape_matrix>
        <source id="Skin-joints">
          <Name_array id="Skin-joints-arr" count="2">BoneA BoneB</Name_array>
          <technique_common><accessor source="#Skin-joints-arr" count="2" stride="1"><param name="JOINT" type="name"/></accessor></technique_common>
        </source>
        <source id="Skin-ibm">
          <float_array id="Skin-ibm-arr" count="32">1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1 1 0 0 0 0 1 0 -1 0 0 1 0 0 0 0 1</float_array>
          <technique_common><accessor source="#Skin-ibm-arr" count="2" stride="16"><param name="TRANSFORM" type="float4x4"/></accessor></technique_common>
        </source>
        <source id="Skin-weights">
          <float_array id="Skin-weights-arr" count="3">1.0 0.5 0.5</float_array>
          <technique_common><accessor source="#Skin-weights-arr" count="3" stride="1"><param name="WEIGHT" type="float"/></accessor></technique_common>
        </source>
        <joints>
          <input semantic="JOINT" source="#Skin-joints"/>
          <input semantic="INV_BIND_MATRIX" source="#Skin-ibm"/>
        </joints>
        <vertex_weights count="4">
          <input semantic="JOINT" source="#Skin-joints" offset="0"/>
          <input semantic="WEIGHT" source="#Skin-weights" offset="1"/>
          <vcount>1 2 2 1</vcount>
          <v>0 0 0 1 1 2 0 1 1 2 1 0</v>
        </vertex_weights>
      </skin>
    </controller>
  </library_controllers>
  <library_visual_scenes>
    <visual_scene id="Scene">
      <node id="Armature" name="Armature" type="NODE">
        <node id="BoneA" name="BoneA" sid="BoneA" type="JOINT">
          <matrix sid="transform">1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1</matrix>
          <node id="BoneB" name="BoneB" sid="BoneB" type="JOINT">
            <matrix sid="transform">1 0 0 0 0 1 0 1 0 0 1 0 0 0 0 1</matrix>
          </node>
        </node>
        <node id="SkinObj" name="SkinObj" type="NODE">
          <instance_controller url="#Arm-Skin"><skeleton>#BoneA</skeleton></instance_controller>
        </node>
      </node>
    </visual_scene>
  </library_visual_scenes>
  <scene><instance_visual_scene url="#Scene"/></scene>
</COLLADA>
""",
    # Morph targets (shape keys) + decomposed transforms
    """<?xml version="1.0" encoding="utf-8"?>
<COLLADA xmlns="http://www.collada.org/2005/11/COLLADASchema" version="1.4.1">
  <asset><up_axis>Z_UP</up_axis></asset>
  <library_geometries>
    <geometry id="Base-mesh" name="Base">
      <mesh>
        <source id="Base-pos">
          <float_array id="Base-pos-arr" count="9">0 0 0 1 0 0 0 1 0</float_array>
          <technique_common><accessor source="#Base-pos-arr" count="3" stride="3"><param name="X" type="float"/><param name="Y" type="float"/><param name="Z" type="float"/></accessor></technique_common>
        </source>
        <vertices id="Base-verts"><input semantic="POSITION" source="#Base-pos"/></vertices>
        <triangles count="1"><input semantic="VERTEX" source="#Base-verts" offset="0"/><p>0 1 2</p></triangles>
      </mesh>
    </geometry>
    <geometry id="Key1-mesh" name="Key1">
      <mesh>
        <source id="Key1-pos">
          <float_array id="Key1-pos-arr" count="9">0 0 1 1 0 1 0 1 1</float_array>
          <technique_common><accessor source="#Key1-pos-arr" count="3" stride="3"><param name="X" type="float"/><param name="Y" type="float"/><param name="Z" type="float"/></accessor></technique_common>
        </source>
        <vertices id="Key1-verts"><input semantic="POSITION" source="#Key1-pos"/></vertices>
        <triangles count="1"><input semantic="VERTEX" source="#Key1-verts" offset="0"/><p>0 1 2</p></triangles>
      </mesh>
    </geometry>
  </library_geometries>
  <library_controllers>
    <controller id="Base-morph">
      <morph source="#Base-mesh" method="NORMALIZED">
        <source id="Morph-tgts"><IDREF_array id="Morph-tgts-arr" count="1">Key1-mesh</IDREF_array><technique_common><accessor source="#Morph-tgts-arr" count="1" stride="1"><param name="IDREF" type="IDREF"/></accessor></technique_common></source>
        <source id="Morph-wts"><float_array id="Morph-wts-arr" count="1">0.5</float_array><technique_common><accessor source="#Morph-wts-arr" count="1" stride="1"><param name="MORPH_WEIGHT" type="float"/></accessor></technique_common></source>
        <targets><input semantic="MORPH_TARGET" source="#Morph-tgts"/><input semantic="MORPH_WEIGHT" source="#Morph-wts"/></targets>
      </morph>
    </controller>
  </library_controllers>
  <library_visual_scenes>
    <visual_scene id="Scene">
      <node id="MorphObj" name="MorphObj" type="NODE">
        <translate sid="location">1 2 3</translate>
        <rotate sid="rotationZ">0 0 1 45</rotate>
        <scale sid="scale">1 1 1</scale>
        <instance_controller url="#Base-morph"/>
      </node>
    </visual_scene>
  </library_visual_scenes>
  <scene><instance_visual_scene url="#Scene"/></scene>
</COLLADA>
""",
    # Animations (matrix + decomposed) with camera and light
    """<?xml version="1.0" encoding="utf-8"?>
<COLLADA xmlns="http://www.collada.org/2005/11/COLLADASchema" version="1.4.1">
  <asset><up_axis>Z_UP</up_axis></asset>
  <library_cameras>
    <camera id="Cam-cam" name="Cam"><optics><technique_common><perspective><xfov>49.13</xfov><znear>0.1</znear><zfar>100</zfar></perspective></technique_common></optics></camera>
  </library_cameras>
  <library_lights>
    <light id="Sun-light" name="Sun"><technique_common><directional><color>1 0.9 0.8</color></directional></technique_common></light>
  </library_lights>
  <library_geometries>
    <geometry id="Tri-mesh" name="Tri">
      <mesh>
        <source id="Tri-pos">
          <float_array id="Tri-pos-arr" count="9">0 0 0 1 0 0 0 1 0</float_array>
          <technique_common><accessor source="#Tri-pos-arr" count="3" stride="3"><param name="X" type="float"/><param name="Y" type="float"/><param name="Z" type="float"/></accessor></technique_common>
        </source>
        <vertices id="Tri-verts"><input semantic="POSITION" source="#Tri-pos"/></vertices>
        <triangles count="1"><input semantic="VERTEX" source="#Tri-verts" offset="0"/><p>0 1 2</p></triangles>
      </mesh>
    </geometry>
  </library_geometries>
  <library_animations>
    <animation id="Tri-anim">
      <source id="Tri-in"><float_array id="Tri-in-arr" count="2">0.0 1.0</float_array><technique_common><accessor source="#Tri-in-arr" count="2" stride="1"><param name="TIME" type="float"/></accessor></technique_common></source>
      <source id="Tri-out"><float_array id="Tri-out-arr" count="32">1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1 1 0 0 2 0 1 0 0 0 0 1 0 0 0 0 1</float_array><technique_common><accessor source="#Tri-out-arr" count="2" stride="16"><param name="TRANSFORM" type="float4x4"/></accessor></technique_common></source>
      <source id="Tri-interp"><Name_array id="Tri-interp-arr" count="2">LINEAR LINEAR</Name_array><technique_common><accessor source="#Tri-interp-arr" count="2" stride="1"><param name="INTERPOLATION" type="name"/></accessor></technique_common></source>
      <sampler id="Tri-samp">
        <input semantic="INPUT" source="#Tri-in"/>
        <input semantic="OUTPUT" source="#Tri-out"/>
        <input semantic="INTERPOLATION" source="#Tri-interp"/>
      </sampler>
      <channel source="#Tri-samp" target="TriNode/transform"/>
    </animation>
  </library_animations>
  <library_visual_scenes>
    <visual_scene id="Scene">
      <node id="TriNode" name="TriNode" type="NODE">
        <matrix sid="transform">1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1</matrix>
        <instance_geometry url="#Tri-mesh"/>
      </node>
      <node id="CamNode" name="CamNode" type="NODE"><instance_camera url="#Cam-cam"/></node>
      <node id="SunNode" name="SunNode" type="NODE"><instance_light url="#Sun-light"/></node>
    </visual_scene>
  </library_visual_scenes>
  <scene><instance_visual_scene url="#Scene"/></scene>
</COLLADA>
""",
]


def _load_seed_corpus() -> list[str]:
    """Return the deterministic built-in fuzz seed corpus (never reads tests/_work)."""
    return list(BUILTIN_SEEDS)


_BAD_FLOAT_TOKENS = ["NaN", "inf", "-inf", "1.#QNAN", "-1.#IND", "x", "1e999", "+1.5", "", "999999999"]
_BAD_INT_TOKENS = ["-1", "99999", "0", "+0", "x", "2147483647", "-999", ""]
_TAG_RE = {
    "float_array": re.compile(r"(<float_array\b[^>]*>)(.*?)(</float_array>)", re.DOTALL),
    "p": re.compile(r"(<p\b[^>]*>)(.*?)(</p>)", re.DOTALL),
    "vcount": re.compile(r"(<vcount\b[^>]*>)(.*?)(</vcount>)", re.DOTALL),
    "attr": re.compile(r'\b(count|stride|offset)="([^"]*)"'),
}


def _mutate_dae(xml_text: str, rng: random.Random) -> str:
    text = xml_text
    n_mutations = rng.randint(1, 4)
    for _ in range(n_mutations):
        kind = rng.choice(["float_array", "p", "vcount", "attr", "truncate_p", "empty_tag"])
        if kind == "float_array":
            matches = list(_TAG_RE["float_array"].finditer(text))
            if matches:
                m = rng.choice(matches)
                toks = m.group(2).split()
                if toks:
                    idx = rng.randrange(len(toks))
                    toks[idx] = rng.choice(_BAD_FLOAT_TOKENS)
                    if rng.random() < 0.3:
                        toks = toks[: rng.randrange(len(toks))]
                repl = m.group(1) + " ".join(toks) + m.group(3)
                text = text[: m.start()] + repl + text[m.end() :]
        elif kind in ("p", "vcount"):
            matches = list(_TAG_RE[kind].finditer(text))
            if matches:
                m = rng.choice(matches)
                toks = m.group(2).split()
                if toks:
                    for _ in range(rng.randint(1, min(3, len(toks)))):
                        toks[rng.randrange(len(toks))] = rng.choice(_BAD_INT_TOKENS)
                    if rng.random() < 0.3:
                        toks = toks[: rng.randrange(len(toks))]
                repl = m.group(1) + " ".join(toks) + m.group(3)
                text = text[: m.start()] + repl + text[m.end() :]
        elif kind == "attr":
            matches = list(_TAG_RE["attr"].finditer(text))
            if matches:
                m = rng.choice(matches)
                val = rng.choice(["0", "-1", "1", "99999", "abc", ""])
                repl = f'{m.group(1)}="{val}"'
                text = text[: m.start()] + repl + text[m.end() :]
        elif kind == "truncate_p":
            matches = list(_TAG_RE["p"].finditer(text))
            if matches:
                m = rng.choice(matches)
                toks = m.group(2).split()
                cut = rng.randrange(max(1, len(toks)))
                repl = m.group(1) + " ".join(toks[:cut]) + m.group(3)
                text = text[: m.start()] + repl + text[m.end() :]
        elif kind == "empty_tag":
            tag = rng.choice(["float_array", "p", "vcount"])
            matches = list(_TAG_RE[tag].finditer(text))
            if matches:
                m = rng.choice(matches)
                repl = m.group(1) + m.group(3)
                text = text[: m.start()] + repl + text[m.end() :]
    return text


WORKER_SCRIPT = r'''
import os
import sys

argv = sys.argv
if "--" in argv:
    argv = argv[argv.index("--") + 1:]
else:
    argv = argv[1:]

repo_root = argv[0]
dae_paths = argv[1:]
sys.path.insert(0, repo_root)

import bpy
import bmesh
import io_scene_collada
from io_scene_collada.import_collada import DocumentImporter, ImportSettings

if not hasattr(bpy.ops.wm, "collada_import"):
    io_scene_collada.register()

for dae_path in dae_paths:
    bpy.ops.wm.read_factory_settings(use_empty=True)
    settings = ImportSettings(filepath=dae_path)
    importer = DocumentImporter(bpy.context, settings)
    importer.import_document()
    for me in bpy.data.meshes:
        bm = bmesh.new()
        bm.from_mesh(me)
        bm.free()
        if me.validate(verbose=False, clean_customdata=False):
            print(f"INVALID_MESH:{os.path.basename(dae_path)}:{me.name}", file=sys.stderr)
            sys.exit(2)
'''


def _run_batch(runner: list[str], worker_path: str, dae_paths: list[str]) -> subprocess.CompletedProcess:
    if runner[0] == sys.executable:
        cmd = [runner[0], worker_path, REPO_ROOT, *dae_paths]
    else:
        cmd = [*runner, worker_path, "--", REPO_ROOT, *dae_paths]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=120)


def main() -> int:
    n_iters = int(os.environ.get("FUZZ_ITERS", "200"))
    seed = int(os.environ.get("FUZZ_SEED", "1337"))
    batch_size = 25

    runner = _find_runner()
    corpus = _load_seed_corpus()
    rng = random.Random(seed)
    print(f"Fuzzing {n_iters} mutated .dae files (seed={seed}, corpus={len(corpus)})", flush=True)

    workdir = tempfile.mkdtemp(prefix="collada_fuzz_")
    try:
        worker_path = os.path.join(workdir, "_fuzz_worker.py")
        with open(worker_path, "w", encoding="utf-8") as f:
            f.write(WORKER_SCRIPT)

        dae_paths: list[str] = []
        for i in range(n_iters):
            base = rng.choice(corpus)
            mutated = _mutate_dae(base, rng)
            p = os.path.join(workdir, f"fuzz_{i:04d}.dae")
            with open(p, "w", encoding="utf-8") as f:
                f.write(mutated)
            dae_paths.append(p)

        failures: list[str] = []
        for start in range(0, len(dae_paths), batch_size):
            batch = dae_paths[start : start + batch_size]
            proc = _run_batch(runner, worker_path, batch)
            if proc.returncode != 0 or "Traceback (most recent call last)" in proc.stderr:
                # Pinpoint exact failing case(s) in this batch
                for p in batch:
                    single = _run_batch(runner, worker_path, [p])
                    if single.returncode != 0 or "Traceback (most recent call last)" in single.stderr:
                        failures.append(
                            f"FAIL {os.path.basename(p)} (rc={single.returncode}):\n"
                            f"{single.stderr[-1000:]}\n{single.stdout[-500:]}"
                        )
            else:
                print(f"  PASS batch {start + 1}..{start + len(batch)} / {n_iters}", flush=True)

        if failures:
            print("\n".join(failures))
            return 1
        print(f"\nAll {n_iters} fuzz iterations passed with zero crashes or invalid meshes!")
        return 0
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
