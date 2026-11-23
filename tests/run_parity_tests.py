# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Automated Byte-Parity & Import-Parity Test Suite: Blender 4.5.1 vs Blender 5.3.0."""

from __future__ import annotations

import difflib
import glob
import json
import os
import re
import subprocess
import sys
import tempfile


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.join(ROOT, ".tools")
TEST_WORK = os.path.join(ROOT, "tests", "_work")
os.makedirs(TEST_WORK, exist_ok=True)


def find_blender(prefix: str, env_var: str) -> str:
    env_path = os.environ.get(env_var)
    if env_path and os.path.exists(env_path):
        return env_path
    exe_names = ("blender.exe", "blender") if os.name == "nt" else ("blender", "blender.exe")
    for exe in exe_names:
        matches = glob.glob(os.path.join(TOOLS, f"blender-{prefix}*", exe))
        if matches:
            return matches[0]
    raise RuntimeError(f"Could not find Blender {prefix} in {TOOLS} (or via {env_var})")


BLENDER_45 = find_blender("4.5", "BLENDER_45")
BLENDER_53 = find_blender("5.", "BLENDER_5X")


def normalize_dae_metadata(data: bytes) -> bytes:
    """Normalize build-version authoring_tool, timestamps, and C++ pointer-ordered animation containers."""
    text = data.decode("utf-8")
    text = re.sub(r"<authoring_tool>.*?</authoring_tool>", "<authoring_tool>NORMALIZED</authoring_tool>", text)
    text = re.sub(r"<created>.*?</created>", "<created>NORMALIZED</created>", text)
    text = re.sub(r"<modified>.*?</modified>", "<modified>NORMALIZED</modified>", text)

    # Blender 4.5.1's BCAnimationSampler iterates a pointer-keyed std::map<Object*, ...>,
    # so top-level <animation id="action_container-..."> order varies by heap address across OSes.
    def _sort_anim_containers(m: re.Match) -> str:
        body = m.group(1)
        blocks = re.findall(r'    <animation id="action_container-[^"]*".*?    </animation>\n', body, flags=re.DOTALL)
        if len(blocks) >= 2 and "".join(blocks) == body:
            blocks.sort(key=lambda b: re.search(r'id="(action_container-[^"]*)"', b).group(1))
            return "<library_animations>\n" + "".join(blocks) + "  </library_animations>"
        return m.group(0)

    text = re.sub(r"<library_animations>\n(.*?)  </library_animations>", _sort_anim_containers, text, flags=re.DOTALL)
    return text.encode("utf-8")


def check_xml_structural_and_float_parity(b45: bytes, b53: bytes, tol: float = 1e-5) -> tuple[bool, str]:
    """Verify exact XML byte structure/whitespace/order while allowing <= 1e-5 float32 ULP rounding."""
    if b45 == b53:
        return True, "EXACT BYTE MATCH"
    s45 = b45.decode("utf-8")
    s53 = b53.decode("utf-8")
    # Split into float tokens vs non-float literal spans
    num_re = re.compile(r"-?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?")
    parts45 = num_re.split(s45)
    parts53 = num_re.split(s53)
    if parts45 != parts53:
        return False, "XML structure/text differs"
    nums45 = num_re.findall(s45)
    nums53 = num_re.findall(s53)
    if len(nums45) != len(nums53):
        return False, f"Numeric token count differs ({len(nums45)} vs {len(nums53)})"
    max_diff = 0.0
    for n1, n2 in zip(nums45, nums53):
        if n1 == n2:
            continue
        v1, v2 = float(n1), float(n2)
        d = abs(v1 - v2)
        if d > max_diff:
            max_diff = d
        limit = max(tol, abs(v1) * 1e-6)
        if d > limit:
            return False, f"Numeric diff {n1} vs {n2} (delta={d:.3g} > {limit:.3g})"
    return True, f"STRUCTURAL BYTE MATCH (max float delta={max_diff:.2e})"


