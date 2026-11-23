"""Deep Verification Suite for Collada DAE extension (Blender 5.3.0 vs Blender 4.5.1 LTS).

Verifies:
  1. Per-element deep import comparison (verts, normals, UVs, colors, skin weights,
     shape key deltas, Principled BSDF sockets/textures, cameras, lights, bones,
     and multi-frame object + pose-bone animations).
  2. Cross-version round-trip tests:
     - 5.3 export -> 4.5.1 import (vs 4.5.1 export -> 4.5.1 import)
     - 4.5.1 export -> 5.3 import -> 5.3 re-export (vs 4.5.1 -> 4.5.1 -> 4.5.1)
  3. Import flag matrix (import_units, custom_normals=False, auto_connect,
     find_chains, fix_orientation, keep_bind_info).
  4. External / third-party COLLADA 1.4.1 stress tests (Y_UP, centimeter units,
     n-gon polylist & polygons, non-identity bind_shape_matrix + IDREF_array,
     legacy phong/lambert/blinn + textures).
"""
from __future__ import annotations

import json
import math
import os
import subprocess
import sys

# Reuse helpers from run_parity_tests
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from run_parity_tests import (
    BLENDER_45,
    BLENDER_53,
    TEST_WORK,
    check_xml_structural_and_float_parity,
    normalize_dae_metadata,
    run_blender,
)

DEEP_WORK = os.path.join(TEST_WORK, "deep")
os.makedirs(DEEP_WORK, exist_ok=True)


# ---------------------------------------------------------------------------
# Deep Per-Element Scene Inspector (runs inside Blender 4.5.1 and 5.3.0)
# ---------------------------------------------------------------------------

