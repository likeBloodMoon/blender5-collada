"""Tests for BeamNG Cached Collada (.cdae v31) reader & Blender importer (Phase 1.5).

Covers:
  1. Pure-Python msgpack_lite unit tests against hand-written byte fixtures.
  2. Synthetic .cdae v31 fixtures:
     - GPU-split cube (24 verts) -> merge_by_distance (8 verts) + UV0/UV1/color corner
       preservation + sharp_edges_from_normals (12 sharp edges)
     - Triangle Strip & Fan primitives + multi-material assignment
     - parentMesh >= 0 child mesh sharing vertex/UV/normal/color arrays from parent
     - encodedNorms (Torque3D 256-entry table) fallback when norms is empty
     - Multi-LOD (HIGHEST vs ALL collections) + collision detail toggle
     - SkinMesh (meshType=1) creating Armature, vertex groups, Armature modifier, and
       sequence-skip info message
     - Unsupported version rejection & strict elemSize mismatch rejection
  3. Subprocess fuzz test: corrupt synthetic .cdae files (truncate, flip bytes, lie in
     counts & elemSize) -> zero crashes, zero tracebacks, clean error reporting.
  4. Optional real-file verification when CDAE_LOCAL_DIR is set (or tests/_local_cdae/
     contains .cdae files).

Usage:
    python tests/test_cdae_import.py
    CDAE_LOCAL_DIR=/path/to/cdae_folder python tests/test_cdae_import.py
"""

from __future__ import annotations

import importlib.util
import json
import os
import random
import shutil
import struct
import subprocess
import sys
import tempfile

import numpy as np

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_mod(name: str, rel_path: str):
    path = os.path.join(REPO_ROOT, rel_path)
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


_mp = _load_mod("io_scene_collada.cdae.msgpack_lite", os.path.join("io_scene_collada", "cdae", "msgpack_lite.py"))
MsgPackReader = _mp.MsgPackReader
MsgPackWriter = _mp.MsgPackWriter
MsgPackError = _mp.MsgPackError


def _find_runner() -> list[str]:
    blender = os.environ.get("BLENDER_5X") or os.environ.get("BLENDER_EXE")
    if blender and os.path.isfile(blender):
        return [blender, "-b", "--factory-startup", "--python"]
    local_bl = os.path.join(REPO_ROOT, ".tools", "blender-5.3.0-windows-x64", "blender.exe")
    if os.path.isfile(local_bl):
        return [local_bl, "-b", "--factory-startup", "--python"]
    return [sys.executable]


# ---------------------------------------------------------------------------
# 1. msgpack_lite unit tests against hand-written byte fixtures
# ---------------------------------------------------------------------------

def test_msgpack_lite_unit() -> None:
    # Hand-written MsgPack fixtures per MsgPack spec
    fixtures = [
        (b"\x00", 0),
        (b"\x7f", 127),
        (b"\xff", -1),
        (b"\xe0", -32),
        (b"\xc0", None),
        (b"\xc2", False),
        (b"\xc3", True),
        (b"\xcc\xc8", 200),
        (b"\xcd\x12\x34", 0x1234),
        (b"\xce\x00\x01\x00\x00", 65536),
        (b"\xd0\x80", -128),
        (b"\xd1\x80\x00", -32768),
        (b"\xd2\xff\xff\x00\x00", -65536),
        (b"\xca\x3f\x80\x00\x00", 1.0),
        (b"\xcb\x3f\xf0\x00\x00\x00\x00\x00\x00", 1.0),
        (b"\xa5hello", "hello"),
        (b"\xd9\x03abc", "abc"),
        (b"\xc4\x04\xde\xad\xbe\xef", b"\xde\xad\xbe\xef"),
        (b"\x93\x01\x02\x03", [1, 2, 3]),
        (b"\x82\xabcompression\xc2\xa8bodysize\xcd\x01\x00", {"compression": False, "bodysize": 256}),
    ]
    for raw, expected in fixtures:
        r = MsgPackReader(raw)
        got = r.read(zero_copy_bin=False)
        assert got == expected and r.eof, f"Fixture mismatch: {raw!r} -> {got!r} != {expected!r}"

    # Zero-copy bin returns memoryview usable by numpy.frombuffer
    raw_f32 = struct.pack("<3f", 1.5, -2.5, 3.25)
    r = MsgPackReader(b"\xc4" + bytes((len(raw_f32),)) + raw_f32)
    mv = r.read(zero_copy_bin=True)
    assert isinstance(mv, memoryview)
    arr = np.frombuffer(mv, dtype="<f4")
    assert np.allclose(arr, [1.5, -2.5, 3.25])

    # Round-trip writer -> reader
    w = MsgPackWriter()
    payload = {"compression": True, "bodysize": 123456, "tags": ["a", "b"], "neg": -99999}
    w.write(payload)
    r2 = MsgPackReader(w.getvalue())
    assert r2.read(zero_copy_bin=False) == payload and r2.eof

    # Truncation & impossible-count rejection
    for bad in (b"\xce\x00\x01", b"\xc4\x10abc", b"\xdd\x7f\xff\xff\xff", b"\xdf\x7f\xff\xff\xff", b"\xc1"):
        try:
            MsgPackReader(bad).read()
            raise AssertionError(f"Expected MsgPackError for {bad!r}")
        except MsgPackError:
            pass
    print("  PASS msgpack_lite_unit")


# ---------------------------------------------------------------------------
# 2. Synthetic .cdae v31 builder for tests
# ---------------------------------------------------------------------------

def _pack_vec(w: MsgPackWriter, count: int, elem_size: int, raw: bytes) -> None:
    w.write_int(count)
    w.write_int(elem_size)
    w.write_bin(raw)


