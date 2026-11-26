# SPDX-FileCopyrightText: 2026 Blender5-Collada Authors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""BeamNG vehicle configuration (.pc) parser and part/mesh resolver (pure Python, no bpy)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .expr import eval_mesh_expr, eval_modifier_expr
from .lenient_json import parse
from .parts import (
    PartDef,
    SlotDef,
    dedup_part_slots,
    has_non_identity_modifiers,
)


@dataclass
class PcPaint:
    base_color_srgb: tuple[float, float, float] = (1.0, 1.0, 1.0)
    metallic: float = 0.0
    roughness: float = 0.5
    clearcoat: float = 0.0
    clearcoat_roughness: float = 0.05


def srgb_to_linear(c: float) -> float:
    """Convert a single sRGB channel [0..1] to linear light (IEC 61966-2-1)."""
    v = max(0.0, float(c))
    if v <= 0.04045:
        return v / 12.92
    return ((v + 0.055) / 1.055) ** 2.4


def _parse_single_paint_dict(item: dict[str, Any]) -> PcPaint:
    bc = item.get("baseColor")
    if isinstance(bc, (list, tuple)) and len(bc) >= 3:
        try:
            rgb = (max(0.0, float(bc[0])), max(0.0, float(bc[1])), max(0.0, float(bc[2])))
        except (TypeError, ValueError):
            rgb = (1.0, 1.0, 1.0)
    else:
        rgb = (1.0, 1.0, 1.0)

    def _f(key: str, default: float) -> float:
        val = item.get(key)
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            return max(0.0, min(1.0, float(val)))
        return default

    return PcPaint(
        base_color_srgb=rgb,
        metallic=_f("metallic", 0.0),
        roughness=_f("roughness", 0.5),
        clearcoat=_f("clearcoat", 0.0),
        clearcoat_roughness=_f("clearcoatRoughness", 0.05),
    )


def _parse_pc_paints(raw_paints: Any) -> list[PcPaint]:
    if not isinstance(raw_paints, list):
        return []
    out: list[PcPaint] = []
    for item in raw_paints[:3]:
        if isinstance(item, dict):
            out.append(_parse_single_paint_dict(item))
    return out


def parse_paint_library(doc: Any) -> dict[str, PcPaint]:
    """Parse a BeamNG *.paintLibrary.json (or info.json 'paints' dict) into {lower_name: PcPaint}."""
    out: dict[str, PcPaint] = {}

    def _walk(node: Any, key_hint: str = "") -> None:
        if isinstance(node, list):
            for item in node:
                _walk(item)
        elif isinstance(node, dict):
            if isinstance(node.get("baseColor"), (list, tuple)) and len(node["baseColor"]) >= 3:
                paint = _parse_single_paint_dict(node)
                nm = node.get("name")
                if isinstance(nm, str) and nm.strip():
                    out[nm.strip().lower()] = paint
                if key_hint and key_hint.strip().lower() not in out:
                    out[key_hint.strip().lower()] = paint
                return
            for k, v in node.items():
                if isinstance(v, (dict, list)):
                    _walk(v, key_hint=str(k) if isinstance(k, str) else "")

    _walk(doc)
    return out


@dataclass
class PcConfig:
    path: str
    fmt: int | None
    model: str | None
    main_part_name: str | None
    main_part_path: str | None
    by_name: dict[str, str] = field(default_factory=dict)
    by_path: dict[str, str] = field(default_factory=dict)
    vars: dict[str, Any] = field(default_factory=dict)
    paints: list[PcPaint] = field(default_factory=list)
    is_legacy: bool = False


@dataclass
class ResolvedPart:
    name: str
    slot_path: str
    slot_name: str
    slot_description: str
    from_common: bool
    file: str


@dataclass
class ResolvedMesh:
    name: str
    kind: str  # "flex" or "prop"
    part_name: str
    slot_path: str
    slot_description: str
    modifiers: dict[str, Any] = field(default_factory=dict)
    evaluated_modifiers: dict[str, Any] = field(default_factory=dict)
    needs_placement: bool = False
    source_category: str = "missing"  # "own", "common", "other_zip", "missing"
    from_expression: bool = False
    is_alternative: bool = False
    raw_expression: str = ""


