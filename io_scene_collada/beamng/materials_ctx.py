# SPDX-FileCopyrightText: 2026 Blender5-Collada Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Shared material table, texture lookup, and per-import material reuse for BeamNG imports."""

from __future__ import annotations

import os
import urllib.parse
import uuid
import zipfile
from typing import TYPE_CHECKING

import bpy

from ..beamng_materials import (
    BeamngMaterialSpec,
    alt_image_refs,
    lookup_material_spec,
    parse_beamng_material_specs,
    parse_beamng_materials_json,
)
from .pbr_nodes import apply_beamng_pbr_material
from .source import VehicleSource, _is_ignored_entry, _norm_path

if TYPE_CHECKING:
    from bpy.types import Image, Material

_MAX_ZIP_ENTRY_BYTES = 1024 * 1024 * 1024       # 1 GiB per entry
_MAX_ZIP_TOTAL_BYTES = 4 * 1024 * 1024 * 1024   # 4 GiB total per import
_MAX_ZIP_RATIO = 100.0
_ZIP_RATIO_THRESHOLD_BYTES = 16 * 1024 * 1024

_IMPORT_TOKEN_PROP = "_beamng_import_token"


class BeamngMaterialContext:
    """Shared material & texture context across all passes of one BeamNG vehicle import.

    - Builds one material table from every ``*.materials.json`` in the vehicle source
      and in ``common.zip`` (vehicle entries win on name clashes).
    - Resolves textures in the vehicle source first, then ``common.zip``.
    - Reuses materials created within this import by name (guarded by a per-import
      custom property token so pre-existing user materials are never reused).
    - Collects missing textures referenced in ``*.materials.json`` so the caller can
      emit a single summary warning per import.
    """

    def __init__(
        self,
        vehicle_src: VehicleSource,
        common_src: VehicleSource | None = None,
        *,
        pbr_mode: bool = True,
        rename_uvs: bool = False,
    ) -> None:
        self.sources: list[VehicleSource] = [vehicle_src]
        if common_src is not None and common_src.path != vehicle_src.path:
            self.sources.append(common_src)

        self.pbr_mode: bool = pbr_mode
        self.rename_uvs: bool = rename_uvs
        self.import_token: str = uuid.uuid4().hex
        self.mat_maps: dict[str, list[str]] = {}
        self.mat_specs: dict[str, BeamngMaterialSpec] = {}
        self.warnings: list[str] = []
        self.missing_textures: list[str] = []
        self._missing_seen: set[str] = set()
        self.glass_fallback_materials: list[str] = []
        self._materials_by_name: dict[str, Material] = {}
        self._images_by_key: dict[tuple[int, str, str], Image] = {}
        self._open_zips: dict[int, zipfile.ZipFile] = {}
        self._bytes_read: int = 0

        # Pre-build deterministic basename index per source
        self._by_basename: list[dict[str, list[str]]] = []
        for src in self.sources:
            by_bn: dict[str, list[str]] = {}
            for norm in sorted(src.entries.keys()):
                if _is_ignored_entry(norm):
                    continue
                bn = norm.rsplit("/", 1)[-1].lower()
                by_bn.setdefault(bn, []).append(norm)
            self._by_basename.append(by_bn)

    def __enter__(self) -> BeamngMaterialContext:
        self._load_all_materials_json()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def close(self) -> None:
        for zf in self._open_zips.values():
            try:
                zf.close()
            except Exception:
                pass
        self._open_zips.clear()

    def _get_zip(self, src_idx: int, src: VehicleSource) -> zipfile.ZipFile:
        zf = self._open_zips.get(src_idx)
        if zf is None:
            zf = zipfile.ZipFile(src.path, "r")
            self._open_zips[src_idx] = zf
        return zf

    def _read_entry_bytes(self, src_idx: int, src: VehicleSource, norm_path: str) -> bytes:
        if src.is_zip:
            orig_name = src.zip_orig_name.get(norm_path)
            if orig_name is None:
                return b""
            zf = self._get_zip(src_idx, src)
            info = zf.getinfo(orig_name)
            file_size = info.file_size
            if file_size > _MAX_ZIP_ENTRY_BYTES:
                raise ValueError(
                    f"Archive entry '{info.filename}' exceeds 1 GiB uncompressed limit ({file_size} bytes)"
                )
            if self._bytes_read + file_size > _MAX_ZIP_TOTAL_BYTES:
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
            if len(data) > _MAX_ZIP_ENTRY_BYTES or self._bytes_read + len(data) > _MAX_ZIP_TOTAL_BYTES:
                raise ValueError(f"Archive entry '{info.filename}' exceeded uncompressed size limit during read")
            self._bytes_read += len(data)
            return data

        disk_path = src.disk_paths.get(norm_path)
        if not disk_path or not os.path.isfile(disk_path):
            return b""
        with open(disk_path, "rb") as f:
            return f.read()

    def _scan_source_materials(
        self, src_idx: int, src: VehicleSource
    ) -> tuple[dict[str, list[str]], dict[str, BeamngMaterialSpec]]:
        mat_entries: list[str] = []
        for norm in src.entries:
            if _is_ignored_entry(norm):
                continue
            bname = norm.rsplit("/", 1)[-1].lower()
            if bname.endswith(".materials.json"):
                mat_entries.append(norm)
        # Shallowest first, then alphabetical
        mat_entries.sort(key=lambda p: (p.count("/"), p.lower()))

        merged: dict[str, list[str]] = {}
        specs: dict[str, BeamngMaterialSpec] = {}
        for norm in mat_entries:
            try:
                raw = self._read_entry_bytes(src_idx, src, norm)
                if not raw:
                    continue
                text = raw.decode("utf-8", errors="replace")
                parsed = parse_beamng_materials_json(text)
                for k, refs in parsed.items():
                    cur = merged.setdefault(k, [])
                    for r in refs:
                        if r not in cur:
                            cur.append(r)
                if self.pbr_mode:
                    parsed_specs = parse_beamng_material_specs(text)
                    for k, sp in parsed_specs.items():
                        if k not in specs:
                            specs[k] = sp
            except Exception as exc:
                self.warnings.append(f"Could not parse '{norm}': {exc}")
        return merged, specs

    def _load_all_materials_json(self) -> None:
        # Load common.zip first, then overwrite any clashing keys with vehicle entries.
        combined: dict[str, list[str]] = {}
        combined_specs: dict[str, BeamngMaterialSpec] = {}
        if len(self.sources) > 1:
            com_maps, com_specs = self._scan_source_materials(1, self.sources[1])
            combined.update(com_maps)
            combined_specs.update(com_specs)
        veh_maps, veh_specs = self._scan_source_materials(0, self.sources[0])
        combined.update(veh_maps)
        combined_specs.update(veh_specs)
        self.mat_maps = combined
        self.mat_specs = combined_specs

    def apply_material(self, ma: Material, *names: str, dae_dir: str = "") -> bool:
        """Apply full v1.5 PBR node tree if pbr_mode is enabled and a v1.5 spec exists."""
        if not self.pbr_mode or not self.mat_specs:
            return False
        spec = lookup_material_spec(self.mat_specs, *names)
        if spec is None or not spec.is_v15:
            return False
        apply_beamng_pbr_material(ma, spec, self, dae_dir=dae_dir)
        return True

    def record_glass_fallback(self, mat_name: str) -> None:
        if mat_name not in self.glass_fallback_materials:
            self.glass_fallback_materials.append(mat_name)

    def get_reusable_material(self, mat_name: str) -> Material | None:
        """Return a material already created in this import, never a pre-existing user material."""
        ma = self._materials_by_name.get(mat_name)
        if ma is None:
            return None
        try:
            if ma.get(_IMPORT_TOKEN_PROP) == self.import_token:
                return ma
        except ReferenceError:
            self._materials_by_name.pop(mat_name, None)
        return None

    def register_material(self, mat_name: str, ma: Material) -> None:
        """Tag a newly created material as owned by this import and cache it for reuse."""
        ma[_IMPORT_TOKEN_PROP] = self.import_token
        self._materials_by_name[mat_name] = ma

    def _resolve_in_source(
        self,
        src_idx: int,
        src: VehicleSource,
        raw_uri: str,
        *,
        dae_dir: str = "",
    ) -> str | None:
        raw = urllib.parse.unquote(raw_uri.strip())
        if raw.lower().startswith("file:///"):
            raw = raw[8:]
        elif raw.lower().startswith("file://"):
            raw = raw[7:]
        raw = raw.replace("\\", "/")
        bname = raw.rsplit("/", 1)[-1]

        def _try(path_str: str) -> str | None:
            norm = _norm_path(path_str)
            if not norm:
                return None
            if norm in src.entries:
                return norm
            return src.entries_ci.get(norm.lower())

        candidates: list[str] = []
        if dae_dir:
            candidates.append(f"{dae_dir}/{raw}")
        candidates.append(raw)

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

        if bname:
            matches = self._by_basename[src_idx].get(bname.lower())
            if matches:
                return matches[0]
        return None

    def load_texture(self, raw_ref: str, *, dae_dir: str = "", colorspace: str = "sRGB") -> Image | None:
        """Look up and load a texture from the vehicle source first, then common.zip."""
        refs = alt_image_refs(raw_ref)
        if not refs:
            return None

        for src_idx, src in enumerate(self.sources):
            for ref in refs:
                norm_entry = self._resolve_in_source(src_idx, src, ref, dae_dir=dae_dir)
                if norm_entry is None:
                    continue

                cache_key = (src_idx, norm_entry, colorspace)
                cached = self._images_by_key.get(cache_key)
                if cached is not None:
                    try:
                        _ = cached.name
                        return cached
                    except ReferenceError:
                        self._images_by_key.pop(cache_key, None)

                img = self._load_source_image(src_idx, src, norm_entry, colorspace=colorspace)
                if img is not None:
                    self._images_by_key[cache_key] = img
                    return img
        return None

    def _load_source_image(
        self, src_idx: int, src: VehicleSource, norm_entry: str, *, colorspace: str = "sRGB"
    ) -> Image | None:
        if not src.is_zip:
            disk_path = src.disk_paths.get(norm_entry)
            if not disk_path or not os.path.isfile(disk_path):
                return None
            try:
                img = bpy.data.images.load(disk_path, check_existing=(colorspace == "sRGB"))
                if colorspace != "sRGB":
                    img.colorspace_settings.name = colorspace
                return img
            except Exception as exc:
                self.warnings.append(f"Could not decode texture '{norm_entry}': {exc}")
                return None

        try:
            data = self._read_entry_bytes(src_idx, src, norm_entry)
        except Exception as exc:
            self.warnings.append(f"Could not read archive texture '{norm_entry}': {exc}")
            return None
        if not data:
            self.warnings.append(f"Archive texture is empty: {norm_entry}")
            return None

        name = norm_entry.rsplit("/", 1)[-1] or "Image"
        img = None
        try:
            img = bpy.data.images.new(name, 8, 8)
            img.filepath = f"//{os.path.basename(src.path)}/{norm_entry}"
            img.pack(data=data, data_len=len(data))
            img.source = "FILE"
            if colorspace != "sRGB":
                img.colorspace_settings.name = colorspace
            px_len = len(img.pixels)
            if not img.has_data or px_len == 0:
                raise RuntimeError("image data could not be decoded")
            return img
        except Exception as exc:
            self.warnings.append(f"Could not decode archive texture '{norm_entry}': {exc}")
            if img is not None:
                try:
                    bpy.data.images.remove(img)
                except Exception:
                    pass
            return None

    def record_missing_textures(self, refs: list[str]) -> None:
        for ref in refs:
            key = ref.replace("\\", "/").strip().lower()
            if key and key not in self._missing_seen:
                self._missing_seen.add(key)
                self.missing_textures.append(ref)

    def format_missing_textures_warning(self, max_names: int = 5) -> str | None:
        n = len(self.missing_textures)
        if n == 0:
            return None
        preview_list = [os.path.basename(r.replace("\\", "/")) or r for r in self.missing_textures[:max_names]]
        preview = ", ".join(preview_list)
        if n > max_names:
            preview += f", ... (+{n - max_names} more)"
        return f"{n} texture(s) named in *.materials.json missing from vehicle and common archives: {preview}"