def build_synthetic_cdae(
    *,
    shape_version: int = 31,
    exporter_version: int = 124,
    nodes_elem_size_override: int | None = None,
    nodes: list[tuple[int, int, int, int, int]] | None = None,
    default_rotations: list[tuple[int, int, int, int]] | None = None,
    default_translations: list[tuple[float, float, float]] | None = None,
    objects: list[tuple[int, int, int, int, int, int]] | None = None,
    details: list[tuple] | None = None,
    names: list[str] | None = None,
    meshes: list[dict] | None = None,
    materials: list[str] | None = None,
    num_sequences: int = 0,
) -> bytes:
    """Serialize a valid (or intentionally broken) .cdae v31 file in memory."""
    names = names or ["Root", "Obj", "detail2"]
    nodes = nodes if nodes is not None else [(0, -1, 0, -1, -1)]
    default_rotations = default_rotations if default_rotations is not None else [(0, 0, 0, 32767)] * len(nodes)
    default_translations = default_translations if default_translations is not None else [(0.0, 0.0, 0.0)] * len(nodes)
    objects = objects if objects is not None else [(1, 1, 0, 0, -1, -1)]
    details = details if details is not None else [(2, 0, 0, 2.0, -1.0, -1.0, 0, 0, 0, 0, 0, 0.0, 0)]
    meshes = meshes or []
    materials = materials if materials is not None else []

    w = MsgPackWriter()
    # 3.1 Shape scalars
    w.write_float32(2.0)
    w.write_int(0)
    w.write_float32(1.0)
    w.write_float32(1.0)
    w.write_bin(struct.pack("<3f", 0.0, 0.0, 0.0))
    w.write_bin(struct.pack("<6f", -1.0, -1.0, -1.0, 1.0, 1.0, 1.0))

    # 3.2 19 pack_vectors
    node_bytes = b"".join(struct.pack("<5i", *n) for n in nodes)
    _pack_vec(w, len(nodes), nodes_elem_size_override if nodes_elem_size_override is not None else 20, node_bytes)
    obj_bytes = b"".join(struct.pack("<6i", *o) for o in objects)
    _pack_vec(w, len(objects), 24, obj_bytes)
    _pack_vec(w, 1, 4, struct.pack("<i", 0))
    _pack_vec(w, 1, 4, struct.pack("<i", 0))
    _pack_vec(w, 1, 4, struct.pack("<i", len(nodes)))
    _pack_vec(w, 1, 4, struct.pack("<i", len(objects)))
    _pack_vec(w, len(default_rotations), 8, b"".join(struct.pack("<4h", *q) for q in default_rotations))
    _pack_vec(w, len(default_translations), 12, b"".join(struct.pack("<3f", *t) for t in default_translations))
    # Animation vectors (empty)
    _pack_vec(w, 0, 8, b"")
    _pack_vec(w, 0, 12, b"")
    _pack_vec(w, 0, 4, b"")
    _pack_vec(w, 0, 12, b"")
    _pack_vec(w, 0, 12, b"")
    _pack_vec(w, 0, 8, b"")
    _pack_vec(w, 0, 12, b"")
    _pack_vec(w, 0, 8, b"")
    _pack_vec(w, 0, 12, b"")
    _pack_vec(w, 0, 8, b"")
    det_bytes = b"".join(struct.pack("<3i3fi2i2IfI", *d) for d in details)
    _pack_vec(w, len(details), 52, det_bytes)

    # 3.3 Names
    w.write_int(len(names))
    for nm in names:
        w.write_str(nm)

    # 3.4 Meshes
    w.write_int(len(meshes))
    for m in meshes:
        mtype = m.get("type", 0)
        w.write_int(mtype)
        if mtype == 4:
            continue
        w.write_int(m.get("num_frames", 1))
        w.write_int(m.get("num_mat_frames", 1))
        parent_mesh = m.get("parent_mesh", -1)
        w.write_int(parent_mesh)
        w.write_bin(struct.pack("<6f", -1.0, -1.0, -1.0, 1.0, 1.0, 1.0))
        w.write_bin(struct.pack("<3f", 0.0, 0.0, 0.0))
        w.write_float32(1.0)

        verts = np.asarray(m.get("verts", np.empty((0, 3), dtype=np.float32)), dtype="<f4")
        tverts = np.asarray(m.get("tverts", np.empty((0, 2), dtype=np.float32)), dtype="<f4")
        tverts2 = np.asarray(m.get("tverts2", np.empty((0, 2), dtype=np.float32)), dtype="<f4")
        colors = np.asarray(m.get("colors", np.empty((0, 4), dtype=np.uint8)), dtype="<u1")
        norms = np.asarray(m.get("norms", np.empty((0, 3), dtype=np.float32)), dtype="<f4")
        enc_norms = np.asarray(m.get("encoded_norms", np.empty(0, dtype=np.uint8)), dtype="<u1")
        prims = m.get("primitives", [])
        indices = np.asarray(m.get("indices", np.empty(0, dtype=np.uint32)), dtype="<u4")
        tangents = np.asarray(m.get("tangents", np.empty((0, 4), dtype=np.float32)), dtype="<f4")

        _pack_vec(w, len(verts), 12, verts.tobytes())
        _pack_vec(w, len(tverts), 8, tverts.tobytes())
        _pack_vec(w, len(tverts2), 8, tverts2.tobytes())
        _pack_vec(w, len(colors), 4, colors.tobytes())
        _pack_vec(w, len(norms), 12, norms.tobytes())
        _pack_vec(w, len(enc_norms), 1, enc_norms.tobytes())
        prim_bytes = b"".join(struct.pack("<2iI", p[0], p[1], p[2] & 0xFFFFFFFF) for p in prims)
        _pack_vec(w, len(prims), 12, prim_bytes)
        _pack_vec(w, len(indices), 4, indices.tobytes())
        _pack_vec(w, len(tangents), 16, tangents.tobytes())

        w.write_int(m.get("verts_per_frame", len(verts)))
        w.write_int(m.get("mesh_flags", 0))

        if mtype == 1:
            is_parent = parent_mesh < 0
            if is_parent:
                iv = np.asarray(m.get("initial_verts", verts), dtype="<f4")
                inm = np.asarray(m.get("initial_norms", norms), dtype="<f4")
                _pack_vec(w, len(iv), 12, iv.tobytes())
                _pack_vec(w, len(inm), 12, inm.tobytes())
            it = np.asarray(m.get("initial_transforms", np.empty((0, 4, 4), dtype=np.float32)), dtype="<f4")
            _pack_vec(w, len(it), 64, it.tobytes())
            if is_parent:
                vi = np.asarray(m.get("vertex_index", np.empty(0, dtype=np.int32)), dtype="<i4")
                bi = np.asarray(m.get("bone_index", np.empty(0, dtype=np.int32)), dtype="<i4")
                wt = np.asarray(m.get("weight", np.empty(0, dtype=np.float32)), dtype="<f4")
                ni = np.asarray(m.get("node_index", np.empty(0, dtype=np.int32)), dtype="<i4")
                _pack_vec(w, len(vi), 4, vi.tobytes())
                _pack_vec(w, len(bi), 4, bi.tobytes())
                _pack_vec(w, len(wt), 4, wt.tobytes())
                _pack_vec(w, len(ni), 4, ni.tobytes())

    # 3.5 Sequences
    w.write_int(num_sequences)
    for _ in range(num_sequences):
        w.write_int(0)
        w.write_int(0)
        w.write_int(1)
        w.write_float32(1.0)
        w.write_int(0)
        for _ in range(9):
            w.write_int(0)
        w.write_float32(0.0)
        for _ in range(6):
            w.write_bin(b"\x00" * 4)

    # 3.6 Material list
    if materials:
        w.write_int(len(materials))
        for mname in materials:
            w.write_str(mname)
            w.write_int(0)
            w.write_int(0)
            w.write_int(0)
            w.write_int(0)
            w.write_float32(1.0)
            w.write_float32(0.0)

    body = w.getvalue()
    hw = MsgPackWriter()
    hw.write({"compression": False, "bodysize": len(body)})
    header = hw.getvalue()
    ver = (shape_version & 0xFF) | ((exporter_version & 0xFFFF) << 16)
    return struct.pack("<II", ver, len(header)) + header + body


def _make_split_cube_mesh() -> dict:
    """Build a 24-vertex GPU-split unit cube (6 faces * 4 corners) with UV0, UV1, colors, normals."""
    faces = [
        ((0, 0, 1), [(0, 0, 1), (1, 0, 1), (1, 1, 1), (0, 1, 1)]),
        ((0, 0, -1), [(0, 1, 0), (1, 1, 0), (1, 0, 0), (0, 0, 0)]),
        ((0, -1, 0), [(0, 0, 0), (1, 0, 0), (1, 0, 1), (0, 0, 1)]),
        ((0, 1, 0), [(1, 1, 0), (0, 1, 0), (0, 1, 1), (1, 1, 1)]),
        ((1, 0, 0), [(1, 0, 0), (1, 1, 0), (1, 1, 1), (1, 0, 1)]),
        ((-1, 0, 0), [(0, 1, 0), (0, 0, 0), (0, 0, 1), (0, 1, 1)]),
    ]
    verts, norms, uvs, uv2s, cols, indices = [], [], [], [], [], []
    for fi, (n, quad) in enumerate(faces):
        base = len(verts)
        for ci, p in enumerate(quad):
            verts.append(p)
            norms.append(n)
            u = float(ci in (1, 2))
            v = float(ci in (2, 3))
            uvs.append((u, 1.0 - v))       # Torque3D flipped V
            uv2s.append((u * 0.5, 1.0 - v * 0.5))
            cols.append((255 if fi % 2 == 0 else 0, 128, 64, 255))
        # Torque3D CW front-face winding (reversed from Blender CCW)
        indices.extend([base + 2, base + 1, base, base + 3, base + 2, base])
    # Split across 2 materials (first 3 faces mat 0, last 3 faces mat 1)
    prims = [
        (0, 18, 0x20000000 | 0),
        (18, 18, 0x20000000 | 1),
    ]
    return {
        "type": 0,
        "verts": np.array(verts, dtype=np.float32),
        "tverts": np.array(uvs, dtype=np.float32),
        "tverts2": np.array(uv2s, dtype=np.float32),
        "colors": np.array(cols, dtype=np.uint8),
        "norms": np.array(norms, dtype=np.float32),
        "primitives": prims,
        "indices": np.array(indices, dtype=np.uint32),
    }


