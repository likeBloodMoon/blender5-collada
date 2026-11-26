# SPDX-FileCopyrightText: 2026 Blender5-Collada Authors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""BeamNG.drive vehicle archive indexing, jbeam/.pc parsing, and config resolution.

All modules in this package except ``picker.py`` are pure Python (no ``bpy`` dependency)
so they can be unit-tested and run directly with standard Python.
"""

from __future__ import annotations

from .expr import eval_mesh_expr, eval_modifier_expr, extract_string_literals
from .lenient_json import JbeamError, parse
from .parts import (
    MeshRef,
    PartDef,
    PartIndex,
    SlotDef,
    VarDef,
    dedup_part_slots,
    has_non_identity_modifiers,
    parse_jbeam_table,
)
from .resolve import (
    PcConfig,
    ResolvedConfig,
    ResolvedMesh,
    ResolvedPart,
    choose_default_config,
    choose_main_part,
    merge_part_components,
    parse_pc,
    resolve_config,
    resolve_missing_part_fallback,
)
from .source import (
    ConfigEntry,
    ModelFileEntry,
    VehicleInfo,
    VehicleSource,
    clear_source_cache,
    extract_dae_node_names,
    load_vehicle_source,
)

__all__ = [
    "ConfigEntry",
    "JbeamError",
    "MeshRef",
    "ModelFileEntry",
    "PartDef",
    "PartIndex",
    "PcConfig",
    "ResolvedConfig",
    "ResolvedMesh",
    "ResolvedPart",
    "SlotDef",
    "VarDef",
    "VehicleInfo",
    "VehicleSource",
    "choose_default_config",
    "choose_main_part",
    "clear_source_cache",
    "dedup_part_slots",
    "eval_mesh_expr",
    "eval_modifier_expr",
    "extract_dae_node_names",
    "extract_string_literals",
    "has_non_identity_modifiers",
    "load_vehicle_source",
    "merge_part_components",
    "parse",
    "parse_jbeam_table",
    "parse_pc",
    "resolve_config",
    "resolve_missing_part_fallback",
]