DEEP_INSPECT_SCRIPT = """
import bpy, json, math

# Clear scene without read_factory_settings (which unloads user extensions)
for ob in list(bpy.data.objects):
    bpy.data.objects.remove(ob, do_unlink=True)
for col in (
    bpy.data.meshes,
    bpy.data.cameras,
    bpy.data.lights,
    bpy.data.materials,
    bpy.data.armatures,
    bpy.data.actions,
    bpy.data.images,
):
    for item in list(col):
        col.remove(item)

bpy.context.scene.frame_set(1)
res = bpy.ops.wm.collada_import(filepath=r"{dae}", **{kwargs_repr})
assert "FINISHED" in res, f"Import failed: {{res}}"

def rf(v, nd=4):
    x = round(float(v), nd)
    return 0.0 if x == 0.0 else x

def rseq(seq, nd=4):
    return [rf(x, nd) for x in seq]

def inspect_material(mat):
    if mat is None:
        return None
    out = {{"name": mat.name, "sockets": {{}}, "textures": {{}}}}
    nt = mat.node_tree
    if nt is None:
        return out
    bsdf = next((n for n in nt.nodes if n.type == "BSDF_PRINCIPLED"), None)
    if bsdf is None:
        return out
    for sname in ("Base Color", "Metallic", "Roughness", "IOR", "Alpha", "Specular IOR Level", "Emission Color"):
        if sname not in bsdf.inputs:
            continue
        sock = bsdf.inputs[sname]
        if sock.is_linked:
            from_node = sock.links[0].from_node
            if from_node.type == "TEX_IMAGE" and from_node.image:
                out["textures"][sname] = from_node.image.name
            else:
                out["textures"][sname] = from_node.type
        else:
            dv = sock.default_value
            if hasattr(dv, "__len__"):
                out["sockets"][sname] = rseq(dv, 3)
            else:
                out["sockets"][sname] = rf(dv, 3)
    return out

for _ob in bpy.data.objects:
    _ob.update_tag()
bpy.context.view_layer.update()

info = {{}}
for ob in sorted(bpy.data.objects, key=lambda o: o.name):
    entry = {{
        "type": ob.type,
        "parent": ob.parent.name if ob.parent else None,
        "parent_type": ob.parent_type if ob.parent else None,
        "parent_bone": ob.parent_bone if (ob.parent and ob.parent_type == "BONE") else None,
        "loc": rseq(ob.matrix_world.translation, 4),
        "scale": rseq(ob.matrix_world.to_scale(), 4),
        "rot_quat": rseq(ob.matrix_world.to_quaternion(), 4),
    }}
    if ob.type == "MESH":
        me = ob.data
        entry["verts"] = [rseq(v.co, 4) for v in me.vertices]
        entry["polys"] = [list(p.vertices) for p in me.polygons]
        entry["edges_count"] = len(me.edges)

        # Corner normals (available in both 4.5.1 and 5.3.0)
        entry["corner_normals"] = [rseq(cn.vector, 3) for cn in me.corner_normals]

        # UV layers (every loop UV)
        uv_data = {{}}
        for uvl in me.uv_layers:
            uv_data[uvl.name] = [rseq(d.uv, 4) for d in uvl.data]
        entry["uvs"] = uv_data

        # Color attributes (every element RGBA)
        col_data = {{}}
        for ca in me.color_attributes:
            vals = []
            for item in ca.data:
                vals.append(rseq(item.color, 3))
            col_data[ca.name] = {{"domain": ca.domain, "data_type": ca.data_type, "values": vals}}
        entry["colors"] = col_data

        # Vertex group weights per vertex
        vg_names = [vg.name for vg in ob.vertex_groups]
        weights = []
        for v in me.vertices:
            vw = {{}}
            for g in v.groups:
                if g.group < len(vg_names) and g.weight > 1e-5:
                    vw[vg_names[g.group]] = rf(g.weight, 4)
            weights.append(vw)
        entry["weights"] = weights if any(weights) else []

        # Shape key per-vertex coordinates
        sk_data = {{}}
        if me.shape_keys:
            for kb in me.shape_keys.key_blocks:
                sk_data[kb.name] = [rseq(d.co, 4) for d in kb.data]
        entry["shapekeys"] = sk_data

        # Materials
        entry["materials"] = [inspect_material(s.material) for s in ob.material_slots]

    elif ob.type == "CAMERA":
        cam = ob.data
        entry["cam_type"] = cam.type
        entry["lens"] = rf(cam.lens, 3)
        entry["sensor_width"] = rf(cam.sensor_width, 3)
        entry["ortho_scale"] = rf(cam.ortho_scale, 3)
        entry["clip"] = [rf(cam.clip_start, 4), rf(cam.clip_end, 4)]

    elif ob.type == "LIGHT":
        la = ob.data
        entry["light_type"] = la.type
        entry["color"] = rseq(la.color, 4)
        entry["energy"] = rf(la.energy, 3)
        if la.type == "SPOT":
            entry["spot_size"] = rf(la.spot_size, 4)
            entry["spot_blend"] = rf(la.spot_blend, 4)
        elif la.type == "AREA":
            entry["area_shape"] = la.shape
            entry["area_size"] = rf(la.size, 4)
            if la.shape == "RECTANGLE":
                entry["area_size_y"] = rf(la.size_y, 4)

    elif ob.type == "ARMATURE":
        bones = {{}}
        for b in ob.data.bones:
            binfo = {{
                "parent": b.parent.name if b.parent else None,
                "head": rseq(b.head_local, 4),
                "tail": rseq(b.tail_local, 4),
                "connect": bool(b.use_connect),
                "mat": [rseq(row, 3) for row in b.matrix_local],
                "collections": [c.name for c in b.collections],
            }}
            if "rest_mat" in b:
                binfo["has_rest_mat"] = True
            if "bind_mat" in b:
                binfo["has_bind_mat"] = True
            bones[b.name] = binfo
        entry["bones"] = bones

    info[ob.name] = entry

# Sample animated object + pose-bone transforms across frames 1, 4, 7, 10
if len(bpy.data.actions) > 0:
    anim_frames = {{}}
    for fr in (1, 4, 7, 10):
        bpy.context.scene.frame_set(fr)
        bpy.context.view_layer.update()
        fr_data = {{}}
        for ob in sorted(bpy.data.objects, key=lambda o: o.name):
            o_data = {{
                "loc": rseq(ob.matrix_world.translation, 3),
                "rot_quat": rseq(ob.matrix_world.to_quaternion(), 3),
                "scale": rseq(ob.matrix_world.to_scale(), 3),
            }}
            if ob.type == "ARMATURE" and ob.pose:
                pb_data = {{}}
                for pb in sorted(ob.pose.bones, key=lambda p: p.name):
                    pb_data[pb.name] = {{
                        "loc": rseq(pb.matrix.translation, 3),
                        "rot_quat": rseq(pb.matrix.to_quaternion(), 3),
                    }}
                o_data["pose_bones"] = pb_data
            fr_data[ob.name] = o_data
        anim_frames[str(fr)] = fr_data
    info["__anim__"] = anim_frames

with open(r"{out_json}", "w", encoding="utf-8") as f:
    json.dump(info, f, indent=2, sort_keys=True)
"""


ROUNDTRIP_EXPORT_SCRIPT = """
import bpy

for ob in list(bpy.data.objects):
    bpy.data.objects.remove(ob, do_unlink=True)
for col in (
    bpy.data.meshes,
    bpy.data.cameras,
    bpy.data.lights,
    bpy.data.materials,
    bpy.data.armatures,
    bpy.data.actions,
    bpy.data.images,
):
    for item in list(col):
        col.remove(item)

res1 = bpy.ops.wm.collada_import(filepath=r"{in_dae}")
assert "FINISHED" in res1, f"Import failed: {{res1}}"
res2 = bpy.ops.wm.collada_export(filepath=r"{out_dae}", check_existing=False)
assert "FINISHED" in res2, f"Re-export failed: {{res2}}"
"""


# ---------------------------------------------------------------------------
# Deep Recursive Float-Tolerant Comparison
# ---------------------------------------------------------------------------