WORKER_SCRIPT = r'''
import json
import os
import sys

argv = sys.argv
if "--" in argv:
    argv = argv[argv.index("--") + 1:]
else:
    argv = argv[1:]

repo_root, cdae_path, out_json, opts_json = argv[0], argv[1], argv[2], argv[3]
sys.path.insert(0, repo_root)

import bpy
import bmesh
from mathutils import Vector
import io_scene_collada
from io_scene_collada.cdae.importer import CdaeImporter, CdaeImportSettings

bpy.ops.wm.read_factory_settings(use_empty=True)
if not hasattr(bpy.ops.import_scene, "beamng_cdae"):
    io_scene_collada.register()

opts = json.loads(opts_json)
settings = CdaeImportSettings(filepath=cdae_path, **opts)
importer = CdaeImporter(bpy.context, settings)
ok = importer.import_document()

meshes_out = []
for ob in bpy.data.objects:
    if ob.type == "MESH":
        me = ob.data
        bm = bmesh.new()
        bm.from_mesh(me)
        bm.free()
        still_invalid = bool(me.validate(verbose=False, clean_customdata=False))
        outward_ok = True
        if ob.name == "CubeObj":
            for poly in me.polygons:
                if poly.normal.dot(poly.center - Vector((0.5, 0.5, 0.5))) <= 0.0:
                    outward_ok = False
                    break
        sharp_attr = me.attributes.get("sharp_edge")
        n_sharp = 0
        if sharp_attr is not None:
            n_sharp = sum(1 for d in sharp_attr.data if d.value)
        uv_names = [u.name for u in me.uv_layers]
        col_names = [c.name for c in me.color_attributes]
        mat_names = [m.name for m in me.materials if m is not None]
        vgroups = [vg.name for vg in ob.vertex_groups]
        mods = [m.type for m in ob.modifiers]
        meshes_out.append({
            "name": ob.name,
            "verts": len(me.vertices),
            "polys": len(me.polygons),
            "sharp_edges": n_sharp,
            "has_custom_normals": me.attributes.get("custom_normal") is not None,
            "uv_layers": uv_names,
            "color_attrs": col_names,
            "materials": mat_names,
            "vertex_groups": vgroups,
            "modifiers": mods,
            "still_invalid": still_invalid,
            "outward_ok": outward_ok,
        })

result = {
    "ok": bool(ok),
    "errors": list(importer.errors),
    "warnings": list(importer.warnings),
    "infos": list(importer.infos),
    "collections": sorted(c.name for c in bpy.data.collections),
    "armatures": [ob.name for ob in bpy.data.objects if ob.type == "ARMATURE"],
    "bones": {
        b.name: {
            "head": [float(x) for x in b.head_local],
            "mat3": [[float(v) for v in row] for row in b.matrix_local.to_3x3()],
        }
        for ob in bpy.data.objects
        if ob.type == "ARMATURE"
        for b in ob.data.bones
    },
    "empties": [
        {"name": ob.name, "display_size": float(ob.empty_display_size)}
        for ob in bpy.data.objects
        if ob.type == "EMPTY"
    ],
    "meshes": meshes_out,
}
with open(out_json, "w", encoding="utf-8") as f:
    json.dump(result, f)
'''