@dataclass
class ResolvedConfig:
    pc_path: str
    main_part: str | None
    parts: list[ResolvedPart] = field(default_factory=list)
    meshes: list[ResolvedMesh] = field(default_factory=list)
    missing_parts: list[str] = field(default_factory=list)
    missing_meshes: list[str] = field(default_factory=list)
    other_zip_meshes: list[str] = field(default_factory=list)
    unused_path_keys: list[str] = field(default_factory=list)
    path_keys_used: int = 0
    path_keys_total: int = 0
    cycles: int = 0
    expr_count: int = 0
    duplicate_slot_warnings: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def primary_mesh_names(self) -> set[str]:
        return {m.name for m in self.meshes if not m.is_alternative and m.source_category != "missing"}

    def alternative_mesh_names(self) -> set[str]:
        return {m.name for m in self.meshes if m.is_alternative and m.source_category != "missing"}


def parse_pc(text: str, path: str = "") -> PcConfig:
    """Parse a BeamNG .pc file (format 2 or legacy flat slot map)."""
    doc = parse(text)
    if not isinstance(doc, dict):
        raise ValueError(f"Expected dict in .pc file '{path}', got {type(doc).__name__}")

    raw_vars = doc.get("vars")
    vars_dict = dict(raw_vars) if isinstance(raw_vars, dict) else {}
    paints_list = _parse_pc_paints(doc.get("paints"))

    if "parts" not in doc or not isinstance(doc.get("parts"), dict):
        # Legacy .pc: the top-level dict IS the slot map (minus 'vars')
        by_name: dict[str, str] = {}
        by_path: dict[str, str] = {}
        for k, v in doc.items():
            if k == "vars" or not isinstance(v, str):
                continue
            if k.startswith("/"):
                by_path[k] = v
            else:
                by_name[k] = v
        return PcConfig(
            path=path,
            fmt=doc.get("format") if isinstance(doc.get("format"), int) else None,
            model=doc.get("model") if isinstance(doc.get("model"), str) else None,
            main_part_name=None,
            main_part_path=None,
            by_name=by_name,
            by_path=by_path,
            vars=vars_dict,
            paints=paints_list,
            is_legacy=True,
        )

    parts_map = doc["parts"]
    by_name = {}
    by_path = {}
    for k, v in parts_map.items():
        if not isinstance(v, str):
            continue
        if k.startswith("/"):
            by_path[k] = v
        else:
            by_name[k] = v

    mpn = doc.get("mainPartName")
    mpp = doc.get("mainPartPath")
    return PcConfig(
        path=path,
        fmt=doc.get("format") if isinstance(doc.get("format"), int) else None,
        model=doc.get("model") if isinstance(doc.get("model"), str) else None,
        main_part_name=mpn if isinstance(mpn, str) and mpn else None,
        main_part_path=mpp if isinstance(mpp, str) and mpp else None,
        by_name=by_name,
        by_path=by_path,
        vars=vars_dict,
        paints=paints_list,
        is_legacy=False,
    )


def resolve_missing_part_fallback(
    slot: SlotDef,
    missing_choice: str,
    pool: dict[str, PartDef],
) -> str | None:
    """Decide what part to use when a .pc names a part that does not exist in the pool.

    Isolated per spec section 4.5. Verified against BeamNG lua/common/jbeam/slotSystem.lua:347-363:
    BeamNG resets the slot to ``slot.default`` (or leaves it empty if default is empty/missing).
    """
    _ = missing_choice
    if slot.default and slot.default != "none" and slot.default in pool:
        return slot.default
    return None


def merge_part_components(target: dict[str, Any], source: dict[str, Any]) -> None:
    """Recursively merge a part's ``components`` dict into ``target`` in pre-order walk order.

    Isolated per spec section 4.6. Matches BeamNG lua/common/jbeam/variables.lua:442-470
    (``unifyJournalC`` pre-order DFS + ``tableMergeRecursiveArray``).
    """
    for k, v in source.items():
        if isinstance(v, dict):
            existing = target.get(k)
            if isinstance(existing, dict):
                merge_part_components(existing, v)
            else:
                fresh: dict[str, Any] = {}
                merge_part_components(fresh, v)
                target[k] = fresh
        else:
            target[k] = v


def choose_default_config(config_paths: list[str], default_pc_name: str | None) -> str | None:
    """Choose the default .pc config for a vehicle.

    Isolated per spec section 3.2 & Open Question 4. Verified against
    lua/ge/extensions/core/vehicles.lua:716-718 (uses info.json ``default_pc`` if present,
    otherwise the first config discovered; we use alphabetical order for determinism).
    """
    if not config_paths:
        return None
    if default_pc_name:
        target = default_pc_name.lower()
        target_pc = target if target.endswith(".pc") else f"{target}.pc"
        for cp in config_paths:
            bname = cp.rsplit("/", 1)[-1].lower()
            if bname == target_pc or bname[:-3] == target:
                return cp
    return sorted(config_paths, key=lambda p: p.lower())[0]