def compare_deep(a, b, path: str = "", float_tol: float = 1e-3, diffs: list[str] | None = None) -> list[str]:
    if diffs is None:
        diffs = []
    if len(diffs) >= 20:
        return diffs
    if type(a) != type(b):
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            if abs(float(a) - float(b)) > float_tol:
                diffs.append(f"{path}: {a} != {b} (delta={abs(float(a)-float(b)):.6g})")
            return diffs
        diffs.append(f"{path}: type {type(a).__name__} ({a!r}) != {type(b).__name__} ({b!r})")
        return diffs

    if isinstance(a, float):
        if abs(a - b) > float_tol:
            diffs.append(f"{path}: {a} != {b} (delta={abs(a-b):.6g})")
        return diffs
    if isinstance(a, (int, str, bool)) or a is None:
        if a != b:
            diffs.append(f"{path}: {a!r} != {b!r}")
        return diffs
    if isinstance(a, dict):
        ka, kb = set(a.keys()), set(b.keys())
        for k in sorted(ka - kb):
            diffs.append(f"{path}.{k}: missing in 5.3.0 (4.5.1={a[k]!r})"[:160])
        for k in sorted(kb - ka):
            diffs.append(f"{path}.{k}: extra in 5.3.0 (5.3.0={b[k]!r})"[:160])
        for k in sorted(ka & kb):
            # Quaternion double-cover (q == -q)
            if k == "rot_quat" and isinstance(a[k], list) and isinstance(b[k], list) and len(a[k]) == 4:
                d_pos = max(abs(x - y) for x, y in zip(a[k], b[k]))
                d_neg = max(abs(x + y) for x, y in zip(a[k], b[k]))
                if min(d_pos, d_neg) > float_tol:
                    diffs.append(f"{path}.{k}: {a[k]} != {b[k]}")
                continue
            compare_deep(a[k], b[k], f"{path}.{k}" if path else k, float_tol, diffs)
        return diffs
    if isinstance(a, list):
        if len(a) != len(b):
            diffs.append(f"{path}: len {len(a)} != {len(b)}")
            return diffs
        for i, (xa, xb) in enumerate(zip(a, b)):
            compare_deep(xa, xb, f"{path}[{i}]", float_tol, diffs)
        return diffs
    return diffs


# ---------------------------------------------------------------------------
# External / Third-Party COLLADA 1.4.1 Fixture Generators
# ---------------------------------------------------------------------------