POS_CHECK_WORKER_SCRIPT = r'''
import collections
import re
import sys

argv = sys.argv
if "--" in argv:
    argv = argv[argv.index("--") + 1:]
else:
    argv = argv[1:]

repo_root = argv[0]
cdae_paths = argv[1:]
sys.path.insert(0, repo_root)

import bpy
import numpy as np
import io_scene_collada
from io_scene_collada.cdae.importer import (
    CdaeImporter,
    CdaeImportSettings,
    _primitives_to_triangles,
    _safe_name,
)
from io_scene_collada.cdae.reader import MESH_TYPE_NULL, MESH_TYPE_SKIN, read_cdae_file

io_scene_collada.register()


def _max_tri_edge(tri_pos: np.ndarray) -> float:
    if len(tri_pos) == 0:
        return 0.0
    e01 = np.linalg.norm(tri_pos[:, 1] - tri_pos[:, 0], axis=1)
    e12 = np.linalg.norm(tri_pos[:, 2] - tri_pos[:, 1], axis=1)
    e20 = np.linalg.norm(tri_pos[:, 0] - tri_pos[:, 2], axis=1)
    return float(max(e01.max(), e12.max(), e20.max()))


def _tri_keys_and_map(tri_pos: np.ndarray, quant_tol: float):
    q = np.round(tri_pos.astype(np.float64) / quant_tol).astype(np.int64)
    ctr = collections.Counter()
    by_key = collections.defaultdict(list)
    for idx in range(len(q)):
        p0 = (int(q[idx, 0, 0]), int(q[idx, 0, 1]), int(q[idx, 0, 2]))
        p1 = (int(q[idx, 1, 0]), int(q[idx, 1, 1]), int(q[idx, 1, 2]))
        p2 = (int(q[idx, 2, 0]), int(q[idx, 2, 1]), int(q[idx, 2, 2]))
        # Cyclic rotation preserving triangle winding
        if p0 <= p1 and p0 <= p2:
            key = (p0, p1, p2)
        elif p1 <= p0 and p1 <= p2:
            key = (p1, p2, p0)
        else:
            key = (p2, p0, p1)
        ctr[key] += 1
        by_key[key].append(idx)
    return ctr, by_key


def _cyclic_corner_dist(t_a: np.ndarray, t_b: np.ndarray) -> float:
    best = 1e9
    for shift in range(3):
        b_rot = np.roll(t_b, -shift, axis=0)
        d = float(np.linalg.norm(t_a - b_rot, axis=1).max())
        if d < best:
            best = d
    return best


def _verify_file(cdae_path: str) -> None:
    shape = read_cdae_file(cdae_path)
    for normals_mode in ("EXACT", "SHARP", "UNMERGED"):
        bpy.ops.wm.read_factory_settings(use_empty=True)
        settings = CdaeImportSettings(
            filepath=cdae_path,
            lod_mode="ALL",
            import_collision=True,
            normals_mode=normals_mode,
        )
        importer = CdaeImporter(bpy.context, settings)
        ok = importer.import_document()
        assert ok, f"{cdae_path} ({normals_mode}) import failed: {importer.errors}"

        # Every created Empty must have a reasonable model-relative display size (never 1.0 m default)
        for ob in bpy.data.objects:
            if ob.type == "EMPTY":
                assert 0.009 <= ob.empty_display_size <= 0.101, (
                    f"{cdae_path} ({normals_mode}): Empty '{ob.name}' has display_size={ob.empty_display_size}"
                )

        # Also verify rest-pose armature evaluation matches initialVerts/initialNorms for SkinMeshes
        skin_meshes = [m for m in shape.meshes if m.mesh_type == MESH_TYPE_SKIN]
        if skin_meshes and normals_mode == "UNMERGED":
            deps = bpy.context.evaluated_depsgraph_get()
            sm_obs = [ob for ob in bpy.data.objects if ob.type == "MESH" and any(m.type == "ARMATURE" for m in ob.modifiers)]
            for ob, sm in zip(sm_obs, skin_meshes):
                iv = sm.initial_verts if len(sm.initial_verts) > 0 else sm.verts
                inorm = sm.initial_norms if len(sm.initial_norms) > 0 else sm.norms
                if len(iv) == 0:
                    continue
                ev_ob = ob.evaluated_get(deps)
                ev_me = ev_ob.to_mesh()
                try:
                    ev_co = np.empty(len(ev_me.vertices) * 3, dtype=np.float32)
                    ev_me.vertices.foreach_get("co", ev_co)
                    ev_co = ev_co.reshape(-1, 3)
                    v_err = float(np.linalg.norm(ev_co - iv[:len(ev_co)], axis=1).max())
                    assert v_err <= 1e-4, f"{cdae_path}: skinned rest-pose vert error {v_err:.6f} > 1e-4"
                    if len(inorm) >= len(ev_co) and len(ev_me.loops) > 0:
                        ev_cn = np.empty(len(ev_me.loops) * 3, dtype=np.float32)
                        ev_me.corner_normals.foreach_get("vector", ev_cn)
                        ev_cn = ev_cn.reshape(-1, 3)
                        lv_s = np.empty(len(ev_me.loops), dtype=np.int32)
                        ev_me.loops.foreach_get("vertex_index", lv_s)
                        sn = inorm[lv_s].astype(np.float64)
                        sn /= np.maximum(np.linalg.norm(sn, axis=1, keepdims=True), 1e-12)
                        en = ev_cn.astype(np.float64)
                        en /= np.maximum(np.linalg.norm(en, axis=1, keepdims=True), 1e-12)
                        max_ndeg = float(np.degrees(np.arccos(np.clip(np.einsum("ij,ij->i", en, sn), -1.0, 1.0))).max())
                        assert max_ndeg <= 0.1, f"{cdae_path}: skinned rest-pose normal error {max_ndeg:.4f}° > 0.1°"
                finally:
                    ev_ob.to_mesh_clear()

        # Parse reported exact duplicate and degenerate removals per mesh name
        reported_dups = collections.Counter()
        reported_degen = collections.Counter()
        for info in importer.infos:
            m = re.match(
                r"^Mesh '(.+)': \d+ double-sided and \d+ layered faces kept unwelded, (\d+) exact duplicates removed$",
                info,
            )
            if m:
                reported_dups[m.group(1)] += int(m.group(2))
            m2 = re.match(r"^Mesh '(.+)': (\d+) tiny faces \(under [0-9.eE+-]+ mm\) merged away$", info)
            if m2:
                reported_degen[m2.group(1)] += int(m2.group(2))

        selected_details = importer._select_details(shape)
        # Collect expected non-null (obj_name, c_mesh) per detail collection in exact creation order
        detail_colls = list(bpy.context.scene.collection.children)
        expected_groups = []
        if selected_details:
            assert len(detail_colls) == len(selected_details), (
                f"{cdae_path}: expected {len(selected_details)} collections, got {len(detail_colls)}"
            )
            for coll, (_det_idx, det) in zip(detail_colls, selected_details):
                sub_idx = int(det["sub_shape_num"])
                obj_det = int(det["object_detail_num"])
                if sub_idx < 0 or sub_idx >= len(shape.sub_shape_first_object):
                    continue
                first_obj = int(shape.sub_shape_first_object[sub_idx])
                num_objs = int(shape.sub_shape_num_objects[sub_idx])
                jobs = []
                for obj_i in range(first_obj, min(first_obj + num_objs, len(shape.objects))):
                    obj = shape.objects[obj_i]
                    if 0 <= obj_det < int(obj["num_meshes"]):
                        mi = int(obj["start_mesh_index"]) + obj_det
                        if 0 <= mi < len(shape.meshes) and shape.meshes[mi].mesh_type != MESH_TYPE_NULL:
                            obj_name = _safe_name(shape.names, int(obj["name_index"]), f"Object_{obj_i}")
                            jobs.append((obj_name, shape.meshes[mi]))
                expected_groups.append((coll, jobs))
        else:
            jobs = []
            for obj_i in range(len(shape.objects)):
                obj = shape.objects[obj_i]
                if int(obj["num_meshes"]) > 0:
                    mi = int(obj["start_mesh_index"])
                    if 0 <= mi < len(shape.meshes) and shape.meshes[mi].mesh_type != MESH_TYPE_NULL:
                        obj_name = _safe_name(shape.names, int(obj["name_index"]), f"Object_{obj_i}")
                        jobs.append((obj_name, shape.meshes[mi]))
            expected_groups.append((bpy.context.scene.collection, jobs))

        dropped_by_name = collections.Counter()
        quant_tol = 1e-5 if normals_mode == "UNMERGED" else 1e-4
        edge_tol = 1e-5 if normals_mode == "UNMERGED" else 1e-4
        merge_tol = 2.5 * settings.merge_distance + 1e-6

        for coll, jobs in expected_groups:
            mesh_obs = [ob for ob in coll.objects if ob.type == "MESH"]
            # Filter jobs whose raw source mesh actually produces >= 1 triangle
            non_empty_jobs = []
            for obj_name, c_mesh in jobs:
                verts_src = c_mesh.verts
                if c_mesh.mesh_type == MESH_TYPE_SKIN and len(verts_src) == 0 and len(c_mesh.initial_verts) > 0:
                    verts_src = c_mesh.initial_verts
                if len(verts_src) == 0:
                    continue
                n_v = len(verts_src)
                if c_mesh.verts_per_frame > 0 and c_mesh.verts_per_frame < n_v:
                    n_v = c_mesh.verts_per_frame
                verts = verts_src[:n_v].astype(np.float32, copy=True)
                if not np.isfinite(verts).all():
                    verts = np.where(np.isfinite(verts), verts, np.float32(0.0))
                raw_tris, _ = _primitives_to_triangles(c_mesh, n_v, [], obj_name)
                if len(raw_tris) > 0:
                    non_empty_jobs.append((obj_name, verts[raw_tris]))

            assert len(mesh_obs) == len(non_empty_jobs), (
                f"{cdae_path} ({normals_mode}, coll={coll.name}): "
                f"imported {len(mesh_obs)} meshes != {len(non_empty_jobs)} source meshes"
            )

            for ob, (obj_name, src_tri_pos) in zip(mesh_obs, non_empty_jobs):
                me = ob.data
                n_p = len(me.polygons)
                assert n_p > 0, f"{cdae_path} ({normals_mode}): '{ob.name}' has 0 polys"
                v_co = np.empty(len(me.vertices) * 3, dtype=np.float32)
                me.vertices.foreach_get("co", v_co)
                v_co = v_co.reshape(-1, 3)

                l_tot = np.empty(n_p, dtype=np.int32)
                me.polygons.foreach_get("loop_total", l_tot)
                assert (l_tot == 3).all(), f"{cdae_path} ({normals_mode}): non-triangle poly in '{ob.name}'"
                l_verts = np.empty(n_p * 3, dtype=np.int32)
                me.loops.foreach_get("vertex_index", l_verts)
                imp_tri_pos = v_co[l_verts.reshape(n_p, 3)]

                # 1. Longest edge invariant (imported mesh must never exceed source longest edge)
                src_max_e = _max_tri_edge(src_tri_pos)
                imp_max_e = _max_tri_edge(imp_tri_pos)
                assert imp_max_e <= src_max_e + edge_tol, (
                    f"{cdae_path} ({normals_mode}): '{ob.name}' longest edge {imp_max_e:.6f} "
                    f"> source {src_max_e:.6f}"
                )

                # 2. Positional triangle multiset invariant (winding-preserving)
                src_ctr, src_by_key = _tri_keys_and_map(src_tri_pos, quant_tol)
                imp_ctr, imp_by_key = _tri_keys_and_map(imp_tri_pos, quant_tol)
                src_diff = src_ctr - imp_ctr
                imp_diff = imp_ctr - src_ctr

                src_rem_idx = []
                for k, cnt in src_diff.items():
                    src_rem_idx.extend(src_by_key[k][:cnt])
                imp_rem_idx = []
                for k, cnt in imp_diff.items():
                    imp_rem_idx.extend(imp_by_key[k][:cnt])

                # Handle boundary-straddling vertices in merged modes by exact cyclic Euclidean distance
                if imp_rem_idx and normals_mode != "UNMERGED":
                    still_imp = []
                    used_src = set()
                    # Fast centroid prefilter before exact cyclic corner check
                    src_rem_arr = src_tri_pos[src_rem_idx]
                    src_cent = src_rem_arr.mean(axis=1)
                    for ii in imp_rem_idx:
                        t_i = imp_tri_pos[ii]
                        c_i = t_i.mean(axis=0)
                        dists = np.linalg.norm(src_cent - c_i, axis=1)
                        cands = np.flatnonzero(dists <= merge_tol * 2.0)
                        matched = False
                        for ci in cands.tolist():
                            if ci in used_src:
                                continue
                            if _cyclic_corner_dist(t_i, src_rem_arr[ci]) <= merge_tol:
                                used_src.add(ci)
                                matched = True
                                break
                        if not matched:
                            still_imp.append(ii)
                    imp_rem_idx = still_imp
                    src_rem_idx = [src_rem_idx[j] for j in range(len(src_rem_idx)) if j not in used_src]

                assert len(imp_rem_idx) == 0, (
                    f"{cdae_path} ({normals_mode}): '{ob.name}' has {len(imp_rem_idx)} imported "
                    f"triangle(s) not matching any raw .cdae triangle!"
                )
                dropped_by_name[obj_name] += len(src_rem_idx)

        all_names = set(dropped_by_name) | set(reported_dups) | set(reported_degen)
        for name in sorted(all_names):
            expected_drop = reported_dups[name] + reported_degen[name]
            got_drop = dropped_by_name[name]
            assert got_drop == expected_drop, (
                f"{cdae_path} ({normals_mode}): '{name}' dropped {got_drop} tris "
                f"!= reported {reported_dups[name]} exact dups + {reported_degen[name]} degenerates"
            )


for fp in cdae_paths:
    _verify_file(fp)
print(f"POS_TRI_CHECK_OK {len(cdae_paths)}")
'''


