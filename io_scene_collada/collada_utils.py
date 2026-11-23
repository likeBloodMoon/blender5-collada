# SPDX-FileCopyrightText: 2010-2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Utilities, math, ID sanitization, and Blender 5.0+ API compatibility helpers.

Directly ported from Blender 4.5.1's collada_internal.cpp, collada_utils.cpp, and BCMath.cpp.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

import bpy
import bmesh
from mathutils import Euler, Matrix, Quaternion, Vector
from bpy_extras.io_utils import axis_conversion

if TYPE_CHECKING:
    from bpy.types import Action, AnimData, Bone, FCurve, Material, Mesh, Object, Scene, ViewLayer


LIMITED_PRECISION = 6

# Exact 256-byte translation tables from collada_internal.cpp
_TRANSLATE_START_NAME_MAP = bytes([
    95,  95,  95,  95,  95,  95,  95,  95,  95,  95,  95,  95,  95,  95,  95,  95,  95,  95,  95,
    95,  95,  95,  95,  95,  95,  95,  95,  95,  95,  95,  95,  95,  95,  95,  95,  95,  95,  95,
    95,  95,  95,  95,  95,  95,  95,  95,  95,  95,  95,  95,  95,  95,  95,  95,  95,  95,  95,
    95,  95,  95,  95,  95,  95,  95,  95,  65,  66,  67,  68,  69,  70,  71,  72,  73,  74,  75,
    76,  77,  78,  79,  80,  81,  82,  83,  84,  85,  86,  87,  88,  89,  90,  95,  95,  95,  95,
    95,  95,  97,  98,  99,  100, 101, 102, 103, 104, 105, 106, 107, 108, 109, 110, 111, 112, 113,
    114, 115, 116, 117, 118, 119, 120, 121, 122, 95,  95,  95,  95,  95,

    128, 129, 130, 131, 132, 133, 134, 135, 136, 137, 138, 139, 140, 141, 142, 143, 144, 145, 146,
    147, 148, 149, 150, 151, 152, 153, 154, 155, 156, 157, 158, 159, 160, 161, 162, 163, 164, 165,
    166, 167, 168, 169, 170, 171, 172, 173, 174, 175, 176, 177, 178, 179, 180, 181, 182, 183, 184,
    185, 186, 187, 188, 189, 190, 191, 192, 193, 194, 195, 196, 197, 198, 199, 200, 201, 202, 203,
    204, 205, 206, 207, 208, 209, 210, 211, 212, 213, 214, 215, 216, 217, 218, 219, 220, 221, 222,
    223, 224, 225, 226, 227, 228, 229, 230, 231, 232, 233, 234, 235, 236, 237, 238, 239, 240, 241,
    242, 243, 244, 245, 246, 247, 248, 249, 250, 251, 252, 253, 254, 255,
])

_TRANSLATE_NAME_MAP = bytes([
    95,  95,  95,  95,  95,  95,  95,  95,  95,  95,  95,  95,  95,  95,  95,  95,  95,  95,  95,
    95,  95,  95,  95,  95,  95,  95,  95,  95,  95,  95,  95,  95,  95,  95,  95,  95,  95,  95,
    95,  95,  95,  95,  95,  95,  95,  45,  95,  95,  48,  49,  50,  51,  52,  53,  54,  55,  56,
    57,  95,  95,  95,  95,  95,  95,  95,  65,  66,  67,  68,  69,  70,  71,  72,  73,  74,  75,
    76,  77,  78,  79,  80,  81,  82,  83,  84,  85,  86,  87,  88,  89,  90,  95,  95,  95,  95,
    95,  95,  97,  98,  99,  100, 101, 102, 103, 104, 105, 106, 107, 108, 109, 110, 111, 112, 113,
    114, 115, 116, 117, 118, 119, 120, 121, 122, 95,  95,  95,  95,  95,

    128, 129, 130, 131, 132, 133, 134, 135, 136, 137, 138, 139, 140, 141, 142, 143, 144, 145, 146,
    147, 148, 149, 150, 151, 152, 153, 154, 155, 156, 157, 158, 159, 160, 161, 162, 163, 164, 165,
    166, 167, 168, 169, 170, 171, 172, 173, 174, 175, 176, 177, 178, 179, 180, 181, 182, 183, 184,
    185, 186, 187, 188, 189, 190, 191, 192, 193, 194, 195, 196, 197, 198, 199, 200, 201, 202, 203,
    204, 205, 206, 207, 208, 209, 210, 211, 212, 213, 214, 215, 216, 217, 218, 219, 220, 221, 222,
    223, 224, 225, 226, 227, 228, 229, 230, 231, 232, 233, 234, 235, 236, 237, 238, 239, 240, 241,
    242, 243, 244, 245, 246, 247, 248, 249, 250, 251, 252, 253, 254, 255,
])