def choose_main_part(
    pc: PcConfig | None,
    vehicle_parts: dict[str, PartDef],
    all_pool: dict[str, PartDef],
    model_dir: str | None,
) -> str | None:
    """Select the root/main part for a vehicle configuration (spec section 4.4)."""
    if pc is not None and pc.main_part_name and pc.main_part_name in all_pool:
        return pc.main_part_name

    mains = [pn for pn, p in vehicle_parts.items() if "main" in p.slot_types]
    if model_dir:
        prefix = f"vehicles/{model_dir.lower()}/"
        in_dir = [pn for pn in mains if vehicle_parts[pn].file.lower().startswith(prefix)]
    else:
        in_dir = []
    cands = in_dir or mains
    if not cands:
        return None
    if model_dir and model_dir in cands:
        return model_dir
    return sorted(cands)[0]


def _evaluate_modifiers(
    mods: dict[str, Any],
    vars_dict: dict[str, Any],
    components_dict: dict[str, Any],
    warnings: list[str],
) -> dict[str, Any]:
    """Evaluate any $= expressions inside pos/rot/scale modifier dicts; fall back to identity on failure."""
    if not mods:
        return {}
    out: dict[str, Any] = {}
    defaults = {
        "pos": {"x": 0.0, "y": 0.0, "z": 0.0},
        "rot": {"x": 0.0, "y": 0.0, "z": 0.0},
        "scale": {"x": 1.0, "y": 1.0, "z": 1.0},
    }
    for key, val in mods.items():
        if key in defaults and isinstance(val, dict):
            ev_vec: dict[str, float] = {}
            for axis, default_f in defaults[key].items():
                raw_comp = val.get(axis, default_f)
                num, warn = eval_modifier_expr(raw_comp, vars_dict, components_dict)
                if warn:
                    warnings.append(warn)
                ev_vec[axis] = default_f if num is None else num
            out[key] = ev_vec
        else:
            out[key] = val
    return out