def _run_import(runner: list[str], worker_path: str, cdae_path: str, opts: dict | None = None, workdir: str | None = None) -> dict:
    base_dir = workdir or os.path.dirname(cdae_path)
    out_json = os.path.join(base_dir, os.path.basename(cdae_path) + ".out.json")
    opts_str = json.dumps(opts or {})
    if runner[0] == sys.executable:
        cmd = [runner[0], worker_path, REPO_ROOT, cdae_path, out_json, opts_str]
    else:
        cmd = [*runner, worker_path, "--", REPO_ROOT, cdae_path, out_json, opts_str]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise AssertionError(
            f"Subprocess crashed (rc={proc.returncode}) on {os.path.basename(cdae_path)}\n"
            f"STDERR:\n{proc.stderr[-1500:]}"
        )
    if "Traceback (most recent call last)" in proc.stderr or "Traceback (most recent call last)" in proc.stdout:
        raise AssertionError(
            f"Unexpected traceback on {os.path.basename(cdae_path)}\n"
            f"STDERR:\n{proc.stderr[-1500:]}"
        )
    with open(out_json, "r", encoding="utf-8") as f:
        data = json.load(f)
    for m in data["meshes"]:
        assert not m["still_invalid"], f"Mesh '{m['name']}' failed me.validate()"
    return data


def _verify_positional_triangles(runner: list[str], pos_worker_path: str, cdae_paths: list[str]) -> None:
    if runner[0] == sys.executable:
        cmd = [runner[0], pos_worker_path, REPO_ROOT, *cdae_paths]
    else:
        cmd = [*runner, pos_worker_path, "--", REPO_ROOT, *cdae_paths]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if proc.returncode != 0 or "Traceback (most recent call last)" in proc.stderr:
        raise AssertionError(
            f"Positional triangle check failed (rc={proc.returncode}):\n"
            f"STDOUT:\n{proc.stdout[-1500:]}\nSTDERR:\n{proc.stderr[-1500:]}"
        )
    assert f"POS_TRI_CHECK_OK {len(cdae_paths)}" in proc.stdout, proc.stdout[-1000:]