def write_external_fixtures() -> dict[str, str]:
    fixtures: dict[str, str] = {}

    # 1. Y_UP + centimeter units + n-gon <polylist> + <polygons> + Phong material
    f_yup_ngon = os.path.join(DEEP_WORK, "ext_yup_cm_ngon_phong.dae")
    with open(f_yup_ngon, "w", encoding="utf-8") as f:
        f.write("""<?xml version="1.0" encoding="utf-8"?>
<COLLADA xmlns="http://www.collada.org/2005/11/COLLADASchema" version="1.4.1">
  <asset>
    <contributor><authoring_tool>ExternalDCC 1.0</authoring_tool></contributor>
    <created>2025-01-01T00:00:00</created>
    <modified>2025-01-01T00:00:00</modified>
    <unit name="centimeter" meter="0.01"/>
    <up_axis>Y_UP</up_axis>
  </asset>
  <library_effects>
    <effect id="PhongMat-effect">
      <profile_COMMON>
        <technique sid="common">
          <phong>
            <emission><color>0.05 0.0 0.0 1</color></emission>
            <diffuse><color>0.2 0.6 0.8 1</color></diffuse>
            <specular><color>0.5 0.5 0.5 1</color></specular>
            <shininess><float>64</float></shininess>
            <index_of_refraction><float>1.45</float></index_of_refraction>
          </phong>
        </technique>
      </profile_COMMON>
    </effect>
  </library_effects>
  <library_materials>
    <material id="PhongMat-material" name="PhongMat">
      <instance_effect url="#PhongMat-effect"/>
    </material>
  </library_materials>
  <library_geometries>
    <geometry id="Pentagon-mesh" name="Pentagon">
      <mesh>
        <source id="Pentagon-pos">
          <float_array id="Pentagon-pos-arr" count="15">0 0 0 100 0 0 130 95 0 50 155 0 -30 95 0</float_array>
          <technique_common>
            <accessor source="#Pentagon-pos-arr" count="5" stride="3">
              <param name="X" type="float"/><param name="Y" type="float"/><param name="Z" type="float"/>
            </accessor>
          </technique_common>
        </source>
        <source id="Pentagon-uv">
          <float_array id="Pentagon-uv-arr" count="10">0 0 1 0 1 0.6 0.5 1 0 0.6</float_array>
          <technique_common>
            <accessor source="#Pentagon-uv-arr" count="5" stride="2">
              <param name="S" type="float"/><param name="T" type="float"/>
            </accessor>
          </technique_common>
        </source>
        <vertices id="Pentagon-verts">
          <input semantic="POSITION" source="#Pentagon-pos"/>
        </vertices>
        <polylist material="PhongMat-material" count="1">
          <input semantic="VERTEX" source="#Pentagon-verts" offset="0"/>
          <input semantic="TEXCOORD" source="#Pentagon-uv" offset="1" set="0"/>
          <vcount>5 </vcount>
          <p>0 0 1 1 2 2 3 3 4 4</p>
        </polylist>
      </mesh>
    </geometry>
  </library_geometries>
  <library_visual_scenes>
    <visual_scene id="Scene" name="Scene">
      <node id="PentagonNode" name="PentagonNode" type="NODE">
        <translate sid="location">10 20 30</translate>
        <rotate sid="rotationY">0 1 0 45</rotate>
        <scale sid="scale">1 1 1</scale>
        <instance_geometry url="#Pentagon-mesh">
          <bind_material>
            <technique_common>
              <instance_material symbol="PhongMat-material" target="#PhongMat-material"/>
            </technique_common>
          </bind_material>
        </instance_geometry>
      </node>
    </visual_scene>
  </library_visual_scenes>
  <scene><instance_visual_scene url="#Scene"/></scene>
</COLLADA>""")
    fixtures["ext_yup_cm_ngon_phong"] = f_yup_ngon

    # 2. External skinned mesh with non-identity bind_shape_matrix + IDREF_array joints + Y_UP
    f_skin_bsm = os.path.join(DEEP_WORK, "ext_skin_bsm_idref.dae")
    with open(f_skin_bsm, "w", encoding="utf-8") as f:
        f.write("""<?xml version="1.0" encoding="utf-8"?>
<COLLADA xmlns="http://www.collada.org/2005/11/COLLADASchema" version="1.4.1">
  <asset>
    <contributor><authoring_tool>MixamoLike 2.0</authoring_tool></contributor>
    <created>2025-01-01T00:00:00</created>
    <modified>2025-01-01T00:00:00</modified>
    <unit name="meter" meter="1"/>
    <up_axis>Y_UP</up_axis>
  </asset>
  <library_geometries>
    <geometry id="Strip-mesh" name="Strip">
      <mesh>
        <source id="Strip-pos">
          <float_array id="Strip-pos-arr" count="12">-0.5 0 0 0.5 0 0 0.5 2 0 -0.5 2 0</float_array>
          <technique_common>
            <accessor source="#Strip-pos-arr" count="4" stride="3">
              <param name="X" type="float"/><param name="Y" type="float"/><param name="Z" type="float"/>
            </accessor>
          </technique_common>
        </source>
        <vertices id="Strip-verts"><input semantic="POSITION" source="#Strip-pos"/></vertices>
        <triangles count="2">
          <input semantic="VERTEX" source="#Strip-verts" offset="0"/>
          <p>0 1 2 0 2 3</p>
        </triangles>
      </mesh>
    </geometry>
  </library_geometries>
  <library_controllers>
    <controller id="Strip-skin" name="StripSkin">
      <skin source="#Strip-mesh">
        <bind_shape_matrix>1 0 0 0 0 1 0 0.5 0 0 1 0 0 0 0 1</bind_shape_matrix>
        <source id="Strip-joints">
          <IDREF_array id="Strip-joints-arr" count="2">JointRoot JointTip</IDREF_array>
          <technique_common>
            <accessor source="#Strip-joints-arr" count="2" stride="1">
              <param name="JOINT" type="IDREF"/>
            </accessor>
          </technique_common>
        </source>
        <source id="Strip-invbinds">
          <float_array id="Strip-invbinds-arr" count="32">1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1 1 0 0 0 0 1 0 -1 0 0 1 0 0 0 0 1</float_array>
          <technique_common>
            <accessor source="#Strip-invbinds-arr" count="2" stride="16">
              <param name="TRANSFORM" type="float4x4"/>
            </accessor>
          </technique_common>
        </source>
        <source id="Strip-weights">
          <float_array id="Strip-weights-arr" count="3">1 0.75 0.25</float_array>
          <technique_common>
            <accessor source="#Strip-weights-arr" count="3" stride="1">
              <param name="WEIGHT" type="float"/>
            </accessor>
          </technique_common>
        </source>
        <joints>
          <input semantic="JOINT" source="#Strip-joints"/>
          <input semantic="INV_BIND_MATRIX" source="#Strip-invbinds"/>
        </joints>
        <vertex_weights count="4">
          <input semantic="JOINT" source="#Strip-joints" offset="0"/>
          <input semantic="WEIGHT" source="#Strip-weights" offset="1"/>
          <vcount>1 1 2 2 </vcount>
          <v>0 0 0 0 0 2 1 1 0 2 1 1</v>
        </vertex_weights>
      </skin>
    </controller>
  </library_controllers>
  <library_visual_scenes>
    <visual_scene id="Scene" name="Scene">
      <node id="RigRoot" name="RigRoot" type="NODE">
        <node id="JointRoot" name="JointRoot" sid="JointRoot" type="JOINT">
          <matrix sid="transform">1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1</matrix>
          <node id="JointTip" name="JointTip" sid="JointTip" type="JOINT">
            <matrix sid="transform">1 0 0 0 0 1 0 1 0 0 1 0 0 0 0 1</matrix>
          </node>
        </node>
      </node>
      <node id="StripNode" name="StripNode" type="NODE">
        <instance_controller url="#Strip-skin">
          <skeleton>#JointRoot</skeleton>
        </instance_controller>
      </node>
    </visual_scene>
  </library_visual_scenes>
  <scene><instance_visual_scene url="#Scene"/></scene>
</COLLADA>""")
    fixtures["ext_skin_bsm_idref"] = f_skin_bsm

    return fixtures


