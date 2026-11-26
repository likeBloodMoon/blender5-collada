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


class BeamngStageSpec:
    """Parsed single layer (Stages[i]) of a BeamNG v1.5 PBR material."""

    __slots__ = (
        "base_color_map",
        "color_palette_map",
        "ambient_occlusion_map",
        "metallic_map",
        "roughness_map",
        "clearcoat_map",
        "normal_map",
        "opacity_map",
        "emissive_map",
        "base_color_uv",
        "color_palette_uv",
        "ambient_occlusion_uv",
        "metallic_uv",
        "roughness_uv",
        "clearcoat_uv",
        "normal_uv",
        "opacity_uv",
        "emissive_uv",
        "base_color_factor",
        "metallic_factor",
        "roughness_factor",
        "clearcoat_factor",
        "clearcoat_roughness_factor",
        "opacity_factor",
        "has_opacity_factor",
        "emissive_factor",
        "instance_diffuse",
    )

    def __init__(
        self,
        *,
        base_color_map: str = "",
        color_palette_map: str = "",
        ambient_occlusion_map: str = "",
        metallic_map: str = "",
        roughness_map: str = "",
        clearcoat_map: str = "",
        normal_map: str = "",
        opacity_map: str = "",
        emissive_map: str = "",
        base_color_uv: int = 0,
        color_palette_uv: int = 0,
        ambient_occlusion_uv: int = 0,
        metallic_uv: int = 0,
        roughness_uv: int = 0,
        clearcoat_uv: int = 0,
        normal_uv: int = 0,
        opacity_uv: int = 0,
        emissive_uv: int = 0,
        base_color_factor: tuple[float, float, float, float] = (1.0, 1.0, 1.0, 1.0),
        metallic_factor: float = 0.0,
        roughness_factor: float = 0.5,
        clearcoat_factor: float = 0.0,
        clearcoat_roughness_factor: float = 0.05,
        opacity_factor: float = 1.0,
        has_opacity_factor: bool = False,
        emissive_factor: tuple[float, float, float] = (0.0, 0.0, 0.0),
        instance_diffuse: bool = False,
    ) -> None:
        self.base_color_map = base_color_map
        self.color_palette_map = color_palette_map
        self.ambient_occlusion_map = ambient_occlusion_map
        self.metallic_map = metallic_map
        self.roughness_map = roughness_map
        self.clearcoat_map = clearcoat_map
        self.normal_map = normal_map
        self.opacity_map = opacity_map
        self.emissive_map = emissive_map
        self.base_color_uv = base_color_uv
        self.color_palette_uv = color_palette_uv
        self.ambient_occlusion_uv = ambient_occlusion_uv
        self.metallic_uv = metallic_uv
        self.roughness_uv = roughness_uv
        self.clearcoat_uv = clearcoat_uv
        self.normal_uv = normal_uv
        self.opacity_uv = opacity_uv
        self.emissive_uv = emissive_uv
        self.base_color_factor = base_color_factor
        self.metallic_factor = metallic_factor
        self.roughness_factor = roughness_factor
        self.clearcoat_factor = clearcoat_factor
        self.clearcoat_roughness_factor = clearcoat_roughness_factor
        self.opacity_factor = opacity_factor
        self.has_opacity_factor = has_opacity_factor
        self.emissive_factor = emissive_factor
        self.instance_diffuse = instance_diffuse


class BeamngMaterialSpec:
    """Parsed BeamNG v1.5 PBR material definition from *.materials.json."""

    __slots__ = (
        "name",
        "is_v15",
        "stages",
        "translucent",
        "alpha_test",
        "alpha_ref",
        "double_sided",
    )

    def __init__(
        self,
        name: str,
        *,
        is_v15: bool = False,
        stages: list[BeamngStageSpec] | None = None,
        translucent: bool = False,
        alpha_test: bool = False,
        alpha_ref: float = 0.5,
        double_sided: bool = False,
    ) -> None:
        self.name = name
        self.is_v15 = is_v15
        self.stages: list[BeamngStageSpec] = stages if stages is not None else []
        self.translucent = translucent
        self.alpha_test = alpha_test
        self.alpha_ref = alpha_ref
        self.double_sided = double_sided


def _first_map(d: dict[str, Any], *keys: str) -> tuple[str, int]:
    """Return (first_valid_map_path, use_uv_index) from dict d checking keys in order."""
    for key in keys:
        if key not in d:
            continue
        refs = _extract_map_strings(d[key])
        if refs:
            uv_raw = d.get(f"{key}UseUV", 0)
            uv_idx = 1 if uv_raw in (1, True, "1") else 0
            return refs[0], uv_idx
    return "", 0