class GlobalIdMap:
    """Exact port of global_id_map and translate_id() from collada_internal.cpp."""

    def __init__(self) -> None:
        self._map: dict[str, list[str]] = {}

    def clear(self) -> None:
        self._map.clear()

    def translate_id(self, name: str) -> str:
        if not name:
            return name
        raw = name.encode("utf-8")
        out = bytearray(len(raw))
        out[0] = _TRANSLATE_START_NAME_MAP[raw[0]]
        for i in range(1, len(raw)):
            out[i] = _TRANSLATE_NAME_MAP[raw[i]]
        id_translated = out.decode("utf-8", errors="surrogateescape")

        if id_translated != name:
            bucket = self._map.get(id_translated)
            if bucket is not None:
                found = False
                idx = 0
                for idx, existing in enumerate(bucket):
                    if name == existing:
                        found = True
                        break
                convert = False
                if found:
                    if idx > 0:
                        convert = True
                else:
                    idx = len(bucket)
                    convert = True
                    bucket.append(name)
                if convert:
                    id_translated += str(idx + 1)
            else:
                self._map[id_translated] = [name]
        return id_translated


def get_geometry_id(id_map: GlobalIdMap, ob: Object, use_instantiation: bool = True) -> str:
    geom_name = ob.data.name if use_instantiation else ob.name
    return id_map.translate_id(geom_name) + "-mesh"


def get_light_id(id_map: GlobalIdMap, ob: Object) -> str:
    return id_map.translate_id(ob.name) + "-light"


def get_camera_id(id_map: GlobalIdMap, ob: Object) -> str:
    return id_map.translate_id(ob.name) + "-camera"


def get_effect_id(id_map: GlobalIdMap, mat: Material) -> str:
    return id_map.translate_id(mat.name) + "-effect"


def get_material_id(id_map: GlobalIdMap, mat: Material) -> str:
    return id_map.translate_id(mat.name) + "-material"


def get_joint_sid(id_map: GlobalIdMap, bone_name: str) -> str:
    return id_map.translate_id(bone_name)


def get_controller_id(id_map: GlobalIdMap, ob_arm: Object, ob: Object) -> str:
    return f"{id_map.translate_id(ob_arm.name)}_{id_map.translate_id(ob.name)}-skin"


def get_morph_controller_id(id_map: GlobalIdMap, ob: Object) -> str:
    return f"{id_map.translate_id(ob.name)}-morph"


def get_action_id(
    id_map: GlobalIdMap,
    action_name: str,
    ob_name: str,
    channel_type: str,
    axis_name: str = "",
    axis_separator: str = "_",
) -> str:
    result = f"{action_name}_{channel_type}"
    if ob_name:
        result = f"{ob_name}_{result}"
    if axis_name:
        result += f"{axis_separator}{axis_name}"
    return id_map.translate_id(result)


# ---------------------------------------------------------------------------
# Math & Axis Conversion (exact port of BCMath.cpp & collada_utils.cpp)
# ---------------------------------------------------------------------------

def double_round(val: float, precision: int) -> float:
    """Match Blender's double_round(val, precision)."""
    factor = 10.0 ** precision
    return math.floor(val * factor + 0.5) / factor


def sanitize_matrix(mat: Matrix, precision: int = LIMITED_PRECISION) -> Matrix:
    out = mat.copy()
    for r in range(4):
        for c in range(4):
            out[r][c] = double_round(out[r][c], precision)
    return out