def run_blender(exe: str, blend_path: str | None, script: str) -> subprocess.CompletedProcess:
    cmd = [exe, "-b"]
    if blend_path:
        cmd.append(blend_path)
    cmd.extend(["--python-expr", script])
    return subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT)


# ---------------------------------------------------------------------------
# Scene Generator Scripts (run in Blender 4.5.1 to produce shared .blend files)
# ---------------------------------------------------------------------------

SCENE_GENERATORS: dict[str, str] = {
    "01_default": """
import bpy
bpy.ops.wm.save_as_mainfile(filepath=r"{blend}")
""",
    "02_advanced_geometry": """
import bpy, bmesh, math

bpy.ops.wm.read_factory_settings(use_empty=True)

# 1. Smooth UV sphere with 2 UV maps and vertex color
bpy.ops.mesh.primitive_uv_sphere_add(segments=8, ring_count=6, location=(0, 0, 0))
sph = bpy.context.active_object
sph.name = "SmoothSphere"
for p in sph.data.polygons:
    p.use_smooth = True
sph.data.uv_layers.new(name="UVMap_Second")
cattr = sph.data.color_attributes.new(name="Col", type="BYTE_COLOR", domain="CORNER")
for i in range(len(cattr.data)):
    cattr.data[i].color_srgb = (0.25, 0.5, 0.75, 1.0)

# 2. Multi-material n-gon cylinder + loose edges
bpy.ops.mesh.primitive_cylinder_add(vertices=6, location=(3, 0, 0))
cyl = bpy.context.active_object
cyl.name = "MultiMatCylinder"
m1 = bpy.data.materials.new("RedMat")
m1.use_nodes = True
m1.node_tree.nodes["Principled BSDF"].inputs["Base Color"].default_value = (1, 0, 0, 1)
m2 = bpy.data.materials.new("BlueMat")
m2.use_nodes = True
m2.node_tree.nodes["Principled BSDF"].inputs["Base Color"].default_value = (0, 0, 1, 1)
cyl.data.materials.append(m1)
cyl.data.materials.append(m2)
for i, p in enumerate(cyl.data.polygons):
    p.material_index = i % 2
# Add a loose edge
bm = bmesh.new()
bm.from_mesh(cyl.data)
v1 = bm.verts.new((0, 0, 2))
v2 = bm.verts.new((1, 0, 2))
bm.edges.new((v1, v2))
bm.to_mesh(cyl.data)
bm.free()

# 3. Instanced cube (2 objects sharing 1 mesh)
bpy.ops.mesh.primitive_cube_add(location=(-3, 0, 0))
c1 = bpy.context.active_object
c1.name = "InstCubeA"
c2 = bpy.data.objects.new("InstCubeB", c1.data)
c2.location = (-3, 3, 0)
bpy.context.scene.collection.objects.link(c2)

# 4. Mesh with Solidify modifier (consistent across 4.5.1 and 5.3.0)
bpy.ops.mesh.primitive_cube_add(location=(0, 4, 0))
mod_ob = bpy.context.active_object
mod_ob.name = "ModCube"
mod = mod_ob.modifiers.new("Solidify", "SOLIDIFY")
mod.thickness = 0.2

bpy.ops.wm.save_as_mainfile(filepath=r"{blend}")
""",
    "03_cameras_lights_materials": """
import bpy, math, os

bpy.ops.wm.read_factory_settings(use_empty=True)

# Perspective Camera
cam1_d = bpy.data.cameras.new("PerspCam")
cam1_d.type = "PERSP"
cam1_d.lens = 35.0
cam1_d.shift_x = 0.05
cam1_d.shift_y = -0.02
cam1_d.dof.focus_distance = 12.5
cam1 = bpy.data.objects.new("PerspCam", cam1_d)
cam1.location = (5, -5, 3)
bpy.context.scene.collection.objects.link(cam1)

# Ortho Camera
cam2_d = bpy.data.cameras.new("OrthoCam")
cam2_d.type = "ORTHO"
cam2_d.ortho_scale = 8.0
cam2_d.clip_start = 0.5
cam2_d.clip_end = 200.0
cam2 = bpy.data.objects.new("OrthoCam", cam2_d)
cam2.location = (0, -10, 0)
bpy.context.scene.collection.objects.link(cam2)

# Lights: Point, Sun, Spot, Area
for ltype, name, loc in [
    ("POINT", "PointLight", (2, 2, 4)),
    ("SUN", "SunLight", (0, 0, 10)),
    ("SPOT", "SpotLight", (-2, 2, 5)),
    ("AREA", "AreaLight", (0, -3, 4)),
]:
    ld = bpy.data.lights.new(name, ltype)
    ld.color = (0.9, 0.8, 0.7)
    ld.energy = 250.0 if ltype != "SUN" else 3.0
    if ltype == "SPOT":
        ld.spot_size = math.radians(45.0)
        ld.spot_blend = 0.25
    if ltype == "AREA":
        ld.shape = "RECTANGLE"
        ld.size = 2.0
        ld.size_y = 1.5
    lo = bpy.data.objects.new(name, ld)
    lo.location = loc
    bpy.context.scene.collection.objects.link(lo)

# Material with emission, metallic, alpha, IOR
bpy.ops.mesh.primitive_cube_add(location=(0, 0, 0))
cube = bpy.context.active_object
cube.name = "PBRCube"
mat = bpy.data.materials.new("PBRMat")
mat.use_nodes = True
bsdf = mat.node_tree.nodes["Principled BSDF"]
bsdf.inputs["Base Color"].default_value = (0.2, 0.6, 0.8, 1.0)
bsdf.inputs["Metallic"].default_value = 0.75
bsdf.inputs["Alpha"].default_value = 0.6
bsdf.inputs["IOR"].default_value = 1.65
bsdf.inputs["Emission Color"].default_value = (0.4, 0.2, 0.1, 1.0)
bsdf.inputs["Emission Strength"].default_value = 1.5
cube.data.materials.append(mat)

# Textured mesh with generated PNG image saved on disk
tex_path = os.path.join(os.path.dirname(r"{blend}"), "test_checker.png")
img = bpy.data.images.new("CheckerTex", width=16, height=16)
img.filepath_raw = tex_path
img.file_format = "PNG"
img.save()

bpy.ops.mesh.primitive_plane_add(location=(3, 0, 0))
plane = bpy.context.active_object
plane.name = "TexturedPlane"
tmat = bpy.data.materials.new("TexMat")
tmat.use_nodes = True
nt = tmat.node_tree
tbsdf = nt.nodes["Principled BSDF"]
tnode = nt.nodes.new("ShaderNodeTexImage")
tnode.image = img
nt.links.new(tnode.outputs["Color"], tbsdf.inputs["Base Color"])
plane.data.materials.append(tmat)

bpy.ops.wm.save_as_mainfile(filepath=r"{blend}")
""",
    "04_hierarchy_transforms": """
import bpy, math

bpy.ops.wm.read_factory_settings(use_empty=True)

empty = bpy.data.objects.new("RootEmpty", None)
empty.location = (1.5, -2.0, 0.5)
empty.rotation_euler = (math.radians(15), math.radians(30), math.radians(45))
empty.scale = (1.2, 1.0, 0.8)
bpy.context.scene.collection.objects.link(empty)

bpy.ops.mesh.primitive_cube_add(location=(0, 0, 0))
child1 = bpy.context.active_object
child1.name = "ChildCube"
child1.parent = empty
child1.location = (2.0, 0.0, 1.0)
child1.rotation_euler = (0.0, math.radians(60), 0.0)

bpy.ops.mesh.primitive_cone_add(vertices=8, location=(0, 0, 0))
grandchild = bpy.context.active_object
grandchild.name = "GrandChildCone"
grandchild.parent = child1
grandchild.location = (0.0, 1.5, 0.5)
grandchild.scale = (0.5, 0.5, 2.0)

bpy.ops.wm.save_as_mainfile(filepath=r"{blend}")
""",
    "05_armature_skin_shapekeys": """
import bpy, math

bpy.ops.wm.read_factory_settings(use_empty=True)

# Create Armature
arm_d = bpy.data.armatures.new("TestArmature")
arm_ob = bpy.data.objects.new("TestArmature", arm_d)
bpy.context.scene.collection.objects.link(arm_ob)
bpy.context.view_layer.objects.active = arm_ob
bpy.ops.object.mode_set(mode="EDIT")

b_root = arm_d.edit_bones.new("Root")
b_root.head = (0, 0, 0)
b_root.tail = (0, 0, 1)

b_spine = arm_d.edit_bones.new("Spine")
b_spine.parent = b_root
b_spine.use_connect = True
b_spine.head = (0, 0, 1)
b_spine.tail = (0, 0, 2)

b_arm = arm_d.edit_bones.new("Arm_L")
b_arm.parent = b_spine
b_arm.use_connect = False
b_arm.head = (0.5, 0, 1.8)
b_arm.tail = (1.5, 0, 1.8)
b_arm.roll = math.radians(20.0)

bpy.ops.object.mode_set(mode="OBJECT")

# Create Skinned Mesh with Shape Keys
bpy.ops.mesh.primitive_cube_add(location=(0, 0, 1))
mesh_ob = bpy.context.active_object
mesh_ob.name = "SkinnedMesh"
mesh_ob.parent = arm_ob
mod = mesh_ob.modifiers.new("Armature", "ARMATURE")
mod.object = arm_ob

vg_root = mesh_ob.vertex_groups.new(name="Root")
vg_spine = mesh_ob.vertex_groups.new(name="Spine")
for v in mesh_ob.data.vertices:
    if v.co.z < 0:
        vg_root.add([v.index], 1.0, "REPLACE")
    else:
        vg_root.add([v.index], 0.3, "REPLACE")
        vg_spine.add([v.index], 0.7, "REPLACE")

# Shape keys
mesh_ob.shape_key_add(name="Basis", from_mix=False)
sk1 = mesh_ob.shape_key_add(name="Stretch", from_mix=False)
for v in sk1.data:
    v.co.z *= 1.5
sk1.value = 0.4

bpy.ops.wm.save_as_mainfile(filepath=r"{blend}")
""",
    "06_animations": """
import bpy, math

bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene
scene.frame_start = 1
scene.frame_end = 10

bpy.ops.mesh.primitive_cube_add(location=(0, 0, 0))
cube = bpy.context.active_object
cube.name = "AnimCube"
cube.location = (0, 0, 0)
cube.rotation_euler = (0, 0, 0)
cube.keyframe_insert(data_path="location", frame=1)
cube.keyframe_insert(data_path="rotation_euler", frame=1)

cube.location = (4, 2, 1)
cube.rotation_euler = (0, 0, math.radians(90))
cube.keyframe_insert(data_path="location", frame=10)
cube.keyframe_insert(data_path="rotation_euler", frame=10)

# Animated Armature
arm_d = bpy.data.armatures.new("AnimArm")
arm_ob = bpy.data.objects.new("AnimArm", arm_d)
bpy.context.scene.collection.objects.link(arm_ob)
bpy.context.view_layer.objects.active = arm_ob
bpy.ops.object.mode_set(mode="EDIT")
b1 = arm_d.edit_bones.new("BoneA")
b1.head = (0, 0, 0)
b1.tail = (0, 0, 1)
b2 = arm_d.edit_bones.new("BoneB")
b2.parent = b1
b2.use_connect = True
b2.head = (0, 0, 1)
b2.tail = (0, 0, 2)
bpy.ops.object.mode_set(mode="OBJECT")

pb = arm_ob.pose.bones["BoneB"]
pb.rotation_mode = "XYZ"
pb.rotation_euler = (0, 0, 0)
pb.keyframe_insert(data_path="rotation_euler", frame=1)
pb.rotation_euler = (math.radians(45), 0, 0)
pb.keyframe_insert(data_path="rotation_euler", frame=10)

bpy.ops.wm.save_as_mainfile(filepath=r"{blend}")
""",
}


