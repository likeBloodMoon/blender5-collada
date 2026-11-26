# SPDX-FileCopyrightText: 2012 GarageGames, LLC (Torque3D, MIT)
# SPDX-FileCopyrightText: 2026 Blender5-Collada Authors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""BeamNG Cached Collada (.cdae) v31 binary reader.

Parses .cdae v31 files into plain Python / NumPy data structures with strict
elemSize checks, remaining-buffer bounds checks, bounded Zstandard decompression,
and parentMesh data-sharing resolution.

Format layout verified from:
  - BeamNG official .cdae v31 documentation & cdae_dump_info.py
  - Torque3D MIT source (tsShape.h, tsMesh.h/.cpp, tsTransform.h, mQuat.cpp)
"""

from __future__ import annotations

import io
import os
import struct
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from .msgpack_lite import MsgPackError, MsgPackReader

SUPPORTED_CDAE_VERSION = 31

# Safety limits matching archive zip-bomb guards
_MAX_HEADER_BYTES = 16 * 1024 * 1024           # 16 MiB
_MAX_BODY_BYTES = 1 * 1024 * 1024 * 1024       # 1 GiB
_ZSTD_RATIO_THRESHOLD_BYTES = 50 * 1024 * 1024 # 50 MB
_MAX_ZSTD_RATIO = 200.0

# Mesh type enum (Torque3D TSMesh::Type)
MESH_TYPE_STANDARD = 0
MESH_TYPE_SKIN = 1
MESH_TYPE_DECAL = 2
MESH_TYPE_SORTED = 3
MESH_TYPE_NULL = 4

# TSDrawPrimitive matIndex bit masks
PRIM_TYPE_MASK = 0xC0000000
PRIM_TYPE_TRIANGLES = 0x00000000
PRIM_TYPE_STRIP = 0x40000000
PRIM_TYPE_FAN = 0x80000000
PRIM_INDEXED = 0x20000000
PRIM_NO_MATERIAL = 0x10000000
PRIM_MATERIAL_MASK = 0x0FFFFFFF

# Structured NumPy dtypes (little-endian, matching exact C++ struct layouts)
DTYPE_NODE = np.dtype(
    [
        ("name_index", "<i4"),
        ("parent_index", "<i4"),
        ("first_object", "<i4"),
        ("first_child", "<i4"),
        ("next_sibling", "<i4"),
    ]
)
DTYPE_OBJECT = np.dtype(
    [
        ("name_index", "<i4"),
        ("num_meshes", "<i4"),
        ("start_mesh_index", "<i4"),
        ("node_index", "<i4"),
        ("next_sibling", "<i4"),
        ("first_decal", "<i4"),
    ]
)
DTYPE_OBJECT_STATE = np.dtype([("vis", "<f4"), ("frame_index", "<i4"), ("mat_frame_index", "<i4")])
DTYPE_TRIGGER = np.dtype([("state", "<u4"), ("pos", "<f4")])
DTYPE_DETAIL = np.dtype(
    [
        ("name_index", "<i4"),
        ("sub_shape_num", "<i4"),
        ("object_detail_num", "<i4"),
        ("size", "<f4"),
        ("average_error", "<f4"),
        ("max_error", "<f4"),
        ("poly_count", "<i4"),
        ("bb_dimension", "<i4"),
        ("bb_detail_level", "<i4"),
        ("bb_equator_steps", "<u4"),
        ("bb_polar_steps", "<u4"),
        ("bb_polar_angle", "<f4"),
        ("bb_include_poles", "<u4"),
    ]
)
DTYPE_PRIMITIVE = np.dtype([("start", "<i4"), ("num_elements", "<i4"), ("mat_index", "<i4")])

assert DTYPE_NODE.itemsize == 20
assert DTYPE_OBJECT.itemsize == 24
assert DTYPE_OBJECT_STATE.itemsize == 12
assert DTYPE_TRIGGER.itemsize == 8
assert DTYPE_DETAIL.itemsize == 52
assert DTYPE_PRIMITIVE.itemsize == 12


class CdaeError(ValueError):
    """Raised when a .cdae file is unsupported, corrupt, or exceeds safety limits."""


@dataclass
class CdaeMaterial:
    name: str
    flags: int = 0
    reflect_map: int = 0
    bump_map: int = 0
    detail_map: int = 0
    detail_scale: float = 1.0
    reflection_amount: float = 0.0


@dataclass
class CdaeMesh:
    index: int
    mesh_type: int
    num_frames: int = 0
    num_mat_frames: int = 0
    parent_mesh: int = -1
    bounds: tuple[float, float, float, float, float, float] = (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    center: tuple[float, float, float] = (0.0, 0.0, 0.0)
    radius: float = 0.0
    verts: np.ndarray = field(default_factory=lambda: np.empty((0, 3), dtype=np.float32))
    tverts: np.ndarray = field(default_factory=lambda: np.empty((0, 2), dtype=np.float32))
    tverts2: np.ndarray = field(default_factory=lambda: np.empty((0, 2), dtype=np.float32))
    colors: np.ndarray = field(default_factory=lambda: np.empty((0, 4), dtype=np.uint8))
    norms: np.ndarray = field(default_factory=lambda: np.empty((0, 3), dtype=np.float32))
    encoded_norms: np.ndarray = field(default_factory=lambda: np.empty(0, dtype=np.uint8))
    primitives: np.ndarray = field(default_factory=lambda: np.empty(0, dtype=DTYPE_PRIMITIVE))
    indices: np.ndarray = field(default_factory=lambda: np.empty(0, dtype=np.uint32))
    tangents: np.ndarray = field(default_factory=lambda: np.empty((0, 4), dtype=np.float32))
    verts_per_frame: int = 0
    mesh_flags: int = 0
    # SkinMesh (mesh_type == 1) fields
    initial_verts: np.ndarray = field(default_factory=lambda: np.empty((0, 3), dtype=np.float32))
    initial_norms: np.ndarray = field(default_factory=lambda: np.empty((0, 3), dtype=np.float32))
    initial_transforms: np.ndarray = field(default_factory=lambda: np.empty((0, 4, 4), dtype=np.float32))
    vertex_index: np.ndarray = field(default_factory=lambda: np.empty(0, dtype=np.int32))
    bone_index: np.ndarray = field(default_factory=lambda: np.empty(0, dtype=np.int32))
    weight: np.ndarray = field(default_factory=lambda: np.empty(0, dtype=np.float32))
    node_index: np.ndarray = field(default_factory=lambda: np.empty(0, dtype=np.int32))


@dataclass
class CdaeShape:
    shape_version: int
    exporter_version: int
    header_info: dict[str, Any]
    smallest_visible_size: float
    smallest_visible_dl: int
    radius: float
    tube_radius: float
    center: tuple[float, float, float]
    bounds: tuple[float, float, float, float, float, float]
    nodes: np.ndarray
    objects: np.ndarray
    sub_shape_first_node: np.ndarray
    sub_shape_first_object: np.ndarray
    sub_shape_num_nodes: np.ndarray
    sub_shape_num_objects: np.ndarray
    default_rotations: np.ndarray      # (N, 4) int16 Quat16 (x, y, z, w)
    default_translations: np.ndarray   # (N, 3) float32
    node_rotations: np.ndarray
    node_translations: np.ndarray
    node_uniform_scales: np.ndarray
    node_aligned_scales: np.ndarray
    node_arbitrary_scale_factors: np.ndarray
    node_arbitrary_scale_rots: np.ndarray
    ground_translations: np.ndarray
    ground_rotations: np.ndarray
    object_states: np.ndarray
    triggers: np.ndarray
    details: np.ndarray
    names: list[str]
    meshes: list[CdaeMesh]
    num_sequences: int
    materials: list[CdaeMaterial]
    warnings: list[str] = field(default_factory=list)


def _decompress_zstd_bounded(compressed: bytes) -> bytes:
    try:
        import zstandard as zstd
    except ImportError as exc:
        raise CdaeError(
            "This .cdae file is Zstandard-compressed, but the 'zstandard' package is not "
            "available in this Blender installation."
        ) from exc

    comp_len = max(len(compressed), 1)
    dctx = zstd.ZstdDecompressor()
    out = bytearray()
    chunk_size = 1024 * 1024
    try:
        with dctx.stream_reader(io.BytesIO(compressed)) as reader:
            while True:
                chunk = reader.read(chunk_size)
                if not chunk:
                    break
                out.extend(chunk)
                cur_len = len(out)
                if cur_len > _MAX_BODY_BYTES:
                    raise CdaeError(
                        f"Decompressed .cdae body exceeds 1 GiB safety limit ({cur_len} bytes)"
                    )
                if cur_len > _ZSTD_RATIO_THRESHOLD_BYTES and (cur_len / comp_len) > _MAX_ZSTD_RATIO:
                    raise CdaeError(
                        f"Decompressed .cdae body exceeds compression ratio safety limit "
                        f"({cur_len / comp_len:.1f}x > {_MAX_ZSTD_RATIO:.0f}x)"
                    )
    except CdaeError:
        raise
    except Exception as exc:
        raise CdaeError(f"Failed to decompress Zstandard .cdae body: {exc}") from exc
    return bytes(out)


class _BodyStream:
    """Typed, bounds-checked reader over the sequential MsgPack body."""

    def __init__(self, data: bytes) -> None:
        self.mp = MsgPackReader(data)

    def read_int(self, label: str) -> int:
        val = self.mp.read(zero_copy_bin=False)
        if isinstance(val, bool):
            raise CdaeError(f"{label}: expected int, got bool")
        if isinstance(val, int):
            return val
        if isinstance(val, float) and abs(val - round(val)) < 1e-9:
            return int(round(val))
        raise CdaeError(f"{label}: expected int, got {type(val).__name__}")

    def read_uint(self, label: str) -> int:
        val = self.read_int(label)
        if val < 0:
            raise CdaeError(f"{label}: expected non-negative int, got {val}")
        return val

    def read_float(self, label: str) -> float:
        val = self.mp.read(zero_copy_bin=False)
        if isinstance(val, bool):
            raise CdaeError(f"{label}: expected float, got bool")
        if isinstance(val, (int, float)):
            return float(val)
        raise CdaeError(f"{label}: expected float, got {type(val).__name__}")

    def read_str(self, label: str) -> str:
        val = self.mp.read(zero_copy_bin=False)
        if not isinstance(val, str):
            raise CdaeError(f"{label}: expected str, got {type(val).__name__}")
        return val

    def read_point3f(self, label: str) -> tuple[float, float, float]:
        val = self.mp.read(zero_copy_bin=True)
        if isinstance(val, list):
            if len(val) != 3:
                raise CdaeError(f"{label}: expected 3 floats, got list of {len(val)}")
            return (float(val[0]), float(val[1]), float(val[2]))
        if isinstance(val, (bytes, memoryview)):
            if len(val) != 12:
                raise CdaeError(f"{label}: expected 12 bytes for Point3F, got {len(val)}")
            x, y, z = struct.unpack("<3f", val)
            return (float(x), float(y), float(z))
        raise CdaeError(f"{label}: expected Point3F list or 12-byte bin, got {type(val).__name__}")

    def read_box3f(self, label: str) -> tuple[float, float, float, float, float, float]:
        val = self.mp.read(zero_copy_bin=True)
        if isinstance(val, list):
            if len(val) != 6:
                raise CdaeError(f"{label}: expected 6 floats, got list of {len(val)}")
            return tuple(float(v) for v in val)  # type: ignore[return-value]
        if isinstance(val, (bytes, memoryview)):
            if len(val) != 24:
                raise CdaeError(f"{label}: expected 24 bytes for Box3F, got {len(val)}")
            vals = struct.unpack("<6f", val)
            return tuple(float(v) for v in vals)  # type: ignore[return-value]
        raise CdaeError(f"{label}: expected Box3F list or 24-byte bin, got {type(val).__name__}")

    def read_pack_vector(
        self,
        label: str,
        expected_elem_size: int,
        dtype: np.dtype[Any] | type | str,
        shape_tail: tuple[int, ...] = (),
    ) -> np.ndarray[Any, Any]:
        """Read a pack_vector (size, elemSize, bin) using strict elemSize validation."""
        count = self.read_int(f"{label}.size")
        elem_size = self.read_int(f"{label}.elemSize")
        raw = self.mp.read(zero_copy_bin=True)
        if not isinstance(raw, (bytes, memoryview)):
            raise CdaeError(f"{label}.data: expected bin, got {type(raw).__name__}")

        if count < 0:
            raise CdaeError(f"{label}: negative element count {count}")
        if elem_size != expected_elem_size:
            raise CdaeError(
                f"{label}: elemSize mismatch (expected {expected_elem_size} bytes, got {elem_size})"
            )
        expected_bytes = count * elem_size
        if len(raw) != expected_bytes:
            raise CdaeError(
                f"{label}: byte length mismatch (expected {count} * {elem_size} = {expected_bytes} bytes, "
                f"got {len(raw)})"
            )

        np_dtype = np.dtype(dtype)
        if count == 0:
            return np.empty((0, *shape_tail), dtype=np_dtype)

        arr = np.frombuffer(raw, dtype=np_dtype).copy()
        if shape_tail:
            arr = arr.reshape((count, *shape_tail))
        return arr

    def skip_integerset(self, label: str) -> None:
        val = self.mp.read(zero_copy_bin=True)
        if not isinstance(val, (bytes, memoryview, list)):
            raise CdaeError(f"{label}: expected TSIntegerSet bytes or list, got {type(val).__name__}")


def _resolve_parent_meshes(meshes: list[CdaeMesh], warnings: list[str]) -> None:
    """Resolve Torque3D TSMesh::assemble parentMesh >= 0 shared vertex data."""
    n = len(meshes)
    for m in meshes:
        if m.mesh_type == MESH_TYPE_NULL or m.parent_mesh < 0:
            continue
        # Walk parent chain safely with cycle detection
        visited = {m.index}
        cur_idx = m.parent_mesh
        chain: list[CdaeMesh] = []
        while 0 <= cur_idx < n and cur_idx not in visited:
            visited.add(cur_idx)
            parent = meshes[cur_idx]
            if parent.mesh_type == MESH_TYPE_NULL:
                break
            chain.append(parent)
            cur_idx = parent.parent_mesh

        if not chain:
            warnings.append(
                f"Mesh {m.index}: invalid parentMesh index {m.parent_mesh} (total meshes={n})"
            )
            continue

        for attr in (
            "verts",
            "tverts",
            "tverts2",
            "colors",
            "norms",
            "encoded_norms",
            "initial_verts",
            "initial_norms",
            "vertex_index",
            "bone_index",
            "weight",
            "node_index",
        ):
            if getattr(m, attr).size == 0:
                for p in chain:
                    pval = getattr(p, attr)
                    if pval.size > 0:
                        setattr(m, attr, pval)
                        break


def _detect_non_cdae_reason(data: bytes) -> str | None:
    """Detect common non-.cdae file headers so users get a clear message instead of a bogus version number."""
    if data.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")):
        return (
            "This file is a ZIP archive, not a .cdae file "
            "(use File > Import > Collada (.dae, .zae, .kmz) for .zip/.zae/.kmz archives)"
        )
    head_stripped = data[:64].lstrip(b" \t\r\n")
    if (
        head_stripped.startswith((b"<?xml", b"<COLLADA", b"<collada"))
        or data.startswith(b"\xef\xbb\xbf<")
    ):
        return (
            "This file is XML/COLLADA text, not a binary .cdae file "
            "(use File > Import > Collada (.dae, .zae, .kmz))"
        )
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "This file is a PNG image, not a .cdae file"
    if data.startswith(b"DDS "):
        return "This file is a DDS texture, not a .cdae file"
    if data.startswith(b"BLENDER"):
        return "This file is a Blender .blend file, not a .cdae file"
    if data.startswith(b"glTF"):
        return "This file is a glTF/GLB file, not a .cdae file"
    if data.startswith(b"Kaydara FBX Binary"):
        return "This file is an FBX file, not a .cdae file"
    if data.startswith(b"\x1f\x8b"):
        return "This file is a gzip archive, not a .cdae file"
    if data.startswith(b"\x28\xb5\x2f\xfd"):
        return "This file is a raw Zstandard stream, not a .cdae file"
    return None


def read_cdae_bytes(data: bytes) -> CdaeShape:
    """Parse a BeamNG .cdae v31 binary buffer into a CdaeShape."""
    if len(data) < 8:
        raise CdaeError(f"File too small to be a .cdae file ({len(data)} bytes)")
    if len(data) > _MAX_BODY_BYTES:
        raise CdaeError(f".cdae file exceeds 1 GiB safety limit ({len(data)} bytes)")

    non_cdae = _detect_non_cdae_reason(data)
    if non_cdae is not None:
        raise CdaeError(non_cdae)

    version_val, header_size = struct.unpack_from("<II", data, 0)
    shape_version = version_val & 0xFF
    reserved_byte = (version_val >> 8) & 0xFF
    exporter_version = version_val >> 16
    if reserved_byte != 0 or shape_version == 0 or shape_version > 40:
        raise CdaeError("Not a valid .cdae file (unrecognized header)")
    if shape_version != SUPPORTED_CDAE_VERSION:
        raise CdaeError(
            f"Unsupported .cdae version {shape_version} (exporter={exporter_version}); "
            f"only version {SUPPORTED_CDAE_VERSION} is supported"
        )

    if header_size > _MAX_HEADER_BYTES or 8 + header_size > len(data):
        raise CdaeError(
            f"Invalid .cdae headerSize {header_size} (file length {len(data)} bytes)"
        )

    header_slice = data[8 : 8 + header_size]
    try:
        header_obj = MsgPackReader(header_slice).read(zero_copy_bin=False)
    except MsgPackError as exc:
        raise CdaeError(f"Invalid .cdae MsgPack header: {exc}") from exc

    if not isinstance(header_obj, dict):
        raise CdaeError(f"Expected MsgPack dict in .cdae header, got {type(header_obj).__name__}")

    is_compressed = bool(header_obj.get("compression", False))
    body_size = header_obj.get("bodysize", 0)
    if not isinstance(body_size, int) or body_size < 0:
        raise CdaeError(f"Invalid bodysize in .cdae header: {body_size!r}")

    body_raw = data[8 + header_size :]
    if len(body_raw) < body_size:
        raise CdaeError(
            f"Truncated .cdae body: header declares bodysize={body_size}, "
            f"but only {len(body_raw)} bytes remain"
        )
    if body_size > 0:
        body_raw = body_raw[:body_size]

    body_bytes = _decompress_zstd_bounded(body_raw) if is_compressed else body_raw

    try:
        return _parse_body(shape_version, exporter_version, header_obj, body_bytes)
    except MsgPackError as exc:
        raise CdaeError(f"Malformed MsgPack body in .cdae: {exc}") from exc


def read_cdae_file(filepath: str) -> CdaeShape:
    """Read and parse a BeamNG .cdae v31 file from disk."""
    try:
        size = os.path.getsize(filepath)
    except OSError as exc:
        raise CdaeError(f"Cannot access .cdae file: {exc}") from exc
    if size > _MAX_BODY_BYTES:
        raise CdaeError(f".cdae file exceeds 1 GiB safety limit ({size} bytes)")
    try:
        with open(filepath, "rb") as f:
            data = f.read()
    except OSError as exc:
        raise CdaeError(f"Cannot read .cdae file: {exc}") from exc
    return read_cdae_bytes(data)


def _parse_body(
    shape_version: int,
    exporter_version: int,
    header_info: dict[str, Any],
    body_bytes: bytes,
) -> CdaeShape:
    s = _BodyStream(body_bytes)
    warnings: list[str] = []

    # 3.1 Shape-level scalars
    smallest_visible_size = s.read_float("mSmallestVisibleSize")
    smallest_visible_dl = s.read_int("mSmallestVisibleDL")
    radius = s.read_float("radius")
    tube_radius = s.read_float("tubeRadius")
    center = s.read_point3f("center")
    bounds = s.read_box3f("bounds")

    # 3.2 Shape pack_vector arrays (19 vectors, in exact stream order)
    nodes = s.read_pack_vector("nodes", 20, DTYPE_NODE)
    objects = s.read_pack_vector("objects", 24, DTYPE_OBJECT)
    sub_shape_first_node = s.read_pack_vector("subShapeFirstNode", 4, "<i4")
    sub_shape_first_object = s.read_pack_vector("subShapeFirstObject", 4, "<i4")
    sub_shape_num_nodes = s.read_pack_vector("subShapeNumNodes", 4, "<i4")
    sub_shape_num_objects = s.read_pack_vector("subShapeNumObjects", 4, "<i4")
    default_rotations = s.read_pack_vector("defaultRotations", 8, "<i2", (4,))
    default_translations = s.read_pack_vector("defaultTranslations", 12, "<f4", (3,))
    node_rotations = s.read_pack_vector("nodeRotations", 8, "<i2", (4,))
    node_translations = s.read_pack_vector("nodeTranslations", 12, "<f4", (3,))
    node_uniform_scales = s.read_pack_vector("nodeUniformScales", 4, "<f4")
    node_aligned_scales = s.read_pack_vector("nodeAlignedScales", 12, "<f4", (3,))
    node_arbitrary_scale_factors = s.read_pack_vector("nodeArbitraryScaleFactors", 12, "<f4", (3,))
    node_arbitrary_scale_rots = s.read_pack_vector("nodeArbitraryScaleRots", 8, "<i2", (4,))
    ground_translations = s.read_pack_vector("groundTranslations", 12, "<f4", (3,))
    ground_rotations = s.read_pack_vector("groundRotations", 8, "<i2", (4,))
    object_states = s.read_pack_vector("objectStates", 12, DTYPE_OBJECT_STATE)
    triggers = s.read_pack_vector("triggers", 8, DTYPE_TRIGGER)
    details = s.read_pack_vector("details", 52, DTYPE_DETAIL)

    # 3.3 Names table
    num_names = s.read_uint("names.size")
    if num_names > s.mp.remaining:
        raise CdaeError(f"names.size ({num_names}) exceeds remaining stream bytes ({s.mp.remaining})")
    names = [s.read_str(f"names[{i}]") for i in range(num_names)]

    # 3.4 Meshes
    total_meshes = s.read_uint("meshes.size")
    if total_meshes > s.mp.remaining:
        raise CdaeError(f"meshes.size ({total_meshes}) exceeds remaining stream bytes ({s.mp.remaining})")

    _MESH_TYPE_NAMES = {
        0: "StandardMesh",
        1: "SkinMesh",
        2: "DecalMesh",
        3: "SortedMesh",
        4: "NullMesh",
        5: "BillboardMesh",
    }

    meshes: list[CdaeMesh] = []
    for mesh_i in range(total_meshes):
        mesh_type = s.read_uint(f"mesh[{mesh_i}].type")
        if mesh_type == MESH_TYPE_NULL:
            meshes.append(CdaeMesh(index=mesh_i, mesh_type=mesh_type))
            continue
        if mesh_type not in (MESH_TYPE_STANDARD, MESH_TYPE_SKIN):
            tname = _MESH_TYPE_NAMES.get(mesh_type, f"Unknown({mesh_type})")
            raise CdaeError(
                f"Mesh {mesh_i}: unsupported .cdae mesh type {mesh_type} ({tname}); "
                f"only StandardMesh (0), SkinMesh (1), and NullMesh (4) are supported"
            )

        num_frames = s.read_int(f"mesh[{mesh_i}].numFrames")
        num_mat_frames = s.read_int(f"mesh[{mesh_i}].numMatFrames")
        parent_mesh = s.read_int(f"mesh[{mesh_i}].parentMesh")
        m_bounds = s.read_box3f(f"mesh[{mesh_i}].mBounds")
        m_center = s.read_point3f(f"mesh[{mesh_i}].mCenter")
        m_radius = s.read_float(f"mesh[{mesh_i}].mRadius")

        verts = s.read_pack_vector(f"mesh[{mesh_i}].verts", 12, "<f4", (3,))
        tverts = s.read_pack_vector(f"mesh[{mesh_i}].tverts", 8, "<f4", (2,))
        tverts2 = s.read_pack_vector(f"mesh[{mesh_i}].tverts2", 8, "<f4", (2,))
        colors = s.read_pack_vector(f"mesh[{mesh_i}].colors", 4, "<u1", (4,))
        norms = s.read_pack_vector(f"mesh[{mesh_i}].norms", 12, "<f4", (3,))
        encoded_norms = s.read_pack_vector(f"mesh[{mesh_i}].encodedNorms", 1, "<u1")
        primitives = s.read_pack_vector(f"mesh[{mesh_i}].primitives", 12, DTYPE_PRIMITIVE)
        indices = s.read_pack_vector(f"mesh[{mesh_i}].indices", 4, "<u4")
        tangents = s.read_pack_vector(f"mesh[{mesh_i}].tangents", 16, "<f4", (4,))

        verts_per_frame = s.read_int(f"mesh[{mesh_i}].vertsPerFrame")
        mesh_flags = s.read_uint(f"mesh[{mesh_i}].meshFlags")

        mesh_obj = CdaeMesh(
            index=mesh_i,
            mesh_type=mesh_type,
            num_frames=num_frames,
            num_mat_frames=num_mat_frames,
            parent_mesh=parent_mesh,
            bounds=m_bounds,
            center=m_center,
            radius=m_radius,
            verts=verts,
            tverts=tverts,
            tverts2=tverts2,
            colors=colors,
            norms=norms,
            encoded_norms=encoded_norms,
            primitives=primitives,
            indices=indices,
            tangents=tangents,
            verts_per_frame=verts_per_frame,
            mesh_flags=mesh_flags,
        )

        if mesh_type == MESH_TYPE_SKIN:
            is_parent = parent_mesh < 0
            if is_parent:
                mesh_obj.initial_verts = s.read_pack_vector(f"skin[{mesh_i}].initialVerts", 12, "<f4", (3,))
                mesh_obj.initial_norms = s.read_pack_vector(f"skin[{mesh_i}].initialNorms", 12, "<f4", (3,))
            mesh_obj.initial_transforms = s.read_pack_vector(
                f"skin[{mesh_i}].initialTransforms", 64, "<f4", (4, 4)
            )
            if is_parent:
                mesh_obj.vertex_index = s.read_pack_vector(f"skin[{mesh_i}].vertexIndex", 4, "<i4")
                mesh_obj.bone_index = s.read_pack_vector(f"skin[{mesh_i}].boneIndex", 4, "<i4")
                mesh_obj.weight = s.read_pack_vector(f"skin[{mesh_i}].weight", 4, "<f4")
                mesh_obj.node_index = s.read_pack_vector(f"skin[{mesh_i}].nodeIndex", 4, "<i4")

        meshes.append(mesh_obj)

    _resolve_parent_meshes(meshes, warnings)

    # 3.5 Sequences (parsed to advance stream; animations are out of scope for import)
    tot_sequences = s.read_uint("sequences.size")
    if tot_sequences > s.mp.remaining:
        raise CdaeError(
            f"sequences.size ({tot_sequences}) exceeds remaining stream bytes ({s.mp.remaining})"
        )
    for seq_i in range(tot_sequences):
        s.read_int(f"seq[{seq_i}].nameIndex")
        s.read_int(f"seq[{seq_i}].flags")
        s.read_int(f"seq[{seq_i}].numKeyframes")
        s.read_float(f"seq[{seq_i}].duration")
        s.read_int(f"seq[{seq_i}].priority")
        s.read_int(f"seq[{seq_i}].firstGroundFrame")
        s.read_int(f"seq[{seq_i}].numGroundFrames")
        s.read_int(f"seq[{seq_i}].baseRotation")
        s.read_int(f"seq[{seq_i}].baseTranslation")
        s.read_int(f"seq[{seq_i}].baseScale")
        s.read_int(f"seq[{seq_i}].baseObjectState")
        s.read_int(f"seq[{seq_i}].baseDecalState")
        s.read_int(f"seq[{seq_i}].firstTrigger")
        s.read_int(f"seq[{seq_i}].numTriggers")
        s.read_float(f"seq[{seq_i}].toolBegin")
        for iset_name in (
            "rotationMatters",
            "translationMatters",
            "scaleMatters",
            "visMatters",
            "frameMatters",
            "matFrameMatters",
        ):
            s.skip_integerset(f"seq[{seq_i}].{iset_name}")

    # 3.6 Optional Material List
    materials: list[CdaeMaterial] = []
    if not s.mp.eof:
        mat_count = s.read_uint("materialList.size")
        if mat_count * 7 > s.mp.remaining:
            raise CdaeError(
                f"materialList.size ({mat_count}) exceeds remaining stream bytes ({s.mp.remaining})"
            )
        for mat_i in range(mat_count):
            materials.append(
                CdaeMaterial(
                    name=s.read_str(f"material[{mat_i}].name"),
                    flags=s.read_uint(f"material[{mat_i}].flags"),
                    reflect_map=s.read_uint(f"material[{mat_i}].reflectMap"),
                    bump_map=s.read_uint(f"material[{mat_i}].bumpMap"),
                    detail_map=s.read_uint(f"material[{mat_i}].detailMap"),
                    detail_scale=s.read_float(f"material[{mat_i}].detailScale"),
                    reflection_amount=s.read_float(f"material[{mat_i}].reflectionAmount"),
                )
            )

    return CdaeShape(
        shape_version=shape_version,
        exporter_version=exporter_version,
        header_info=header_info,
        smallest_visible_size=smallest_visible_size,
        smallest_visible_dl=smallest_visible_dl,
        radius=radius,
        tube_radius=tube_radius,
        center=center,
        bounds=bounds,
        nodes=nodes,
        objects=objects,
        sub_shape_first_node=sub_shape_first_node,
        sub_shape_first_object=sub_shape_first_object,
        sub_shape_num_nodes=sub_shape_num_nodes,
        sub_shape_num_objects=sub_shape_num_objects,
        default_rotations=default_rotations,
        default_translations=default_translations,
        node_rotations=node_rotations,
        node_translations=node_translations,
        node_uniform_scales=node_uniform_scales,
        node_aligned_scales=node_aligned_scales,
        node_arbitrary_scale_factors=node_arbitrary_scale_factors,
        node_arbitrary_scale_rots=node_arbitrary_scale_rots,
        ground_translations=ground_translations,
        ground_rotations=ground_rotations,
        object_states=object_states,
        triggers=triggers,
        details=details,
        names=names,
        meshes=meshes,
        num_sequences=tot_sequences,
        materials=materials,
        warnings=warnings,
    )
