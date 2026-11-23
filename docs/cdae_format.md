# BeamNG Cached Collada (`.cdae`) v31 Binary Format Contract

This document specifies the verified byte-level layout of BeamNG.drive's `.cdae` v31 binary shape format and serves as the implementation contract for the reader, importer, and experimental exporter.

Every fact below cites its primary verification source:
- **[Spec]**: Official BeamNG `.cdae` v31 documentation (`https://documentation.beamng.com/modding/file_formats/cdae/`).
- **[Dump]**: BeamNG's official `cdae_dump_info.py` reference script (downloaded from the documentation page).
- **[T3D]**: Torque3D MIT source (`github.com/TorqueGameEngines/Torque3D`, `development` branch).
- **[Real]**: Observed in real BeamNG `.cdae` files (currently pending local game files via `CDAE_LOCAL_DIR`).

---

## 1. Outer File Container

All raw integer fields and packed binary buffers are **little-endian**.

| Offset | Type | Encoding | Description | Source |
| --- | --- | --- | --- | --- |
| `0` | `uint32` (`<I`) | Raw bytes | `version = smVersion \| (exporterVersion << 16)`. Valid v31 files satisfy `(version & 0xFF) == 31`. `exporterVersion = version >> 16` (Torque3D uses `124`). | [Spec], [Dump:146-159], [T3D `tsShapeLoader.cpp:139`] |
| `4` | `uint32` (`<I`) | Raw bytes | `headerSize`: byte length of the uncompressed MsgPack header immediately following. | [Spec], [Dump:163-167] |
| `8` | `bytes[headerSize]` | MsgPack map | Header dictionary with keys:<br>- `"compression"` (`bool`): `True` if body is Zstd-compressed.<br>- `"bodysize"` (`int`): **stored (on-disk, compressed if compressed)** byte length of the body payload. | [Spec], [Dump:170-211] |
| `8 + headerSize` | `bytes[bodysize]` | Raw or Zstd-compressed MsgPack stream | Body payload. If `compression` is `True`, decompress with Zstandard before MsgPack unpacking. | [Spec], [Dump:201-218] |

---

## 2. Body Stream Structure

The decompressed body is a **flat sequential stream of MsgPack values** (unpacked sequentially with `next(unpacker)`, **not** a top-level MsgPack map) [Dump:217-232].

### Primitive MsgPack Helpers
- **Scalar ints/floats/strings**: Standard MsgPack integer, float (`float32`/`float64`), or UTF-8 string objects [Dump:234-280].
- **`Point3F`**: Either a 3-element MsgPack array `[x, y, z]` of floats **or** a 12-byte MsgPack `bin` (`<3f`) [Dump:282-298].
- **`Box3F`**: Either a 6-element MsgPack array `[minX, minY, minZ, maxX, maxY, maxZ]` **or** a 24-byte MsgPack `bin` (`<6f`) [Dump:300-317].
- **`pack_vector` (`read_vector`)**: Three consecutive MsgPack objects [Dump:373-388]:
  1. `size` (MsgPack int): number of elements.
  2. `elemSize` (MsgPack int): byte size of each element. **The reader must always use `elemSize` from the stream and fail with a clear error if `elemSize` does not match the expected contract size (or if `len(data) != size * elemSize`).**
  3. `data` (MsgPack `bin`): raw little-endian byte buffer of length `size * elemSize`.
- **`TSIntegerSet`**: A single MsgPack object — either a `bin` buffer or a `list` of `uint32` bit-words (`MAX_TS_SET_DWORDS = 64`, 2048 bits max) [Dump:393-401], [T3D `tsIntegerSet.h:37-49`].

---

## 3. Shape Header & Top-Level Vectors (in exact stream order)

### 3.1 Shape-Level Scalars [Dump:408-413]
1. `mSmallestVisibleSize` (`float`)
2. `mSmallestVisibleDL` (`int`)
3. `radius` (`float`)
4. `tubeRadius` (`float`)
5. `center` (`Point3F`)
6. `bounds` (`Box3F`)

### 3.2 Shape `pack_vector` Arrays (19 vectors, in exact order) [Dump:334-353, 431-633]