EXPORT_TEST_CASES: list[tuple[str, str, dict]] = [
    ("01_default_export", "01_default", {}),
    ("02_geom_triangulated", "02_advanced_geometry", {"triangulate": True}),
    ("02_geom_polylist", "02_advanced_geometry", {"triangulate": False}),
    ("02_geom_active_uv_only", "02_advanced_geometry", {"active_uv_only": True}),
    ("02_geom_no_instantiation", "02_advanced_geometry", {"use_object_instantiation": False}),
    ("02_geom_apply_modifiers", "02_advanced_geometry", {"apply_modifiers": True}),
    ("03_cameras_lights_materials", "03_cameras_lights_materials", {"use_texture_copies": True}),
    ("04_hierarchy_matrix", "04_hierarchy_transforms", {"export_object_transformation_type_selection": "matrix", "include_animations": False}),
    ("04_hierarchy_decomposed", "04_hierarchy_transforms", {"export_object_transformation_type_selection": "decomposed", "include_animations": False}),
    ("04_hierarchy_global_orient", "04_hierarchy_transforms", {"apply_global_orientation": True, "export_global_forward_selection": "-Z", "export_global_up_selection": "Y", "include_animations": False}),
    ("04_hierarchy_sort_precision", "04_hierarchy_transforms", {"sort_by_name": True, "limit_precision": True, "include_animations": False}),
    ("05_armature_skin_shapekeys", "05_armature_skin_shapekeys", {"include_shapekeys": True}),
    ("05_armature_opensim", "05_armature_skin_shapekeys", {"open_sim": True, "deform_bones_only": True}),
    ("06_anim_matrix_sample", "06_animations", {"include_animations": True, "export_animation_type_selection": "sample", "export_animation_transformation_type_selection": "matrix", "sampling_rate": 3}),
    ("06_anim_decomposed_sample", "06_animations", {"include_animations": True, "export_animation_type_selection": "sample", "export_animation_transformation_type_selection": "decomposed", "sampling_rate": 3}),
    ("06_anim_curves_keys", "06_animations", {"include_animations": True, "export_animation_type_selection": "keys", "keep_smooth_curves": True}),
]