def sanitize_vec3(v: Vector, precision: int = LIMITED_PRECISION) -> Vector:
    return Vector((
        double_round(v[0], precision),
        double_round(v[1], precision),
        double_round(v[2], precision),
    ))


def build_global_transform(global_forward: str, global_up: str) -> Matrix:
    """Exact port of BCMatrix(global_forward, global_up).

    Default in Blender 4.5.1: BC_DEFAULT_FORWARD = 'Y', BC_DEFAULT_UP = 'Z'.
    """
    if global_forward == "Y" and global_up == "Z":
        return Matrix.Identity(4)
    try:
        m3 = axis_conversion(
            from_forward="Y",
            from_up="Z",
            to_forward=global_forward,
            to_up=global_up,
        )
        return m3.to_4x4()
    except Exception:
        return Matrix.Identity(4)


def bc_add_global_transform_mat(mat: Matrix, global_transform: Matrix, invert: bool = False) -> Matrix:
    if invert:
        return global_transform.inverted() @ mat
    return global_transform @ mat


def bc_apply_global_transform_mat(mat: Matrix, global_transform: Matrix, invert: bool = False) -> Matrix:
    globinv = global_transform.inverted()
    if invert:
        return globinv @ mat
    return global_transform @ mat @ globinv


def bc_add_global_transform_vec(vec: Vector, global_transform: Matrix, invert: bool = False) -> Vector:
    gt = sanitize_matrix(global_transform, 6)
    if invert:
        gt = gt.inverted()
    return gt @ vec


def bc_decompose(mat: Matrix) -> tuple[Vector, Euler, Quaternion, Vector]:
    """Match bc_decompose: loc, euler(XYZ), quat, scale."""
    loc, quat, scale = mat.decompose()
    eul = quat.to_euler("XYZ")
    return loc, eul, quat, scale


def matrix_to_dae_flat(mat: Matrix, limit_precision: bool = False) -> list[float]:
    """Convert Blender row-major 4x4 Matrix to COLLADA row-major flat 16 values.

    Note: In Blender C++, Matrix[col][row] is column-major, so transpose_m4 produces
    row-major for COLLADA. In Python mathutils, Matrix[row][col] is ALREADY row-major!
    So iterating row 0..3, col 0..3 gives the exact COLLADA matrix order!
    """
    vals: list[float] = []
    for r in range(4):
        for c in range(4):
            v = mat[r][c]
            if limit_precision:
                v = double_round(v, LIMITED_PRECISION)
            vals.append(v)
    return vals


def dae_flat_to_matrix(vals: list[float]) -> Matrix:
    """Convert 16 COLLADA matrix floats (row-major) into mathutils.Matrix."""
    return Matrix((
        (vals[0], vals[1], vals[2], vals[3]),
        (vals[4], vals[5], vals[6], vals[7]),
        (vals[8], vals[9], vals[10], vals[11]),
        (vals[12], vals[13], vals[14], vals[15]),
    ))


def focallength_to_fov(focal_length: float, sensor: float) -> float:
    return 2.0 * math.atan((sensor / 2.0) / focal_length)


def fov_to_focallength(hfov: float, sensor: float) -> float:
    return sensor / (2.0 * math.tan(hfov / 2.0))


# ---------------------------------------------------------------------------
# Object & Export Set Helpers
# ---------------------------------------------------------------------------

def get_assigned_armature(ob: Object) -> Object | None:
    """Exact port of bc_get_assigned_armature()."""
    if ob.parent and ob.parent_type == "ARMATURE" and ob.parent.type == "ARMATURE":
        return ob.parent
    ob_arm = None
    for mod in ob.modifiers:
        if mod.type == "ARMATURE" and mod.object is not None:
            ob_arm = mod.object
    return ob_arm


def is_object_visible_in_view_layer(ob: Object, view_layer: ViewLayer) -> bool:
    return ob.visible_get(view_layer=view_layer)