| # | Vector Name | `elemSize` | Struct Layout (little-endian) | Fields | Source |
| --- | --- | --- | --- | --- | --- |
| 1 | `nodes` | `20` | `<5i` | `nameIndex`, `parentIndex`, `firstObject`, `firstChild`, `nextSibling` | [Dump:445], [T3D `tsShape.h`] |
| 2 | `objects` | `24` | `<6i` | `nameIndex`, `numMeshes`, `startMeshIndex`, `nodeIndex`, `nextSibling`, `firstDecal` | [Dump:473], [T3D `tsShape.h`] |
| 3 | `subShapeFirstNode` | `4` | `<i` | `int32` | [Dump:337] |
| 4 | `subShapeFirstObject` | `4` | `<i` | `int32` | [Dump:338] |
| 5 | `subShapeNumNodes` | `4` | `<i` | `int32` | [Dump:339] |
| 6 | `subShapeNumObjects` | `4` | `<i` | `int32` | [Dump:340] |
| 7 | `defaultRotations` | `8` | `<4h` | `Quat16`: `int16 x, y, z, w` (see §5.2) | [Dump:341], [T3D `tsTransform.h:33-37`] |
| 8 | `defaultTranslations` | `12` | `<3f` | `Point3F`: `float x, y, z` | [Dump:342] |
| 9 | `nodeRotations` | `8` | `<4h` | `Quat16` | [Dump:343] |
| 10 | `nodeTranslations` | `12` | `<3f` | `Point3F` | [Dump:344] |
| 11 | `nodeUniformScales` | `4` | `<f` | `float32` | [Dump:345] |
| 12 | `nodeAlignedScales` | `12` | `<3f` | `Point3F` | [Dump:346] |
| 13 | `nodeArbitraryScaleFactors` | `12` | `<3f` | `Point3F` | [Dump:347] |
| 14 | `nodeArbitraryScaleRots` | `8` | `<4h` | `Quat16` | [Dump:348] |
| 15 | `groundTranslations` | `12` | `<3f` | `Point3F` | [Dump:349] |
| 16 | `groundRotations` | `8` | `<4h` | `Quat16` | [Dump:350] |
| 17 | `objectStates` | `12` | `<fii` | `vis` (`f32`), `frameIndex` (`i32`), `matFrameIndex` (`i32`) | [Dump:550], [T3D `tsShape.h:250-255`] |
| 18 | `triggers` | `8` | `<If` | `state` (`u32`), `pos` (`f32`) | [Dump:571], [T3D `tsShape.h:259-270`] |
| 19 | `details` | `52` | `<3i3fi2i2IfI` | `nameIndex`, `subShapeNum`, `objectDetailNum`, `size`, `averageError`, `maxError`, `polyCount`, `bbDimension`, `bbDetailLevel`, `bbEquatorSteps`, `bbPolarSteps`, `bbPolarAngle`, `bbIncludePoles` | [Dump:614], [T3D `tsShape.h:284-303`] |

### 3.3 Names Table [Dump:641-645]
- `num_names` (MsgPack `uint32`), followed by `num_names` MsgPack strings.

### 3.4 Meshes [Dump:652-841]
- `total_meshes` (MsgPack `uint32`), followed by `total_meshes` mesh records.
- Each mesh begins with `meshType` (MsgPack `uint32`):
  - `0`: `StandardMesh`
  - `1`: `SkinMesh`
  - `2`: `DecalMesh` (deprecated)
  - `3`: `SortedMesh`
  - `4`: `NullMesh` (**no further fields for this mesh**)
