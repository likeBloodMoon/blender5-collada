# SPDX-FileCopyrightText: 2009-2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Complete Collada (.dae) importer faithfully ported from Blender 4.5.1 for Blender 5.0+."""

from __future__ import annotations

import io
import math
import os
import re
import traceback
import urllib.parse
import xml.etree.ElementTree as ET
import xml.parsers.expat as _expat
import zipfile
from dataclasses import dataclass
from typing import TYPE_CHECKING

import bpy
import numpy as np
from mathutils import Matrix, Vector

from ._native_loader import native_parse_f32, native_parse_i32
from .beamng_materials import (
    alt_image_refs,
    lookup_material_color_maps,
    parse_beamng_materials_json,
)
from .collada_utils import (
    check_target_collection_visibility,
    create_fcurve_on_channelbag,
    dae_flat_to_matrix,
    ensure_action_and_channelbag,
    fov_to_focallength,
    set_editbone_property_matrix,
)

if TYPE_CHECKING:
    from bpy.types import Context, Material, Mesh, Object, Scene


MINIMUM_BONE_LENGTH = 0.000001
_MAX_ZIP_ENTRIES = 50_000
_MAX_ZIP_ENTRY_BYTES = 1 * 1024 * 1024 * 1024       # 1 GiB
_MAX_ZIP_TOTAL_BYTES = 4 * 1024 * 1024 * 1024       # 4 GiB
_ZIP_RATIO_THRESHOLD_BYTES = 50 * 1024 * 1024       # 50 MB
_MAX_ZIP_RATIO = 200.0
_DTD_CHUNK_BYTES = 65536


class _XmlPrologDone(Exception):
    pass


def _reject_dtd_expat(source: str | bytes) -> None:
    """Reject DOCTYPE / ENTITY / NOTATION declarations at the expat parser level.

    Feeds only the XML prolog (stopping as soon as the root element starts) so
    multi-megabyte COLLADA files incur sub-millisecond overhead while arbitrary-length
    leading comments, processing instructions, and encodings are handled accurately.
    """
    parser = _expat.ParserCreate()

    def _forbid(*_args, **_kwargs) -> None:
        raise ValueError("DTDs are not supported in COLLADA files")

    def _on_root(*_args, **_kwargs) -> None:
        raise _XmlPrologDone()

    parser.StartDoctypeDeclHandler = _forbid
    parser.EntityDeclHandler = _forbid
    parser.UnparsedEntityDeclHandler = _forbid
    parser.NotationDeclHandler = _forbid
    parser.StartElementHandler = _on_root

    try:
        if isinstance(source, (bytes, bytearray)):
            mv = memoryview(source)
            n = len(mv)
            for off in range(0, n, _DTD_CHUNK_BYTES):
                end = min(off + _DTD_CHUNK_BYTES, n)
                parser.Parse(mv[off:end], end == n)
            if n == 0:
                parser.Parse(b"", True)
        else:
            with open(source, "rb") as fh:
                while True:
                    chunk = fh.read(_DTD_CHUNK_BYTES)
                    if not chunk:
                        parser.Parse(b"", True)
                        break
                    parser.Parse(chunk, False)
    except _XmlPrologDone:
        return
    except _expat.ExpatError:
        # Let ET.parse report ordinary XML syntax errors uniformly.
        return


def _parse_xml_safe(source: str | bytes) -> ET.ElementTree:
    """Parse XML from a file path or bytes after rejecting DTD declarations via expat."""
    _reject_dtd_expat(source)
    if isinstance(source, (bytes, bytearray)):
        return ET.parse(io.BytesIO(source))
    return ET.parse(source)


@dataclass
class ImportSettings:
    filepath: str = ""
    import_units: bool = False
    custom_normals: bool = True
    fast_exact_normals: bool = False
    sharp_edges_from_normals: bool = False
    sharp_angle_threshold: float = math.radians(1.0)
    archive_mode: str = "ALL"  # "ALL" (all .dae into collections) or "MAIN" (single main .dae)
    node_filter: set[str] | None = None
    archive_entry_filter: list[str] | None = None
    find_chains: bool = False
    auto_connect: bool = False
    fix_orientation: bool = False
    min_chain_length: int = 0
    keep_bind_info: bool = False


def _strip_ns(tag: str) -> str:
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag


def _find_child(elem: ET.Element, name: str) -> ET.Element | None:
    for ch in elem:
        if _strip_ns(ch.tag) == name:
            return ch
    return None


def _find_children(elem: ET.Element, name: str) -> list[ET.Element]:
    return [ch for ch in elem if _strip_ns(ch.tag) == name]


_LEGACY_NONFINITE_RE = re.compile(r"[+-]?\d+\.#(?:QNAN|SNAN|IND|INF)\d*", re.IGNORECASE)


def _fromstring_f32_strict(text: str):
    """Parse whitespace-separated float32 values without relying on NumPy DeprecationWarning."""
    import numpy as np

    toks = text.split()
    if not toks:
        return np.empty(0, dtype=np.float32)
    # Validate last token explicitly: NumPy 1.26's fromstring stops mid-token on trailing
    # garbage (e.g. "1.0 2.0 3.0x" or "1.0 2.0 1.#QNAN") while still returning len(toks) items.
    float(toks[-1])
    arr = np.fromstring(text, sep=" ", dtype=np.float32)
    if len(arr) != len(toks):
        raise ValueError("Unmatched data in float array")
    return arr


def _fromstring_i32_strict(text: str):
    """Parse whitespace-separated int32 values without relying on NumPy DeprecationWarning."""
    import numpy as np

    toks = text.split()
    if not toks:
        return np.empty(0, dtype=np.int32)
    int(toks[-1])
    arr = np.fromstring(text, sep=" ", dtype=np.int32)
    if len(arr) != len(toks):
        raise ValueError("Unmatched data in integer array")
    return arr


def _parse_f32_np(text: str | None, warnings: list[str] | None = None, label: str = ""):
    import numpy as np

    if not text or not text.strip():
        return np.empty(0, dtype=np.float32)
    res = native_parse_f32(text)
    if res is not None:
        return res
    try:
        return _fromstring_f32_strict(text)
    except ValueError:
        if "#" in text:
            cleaned, count = _LEGACY_NONFINITE_RE.subn("0.0", text)
            if count > 0:
                try:
                    arr = _fromstring_f32_strict(cleaned)
                    if warnings is not None:
                        warnings.append(f"Replaced {count} legacy non-finite token(s) with 0.0 in {label}")
                    return arr
                except ValueError:
                    pass
        if warnings is not None:
            warnings.append(f"Invalid number in {label}")
        return None


def _parse_i32_np(text: str | None, warnings: list[str] | None = None, label: str = ""):
    import numpy as np

    if not text or not text.strip():
        return np.empty(0, dtype=np.int32)
    res = native_parse_i32(text)
    if res is not None:
        return res
    try:
        return _fromstring_i32_strict(text)
    except ValueError:
        if warnings is not None:
            warnings.append(f"Invalid integer in {label}")
        return None


def _safe_float(text: str | None, default: float = 0.0) -> float:
    if not text:
        return default
    try:
        v = float(text.strip())
        return v if math.isfinite(v) else default
    except ValueError:
        return default


def _safe_int(text: str | None, default: int = 0) -> int:
    if not text:
        return default
    try:
        return int(text.strip())
    except ValueError:
        return default


def _parse_floats(text: str | None) -> list[float]:
    if not text:
        return []
    toks = text.split()
    try:
        vals = [float(x) for x in toks]
    except ValueError:
        vals = []
        for x in toks:
            try:
                vals.append(float(x))
            except ValueError:
                vals.append(0.0)
    return [v if math.isfinite(v) else 0.0 for v in vals]


def _parse_ints(text: str | None) -> list[int]:
    if not text:
        return []
    toks = text.split()
    try:
        return [int(x) for x in toks]
    except ValueError:
        out: list[int] = []
        for x in toks:
            try:
                out.append(int(x))
            except ValueError:
                out.append(0)
        return out


def _parse_strings(text: str | None) -> list[str]:
    if not text:
        return []
    return text.split()


def _parse_extra_blender(elem: ET.Element) -> dict[str, str]:
    """Extract all <extra><technique profile="blender"> child elements as tag->text."""
    result: dict[str, str] = {}
    for extra in _find_children(elem, "extra"):
        for tech in _find_children(extra, "technique"):
            if tech.attrib.get("profile") == "blender":
                for ch in tech:
                    result[_strip_ns(ch.tag)] = ch.text or ""
    return result


def _uri_to_local_id(uri: str | None) -> str:
    if not uri:
        return ""
    if uri.startswith("#"):
        return uri[1:]
    return uri


def _uri_to_native_path(uri: str) -> str:
    if not uri:
        return ""
    if uri.startswith("file:///"):
        p = urllib.parse.unquote(uri[8:])
        if len(p) >= 2 and p[1] == ":":
            return p.replace("/", os.sep)
        return "/" + p
    if uri.startswith("file://"):
        return urllib.parse.unquote(uri[7:])
    return urllib.parse.unquote(uri)


def _norm_archive_path(raw: str) -> str:
    """Normalize a zip entry or URI path strictly within the archive namespace."""
    s = raw.replace("\\", "/")
    parts: list[str] = []
    for seg in s.split("/"):
        if not seg or seg == ".":
            continue
        if seg == "..":
            if parts:
                parts.pop()
            continue
        parts.append(seg)
    return "/".join(parts)


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


@dataclass
class _AnimChannelData:
    target: str
    times: list[float]
    values: list[float]
    stride: int
    interps: list[str]
    in_tangents: list[float]
    out_tangents: list[float]