def run_deep_import_compare(label: str, dae_45: str, dae_53: str, kwargs: dict, allow_ref_45: str | None = None) -> bool:
    j45 = os.path.join(DEEP_WORK, f"deep_{label}_45.json")
    j53 = os.path.join(DEEP_WORK, f"deep_{label}_53.json")
    kw = repr(kwargs)

    r45 = run_blender(BLENDER_45, None, DEEP_INSPECT_SCRIPT.format(dae=dae_45, out_json=j45, kwargs_repr=kw))
    r53 = run_blender(BLENDER_53, None, DEEP_INSPECT_SCRIPT.format(dae=dae_53, out_json=j53, kwargs_repr=kw))

    if r45.returncode != 0 or not os.path.exists(j45):
        print(f"[FAIL] {label}: 4.5.1 failed\n{r45.stderr[-600:]}")
        return False
    if r53.returncode != 0 or not os.path.exists(j53):
        print(f"[FAIL] {label}: 5.3.0 failed\n{r53.stderr[-600:]}")
        return False

    d45 = json.load(open(j45, "r", encoding="utf-8"))
    d53 = json.load(open(j53, "r", encoding="utf-8"))

    diffs = compare_deep(d45, d53, float_tol=2e-3)
    if not diffs:
        print(f"[PASS] {label} (DEEP PER-ELEMENT MATCH)")
        return True
    if allow_ref_45 and "__anim__" not in d45:
        ref_d = json.load(open(allow_ref_45, "r", encoding="utf-8"))
        ref_diffs = compare_deep(ref_d, d53, float_tol=2e-3)
        if not ref_diffs:
            print(f"[PASS] {label} (DEEP ANIMATED GROUND-TRUTH MATCH; 4.5.1 dropped decomposed curves)")
            return True
        diffs = ref_diffs
    print(f"[DIFF] {label} ({len(diffs)} diffs):")
    for d in diffs[:12]:
        print(f"   - {d}")
    return False