- Non-null meshes (`meshType != 4`) contain, in order:
  1. `numFrames` (`int`), `numMatFrames` (`int`), `parentMesh` (`int`, `< 0` means root/parent mesh)
  2. `mBounds` (`Box3F`), `mCenter` (`Point3F`), `mRadius` (`float`)
  3. 9 `pack_vector` arrays:
     - `verts` (`elemSize=12`, `<3f` `Point3F`)
     - `tverts` (`elemSize=8`, `<2f` `Point2F`, UV0)
     - `tverts2` (`elemSize=8`, `<2f` `Point2F`, UV1)
     - `colors` (`elemSize=4`, `<4B` `ColorI` RGBA)
     - `norms` (`elemSize=12`, `<3f` `Point3F`)
     - `encodedNorms` (`elemSize=1`, `<B` `uint8` table index)
     - `primitives` (`elemSize=12`, `<3i` `TSDrawPrimitive`: `start`, `numElements`, `matIndex`)
     - `indices` (`elemSize=4`, `<I` `uint32`)
     - `tangents` (`elemSize=16`, `<4f` `Point4F`: `x, y, z, handedness`)
  4. `vertsPerFrame` (`int`)
  5. `meshFlags` (`uint32`):
     - `0x80000000` (`1 << 31`): `Billboard`
     - `0x40000000` (`1 << 30`): `HasDetailTexture`
     - `0x20000000` (`1 << 29`): `BillboardZAxis`
     - `0x10000000` (`1 << 28`): `UseEncodedNormals`
- **SkinMesh (`meshType == 1`) additional `pack_vector` arrays** [Dump:808-836]:
  - If `parentMesh < 0` (`is_parent_mesh`):
    - `initialVerts` (`elemSize=12`, `<3f`)
    - `initialNorms` (`elemSize=12`, `<3f`)
  - Always (for all SkinMeshes):
    - `initialTransforms` (`elemSize=64`, `<16f` row-major `MatrixF` inverse bind matrices)
  - If `parentMesh < 0`:
    - `vertexIndex` (`elemSize=4`, `<i`)
    - `boneIndex` (`elemSize=4`, `<i`)
    - `weight` (`elemSize=4`, `<f`)
    - `nodeIndex` (`elemSize=4`, `<i`, maps `boneIndex -> shape node index`)
- **Child mesh data sharing (`parentMesh >= 0`)** [T3D `tsMesh.cpp:2446-2492`], [Dump:811-836]:
  - When `parentMesh >= 0`, `TSMesh::assemble()` resolves empty vertex streams (`verts`, `tverts`, `tverts2`, `colors`, `norms`, `encodedNorms`) from `meshes[parentMesh]` via `getSharedData32`/`getSharedData8` (transitively along the parent chain, guarding against invalid indices or cycles).
  - For `SkinMesh` (`meshType == 1`) with `parentMesh >= 0`, `initialVerts`, `initialNorms`, `vertexIndex`, `boneIndex`, `weight`, and `nodeIndex` are omitted from the child stream and inherited from `meshes[parentMesh]`; `initialTransforms` is still stored on the child mesh.

### 3.5 Sequences [Dump:846-872]
- `tot_sequences` (MsgPack `uint32`), followed by `tot_sequences` records of 15 scalars (`nameIndex`, `flags`, `numKeyframes`, `duration`, `priority`, `firstGroundFrame`, `numGroundFrames`, `baseRotation`, `baseTranslation`, `baseScale`, `baseObjectState`, `baseDecalState`, `firstTrigger`, `numTriggers`, `toolBegin`) + 6 `TSIntegerSet` objects (`rotationMatters`, `translationMatters`, `scaleMatters`, `visMatters`, `frameMatters`, `matFrameMatters`).

### 3.6 Optional Material List [Dump:898-935]
- Present if additional MsgPack objects remain after sequences:
  - `mat_list_size` (MsgPack `uint32`), followed per material by:
    - `name` (`str`), `flags` (`uint32`), `reflectMap` (`uint32`), `bumpMap` (`uint32`), `detailMap` (`uint32`), `detailScale` (`float`), `reflectionAmount` (`float`).

---

## 4. `TSDrawPrimitive` Encoding & Triangle Conversion