def _first_float(d: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        val = d.get(key)
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            return float(val)
    return None


def _first_bool(d: dict[str, Any], *keys: str) -> bool:
    for key in keys:
        val = d.get(key)
        if isinstance(val, bool):
            return val
        if isinstance(val, (int, float)):
            return bool(val)
    return False


def _parse_rgba_factor(val: Any) -> tuple[float, float, float, float]:
    if isinstance(val, (list, tuple)) and len(val) >= 3:
        try:
            r = max(0.0, float(val[0]))
            g = max(0.0, float(val[1]))
            b = max(0.0, float(val[2]))
            a = max(0.0, min(1.0, float(val[3]))) if len(val) >= 4 else 1.0
            return (r, g, b, a)
        except (TypeError, ValueError):
            pass
    return (1.0, 1.0, 1.0, 1.0)


def _parse_rgb_factor(val: Any, default: tuple[float, float, float]) -> tuple[float, float, float]:
    if isinstance(val, (list, tuple)) and len(val) >= 3:
        try:
            return (max(0.0, float(val[0])), max(0.0, float(val[1])), max(0.0, float(val[2])))
        except (TypeError, ValueError):
            pass
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        v = max(0.0, float(val))
        return (v, v, v)
    return default


def _is_v15_version(val: Any) -> bool:
    if isinstance(val, bool):
        return False
    if isinstance(val, (int, float)):
        return abs(float(val) - 1.5) < 1e-4
    if isinstance(val, str):
        try:
            return abs(float(val.strip()) - 1.5) < 1e-4
        except ValueError:
            return False
    return False


def _parse_stage(stage_dict: dict[str, Any], entry: dict[str, Any], *, is_base_layer: bool) -> BeamngStageSpec:
    def _map(*keys: str) -> tuple[str, int]:
        path, uv = _first_map(stage_dict, *keys)
        if not path and is_base_layer:
            path, uv = _first_map(entry, *keys)
        return path, uv

    bc_map, bc_uv = _map("baseColorMap", "colorMap", "diffuseMap")
    pal_map, pal_uv = _map("colorPaletteMap")
    ao_map, ao_uv = _map("ambientOcclusionMap")
    met_map, met_uv = _map("metallicMap")
    rgh_map, rgh_uv = _map("roughnessMap")
    coat_map, coat_uv = _map("clearCoatMap", "clearcoatMap")
    nrm_map, nrm_uv = _map("normalMap")
    op_map, op_uv = _map("opacityMap")
    em_map, em_uv = _map("emissiveMap")

    bc_raw = stage_dict.get("baseColorFactor")
    if bc_raw is None and is_base_layer:
        bc_raw = entry.get("baseColorFactor")
    bc_fac = _parse_rgba_factor(bc_raw)

    met_raw = _first_float(stage_dict, "metallicFactor")
    if met_raw is None and is_base_layer:
        met_raw = _first_float(entry, "metallicFactor")
    met_fac = max(0.0, min(1.0, met_raw)) if met_raw is not None else (1.0 if met_map else 0.0)

    rgh_raw = _first_float(stage_dict, "roughnessFactor")
    if rgh_raw is None and is_base_layer:
        rgh_raw = _first_float(entry, "roughnessFactor")
    rgh_fac = max(0.0, min(1.0, rgh_raw)) if rgh_raw is not None else (1.0 if rgh_map else 0.5)

    coat_raw = _first_float(stage_dict, "clearCoatFactor", "clearcoatFactor")
    if coat_raw is None and is_base_layer:
        coat_raw = _first_float(entry, "clearCoatFactor", "clearcoatFactor")
    coat_fac = max(0.0, min(1.0, coat_raw)) if coat_raw is not None else (1.0 if coat_map else 0.0)

    coat_rgh_raw = _first_float(stage_dict, "clearCoatRoughnessFactor", "clearcoatRoughnessFactor")
    if coat_rgh_raw is None and is_base_layer:
        coat_rgh_raw = _first_float(entry, "clearCoatRoughnessFactor", "clearcoatRoughnessFactor")
    coat_rgh_fac = max(0.0, min(1.0, coat_rgh_raw)) if coat_rgh_raw is not None else 0.05

    op_raw = _first_float(stage_dict, "opacityFactor")
    if op_raw is None and is_base_layer:
        op_raw = _first_float(entry, "opacityFactor")
    has_op = op_raw is not None
    op_fac = max(0.0, min(1.0, op_raw)) if op_raw is not None else 1.0

    em_default = (1.0, 1.0, 1.0) if em_map else (0.0, 0.0, 0.0)
    em_raw = stage_dict.get("emissiveFactor")
    if em_raw is None and is_base_layer:
        em_raw = entry.get("emissiveFactor")
    em_fac = _parse_rgb_factor(em_raw, em_default) if em_raw is not None else em_default

    inst_diff = _first_bool(stage_dict, "instanceDiffuse") or (
        is_base_layer and _first_bool(entry, "instanceDiffuse")
    )

    return BeamngStageSpec(
        base_color_map=bc_map,
        color_palette_map=pal_map,
        ambient_occlusion_map=ao_map,
        metallic_map=met_map,
        roughness_map=rgh_map,
        clearcoat_map=coat_map,
        normal_map=nrm_map,
        opacity_map=op_map,
        emissive_map=em_map,
        base_color_uv=bc_uv,
        color_palette_uv=pal_uv,
        ambient_occlusion_uv=ao_uv,
        metallic_uv=met_uv,
        roughness_uv=rgh_uv,
        clearcoat_uv=coat_uv,
        normal_uv=nrm_uv,
        opacity_uv=op_uv,
        emissive_uv=em_uv,
        base_color_factor=bc_fac,
        metallic_factor=met_fac,
        roughness_factor=rgh_fac,
        clearcoat_factor=coat_fac,
        clearcoat_roughness_factor=coat_rgh_fac,
        opacity_factor=op_fac,
        has_opacity_factor=has_op,
        emissive_factor=em_fac,
        instance_diffuse=inst_diff,
    )


def _parse_single_spec(name: str, entry: dict[str, Any]) -> BeamngMaterialSpec | None:
    if not _is_v15_version(entry.get("version")):
        return None
    stages_raw = entry.get("Stages")
    if not isinstance(stages_raw, list) or not stages_raw or not isinstance(stages_raw[0], dict):
        return None

    al_raw = entry.get("activeLayers", 1)
    if isinstance(al_raw, (int, float)) and not isinstance(al_raw, bool):
        active_layers = max(1, min(int(al_raw), len(stages_raw)))
    else:
        active_layers = 1

    parsed_stages: list[BeamngStageSpec] = []
    for idx in range(active_layers):
        st = stages_raw[idx]
        if isinstance(st, dict):
            parsed_stages.append(_parse_stage(st, entry, is_base_layer=(idx == 0)))
    if not parsed_stages:
        return None

    s0 = stages_raw[0]
    translucent = _first_bool(entry, "translucent") or _first_bool(s0, "translucent")
    alpha_test = _first_bool(entry, "alphaTest") or _first_bool(s0, "alphaTest")
    double_sided = _first_bool(entry, "doubleSided") or _first_bool(s0, "doubleSided")

    aref_raw = _first_float(entry, "alphaRef")
    if aref_raw is None:
        aref_raw = _first_float(s0, "alphaRef")
    if aref_raw is None:
        alpha_ref = 0.5
    elif aref_raw > 1.0:
        alpha_ref = max(0.0, min(1.0, aref_raw / 255.0))
    else:
        alpha_ref = max(0.0, min(1.0, aref_raw))

    return BeamngMaterialSpec(
        name=name,
        is_v15=True,
        stages=parsed_stages,
        translucent=translucent,
        alpha_test=alpha_test,
        alpha_ref=alpha_ref,
        double_sided=double_sided,
    )


def parse_beamng_material_specs(text: str) -> dict[str, BeamngMaterialSpec]:
    """Parse BeamNG *.materials.json into {lowercased_material_name: BeamngMaterialSpec}."""
    data = loads_lenient_json(text)
    if not isinstance(data, dict):
        return {}

    primary: dict[str, BeamngMaterialSpec] = {}
    secondary: dict[str, BeamngMaterialSpec] = {}

    for top_key, entry in data.items():
        if not isinstance(entry, dict):
            continue
        spec = _parse_single_spec(str(top_key), entry)
        if spec is None:
            continue

        map_to = entry.get("mapTo")
        if isinstance(map_to, str) and map_to.strip() and map_to.strip().lower() != "unmapped_mat":
            k = map_to.strip().lower()
            if k not in primary:
                primary[k] = spec

        mat_name = entry.get("name")
        if isinstance(mat_name, str) and mat_name.strip():
            k = mat_name.strip().lower()
            if k not in secondary:
                secondary[k] = spec
        if isinstance(top_key, str) and top_key.strip():
            k = top_key.strip().lower()
            if k not in secondary:
                secondary[k] = spec

    result = dict(primary)
    for k, spec in secondary.items():
        if k not in result:
            result[k] = spec
    return result


def lookup_material_spec(
    spec_index: dict[str, BeamngMaterialSpec], *names: str
) -> BeamngMaterialSpec | None:
    """Look up a BeamngMaterialSpec trying exact names and common COLLADA suffixes."""
    if not spec_index:
        return None
    for nm in names:
        if not nm:
            continue
        k = nm.strip().lower()
        if k in spec_index:
            return spec_index[k]
        for suffix in ("-material", "-effect", "_material"):
            if k.endswith(suffix) and len(k) > len(suffix):
                stripped = k[: -len(suffix)]
                if stripped in spec_index:
                    return spec_index[stripped]
    return None