IMPORT_INSPECT_SCRIPT = """
import bpy, json

# Clear scene without read_factory_settings (which unloads user extensions)
for ob in list(bpy.data.objects):
    bpy.data.objects.remove(ob, do_unlink=True)
for col in (bpy.data.meshes, bpy.data.cameras, bpy.data.lights, bpy.data.materials, bpy.data.armatures, bpy.data.actions, bpy.data.images):
    for item in list(col):
        col.remove(item)

res = bpy.ops.wm.collada_import(filepath=r"{dae}")
assert "FINISHED" in res, f"Import failed: {{res}}"

def r3(v):
    return [round(float(x), 4) for x in v]

def r4(v):
    return [round(float(x), 4) for x in v]

info = {{}}
for ob in sorted(bpy.data.objects, key=lambda o: o.name):
    entry = {{
        "type": ob.type,
        "parent": ob.parent.name if ob.parent else None,
        "loc": r3(ob.matrix_world.translation),
        "scale": r3(ob.matrix_world.to_scale()),
        "rot_quat": r4(ob.matrix_world.to_quaternion()),
    }}
    if ob.type == "MESH":
        me = ob.data
        entry["verts"] = len(me.vertices)
        entry["edges"] = len(me.edges)
        entry["polys"] = len(me.polygons)
        entry["uv_layers"] = len(me.uv_layers)
        entry["color_attrs"] = len(me.color_attributes)
        entry["vgroups"] = sorted([vg.name for vg in ob.vertex_groups])
        entry["shapekeys"] = [kb.name for kb in me.shape_keys.key_blocks] if me.shape_keys else []
        entry["mats"] = [s.material.name if s.material else None for s in ob.material_slots]
    elif ob.type == "CAMERA":
        cam = ob.data
        entry["cam_type"] = cam.type
        entry["lens"] = round(cam.lens, 3)
        entry["ortho_scale"] = round(cam.ortho_scale, 3)
        entry["clip"] = [round(cam.clip_start, 4), round(cam.clip_end, 4)]
    elif ob.type == "LIGHT":
        la = ob.data
        entry["light_type"] = la.type
        entry["color"] = r3(la.color)
        entry["energy"] = round(la.energy, 3)
    elif ob.type == "ARMATURE":
        entry["bones"] = {{
            b.name: {{
                "parent": b.parent.name if b.parent else None,
                "head": r3(b.head_local),
                "tail": r3(b.tail_local),
                "connect": b.use_connect,
            }}
            for b in ob.data.bones
        }}
    info[ob.name] = entry

# Sample animated positions at frame 1 and frame 10 if any actions exist
if len(bpy.data.actions) > 0:
    anim_check = {{}}
    for fr in (1, 10):
        bpy.context.scene.frame_set(fr)
        fr_data = {{}}
        for ob in sorted(bpy.data.objects, key=lambda o: o.name):
            fr_data[ob.name] = r3(ob.matrix_world.translation)
        anim_check[str(fr)] = fr_data
    info["__anim__"] = anim_check

with open(r"{out_json}", "w", encoding="utf-8") as f:
    json.dump(info, f, indent=2, sort_keys=True)
"""