Each `TSDrawPrimitive` is 12 bytes (`<3i`: `start`, `numElements`, `matIndex`) [Dump:744-764], [T3D `tsMesh.h`]:
- `TYPE_MASK     = 0xC0000000`
  - `0x00000000`: **Triangles** — groups of 3 elements `(e[0], e[1], e[2]), (e[3], e[4], e[5]), ...` [T3D `tsMesh.cpp:387-401`]
  - `0x40000000`: **Strip** — alternating-winding triangle strip starting at `j = 2`; even `j` emits `(e[j-2], e[j-1], e[j])`, odd `j` emits `(e[j-1], e[j-2], e[j])`; skip degenerate triangles where any two indices are equal [T3D `tsMesh.cpp:405-426`]
  - `0x80000000`: **Fan** — `(e[0], e[j-1], e[j])` for `j = 2 .. numElements - 1` [T3D `tsMesh.cpp:1144`]
- `INDEXED       = 0x20000000`: if set, element `k` is `indices[start + k]`; if clear, element `k` is `start + k`.
- `NO_MATERIAL   = 0x10000000`: if set, primitive has no material assigned.
- `MATERIAL_MASK = 0x0FFFFFFF`: `matIndex & 0x0FFFFFFF` indexes into the shape's `materialList`.

---

## 5. Coordinate System, Quaternions, UVs, Colors & Normals

### 5.1 Coordinate System & Units
- Torque3D / BeamNG uses a **right-handed Z-up** coordinate system in meters, matching Blender directly (no axis swap needed for positions or normals) [T3D `colladaShapeLoader.cpp`].

### 5.2 `Quat16` & Quaternion Sign Convention
- `Quat16` stores 4 signed 16-bit integers `int16 x, y, z, w` (`<4h`, 8 bytes) scaled by `MAX_VAL = 0x7fff = 32767` [T3D `tsTransform.h:33-37`]:
  ```python
  qx, qy, qz, qw = x / 32767.0, y / 32767.0, z / 32767.0, w / 32767.0
  ```
- **Crucial sign convention** [T3D `mQuat.cpp:167-182`]: Torque3D's `QuatF::set(MatrixF)` computes `x = (m[1,2] - m[2,1]) * s`, `y = (m[2,0] - m[0,2]) * s`, `z = (m[0,1] - m[1,0]) * s`, which is the **conjugate** of standard right-handed column-vector quaternions used by Blender (`mathutils.Quaternion((w, x, y, z))`).
  - **CDAE → Blender**: `Quaternion((qw, -qx, -qy, -qz)).normalized()`
  - **Blender → CDAE**: `qx, qy, qz, qw = -q.x, -q.y, -q.z, q.w` quantized to `round(v * 32767)` clamped to `[-32767, 32767]`.

### 5.3 UV Coordinates (`tverts`, `tverts2`)
- Torque3D flips V when importing COLLADA (`uv.y = 1.0f - uv.y` for both `tverts` and `tverts2`) [T3D `colladaAppMesh.cpp:703, 749`].
- **CDAE → Blender**: `(u, 1.0 - v)`
- **Blender → CDAE**: `(u, 1.0 - v)`

### 5.4 Vertex Colors (`colors`)
- Torque3D's `ColorI` is 4 unsigned bytes in **RGBA** order (`U8 red, green, blue, alpha`, `<4B`) [T3D `color.h:142-148`], [Dump:720].
- Matches Blender's `BYTE_COLOR` `color_srgb` (`[r/255, g/255, b/255, a/255]`) directly.

### 5.5 Normals & `encodedNorms`
- Explicit per-vertex normals are stored in `mesh.norms` (`Point3F`, `<3f`).
- When `mesh.norms` is empty and `mesh.encodedNorms` (`uint8`) is present (or `UseEncodedNormals = 1 << 28` is set), each byte indexes Torque3D's 256-entry `TSMesh::smU8ToNormalTable` [T3D `tsMesh.cpp:1579-1837`].

### 5.6 Detail Levels (LODs) & Collision Meshes
- Each `TSShape::Detail` has `nameIndex`, `subShapeNum`, `objectDetailNum`, and `size` (pixel size; higher `size` = higher detail; negative `size` = collision / LOS detail levels, e.g., `collision-1`, `colmesh-1`, `Colbox-1`) [T3D `colladaShapeLoader.cpp:287-320`, `tsShape.h:284-293`].
- For a given `Detail` (`subShapeNum >= 0`, `objectDetailNum >= 0`), the objects belonging to `subShapeNum` are `objects[subShapeFirstObject[s] .. subShapeFirstObject[s] + subShapeNumObjects[s]]`, and each object's mesh at that detail level is `meshes[obj.startMeshIndex + detail.objectDetailNum]` (when `objectDetailNum < obj.numMeshes` and the mesh is not `NullMesh`).

