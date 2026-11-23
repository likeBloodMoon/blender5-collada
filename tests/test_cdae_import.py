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
        indices.extend([base, base + 1, base + 2, base, base + 2, base + 3])
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
            "uv_layers": uv_names,
            "color_attrs": col_names,
            "materials": mat_names,
            "vertex_groups": vgroups,
            "modifiers": mods,
            "still_invalid": still_invalid,
        })

result = {
    "ok": bool(ok),
    "errors": list(importer.errors),
    "warnings": list(importer.warnings),
    "infos": list(importer.infos),
    "collections": sorted(c.name for c in bpy.data.collections),
    "armatures": [ob.name for ob in bpy.data.objects if ob.type == "ARMATURE"],
    "meshes": meshes_out,
}
with open(out_json, "w", encoding="utf-8") as f:
    json.dump(result, f)
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


def main() -> int:
    print("Running BeamNG .cdae v31 import test suite...")
    test_msgpack_lite_unit()

    runner = _find_runner()
    workdir = tempfile.mkdtemp(prefix="collada_cdae_")
    try:
        worker_path = os.path.join(workdir, "_cdae_worker.py")
        with open(worker_path, "w", encoding="utf-8") as f:
            f.write(WORKER_SCRIPT)

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
        assert m1["sharp_edges"] == 12, f"Expected 12 sharp edges on merged cube, got {m1}"
        assert m1["uv_layers"] == ["UVMap", "UVMap.001"], m1
        assert m1["color_attrs"] == ["Color"], m1
        assert m1["materials"] == ["paint_main", "chrome_trim"], m1
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
        r2_hi = _run_import(runner, worker_path, c2, {"lod_mode": "HIGHEST", "import_collision": False})
        assert r2_hi["ok"] and len(r2_hi["meshes"]) == 1, r2_hi
        print("  PASS parentMesh_sharing_strip_fan_lod_collision")

        # Case 3: SkinMesh + skipped sequence info
        skin_m = {
            "type": 1,
            "verts": quad_v,
            "norms": np.tile([0.0, 0.0, 1.0], (4, 1)).astype(np.float32),
            "primitives": [(0, 6, 0x20000000 | 0)],
            "indices": np.array([0, 1, 3, 0, 3, 2], dtype=np.uint32),
            "initial_transforms": np.array([np.eye(4, dtype=np.float32), np.eye(4, dtype=np.float32)]),
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
        print("  PASS skin_mesh_armature_and_sequence_info")

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
        assert any("2 double-sided faces kept unwelded, 1 exact duplicates removed" in inf for inf in r_ds["infos"]), r_ds["infos"]
        print("  PASS double_sided_kept_and_exact_dup_removed")

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
                    print(f"    PASS real {os.path.basename(rf)}")

        print("\nAll .cdae import tests passed!")
        return 0
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
