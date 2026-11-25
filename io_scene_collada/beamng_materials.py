# SPDX-FileCopyrightText: 2026 Blender5-Collada Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""BeamNG main.materials.json lenient parser and image fallback helpers."""

from __future__ import annotations

import os
from typing import Any

from .beamng.lenient_json import parse as loads_lenient_json

_RASTER_EXTS_FOR_DDS_FALLBACK = frozenset({
    ".png",
    ".jpg",
    ".jpeg",
    ".tga",
    ".bmp",
    ".tif",
    ".tiff",
})

_COLOR_MAP_KEYS = ("baseColorMap", "colorMap", "diffuseMap")


def alt_image_refs(raw_ref: str) -> list[str]:
    """Return image reference candidates, adding .dds/.png fallbacks for BeamNG assets."""
    if not raw_ref:
        return []
    out = [raw_ref]
    root, ext = os.path.splitext(raw_ref)
    ext_low = ext.lower()
    if ext_low in _RASTER_EXTS_FOR_DDS_FALLBACK:
        out.append(root + ".dds")
    elif ext_low == ".dds":
        out.append(root + ".png")
    elif not ext_low:
        out.extend((root + ".dds", root + ".png", root + ".jpg", root + ".tga"))
    return out


def _extract_map_strings(val: Any) -> list[str]:
    refs: list[str] = []
    if isinstance(val, str):
        s = val.strip()
        if s and not s.startswith("@"):
            refs.append(s)
    elif isinstance(val, list):
        for item in val:
            if isinstance(item, str):
                s = item.strip()
                if s and not s.startswith("@"):
                    refs.append(s)
    return refs


def _extract_entry_color_maps(entry: dict[str, Any]) -> list[str]:
    """Extract candidate baseColorMap / colorMap / diffuseMap paths from one material dict."""
    found: list[str] = []
    seen: set[str] = set()

    def _add_from_dict(d: Any) -> None:
        if not isinstance(d, dict):
            return
        for key in _COLOR_MAP_KEYS:
            if key in d:
                for ref in _extract_map_strings(d[key]):
                    if ref not in seen:
                        seen.add(ref)
                        found.append(ref)

    stages = entry.get("Stages")
    if isinstance(stages, list) and stages:
        _add_from_dict(stages[0])
    if not found:
        _add_from_dict(entry)
    if not found and isinstance(stages, list) and len(stages) > 1:
        for st in stages[1:]:
            _add_from_dict(st)
            if found:
                break
    return found


def parse_beamng_materials_json(text: str) -> dict[str, list[str]]:
    """Parse a BeamNG main.materials.json into {lowercased_material_name: [color_map_paths]}."""
    data = loads_lenient_json(text)
    if not isinstance(data, dict):
        return {}

    primary: dict[str, list[str]] = {}
    secondary: dict[str, list[str]] = {}

    def _merge(dst: dict[str, list[str]], key: str, maps: list[str]) -> None:
        k = key.strip().lower()
        if not k:
            return
        cur = dst.setdefault(k, [])
        for m in maps:
            if m not in cur:
                cur.append(m)

    for top_key, entry in data.items():
        if not isinstance(entry, dict):
            continue
        maps = _extract_entry_color_maps(entry)
        if not maps:
            continue

        map_to = entry.get("mapTo")
        if isinstance(map_to, str) and map_to.strip() and map_to.strip().lower() != "unmapped_mat":
            _merge(primary, map_to, maps)

        mat_name = entry.get("name")
        if isinstance(mat_name, str) and mat_name.strip():
            _merge(secondary, mat_name, maps)
        if isinstance(top_key, str) and top_key.strip():
            _merge(secondary, top_key, maps)

    result = dict(primary)
    for k, maps in secondary.items():
        if k not in result:
            result[k] = list(maps)
        else:
            for m in maps:
                if m not in result[k]:
                    result[k].append(m)
    return result


def lookup_material_color_maps(mat_index: dict[str, list[str]], *names: str) -> list[str]:
    """Look up candidate color maps trying exact names and common COLLADA suffixes."""
    if not mat_index:
        return []
    candidates: list[str] = []
    seen_keys: set[str] = set()

    def _push_key(raw: str) -> None:
        k = raw.strip().lower()
        if k and k not in seen_keys:
            seen_keys.add(k)
            candidates.append(k)

    for nm in names:
        if not nm:
            continue
        _push_key(nm)
        for suffix in ("-material", "-effect", "_material"):
            if nm.lower().endswith(suffix) and len(nm) > len(suffix):
                _push_key(nm[: -len(suffix)])

    out: list[str] = []
    seen_maps: set[str] = set()
    for key in candidates:
        for ref in mat_index.get(key, ()):
            if ref not in seen_maps:
                seen_maps.add(ref)
                out.append(ref)
    return out
