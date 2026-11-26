# SPDX-FileCopyrightText: 2009-2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Skin and morph controllers, armatures, edit bones, bone collections, and shape keys."""

from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import TYPE_CHECKING

import bpy
from mathutils import Matrix, Vector

from ..collada_utils import dae_flat_to_matrix, set_editbone_property_matrix
from .xml_parse import (
    _find_child,
    _find_children,
    _parse_extra_blender,
    _parse_floats,
    _parse_ints,
    _parse_strings,
    _safe_float,
    _safe_int,
    _slice_accessor_array,
    _uri_to_local_id,
)

if TYPE_CHECKING:
    from bpy.types import Object

MINIMUM_BONE_LENGTH = 0.000001


@dataclass
class _SkinData:
    controller_id: str
    geometry_id: str
    bind_shape_matrix: Matrix
    joint_names: list[str]
    inv_bind_matrices: list[Matrix]
    vcounts: list[int]
    v_pairs: list[tuple[int, float]]  # flattened (joint_idx, weight)
    ob_arm: Object | None = None


@dataclass
class _MorphData:
    controller_id: str
    geometry_id: str
    target_geom_ids: list[str]
    weights: list[float]


class RigMixin:
    def _read_library_controllers(self, lib: ET.Element) -> None:
        for ctrl in _find_children(lib, "controller"):
            cid = ctrl.attrib.get("id", "")
            skin = _find_child(ctrl, "skin")
            if skin is not None:
                self._read_skin_controller(cid, skin)
            morph = _find_child(ctrl, "morph")
            if morph is not None:
                self._read_morph_controller(cid, morph)

    def _read_skin_controller(self, cid: str, skin: ET.Element) -> None:
        geom_id = _uri_to_local_id(skin.attrib.get("source"))
        bsm_el = _find_child(skin, "bind_shape_matrix")
        bsm_vals = _parse_floats(bsm_el.text) if bsm_el is not None else []
        bsm = dae_flat_to_matrix(bsm_vals) if len(bsm_vals) >= 16 else Matrix.Identity(4)

        name_sources: dict[str, list[str]] = {}
        float_sources: dict[str, tuple[list[float], int | None]] = {}
        for src in _find_children(skin, "source"):
            sid = src.attrib.get("id", "")
            stride_val: int | None = None
            acc = None
            tc = _find_child(src, "technique_common")
            if tc is not None:
                acc = _find_child(tc, "accessor")
                if acc is not None and "stride" in acc.attrib:
                    try:
                        stride_val = int(acc.attrib["stride"])
                    except ValueError:
                        self.warnings.append(
                            f"Invalid stride '{acc.attrib['stride']}' in skin '{cid}' source '{sid}'"
                        )
                        stride_val = 0
            eff_stride = stride_val if (stride_val is not None and stride_val > 0) else 1
            na = _find_child(src, "Name_array")
            if na is None:
                na = _find_child(src, "IDREF_array")
            if na is not None:
                names_raw = _parse_strings(na.text)
                name_sources[sid] = _slice_accessor_array(
                    names_raw, acc, eff_stride, self.warnings, f"skin '{cid}' source '{sid}'"
                )
            fa = _find_child(src, "float_array")
            if fa is not None:
                floats_raw = _parse_floats(fa.text)
                floats_sliced = _slice_accessor_array(
                    floats_raw, acc, eff_stride, self.warnings, f"skin '{cid}' source '{sid}'"
                )
                float_sources[sid] = (floats_sliced, stride_val)

        joint_names: list[str] = []
        inv_bind_mats: list[Matrix] = []
        joints_el = _find_child(skin, "joints")
        if joints_el is not None:
            for inp in _find_children(joints_el, "input"):
                sem = inp.attrib.get("semantic", "")
                sid = _uri_to_local_id(inp.attrib.get("source"))
                if sem == "JOINT":
                    joint_names = name_sources.get(sid, [])
                elif sem == "INV_BIND_MATRIX":
                    floats, s_stride = float_sources.get(sid, ([], None))
                    if s_stride is not None and s_stride < 16:
                        self.warnings.append(
                            f"Skin '{cid}': INV_BIND_MATRIX source '{sid}' has stride {s_stride} < 16; skipping bind matrices"
                        )
                    else:
                        step = s_stride if (s_stride is not None and s_stride >= 16) else 16
                        for i in range(len(floats) // step):
                            inv_bind_mats.append(dae_flat_to_matrix(floats[i * step:i * step + 16]))

        vcounts: list[int] = []
        v_pairs: list[tuple[int, float]] = []
        vw_el = _find_child(skin, "vertex_weights")
        if vw_el is not None:
            j_off = -1
            w_off = -1
            w_src = ""
            max_off = -1
            for inp in _find_children(vw_el, "input"):
                sem = inp.attrib.get("semantic", "")
                raw_off = inp.attrib.get("offset", "0")
                try:
                    off = int(raw_off)
                except ValueError:
                    self.warnings.append(f"Skin '{cid}': invalid vertex_weights offset '{raw_off}'")
                    continue
                if off < 0:
                    self.warnings.append(f"Skin '{cid}': negative vertex_weights offset {off}")
                    continue
                if off > max_off:
                    max_off = off
                if sem == "JOINT":
                    j_off = off
                elif sem == "WEIGHT":
                    w_off = off
                    w_src = _uri_to_local_id(inp.attrib.get("source"))
            stride = max_off + 1 if max_off >= 0 else 0
            weights, w_stride = float_sources.get(w_src, ([], None))
            if w_stride is not None and w_stride <= 0:
                self.warnings.append(
                    f"Skin '{cid}': WEIGHT source '{w_src}' has invalid stride {w_stride}; skipping weights"
                )
                weights = []
            elif w_stride is not None and w_stride > 1 and weights:
                weights = weights[::w_stride]
            vc_el = _find_child(vw_el, "vcount")
            v_el = _find_child(vw_el, "v")
            raw_vcounts = _parse_ints(vc_el.text) if vc_el is not None else []
            v_ints = _parse_ints(v_el.text) if v_el is not None else []
            if stride > 0 and j_off >= 0 and w_off >= 0:
                max_pairs = len(v_ints) // stride
                neg_vc = 0
                truncated = False
                used_pairs = 0
                for vc in raw_vcounts:
                    if vc < 0:
                        neg_vc += 1
                        vcounts.append(0)
                        continue
                    rem = max_pairs - used_pairs
                    if rem <= 0:
                        if vc > 0:
                            truncated = True
                        break
                    if vc > rem:
                        vcounts.append(rem)
                        used_pairs += rem
                        truncated = True
                        break
                    vcounts.append(vc)
                    used_pairs += vc
                if neg_vc > 0:
                    self.warnings.append(f"Skin '{cid}': clamped {neg_vc} negative <vcount> entries to 0")
                if truncated:
                    self.warnings.append(
                        f"Skin '{cid}': <vertex_weights>/<vcount> exceeds <v> pairs ({max_pairs}); truncated"
                    )
                for i in range(used_pairs):
                    base = i * stride
                    ji = v_ints[base + j_off]
                    wi = v_ints[base + w_off]
                    w = weights[wi] if 0 <= wi < len(weights) else 0.0
                    v_pairs.append((ji, w))

        self.skins_by_id[cid] = _SkinData(
            controller_id=cid,
            geometry_id=geom_id,
            bind_shape_matrix=bsm,
            joint_names=joint_names,
            inv_bind_matrices=inv_bind_mats,
            vcounts=vcounts,
            v_pairs=v_pairs,
        )

    def _read_morph_controller(self, cid: str, morph: ET.Element) -> None:
        geom_id = _uri_to_local_id(morph.attrib.get("source"))
        idref_sources: dict[str, list[str]] = {}
        float_sources: dict[str, list[float]] = {}
        for src in _find_children(morph, "source"):
            sid = src.attrib.get("id", "")
            ia = _find_child(src, "IDREF_array")
            if ia is None:
                ia = _find_child(src, "Name_array")
            if ia is not None:
                idref_sources[sid] = _parse_strings(ia.text)
            fa = _find_child(src, "float_array")
            if fa is not None:
                float_sources[sid] = _parse_floats(fa.text)

        targets_el = _find_child(morph, "targets")
        target_ids: list[str] = []
        weights: list[float] = []
        if targets_el is not None:
            for inp in _find_children(targets_el, "input"):
                sem = inp.attrib.get("semantic", "")
                sid = _uri_to_local_id(inp.attrib.get("source"))
                if sem == "MORPH_TARGET":
                    target_ids = idref_sources.get(sid, [])
                elif sem == "MORPH_WEIGHT":
                    weights = float_sources.get(sid, [])

        self.morphs_by_id[cid] = _MorphData(
            controller_id=cid,
            geometry_id=geom_id,
            target_geom_ids=target_ids,
            weights=weights,
        )

    def _apply_armature_collections(self, node: ET.Element, arm_data) -> None:
        extras = _parse_extra_blender(node)
        if not extras:
            return
        all_colls = [c for c in extras.get("collections", "").split("\n") if c]
        vis_colls = set(c for c in extras.get("visible_collections", "").split("\n") if c)
        for name in all_colls:
            bc = arm_data.collections.new(name)
            bc.is_visible = name in vis_colls
        active_name = extras.get("active_collection", "")
        if active_name and active_name in arm_data.collections:
            arm_data.collections.active = arm_data.collections[active_name]

    def _make_armatures(self) -> None:
        # Index all joint nodes under root_joint_nodes
        def index_joints(jnode: ET.Element, ob_arm: Object, parent_j: ET.Element | None) -> None:
            jid = jnode.attrib.get("id", "")
            jsid = jnode.attrib.get("sid", "")
            jname = jnode.attrib.get("name") or jid or jsid
            if jid:
                self.joint_elem_by_id[jid] = jnode
                self.joint_arm_by_id[jid] = ob_arm
                self.joint_bone_name_by_id[jid] = jname
                self.joint_parent_elem_by_id[jid] = parent_j
            if jsid:
                self.joint_elem_by_sid[jsid] = jnode
            for ch in _find_children(jnode, "node"):
                if ch.attrib.get("type", "NODE").upper() == "JOINT":
                    index_joints(ch, ob_arm, jnode)

        for rj, ob_arm in self.root_joint_nodes:
            index_joints(rj, ob_arm, None)

        # Build lookup from joint_elem -> inv_bind_mat across all skins
        joint_inv_bind: dict[int, Matrix] = {}
        for skin in self.skins_by_id.values():
            for jname, ibm in zip(skin.joint_names, skin.inv_bind_matrices):
                je = self.joint_elem_by_sid.get(jname) or self.joint_elem_by_id.get(jname)
                if je is not None:
                    joint_inv_bind[id(je)] = ibm
                    if skin.ob_arm is None:
                        jid = je.attrib.get("id", "")
                        skin.ob_arm = self.joint_arm_by_id.get(jid)

        # Group root joints by armature object
        arms_to_roots: dict[int, tuple[Object, list[ET.Element]]] = {}
        for rj, ob_arm in self.root_joint_nodes:
            entry = arms_to_roots.get(ob_arm.as_pointer())
            if entry is None:
                arms_to_roots[ob_arm.as_pointer()] = (ob_arm, [rj])
            else:
                entry[1].append(rj)

        for ob_arm, rjoints in arms_to_roots.values():
            self._build_armature_edit_bones(ob_arm, rjoints, joint_inv_bind)

        # Create non-JOINT child objects parented to armature bones (#34)
        n_mesh_before = len(self.imported_mesh_objects)

        def attach_bone_children(jnode: ET.Element, ob_arm: Object) -> None:
            jid = jnode.attrib.get("id", "")
            bname = (
                self.joint_bone_name_by_id.get(jid)
                or jnode.attrib.get("name")
                or jid
                or jnode.attrib.get("sid", "")
            )
            for ch in _find_children(jnode, "node"):
                if ch.attrib.get("type", "NODE").upper() == "JOINT":
                    attach_bone_children(ch, ob_arm)
                else:
                    self._process_node(ch, jnode, ob_arm, parent_bone=bname)

        for ob_arm, rjoints in arms_to_roots.values():
            for rj in rjoints:
                attach_bone_children(rj, ob_arm)

        if len(self.imported_mesh_objects) > n_mesh_before:
            self._optimize_material_assignments()

        # Link skins to meshes
        for skin in self.skins_by_id.values():
            ob_mesh = self.object_by_geom_id.get(skin.geometry_id)
            ob_arm = skin.ob_arm
            if ob_mesh is None or ob_arm is None:
                continue
            mod = ob_mesh.modifiers.new(name="Armature", type="ARMATURE")
            mod.object = ob_arm
            mod.use_vertex_groups = True
            if ob_mesh.parent != ob_arm:
                ob_mesh.parent = ob_arm
                if ob_mesh in self.root_objects:
                    self.root_objects.remove(ob_mesh)
            ob_mesh.matrix_basis = skin.bind_shape_matrix

            # Create vertex groups
            vgroups = []
            for jname in skin.joint_names:
                je = self.joint_elem_by_sid.get(jname) or self.joint_elem_by_id.get(jname)
                bname = (je.attrib.get("name") or je.attrib.get("id") or jname) if je is not None else jname
                vgroups.append(ob_mesh.vertex_groups.new(name=bname))

            n_mesh_verts = len(ob_mesh.data.vertices)
            cursor = 0
            for vi, vc in enumerate(skin.vcounts):
                if vi >= n_mesh_verts or cursor >= len(skin.v_pairs):
                    break
                for _ in range(vc):
                    if cursor < len(skin.v_pairs):
                        ji, w = skin.v_pairs[cursor]
                        if 0 <= ji < len(vgroups) and math.isfinite(w) and w > 0.0:
                            vgroups[ji].add([vi], w, "REPLACE")
                    cursor += 1

    def _build_armature_edit_bones(
        self, ob_arm: Object, root_joints: list[ET.Element], joint_inv_bind: dict[int, Matrix]
    ) -> None:
        self.view_layer.objects.active = ob_arm
        bpy.ops.object.mode_set(mode="EDIT")
        arm = ob_arm.data
        try:
            arm_world_inv = ob_arm.matrix_world.inverted()
        except ValueError:
            arm_world_inv = Matrix.Identity(4)

        leaf_bone_length = float("inf")
        ebone_extras: dict[str, dict[str, str]] = {}
        ebone_has_tail: set[str] = set()
        ebone_chain_len: dict[str, int] = {}

        def create_bone_rec(jnode: ET.Element, parent_eb, parent_mat: Matrix | None, sibcount: int) -> int:
            nonlocal leaf_bone_length
            bname = jnode.attrib.get("name") or jnode.attrib.get("id") or "Bone"
            eb = arm.edit_bones.new(bname)
            extras = _parse_extra_blender(jnode)
            ebone_extras[eb.name] = extras

            ibm = joint_inv_bind.get(id(jnode))
            bone_is_skinned = False
            if ibm is not None:
                try:
                    joint_bind_mat = ibm.inverted()
                    mat = arm_world_inv @ joint_bind_mat
                    bone_is_skinned = True
                except ValueError:
                    self.warnings.append(f"Bone '{bname}': singular inverse bind matrix; falling back to node transform")
                    ibm = None
            if ibm is None:
                local_m = self._read_node_matrix(jnode)
                mat = (parent_mat @ local_m) if parent_mat is not None else local_m
                joint_bind_mat = mat

            if parent_eb is not None:
                eb.parent = parent_eb

            # Collections
            for cname in (c for c in extras.get("collections", "").split("\n") if c):
                if cname in arm.collections:
                    arm.collections[cname].assign(eb)

            # Connect (must be set BEFORE eb.head so setting eb.use_connect=True
            # does not snap eb.head to the parent's placeholder tail!)
            has_tail = "tip_x" in extras and "tip_y" in extras and "tip_z" in extras
            has_connect = "connect" in extras
            connect_type = extras.get("connect", "") in ("1", "true", "True") if has_connect else False
            if has_tail and not has_connect:
                has_connect = True
                connect_type = False
            if not has_connect and self.settings.auto_connect:
                connect_type = sibcount == 1
            eb.use_connect = connect_type

            # Head & Tail
            eb.head = mat.translation
            if has_tail:
                tip = Vector((
                    _safe_float(extras["tip_x"]),
                    _safe_float(extras["tip_y"]),
                    _safe_float(extras["tip_z"]),
                ))
                if tip.length_squared > 0.0:
                    eb.tail = eb.head + tip
                    ebone_has_tail.add(eb.name)
                else:
                    eb.tail = eb.head + Vector((0.0, 0.5, 0.0))
            else:
                eb.tail = eb.head + Vector((0.0, 0.5, 0.0))

            # Roll
            if "roll" in extras:
                eb.roll = _safe_float(extras["roll"])
            else:
                _, roll = bpy.types.Bone.AxisRollFromMatrix(mat.to_3x3())
                eb.roll = roll

            if bone_is_skinned and self.settings.keep_bind_info:
                rest_m = self._read_node_matrix(jnode)
                set_editbone_property_matrix(eb, "bind_mat", joint_bind_mat)
                set_editbone_property_matrix(eb, "rest_mat", rest_m)

            if parent_eb is not None:
                if eb.use_connect:
                    parent_eb.tail = eb.head.copy()
                    ebone_has_tail.add(parent_eb.name)
                dist = (eb.head - parent_eb.head).length
                if dist < leaf_bone_length and dist > MINIMUM_BONE_LENGTH:
                    leaf_bone_length = dist

            child_joints = [
                ch for ch in _find_children(jnode, "node")
                if ch.attrib.get("type", "NODE").upper() == "JOINT"
            ]
            max_cl = 0
            for ch in child_joints:
                cl = create_bone_rec(ch, eb, mat, len(child_joints))
                if cl > max_cl:
                    max_cl = cl
            chain_len = max_cl + 1
            ebone_chain_len[eb.name] = chain_len
            return chain_len

        for rj in root_joints:
            create_bone_rec(rj, None, None, len(root_joints))

        # Connect bone chains if find_chains is requested (ArmatureImporter.cpp:280-358)
        if self.settings.find_chains:
            UNLIMITED_CHAIN_MAX = 10**9

            def connect_bone_chains(parent_eb, max_chain_len: int) -> None:
                children = list(parent_eb.children)
                dominant_child = None
                maxlen = 0
                if children:
                    for ch in children:
                        cl = ebone_chain_len.get(ch.name, 1)
                        if cl <= max_chain_len:
                            if cl > maxlen:
                                dominant_child = ch
                                maxlen = cl
                            elif cl == maxlen:
                                dominant_child = None
                if dominant_child is not None:
                    if not dominant_child.use_connect:
                        vec = dominant_child.head - parent_eb.head
                        if vec.length_squared > MINIMUM_BONE_LENGTH:
                            parent_eb.tail = dominant_child.head.copy()
                            ebone_has_tail.add(parent_eb.name)
                            if ebone_chain_len.get(parent_eb.name, 1) >= self.settings.min_chain_length:
                                dominant_child.use_connect = True
                    for ch in children:
                        connect_bone_chains(ch, UNLIMITED_CHAIN_MAX)
                elif maxlen > 1 and maxlen > self.settings.min_chain_length:
                    connect_bone_chains(parent_eb, maxlen - 1)
                else:
                    for ch in children:
                        connect_bone_chains(ch, UNLIMITED_CHAIN_MAX)

            for eb in list(arm.edit_bones):
                if eb.parent is None:
                    connect_bone_chains(eb, UNLIMITED_CHAIN_MAX)

        # Fix leaf bones
        default_leaf_len = 1.0 if leaf_bone_length == float("inf") else leaf_bone_length
        for eb in arm.edit_bones:
            is_leaf = not any(c.use_connect for c in eb.children)
            if is_leaf and eb.name not in ebone_has_tail:
                if self.settings.fix_orientation and eb.parent is not None:
                    vec = eb.head - eb.parent.head
                    if vec.length_squared < MINIMUM_BONE_LENGTH:
                        vec = eb.parent.tail - eb.parent.head
                else:
                    vec = eb.tail - eb.head
                if vec.length > 0.0:
                    vec.normalize()
                    eb.tail = eb.head + vec * default_leaf_len

        bpy.ops.object.mode_set(mode="OBJECT")

        # Set pose bone rotation mode (EUL if decomposed else QUAT)
        def set_rotmode_rec(jnode: ET.Element) -> None:
            bname = jnode.attrib.get("name") or jnode.attrib.get("id") or "Bone"
            pchan = ob_arm.pose.bones.get(bname)
            if pchan is not None:
                pchan.rotation_mode = "XYZ" if self._node_is_decomposed(jnode) else "QUATERNION"
            for ch in _find_children(jnode, "node"):
                if ch.attrib.get("type", "NODE").upper() == "JOINT":
                    set_rotmode_rec(ch)

        for rj in root_joints:
            set_rotmode_rec(rj)

    def _make_shape_keys(self) -> None:
        for morph in self.morphs_by_id.values():
            ob = self.object_by_geom_id.get(morph.geometry_id)
            if ob is None or ob.type != "MESH":
                continue
            if ob.data.shape_keys is None:
                ob.shape_key_add(name="Basis", from_mix=False)
            for idx, tid in enumerate(morph.target_geom_ids):
                tmesh = self.meshes_by_id.get(tid)
                if tmesh is None or len(tmesh.vertices) != len(ob.data.vertices):
                    continue
                sk = ob.shape_key_add(name=tmesh.name, from_mix=False)
                for vi, v in enumerate(tmesh.vertices):
                    sk.data[vi].co = v.co
                if idx < len(morph.weights):
                    sk.value = morph.weights[idx]
