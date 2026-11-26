# SPDX-FileCopyrightText: 2009-2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Archive (.zip/.zae/.kmz) indexing, safety caps, manifest, materials JSON, and image loading."""

from __future__ import annotations

import os
import urllib.parse
import zipfile

import bpy

from ..beamng_materials import alt_image_refs, parse_beamng_materials_json
from .xml_parse import _find_child, _norm_archive_path, _parse_xml_safe

_MAX_ZIP_ENTRIES = 50_000
_MAX_ZIP_ENTRY_BYTES = 1 * 1024 * 1024 * 1024       # 1 GiB
_MAX_ZIP_TOTAL_BYTES = 4 * 1024 * 1024 * 1024       # 4 GiB
_ZIP_RATIO_THRESHOLD_BYTES = 50 * 1024 * 1024       # 50 MB
_MAX_ZIP_RATIO = 200.0

class ArchiveMixin:
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