class DocumentImporter:
    """Complete Collada (.dae) DocumentImporter ported from Blender 4.5.1."""

    def __init__(self, context: Context, settings: ImportSettings) -> None:
        self.context = context
        self.scene: Scene = context.scene
        self.view_layer = context.view_layer
        alc = context.view_layer.active_layer_collection
        self.target_collection = alc.collection if alc is not None else context.scene.collection
        self.settings = settings
        self.dae_dir = os.path.dirname(os.path.abspath(settings.filepath))

        self._archive_zf: zipfile.ZipFile | None = None
        self._archive_basename: str = ""
        self._archive_dae_dir: str = ""
        self._archive_entries: dict[str, zipfile.ZipInfo] = {}
        self._archive_entries_ci: dict[str, tuple[str, zipfile.ZipInfo]] = {}
        self._archive_by_basename: dict[str, list[tuple[str, zipfile.ZipInfo]]] = {}
        self._archive_images_by_entry: dict[str, object] = {}
        self._archive_bytes_read: int = 0
        self._beamng_mat_maps: dict[str, list[str]] = {}
        self.errors: list[str] = []
        self.warnings: list[str] = []

        self._reset_dae_state()

    def _reset_dae_state(self) -> None:
        """Reset per-DAE parsing and scene instantiation state (preserves archive caches)."""
        # Asset info
        self.up_axis: str = "Z_UP"
        self.unit_name: str = "meter"
        self.unit_meter: float = 1.0
        self.import_from_version: str = ""

        # Parsed library maps
        self.images_by_id: dict[str, bpy.types.Image] = {}
        self.effects_by_id: dict[str, ET.Element] = {}
        self.materials_by_id: dict[str, Material] = {}
        self.cameras_by_id: dict[str, bpy.types.Camera] = {}
        self.lights_by_id: dict[str, bpy.types.Light] = {}
        self.meshes_by_id: dict[str, Mesh] = {}
        self.mesh_mat_prims: dict[str, dict[str, np.ndarray]] = {}
        self.skins_by_id: dict[str, _SkinData] = {}
        self.morphs_by_id: dict[str, _MorphData] = {}
        self.lib_nodes_by_id: dict[str, ET.Element] = {}
        self.animations: list[_AnimChannelData] = []

        # Scene instantiation state
        self.node_elem_by_id: dict[str, ET.Element] = {}
        self.object_by_node_id: dict[str, Object] = {}
        self.object_by_geom_id: dict[str, Object] = {}
        self.imported_mesh_objects: list[Object] = []
        self.imported_objects_by_node_name: dict[str, list[Object]] = {}
        self.root_objects: list[Object] = []
        self._kept_nodes: set[int] | None = None
        self._allowed_geom_ids: set[str] | None = None
        self._allowed_mat_ids: set[str] | None = None
        self._allowed_img_ids: set[str] | None = None

        # Armature tracking
        self.root_joint_nodes: list[tuple[ET.Element, Object]] = []  # (joint_elem, ob_arm)
        self.joint_elem_by_sid: dict[str, ET.Element] = {}
        self.joint_elem_by_id: dict[str, ET.Element] = {}
        self.joint_arm_by_id: dict[str, Object] = {}
        self.joint_bone_name_by_id: dict[str, str] = {}
        self.joint_parent_elem_by_id: dict[str, ET.Element | None] = {}

    @staticmethod
    def _is_ignored_archive_entry(norm_name: str) -> bool:
        parts = norm_name.split("/")
        if any(p == "__MACOSX" for p in parts):
            return True
        return parts[-1].startswith("._")

    def _find_archive_daes(self, zf: zipfile.ZipFile) -> tuple[list[tuple[str, zipfile.ZipInfo]], bool]:
        """Discover .dae entries inside an open ZipFile.

        Returns (entries, from_manifest):
        - If manifest.xml specifies a valid <dae_root>, returns ([root], True).
        - Otherwise returns all .dae entries ranked so the one matching the archive
          name (and shallowest path) comes first, with deterministic alphabetical tie-breaking.
        """
        manifest_entries: list[tuple[str, zipfile.ZipInfo]] = []
        dae_entries: list[tuple[str, zipfile.ZipInfo]] = []
        for norm_name, info in sorted(self._archive_entries.items()):
            if self._is_ignored_archive_entry(norm_name):
                continue
            low = norm_name.lower()
            if low == "manifest.xml" or low.endswith("/manifest.xml"):
                manifest_entries.append((norm_name, info))
            elif low.endswith(".dae"):
                dae_entries.append((norm_name, info))

        for m_norm, m_info in manifest_entries:
            try:
                m_bytes = self._read_zip_entry(zf, m_info)
                mroot = _parse_xml_safe(m_bytes).getroot()
                dae_root_el = _find_child(mroot, "dae_root")
                if dae_root_el is not None and dae_root_el.text:
                    rel = dae_root_el.text.strip()
                    m_dir = m_norm.rsplit("/", 1)[0] if "/" in m_norm else ""
                    found = self._lookup_archive_path(m_dir, rel)
                    if found is not None:
                        return [found], True
            except ValueError:
                raise
            except Exception as e:
                self.warnings.append(f"Could not parse archive manifest.xml: {e}")

        zip_stem = os.path.splitext(self._archive_basename)[0].lower()

        def _rank(item: tuple[str, zipfile.ZipInfo]) -> tuple[int, int, str]:
            norm_name = item[0]
            bname = norm_name.rsplit("/", 1)[-1]
            stem = os.path.splitext(bname)[0].lower()
            exact_match = 0 if (zip_stem and stem == zip_stem) else 1
            depth = norm_name.count("/")
            return (exact_match, depth, norm_name.lower())

        dae_entries.sort(key=_rank)
        return dae_entries, False

    def _merge_beamng_mat_maps(self, parsed: dict[str, list[str]]) -> None:
        for k, maps in parsed.items():
            cur = self._beamng_mat_maps.setdefault(k, [])
            for m in maps:
                if m not in cur:
                    cur.append(m)

    def _load_archive_materials_json(self, zf: zipfile.ZipFile) -> None:
        mat_entries: list[tuple[str, zipfile.ZipInfo]] = []
        for norm_name, info in self._archive_entries.items():
            if self._is_ignored_archive_entry(norm_name):
                continue
            bname = norm_name.rsplit("/", 1)[-1].lower()
            if bname == "main.materials.json":
                mat_entries.append((norm_name, info))
        # Shallowest (closest to vehicle root) first, then alphabetical
        mat_entries.sort(key=lambda item: (item[0].count("/"), item[0].lower()))
        for norm_name, info in mat_entries:
            try:
                raw = self._read_zip_entry(zf, info)
                parsed = parse_beamng_materials_json(raw.decode("utf-8", errors="replace"))
                self._merge_beamng_mat_maps(parsed)
            except Exception as e:
                self.warnings.append(f"Could not parse '{norm_name}': {e}")

    def _load_disk_materials_json(self, base_dir: str) -> None:
        if not base_dir:
            return
        candidates = [os.path.join(base_dir, "main.materials.json")]
        parent = os.path.dirname(base_dir)
        if parent and parent != base_dir:
            candidates.append(os.path.join(parent, "main.materials.json"))
        for path in candidates:
            if not os.path.isfile(path):
                continue
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    parsed = parse_beamng_materials_json(f.read())
                self._merge_beamng_mat_maps(parsed)
            except Exception as e:
                self.warnings.append(f"Could not parse '{os.path.basename(path)}': {e}")

    def _read_zip_entry(self, zf: zipfile.ZipFile, info: zipfile.ZipInfo) -> bytes:
        file_size = info.file_size
        if file_size > _MAX_ZIP_ENTRY_BYTES:
            raise ValueError(
                f"Archive entry '{info.filename}' exceeds 1 GiB uncompressed limit ({file_size} bytes)"
            )
        if self._archive_bytes_read + file_size > _MAX_ZIP_TOTAL_BYTES:
            raise ValueError(
                f"Archive total uncompressed size exceeds 4 GiB limit at '{info.filename}'"
            )
        if file_size > _ZIP_RATIO_THRESHOLD_BYTES:
            ratio = file_size / max(info.compress_size, 1)
            if ratio > _MAX_ZIP_RATIO:
                raise ValueError(
                    f"Archive entry '{info.filename}' exceeds compression ratio limit "
                    f"({ratio:.1f}x > {_MAX_ZIP_RATIO:.0f}x for {file_size} bytes)"
                )
        data = zf.read(info)
        if len(data) > _MAX_ZIP_ENTRY_BYTES or self._archive_bytes_read + len(data) > _MAX_ZIP_TOTAL_BYTES:
            raise ValueError(f"Archive entry '{info.filename}' exceeded uncompressed size limit during read")
        self._archive_bytes_read += len(data)
        return data

    def _build_archive_index(self, zf: zipfile.ZipFile) -> None:
        infos = zf.infolist()
        if len(infos) > _MAX_ZIP_ENTRIES:
            raise ValueError(
                f"Archive contains {len(infos)} entries, exceeding the {_MAX_ZIP_ENTRIES}-entry limit"
            )
        self._archive_entries = {}
        self._archive_entries_ci = {}
        self._archive_by_basename = {}
        for info in sorted(infos, key=lambda i: i.filename):
            if info.is_dir():
                continue
            norm = _norm_archive_path(info.filename)
            if not norm:
                continue
            if norm not in self._archive_entries:
                self._archive_entries[norm] = info
            low = norm.lower()
            if low not in self._archive_entries_ci:
                self._archive_entries_ci[low] = (norm, info)
            bname_low = norm.rsplit("/", 1)[-1].lower()
            self._archive_by_basename.setdefault(bname_low, []).append((norm, info))

    def _lookup_archive_path(self, base_dir: str, rel_uri: str) -> tuple[str, zipfile.ZipInfo] | None:
        raw = urllib.parse.unquote(rel_uri.strip())
        if raw.lower().startswith("file:///"):
            raw = raw[8:]
        elif raw.lower().startswith("file://"):
            raw = raw[7:]
        raw = raw.replace("\\", "/")
        cand = _norm_archive_path(f"{base_dir}/{raw}" if base_dir else raw)
        if cand in self._archive_entries:
            return (cand, self._archive_entries[cand])
        ci = self._archive_entries_ci.get(cand.lower())
        if ci is not None:
            return ci
        cand2 = _norm_archive_path(raw)
        if cand2 in self._archive_entries:
            return (cand2, self._archive_entries[cand2])
        return self._archive_entries_ci.get(cand2.lower())

    def import_document(self) -> bool:
        ext = os.path.splitext(self.settings.filepath)[1].lower()
        if ext in (".zae", ".kmz", ".zip"):
            self._archive_basename = os.path.basename(self.settings.filepath)
            self._archive_images_by_entry = {}
            self._archive_bytes_read = 0
            self._beamng_mat_maps = {}
            base_collection = self.target_collection
            try:
                with zipfile.ZipFile(self.settings.filepath, "r") as zf:
                    self._archive_zf = zf
                    self._build_archive_index(zf)
                    dae_entries, from_manifest = self._find_archive_daes(zf)
                    if not dae_entries:
                        self.errors.append(
                            f"No .dae file found inside archive: {self._archive_basename}"
                        )
                        return False
                    self._load_archive_materials_json(zf)

                    if self.settings.archive_entry_filter is not None:
                        want_ci = {_norm_archive_path(p).lower() for p in self.settings.archive_entry_filter}
                        dae_entries = [e for e in dae_entries if e[0].lower() in want_ci]
                        from_manifest = False
                        if not dae_entries:
                            self.errors.append(
                                f"None of the selected .dae entries were found in '{self._archive_basename}'"
                            )
                            return False

                    if from_manifest or len(dae_entries) == 1 or self.settings.archive_mode == "MAIN":
                        if not from_manifest and len(dae_entries) > 1:
                            skipped = [n for n, _ in dae_entries[1:]]
                            preview = ", ".join(skipped[:4])
                            if len(skipped) > 4:
                                preview += f", ... (+{len(skipped) - 4} more)"
                            self.warnings.append(
                                f"Archive '{self._archive_basename}' contains {len(dae_entries)} .dae files; "
                                f"imported '{dae_entries[0][0]}' and skipped {len(skipped)} other(s) ({preview}). "
                                f"Set Archive Models to 'All models (collections)' to import all"
                            )
                        dae_norm, dae_info = dae_entries[0]
                        self._archive_dae_dir = dae_norm.rsplit("/", 1)[0] if "/" in dae_norm else ""
                        dae_bytes = self._read_zip_entry(zf, dae_info)
                        try:
                            tree = _parse_xml_safe(dae_bytes)
                        except Exception as e:
                            self.errors.append(f"Failed to parse XML: {e}")
                            return False
                        ok_main = self._safe_import_dae_tree(tree)
                        if ok_main:
                            vis_warn = check_target_collection_visibility(self.view_layer, base_collection)
                            if vis_warn:
                                self.warnings.append(vis_warn)
                        return ok_main

                    # Multi-DAE import ("ALL"): import every .dae into its own sub-collection
                    for ob in self.view_layer.objects:
                        ob.select_set(False)

                    stem_counts: dict[str, int] = {}
                    for dae_norm, _ in dae_entries:
                        st = os.path.splitext(dae_norm.rsplit("/", 1)[-1])[0].lower()
                        stem_counts[st] = stem_counts.get(st, 0) + 1

                    any_ok = False
                    for dae_norm, dae_info in dae_entries:
                        self._reset_dae_state()
                        self._archive_dae_dir = dae_norm.rsplit("/", 1)[0] if "/" in dae_norm else ""
                        dae_bytes = self._read_zip_entry(zf, dae_info)
                        try:
                            tree = _parse_xml_safe(dae_bytes)
                        except Exception as e:
                            self.warnings.append(f"Failed to parse XML in '{dae_norm}': {e}")
                            continue
                        bname = dae_norm.rsplit("/", 1)[-1]
                        stem = os.path.splitext(bname)[0] or "Model"
                        if stem_counts.get(stem.lower(), 0) > 1 and "/" in dae_norm:
                            parent_seg = dae_norm.rsplit("/", 2)[-2]
                            coll_name = f"{parent_seg}_{stem}"
                        else:
                            coll_name = stem
                        sub_coll = bpy.data.collections.new(coll_name)
                        base_collection.children.link(sub_coll)
                        self.target_collection = sub_coll
                        if self._safe_import_dae_tree(
                            tree,
                            deselect_existing=False,
                            entry_label=dae_norm,
                            as_warning=True,
                        ):
                            any_ok = True
                        elif len(sub_coll.objects) == 0 and len(sub_coll.children) == 0:
                            base_collection.children.unlink(sub_coll)
                            bpy.data.collections.remove(sub_coll)
                    if any_ok:
                        vis_warn = check_target_collection_visibility(self.view_layer, base_collection)
                        if vis_warn:
                            self.warnings.append(vis_warn)
                    return any_ok
            except Exception as e:
                self.errors.append(f"Archive import failed: {e}")
                return False
            finally:
                self.target_collection = base_collection
                self._archive_zf = None
                self._archive_entries = {}
                self._archive_entries_ci = {}
                self._archive_by_basename = {}
                self._archive_images_by_entry = {}
                self._archive_dae_dir = ""
                self._beamng_mat_maps = {}
        ok = self._import_dae_file(self.settings.filepath)
        if ok:
            vis_warn = check_target_collection_visibility(self.view_layer, self.target_collection)
            if vis_warn:
                self.warnings.append(vis_warn)
        return ok

    def _safe_import_dae_tree(
        self,
        tree: ET.ElementTree,
        *,
        deselect_existing: bool = True,
        entry_label: str | None = None,
        as_warning: bool = False,
    ) -> bool:
        """Run ``_import_dae_tree`` with clean error reporting and partial-datablock cleanup."""
        pre_objects = set(bpy.data.objects)
        pre_meshes = set(bpy.data.meshes)
        pre_armatures = set(bpy.data.armatures)
        pre_cameras = set(bpy.data.cameras)
        pre_lights = set(bpy.data.lights)
        pre_materials = set(bpy.data.materials)
        pre_images = set(bpy.data.images)
        pre_actions = set(bpy.data.actions)
        pre_collections = set(bpy.data.collections)
        try:
            return self._import_dae_tree(tree, deselect_existing=deselect_existing)
        except Exception as exc:
            traceback.print_exc()
            # Clean up any objects / datablocks partially created before the failure
            for ob in list(bpy.data.objects):
                if ob not in pre_objects:
                    try:
                        bpy.data.objects.remove(ob, do_unlink=True)
                    except Exception:
                        pass
            for data_coll, pre_set in (
                (bpy.data.meshes, pre_meshes),
                (bpy.data.armatures, pre_armatures),
                (bpy.data.cameras, pre_cameras),
                (bpy.data.lights, pre_lights),
                (bpy.data.materials, pre_materials),
                (bpy.data.images, pre_images),
                (bpy.data.actions, pre_actions),
                (bpy.data.collections, pre_collections),
            ):
                for item in list(data_coll):
                    if item not in pre_set:
                        try:
                            data_coll.remove(item)
                        except Exception:
                            pass
            msg = f"Import failed for '{entry_label}': {exc}" if entry_label else f"Import failed: {exc}"
            if as_warning:
                self.warnings.append(msg)
            else:
                self.errors.append(msg)
            return False

    def _import_dae_file(self, dae_path: str) -> bool:
        self._beamng_mat_maps = {}
        self._load_disk_materials_json(self.dae_dir)
        try:
            tree = _parse_xml_safe(dae_path)
        except Exception as e:
            self.errors.append(f"Failed to parse XML: {e}")
            return False
        return self._safe_import_dae_tree(tree)

    def _compute_node_filter_plan(self, root: ET.Element, node_filter: set[str]) -> None:
        """Pre-scan visual scenes to determine kept nodes, geometries, materials, and images."""
        lib_nodes: dict[str, ET.Element] = {}
        for lib in _find_children(root, "library_nodes"):
            for n in _find_children(lib, "node"):
                nid = n.attrib.get("id", "")
                if nid:
                    lib_nodes[nid] = n

        kept_nodes: set[int] = set()
        kept_geoms: set[str] = set()
        kept_ctrls: set[str] = set()
        kept_mats: set[str] = set()

        def _collect_bindings(inst_el: ET.Element) -> None:
            bm = _find_child(inst_el, "bind_material")
            if bm is None:
                return
            tc = _find_child(bm, "technique_common")
            if tc is None:
                return
            for im in _find_children(tc, "instance_material"):
                tid = _uri_to_local_id(im.attrib.get("target"))
                if tid:
                    kept_mats.add(tid)

        def _walk_node(node: ET.Element, visiting: set[int]) -> bool:
            key = id(node)
            if key in visiting:
                return False
            visiting.add(key)
            try:
                node_name = node.attrib.get("name") or node.attrib.get("id") or ""
                node_id = node.attrib.get("id", "")
                direct_match = (node_name in node_filter) or (bool(node_id) and node_id in node_filter)
                if not direct_match:
                    for ig in _find_children(node, "instance_geometry"):
                        iname = ig.attrib.get("name", "")
                        if iname and iname in node_filter:
                            direct_match = True
                            break

                child_match = False
                for ch in _find_children(node, "node"):
                    if _walk_node(ch, visiting):
                        child_match = True
                for ino in _find_children(node, "instance_node"):
                    src_n = lib_nodes.get(_uri_to_local_id(ino.attrib.get("url")))
                    if src_n is not None and _walk_node(src_n, visiting):
                        child_match = True

                if direct_match or child_match:
                    kept_nodes.add(key)
                    if direct_match:
                        for ig in _find_children(node, "instance_geometry"):
                            gid = _uri_to_local_id(ig.attrib.get("url"))
                            if gid:
                                kept_geoms.add(gid)
                            _collect_bindings(ig)
                        for ic in _find_children(node, "instance_controller"):
                            cid = _uri_to_local_id(ic.attrib.get("url"))
                            if cid:
                                kept_ctrls.add(cid)
                            _collect_bindings(ic)
                    return True
                return False
            finally:
                visiting.discard(key)

        for lib in _find_children(root, "library_visual_scenes"):
            for vscene in _find_children(lib, "visual_scene"):
                for node in _find_children(vscene, "node"):
                    _walk_node(node, set())

        # Resolve geometry IDs referenced by kept controllers (skin / morph)
        if kept_ctrls:
            for lib in _find_children(root, "library_controllers"):
                for ctrl in _find_children(lib, "controller"):
                    cid = ctrl.attrib.get("id", "")
                    if cid not in kept_ctrls:
                        continue
                    skin = _find_child(ctrl, "skin")
                    if skin is not None:
                        gid = _uri_to_local_id(skin.attrib.get("source"))
                        if gid:
                            kept_geoms.add(gid)
                    morph = _find_child(ctrl, "morph")
                    if morph is not None:
                        gid = _uri_to_local_id(morph.attrib.get("source"))
                        if gid:
                            kept_geoms.add(gid)

        # Resolve image IDs referenced by kept materials -> effects
        kept_effs: set[str] = set()
        for lib in _find_children(root, "library_materials"):
            for mat_elem in _find_children(lib, "material"):
                if mat_elem.attrib.get("id", "") in kept_mats:
                    ie = _find_child(mat_elem, "instance_effect")
                    if ie is not None:
                        eid = _uri_to_local_id(ie.attrib.get("url"))
                        if eid:
                            kept_effs.add(eid)
        kept_imgs: set[str] = set()
        for lib in _find_children(root, "library_effects"):
            for eff_elem in _find_children(lib, "effect"):
                if eff_elem.attrib.get("id", "") not in kept_effs:
                    continue
                profile = _find_child(eff_elem, "profile_COMMON")
                if profile is None:
                    continue
                surface_to_img: dict[str, str] = {}
                sampler_to_surface: dict[str, str] = {}
                for np_el in _find_children(profile, "newparam"):
                    sid = np_el.attrib.get("sid", "")
                    surf = _find_child(np_el, "surface")
                    if surf is not None:
                        init_f = _find_child(surf, "init_from")
                        if init_f is not None and init_f.text:
                            img_ref = init_f.text.strip()
                            kept_imgs.add(img_ref)
                            if sid:
                                surface_to_img[sid] = img_ref
                    samp = _find_child(np_el, "sampler2D")
                    if samp is not None:
                        src = _find_child(samp, "source")
                        if src is not None and src.text and sid:
                            sampler_to_surface[sid] = src.text.strip()
                # Also keep images referenced directly via <texture texture="img_id"/>
                for el in profile.iter():
                    if _strip_ns(el.tag) == "texture":
                        tex_ref = el.attrib.get("texture", "").strip()
                        if tex_ref:
                            surf_ref = sampler_to_surface.get(tex_ref, tex_ref)
                            img_ref = surface_to_img.get(surf_ref, surf_ref)
                            if img_ref:
                                kept_imgs.add(img_ref)

        self._kept_nodes = kept_nodes
        self._allowed_geom_ids = kept_geoms
        self._allowed_mat_ids = kept_mats
        self._allowed_img_ids = kept_imgs

    def _import_dae_tree(self, tree: ET.ElementTree, *, deselect_existing: bool = True) -> bool:

        root = tree.getroot()
        if _strip_ns(root.tag) != "COLLADA":
            self.errors.append("Root element is not <COLLADA>")
            return False

        # Deselect all existing objects
        if deselect_existing:
            for ob in self.view_layer.objects:
                ob.select_set(False)

        if self.settings.node_filter is not None:
            self._compute_node_filter_plan(root, self.settings.node_filter)
        else:
            self._kept_nodes = None
            self._allowed_geom_ids = None
            self._allowed_mat_ids = None
            self._allowed_img_ids = None

        # 1. Asset
        asset_elem = _find_child(root, "asset")
        if asset_elem is not None:
            self._read_asset(asset_elem)

        # 2. Libraries
        for lib in _find_children(root, "library_images"):
            self._read_library_images(lib)
        for lib in _find_children(root, "library_effects"):
            self._read_library_effects(lib)
        for lib in _find_children(root, "library_materials"):
            self._read_library_materials(lib)
        for lib in _find_children(root, "library_cameras"):
            self._read_library_cameras(lib)
        for lib in _find_children(root, "library_lights"):
            self._read_library_lights(lib)
        for lib in _find_children(root, "library_geometries"):
            self._read_library_geometries(lib)
        for lib in _find_children(root, "library_controllers"):
            self._read_library_controllers(lib)
        for lib in _find_children(root, "library_nodes"):
            for n in _find_children(lib, "node"):
                nid = n.attrib.get("id", "")
                if nid:
                    self.lib_nodes_by_id[nid] = n
        for lib in _find_children(root, "library_animations"):
            self._read_library_animations(lib)

        # 3. Apply units if requested
        if self.settings.import_units:
            us = self.scene.unit_settings
            if self.unit_name in ("millimeter", "centimeter", "decimeter", "meter", "kilometer"):
                us.system = "METRIC"
            elif self.unit_name in ("inch", "foot", "yard"):
                us.system = "IMPERIAL"
            else:
                us.system = "NONE"
            us.scale_length = self.unit_meter

        # 4. Visual Scenes
        for lib in _find_children(root, "library_visual_scenes"):
            for vscene in _find_children(lib, "visual_scene"):
                for node in _find_children(vscene, "node"):
                    self._process_node(node, None, None)

        # 5. Optimize material assignments (move identical object mats to mesh data)
        self._optimize_material_assignments()

        # 6. Build Armatures, link skins, build shape keys
        self._make_armatures()
        self._make_shape_keys()

        # 7. Import Animations
        self._apply_animations()

        # 8. Match scale & up-axis rotation on root objects
        self._apply_unit_and_up_axis()

        self.view_layer.update()
        return True

    # ------------------------------------------------------------------
    # <asset>
    # ------------------------------------------------------------------

    def _read_asset(self, asset: ET.Element) -> None:
        unit = _find_child(asset, "unit")
        if unit is not None:
            self.unit_name = unit.attrib.get("name", "meter").lower()
            try:
                self.unit_meter = float(unit.attrib.get("meter", "1.0"))
            except ValueError:
                self.unit_meter = 1.0
        up = _find_child(asset, "up_axis")
        if up is not None and up.text:
            self.up_axis = up.text.strip().upper()
        contrib = _find_child(asset, "contributor")
        if contrib is not None:
            tool = _find_child(contrib, "authoring_tool")
            if tool is not None and tool.text and tool.text.startswith("Blender "):
                parts = tool.text[len("Blender "):].split(" ", 1)
                if parts:
                    self.import_from_version = parts[0]

    def _get_up_axis_matrix(self) -> Matrix:
        if self.up_axis == "X_UP":
            return Matrix.Rotation(-math.pi / 2.0, 4, "Y")
        if self.up_axis == "Y_UP":
            return Matrix.Rotation(math.pi / 2.0, 4, "X")
        return Matrix.Identity(4)

    def _get_unit_scale_matrix(self) -> Matrix:
        us = self.scene.unit_settings
        bl_scale = 1.0 if us.system == "NONE" else us.scale_length
        factor = self.unit_meter / bl_scale if bl_scale else 1.0
        return Matrix.Diagonal((factor, factor, factor, 1.0))

    def _apply_unit_and_up_axis(self) -> None:
        rot_m = self._get_up_axis_matrix()
        scale_m = self._get_unit_scale_matrix() if not self.settings.import_units else Matrix.Identity(4)
        if rot_m == Matrix.Identity(4) and scale_m == Matrix.Identity(4):
            return
        for ob in self.root_objects:
            if ob.parent is None:
                mat = rot_m @ scale_m @ ob.matrix_basis
                ob.matrix_basis = mat

    # ------------------------------------------------------------------
    # <library_images>
    # ------------------------------------------------------------------

    def _resolve_archive_image_exact(self, raw_uri: str) -> tuple[str, zipfile.ZipInfo] | None:
        """Resolve a single <init_from> URI strictly against entries inside the open archive."""
        raw = urllib.parse.unquote(raw_uri.strip())
        if raw.lower().startswith("file:///"):
            raw = raw[8:]
        elif raw.lower().startswith("file://"):
            raw = raw[7:]
        raw = raw.replace("\\", "/")
        bname = raw.rsplit("/", 1)[-1]
        dae_dir = self._archive_dae_dir

        def _try(path_str: str) -> tuple[str, zipfile.ZipInfo] | None:
            norm = _norm_archive_path(path_str)
            if not norm:
                return None
            if norm in self._archive_entries:
                return (norm, self._archive_entries[norm])
            return self._archive_entries_ci.get(norm.lower())

        # 1. Exact path relative to root .dae's folder, then archive root
        candidates: list[str] = []
        if dae_dir:
            candidates.append(f"{dae_dir}/{raw}")
        candidates.append(raw)

        # 2. Basename next to .dae and standard texture subfolders
        if bname:
            if dae_dir:
                candidates.append(f"{dae_dir}/{bname}")
            candidates.append(bname)
            for sub in ("textures", "Textures", "images", "Images", "maps"):
                if dae_dir:
                    candidates.append(f"{dae_dir}/{sub}/{bname}")
                candidates.append(f"{sub}/{bname}")

        for cand in candidates:
            hit = _try(cand)
            if hit is not None:
                return hit

        # 3. Basename anywhere in the archive (deterministic sorted order)
        if bname:
            matches = self._archive_by_basename.get(bname.lower())
            if matches:
                return matches[0]
        return None

    def _resolve_archive_image(self, raw_uri: str) -> tuple[str, zipfile.ZipInfo] | None:
        """Resolve an image URI inside the open archive, with .dds/.png extension fallback."""
        for ref in alt_image_refs(raw_uri):
            hit = self._resolve_archive_image_exact(ref)
            if hit is not None:
                return hit
        return None

    def _resolve_image_path_exact(self, raw_uri: str) -> str | None:
        native = _uri_to_native_path(raw_uri)
        # Normalize embedded backslashes so Windows-authored DAEs resolve on Linux/macOS
        norm = native.replace("\\", os.sep)
        bname = os.path.basename(norm)
        parent_dir = os.path.dirname(self.dae_dir)

        candidates = [
            os.path.join(self.dae_dir, native),
            os.path.join(self.dae_dir, norm),
            native,
            norm,
            os.path.join(self.dae_dir, bname),
            os.path.join(self.dae_dir, "textures", bname),
            os.path.join(self.dae_dir, "Textures", bname),
            os.path.join(self.dae_dir, "images", bname),
            os.path.join(self.dae_dir, "Images", bname),
            os.path.join(self.dae_dir, "maps", bname),
        ]
        if parent_dir and parent_dir != self.dae_dir:
            candidates.extend((
                os.path.join(parent_dir, bname),
                os.path.join(parent_dir, "textures", bname),
            ))
        for cand in candidates:
            if cand and os.path.isfile(cand):
                return cand
        return None

    def _resolve_image_path(self, raw_uri: str) -> str | None:
        for ref in alt_image_refs(raw_uri):
            hit = self._resolve_image_path_exact(ref)
            if hit is not None:
                return hit
        return None

    def _load_archive_image(self, raw_ref: str, *, warn_missing: bool = True):
        hit = self._resolve_archive_image(raw_ref)
        if hit is None:
            if warn_missing:
                bname = raw_ref.replace("\\", "/").rsplit("/", 1)[-1]
                self.warnings.append(f"Texture not found in archive: {bname or raw_ref}")
            return None
        norm_entry, info = hit
        cached = self._archive_images_by_entry.get(norm_entry)
        if cached is not None:
            return cached
        assert self._archive_zf is not None
        data = self._read_zip_entry(self._archive_zf, info)
        if not data:
            self.warnings.append(f"Archive texture is empty: {norm_entry}")
            return None
        name = norm_entry.rsplit("/", 1)[-1] or "Image"
        img = None
        try:
            img = bpy.data.images.new(name, 8, 8)
            img.filepath = f"//{self._archive_basename}/{norm_entry}"
            img.pack(data=data, data_len=len(data))
            img.source = "FILE"
            # Trigger decode and verify pixels/has_data
            px_len = len(img.pixels)
            if not img.has_data or px_len == 0:
                raise RuntimeError("image data could not be decoded")
            self._archive_images_by_entry[norm_entry] = img
            return img
        except Exception as e:
            self.warnings.append(f"Could not decode archive texture '{norm_entry}': {e}")
            if img is not None:
                try:
                    bpy.data.images.remove(img)
                except Exception:
                    pass
            return None

    def _read_library_images(self, lib: ET.Element) -> None:
        for img_elem in _find_children(lib, "image"):
            img_id = img_elem.attrib.get("id", "")
            if self._allowed_img_ids is not None and img_id not in self._allowed_img_ids:
                continue
            init_from = _find_child(img_elem, "init_from")
            if init_from is None or not init_from.text:
                continue
            raw_ref = init_from.text.strip()
            if self._archive_zf is not None:
                ima = self._load_archive_image(raw_ref)
                if ima is not None:
                    self.images_by_id[img_id] = ima
                continue

            workpath = self._resolve_image_path(raw_ref)
            if workpath is None:
                self.warnings.append(f"Missing texture: {os.path.basename(_uri_to_native_path(raw_ref)) or raw_ref}")
                continue
            try:
                ima = bpy.data.images.load(workpath, check_existing=True)
                self.images_by_id[img_id] = ima
            except Exception as e:
                self.warnings.append(f"Cannot load image {os.path.basename(workpath)}: {e}")

    # ------------------------------------------------------------------
    # <library_effects> & <library_materials>
    # ------------------------------------------------------------------

    def _read_library_effects(self, lib: ET.Element) -> None:
        for eff in _find_children(lib, "effect"):
            eid = eff.attrib.get("id", "")
            if eid:
                self.effects_by_id[eid] = eff

    def _read_library_materials(self, lib: ET.Element) -> None:
        for mat_elem in _find_children(lib, "material"):
            mat_id = mat_elem.attrib.get("id", "")
            if self._allowed_mat_ids is not None and mat_id not in self._allowed_mat_ids:
                continue
            mat_name = mat_elem.attrib.get("name") or mat_id
            ma = bpy.data.materials.new(name=mat_name)
            self.materials_by_id[mat_id] = ma

            inst_eff = _find_child(mat_elem, "instance_effect")
            if inst_eff is not None:
                eff_id = _uri_to_local_id(inst_eff.attrib.get("url"))
                eff_elem = self.effects_by_id.get(eff_id)
                if eff_elem is not None:
                    self._apply_effect_to_material(eff_elem, ma)

            if self._beamng_mat_maps:
                self._try_apply_beamng_material_texture(ma, mat_name, mat_id)

    def _try_apply_beamng_material_texture(self, ma: Material, mat_name: str, mat_id: str) -> None:
        """If a material has no Base Color texture yet, look up colorMap/baseColorMap from main.materials.json."""
        refs = lookup_material_color_maps(self._beamng_mat_maps, mat_name, mat_id)
        if not refs:
            return

        ntree = ma.node_tree
        shader_node = None
        if ntree is not None:
            for node in ntree.nodes:
                if node.type == "BSDF_PRINCIPLED":
                    shader_node = node
                    break
        if shader_node is not None:
            bc_sock = shader_node.inputs.get("Base Color")
            if bc_sock is not None and bc_sock.is_linked:
                return

        ima = None
        for ref in refs:
            if self._archive_zf is not None:
                ima = self._load_archive_image(ref, warn_missing=False)
            else:
                workpath = self._resolve_image_path(ref)
                if workpath is not None:
                    try:
                        ima = bpy.data.images.load(workpath, check_existing=True)
                    except Exception:
                        ima = None
            if ima is not None:
                break
        if ima is None:
            return

        if ntree is None:
            ma.use_nodes = True
            ntree = ma.node_tree
        if shader_node is None:
            for node in ntree.nodes:
                if node.type == "BSDF_PRINCIPLED":
                    shader_node = node
                    break
        if shader_node is None:
            shader_node = ntree.nodes.new("ShaderNodeBsdfPrincipled")
            shader_node.location = (0, 300)
            output_node = next((n for n in ntree.nodes if n.type == "OUTPUT_MATERIAL"), None)
            if output_node is None:
                output_node = ntree.nodes.new("ShaderNodeOutputMaterial")
                output_node.location = (300, 300)
            ntree.links.new(shader_node.outputs[0], output_node.inputs[0])

        bc_sock = shader_node.inputs.get("Base Color")
        if bc_sock is not None and not bc_sock.is_linked:
            tn = ntree.nodes.new("ShaderNodeTexImage")
            tn.label = "Base Color"
            tn.location = (-300, 300)
            tn.image = ima
            ntree.links.new(tn.outputs[0], bc_sock)

    def _apply_effect_to_material(self, eff_elem: ET.Element, ma: Material) -> None:
        profile = _find_child(eff_elem, "profile_COMMON")
        if profile is None:
            return

        # Parse <newparam> surface & sampler2D mappings
        surface_to_img: dict[str, str] = {}
        sampler_to_surface: dict[str, str] = {}
        for np_el in _find_children(profile, "newparam"):
            sid = np_el.attrib.get("sid", "")
            surf = _find_child(np_el, "surface")
            if surf is not None:
                init_from = _find_child(surf, "init_from")
                if init_from is not None and init_from.text:
                    surface_to_img[sid] = init_from.text.strip()
            samp = _find_child(np_el, "sampler2D")
            if samp is not None:
                src = _find_child(samp, "source")
                if src is not None and src.text:
                    sampler_to_surface[sid] = src.text.strip()

        def resolve_texture_image(tex_elem: ET.Element):
            tex_ref = tex_elem.attrib.get("texture", "")
            surf_ref = sampler_to_surface.get(tex_ref, tex_ref)
            img_id = surface_to_img.get(surf_ref, surf_ref)
            return self.images_by_id.get(img_id)

        tech = _find_child(profile, "technique")
        if tech is None:
            return
        shader_elem = None
        for stype in ("lambert", "phong", "blinn", "constant"):
            shader_elem = _find_child(tech, stype)
            if shader_elem is not None:
                break
        if shader_elem is None:
            return

        # Build Principled BSDF node tree (matching Materials.cpp)
        ntree = ma.node_tree
        if ntree is None:
            ma.use_nodes = True
            ntree = ma.node_tree
        ntree.nodes.clear()
        shader_node = ntree.nodes.new("ShaderNodeBsdfPrincipled")
        shader_node.location = (0, 300)
        output_node = ntree.nodes.new("ShaderNodeOutputMaterial")
        output_node.location = (300, 300)
        ntree.links.new(shader_node.outputs[0], output_node.inputs[0])

        extra_node_count = 0

        def add_tex_node(img, label: str):
            nonlocal extra_node_count
            locy = -300 * extra_node_count
            extra_node_count += 1
            tn = ntree.nodes.new("ShaderNodeTexImage")
            tn.label = label
            tn.location = (-300, locy)
            tn.image = img
            return tn

        def add_rgb_node(col: list[float], label: str):
            nonlocal extra_node_count
            locy = -300 * extra_node_count
            extra_node_count += 1
            rn = ntree.nodes.new("ShaderNodeRGB")
            rn.label = label
            rn.location = (-300, locy)
            rn.outputs[0].default_value = (col[0], col[1], col[2], 1.0)
            return rn

        # Diffuse
        diff = _find_child(shader_elem, "diffuse")
        bc_sock = shader_node.inputs.get("Base Color")
        if diff is not None:
            tex = _find_child(diff, "texture")
            col_el = _find_child(diff, "color")
            if tex is not None:
                ima = resolve_texture_image(tex)
                if ima is not None and bc_sock is not None:
                    tn = add_tex_node(ima, "Base Color")
                    ntree.links.new(tn.outputs[0], bc_sock)
            elif col_el is not None:
                c = _parse_floats(col_el.text)
                if len(c) >= 3:
                    a = c[3] if len(c) >= 4 else 1.0
                    if bc_sock is not None:
                        bc_sock.default_value = (c[0], c[1], c[2], a)
                    ma.diffuse_color = (c[0], c[1], c[2], a)
        else:
            if bc_sock is not None:
                bc_sock.default_value = (0.0, 0.0, 0.0, 1.0)
            ma.diffuse_color = (0.0, 0.0, 0.0, 1.0)

        # Emission
        em = _find_child(shader_elem, "emission")
        if em is not None:
            tex = _find_child(em, "texture")
            col_el = _find_child(em, "color")
            ec_sock = shader_node.inputs.get("Emission Color")
            if tex is not None:
                ima = resolve_texture_image(tex)
                if ima is not None and ec_sock is not None:
                    tn = add_tex_node(ima, "Emission Color")
                    ntree.links.new(tn.outputs["Color"], ec_sock)
            elif col_el is not None and ec_sock is not None:
                c = _parse_floats(col_el.text)
                if len(c) >= 3:
                    a = c[3] if len(c) >= 4 else 1.0
                    ec_sock.default_value = (c[0], c[1], c[2], a)
            # Match Blender 4.5.1 Materials.cpp:353-356 where Emission Strength
            # remained at default 0.0 on imported Principled BSDF nodes.

        # IOR
        ior_el = _find_child(shader_elem, "index_of_refraction")
        if ior_el is not None:
            fl = _find_child(ior_el, "float")
            if fl is not None and fl.text:
                ior_val = _safe_float(fl.text, -1.0)
                if ior_val >= 0.0:
                    ior_sock = shader_node.inputs.get("IOR")
                    if ior_sock is not None:
                        ior_sock.default_value = ior_val

        # Alpha / Transparency (Collada 1.4.1 page 7-5)
        trans_el = _find_child(shader_elem, "transparent")
        transp_el = _find_child(shader_elem, "transparency")
        if trans_el is not None or transp_el is not None:
            opaque_mode = trans_el.attrib.get("opaque", "A_ONE") if trans_el is not None else "A_ONE"
            tex = _find_child(trans_el, "texture") if trans_el is not None else None
            if tex is not None:
                ima = resolve_texture_image(tex)
                if ima is not None:
                    add_tex_node(ima, "Alpha")
            else:
                t_alpha = 1.0
                if trans_el is not None:
                    col_el = _find_child(trans_el, "color")
                    if col_el is not None:
                        c = _parse_floats(col_el.text)
                        if len(c) >= 4:
                            t_alpha = c[3]
                tr_alpha = 1.0
                if transp_el is not None:
                    fl = _find_child(transp_el, "float")
                    if fl is not None and fl.text:
                        val = _safe_float(fl.text, -1.0)
                        if val >= 0.0:
                            tr_alpha = val
                alpha = t_alpha * tr_alpha
                if opaque_mode == "RGB_ZERO":
                    alpha = 1.0 - alpha
                a_sock = shader_node.inputs.get("Alpha")
                if a_sock is not None:
                    a_sock.default_value = alpha
                ma.diffuse_color = (ma.diffuse_color[0], ma.diffuse_color[1], ma.diffuse_color[2], alpha)

        # Reflectivity -> Metallic
        refl_el = _find_child(shader_elem, "reflectivity")
        if refl_el is not None:
            fl = _find_child(refl_el, "float")
            if fl is not None and fl.text:
                rval = _safe_float(fl.text, -1.0)
                if rval >= 0.0:
                    met_sock = shader_node.inputs.get("Metallic")
                    if met_sock is not None:
                        met_sock.default_value = rval
                    ma.metallic = rval

        # Specular
        spec_el = _find_child(shader_elem, "specular")
        has_spec = False
        if spec_el is not None:
            tex = _find_child(spec_el, "texture")
            col_el = _find_child(spec_el, "color")
            if tex is not None:
                ima = resolve_texture_image(tex)
                if ima is not None:
                    add_tex_node(ima, "Specular IOR Level")
                    has_spec = True
            elif col_el is not None:
                c = _parse_floats(col_el.text)
                if len(c) >= 3 and (c[0] != 0.0 or c[1] != 0.0 or c[2] != 0.0):
                    add_rgb_node(c, "Specular IOR Level")
                    has_spec = True
        if not has_spec:
            spec_sock = shader_node.inputs.get("Specular IOR Level")
            if spec_sock is not None:
                spec_sock.default_value = 0.0

        # Ambient
        amb_el = _find_child(shader_elem, "ambient")
        if amb_el is not None:
            col_el = _find_child(amb_el, "color")
            if col_el is not None:
                c = _parse_floats(col_el.text)
                if len(c) >= 3:
                    add_rgb_node(c, "Ambient")

    # ------------------------------------------------------------------
    # <library_cameras>
    # ------------------------------------------------------------------

    def _read_library_cameras(self, lib: ET.Element) -> None:
        for cam_elem in _find_children(lib, "camera"):
            cam_id = cam_elem.attrib.get("id", "")
            cam_name = cam_elem.attrib.get("name") or cam_id
            cam = bpy.data.cameras.new(name=cam_name)

            extras = _parse_extra_blender(cam_elem)
            if "shiftx" in extras:
                cam.shift_x = _safe_float(extras["shiftx"])
            if "shifty" in extras:
                cam.shift_y = _safe_float(extras["shifty"])
            if "dof_distance" in extras:
                cam.dof.focus_distance = _safe_float(extras["dof_distance"])

            optics = _find_child(cam_elem, "optics")
            if optics is not None:
                tc = _find_child(optics, "technique_common")
                if tc is not None:
                    persp = _find_child(tc, "perspective")
                    ortho = _find_child(tc, "orthographic")
                    if persp is not None:
                        cam.type = "PERSP"
                        zn = _find_child(persp, "znear")
                        zf = _find_child(persp, "zfar")
                        if zn is not None and zn.text:
                            cam.clip_start = max(1e-6, _safe_float(zn.text, cam.clip_start))
                        if zf is not None and zf.text:
                            cam.clip_end = max(1e-6, _safe_float(zf.text, cam.clip_end))
                        xfov_el = _find_child(persp, "xfov")
                        yfov_el = _find_child(persp, "yfov")
                        ar_el = _find_child(persp, "aspect_ratio")
                        if xfov_el is not None and xfov_el.text:
                            xfv = _safe_float(xfov_el.text, 0.0)
                            if 0.0 < xfv < 180.0:
                                cam.lens = fov_to_focallength(math.radians(xfv), cam.sensor_width)
                        elif yfov_el is not None and yfov_el.text:
                            yfv = _safe_float(yfov_el.text, 0.0)
                            if 0.0 < yfv < 180.0:
                                yfov = math.radians(yfv)
                                if ar_el is not None and ar_el.text:
                                    aspect = _safe_float(ar_el.text, 1.0)
                                    if aspect > 0.0:
                                        xfov = 2.0 * math.atan(aspect * math.tan(yfov * 0.5))
                                        cam.lens = fov_to_focallength(xfov, cam.sensor_width)
                                else:
                                    cam.lens = fov_to_focallength(yfov, cam.sensor_width)
                    elif ortho is not None:
                        cam.type = "ORTHO"
                        zn = _find_child(ortho, "znear")
                        zf = _find_child(ortho, "zfar")
                        if zn is not None and zn.text:
                            cam.clip_start = max(1e-6, _safe_float(zn.text, cam.clip_start))
                        if zf is not None and zf.text:
                            cam.clip_end = max(1e-6, _safe_float(zf.text, cam.clip_end))
                        xmag_el = _find_child(ortho, "xmag")
                        ymag_el = _find_child(ortho, "ymag")
                        ar_el = _find_child(ortho, "aspect_ratio")
                        if xmag_el is not None and xmag_el.text:
                            cam.ortho_scale = max(1e-6, _safe_float(xmag_el.text, 3.0) * 2.0)
                        elif ymag_el is not None and ymag_el.text:
                            ymag = _safe_float(ymag_el.text, 3.0)
                            if ar_el is not None and ar_el.text:
                                cam.ortho_scale = max(1e-6, 2.0 * ymag * _safe_float(ar_el.text, 1.0))
                            else:
                                cam.ortho_scale = max(1e-6, ymag)

            self.cameras_by_id[cam_id] = cam

    # ------------------------------------------------------------------
    # <library_lights>
    # ------------------------------------------------------------------

    def _read_library_lights(self, lib: ET.Element) -> None:
        int_to_type = {0: "POINT", 1: "SUN", 2: "SPOT", 4: "AREA"}
        int_to_shape = {0: "SQUARE", 1: "RECTANGLE", 4: "DISK", 5: "ELLIPSE"}

        for la_elem in _find_children(lib, "light"):
            la_id = la_elem.attrib.get("id", "")
            la_name = la_elem.attrib.get("name") or la_id
            extras = _parse_extra_blender(la_elem)

            if extras and "type" in extras:
                la_type = int_to_type.get(_safe_int(extras["type"], 0), "POINT")
                la = bpy.data.lights.new(name=la_name, type=la_type)
                la_key = la.name
                la.color = (
                    _safe_float(extras.get("red"), 1.0),
                    _safe_float(extras.get("green"), 1.0),
                    _safe_float(extras.get("blue"), 1.0),
                )
                if "energy" in extras:
                    la.energy = _safe_float(extras["energy"], la.energy)
                if "mode" in extras:
                    mode = _safe_int(extras["mode"], 0)
                    la.use_shadow = bool(mode & 1)
                    if hasattr(la, "normalize"):
                        la.normalize = not bool(mode & (1 << 22))
                if "radius" in extras and hasattr(la, "shadow_soft_size"):
                    la.shadow_soft_size = _safe_float(extras["radius"], la.shadow_soft_size)
                if "clipend" in extras and hasattr(la, "cutoff_distance"):
                    la.cutoff_distance = _safe_float(extras["clipend"], la.cutoff_distance)
                # Match DocumentImporter.cpp:999-1008: write spot & area DNA fields
                # regardless of final light type so round-trips preserve DNA values.
                if "spotsize" in extras or "spotblend" in extras:
                    if la.type != "SPOT":
                        la.type = "SPOT"
                        la = bpy.data.lights[la_key]
                    if "spotsize" in extras:
                        la.spot_size = math.radians(_safe_float(extras["spotsize"], 45.0))
                    if "spotblend" in extras:
                        la.spot_blend = _safe_float(extras["spotblend"], la.spot_blend)
                if "area_shape" in extras or "area_size" in extras or "area_sizey" in extras:
                    if la.type != "AREA":
                        la.type = "AREA"
                        la = bpy.data.lights[la_key]
                    if "area_shape" in extras:
                        la.shape = int_to_shape.get(_safe_int(extras["area_shape"], 1), "RECTANGLE")
                    if "area_size" in extras:
                        la.size = _safe_float(extras["area_size"], la.size)
                    if "area_sizey" in extras and la.shape in ("RECTANGLE", "ELLIPSE"):
                        la.size_y = _safe_float(extras["area_sizey"], la.size_y)
                if la.type != la_type:
                    la.type = la_type
                    la = bpy.data.lights[la_key]
            else:
                tc = _find_child(la_elem, "technique_common")
                la_type = "POINT"
                sub = None
                if tc is not None:
                    for tag, btype in (
                        ("directional", "SUN"),
                        ("spot", "SPOT"),
                        ("point", "POINT"),
                        ("ambient", "SUN"),
                    ):
                        sub = _find_child(tc, tag)
                        if sub is not None:
                            la_type = btype
                            break
                la = bpy.data.lights.new(name=la_name, type=la_type)
                la.energy = 1.0
                if sub is not None:
                    col_el = _find_child(sub, "color")
                    if col_el is not None:
                        c = _parse_floats(col_el.text)
                        if len(c) >= 3:
                            la.color = (c[0], c[1], c[2])
                    if la_type == "SPOT":
                        fa = _find_child(sub, "falloff_angle")
                        fe = _find_child(sub, "falloff_exponent")
                        if fa is not None and fa.text:
                            la.spot_size = math.radians(_safe_float(fa.text, 45.0))
                        if fe is not None and fe.text:
                            la.spot_blend = _safe_float(fe.text, la.spot_blend)

            self.lights_by_id[la_id] = la

    # ------------------------------------------------------------------
    # <library_geometries>
    # ------------------------------------------------------------------

    def _read_library_geometries(self, lib: ET.Element) -> None:
        for geom_elem in _find_children(lib, "geometry"):
            geom_id = geom_elem.attrib.get("id", "")
            if self._allowed_geom_ids is not None and geom_id not in self._allowed_geom_ids:
                continue
            geom_name = geom_elem.attrib.get("name") or geom_id
            mesh_elem = _find_child(geom_elem, "mesh")
            if mesh_elem is None:
                continue
            self._read_mesh(geom_id, geom_name, mesh_elem)

    def _read_mesh(self, geom_id: str, geom_name: str, mesh_elem: ET.Element) -> None:
        import numpy as np

        # Parse all <source> elements with C-speed parser
        sources: dict[str, tuple[np.ndarray, int, str]] = {}
        for src in _find_children(mesh_elem, "source"):
            sid = src.attrib.get("id", "")
            sname = src.attrib.get("name") or sid
            fa = _find_child(src, "float_array")
            if fa is not None:
                fa_id = fa.attrib.get("id", sid)
                vals = _parse_f32_np(
                    fa.text,
                    self.warnings,
                    f"<float_array id='{fa_id}'> of geometry '{geom_name}'",
                )
                if vals is None:
                    return
            else:
                vals = np.empty(0, dtype=np.float32)
            stride = 3
            tc = _find_child(src, "technique_common")
            if tc is not None:
                acc = _find_child(tc, "accessor")
                if acc is not None and "stride" in acc.attrib:
                    try:
                        stride = int(acc.attrib["stride"])
                    except ValueError:
                        self.warnings.append(
                            f"Invalid stride '{acc.attrib['stride']}' in source '{sid}' of geometry '{geom_name}'"
                        )
                        stride = 0
            if stride <= 0:
                self.warnings.append(
                    f"Invalid stride {stride} in source '{sid}' of geometry '{geom_name}'"
                )
                vals = np.empty(0, dtype=np.float32)
                stride = 3
            sources[sid] = (vals, stride, sname)

        # Parse <vertices>
        verts_elem = _find_child(mesh_elem, "vertices")
        pos_source_id = ""
        vert_normal_src_id = ""
        if verts_elem is not None:
            for inp in _find_children(verts_elem, "input"):
                sem = inp.attrib.get("semantic", "")
                if sem == "POSITION":
                    pos_source_id = _uri_to_local_id(inp.attrib.get("source"))
                elif sem == "NORMAL":
                    vert_normal_src_id = _uri_to_local_id(inp.attrib.get("source"))

        pos_vals, pos_stride, _ = sources.get(pos_source_id, (np.empty(0, dtype=np.float32), 3, ""))
        if pos_stride <= 0:
            pos_stride = 3
        elif pos_vals.size > 0 and pos_stride < 3:
            self.warnings.append(
                f"Geometry '{geom_name}': POSITION source '{pos_source_id}' has stride {pos_stride} < 3; zero-padding missing axes"
            )
        num_verts = pos_vals.size // pos_stride
        if pos_vals.size > 0 and not np.isfinite(pos_vals).all():
            pos_vals = np.where(np.isfinite(pos_vals), pos_vals, np.float32(0.0))
            self.warnings.append(f"Geometry '{geom_name}': replaced non-finite vertex positions with 0.0")
        warned_bad_strides: set[tuple[str, str]] = set()

        # Collect per-primitive vectorized chunks
        vcount_chunks: list[np.ndarray] = []
        loop_vert_chunks: list[np.ndarray] = []
        face_sharp_chunks: list[np.ndarray] = []
        loop_norm_chunks: list[np.ndarray] = []
        has_all_loop_normals = True
        uv_chunks: dict[str, list[np.ndarray]] = {}
        uv_set_order: list[str] = []
        col_chunks: dict[str, list[np.ndarray]] = {}
        col_set_order: list[str] = []
        loose_edges: list[tuple[int, int]] = []
        mat_sym_order: list[str] = []
        mat_sym_to_id: dict[str, int] = {}
        face_mat_chunks: list[np.ndarray] = []
        total_faces = 0
        total_loops = 0
        dropped_faces_total = 0
        invalid_vcount_total = 0

        def extract_vcolname(src_id: str, src_name: str) -> str:
            if src_name and src_name != src_id:
                return src_name
            idx = src_id.find("-mesh-colors-")
            if idx != -1:
                return src_id[idx + 13:]
            return src_id

        for child in mesh_elem:
            tag = _strip_ns(child.tag)
            if tag not in ("triangles", "polylist", "polygons", "trifans", "tristrips", "lines"):
                continue

            inputs: list[tuple[str, str, int, int]] = []
            max_offset = 0
            for inp in _find_children(child, "input"):
                sem = inp.attrib.get("semantic", "")
                src_id = _uri_to_local_id(inp.attrib.get("source"))
                try:
                    off = int(inp.attrib.get("offset", "0"))
                except ValueError:
                    self.warnings.append(
                        f"Invalid input offset '{inp.attrib.get('offset')}' in geometry '{geom_name}'"
                    )
                    continue
                if off < 0:
                    self.warnings.append(
                        f"Negative input offset {off} in geometry '{geom_name}'"
                    )
                    continue
                try:
                    iset = int(inp.attrib.get("set", "0"))
                except ValueError:
                    iset = 0
                inputs.append((sem, src_id, off, iset))
                if off > max_offset:
                    max_offset = off
            num_inputs = max_offset + 1

            vert_off = -1
            norm_off = -1
            norm_src_id = vert_normal_src_id
            uv_inputs: list[tuple[str, int]] = []
            col_inputs: list[tuple[str, str, int]] = []

            for sem, src_id, off, iset in inputs:
                if sem == "VERTEX":
                    vert_off = off
                elif sem == "NORMAL":
                    norm_off = off
                    norm_src_id = src_id
                elif sem == "TEXCOORD":
                    if src_id not in uv_chunks:
                        uv_chunks[src_id] = (
                            [np.zeros((total_loops, 2), dtype=np.float32)] if total_loops > 0 else []
                        )
                        uv_set_order.append(src_id)
                    uv_inputs.append((src_id, off))
                elif sem == "COLOR":
                    sname = sources[src_id][2] if src_id in sources else src_id
                    cname = extract_vcolname(src_id, sname)
                    if cname not in col_chunks:
                        col_chunks[cname] = (
                            [np.ones((total_loops, 4), dtype=np.float32)] if total_loops > 0 else []
                        )
                        col_set_order.append(cname)
                    col_inputs.append((cname, src_id, off))

            if vert_off < 0:
                continue

            if tag == "lines":
                p_el = _find_child(child, "p")
                if p_el is not None and p_el.text:
                    p_arr = _parse_i32_np(p_el.text, self.warnings, f"<lines>/<p> of geometry '{geom_name}'")
                    if p_arr is None:
                        continue
                    cnt = p_arr.size // (2 * num_inputs)
                    if cnt > 0 and num_verts > 0:
                        p2 = p_arr[:cnt * 2 * num_inputs].reshape(cnt, 2, num_inputs)
                        e0 = p2[:, 0, vert_off]
                        e1 = p2[:, 1, vert_off]
                        e_ok = (e0 >= 0) & (e0 < num_verts) & (e1 >= 0) & (e1 < num_verts) & (e0 != e1)
                        if e_ok.any():
                            loose_edges.extend(zip(e0[e_ok].tolist(), e1[e_ok].tolist()))
                continue

            # Parse primitive into (prim_vcounts, prim_p[n_loops, num_inputs])
            prim_vcounts: np.ndarray | None = None
            prim_p: np.ndarray | None = None

            if tag == "triangles":
                p_el = _find_child(child, "p")
                if p_el is not None and p_el.text:
                    p_raw = _parse_i32_np(p_el.text, self.warnings, f"<triangles>/<p> of geometry '{geom_name}'")
                    if p_raw is None:
                        continue
                    tri_count = p_raw.size // (3 * num_inputs)
                    if tri_count > 0:
                        prim_p = p_raw[:tri_count * 3 * num_inputs].reshape(tri_count * 3, num_inputs)
                        prim_vcounts = np.full(tri_count, 3, dtype=np.int32)
            elif tag == "polylist":
                vc_el = _find_child(child, "vcount")
                p_el = _find_child(child, "p")
                if vc_el is not None and vc_el.text:
                    vc_raw = _parse_i32_np(vc_el.text, self.warnings, f"<polylist>/<vcount> of geometry '{geom_name}'")
                    if vc_raw is None or vc_raw.size == 0:
                        continue
                    if p_el is None or not p_el.text:
                        n_lt3 = int((vc_raw < 3).sum())
                        invalid_vcount_total += n_lt3
                        if n_lt3 < vc_raw.size:
                            self.warnings.append(
                                f"Geometry '{geom_name}': <polylist> has empty <p> with non-zero <vcount>; skipped"
                            )
                        continue
                    p_raw = _parse_i32_np(p_el.text, self.warnings, f"<polylist>/<p> of geometry '{geom_name}'")
                    if p_raw is None:
                        continue
                    n_tuples = p_raw.size // num_inputs
                    p_2d = p_raw[:n_tuples * num_inputs].reshape(n_tuples, num_inputs)
                    if bool((vc_raw >= 3).all()):
                        need = int(vc_raw.sum(dtype=np.int64))
                        if need <= n_tuples:
                            prim_vcounts = vc_raw
                            prim_p = p_2d[:need]
                        else:
                            # vcount sum exceeds <p>; keep only complete polygons
                            cs = np.cumsum(vc_raw, dtype=np.int64)
                            valid_n = int(np.searchsorted(cs, n_tuples, side="right"))
                            if valid_n > 0:
                                prim_vcounts = vc_raw[:valid_n]
                                prim_p = p_2d[:int(cs[valid_n - 1])]
                            self.warnings.append(
                                f"Geometry '{geom_name}': <polylist> vcount sum ({need}) exceeds <p> tuples ({n_tuples}); truncated"
                            )
                    else:
                        valid_vc: list[int] = []
                        valid_rows: list[np.ndarray] = []
                        cursor = 0
                        truncated = False
                        for vc in vc_raw.tolist():
                            if vc < 3:
                                invalid_vcount_total += 1
                                cursor += max(vc, 0)
                            elif cursor + vc <= n_tuples:
                                valid_vc.append(vc)
                                valid_rows.append(p_2d[cursor:cursor + vc])
                                cursor += vc
                            else:
                                truncated = True
                                cursor += vc
                        if truncated:
                            self.warnings.append(
                                f"Geometry '{geom_name}': <polylist> vcount exceeds <p> tuples ({n_tuples}); truncated"
                            )
                        if valid_vc:
                            prim_vcounts = np.asarray(valid_vc, dtype=np.int32)
                            prim_p = np.vstack(valid_rows)
            elif tag == "polygons":
                p_list: list[np.ndarray] = []
                vc_list: list[int] = []
                p_elems = list(_find_children(child, "p"))
                for ph_el in _find_children(child, "ph"):
                    pe = _find_child(ph_el, "p")
                    if pe is not None:
                        p_elems.append(pe)
                for p_el in p_elems:
                    if not p_el.text:
                        invalid_vcount_total += 1
                        continue
                    pr = _parse_i32_np(p_el.text, self.warnings, f"<polygons>/<p> of geometry '{geom_name}'")
                    if pr is None:
                        continue
                    vc = pr.size // num_inputs
                    if vc >= 3:
                        vc_list.append(vc)
                        p_list.append(pr[:vc * num_inputs].reshape(vc, num_inputs))
                    else:
                        invalid_vcount_total += 1
                if vc_list:
                    prim_vcounts = np.asarray(vc_list, dtype=np.int32)
                    prim_p = np.vstack(p_list)
            elif tag == "trifans":
                tri_rows: list[np.ndarray] = []
                tri_total = 0
                for p_el in _find_children(child, "p"):
                    if p_el.text:
                        pr = _parse_i32_np(p_el.text, self.warnings, f"<trifans>/<p> of geometry '{geom_name}'")
                        if pr is None:
                            continue
                        vc = pr.size // num_inputs
                        if vc >= 3:
                            p2 = pr[:vc * num_inputs].reshape(vc, num_inputs)
                            idx = np.empty((vc - 2) * 3, dtype=np.int32)
                            idx[0::3] = 0
                            idx[1::3] = np.arange(1, vc - 1, dtype=np.int32)
                            idx[2::3] = np.arange(2, vc, dtype=np.int32)
                            tri_rows.append(p2[idx])
                            tri_total += vc - 2
                if tri_total > 0:
                    prim_vcounts = np.full(tri_total, 3, dtype=np.int32)
                    prim_p = np.vstack(tri_rows)
            elif tag == "tristrips":
                tri_rows = []
                tri_total = 0
                for p_el in _find_children(child, "p"):
                    if p_el.text:
                        pr = _parse_i32_np(p_el.text, self.warnings, f"<tristrips>/<p> of geometry '{geom_name}'")
                        if pr is None:
                            continue
                        vc = pr.size // num_inputs
                        if vc >= 3:
                            p2 = pr[:vc * num_inputs].reshape(vc, num_inputs)
                            nt = vc - 2
                            ks = np.arange(2, vc, dtype=np.int32)
                            even = (ks % 2) == 0
                            idx = np.empty(nt * 3, dtype=np.int32)
                            idx[0::3] = np.where(even, ks - 2, ks - 1)
                            idx[1::3] = np.where(even, ks - 1, ks - 2)
                            idx[2::3] = ks
                            tri_rows.append(p2[idx])
                            tri_total += nt
                if tri_total > 0:
                    prim_vcounts = np.full(tri_total, 3, dtype=np.int32)
                    prim_p = np.vstack(tri_rows)

            if (
                prim_vcounts is None
                or prim_p is None
                or prim_vcounts.size == 0
                or prim_p.shape[0] == 0
                or int(prim_vcounts.sum(dtype=np.int64)) != int(prim_p.shape[0])
            ):
                continue

            # Vectorized bounds check: drop only faces with out-of-range VERTEX/NORMAL/TEXCOORD/COLOR indices
            norm_vals, norm_stride, _ = sources.get(norm_src_id, (np.empty(0, dtype=np.float32), 3, ""))
            if norm_stride < 3:
                if (norm_off >= 0 or vert_normal_src_id) and ("NORMAL", norm_src_id) not in warned_bad_strides:
                    warned_bad_strides.add(("NORMAL", norm_src_id))
                    self.warnings.append(
                        f"Geometry '{geom_name}': NORMAL source '{norm_src_id}' has stride {norm_stride} < 3; skipping normals"
                    )
                n_norm_elems = 0
                has_norm_src = False
            else:
                n_norm_elems = norm_vals.size // norm_stride
                has_norm_src = (norm_off >= 0 or bool(vert_normal_src_id)) and n_norm_elems > 0

            v_col = prim_p[:, vert_off]
            loop_ok = (v_col >= 0) & (v_col < num_verts)
            if has_norm_src:
                n_col = prim_p[:, norm_off] if norm_off >= 0 else v_col
                loop_ok &= (n_col >= 0) & (n_col < n_norm_elems)
            for u_id, u_off in uv_inputs:
                u_vals_chk, u_stride_chk, _ = sources.get(u_id, (np.empty(0, dtype=np.float32), 2, ""))
                if u_stride_chk < 2:
                    if ("TEXCOORD", u_id) not in warned_bad_strides:
                        warned_bad_strides.add(("TEXCOORD", u_id))
                        self.warnings.append(
                            f"Geometry '{geom_name}': TEXCOORD source '{u_id}' has stride {u_stride_chk} < 2; skipping UV values"
                        )
                    continue
                n_u_chk = u_vals_chk.size // u_stride_chk
                if n_u_chk > 0:
                    u_col = prim_p[:, u_off]
                    loop_ok &= (u_col >= 0) & (u_col < n_u_chk)
            for _cn, c_src_id, c_off in col_inputs:
                c_vals_chk, c_stride_chk, _ = sources.get(c_src_id, (np.empty(0, dtype=np.float32), 3, ""))
                if c_stride_chk < 3:
                    if ("COLOR", c_src_id) not in warned_bad_strides:
                        warned_bad_strides.add(("COLOR", c_src_id))
                        self.warnings.append(
                            f"Geometry '{geom_name}': COLOR source '{c_src_id}' has stride {c_stride_chk} < 3; skipping color values"
                        )
                    continue
                n_c_chk = c_vals_chk.size // c_stride_chk
                if n_c_chk > 0:
                    c_col = prim_p[:, c_off]
                    loop_ok &= (c_col >= 0) & (c_col < n_c_chk)

            if not bool(loop_ok.all()):
                if tag in ("triangles", "trifans", "tristrips") or bool((prim_vcounts == 3).all()):
                    face_ok = loop_ok.reshape(-1, 3).all(axis=1)
                else:
                    starts_chk = np.cumsum(prim_vcounts, dtype=np.int64) - prim_vcounts
                    face_ok = np.logical_and.reduceat(loop_ok, starts_chk)
                dropped = int((~face_ok).sum())
                dropped_faces_total += dropped
                if not bool(face_ok.any()):
                    continue
                loop_keep = np.repeat(face_ok, prim_vcounts)
                prim_vcounts = prim_vcounts[face_ok]
                prim_p = prim_p[loop_keep]
                if prim_vcounts.size == 0 or prim_p.shape[0] == 0:
                    continue

            prim_face_count = int(prim_vcounts.size)
            prim_loop_count = int(prim_p.shape[0])
            mat_sym = child.attrib.get("material", "")
            sym_id = mat_sym_to_id.get(mat_sym)
            if sym_id is None:
                sym_id = len(mat_sym_order)
                mat_sym_to_id[mat_sym] = sym_id
                mat_sym_order.append(mat_sym)
            face_mat_chunks.append(np.full(prim_face_count, sym_id, dtype=np.int32))

            l_verts = prim_p[:, vert_off]
            vcount_chunks.append(prim_vcounts)
            loop_vert_chunks.append(l_verts)

            # Normals (vectorized)
            if has_norm_src:
                n_src = norm_vals[:n_norm_elems * norm_stride].reshape(n_norm_elems, norm_stride)[:, :3]
                n_idx = prim_p[:, norm_off] if norm_off >= 0 else l_verts
                p_norms = n_src[n_idx].astype(np.float64)
                if not np.isfinite(p_norms).all():
                    p_norms = np.where(np.isfinite(p_norms), p_norms, 0.0)
                nlens = np.linalg.norm(p_norms, axis=1, keepdims=True)
                nmask = nlens[:, 0] > 0.0
                p_norms[nmask] /= nlens[nmask]
                # Determine flat faces (all loop normals in face have dot product ~1.0 with loop 0)
                if tag in ("triangles", "trifans", "tristrips") or bool((prim_vcounts == 3).all()):
                    n3 = p_norms.reshape(prim_face_count, 3, 3)
                    dp1 = (n3[:, 0] * n3[:, 1]).sum(axis=1)
                    dp2 = (n3[:, 0] * n3[:, 2]).sum(axis=1)
                    is_flat = (dp1 >= 0.99999) & (dp1 <= 1.00001) & (dp2 >= 0.99999) & (dp2 <= 1.00001)
                else:
                    starts = np.cumsum(prim_vcounts, dtype=np.int64) - prim_vcounts
                    if p_norms.shape[0] > 0 and starts.size > 0:
                        first_n = np.repeat(p_norms[starts], prim_vcounts, axis=0)
                        dots = (p_norms * first_n).sum(axis=1)
                        ok = (dots >= 0.99999) & (dots <= 1.00001)
                        is_flat = np.logical_and.reduceat(ok, starts)
                    else:
                        is_flat = np.ones(prim_face_count, dtype=bool)
                face_sharp_chunks.append(is_flat)
                loop_norm_chunks.append(p_norms.astype(np.float32))
            else:
                face_sharp_chunks.append(np.ones(prim_face_count, dtype=bool))
                has_all_loop_normals = False

            # UVs (vectorized)
            cur_uv_srcs = {s for s, _ in uv_inputs}
            for u_id in uv_set_order:
                if u_id not in cur_uv_srcs:
                    uv_chunks[u_id].append(np.zeros((prim_loop_count, 2), dtype=np.float32))
            for u_id, u_off in uv_inputs:
                u_vals, u_stride, _ = sources.get(u_id, (np.empty(0, dtype=np.float32), 2, ""))
                if u_stride <= 0:
                    u_stride = 2
                n_u = u_vals.size // u_stride
                if n_u > 0 and u_stride >= 2:
                    u_src = u_vals[:n_u * u_stride].reshape(n_u, u_stride)[:, :2]
                    u_idx = np.clip(prim_p[:, u_off], 0, n_u - 1)
                    uv_chunks[u_id].append(u_src[u_idx])
                else:
                    uv_chunks[u_id].append(np.zeros((prim_loop_count, 2), dtype=np.float32))

            # Colors (vectorized)
            cur_col_names = {cn for cn, _, _ in col_inputs}
            for cn in col_set_order:
                if cn not in cur_col_names:
                    col_chunks[cn].append(np.ones((prim_loop_count, 4), dtype=np.float32))
            for cn, c_src_id, c_off in col_inputs:
                c_vals, c_stride, _ = sources.get(c_src_id, (np.empty(0, dtype=np.float32), 3, ""))
                if c_stride <= 0:
                    c_stride = 3
                n_c = c_vals.size // c_stride
                if n_c > 0 and c_stride >= 3:
                    c_src = c_vals[:n_c * c_stride].reshape(n_c, c_stride)
                    c_idx = np.clip(prim_p[:, c_off], 0, n_c - 1)
                    if c_stride >= 4:
                        col_chunks[cn].append(c_src[c_idx, :4])
                    else:
                        rgb = c_src[c_idx, :3]
                        rgba = np.ones((prim_loop_count, 4), dtype=np.float32)
                        rgba[:, :3] = rgb
                        col_chunks[cn].append(rgba)
                else:
                    col_chunks[cn].append(np.ones((prim_loop_count, 4), dtype=np.float32))

            total_faces += prim_face_count
            total_loops += prim_loop_count

        if invalid_vcount_total > 0:
            self.warnings.append(
                f"Geometry '{geom_name}': {invalid_vcount_total} invalid faces with fewer than 3 vertices skipped"
            )
        if dropped_faces_total > 0:
            self.warnings.append(
                f"Geometry '{geom_name}': dropped {dropped_faces_total} face(s) with out-of-range indices"
            )

        # Concatenate per-face and per-loop arrays and drop degenerate/duplicate faces
        # BEFORE building the Blender mesh so UVs, vertex colors, and custom normals
        # on all remaining faces are preserved instead of wiped by me.validate().
        all_vcounts: np.ndarray | None = None
        all_loop_verts: np.ndarray | None = None
        all_face_mats: np.ndarray | None = None
        all_face_sharp: np.ndarray | None = None
        all_loop_normals = (
            (np.vstack(loop_norm_chunks) if len(loop_norm_chunks) > 1 else loop_norm_chunks[0])
            if (has_all_loop_normals and loop_norm_chunks)
            else None
        )
        uv_arrays: dict[str, np.ndarray] = {}
        for u_id in uv_set_order:
            chunks = uv_chunks[u_id]
            if chunks:
                uv_arrays[u_id] = np.vstack(chunks) if len(chunks) > 1 else chunks[0]
        col_arrays: dict[str, np.ndarray] = {}
        for cn in col_set_order:
            chunks = col_chunks[cn]
            if chunks:
                col_arrays[cn] = np.vstack(chunks) if len(chunks) > 1 else chunks[0]

        if total_faces > 0:
            all_vcounts = np.concatenate(vcount_chunks) if len(vcount_chunks) > 1 else vcount_chunks[0]
            all_loop_verts = np.concatenate(loop_vert_chunks) if len(loop_vert_chunks) > 1 else loop_vert_chunks[0]
            all_face_mats = np.concatenate(face_mat_chunks) if len(face_mat_chunks) > 1 else face_mat_chunks[0]
            if face_sharp_chunks:
                all_face_sharp = np.concatenate(face_sharp_chunks) if len(face_sharp_chunks) > 1 else face_sharp_chunks[0]

            keep_face: np.ndarray | None = None
            n_degen = 0
            n_dups = 0
            if bool((all_vcounts == 3).all()):
                tris = all_loop_verts.reshape(total_faces, 3)
                non_degen = (tris[:, 0] != tris[:, 1]) & (tris[:, 1] != tris[:, 2]) & (tris[:, 0] != tris[:, 2])
                n_degen = int((~non_degen).sum())
                valid_idx = np.flatnonzero(non_degen)
                if valid_idx.size > 0:
                    s_tris = np.ascontiguousarray(np.sort(tris[valid_idx], axis=1))
                    s_view = s_tris.view(np.dtype((np.void, s_tris.dtype.itemsize * 3))).ravel()
                    _, first_pos = np.unique(s_view, return_index=True)
                    n_dups = int(valid_idx.size - first_pos.size)
                    if n_degen > 0 or n_dups > 0:
                        keep_face = np.zeros(total_faces, dtype=bool)
                        keep_face[valid_idx[first_pos]] = True
                elif n_degen > 0:
                    keep_face = np.zeros(total_faces, dtype=bool)
            elif bool((all_vcounts == 4).all()):
                quads = all_loop_verts.reshape(total_faces, 4)
                s_all = np.ascontiguousarray(np.sort(quads, axis=1))
                non_degen = (
                    (s_all[:, 0] != s_all[:, 1])
                    & (s_all[:, 1] != s_all[:, 2])
                    & (s_all[:, 2] != s_all[:, 3])
                )
                n_degen = int((~non_degen).sum())
                valid_idx = np.flatnonzero(non_degen)
                if valid_idx.size > 0:
                    s_valid = np.ascontiguousarray(s_all[valid_idx])
                    s_view = s_valid.view(np.dtype((np.void, s_valid.dtype.itemsize * 4))).ravel()
                    _, first_pos = np.unique(s_view, return_index=True)
                    n_dups = int(valid_idx.size - first_pos.size)
                    if n_degen > 0 or n_dups > 0:
                        keep_face = np.zeros(total_faces, dtype=bool)
                        keep_face[valid_idx[first_pos]] = True
                elif n_degen > 0:
                    keep_face = np.zeros(total_faces, dtype=bool)
            else:
                kf = np.ones(total_faces, dtype=bool)
                lv_list_chk = all_loop_verts.tolist()
                seen_faces: set[tuple[int, ...]] = set()
                cur_chk = 0
                for fi, vc in enumerate(all_vcounts.tolist()):
                    sv = tuple(sorted(lv_list_chk[cur_chk : cur_chk + vc]))
                    cur_chk += vc
                    if any(sv[k] == sv[k + 1] for k in range(vc - 1)):
                        kf[fi] = False
                        n_degen += 1
                    elif sv in seen_faces:
                        kf[fi] = False
                        n_dups += 1
                    else:
                        seen_faces.add(sv)
                if n_degen > 0 or n_dups > 0:
                    keep_face = kf

            if keep_face is not None:
                keep_loop = np.repeat(keep_face, all_vcounts)
                all_vcounts = all_vcounts[keep_face]
                all_loop_verts = all_loop_verts[keep_loop]
                all_face_mats = all_face_mats[keep_face]
                if all_face_sharp is not None:
                    all_face_sharp = all_face_sharp[keep_face]
                if all_loop_normals is not None and len(all_loop_normals) == keep_loop.size:
                    all_loop_normals = all_loop_normals[keep_loop]
                for u_id in list(uv_arrays.keys()):
                    if len(uv_arrays[u_id]) == keep_loop.size:
                        uv_arrays[u_id] = uv_arrays[u_id][keep_loop]
                for cn in list(col_arrays.keys()):
                    if len(col_arrays[cn]) == keep_loop.size:
                        col_arrays[cn] = col_arrays[cn][keep_loop]
                total_faces = int(all_vcounts.size)
                total_loops = int(all_loop_verts.size)
                parts = []
                if n_dups > 0:
                    parts.append(f"{n_dups} duplicate")
                if n_degen > 0:
                    parts.append(f"{n_degen} degenerate")
                self.warnings.append(
                    f"Geometry '{geom_name}': dropped {' and '.join(parts)} face(s)"
                )

        mat_prims: dict[str, np.ndarray] = {}
        if total_faces > 0 and all_face_mats is not None:
            for sym_id, sym in enumerate(mat_sym_order):
                f_ids = np.flatnonzero(all_face_mats == sym_id)
                if f_ids.size > 0:
                    mat_prims[sym] = f_ids

        # Construct Blender Mesh with foreach_set (fast path when no loose edges)
        me = bpy.data.meshes.new(name=geom_name)
        if num_verts > 0:
            if pos_stride == 3:
                pos_flat = pos_vals[:num_verts * 3]
            else:
                p_mat = np.zeros((num_verts, 3), dtype=np.float32)
                cols_k = min(pos_stride, 3)
                p_mat[:, :cols_k] = pos_vals[:num_verts * pos_stride].reshape(num_verts, pos_stride)[:, :cols_k]
                pos_flat = p_mat.ravel()
        else:
            pos_flat = np.empty(0, dtype=np.float32)

        if not loose_edges and total_faces > 0 and all_vcounts is not None and all_loop_verts is not None:
            loop_starts = np.empty(total_faces, dtype=np.int32)
            loop_starts[0] = 0
            if total_faces > 1:
                np.cumsum(all_vcounts[:-1], out=loop_starts[1:])

            me.vertices.add(num_verts)
            me.vertices.foreach_set("co", pos_flat)
            me.loops.add(total_loops)
            me.loops.foreach_set("vertex_index", all_loop_verts)
            me.polygons.add(total_faces)
            me.polygons.foreach_set("loop_start", loop_starts)
            me.polygons.foreach_set("loop_total", all_vcounts)
            me.update(calc_edges=True)
        else:
            verts_co = pos_flat.reshape(num_verts, 3).tolist() if num_verts > 0 else []
            face_vert_indices: list[list[int]] = []
            if total_faces > 0 and all_vcounts is not None and all_loop_verts is not None:
                lv_list = all_loop_verts.tolist()
                cur = 0
                for vc in all_vcounts.tolist():
                    face_vert_indices.append(lv_list[cur:cur + vc])
                    cur += vc
            me.from_pydata(verts_co, loose_edges, face_vert_indices)

        if me.validate(clean_customdata=False):
            self.warnings.append(f"Geometry '{geom_name}' had invalid data and was repaired")

        # Smooth/flat face flags (foreach_set)
        cur_n_loops = len(me.loops)
        cur_n_polys = len(me.polygons)
        if total_faces > 0 and cur_n_polys == total_faces:
            if self.settings.sharp_edges_from_normals and all_loop_normals is not None and len(all_loop_normals) == cur_n_loops:
                me.polygons.foreach_set("use_smooth", np.ones(total_faces, dtype=bool))
            elif all_face_sharp is not None:
                me.polygons.foreach_set("use_smooth", np.logical_not(all_face_sharp))

        loops_match = (cur_n_loops == total_loops)
        if not loops_match and (uv_set_order or col_set_order or all_loop_normals is not None):
            self.warnings.append(
                f"Geometry '{geom_name}': skipped per-loop UVs/colors/normals after mesh repair "
                f"(loop count changed from {total_loops} to {cur_n_loops})"
            )

        if loops_match:
            # UVs (foreach_set)
            for u_id in uv_set_order:
                uv_arr = uv_arrays.get(u_id)
                if uv_arr is None:
                    continue
                if len(uv_arr) == cur_n_loops:
                    if not np.isfinite(uv_arr).all():
                        uv_arr = np.where(np.isfinite(uv_arr), uv_arr, np.float32(0.0))
                    uv_layer = me.uv_layers.new(name=u_id)
                    uv_layer.data.foreach_set("uv", uv_arr.ravel())

            # Byte Color Attributes (foreach_set)
            for cn in col_set_order:
                c_arr = col_arrays.get(cn)
                if c_arr is None:
                    continue
                if len(c_arr) == cur_n_loops:
                    if not np.isfinite(c_arr).all():
                        c_arr = np.where(np.isfinite(c_arr), c_arr, np.float32(0.0))
                    cattr = me.color_attributes.new(name=cn, type="BYTE_COLOR", domain="CORNER")
                    cattr.data.foreach_set("color_srgb", c_arr.ravel())

            # Custom normals / Clean sharp edges from normals
            if all_loop_normals is not None and len(all_loop_normals) == cur_n_loops and len(all_loop_normals) > 0:
                if not np.isfinite(all_loop_normals).all():
                    all_loop_normals = np.where(np.isfinite(all_loop_normals), all_loop_normals, np.float32(0.0))
                if self.settings.sharp_edges_from_normals:
                    self._apply_sharp_edges_from_normals(me, all_loop_normals, self.settings.sharp_angle_threshold)
                elif self.settings.custom_normals:
                    if self.settings.fast_exact_normals:
                        try:
                            cn = me.attributes.get("custom_normal")
                            if cn is not None and (cn.data_type != "FLOAT_VECTOR" or cn.domain != "CORNER"):
                                me.attributes.remove(cn)
                                cn = None
                            if cn is None:
                                cn = me.attributes.new(name="custom_normal", type="FLOAT_VECTOR", domain="CORNER")
                            cn.data.foreach_set("vector", all_loop_normals.ravel())
                        except Exception as e:
                            self.warnings.append(f"Fast exact normals failed on {geom_name}, falling back: {e}")
                            me.normals_split_custom_set(all_loop_normals.tolist())
                    else:
                        try:
                            # Passing a Python list is ~2.5x faster than passing an ndarray to Blender's RNA wrapper
                            me.normals_split_custom_set(all_loop_normals.tolist())
                        except Exception as e:
                            self.warnings.append(f"Could not set custom normals on {geom_name}: {e}")

        me.update()
        self.meshes_by_id[geom_id] = me
        self.mesh_mat_prims[geom_id] = mat_prims

    @staticmethod
    def _apply_sharp_edges_from_normals(
        me: Mesh, loop_normals: list[tuple[float, float, float]] | np.ndarray, threshold_rad: float
    ) -> None:
        """Mark sharp_edge directly from loop normal discontinuities across each edge (vectorized)."""
        n_edges = len(me.edges)
        if n_edges == 0:
            return

        sharp_flags = np.zeros(n_edges, dtype=bool)
        n_loops = len(me.loops)
        n_polys = len(me.polygons)

        if n_loops > 0 and n_polys > 0:
            loop_verts = np.empty(n_loops, dtype=np.int32)
            me.loops.foreach_get("vertex_index", loop_verts)
            loop_edges = np.empty(n_loops, dtype=np.int32)
            me.loops.foreach_get("edge_index", loop_edges)

            poly_starts = np.empty(n_polys, dtype=np.int32)
            me.polygons.foreach_get("loop_start", poly_starts)
            poly_totals = np.empty(n_polys, dtype=np.int32)
            me.polygons.foreach_get("loop_total", poly_totals)

            arange_l = np.arange(n_loops, dtype=np.int32)
            next_loop = arange_l + 1
            poly_ends = poly_starts + poly_totals - 1
            next_loop[poly_ends] = poly_starts

            # Canonicalize per-loop endpoint order by vertex index so both sides of an edge pair matching ends
            v0 = loop_verts
            v1 = loop_verts[next_loop]
            swap = v0 > v1
            l_lo = np.where(swap, next_loop, arange_l)
            l_hi = np.where(swap, arange_l, next_loop)

            norms = np.asarray(loop_normals, dtype=np.float64).reshape(n_loops, 3)
            lens_sq = np.einsum("ij,ij->i", norms, norms)
            valid = lens_sq > 0.0
            unit = np.zeros_like(norms)
            unit[valid] = norms[valid] / np.sqrt(lens_sq[valid])[:, None]

            order = np.argsort(loop_edges, kind="stable")
            e_sorted = loop_edges[order]
            counts = np.bincount(e_sorted, minlength=n_edges)
            e_starts = np.empty(n_edges, dtype=np.int32)
            e_starts[0] = 0
            if n_edges > 1:
                np.cumsum(counts[:-1], out=e_starts[1:])

            # Fast path: manifold edges (exactly 2 loops)
            m2 = np.flatnonzero(counts == 2)
            if m2.size > 0:
                s2 = e_starts[m2]
                la = order[s2]
                lb = order[s2 + 1]
                ia0 = l_lo[la]
                ia1 = l_hi[la]
                ib0 = l_lo[lb]
                ib1 = l_hi[lb]
                dot0 = np.clip(np.einsum("ij,ij->i", unit[ia0], unit[ib0]), -1.0, 1.0)
                dot1 = np.clip(np.einsum("ij,ij->i", unit[ia1], unit[ib1]), -1.0, 1.0)
                ang0 = np.where(valid[ia0] & valid[ib0], np.arccos(dot0), 0.0)
                ang1 = np.where(valid[ia1] & valid[ib1], np.arccos(dot1), 0.0)
                sharp_flags[m2] = (ang0 + ang1) * 0.5 > threshold_rad

            # Non-manifold edges (3+ loops): compare first face pair against remaining pairs
            m3 = np.flatnonzero(counts >= 3)
            if m3.size > 0:
                for ei in m3.tolist():
                    st = int(e_starts[ei])
                    cnt = int(counts[ei])
                    la_i = int(order[st])
                    ia0_i = int(l_lo[la_i])
                    ia1_i = int(l_hi[la_i])
                    ua0 = unit[ia0_i]
                    ua1 = unit[ia1_i]
                    va0 = bool(valid[ia0_i])
                    va1 = bool(valid[ia1_i])
                    for k in range(1, cnt):
                        lb_i = int(order[st + k])
                        ib0_i = int(l_lo[lb_i])
                        ib1_i = int(l_hi[lb_i])
                        ang0_f = (
                            math.acos(max(-1.0, min(1.0, float(np.dot(ua0, unit[ib0_i])))))
                            if (va0 and valid[ib0_i])
                            else 0.0
                        )
                        ang1_f = (
                            math.acos(max(-1.0, min(1.0, float(np.dot(ua1, unit[ib1_i])))))
                            if (va1 and valid[ib1_i])
                            else 0.0
                        )
                        if (ang0_f + ang1_f) * 0.5 > threshold_rad:
                            sharp_flags[ei] = True
                            break

        se = me.attributes.get("sharp_edge")
        if se is None:
            se = me.attributes.new(name="sharp_edge", type="BOOLEAN", domain="EDGE")
        se.data.foreach_set("value", sharp_flags)

    # ------------------------------------------------------------------
    # <library_controllers>
    # ------------------------------------------------------------------

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
            na = _find_child(src, "Name_array")
            if na is None:
                na = _find_child(src, "IDREF_array")
            if na is not None:
                name_sources[sid] = _parse_strings(na.text)
            fa = _find_child(src, "float_array")
            if fa is not None:
                float_sources[sid] = (_parse_floats(fa.text), stride_val)

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

    # ------------------------------------------------------------------
    # <library_visual_scenes> Node Processing
    # ------------------------------------------------------------------

    def _read_node_matrix(self, node: ET.Element) -> Matrix:
        mat = Matrix.Identity(4)
        for ch in node:
            tag = _strip_ns(ch.tag)
            if tag == "matrix":
                vals = _parse_floats(ch.text)
                if len(vals) == 16:
                    return dae_flat_to_matrix(vals)
            elif tag == "translate":
                vals = _parse_floats(ch.text)
                if len(vals) >= 3:
                    mat = mat @ Matrix.Translation((vals[0], vals[1], vals[2]))
            elif tag == "rotate":
                vals = _parse_floats(ch.text)
                if len(vals) >= 4:
                    axis = Vector((vals[0], vals[1], vals[2]))
                    if axis.length > 0.0:
                        mat = mat @ Matrix.Rotation(math.radians(vals[3]), 4, axis)
            elif tag == "scale":
                vals = _parse_floats(ch.text)
                if len(vals) >= 3:
                    mat = mat @ Matrix.Diagonal((vals[0], vals[1], vals[2], 1.0))
        return mat

    def _node_is_decomposed(self, node: ET.Element) -> bool:
        for ch in node:
            if _strip_ns(ch.tag) == "matrix":
                return False
        return True

    def _add_object_to_scene(self, name: str, data) -> Object:
        ob = bpy.data.objects.new(name=name, object_data=data)
        self.target_collection.objects.link(ob)
        ob.select_set(True)
        self.view_layer.objects.active = ob
        return ob

    def _apply_material_bindings(self, inst_elem: ET.Element, ob: Object, geom_id: str) -> None:
        bm = _find_child(inst_elem, "bind_material")
        if bm is None:
            return
        tc = _find_child(bm, "technique_common")
        if tc is None:
            return
        mat_prims = self.mesh_mat_prims.get(geom_id, {})
        mesh: Mesh = ob.data
        import numpy as np

        n_polys = len(mesh.polygons)
        mat_idx_arr = np.zeros(n_polys, dtype=np.int32)
        has_mat_assign = False
        for idx, im in enumerate(_find_children(tc, "instance_material")):
            sym = im.attrib.get("symbol", "")
            target_id = _uri_to_local_id(im.attrib.get("target"))
            ma = self.materials_by_id.get(target_id)
            if ma is None:
                continue
            if len(ob.material_slots) <= idx:
                ob.data.materials.append(None)
            ob.material_slots[idx].link = "OBJECT"
            ob.material_slots[idx].material = ma
            face_ids = mat_prims.get(sym)
            if face_ids is not None and face_ids.size > 0:
                valid_ids = face_ids[face_ids < n_polys]
                if valid_ids.size > 0:
                    mat_idx_arr[valid_ids] = idx
                    has_mat_assign = True
        if has_mat_assign and n_polys > 0:
            mesh.polygons.foreach_set("material_index", mat_idx_arr)

    def _process_node(
        self, node: ET.Element, parent_node: ET.Element | None, parent_ob: Object | None
    ) -> None:
        if self._kept_nodes is not None and id(node) not in self._kept_nodes:
            return
        node_id = node.attrib.get("id", "")
        node_name = node.attrib.get("name") or node_id or "Node"
        node_type = node.attrib.get("type", "NODE").upper()
        if node_id:
            self.node_elem_by_id[node_id] = node

        child_nodes = _find_children(node, "node")

        if node_type == "JOINT":
            if parent_node is None:
                arm_data = bpy.data.armatures.new(name="Armature")
                parent_ob = self._add_object_to_scene("Armature", arm_data)
                self.root_objects.append(parent_ob)
            if parent_node is None or parent_node.attrib.get("type", "NODE").upper() != "JOINT":
                if parent_ob is not None and parent_ob.type == "ARMATURE":
                    self.root_joint_nodes.append((node, parent_ob))
            return

        # When node_filter is active, only instantiate geometries on directly matched nodes
        # (ancestor nodes that were kept only for hierarchy become Empties carrying transforms).
        nf = self.settings.node_filter
        direct_node_match = (
            nf is None
            or (node_name in nf)
            or (bool(node_id) and node_id in nf)
        )
        inst_geoms = _find_children(node, "instance_geometry")
        if nf is not None and not direct_node_match:
            inst_geoms = [ig for ig in inst_geoms if ig.attrib.get("name", "") in nf]
        inst_ctrls = _find_children(node, "instance_controller") if direct_node_match else []
        inst_cams = _find_children(node, "instance_camera") if nf is None else []
        inst_lights = _find_children(node, "instance_light") if nf is None else []
        inst_nodes = _find_children(node, "instance_node")

        created: list[Object] = []

        for ig in inst_geoms:
            gid = _uri_to_local_id(ig.attrib.get("url"))
            me = self.meshes_by_id.get(gid)
            if me is not None:
                ob = self._add_object_to_scene(node_name, me)
                self._apply_material_bindings(ig, ob, gid)
                self.object_by_geom_id[gid] = ob
                self.imported_mesh_objects.append(ob)
                self.imported_objects_by_node_name.setdefault(node_name, []).append(ob)
                created.append(ob)

        for ic in inst_ctrls:
            cid = _uri_to_local_id(ic.attrib.get("url"))
            gid = ""
            if cid in self.skins_by_id:
                gid = self.skins_by_id[cid].geometry_id
            elif cid in self.morphs_by_id:
                gid = self.morphs_by_id[cid].geometry_id
            me = self.meshes_by_id.get(gid)
            if me is not None:
                ob = self._add_object_to_scene(node_name, me)
                self._apply_material_bindings(ic, ob, gid)
                self.object_by_geom_id[gid] = ob
                self.imported_mesh_objects.append(ob)
                self.imported_objects_by_node_name.setdefault(node_name, []).append(ob)
                created.append(ob)

        for icam in inst_cams:
            cam_id = _uri_to_local_id(icam.attrib.get("url"))
            cam = self.cameras_by_id.get(cam_id)
            if cam is not None:
                created.append(self._add_object_to_scene(node_name, cam))

        for ila in inst_lights:
            la_id = _uri_to_local_id(ila.attrib.get("url"))
            la = self.lights_by_id.get(la_id)
            if la is not None:
                created.append(self._add_object_to_scene(node_name, la))

        for ino in inst_nodes:
            target_id = _uri_to_local_id(ino.attrib.get("url"))
            src_node = self.lib_nodes_by_id.get(target_id)
            if src_node is not None:
                self._process_node(src_node, parent_node, parent_ob)

        if not created and not inst_nodes:
            is_arm = any(c.attrib.get("type", "NODE").upper() == "JOINT" for c in child_nodes)
            if is_arm:
                arm_data = bpy.data.armatures.new(name=node_name)
                ob = self._add_object_to_scene(node_name, arm_data)
                self._apply_armature_collections(node, arm_data)
            else:
                ob = self._add_object_to_scene(node_name, None)
            created.append(ob)

        mat = self._read_node_matrix(node)
        for ob in created:
            ob.matrix_basis = mat
            if parent_ob is not None:
                ob.parent = parent_ob
            else:
                self.root_objects.append(ob)
            if node_id:
                self.object_by_node_id[node_id] = ob

        primary_ob = created[0] if created else parent_ob
        for ch in child_nodes:
            self._process_node(ch, node, primary_ob)

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

    def _optimize_material_assignments(self) -> None:
        """Port of MeshImporter::optimize_material_assignements()."""
        by_mesh: dict[int, list[Object]] = {}
        for ob in self.imported_mesh_objects:
            by_mesh.setdefault(ob.data.as_pointer(), []).append(ob)

        for objs in by_mesh.values():
            ref = objs[0]
            ref_mats = [s.material for s in ref.material_slots]
            if not ref_mats:
                continue
            all_same = all([s.material for s in o.material_slots] == ref_mats for o in objs[1:])
            if all_same:
                mesh = ref.data
                for idx, ma in enumerate(ref_mats):
                    if idx < len(mesh.materials):
                        mesh.materials[idx] = ma
                    else:
                        mesh.materials.append(ma)
                for o in objs:
                    for idx in range(len(ref_mats)):
                        o.material_slots[idx].link = "DATA"

    # ------------------------------------------------------------------
    # Armatures, Skinning & Shape Keys
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # <library_animations>
    # ------------------------------------------------------------------

    def _read_library_animations(self, lib: ET.Element) -> None:
        for anim in _find_children(lib, "animation"):
            self._read_animation_elem(anim)

    def _read_animation_elem(self, anim: ET.Element) -> None:
        # Recurse nested <animation> elements
        for sub in _find_children(anim, "animation"):
            self._read_animation_elem(sub)

        float_sources: dict[str, tuple[list[float], int]] = {}
        name_sources: dict[str, list[str]] = {}
        for src in _find_children(anim, "source"):
            sid = src.attrib.get("id", "")
            stride = 1
            tc = _find_child(src, "technique_common")
            if tc is not None:
                acc = _find_child(tc, "accessor")
                if acc is not None and "stride" in acc.attrib:
                    try:
                        stride = int(acc.attrib["stride"])
                    except ValueError:
                        self.warnings.append(
                            f"Invalid stride '{acc.attrib['stride']}' in animation source '{sid}'"
                        )
                        continue
            if stride <= 0:
                self.warnings.append(f"Invalid stride {stride} in animation source '{sid}'")
                continue
            fa = _find_child(src, "float_array")
            if fa is not None:
                float_sources[sid] = (_parse_floats(fa.text), stride)
            na = _find_child(src, "Name_array")
            if na is not None:
                name_sources[sid] = _parse_strings(na.text)

        samplers: dict[str, dict[str, str]] = {}
        for samp in _find_children(anim, "sampler"):
            sid = samp.attrib.get("id", "")
            smap: dict[str, str] = {}
            for inp in _find_children(samp, "input"):
                smap[inp.attrib.get("semantic", "")] = _uri_to_local_id(inp.attrib.get("source"))
            samplers[sid] = smap

        for ch in _find_children(anim, "channel"):
            samp_id = _uri_to_local_id(ch.attrib.get("source"))
            target = ch.attrib.get("target", "")
            smap = samplers.get(samp_id)
            if not smap or not target:
                continue
            times, _ = float_sources.get(smap.get("INPUT", ""), ([], 1))
            values, stride = float_sources.get(smap.get("OUTPUT", ""), ([], 1))
            interps = name_sources.get(smap.get("INTERPOLATION", ""), [])
            in_tan, _ = float_sources.get(smap.get("IN_TANGENT", ""), ([], 2))
            out_tan, _ = float_sources.get(smap.get("OUT_TANGENT", ""), ([], 2))
            if times and values:
                self.animations.append(
                    _AnimChannelData(
                        target=target,
                        times=times,
                        values=values,
                        stride=stride,
                        interps=interps,
                        in_tangents=in_tan,
                        out_tangents=out_tan,
                    )
                )

    def _apply_animations(self) -> None:
        if not self.animations:
            return
        fps = self.scene.render.fps / self.scene.render.fps_base

        for ch in self.animations:
            if "/" not in ch.target:
                continue
            target_id, prop_part = ch.target.split("/", 1)
            axis_part = ""
            if "." in prop_part:
                prop_name, axis_part = prop_part.split(".", 1)
            else:
                prop_name = prop_part

            # Check if target is a joint node
            if target_id in self.joint_arm_by_id:
                ob_arm = self.joint_arm_by_id[target_id]
                bname = self.joint_bone_name_by_id[target_id]
                jnode = self.joint_elem_by_id[target_id]
                if ch.stride == 16:
                    self._apply_joint_matrix_animation(ob_arm, bname, jnode, ch, fps)
                else:
                    self._apply_decomposed_animation(
                        ob_arm, f'pose.bones["{bname}"].', prop_name, axis_part, ch, fps, node_elem=jnode
                    )
                continue

            # Check if target is an object node
            ob = self.object_by_node_id.get(target_id)
            if ob is not None:
                if ch.stride == 16:
                    self._apply_object_matrix_animation(ob, ch, fps)
                else:
                    self._apply_decomposed_animation(
                        ob, "", prop_name, axis_part, ch, fps, node_elem=self.node_elem_by_id.get(target_id)
                    )
                continue

            # Check camera
            if target_id in self.cameras_by_id:
                cam = self.cameras_by_id[target_id]
                _, cbag = ensure_action_and_channelbag(cam)
                if prop_name == "xfov":
                    fcu = create_fcurve_on_channelbag(cbag, "lens", 0)
                    vals = [fov_to_focallength(math.radians(v), cam.sensor_width) for v in ch.values]
                    self._populate_fcurve(fcu, ch.times, vals, ch.interps, fps)
                elif prop_name == "znear":
                    fcu = create_fcurve_on_channelbag(cbag, "clip_start", 0)
                    self._populate_fcurve(fcu, ch.times, ch.values, ch.interps, fps)
                elif prop_name == "zfar":
                    fcu = create_fcurve_on_channelbag(cbag, "clip_end", 0)
                    self._populate_fcurve(fcu, ch.times, ch.values, ch.interps, fps)
                elif prop_name == "xmag":
                    fcu = create_fcurve_on_channelbag(cbag, "ortho_scale", 0)
                    self._populate_fcurve(fcu, ch.times, [v * 2.0 for v in ch.values], ch.interps, fps)
                continue

            # Check light
            if target_id in self.lights_by_id:
                la = self.lights_by_id[target_id]
                _, cbag = ensure_action_and_channelbag(la)
                if prop_name == "color":
                    axis_idx = {"R": 0, "G": 1, "B": 2}.get(axis_part.upper(), -1)
                    if axis_idx >= 0:
                        fcu = create_fcurve_on_channelbag(cbag, "color", axis_idx)
                        self._populate_fcurve(fcu, ch.times, ch.values, ch.interps, fps)
                elif prop_name in ("falloff_angle", "fall_off_angle"):
                    fcu = create_fcurve_on_channelbag(cbag, "spot_size", 0)
                    self._populate_fcurve(fcu, ch.times, [math.radians(v) for v in ch.values], ch.interps, fps)
                elif prop_name in ("falloff_exponent", "fall_off_exponent"):
                    fcu = create_fcurve_on_channelbag(cbag, "spot_blend", 0)
                    self._populate_fcurve(fcu, ch.times, ch.values, ch.interps, fps)

    def _populate_fcurve(
        self,
        fcu,
        times: list[float],
        values: list[float],
        interps: list[str],
        fps: float,
        in_tan: list[float] | None = None,
        out_tan: list[float] | None = None,
        is_angle: bool = False,
    ) -> None:
        count = min(len(times), len(values))
        fcu.keyframe_points.add(count)
        for i in range(count):
            kp = fcu.keyframe_points[i]
            kp.co = (times[i] * fps, values[i])
            interp = interps[i] if i < len(interps) else "LINEAR"
            if interp == "BEZIER":
                kp.interpolation = "BEZIER"
                if in_tan and 2 * i + 1 < len(in_tan):
                    hy = math.radians(in_tan[2 * i + 1]) if is_angle else in_tan[2 * i + 1]
                    kp.handle_left = (in_tan[2 * i] * fps, hy)
                if out_tan and 2 * i + 1 < len(out_tan):
                    hy = math.radians(out_tan[2 * i + 1]) if is_angle else out_tan[2 * i + 1]
                    kp.handle_right = (out_tan[2 * i] * fps, hy)
            elif interp == "STEP":
                kp.interpolation = "CONSTANT"
            else:
                kp.interpolation = "LINEAR"
        fcu.update()

    def _apply_object_matrix_animation(self, ob: Object, ch: _AnimChannelData, fps: float) -> None:
        count = min(len(ch.times), len(ch.values) // 16)
        if count == 0:
            return
        _, cbag = ensure_action_and_channelbag(ob)
        ob.rotation_mode = "QUATERNION"
        locs: list[list[float]] = [[], [], []]
        quats: list[list[float]] = [[], [], [], []]
        scales: list[list[float]] = [[], [], []]
        times = ch.times[:count]
        for i in range(count):
            m = dae_flat_to_matrix(ch.values[i * 16:(i + 1) * 16])
            loc, quat, scale = m.decompose()
            for a in range(3):
                locs[a].append(loc[a])
                scales[a].append(scale[a])
            for a in range(4):
                quats[a].append(quat[a])

        for a in range(4):
            fcu = create_fcurve_on_channelbag(cbag, "rotation_quaternion", a)
            self._populate_fcurve(fcu, times, quats[a], ch.interps, fps)
        for a in range(3):
            fcu = create_fcurve_on_channelbag(cbag, "location", a)
            self._populate_fcurve(fcu, times, locs[a], ch.interps, fps)
        for a in range(3):
            fcu = create_fcurve_on_channelbag(cbag, "scale", a)
            self._populate_fcurve(fcu, times, scales[a], ch.interps, fps)

    def _apply_joint_matrix_animation(
        self, ob_arm: Object, bname: str, jnode: ET.Element, ch: _AnimChannelData, fps: float
    ) -> None:
        count = min(len(ch.times), len(ch.values) // 16)
        if count == 0:
            return
        bone = ob_arm.data.bones.get(bname)
        pchan = ob_arm.pose.bones.get(bname)
        if bone is None or pchan is None:
            return
        _, cbag = ensure_action_and_channelbag(ob_arm)
        pchan.rotation_mode = "QUATERNION"

        # Compute rest matrices (AnimationImporter::apply_matrix_curves formula: iR @ M @ iR_dae @ R)
        rest_local = self._read_node_matrix(jnode)
        rest = bone.matrix_local.copy()
        parent_rest = bone.parent.matrix_local.copy() if bone.parent else Matrix.Identity(4)
        try:
            irest = rest.inverted()
            iparent_rest_local = (parent_rest @ rest_local).inverted()
        except ValueError:
            self.warnings.append(f"Bone '{bname}': singular rest matrix in matrix animation; skipped")
            return

        prefix = f'pose.bones["{bname}"].'
        locs: list[list[float]] = [[], [], []]
        quats: list[list[float]] = [[], [], [], []]
        scales: list[list[float]] = [[], [], []]
        times = ch.times[:count]

        for i in range(count):
            matfra = dae_flat_to_matrix(ch.values[i * 16:(i + 1) * 16])
            temp = parent_rest @ matfra
            mat = irest @ temp @ iparent_rest_local @ rest
            loc, quat, scale = mat.decompose()
            for a in range(3):
                locs[a].append(loc[a])
                scales[a].append(scale[a])
            for a in range(4):
                quats[a].append(quat[a])

        for a in range(4):
            fcu = create_fcurve_on_channelbag(cbag, f"{prefix}rotation_quaternion", a)
            self._populate_fcurve(fcu, times, quats[a], ch.interps, fps)
        for a in range(3):
            fcu = create_fcurve_on_channelbag(cbag, f"{prefix}location", a)
            self._populate_fcurve(fcu, times, locs[a], ch.interps, fps)
        for a in range(3):
            fcu = create_fcurve_on_channelbag(cbag, f"{prefix}scale", a)
            self._populate_fcurve(fcu, times, scales[a], ch.interps, fps)

    def _resolve_transform_sid(self, node_elem: ET.Element | None, prop_name: str) -> tuple[str, int]:
        """Resolve a transform sid on node_elem to (kind, rot_axis_idx) for third-party DCCs."""
        if node_elem is not None:
            for child in node_elem:
                if child.attrib.get("sid") == prop_name:
                    tag = _strip_ns(child.tag)
                    if tag == "translate":
                        return ("location", -1)
                    if tag == "scale":
                        return ("scale", -1)
                    if tag == "rotate":
                        vals = _parse_floats(child.text)
                        if len(vals) >= 3:
                            ax = Vector((abs(vals[0]), abs(vals[1]), abs(vals[2])))
                            if ax.x >= ax.y and ax.x >= ax.z and ax.x > 0.5:
                                return ("rotation", 0)
                            if ax.y >= ax.x and ax.y >= ax.z and ax.y > 0.5:
                                return ("rotation", 1)
                            if ax.z > 0.5:
                                return ("rotation", 2)
        if prop_name in ("location", "translate"):
            return ("location", -1)
        if prop_name == "scale":
            return ("scale", -1)
        if prop_name in ("rotationX", "rotateX"):
            return ("rotation", 0)
        if prop_name in ("rotationY", "rotateY"):
            return ("rotation", 1)
        if prop_name in ("rotationZ", "rotateZ"):
            return ("rotation", 2)
        return ("", -1)

    def _apply_decomposed_animation(
        self,
        ob: Object,
        prefix: str,
        prop_name: str,
        axis_part: str,
        ch: _AnimChannelData,
        fps: float,
        node_elem: ET.Element | None = None,
    ) -> None:
        _, cbag = ensure_action_and_channelbag(ob)
        axis_map = {"X": 0, "Y": 1, "Z": 2}
        kind, rot_axis = self._resolve_transform_sid(node_elem, prop_name)
        if kind == "location":
            if ch.stride == 3:
                count3 = min(len(ch.times), len(ch.values) // 3)
                times3 = ch.times[:count3]
                for a in range(3):
                    vals = [ch.values[i * 3 + a] for i in range(count3)]
                    fcu = create_fcurve_on_channelbag(cbag, f"{prefix}location", a)
                    self._populate_fcurve(fcu, times3, vals, ch.interps, fps)
            else:
                a = axis_map.get(axis_part.upper(), 0)
                fcu = create_fcurve_on_channelbag(cbag, f"{prefix}location", a)
                self._populate_fcurve(
                    fcu, ch.times, ch.values, ch.interps, fps, ch.in_tangents, ch.out_tangents
                )
        elif kind == "scale":
            if ch.stride == 3:
                count3 = min(len(ch.times), len(ch.values) // 3)
                times3 = ch.times[:count3]
                for a in range(3):
                    vals = [ch.values[i * 3 + a] for i in range(count3)]
                    fcu = create_fcurve_on_channelbag(cbag, f"{prefix}scale", a)
                    self._populate_fcurve(fcu, times3, vals, ch.interps, fps)
            else:
                a = axis_map.get(axis_part.upper(), 0)
                fcu = create_fcurve_on_channelbag(cbag, f"{prefix}scale", a)
                self._populate_fcurve(
                    fcu, ch.times, ch.values, ch.interps, fps, ch.in_tangents, ch.out_tangents
                )
        elif kind == "rotation" and rot_axis >= 0:
            vals = [math.radians(v) for v in ch.values]
            fcu = create_fcurve_on_channelbag(cbag, f"{prefix}rotation_euler", rot_axis)
            self._populate_fcurve(
                fcu, ch.times, vals, ch.interps, fps, ch.in_tangents, ch.out_tangents, is_angle=True
            )