def main() -> int:
    print(f"Blender 4.5.1: {BLENDER_45}")
    print(f"Blender 5.3.0: {BLENDER_53}")

    # Step 1: Generate shared .blend test files in Blender 4.5.1
    blend_files: dict[str, str] = {}
    for name, gen_code in SCENE_GENERATORS.items():
        bpath = os.path.join(TEST_WORK, f"{name}.blend")
        res = run_blender(BLENDER_45, None, gen_code.format(blend=bpath))
        if res.returncode != 0 or not os.path.exists(bpath):
            print(f"[FAIL] Generating {name}.blend:\n{res.stdout}\n{res.stderr}")
            return 1
        blend_files[name] = bpath
    print(f"Generated {len(blend_files)} shared .blend test scenes.\n")

    # Step 2: Run Export Byte-Parity Tests
    print("=== EXPORT BYTE-PARITY TESTS ===")
    export_pass = 0
    export_fail = 0
    dae_45_paths: dict[str, str] = {}

    for test_name, scene_key, kwargs in EXPORT_TEST_CASES:
        bpath = blend_files[scene_key]
        dae45 = os.path.join(TEST_WORK, f"{test_name}_45.dae")
        dae53 = os.path.join(TEST_WORK, f"{test_name}_53.dae")
        dae_45_paths[test_name] = dae45

        kw_str = ", ".join(f"{k}={repr(v)}" for k, v in kwargs.items())
        if kw_str:
            kw_str = ", " + kw_str

        s45 = f"import bpy; bpy.ops.wm.collada_export(filepath=r'{dae45}'{kw_str})"
        s53 = f"import bpy; bpy.ops.wm.collada_export(filepath=r'{dae53}'{kw_str})"

        r45 = run_blender(BLENDER_45, bpath, s45)
        r53 = run_blender(BLENDER_53, bpath, s53)

        if r45.returncode != 0 or not os.path.exists(dae45):
            print(f"[FAIL] {test_name}: 4.5.1 export failed\n{r45.stderr}")
            export_fail += 1
            continue
        if r53.returncode != 0 or not os.path.exists(dae53):
            print(f"[FAIL] {test_name}: 5.3.0 export failed\n{r53.stderr}")
            export_fail += 1
            continue

        b45 = normalize_dae_metadata(open(dae45, "rb").read())
        b53 = normalize_dae_metadata(open(dae53, "rb").read())

        ok, reason = check_xml_structural_and_float_parity(b45, b53)
        if ok:
            print(f"[PASS] {test_name} ({reason}, {len(b53)} bytes)")
            export_pass += 1
        else:
            print(f"[DIFF] {test_name}: {reason} (4.5.1={len(b45)}B vs 5.3.0={len(b53)}B)")
            a_lines = b45.decode("utf-8").splitlines(True)
            b_lines = b53.decode("utf-8").splitlines(True)
            diff = list(difflib.unified_diff(a_lines, b_lines, fromfile="4.5.1", tofile="5.3.0", n=1))
            print("".join(diff[:30]))
            if len(diff) > 30:
                print(f"  ... ({len(diff) - 30} more diff lines)")
            export_fail += 1

    # Step 3: Run Import Parity Tests (import 4.5.1-exported .dae into both 4.5.1 and 5.3.0)
    print("\n=== IMPORT PARITY TESTS ===")
    import_cases = [
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
    import_pass = 0
    import_fail = 0
    for tc in import_cases:
        dae = dae_45_paths[tc]
        j45 = os.path.join(TEST_WORK, f"imp_{tc}_45.json")
        j53 = os.path.join(TEST_WORK, f"imp_{tc}_53.json")

        r45 = run_blender(BLENDER_45, None, IMPORT_INSPECT_SCRIPT.format(dae=dae, out_json=j45))
        r53 = run_blender(BLENDER_53, None, IMPORT_INSPECT_SCRIPT.format(dae=dae, out_json=j53))

        if r45.returncode != 0 or not os.path.exists(j45):
            print(f"[FAIL] import {tc}: 4.5.1 failed\n{r45.stderr}")
            import_fail += 1
            continue
        if r53.returncode != 0 or not os.path.exists(j53):
            print(f"[FAIL] import {tc}: 5.3.0 failed\n{r53.stderr}")
            import_fail += 1
            continue

        d45 = json.load(open(j45, "r", encoding="utf-8"))
        d53 = json.load(open(j53, "r", encoding="utf-8"))
        if d45 == d53:
            print(f"[PASS] import {tc} (EXACT SCENE DATA MATCH)")
            import_pass += 1
        elif tc == "06_anim_decomposed_sample" and "__anim__" not in d45:
            # Blender 4.5.1's native OpenCOLLADA importer drops decomposed curve
            # channels ("removed 5 unused curves", ACTIONS=[]). Validate 5.3.0's
            # imported animated scene against 4.5.1's ground-truth animated scene
            # from 06_anim_matrix_sample instead.
            ref_path = os.path.join(TEST_WORK, "imp_06_anim_matrix_sample_45.json")
            ref_data = json.load(open(ref_path, "r", encoding="utf-8"))
            if d53 == ref_data:
                print(f"[PASS] import {tc} (EXACT ANIMATED GROUND-TRUTH MATCH; 4.5.1 dropped decomposed curves)")
                import_pass += 1
            else:
                print(f"[DIFF] import {tc} (vs animated ground truth)")
                a_l = json.dumps(ref_data, indent=2, sort_keys=True).splitlines(True)
                b_l = json.dumps(d53, indent=2, sort_keys=True).splitlines(True)
                diff = list(difflib.unified_diff(a_l, b_l, fromfile="ground_truth", tofile="5.3.0", n=1))
                print("".join(diff[:35]))
                import_fail += 1
        else:
            print(f"[DIFF] import {tc}")
            a_l = json.dumps(d45, indent=2, sort_keys=True).splitlines(True)
            b_l = json.dumps(d53, indent=2, sort_keys=True).splitlines(True)
            diff = list(difflib.unified_diff(a_l, b_l, fromfile="4.5.1", tofile="5.3.0", n=1))
            print("".join(diff[:35]))
            import_fail += 1

    print(f"\nSUMMARY: Export {export_pass}/{export_pass + export_fail} | Import {import_pass}/{import_pass + import_fail}")
    return 0 if (export_fail == 0 and import_fail == 0) else 1


if __name__ == "__main__":
    sys.exit(main())