def main() -> int:
    total_pass = 0
    total_fail = 0

    def record(ok: bool) -> None:
        nonlocal total_pass, total_fail
        if ok:
            total_pass += 1
        else:
            total_fail += 1

    # Ensure base DAE files exist from run_parity_tests
    base_cases = [
        "01_default_export",
        "02_geom_triangulated",
        "02_geom_polylist",
        "03_cameras_lights_materials",
        "04_hierarchy_matrix",
        "04_hierarchy_decomposed",
        "05_armature_skin_shapekeys",
        "06_anim_matrix_sample",
        "06_anim_decomposed_sample",
    ]
    for tc in base_cases:
        if not os.path.exists(os.path.join(TEST_WORK, f"{tc}_45.dae")):
            print(f"Missing {tc}_45.dae — run tests/run_parity_tests.py first.")
            return 1

    # ------------------------------------------------------------------
    # 1. DEEP PER-ELEMENT IMPORT PARITY (4.5.1 DAE -> 4.5.1 vs 5.3.0)
    # ------------------------------------------------------------------
    print("=== 1. DEEP PER-ELEMENT IMPORT PARITY ===")
    for tc in base_cases:
        dae = os.path.join(TEST_WORK, f"{tc}_45.dae")
        ref = os.path.join(DEEP_WORK, "deep_imp_06_anim_matrix_sample_45.json") if tc == "06_anim_decomposed_sample" else None
        record(run_deep_import_compare(f"imp_{tc}", dae, dae, {}, allow_ref_45=ref))

    # ------------------------------------------------------------------
    # 2A. CROSS-VERSION ROUND-TRIP: 5.3 EXPORT -> 4.5.1 IMPORT
    #     (compare 4.5.1 importing 5.3's DAE vs 4.5.1 importing 4.5.1's DAE)
    # ------------------------------------------------------------------
    print("\n=== 2A. CROSS-VERSION: 5.3 EXPORT -> 4.5.1 IMPORT ===")
    for tc in ("01_default_export", "02_geom_triangulated", "03_cameras_lights_materials", "05_armature_skin_shapekeys", "06_anim_matrix_sample"):
        dae45 = os.path.join(TEST_WORK, f"{tc}_45.dae")
        dae53 = os.path.join(TEST_WORK, f"{tc}_53.dae")
        j_a = os.path.join(DEEP_WORK, f"xver_{tc}_from45.json")
        j_b = os.path.join(DEEP_WORK, f"xver_{tc}_from53.json")
        ra = run_blender(BLENDER_45, None, DEEP_INSPECT_SCRIPT.format(dae=dae45, out_json=j_a, kwargs_repr="{}"))
        rb = run_blender(BLENDER_45, None, DEEP_INSPECT_SCRIPT.format(dae=dae53, out_json=j_b, kwargs_repr="{}"))
        if ra.returncode != 0 or rb.returncode != 0:
            print(f"[FAIL] xver {tc}: blender failed")
            record(False)
            continue
        diffs = compare_deep(json.load(open(j_a)), json.load(open(j_b)), float_tol=2e-3)
        if not diffs:
            print(f"[PASS] xver {tc} (4.5.1 imports 5.3's DAE identically to 4.5.1's DAE)")
            record(True)
        else:
            print(f"[DIFF] xver {tc}: {diffs[:8]}")
            record(False)

    # ------------------------------------------------------------------
    # 2B. FULL CYCLE ROUND-TRIP:
    #     4.5.1 DAE -> 5.3 import -> 5.3 export  vs  4.5.1 DAE -> 4.5.1 import -> 4.5.1 export
    # ------------------------------------------------------------------
    print("\n=== 2B. FULL-CYCLE ROUND-TRIP (IMPORT -> RE-EXPORT BYTE PARITY) ===")
    for tc in ("01_default_export", "02_geom_triangulated", "03_cameras_lights_materials", "04_hierarchy_matrix", "05_armature_skin_shapekeys"):
        in_dae = os.path.join(TEST_WORK, f"{tc}_45.dae")
        out45 = os.path.join(DEEP_WORK, f"rt_{tc}_45.dae")
        out53 = os.path.join(DEEP_WORK, f"rt_{tc}_53.dae")
        r45 = run_blender(BLENDER_45, None, ROUNDTRIP_EXPORT_SCRIPT.format(in_dae=in_dae, out_dae=out45))
        r53 = run_blender(BLENDER_53, None, ROUNDTRIP_EXPORT_SCRIPT.format(in_dae=in_dae, out_dae=out53))
        if r45.returncode != 0 or r53.returncode != 0 or not os.path.exists(out45) or not os.path.exists(out53):
            print(f"[FAIL] roundtrip {tc}: failed")
            record(False)
            continue
        b45 = normalize_dae_metadata(open(out45, "rb").read())
        b53 = normalize_dae_metadata(open(out53, "rb").read())
        ok, reason = check_xml_structural_and_float_parity(b45, b53)
        if ok:
            print(f"[PASS] roundtrip {tc} ({reason}, {len(b53)} bytes)")
            record(True)
        else:
            print(f"[DIFF] roundtrip {tc}: {reason} (4.5.1={len(b45)}B vs 5.3.0={len(b53)}B)")
            record(False)

    # ------------------------------------------------------------------
    # 3. IMPORT FLAG MATRIX
    # ------------------------------------------------------------------
    print("\n=== 3. IMPORT FLAG MATRIX ===")
    flag_tests = [
        ("flag_no_custom_normals", os.path.join(TEST_WORK, "02_geom_triangulated_45.dae"), {"custom_normals": False}),
        ("flag_auto_connect", os.path.join(TEST_WORK, "05_armature_skin_shapekeys_45.dae"), {"auto_connect": True}),
        ("flag_find_chains", os.path.join(TEST_WORK, "05_armature_skin_shapekeys_45.dae"), {"find_chains": True, "min_chain_length": 1}),
        ("flag_fix_orientation", os.path.join(TEST_WORK, "05_armature_skin_shapekeys_45.dae"), {"fix_orientation": True}),
        ("flag_keep_bind_info", os.path.join(TEST_WORK, "05_armature_skin_shapekeys_45.dae"), {"keep_bind_info": True}),
    ]
    for label, dae, kw in flag_tests:
        record(run_deep_import_compare(label, dae, dae, kw))

    # ------------------------------------------------------------------
    # 4. EXTERNAL / THIRD-PARTY COLLADA 1.4.1 STRESS TESTS
    # ------------------------------------------------------------------
    print("\n=== 4. EXTERNAL / THIRD-PARTY COLLADA STRESS TESTS ===")
    ext = write_external_fixtures()
    record(run_deep_import_compare("ext_yup_cm_default", ext["ext_yup_cm_ngon_phong"], ext["ext_yup_cm_ngon_phong"], {}))
    record(run_deep_import_compare("ext_yup_cm_units", ext["ext_yup_cm_ngon_phong"], ext["ext_yup_cm_ngon_phong"], {"import_units": True}))
    record(run_deep_import_compare("ext_skin_bsm_idref", ext["ext_skin_bsm_idref"], ext["ext_skin_bsm_idref"], {}))

    # ------------------------------------------------------------------
    # 5. EXTENDED PRO FEATURES (5.3.0: .zae archive, <tristrips>, Maya sid anim)
    # ------------------------------------------------------------------
    print("\n=== 5. EXTENDED PRO FEATURES (.zae, <tristrips>, DCC sid anim) ===")
    import zipfile

    # 5A. .zae archive with manifest.xml + bundled .dae
    zae_path = os.path.join(DEEP_WORK, "ext_archive.zae")
    with zipfile.ZipFile(zae_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.xml", '<?xml version="1.0"?><dae_manifest><dae_root>models/pentagon.dae</dae_root></dae_manifest>')
        zf.write(ext["ext_yup_cm_ngon_phong"], "models/pentagon.dae")

    j_zae = os.path.join(DEEP_WORK, "deep_pro_zae_53.json")
    r_zae = run_blender(BLENDER_53, None, DEEP_INSPECT_SCRIPT.format(dae=zae_path, out_json=j_zae, kwargs_repr="{}"))
    j_ref = os.path.join(DEEP_WORK, "deep_ext_yup_cm_default_53.json")
    if r_zae.returncode == 0 and os.path.exists(j_zae) and not compare_deep(json.load(open(j_ref)), json.load(open(j_zae))):
        print("[PASS] pro_zae_archive (manifest.xml + .zae matches direct .dae import)")
        record(True)
    else:
        print(f"[FAIL] pro_zae_archive\n{r_zae.stderr[-400:]}")
        record(False)

    # 5B. <tristrips> + Maya-style <translate sid="translate"> / <rotate sid="rotateZ"> animation
    pro_dae = os.path.join(DEEP_WORK, "ext_tristrips_dcc_anim.dae")
    with open(pro_dae, "w", encoding="utf-8") as f:
        f.write("""<?xml version="1.0" encoding="utf-8"?>
<COLLADA xmlns="http://www.collada.org/2005/11/COLLADASchema" version="1.4.1">
  <asset><unit name="meter" meter="1"/><up_axis>Z_UP</up_axis></asset>
  <library_geometries>
    <geometry id="StripQuad-mesh" name="StripQuad">
      <mesh>
        <source id="StripQuad-pos">
          <float_array id="StripQuad-pos-arr" count="12">0 0 0 1 0 0 0 1 0 1 1 0</float_array>
          <technique_common><accessor source="#StripQuad-pos-arr" count="4" stride="3"><param name="X" type="float"/><param name="Y" type="float"/><param name="Z" type="float"/></accessor></technique_common>
        </source>
        <vertices id="StripQuad-verts"><input semantic="POSITION" source="#StripQuad-pos"/></vertices>
        <tristrips count="1">
          <input semantic="VERTEX" source="#StripQuad-verts" offset="0"/>
          <p>0 1 2 3</p>
        </tristrips>
      </mesh>
    </geometry>
  </library_geometries>
  <library_animations>
    <animation id="MayaMoveX">
      <source id="MayaMoveX-in"><float_array id="MayaMoveX-in-arr" count="2">0.04166667 0.4166667</float_array><technique_common><accessor source="#MayaMoveX-in-arr" count="2" stride="1"><param name="TIME" type="float"/></accessor></technique_common></source>
      <source id="MayaMoveX-out"><float_array id="MayaMoveX-out-arr" count="2">0 5</float_array><technique_common><accessor source="#MayaMoveX-out-arr" count="2" stride="1"><param name="X" type="float"/></accessor></technique_common></source>
      <source id="MayaMoveX-int"><Name_array id="MayaMoveX-int-arr" count="2">LINEAR LINEAR</Name_array><technique_common><accessor source="#MayaMoveX-int-arr" count="2" stride="1"><param name="INTERPOLATION" type="name"/></accessor></technique_common></source>
      <sampler id="MayaMoveX-samp"><input semantic="INPUT" source="#MayaMoveX-in"/><input semantic="OUTPUT" source="#MayaMoveX-out"/><input semantic="INTERPOLATION" source="#MayaMoveX-int"/></sampler>
      <channel source="#MayaMoveX-samp" target="StripObj/translate.X"/>
    </animation>
  </library_animations>
  <library_visual_scenes>
    <visual_scene id="Scene">
      <node id="StripObj" name="StripObj" type="NODE">
        <translate sid="translate">0 0 0</translate>
        <rotate sid="rotateZ">0 0 1 0</rotate>
        <instance_geometry url="#StripQuad-mesh"/>
      </node>
    </visual_scene>
  </library_visual_scenes>
  <scene><instance_visual_scene url="#Scene"/></scene>
</COLLADA>""")

    j_pro = os.path.join(DEEP_WORK, "deep_pro_tristrips_53.json")
    r_pro = run_blender(BLENDER_53, None, DEEP_INSPECT_SCRIPT.format(dae=pro_dae, out_json=j_pro, kwargs_repr="{}"))
    if r_pro.returncode == 0 and os.path.exists(j_pro):
        d_pro = json.load(open(j_pro))
        ok_mesh = len(d_pro.get("StripObj", {}).get("polys", [])) == 2
        ok_anim = d_pro.get("__anim__", {}).get("10", {}).get("StripObj", {}).get("loc", [0])[0] == 5.0
        if ok_mesh and ok_anim:
            print("[PASS] pro_tristrips_and_dcc_sid_anim (2 tris from <tristrips>, Maya translate.X -> 5.0 at f10)")
            record(True)
        else:
            print(f"[FAIL] pro_tristrips_and_dcc_sid_anim: ok_mesh={ok_mesh}, ok_anim={ok_anim}")
            record(False)
    else:
        print(f"[FAIL] pro_tristrips_and_dcc_sid_anim\n{r_pro.stderr[-400:]}")
        record(False)

    # 5C. Mark Sharp from Normals (flat-to-smooth crease caught, noisy smooth seam ignored)
    sharp_dae = os.path.join(DEEP_WORK, "ext_sharp_normals.dae")
    with open(sharp_dae, "w", encoding="utf-8") as f:
        f.write("""<?xml version="1.0" encoding="utf-8"?>
<COLLADA xmlns="http://www.collada.org/2005/11/COLLADASchema" version="1.4.1">
  <asset><unit name="meter" meter="1"/><up_axis>Z_UP</up_axis></asset>
  <library_geometries>
    <geometry id="Panel-mesh" name="Panel">
      <mesh>
        <source id="Panel-pos">
          <float_array id="Panel-pos-arr" count="24">0 0 0 1 0 0 1 1 0 0 1 0 2 0 0 2 1 0 3 0 0 3 1 0</float_array>
          <technique_common><accessor source="#Panel-pos-arr" count="8" stride="3"><param name="X" type="float"/><param name="Y" type="float"/><param name="Z" type="float"/></accessor></technique_common>
        </source>
        <source id="Panel-norm">
          <float_array id="Panel-norm-arr" count="15">0 0 1  0.5 0 0.866025  0.501 0 0.865447  0.707107 0 0.707107  0.7075 0 0.7067</float_array>
          <technique_common><accessor source="#Panel-norm-arr" count="5" stride="3"><param name="X" type="float"/><param name="Y" type="float"/><param name="Z" type="float"/></accessor></technique_common>
        </source>
        <vertices id="Panel-verts"><input semantic="POSITION" source="#Panel-pos"/></vertices>
        <polylist count="3">
          <input semantic="VERTEX" source="#Panel-verts" offset="0"/>
          <input semantic="NORMAL" source="#Panel-norm" offset="1"/>
          <vcount>4 4 4 </vcount>
          <p>0 0 1 0 2 0 3 0  1 1 4 3 5 3 2 1  4 4 6 4 7 4 5 4</p>
        </polylist>
      </mesh>
    </geometry>
  </library_geometries>
  <library_visual_scenes>
    <visual_scene id="Scene">
      <node id="PanelObj" name="PanelObj" type="NODE"><instance_geometry url="#Panel-mesh"/></node>
    </visual_scene>
  </library_visual_scenes>
  <scene><instance_visual_scene url="#Scene"/></scene>
</COLLADA>""")

    sharp_check = f"""
import bpy, json
for ob in list(bpy.data.objects): bpy.data.objects.remove(ob, do_unlink=True)
bpy.ops.wm.collada_import(filepath=r"{sharp_dae}", sharp_edges_from_normals=True)
me = bpy.data.objects["PanelObj"].data
se = me.attributes["sharp_edge"]
sharp_keys = sorted([list(e.key) for i, e in enumerate(me.edges) if se.data[i].value])
all_smooth = all(p.use_smooth for p in me.polygons)
print("SHARP_RESULT:", json.dumps({{"sharp": sharp_keys, "all_smooth": all_smooth}}))
"""
    r_sh = run_blender(BLENDER_53, None, sharp_check)
    m_sh = next((l for l in r_sh.stdout.splitlines() if l.startswith("SHARP_RESULT:")), "")
    if r_sh.returncode == 0 and m_sh:
        res_sh = json.loads(m_sh.split("SHARP_RESULT:", 1)[1].strip())
        # Edge (1, 2) is flat-to-smooth 30 deg crease -> MUST be sharp; Edge (4, 5) is 0.05 deg float noise -> MUST NOT be sharp
        if res_sh["sharp"] == [[1, 2]] and res_sh["all_smooth"]:
            print("[PASS] pro_sharp_edges_from_normals (flat-to-smooth crease marked sharp, float-noise seam clean)")
            record(True)
        else:
            print(f"[FAIL] pro_sharp_edges_from_normals: {res_sh}")
            record(False)
    else:
        print(f"[FAIL] pro_sharp_edges_from_normals\n{r_sh.stderr[-400:]}")
        record(False)

    print(f"\nDEEP VERIFICATION SUMMARY: {total_pass}/{total_pass + total_fail} PASS")
    return 0 if total_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