### 5.7 GPU Vertex Splitting & Merge-by-Distance
- When Torque3D converts a `.dae` into a `TSMesh`, `ColladaAppMesh::getPrimitives()` builds a GPU-style `VertTupleMap` keyed on unique `(position, normal, uv0, uv1, color)` tuples [T3D `colladaAppMesh.cpp:505, 553-625`].
- As a result, vertices along hard edges, UV seams, and vertex-color discontinuities are duplicated in `verts[]` (and faces on opposite sides of a seam do not share vertex indices).
- On import, per-corner (`CORNER` / loop) UVs, vertex colors, and normals must be extracted from the unmerged indices **before** merging vertices by position (within each mesh only), so UV/color seams survive the merge and `sharp_edges_from_normals` can detect shared hard edges.

---

## 6. Dependencies & Bundling Plan (Phase 0.4)

- **MsgPack**: Pure-Python `io_scene_collada/cdae/msgpack_lite.py` implementing only the MsgPack types used by v31 (positive/negative fixint, `uint8/16/32/64`, `int8/16/32/64`, `bool`, `nil`, `float32/64`, `fixstr`/`str8/16/32`, `bin8/16/32`, `fixarray`/`array16/32`, `fixmap`/`map16/32`). Binary payloads (`pack_vector` data) are sliced as `memoryview` and read directly via `numpy.frombuffer`.
- **Zstandard (`zstandard==0.25.0`)**: Verified on PyPI (`https://pypi.org/pypi/zstandard/json`) — prebuilt wheels exist for **all 4 target platforms** and **both Blender Python ABIs** (`cp311` for Blender 5.0, `cp313` for Blender 5.1/5.3):
  - `windows-x64`: `zstandard-0.25.0-cp311-cp311-win_amd64.whl`, `zstandard-0.25.0-cp313-cp313-win_amd64.whl`
  - `linux-x64`: `zstandard-0.25.0-cp311-cp311-manylinux2014_x86_64.manylinux_2_17_x86_64.whl`, `zstandard-0.25.0-cp313-cp313-manylinux2014_x86_64.manylinux_2_17_x86_64.whl`
  - `macos-arm64`: `zstandard-0.25.0-cp311-cp311-macosx_11_0_arm64.whl`, `zstandard-0.25.0-cp313-cp313-macosx_11_0_arm64.whl`
  - `macos-x64`: `zstandard-0.25.0-cp311-cp311-macosx_10_9_x86_64.whl`, `zstandard-0.25.0-cp313-cp313-macosx_10_13_x86_64.whl`
- If `zstandard` is unavailable at runtime, reading a compressed `.cdae` reports a clear error (never a traceback), and writing uncompressed `.cdae` (`compression=False`) works without `zstandard`.

---

## 7. Items Pending Verification Against Real BeamNG `.cdae` Files

No local BeamNG.drive installation was found in default Steam paths (`C:/D:/E:`). Once real `.cdae` files are placed in `tests/_local_cdae/` (or pointed to via `CDAE_LOCAL_DIR`), Phase 1.5 and Phase 2.3 will verify:
1. Whether real BeamNG v31 files ever use `Point3F`/`Box3F` as MsgPack lists vs `bin`, or `TSIntegerSet` as `bytes` vs `list`.
2. Whether real BeamNG v31 files populate `encodedNorms` alongside `norms`, or leave `encodedNorms` empty.
3. Exact `Quat16` sign & `initialTransforms` (row-major vs column-major) match against a paired `.dae` + `.cdae` from BeamNG.
4. Where BeamNG writes cached `.cdae` files in the user folder (`%LOCALAPPDATA%/BeamNG.drive/<ver>/temp/` or alongside the `.dae`).
