# SPDX-FileCopyrightText: 2026 Blender5-Collada Authors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""BeamNG jbeam table parser and part index (pure Python, no bpy)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .lenient_json import JbeamError, parse

# Prop columns whose normal data values are dicts ({x, y, z}), not row-local modifier dicts
_DICT_VALUED_COLUMNS = frozenset({
    "baseRotation",
    "rotation",
    "translation",
    "baseTranslation",
    "baseTranslationGlobal",
    "baseTranslationGlobalElastic",
    "baseTranslationGlobalRigid",
    "translationGlobal",
    "translationOffset",
    "baseRotationGlobal",
    "rotationGlobal",
    "lightColor",
    "lightAttenuation",
})

_NON_MESH_PROPS = frozenset({"SPOTLIGHT", "POINTLIGHT"})


@dataclass
class SlotDef:
    name: str
    default: str
    description: str
    allow_types: list[str] = field(default_factory=list)
    deny_types: list[str] = field(default_factory=list)
    fmt: int = 1
    modifiers: dict[str, Any] = field(default_factory=dict)


@dataclass
class MeshRef:
    kind: str  # "flex" or "prop"
    mesh: str
    is_expr: bool
    modifiers: dict[str, Any] = field(default_factory=dict)
    raw_row: dict[str, Any] = field(default_factory=dict)


@dataclass
class VarDef:
    name: str
    var_type: str = "range"
    default: Any = None
    min_val: Any = None
    max_val: Any = None


@dataclass
class PartDef:
    name: str
    file: str
    slot_types: list[str]
    title: str = ""
    slots: list[SlotDef] = field(default_factory=list)
    meshes: list[MeshRef] = field(default_factory=list)
    variables: list[VarDef] = field(default_factory=list)
    components: dict[str, Any] = field(default_factory=dict)
    glow_map: dict[str, dict[str, str]] = field(default_factory=dict)
    raw_slot_type: Any = None