def main() -> int:
    print("Running BeamNG .cdae v31 import test suite...")
    test_msgpack_lite_unit()

    runner = _find_runner()
    workdir = tempfile.mkdtemp(prefix="collada_cdae_")
    try:
        worker_path = os.path.join(workdir, "_cdae_worker.py")
        with open(worker_path, "w", encoding="utf-8") as f:
            f.write(WORKER_SCRIPT)
        pos_worker_path = os.path.join(workdir, "_pos_tri_worker.py")
        with open(pos_worker_path, "w", encoding="utf-8") as f:
            f.write(POS_CHECK_WORKER_SCRIPT)

        # Case 1: Split cube -> merge_by_distance (24 -> 8 verts) + UV0/UV1/Color + 12 sharp edges + 2 materials
        c1 = os.path.join(workdir, "split_cube.cdae")
        with open(c1, "wb") as f:
            f.write(build_synthetic_cdae(
                names=["Root", "CubeObj", "detail2"],
                meshes=[_make_split_cube_mesh()],
                materials=["paint_main", "chrome_trim"],
            ))
        r1 = _run_import(runner, worker_path, c1)
        assert r1["ok"], r1["errors"]
        assert len(r1["meshes"]) == 1
        m1 = r1["meshes"][0]
        assert m1["verts"] == 8 and m1["polys"] == 12, m1
        assert m1["outward_ok"], f"CubeObj face winding is inside-out: {m1}"
        assert m1["has_custom_normals"], f"Expected custom_normal in default EXACT mode: {m1}"
        assert m1["sharp_edges"] == 12, f"Expected 12 sharp edges on merged cube, got {m1}"
        assert m1["uv_layers"] == ["UVMap", "UVMap.001"], m1
        assert m1["color_attrs"] == ["Color"], m1
        assert m1["materials"] == ["paint_main", "chrome_trim"], m1
        # Verify SHARP (merge + sharp edges only) and UNMERGED (raw GPU 24 verts + custom normals) modes
        r1_sharp = _run_import(runner, worker_path, c1, {"normals_mode": "SHARP"})
        assert r1_sharp["ok"] and r1_sharp["meshes"][0]["verts"] == 8
        assert not r1_sharp["meshes"][0]["has_custom_normals"] and r1_sharp["meshes"][0]["sharp_edges"] == 12
        r1_unm = _run_import(runner, worker_path, c1, {"normals_mode": "UNMERGED"})
        assert r1_unm["ok"] and r1_unm["meshes"][0]["verts"] == 24 and r1_unm["meshes"][0]["has_custom_normals"]
        print("  PASS split_cube_merge_sharp_uv_color_mats")

        # Case 2: Strip + Fan + encodedNorms + parentMesh >= 0 sharing
        quad_v = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0], [1, 1, 0]], dtype=np.float32)
        parent_m = {
            "type": 0,
            "verts": quad_v,
            "encoded_norms": np.array([0, 0, 0, 0], dtype=np.uint8),
            "primitives": [(0, 4, 0x40000000 | 0x20000000 | 0)],  # Indexed Strip -> 2 tris
            "indices": np.array([0, 1, 2, 3], dtype=np.uint32),
        }
        child_m = {
            "type": 0,
            "parent_mesh": 0,  # inherits verts & encoded_norms from mesh 0
            "verts": np.empty((0, 3), dtype=np.float32),
            "primitives": [(0, 4, 0x80000000 | 0)],  # Non-indexed Fan -> 2 tris
            "indices": np.empty(0, dtype=np.uint32),
        }
        c2 = os.path.join(workdir, "parent_strip_fan.cdae")
        with open(c2, "wb") as f:
            f.write(build_synthetic_cdae(
                names=["Root", "ParentObj", "ChildObj", "detail100", "detail50", "collision-1"],
                objects=[
                    (1, 2, 0, 0, -1, -1),  # detail 0 -> mesh 0, detail 1 -> mesh 1 (child!)
                ],
                details=[
                    (3, 0, 0, 100.0, -1.0, -1.0, 2, 0, 0, 0, 0, 0.0, 0),
                    (4, 0, 1, 50.0, -1.0, -1.0, 2, 0, 0, 0, 0, 0.0, 0),
                    (5, 0, 1, -1.0, -1.0, -1.0, 2, 0, 0, 0, 0, 0.0, 0),
                ],
                meshes=[parent_m, child_m],
                materials=["mat_a"],
            ))
        r2_all = _run_import(runner, worker_path, c2, {"lod_mode": "ALL", "import_collision": True})
        assert r2_all["ok"], r2_all["errors"]
        assert set(r2_all["collections"]) == {"detail100", "detail50", "collision-1"}, r2_all["collections"]
        assert len(r2_all["meshes"]) == 3 and all(m["polys"] == 2 for m in r2_all["meshes"]), r2_all["meshes"]
        # Multi-LOD node must get an Empty with model-relative display size (never 1.0 m default)
        assert len(r2_all["empties"]) == 1 and 0.009 <= r2_all["empties"][0]["display_size"] <= 0.1, r2_all["empties"]
        r2_hi = _run_import(runner, worker_path, c2, {"lod_mode": "HIGHEST", "import_collision": False})
        assert r2_hi["ok"] and len(r2_hi["meshes"]) == 1, r2_hi
        # Single-LOD 1-to-1 node+mesh (node_name == obj_name) must produce 0 Empties in BOTH HIGHEST and ALL modes
        c2_single = os.path.join(workdir, "single_lod_direct.cdae")
        with open(c2_single, "wb") as f:
            f.write(build_synthetic_cdae(
                names=["CubeObj", "detail2"],
                nodes=[(0, -1, -1, -1, -1)],
                objects=[(0, 1, 0, 0, -1, -1)],
                details=[(1, 0, 0, 2.0, -1.0, -1.0, 12, 0, 0, 0, 0, 0.0, 0)],
                meshes=[_make_split_cube_mesh()],
                materials=["paint_main", "chrome_trim"],
            ))
        for lm in ("HIGHEST", "ALL"):
            r_dir = _run_import(runner, worker_path, c2_single, {"lod_mode": lm})
            assert r_dir["ok"] and len(r_dir["empties"]) == 0 and len(r_dir["meshes"]) == 1, (lm, r_dir)
        print("  PASS parentMesh_sharing_strip_fan_lod_collision")

        # Case 3: SkinMesh with non-identity row-major initialTransforms bind pose + roll + skipped sequence info
        # Bone 1 bind pose = 90 deg rotation around Y + translation (0.5, 1.5, 0.25), distinct from default_translations
        bind1 = np.array(
            [
                [0.0, 0.0, 1.0, 0.5],
                [0.0, 1.0, 0.0, 1.5],
                [-1.0, 0.0, 0.0, 0.25],
                [0.0, 0.0, 0.0, 1.0],
            ],
            dtype=np.float32,
        )
        inv_bind1 = np.linalg.inv(bind1).astype(np.float32)
        skin_m = {
            "type": 1,
            "verts": quad_v,
            "norms": np.tile([0.0, 0.0, 1.0], (4, 1)).astype(np.float32),
            "primitives": [(0, 6, 0x20000000 | 0)],
            "indices": np.array([0, 1, 3, 0, 3, 2], dtype=np.uint32),
            "initial_transforms": np.array([np.eye(4, dtype=np.float32), inv_bind1]),
            "vertex_index": np.array([0, 1, 2, 3], dtype=np.int32),
            "bone_index": np.array([0, 0, 1, 1], dtype=np.int32),
            "weight": np.array([1.0, 1.0, 1.0, 1.0], dtype=np.float32),
            "node_index": np.array([0, 1], dtype=np.int32),
        }
        c3 = os.path.join(workdir, "skin_mesh.cdae")
        with open(c3, "wb") as f:
            f.write(build_synthetic_cdae(
                names=["BoneRoot", "BoneTip", "SkinObj", "detail2"],
                nodes=[(0, -1, -1, 1, -1), (1, 0, -1, -1, -1)],
                default_translations=[(0.0, 0.0, 0.0), (0.0, 1.0, 0.0)],
                objects=[(2, 1, 0, 0, -1, -1)],
                details=[(3, 0, 0, 2.0, -1.0, -1.0, 2, 0, 0, 0, 0, 0.0, 0)],
                meshes=[skin_m],
                materials=["skin_mat"],
                num_sequences=2,
            ))
        r3 = _run_import(runner, worker_path, c3)
        assert r3["ok"], r3["errors"]
        assert len(r3["armatures"]) == 1, r3
        assert r3["meshes"][0]["modifiers"] == ["ARMATURE"], r3["meshes"]
        assert set(r3["meshes"][0]["vertex_groups"]) == {"BoneRoot", "BoneTip"}, r3["meshes"]
        assert any("2 animation sequence" in inf for inf in r3["infos"]), r3["infos"]
        # Verify BoneTip uses initialTransforms bind pose translation & roll (not default_translations)
        btip = r3["bones"]["BoneTip"]
        assert np.allclose(btip["head"], [0.5, 1.5, 0.25], atol=1e-4), btip
        assert np.allclose(btip["mat3"], bind1[:3, :3], atol=1e-4), btip
        print("  PASS skin_mesh_armature_bind_pose_and_sequence_info")

        # Case 3b: KD-tree merge across grid boundary (0.000004 vs 0.000006 -> dist 2e-6 <= 1e-5, straddles 0.000005)
        gb_verts = np.array(
            [
                [0.000004, 0.0, 0.0], [1.0, 0.0, 0.0], [0.000004, 1.0, 0.0],
                [0.000006, 0.0, 0.0], [0.000006, 1.0, 0.0], [-1.0, 0.0, 0.0],
            ],
            dtype=np.float32,
        )
        gb_norms = np.tile([0.0, 0.0, 1.0], (6, 1)).astype(np.float32)
        gb_mesh = {
            "type": 0,
            "verts": gb_verts,
            "norms": gb_norms,
            "primitives": [(0, 6, 0x20000000 | 0)],
            "indices": np.array([0, 2, 1, 3, 5, 4], dtype=np.uint32),
        }
        c_gb = os.path.join(workdir, "grid_boundary_merge.cdae")
        with open(c_gb, "wb") as f:
            f.write(build_synthetic_cdae(
                names=["GridObj", "detail2"],
                nodes=[(0, -1, -1, -1, -1)],
                objects=[(0, 1, 0, 0, -1, -1)],
                details=[(1, 0, 0, 2.0, -1.0, -1.0, 2, 0, 0, 0, 0, 0.0, 0)],
                meshes=[gb_mesh],
                materials=["mat_a"],
            ))
        r_gb = _run_import(runner, worker_path, c_gb, {"normals_mode": "EXACT", "merge_distance": 1e-5})
        assert r_gb["ok"] and r_gb["meshes"][0]["verts"] == 4 and r_gb["meshes"][0]["polys"] == 2, r_gb
        # Verify KD-tree merge does NOT transitively chain (v0=0, v1=0.8e-5, v2=1.6e-5 with tol=1e-5:
        # v0 & v1 merge, v2 stays separate because ||v2 - v0|| = 1.6e-5 > 1e-5)
        ch_verts = np.array(
            [
                [0.0, 0.0, 0.0], [0.0, 1.0, 0.0], [1.0, 0.0, 0.0],
                [0.8e-5, 0.0, 0.0], [1.6e-5, 0.0, 0.0], [1.0, 1.0, 0.0],
            ],
            dtype=np.float32,
        )
        ch_mesh = {
            "type": 0,
            "verts": ch_verts,
            "norms": np.tile([0.0, 0.0, 1.0], (6, 1)).astype(np.float32),
            "primitives": [(0, 6, 0x20000000 | 0)],
            "indices": np.array([0, 2, 1, 3, 5, 4], dtype=np.uint32),
        }
        c_ch = os.path.join(workdir, "kdtree_no_chain.cdae")
        with open(c_ch, "wb") as f:
            f.write(build_synthetic_cdae(
                names=["ChainObj", "detail2"],
                nodes=[(0, -1, -1, -1, -1)],
                objects=[(0, 1, 0, 0, -1, -1)],
                details=[(1, 0, 0, 2.0, -1.0, -1.0, 2, 0, 0, 0, 0, 0.0, 0)],
                meshes=[ch_mesh],
                materials=["mat_a"],
            ))
        r_ch = _run_import(runner, worker_path, c_ch, {"normals_mode": "EXACT", "merge_distance": 1e-5})
        assert r_ch["ok"] and r_ch["meshes"][0]["verts"] == 5 and r_ch["meshes"][0]["polys"] == 2, r_ch
        print("  PASS kdtree_merge_across_grid_boundary_no_chaining")

        # Case 4b: Double-sided quad (opposite winding kept unwelded) + exact-duplicate triangle (dropped)
        # Front quad: 2 tris (0,1,2) & (0,2,3); back quad: 2 tris (4,6,5) & (4,7,6) at identical positions;
        # plus 1 exact same-winding duplicate of front tri (0,1,2) using verts (8,9,10).
        ds_verts = np.array(
            [
                [0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0],  # front
                [0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0],  # back
                [0, 0, 0], [1, 0, 0], [1, 1, 0],             # exact dup of first front tri
            ],
            dtype=np.float32,
        )
        ds_norms = np.array(
            [[0, 0, 1]] * 4 + [[0, 0, -1]] * 4 + [[0, 0, 1]] * 3,
            dtype=np.float32,
        )
        ds_uvs = np.array(
            [
                [0, 1], [1, 1], [1, 0], [0, 0],
                [0, 1], [1, 1], [1, 0], [0, 0],
                [0, 1], [1, 1], [1, 0],
            ],
            dtype=np.float32,
        )
        ds_cols = np.full((11, 4), 255, dtype=np.uint8)
        ds_mesh = {
            "type": 0,
            "verts": ds_verts,
            "tverts": ds_uvs,
            "colors": ds_cols,
            "norms": ds_norms,
            "primitives": [(0, 15, 0x20000000 | 0)],
            "indices": np.array([0, 1, 2, 0, 2, 3, 4, 6, 5, 4, 7, 6, 8, 9, 10], dtype=np.uint32),
        }
        c_ds = os.path.join(workdir, "double_sided_and_exact_dup.cdae")
        with open(c_ds, "wb") as f:
            f.write(build_synthetic_cdae(
                names=["Root", "GlassObj", "detail2"],
                meshes=[ds_mesh],
                materials=["glass_mat"],
            ))
        r_ds = _run_import(runner, worker_path, c_ds, workdir=workdir)
        assert r_ds["ok"], r_ds["errors"]
        assert not r_ds["warnings"], f"Expected 0 repair warnings on double-sided mesh, got {r_ds['warnings']}"
        m_ds = r_ds["meshes"][0]
        assert m_ds["polys"] == 4, f"Expected 4 polys (2 front + 2 back, 1 exact dup dropped), got {m_ds}"
        assert m_ds["verts"] == 8, f"Expected 8 verts (4 front + 4 unwelded back), got {m_ds}"
        assert m_ds["uv_layers"] == ["UVMap"] and m_ds["color_attrs"] == ["Color"], m_ds
        assert any("2 double-sided and 0 layered faces kept unwelded, 1 exact duplicates removed" in inf for inf in r_ds["infos"]), r_ds["infos"]
        print("  PASS double_sided_kept_and_exact_dup_removed")

        # Case 4c: Overlay / decal quad with identical geometry+winding but different material & UVs -> BOTH must survive
        ov_verts = np.array(
            [
                [0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0],  # base quad
                [0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0],  # decal overlay quad (same winding!)
            ],
            dtype=np.float32,
        )
        ov_norms = np.array([[0, 0, 1]] * 8, dtype=np.float32)
        ov_uvs = np.array(
            [
                [0.0, 1.0], [1.0, 1.0], [1.0, 0.0], [0.0, 0.0],
                [0.2, 0.8], [0.8, 0.8], [0.8, 0.2], [0.2, 0.2],
            ],
            dtype=np.float32,
        )
        ov_mesh = {
            "type": 0,
            "verts": ov_verts,
            "tverts": ov_uvs,
            "norms": ov_norms,
            "primitives": [
                (0, 6, 0x20000000 | 0),  # base_paint
                (6, 6, 0x20000000 | 1),  # decal_logo
            ],
            "indices": np.array([0, 1, 2, 0, 2, 3, 4, 5, 6, 4, 6, 7], dtype=np.uint32),
        }
        c_ov = os.path.join(workdir, "overlay_decal_quad.cdae")
        with open(c_ov, "wb") as f:
            f.write(build_synthetic_cdae(
                names=["Root", "DecalObj", "detail2"],
                meshes=[ov_mesh],
                materials=["base_paint", "decal_logo"],
            ))
        r_ov = _run_import(runner, worker_path, c_ov, workdir=workdir)
        assert r_ov["ok"], r_ov["errors"]
        assert not r_ov["warnings"], f"Expected 0 repair warnings on overlay mesh, got {r_ov['warnings']}"
        m_ov = r_ov["meshes"][0]
        assert m_ov["polys"] == 4, f"Expected all 4 polys (2 base + 2 overlay) to survive, got {m_ov}"
        assert m_ov["verts"] == 8, f"Expected 8 verts (4 base + 4 unwelded overlay), got {m_ov}"
        assert set(m_ov["materials"]) == {"base_paint", "decal_logo"}, m_ov
        assert any("0 double-sided and 2 layered faces kept unwelded, 0 exact duplicates removed" in inf for inf in r_ov["infos"]), r_ov["infos"]
        print("  PASS overlay_decal_quad_survives")

        # Positional triangle & longest-edge check across all synthetic fixtures, all details, all 3 Normals modes
        _verify_positional_triangles(runner, pos_worker_path, [c1, c2, c2_single, c3, c_ds, c_ov])
        print("  PASS positional_triangles_and_max_edge_all_3_modes_synthetic")

        # Case 4: Unsupported version (24) -> clear error naming version 24
        c4 = os.path.join(workdir, "bad_version.cdae")
        with open(c4, "wb") as f:
            f.write(build_synthetic_cdae(shape_version=24, meshes=[_make_split_cube_mesh()]))
        r4 = _run_import(runner, worker_path, c4)
        assert not r4["ok"] and any("24" in e for e in r4["errors"]), r4
        print("  PASS unsupported_version_rejected")

        # Case 5: Strict elemSize mismatch -> clear error
        c5 = os.path.join(workdir, "bad_elemsize.cdae")
        with open(c5, "wb") as f:
            f.write(build_synthetic_cdae(nodes_elem_size_override=16, meshes=[_make_split_cube_mesh()]))
        r5 = _run_import(runner, worker_path, c5)
        assert not r5["ok"] and any("elemSize" in e for e in r5["errors"]), r5
        print("  PASS strict_elemsize_mismatch_rejected")

        # Case 5b: Unsupported mesh type (SortedMesh = 3) -> clear error (never silently read as StandardMesh)
        sorted_m = dict(_make_split_cube_mesh())
        sorted_m["type"] = 3
        c5b = os.path.join(workdir, "sorted_mesh_type3.cdae")
        with open(c5b, "wb") as f:
            f.write(build_synthetic_cdae(meshes=[sorted_m]))
        r5b = _run_import(runner, worker_path, c5b)
        assert not r5b["ok"] and any("SortedMesh" in e and "3" in e for e in r5b["errors"]), r5b
        print("  PASS unsupported_mesh_type_rejected")

        # Case 5c: Active collection linking for both .dae and .cdae (HIGHEST and ALL modes)
        coll_worker = os.path.join(workdir, "_active_coll_worker.py")
        with open(coll_worker, "w", encoding="utf-8") as f:
            f.write(r'''
import os, sys
argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else sys.argv[1:]
repo_root, cdae_single, cdae_multi, workdir = argv[0], argv[1], argv[2], argv[3]
sys.path.insert(0, repo_root)
import bpy, io_scene_collada
io_scene_collada.register()

def _setup_active_car():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    car = bpy.data.collections.new("Car")
    bpy.context.scene.collection.children.link(car)
    lc = bpy.context.view_layer.layer_collection.children["Car"]
    bpy.context.view_layer.active_layer_collection = lc
    return car

# 1. Export a tiny .dae then import into active collection "Car"
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.mesh.primitive_cube_add()
dae_fp = os.path.join(workdir, "coll_test.dae")
assert bpy.ops.wm.collada_export(filepath=dae_fp) == {"FINISHED"}

car = _setup_active_car()
assert bpy.ops.wm.collada_import(filepath=dae_fp) == {"FINISHED"}
assert len(bpy.context.scene.collection.objects) == 0, list(bpy.context.scene.collection.objects)
assert len(car.objects) == 1, list(car.objects)

# 2. Import single-LOD .cdae (HIGHEST) into active collection "Car"
car = _setup_active_car()
assert bpy.ops.import_scene.beamng_cdae(filepath=cdae_single, lod_mode="HIGHEST") == {"FINISHED"}
assert len(bpy.context.scene.collection.objects) == 0, list(bpy.context.scene.collection.objects)
assert len(car.objects) == 1, list(car.objects)

# 3. Import multi-LOD .cdae (ALL + collision) into active collection "Car"
car = _setup_active_car()
assert bpy.ops.import_scene.beamng_cdae(filepath=cdae_multi, lod_mode="ALL", import_collision=True) == {"FINISHED"}
assert len(bpy.context.scene.collection.objects) == 0, list(bpy.context.scene.collection.objects)
assert [c.name for c in bpy.context.scene.collection.children] == ["Car"]
assert set(c.name for c in car.children) == {"detail100", "detail50", "collision-1"}
assert len(car.objects) == 1  # parent Empty linked inside Car, not Scene Collection
print("ACTIVE_COLL_OK")
''')
        cmd_ac = (
            [runner[0], coll_worker, REPO_ROOT, c2_single, c2, workdir]
            if runner[0] == sys.executable
            else [*runner, coll_worker, "--", REPO_ROOT, c2_single, c2, workdir]
        )
        proc_ac = subprocess.run(cmd_ac, capture_output=True, text=True, timeout=60)
        assert proc_ac.returncode == 0 and "ACTIVE_COLL_OK" in proc_ac.stdout and "Traceback" not in proc_ac.stderr, (
            f"Active collection test failed:\n{proc_ac.stdout}\n{proc_ac.stderr}"
        )
        print("  PASS active_collection_dae_and_cdae")

        # Case 6: Subprocess fuzz test (50 corrupted mutations)
        seed_bytes = open(c1, "rb").read()
        rng = random.Random(2026)
        fuzz_batch = []
        for i in range(50):
            buf = bytearray(seed_bytes)
            op = rng.choice(["truncate", "flip", "zero_slice"])
            if op == "truncate":
                buf = buf[: rng.randrange(0, len(buf))]
            elif op == "flip":
                for _ in range(rng.randint(1, 8)):
                    pos = rng.randrange(len(buf))
                    buf[pos] ^= rng.randint(1, 255)
            else:
                s = rng.randrange(len(buf))
                e = min(len(buf), s + rng.randint(1, 16))
                buf[s:e] = b"\xff" * (e - s)
            fp = os.path.join(workdir, f"fuzz_{i:02d}.cdae")
            with open(fp, "wb") as f:
                f.write(buf)
            fuzz_batch.append(fp)

        # Run all 50 fuzz files in one subprocess (re-run individually if it fails)
        fuzz_worker = os.path.join(workdir, "_fuzz_cdae_worker.py")
        with open(fuzz_worker, "w", encoding="utf-8") as f:
            f.write(r'''
import sys
argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else sys.argv[1:]
sys.path.insert(0, argv[0])
import bpy, bmesh, io_scene_collada
from io_scene_collada.cdae.importer import CdaeImporter, CdaeImportSettings
if not hasattr(bpy.ops.import_scene, "beamng_cdae"):
    io_scene_collada.register()
for p in argv[1:]:
    bpy.ops.wm.read_factory_settings(use_empty=True)
    imp = CdaeImporter(bpy.context, CdaeImportSettings(filepath=p))
    imp.import_document()
    for me in bpy.data.meshes:
        bm = bmesh.new(); bm.from_mesh(me); bm.free()
        assert not me.validate(verbose=False, clean_customdata=False)
''')
        cmd = (
            [runner[0], fuzz_worker, REPO_ROOT, *fuzz_batch]
            if runner[0] == sys.executable
            else [*runner, fuzz_worker, "--", REPO_ROOT, *fuzz_batch]
        )
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if proc.returncode != 0 or "Traceback (most recent call last)" in proc.stderr:
            raise AssertionError(f"CDAE fuzz failed (rc={proc.returncode}):\n{proc.stderr[-1500:]}")
        print("  PASS fuzz_50_corrupted_cdae")

        # Case 7: Optional real local BeamNG .cdae files (CDAE_LOCAL_DIR or tests/_local_cdae/)
        local_dir = os.environ.get("CDAE_LOCAL_DIR") or os.path.join(REPO_ROOT, "tests", "_local_cdae")
        if os.path.isdir(local_dir):
            real_files = sorted(
                os.path.join(r, fn)
                for r, _, fns in os.walk(local_dir)
                for fn in fns
                if fn.lower().endswith(".cdae")
            )
            if real_files:
                print(f"  Testing {len(real_files)} real .cdae file(s) from {local_dir}...")
                for rf in real_files:
                    for mode in ("HIGHEST", "ALL"):
                        rr = _run_import(runner, worker_path, rf, {"lod_mode": mode}, workdir=workdir)
                        assert rr["ok"], f"Real file {rf} ({mode}) failed: {rr['errors']}"
                        assert any(m["polys"] > 0 for m in rr["meshes"]), f"Real file {rf} ({mode}) had 0 polys"
                        for em in rr["empties"]:
                            assert em["display_size"] <= 0.101, f"Real file {rf} ({mode}) large empty: {em}"
                        if os.path.basename(rf).lower() == "wigeon.cdae":
                            assert len(rr["empties"]) == 0, (
                                f"wigeon.cdae ({mode}) should have 0 Empties, got {len(rr['empties'])}"
                            )
                    print(f"    PASS real {os.path.basename(rf)}")
                _verify_positional_triangles(runner, pos_worker_path, real_files)
                print(f"    PASS positional_triangles_and_max_edge_all_3_modes ({len(real_files)} real files)")

        print("\nAll .cdae import tests passed!")
        return 0
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
