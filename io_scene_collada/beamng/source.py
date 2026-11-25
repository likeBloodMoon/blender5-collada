# SPDX-FileCopyrightText: 2026 Blender5-Collada Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Read-only BeamNG vehicle archive / folder scanner and session cache (pure Python, no bpy).

Never writes to or extracts from the BeamNG install directory; all zip access is strictly
read-only (``zipfile.ZipFile(path, "r")``).
"""

from __future__ import annotations

import os
import re
import zipfile
from dataclasses import dataclass, field
from typing import Any

from .lenient_json import JbeamError, parse
from .parts import PartIndex
from .resolve import PcConfig, choose_default_config, parse_pc

_NODE_TAG_RE = re.compile(rb"<node\b([^>]*)>")
_ATTR_NAME_RE = re.compile(rb'\bname="([^"]*)"')
_ATTR_ID_RE = re.compile(rb'\bid="([^"]*)"')
_INST_NAME_RE = re.compile(rb'<instance_geometry\b[^>]*?\bname="([^"]*)"')

_MAX_TEXT_ENTRY_BYTES = 64 * 1024 * 1024  # 64 MiB safety cap for json/jbeam/pc
_MAX_ZIP_ENTRIES = 50_000


def _norm_path(raw: str) -> str:
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


def _is_ignored_entry(norm: str) -> bool:
    parts = norm.split("/")
    if any(p == "__MACOSX" for p in parts):
        return True
    return parts[-1].startswith("._")


def extract_dae_node_names(dae_bytes: bytes) -> set[str]:
    """Fast scan of `<node>` `name` attributes (falling back to `id`) in any attribute order.

    Also includes `<instance_geometry name=...>` for compatibility, matching spec section 4.7.
    """
    names: set[str] = set()
    for attrs in _NODE_TAG_RE.findall(dae_bytes):
        m = _ATTR_NAME_RE.search(attrs) or _ATTR_ID_RE.search(attrs)
        if m:
            names.add(m.group(1).decode("utf-8", errors="replace"))
    for inst_m in _INST_NAME_RE.findall(dae_bytes):
        names.add(inst_m.decode("utf-8", errors="replace"))
    return names


@dataclass
class ConfigEntry:
    path: str          # normalized path inside archive or relative to root
    name: str          # stem, e.g. "13s_M"
    vehicle: str       # vehicle model folder name, e.g. "covet"
    config_type: str = ""  # from info_<config>.json "Config Type" (tooltip)
    pc: PcConfig | None = None


@dataclass
class ModelFileEntry:
    path: str          # normalized path inside archive or relative to root
    size_bytes: int
    ext: str           # ".dae" or ".cdae"
    folder: str        # relative folder grouping ("" for root)
    vehicle: str       # detected vehicle folder if under vehicles/<model>/, else ""
    node_names: set[str] | None = None  # lazily populated or pre-indexed


@dataclass
class VehicleInfo:
    name: str
    default_pc: str | None = None
    configs: list[ConfigEntry] = field(default_factory=list)
    jbeam_files: list[str] = field(default_factory=list)


class VehicleSource:
    """Read-only index over a BeamNG vehicle .zip archive or a loose vehicle folder on disk."""

    def __init__(self, path: str, *, is_zip: bool, vehicle_root_dir: str | None = None) -> None:
        self.path = os.path.abspath(path)
        self.is_zip = is_zip
        self.vehicle_root_dir = vehicle_root_dir  # for loose files: parent vehicles/ dir if found
        self.entries: dict[str, int] = {}         # norm_path -> size_bytes
        self.entries_ci: dict[str, str] = {}      # lower -> norm_path
        self.zip_orig_name: dict[str, str] = {}   # norm_path -> raw zip filename
        self.disk_paths: dict[str, str] = {}      # norm_path -> abs path on disk (loose mode)

        self.vehicles: dict[str, VehicleInfo] = {}
        self.part_index = PartIndex()
        self.dae_models: list[ModelFileEntry] = []
        self.cdae_models: list[ModelFileEntry] = []
        self.all_configs: list[ConfigEntry] = []
        self.common_path: str | None = None
        self.common_is_zip: bool = False
        self.warnings: list[str] = []

        self._dae_nodes_indexed: bool = False
        self._cdae_nodes_indexed: bool = False
        self._mesh_to_dae: dict[str, list[str]] = {}
        self._mesh_to_cdae: dict[str, list[str]] = {}

    def scan(self, *, index_dae_nodes: bool = True) -> None:
        if self.is_zip:
            self._scan_zip(index_dae_nodes=index_dae_nodes)
        else:
            self._scan_loose_folder(index_dae_nodes=index_dae_nodes)
        self._locate_common()

    def _detect_vehicle_for_path(self, norm: str) -> str:
        parts = norm.split("/")
        if len(parts) >= 3 and parts[0].lower() == "vehicles":
            return parts[1]
        if not self.is_zip and self.vehicles:
            return next(iter(self.vehicles.keys()))
        return ""

    def _read_entry_bytes(self, zf: zipfile.ZipFile | None, norm: str) -> bytes:
        if self.is_zip:
            assert zf is not None
            return zf.read(self.zip_orig_name[norm])
        with open(self.disk_paths[norm], "rb") as f:
            return f.read()

    def _scan_zip(self, *, index_dae_nodes: bool) -> None:
        with zipfile.ZipFile(self.path, "r") as zf:
            infos = zf.infolist()
            if len(infos) > _MAX_ZIP_ENTRIES:
                raise ValueError(
                    f"Archive contains {len(infos)} entries, exceeding the {_MAX_ZIP_ENTRIES}-entry limit"
                )
            for info in sorted(infos, key=lambda i: i.filename):
                if info.is_dir():
                    continue
                norm = _norm_path(info.filename)
                if not norm or _is_ignored_entry(norm):
                    continue
                if norm not in self.entries:
                    self.entries[norm] = info.file_size
                    self.entries_ci[norm.lower()] = norm
                    self.zip_orig_name[norm] = info.filename
            self._parse_entries(zf, index_dae_nodes=index_dae_nodes)

    def _scan_loose_folder(self, *, index_dae_nodes: bool) -> None:
        base_dir = self.path if os.path.isdir(self.path) else os.path.dirname(self.path)
        # Walk up up to 4 levels to see if we sit inside a `vehicles/<model>` tree
        cur = base_dir
        veh_dir = None
        for _ in range(4):
            parent = os.path.dirname(cur)
            if os.path.basename(parent).lower() == "vehicles":
                veh_dir = parent
                break
            if not parent or parent == cur:
                break
            cur = parent
        self.vehicle_root_dir = veh_dir
        model_name = os.path.basename(cur if veh_dir else base_dir)
        scan_root = cur if veh_dir else base_dir
        prefix = f"vehicles/{model_name}"

        def _record_file(abs_p: str) -> None:
            rel = os.path.relpath(abs_p, scan_root)
            norm = _norm_path(f"{prefix}/{rel}")
            if not norm or _is_ignored_entry(norm):
                return
            try:
                sz = os.path.getsize(abs_p)
            except OSError:
                sz = 0
            self.entries[norm] = sz
            self.entries_ci[norm.lower()] = norm
            self.disk_paths[norm] = abs_p

        if veh_dir is None:
            # Outside a `vehicles/<model>/` tree: scan only direct files in base_dir
            # (non-recursive) so opening a loose file on Desktop/Downloads never walks subtrees.
            try:
                with os.scandir(scan_root) as it:
                    files = sorted(
                        (e.path for e in it if e.is_file(follow_symlinks=False)),
                        key=str.lower,
                    )
            except OSError:
                files = []
            for abs_p in files:
                _record_file(abs_p)
        else:
            # Inside `vehicles/<model>/`: allow bounded recursion (depth <= 3) for standard
            # vehicle subdirs (e.g. 3wheel/, tires/, skins/) without runaway walks.
            max_depth = 3
            for root, dirs, files in os.walk(scan_root):
                rel_dir = os.path.relpath(root, scan_root)
                depth = 0 if rel_dir == "." else rel_dir.count(os.sep) + 1
                if depth >= max_depth:
                    dirs[:] = []
                else:
                    dirs[:] = sorted(d for d in dirs if d != "__MACOSX" and not d.startswith("."))
                for fn in sorted(files):
                    _record_file(os.path.join(root, fn))
        self._parse_entries(None, index_dae_nodes=index_dae_nodes)

    def _parse_entries(self, zf: zipfile.ZipFile | None, *, index_dae_nodes: bool) -> None:
        info_json_by_vehicle: dict[str, dict[str, Any]] = {}
        config_info_by_key: dict[tuple[str, str], dict[str, Any]] = {}

        for norm, size_bytes in sorted(self.entries.items()):
            low = norm.lower()
            ext = os.path.splitext(low)[1]
            veh = self._detect_vehicle_for_path(norm)
            folder = norm.rsplit("/", 1)[0] if "/" in norm else ""

            if ext == ".jbeam":
                if size_bytes > _MAX_TEXT_ENTRY_BYTES:
                    self.warnings.append(f"Skipped oversized jbeam '{norm}'")
                    continue
                if veh:
                    vinfo = self.vehicles.setdefault(veh, VehicleInfo(name=veh))
                    vinfo.jbeam_files.append(norm)
                raw = self._read_entry_bytes(zf, norm)
                self.part_index.add_jbeam_text(raw.decode("utf-8", errors="replace"), norm)

            elif ext == ".pc":
                if size_bytes > _MAX_TEXT_ENTRY_BYTES:
                    continue
                raw = self._read_entry_bytes(zf, norm)
                stem = os.path.splitext(norm.rsplit("/", 1)[-1])[0]
                try:
                    pc_obj = parse_pc(raw.decode("utf-8", errors="replace"), norm)
                except (JbeamError, ValueError) as exc:
                    self.warnings.append(f"Could not parse config '{norm}': {exc}")
                    continue
                ce = ConfigEntry(path=norm, name=stem, vehicle=veh, pc=pc_obj)
                self.all_configs.append(ce)
                if veh:
                    self.vehicles.setdefault(veh, VehicleInfo(name=veh)).configs.append(ce)

            elif ext == ".json":
                bname = low.rsplit("/", 1)[-1]
                if bname == "info.json" and veh and size_bytes <= _MAX_TEXT_ENTRY_BYTES:
                    try:
                        doc = parse(self._read_entry_bytes(zf, norm).decode("utf-8", errors="replace"))
                        if isinstance(doc, dict):
                            info_json_by_vehicle[veh] = doc
                    except JbeamError:
                        pass
                elif bname.startswith("info_") and veh and size_bytes <= _MAX_TEXT_ENTRY_BYTES:
                    cfg_key = bname[5:-5]  # strip "info_" and ".json"
                    try:
                        doc = parse(self._read_entry_bytes(zf, norm).decode("utf-8", errors="replace"))
                        if isinstance(doc, dict):
                            config_info_by_key[(veh.lower(), cfg_key)] = doc
                    except JbeamError:
                        pass

            elif ext == ".dae":
                nodes = extract_dae_node_names(self._read_entry_bytes(zf, norm)) if index_dae_nodes else None
                entry = ModelFileEntry(
                    path=norm,
                    size_bytes=size_bytes,
                    ext=".dae",
                    folder=folder,
                    vehicle=veh,
                    node_names=nodes,
                )
                self.dae_models.append(entry)
                if nodes is not None:
                    for nm in nodes:
                        self._mesh_to_dae.setdefault(nm, []).append(norm)

            elif ext == ".cdae":
                self.cdae_models.append(
                    ModelFileEntry(
                        path=norm,
                        size_bytes=size_bytes,
                        ext=".cdae",
                        folder=folder,
                        vehicle=veh,
                        node_names=None,
                    )
                )

        if index_dae_nodes:
            self._dae_nodes_indexed = True

        # Attach info.json default_pc and info_<config>.json Config Type tooltips
        for veh_name, vinfo in self.vehicles.items():
            idict = info_json_by_vehicle.get(veh_name)
            if idict and isinstance(idict.get("default_pc"), str) and idict["default_pc"].strip():
                vinfo.default_pc = idict["default_pc"].strip()
            vinfo.configs.sort(key=lambda c: c.name.lower())
            for ce in vinfo.configs:
                cinfo = config_info_by_key.get((veh_name.lower(), ce.name.lower()))
                if cinfo and isinstance(cinfo.get("Config Type"), str):
                    ce.config_type = cinfo["Config Type"]

        # Remove vehicle folders that have neither jbeam nor configs (e.g. stray subdirs)
        for vname in [k for k, v in self.vehicles.items() if not v.jbeam_files and not v.configs]:
            del self.vehicles[vname]

    def _locate_common(self) -> None:
        if self.is_zip:
            if os.path.basename(self.path).lower() == "common.zip":
                self.common_path = None
                return
            sibling = os.path.join(os.path.dirname(self.path), "common.zip")
            if os.path.isfile(sibling):
                self.common_path = sibling
                self.common_is_zip = True
                return
        else:
            if self.vehicle_root_dir:
                c_dir = os.path.join(self.vehicle_root_dir, "common")
                if os.path.isdir(c_dir):
                    self.common_path = c_dir
                    self.common_is_zip = False
                    return
                c_zip = os.path.join(self.vehicle_root_dir, "common.zip")
                if os.path.isfile(c_zip):
                    self.common_path = c_zip
                    self.common_is_zip = True
                    return

    def ensure_dae_nodes_indexed(self) -> None:
        if self._dae_nodes_indexed:
            return
        zf = zipfile.ZipFile(self.path, "r") if self.is_zip else None
        try:
            for entry in self.dae_models:
                if entry.node_names is None:
                    entry.node_names = extract_dae_node_names(self._read_entry_bytes(zf, entry.path))
                for nm in entry.node_names:
                    self._mesh_to_dae.setdefault(nm, []).append(entry.path)
            self._dae_nodes_indexed = True
        finally:
            if zf is not None:
                zf.close()

    def ensure_cdae_nodes_indexed(self) -> None:
        if self._cdae_nodes_indexed:
            return
        import struct
        from ..cdae.msgpack_lite import MsgPackError, MsgPackReader
        from ..cdae.reader import CdaeError, SUPPORTED_CDAE_VERSION, read_cdae_bytes

        zf = zipfile.ZipFile(self.path, "r") if self.is_zip else None
        try:
            for entry in self.cdae_models:
                if entry.node_names is None:
                    names: set[str] | None = None
                    # Fast path: read only the small MsgPack header for `objectNames`
                    try:
                        if self.is_zip:
                            assert zf is not None
                            with zf.open(self.zip_orig_name[entry.path], "r") as fh:
                                head8 = fh.read(8)
                                if len(head8) == 8:
                                    ver_val, hsz = struct.unpack_from("<II", head8, 0)
                                    if (ver_val & 0xFF) == SUPPORTED_CDAE_VERSION and 0 < hsz <= 4 * 1024 * 1024:
                                        hdr = MsgPackReader(fh.read(hsz)).read(zero_copy_bin=False)
                                        if isinstance(hdr, dict) and isinstance(hdr.get("objectNames"), list):
                                            obj_names = [s for s in hdr["objectNames"] if isinstance(s, str) and s]
                                            if obj_names:
                                                names = set(obj_names)
                        else:
                            with open(self.disk_paths[entry.path], "rb") as fh:
                                head8 = fh.read(8)
                                if len(head8) == 8:
                                    ver_val, hsz = struct.unpack_from("<II", head8, 0)
                                    if (ver_val & 0xFF) == SUPPORTED_CDAE_VERSION and 0 < hsz <= 4 * 1024 * 1024:
                                        hdr = MsgPackReader(fh.read(hsz)).read(zero_copy_bin=False)
                                        if isinstance(hdr, dict) and isinstance(hdr.get("objectNames"), list):
                                            obj_names = [s for s in hdr["objectNames"] if isinstance(s, str) and s]
                                            if obj_names:
                                                names = set(obj_names)
                    except (MsgPackError, OSError, ValueError):
                        names = None

                    if names is None:
                        try:
                            shape = read_cdae_bytes(self._read_entry_bytes(zf, entry.path))
                            names = set()
                            for obj in shape.objects:
                                ni = int(obj["name_index"])
                                if 0 <= ni < len(shape.names) and shape.names[ni]:
                                    names.add(shape.names[ni])
                            for node in shape.nodes:
                                ni = int(node["name_index"])
                                if 0 <= ni < len(shape.names) and shape.names[ni]:
                                    names.add(shape.names[ni])
                        except (CdaeError, Exception):
                            names = set()
                    entry.node_names = names
                for nm in entry.node_names:
                    self._mesh_to_cdae.setdefault(nm, []).append(entry.path)
            self._cdae_nodes_indexed = True
        finally:
            if zf is not None:
                zf.close()

    def all_dae_mesh_names(self) -> set[str]:
        self.ensure_dae_nodes_indexed()
        return set(self._mesh_to_dae.keys())

    def all_cdae_mesh_names(self) -> set[str]:
        self.ensure_cdae_nodes_indexed()
        return set(self._mesh_to_cdae.keys())

    def dae_files_for_meshes(self, mesh_names: set[str]) -> dict[str, set[str]]:
        """Map each .dae path to the subset of ``mesh_names`` it contains."""
        self.ensure_dae_nodes_indexed()
        result: dict[str, set[str]] = {}
        for nm in mesh_names:
            for dae_path in self._mesh_to_dae.get(nm, ()):
                result.setdefault(dae_path, set()).add(nm)
        return result

    def cdae_files_for_meshes(self, mesh_names: set[str]) -> dict[str, set[str]]:
        """Map each .cdae path to the subset of ``mesh_names`` it contains."""
        self.ensure_cdae_nodes_indexed()
        result: dict[str, set[str]] = {}
        for nm in mesh_names:
            for cdae_path in self._mesh_to_cdae.get(nm, ()):
                result.setdefault(cdae_path, set()).add(nm)
        return result

    def default_config_for_vehicle(self, vehicle_name: str) -> ConfigEntry | None:
        vinfo = self.vehicles.get(vehicle_name)
        if not vinfo or not vinfo.configs:
            return None
        chosen_path = choose_default_config([c.path for c in vinfo.configs], vinfo.default_pc)
        if chosen_path is None:
            return None
        for ce in vinfo.configs:
            if ce.path == chosen_path:
                return ce
        return vinfo.configs[0]

    def has_picker_choices(self, *, cdae_mode: bool = False) -> bool:
        """True when an interactive import should open the BeamNG picker dialog (spec 3.1)."""
        has_valid_configs = len(self.all_configs) > 0 and self.part_index.jbeam_ok > 0
        if not self.is_zip:
            return has_valid_configs
        models = self.cdae_models if cdae_mode else self.dae_models
        return len(models) > 1 or has_valid_configs


# Session cache keyed by (abs_path, size, mtime_ns, index_dae_nodes)
_SOURCE_CACHE: dict[tuple[str, int, int], VehicleSource] = {}


def clear_source_cache() -> None:
    _SOURCE_CACHE.clear()


def _cache_key(path: str) -> tuple[str, int, int]:
    abs_p = os.path.abspath(path)
    try:
        st = os.stat(abs_p)
        return (abs_p, st.st_size, st.st_mtime_ns)
    except OSError:
        return (abs_p, 0, 0)


def load_vehicle_source(path: str, *, index_dae_nodes: bool = True) -> VehicleSource:
    """Load (or return cached) VehicleSource for a .zip archive or loose file/folder."""
    abs_p = os.path.abspath(path)
    is_zip = os.path.isfile(abs_p) and os.path.splitext(abs_p)[1].lower() in (".zip", ".zae", ".kmz")
    key = _cache_key(abs_p)
    cached = _SOURCE_CACHE.get(key)
    if cached is not None:
        if index_dae_nodes:
            cached.ensure_dae_nodes_indexed()
        return cached
    src = VehicleSource(abs_p, is_zip=is_zip)
    src.scan(index_dae_nodes=index_dae_nodes)
    _SOURCE_CACHE[key] = src
    return src