def parse_jbeam_table(
    tbl: Any,
    *,
    allow_inline_trailing_dict: bool = True,
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Parse a jbeam section table into ``(row_dict, effective_modifiers)`` tuples.

    Rules:
      - Row 0 is the header (list of column names).
      - Dict rows update the running modifier dict for all following data rows.
      - List rows are mapped by header column index (never fixed column positions).
      - A trailing dict at the end of a data row applies only to that row (skipped when
        the column at that index is legitimately a dict-valued prop column).
    """
    if not isinstance(tbl, list) or not tbl or not isinstance(tbl[0], list):
        return []
    header = [str(h) for h in tbl[0]]
    n_hdr = len(header)
    running_mods: dict[str, Any] = {}
    out: list[tuple[dict[str, Any], dict[str, Any]]] = []

    for raw_row in tbl[1:]:
        if isinstance(raw_row, dict):
            for k, v in raw_row.items():
                if v == "":
                    running_mods.pop(k, None)
                else:
                    running_mods[k] = v
            continue
        if not isinstance(raw_row, list) or not raw_row:
            continue

        row_mods = dict(running_mods)
        data_end = len(raw_row)
        if allow_inline_trailing_dict and isinstance(raw_row[-1], dict):
            last_idx = len(raw_row) - 1
            col_name = header[last_idx] if last_idx < n_hdr else None
            if col_name is None or col_name not in _DICT_VALUED_COLUMNS:
                for k, v in raw_row[-1].items():
                    if v == "":
                        row_mods.pop(k, None)
                    else:
                        row_mods[k] = v
                data_end -= 1

        row_dict: dict[str, Any] = {}
        for idx in range(data_end):
            key = header[idx] if idx < n_hdr else f"_{idx}"
            row_dict[key] = raw_row[idx]
        out.append((row_dict, row_mods))

    return out


def dedup_part_slots(slots: list[SlotDef]) -> tuple[list[SlotDef], list[str]]:
    """Deduplicate slots with repeated names inside one part (last occurrence wins).

    Isolated as a single function per spec section 4.3. Matches BeamNG's
    lua/common/jbeam/slotSystem.lua where all rows are walked and later entries
    overwrite earlier ones in chosenPartsTree / slotPartMap.
    """
    last_idx: dict[str, int] = {}
    for idx, slot in enumerate(slots):
        last_idx[slot.name] = idx
    kept: list[SlotDef] = []
    dups: list[str] = []
    for idx, slot in enumerate(slots):
        if last_idx[slot.name] == idx:
            kept.append(slot)
        else:
            dups.append(slot.name)
    return kept, dups


def _vec3_differs(val: Any, default_xyz: tuple[float, float, float], eps: float = 1e-6) -> bool:
    if val is None or val == "":
        return False
    if isinstance(val, str):
        return True  # expression or non-numeric string
    if isinstance(val, dict):
        for axis, target in zip(("x", "y", "z"), default_xyz):
            comp = val.get(axis, target)
            if isinstance(comp, str):
                return True
            try:
                if abs(float(comp) - target) > eps:
                    return True
            except (TypeError, ValueError):
                return True
        return False
    if isinstance(val, (list, tuple)):
        for idx, target in enumerate(default_xyz):
            if idx >= len(val):
                continue
            comp = val[idx]
            if isinstance(comp, str):
                return True
            try:
                if abs(float(comp) - target) > eps:
                    return True
            except (TypeError, ValueError):
                return True
        return False
    return False


def has_non_identity_modifiers(mods: dict[str, Any]) -> bool:
    """Return True if effective flexbody/prop modifiers include non-identity pos/rot/scale or offsets."""
    if not mods:
        return False
    if _vec3_differs(mods.get("pos"), (0.0, 0.0, 0.0)):
        return True
    if _vec3_differs(mods.get("rot"), (0.0, 0.0, 0.0)):
        return True
    if _vec3_differs(mods.get("scale"), (1.0, 1.0, 1.0)):
        return True
    if _vec3_differs(mods.get("nodeOffset"), (0.0, 0.0, 0.0)):
        return True
    if _vec3_differs(mods.get("nodeMove"), (0.0, 0.0, 0.0)):
        return True
    return False


def _normalize_slot_types(raw: Any) -> list[str]:
    if isinstance(raw, str):
        return [raw] if raw else []
    if isinstance(raw, list):
        return [str(x) for x in raw if isinstance(x, str) and x]
    return []


def extract_part_def(part_name: str, part_data: dict[str, Any], source_file: str) -> PartDef:
    """Convert a parsed jbeam part dict into a normalized PartDef."""
    raw_st = part_data.get("slotType")
    slot_types = _normalize_slot_types(raw_st)

    info = part_data.get("information")
    title = ""
    if isinstance(info, dict) and isinstance(info.get("name"), str):
        title = info["name"]

    slots: list[SlotDef] = []
    if isinstance(part_data.get("slots"), list):
        for row, mods in parse_jbeam_table(part_data["slots"]):
            sname = row.get("type")
            if not isinstance(sname, str) or not sname:
                continue
            default = row.get("default")
            default_s = default if isinstance(default, str) else ""
            desc = row.get("description")
            desc_s = desc if isinstance(desc, str) else sname
            slots.append(
                SlotDef(
                    name=sname,
                    default=default_s,
                    description=desc_s,
                    allow_types=[sname],
                    deny_types=[],
                    fmt=1,
                    modifiers=mods,
                )
            )
    if isinstance(part_data.get("slots2"), list):
        for row, mods in parse_jbeam_table(part_data["slots2"]):
            sname = row.get("name")
            if not isinstance(sname, str) or not sname:
                continue
            default = row.get("default")
            default_s = default if isinstance(default, str) else ""
            desc = row.get("description")
            desc_s = desc if isinstance(desc, str) else sname
            allow = row.get("allowTypes")
            deny = row.get("denyTypes")
            slots.append(
                SlotDef(
                    name=sname,
                    default=default_s,
                    description=desc_s,
                    allow_types=list(allow) if isinstance(allow, list) else [],
                    deny_types=list(deny) if isinstance(deny, list) else [],
                    fmt=2,
                    modifiers=mods,
                )
            )

    meshes: list[MeshRef] = []
    if isinstance(part_data.get("flexbodies"), list):
        for row, mods in parse_jbeam_table(part_data["flexbodies"]):
            m = row.get("mesh")
            if isinstance(m, str) and m:
                meshes.append(
                    MeshRef(
                        kind="flex",
                        mesh=m,
                        is_expr=m.startswith("$"),
                        modifiers=mods,
                        raw_row=row,
                    )
                )
    if isinstance(part_data.get("props"), list):
        for row, mods in parse_jbeam_table(part_data["props"]):
            m = row.get("mesh")
            if isinstance(m, str) and m and m not in _NON_MESH_PROPS:
                meshes.append(
                    MeshRef(
                        kind="prop",
                        mesh=m,
                        is_expr=m.startswith("$"),
                        modifiers=mods,
                        raw_row=row,
                    )
                )

    variables: list[VarDef] = []
    if isinstance(part_data.get("variables"), list):
        for row, _mods in parse_jbeam_table(part_data["variables"]):
            vname = row.get("name")
            if not isinstance(vname, str) or not vname:
                continue
            variables.append(
                VarDef(
                    name=vname,
                    var_type=str(row.get("type", "range")),
                    default=row.get("default"),
                    min_val=row.get("min"),
                    max_val=row.get("max"),
                )
            )

    raw_comp = part_data.get("components")
    components = dict(raw_comp) if isinstance(raw_comp, dict) else {}

    glow_map: dict[str, dict[str, str]] = {}
    raw_glow = part_data.get("glowMap")
    if isinstance(raw_glow, dict):
        for mat_key, gval in raw_glow.items():
            if not isinstance(mat_key, str) or not mat_key.strip() or not isinstance(gval, dict):
                continue
            entry_map: dict[str, str] = {}
            for state in ("off", "on", "on_intense"):
                sv = gval.get(state)
                if isinstance(sv, str) and sv.strip():
                    entry_map[state] = sv.strip()
            if entry_map:
                glow_map[mat_key.strip()] = entry_map

    return PartDef(
        name=part_name,
        file=source_file,
        slot_types=slot_types,
        title=title,
        slots=slots,
        meshes=meshes,
        variables=variables,
        components=components,
        glow_map=glow_map,
        raw_slot_type=raw_st,
    )


class PartIndex:
    """Collection of parsed BeamNG jbeam parts."""

    def __init__(self) -> None:
        self.parts: dict[str, PartDef] = {}
        self.dup_parts: list[tuple[str, str, str]] = []
        self.jbeam_ok: int = 0
        self.jbeam_fail: list[tuple[str, str]] = []

    def add_jbeam_text(self, text: str, source_file: str) -> bool:
        try:
            doc = parse(text)
        except JbeamError as exc:
            self.jbeam_fail.append((source_file, str(exc)))
            return False
        if not isinstance(doc, dict):
            self.jbeam_fail.append((source_file, f"top-level {type(doc).__name__}"))
            return False
        self.jbeam_ok += 1
        for part_name, part_val in doc.items():
            if not isinstance(part_val, dict):
                continue
            if part_name in self.parts:
                self.dup_parts.append((part_name, source_file, self.parts[part_name].file))
            self.parts[part_name] = extract_part_def(part_name, part_val, source_file)
        return True

    @staticmethod
    def merged_pool(common: PartIndex | None, vehicle: PartIndex | None) -> dict[str, PartDef]:
        """Build a merged lookup dict: common parts first, vehicle parts overwrite on clash."""
        pool: dict[str, PartDef] = {}
        if common is not None:
            pool.update(common.parts)
        if vehicle is not None:
            pool.update(vehicle.parts)
        return pool