def build_export_set(
    scene: Scene,
    view_layer: ViewLayer,
    selected: bool,
    include_children: bool,
    include_armatures: bool,
    sort_by_name: bool,
    collection: str = "",
) -> list[Object]:
    """Exact port of BKE_object_relational_superset + bc_bubble_sort_by_Object_name."""
    # Base objects in view_layer order (matching BKE_object_relational_superset)
    # In Blender C++, view_layer->object_bases is iterated in order.
    if collection:
        coll = bpy.data.collections.get(collection)
        if coll is None:
            return []
        coll_ptrs = {ob.as_pointer() for ob in coll.all_objects}
        visible_objs = [
            ob for ob in view_layer.objects
            if ob.as_pointer() in coll_ptrs and ob.visible_get(view_layer=view_layer)
        ]
    else:
        visible_objs = [ob for ob in view_layer.objects if ob.visible_get(view_layer=view_layer)]

    if selected:
        seed = [ob for ob in visible_objs if ob.select_get(view_layer=view_layer)]
    else:
        seed = list(visible_objs)

    export_set: list[Object] = []
    seen: set[int] = set()

    def add_obj(o: Object) -> None:
        ptr = o.as_pointer()
        if ptr not in seen:
            seen.add(ptr)
            export_set.append(o)

    for ob in seed:
        add_obj(ob)

    if include_children or include_armatures:
        # Iteratively expand relations
        idx = 0
        while idx < len(export_set):
            ob = export_set[idx]
            idx += 1
            if include_children:
                for child in visible_objs:
                    if child.parent == ob:
                        add_obj(child)
            if include_armatures:
                arm = get_assigned_armature(ob)
                if arm is not None and arm.visible_get(view_layer=view_layer):
                    add_obj(arm)

    if sort_by_name:
        export_set.sort(key=lambda o: o.name)
    else:
        # BKE_object_relational_superset uses BLI_linklist_prepend while iterating
        # view_layer->object_bases forward, which reverses the order.
        export_set.reverse()

    return export_set


def get_children_in_scene(ob: Object, view_layer: ViewLayer) -> list[Object]:
    """Match bc_get_children: return children of ob in view_layer order."""
    return [c for c in view_layer.objects if c.parent == ob]


def is_export_root(ob: Object, export_set_ptrs: set[int]) -> bool:
    """Exact port of bc_is_base_node()."""
    par = ob.parent
    while par is not None:
        if par.as_pointer() in export_set_ptrs:
            return False
        par = par.parent
    return True


def is_root_bone(bone: Bone, deform_bones_only: bool) -> bool:
    if deform_bones_only:
        root = None
        b = bone
        while b is not None:
            if b.use_deform:
                root = b
            b = b.parent
        return bone == root
    return bone.parent is None


def is_leaf_bone(bone: Bone) -> bool:
    for child in bone.children:
        if child.use_connect:
            return False
    return True


def get_bone_property_matrix(bone: Bone, key: str) -> Matrix | None:
    prop = bone.get(key)
    if prop is not None and len(prop) == 16:
        # Stored in C++ column-major float[4][4], so mat[i][j] = array[4*i + j]
        # In mathutils row-major, column i row j -> row j col i
        return Matrix((
            (prop[0], prop[4], prop[8], prop[12]),
            (prop[1], prop[5], prop[9], prop[13]),
            (prop[2], prop[6], prop[10], prop[14]),
            (prop[3], prop[7], prop[11], prop[15]),
        ))
    return None


def set_editbone_property_matrix(ebone, key: str, mat: Matrix) -> None:
    # Store in C++ column-major layout for full compatibility with 4.5.1
    arr = [0.0] * 16
    for c in range(4):
        for r in range(4):
            arr[4 * c + r] = float(mat[r][c])
    ebone[key] = arr


# ---------------------------------------------------------------------------
# Mesh Helpers (Triangulation matching bc_triangulate_mesh)
# ---------------------------------------------------------------------------