def resolve_config(
    pc: PcConfig,
    vehicle_parts: dict[str, PartDef],
    common_parts: dict[str, PartDef] | None = None,
    *,
    model_dir: str | None = None,
    own_meshes: set[str] | None = None,
    common_meshes: set[str] | None = None,
    other_zip_meshes: set[str] | None = None,
    fallback_on_missing_part: bool = True,
) -> ResolvedConfig:
    """Walk the BeamNG slot tree for ``pc`` and resolve all parts, expressions, and meshes."""
    pool: dict[str, PartDef] = {}
    if common_parts:
        pool.update(common_parts)
    pool.update(vehicle_parts)

    own_set = own_meshes or set()
    common_set = common_meshes or set()
    other_set = other_zip_meshes or set()
    known_meshes = own_set | common_set | other_set

    if model_dir is None and pc.path.count("/") >= 2:
        parts_seg = pc.path.split("/")
        if parts_seg[0].lower() == "vehicles":
            model_dir = parts_seg[1]

    main_part = choose_main_part(pc, vehicle_parts, pool, model_dir)
    result = ResolvedConfig(
        pc_path=pc.path,
        main_part=main_part,
        path_keys_total=len(pc.by_path),
    )

    used_path_keys: set[str] = set()
    seen_visits: set[tuple[str, str]] = set()
    walked_parts: list[tuple[PartDef, str, str, str]] = []  # (part, slot_path, slot_name, slot_desc)
    components: dict[str, Any] = {}
    vars_dict: dict[str, Any] = {}

    def _walk(part_name: str, slot_path: str, slot_name: str, slot_desc: str, depth: int = 0) -> None:
        if not part_name or part_name == "none":
            return
        part = pool.get(part_name)
        if part is None:
            if part_name not in result.missing_parts:
                result.missing_parts.append(part_name)
            return

        visit_key = (part_name, slot_path)
        if visit_key in seen_visits:
            result.cycles += 1
            result.warnings.append(
                f"Cycle detected: part '{part_name}' already visited at '{slot_path}'"
            )
            return
        if depth > 50:
            result.cycles += 1
            result.warnings.append(
                f"Slot tree depth limit (50) exceeded at '{slot_path}' (part '{part_name}')"
            )
            return
        seen_visits.add(visit_key)
        from_common = (part_name not in vehicle_parts) and (common_parts is not None and part_name in common_parts)
        result.parts.append(
            ResolvedPart(
                name=part_name,
                slot_path=slot_path,
                slot_name=slot_name,
                slot_description=slot_desc,
                from_common=from_common,
                file=part.file,
            )
        )
        walked_parts.append((part, slot_path, slot_name, slot_desc))

        # Collect variables defaults and merge components in pre-order DFS walk order
        for vd in part.variables:
            if vd.default is not None:
                vars_dict[vd.name] = vd.default
        if part.components:
            merge_part_components(components, part.components)

        deduped_slots, dup_names = dedup_part_slots(part.slots)
        for dup in dup_names:
            result.duplicate_slot_warnings.append(f"Part '{part_name}' defines duplicate slot '{dup}' (kept last)")

        for slot in deduped_slots:
            spath = f"{slot_path}{slot.name}/"
            if spath in pc.by_path:
                chosen = pc.by_path[spath]
                used_path_keys.add(spath)
            elif slot.name in pc.by_name:
                chosen = pc.by_name[slot.name]
            else:
                chosen = slot.default

            if not chosen or chosen == "none":
                continue

            if chosen not in pool:
                if chosen not in result.missing_parts:
                    result.missing_parts.append(chosen)
                if fallback_on_missing_part:
                    fb = resolve_missing_part_fallback(slot, chosen, pool)
                    if fb:
                        result.warnings.append(
                            f"Missing part '{chosen}' in slot '{slot.name}'; falling back to default '{fb}'"
                        )
                        _walk(fb, spath, slot.name, slot.description, depth + 1)
                continue

            _walk(chosen, spath, slot.name, slot.description, depth + 1)

    if main_part:
        _walk(main_part, "/", "main", "Main")

    # Apply .pc user variable overrides on top of part variable defaults
    vars_dict.update(pc.vars)

    result.path_keys_used = len(used_path_keys)
    result.unused_path_keys = sorted(set(pc.by_path) - used_path_keys)

    def _classify(mesh_name: str) -> str:
        if mesh_name in own_set:
            return "own"
        if mesh_name in common_set:
            return "common"
        if mesh_name in other_set:
            return "other_zip"
        return "missing"

    missing_seen: set[str] = set()
    other_seen: set[str] = set()

    for part, slot_path, _slot_name, slot_desc in walked_parts:
        for mref in part.meshes:
            eval_mods = _evaluate_modifiers(mref.modifiers, vars_dict, components, result.warnings)
            non_id = has_non_identity_modifiers(eval_mods) or has_non_identity_modifiers(mref.modifiers)

            if mref.is_expr:
                result.expr_count += 1
                cands, is_multi, expr_warns = eval_mesh_expr(mref.mesh, vars_dict, components, known_meshes)
                result.warnings.extend(expr_warns)
                for idx, cand_name in enumerate(cands):
                    cat = _classify(cand_name)
                    if cat == "missing" and cand_name not in missing_seen:
                        missing_seen.add(cand_name)
                        result.missing_meshes.append(cand_name)
                    elif cat == "other_zip" and cand_name not in other_seen:
                        other_seen.add(cand_name)
                        result.other_zip_meshes.append(cand_name)
                    result.meshes.append(
                        ResolvedMesh(
                            name=cand_name,
                            kind=mref.kind,
                            part_name=part.name,
                            slot_path=slot_path,
                            slot_description=slot_desc,
                            modifiers=mref.modifiers,
                            evaluated_modifiers=eval_mods,
                            needs_placement=(non_id or cat == "common"),
                            source_category=cat,
                            from_expression=True,
                            is_alternative=(is_multi and idx > 0),
                            raw_expression=mref.mesh,
                        )
                    )
            else:
                cat = _classify(mref.mesh)
                if cat == "missing" and mref.mesh not in missing_seen:
                    missing_seen.add(mref.mesh)
                    result.missing_meshes.append(mref.mesh)
                elif cat == "other_zip" and mref.mesh not in other_seen:
                    other_seen.add(mref.mesh)
                    result.other_zip_meshes.append(mref.mesh)
                result.meshes.append(
                    ResolvedMesh(
                        name=mref.mesh,
                        kind=mref.kind,
                        part_name=part.name,
                        slot_path=slot_path,
                        slot_description=slot_desc,
                        modifiers=mref.modifiers,
                        evaluated_modifiers=eval_mods,
                        needs_placement=(non_id or cat == "common"),
                        source_category=cat,
                        from_expression=False,
                        is_alternative=False,
                    )
                )

    return result