def get_mesh_copy(
    depsgraph,
    ob: Object,
    apply_modifiers: bool,
    triangulate: bool,
) -> tuple[Mesh, bool]:
    """Return (mesh, needs_free).

    If apply_modifiers is True, gets evaluated mesh copy.
    If triangulate is True, triangulates using SHORT_EDGE quad method and CLIP ngon method,
    matching bc_triangulate_mesh() in collada_utils.cpp.
    """
    if apply_modifiers:
        ob_eval = ob.evaluated_get(depsgraph)
        me = ob_eval.to_mesh(preserve_all_data_layers=True, depsgraph=depsgraph)
        mesh_copy = me.copy()
        ob_eval.to_mesh_clear()
    else:
        mesh_copy = ob.data.copy()

    if triangulate:
        bm = bmesh.new()
        bm.from_mesh(mesh_copy)
        bm.normal_update()
        bmesh.ops.triangulate(
            bm,
            faces=bm.faces[:],
            quad_method="SHORT_EDGE",
            ngon_method="BEAUTY",
        )
        bm.to_mesh(mesh_copy)
        bm.free()

    return mesh_copy, True


# ---------------------------------------------------------------------------
# Material / Principled BSDF Helpers (Blender 5.0+ safe, no use_nodes warning)
# ---------------------------------------------------------------------------

def get_master_shader(mat: Material):
    """Exact port of bc_get_master_shader()."""
    ntree = mat.node_tree
    if ntree is None:
        return None
    for node in ntree.nodes:
        if node.bl_idname == "ShaderNodeBsdfPrincipled":
            return node
    return None


def get_diffuse_image(mat: Material):
    """Exact port of MaterialNode::get_diffuse_image()."""
    shader = get_master_shader(mat)
    if shader is None:
        return None
    in_socket = shader.inputs.get("Base Color")
    if in_socket is None or not in_socket.is_linked:
        return None
    link = in_socket.links[0]
    tex_node = link.from_node
    if tex_node is None or tex_node.bl_idname != "ShaderNodeTexImage":
        return None
    return tex_node.image


# ---------------------------------------------------------------------------
# Blender 5.0+ Layered Action / Channelbag Helpers
# ---------------------------------------------------------------------------

def get_assigned_fcurves(anim_data: AnimData | None) -> list[FCurve]:
    """Return list of FCurves for an AnimData's assigned action/slot in Blender 5.0+."""
    if anim_data is None or anim_data.action is None:
        return []
    action = anim_data.action
    # Blender 5.0+ layered actions
    if hasattr(action, "layers"):
        from bpy_extras import anim_utils
        slot = anim_data.action_slot
        if slot is None:
            # Fallback: try first suitable slot or first slot
            if len(action.slots) > 0:
                slot = action.slots[0]
            else:
                return []
        cbag = anim_utils.action_get_channelbag_for_slot(action, slot)
        if cbag is not None:
            return list(cbag.fcurves)
        return []
    # Fallback for older Blender 4.x
    if hasattr(action, "fcurves"):
        return list(action.fcurves)
    return []


def ensure_action_and_channelbag(id_block, action_name: str | None = None):
    """Ensure id_block has animation_data, an Action, an assigned Slot, and a Channelbag."""
    if id_block.animation_data is None:
        id_block.animation_data_create()
    adt = id_block.animation_data
    if adt.action is None:
        name = action_name or f"{id_block.name}Action"
        adt.action = bpy.data.actions.new(name=name)
    action = adt.action

    if hasattr(action, "layers"):
        from bpy_extras import anim_utils
        if adt.action_slot is None:
            slot = action.slots.new(id_type=id_block.id_type, name=id_block.name)
            adt.action_slot = slot
        cbag = anim_utils.action_ensure_channelbag_for_slot(action, adt.action_slot)
        return action, cbag
    return action, action


def create_fcurve_on_channelbag(cbag, data_path: str, array_index: int = 0):
    """Create or find an FCurve on a channelbag (Blender 5.0+) or legacy Action."""
    fcu = cbag.fcurves.find(data_path, index=array_index)
    if fcu is None:
        fcu = cbag.fcurves.new(data_path=data_path, index=array_index)
    return fcu


def assigned_action_has_keyframes(anim_data: AnimData | None) -> bool:
    for fcu in get_assigned_fcurves(anim_data):
        if len(fcu.keyframe_points) > 0:
            return True
    return False
