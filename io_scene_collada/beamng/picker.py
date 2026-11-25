# SPDX-FileCopyrightText: 2026 Blender5-Collada Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""BeamNG archive picker UI dialog and vehicle-config import orchestrator (bpy)."""

from __future__ import annotations

import json
import math
import os
import zipfile
from typing import Any

import bpy
from bpy.props import (
    BoolProperty,
    CollectionProperty,
    EnumProperty,
    IntProperty,
    StringProperty,
)
from mathutils import Vector

from ..cdae.importer import CdaeImporter, CdaeImportSettings
from ..import_collada import DocumentImporter, ImportSettings
from .resolve import ResolvedConfig, ResolvedMesh, resolve_config
from .source import (
    ConfigEntry,
    ModelFileEntry,
    VehicleSource,
    load_vehicle_source,
)

# Module-level references required by Blender for dynamic EnumProperty callbacks (spec 6.2)
_ENUM_VEHICLES_CACHE: list[tuple[str, str, str]] = []
_ENUM_CONFIGS_CACHE: list[tuple[str, str, str]] = []

_NO_CONFIG_ID = "__NONE__"


# ---------------------------------------------------------------------------
# Helpers for Blender collections, LayerCollection exclusion, and layout
# ---------------------------------------------------------------------------

def _find_layer_collection(root_lc, target_coll):
    if root_lc.collection == target_coll:
        return root_lc
    for ch in root_lc.children:
        found = _find_layer_collection(ch, target_coll)
        if found is not None:
            return found
    return None


def _set_collection_excluded(context, coll, exclude: bool = True) -> None:
    lc = _find_layer_collection(context.view_layer.layer_collection, coll)
    if lc is not None:
        lc.exclude = exclude


def _move_object_to_collection(ob, dst_coll) -> None:
    if dst_coll not in ob.users_collection:
        dst_coll.objects.link(ob)
    for c in list(ob.users_collection):
        if c != dst_coll:
            c.objects.unlink(ob)


def _ensure_subcollection(parent_coll, name: str, cache: dict[str, Any]) -> Any:
    existing = cache.get(name)
    if existing is not None:
        return existing
    coll = bpy.data.collections.new(name)
    parent_coll.children.link(coll)
    cache[name] = coll
    return coll


def _object_world_bbox(objs: list) -> tuple[Vector, Vector] | None:
    mins = Vector((math.inf, math.inf, math.inf))
    maxs = Vector((-math.inf, -math.inf, -math.inf))
    found = False
    for ob in objs:
        if ob.type != "MESH" or not ob.data or len(ob.data.vertices) == 0:
            continue
        mat = ob.matrix_world
        for corner in ob.bound_box:
            wc = mat @ Vector(corner)
            mins.x = min(mins.x, wc.x)
            mins.y = min(mins.y, wc.y)
            mins.z = min(mins.z, wc.z)
            maxs.x = max(maxs.x, wc.x)
            maxs.y = max(maxs.y, wc.y)
            maxs.z = max(maxs.z, wc.z)
            found = True
    return (mins, maxs) if found else None


def _spread_model_groups(context, groups: list[tuple[list, list]], gap: float = 0.5) -> None:
    """Lay out each imported model group's root objects on a grid without overlap."""
    if len(groups) <= 1:
        return
    context.view_layer.update()
    bboxes: list[tuple[Vector, Vector] | None] = [_object_world_bbox(all_obs) for _roots, all_obs in groups]
    max_w = 1.0
    max_d = 1.0
    for bb in bboxes:
        if bb is not None:
            max_w = max(max_w, bb[1].x - bb[0].x)
            max_d = max(max_d, bb[1].y - bb[0].y)

    cols = max(1, math.ceil(math.sqrt(len(groups))))
    step_x = max_w + gap
    step_y = max_d + gap

    for idx, ((roots, _all_obs), bb) in enumerate(zip(groups, bboxes)):
        col = idx % cols
        row = idx // cols
        cx = (bb[0].x + bb[1].x) * 0.5 if bb is not None else 0.0
        cy = (bb[0].y + bb[1].y) * 0.5 if bb is not None else 0.0
        dx = col * step_x - cx
        dy = row * step_y - cy
        for ob in roots:
            if ob.parent is None:
                ob.location.x += dx
                ob.location.y += dy
    context.view_layer.update()


def _detect_other_zip_meshes(src: VehicleSource, missing_names: list[str], *, cdae_mode: bool) -> set[str]:
    """Quickly check sibling vehicle zips in the same directory for cross-vehicle meshes."""
    if not missing_names:
        return set()
    other_found: set[str] = set()
    src_abs = src.path
    src_dir = os.path.dirname(src_abs) if src.is_zip else (src.vehicle_root_dir or "")
    remaining = set(missing_names)
    if not remaining or not src_dir or not os.path.isdir(src_dir):
        return other_found

    # Probe only sibling zips in src_dir whose stem matches a prefix of any missing mesh name
    candidate_stems: set[str] = set()
    for nm in remaining:
        parts = nm.split("_")
        if parts and parts[0]:
            candidate_stems.add(parts[0].lower())
        if len(parts) >= 2:
            candidate_stems.add(f"{parts[0]}_{parts[1]}".lower())

    for stem in sorted(candidate_stems):
        if stem == "common":
            continue
        cand_zip = os.path.join(src_dir, f"{stem}.zip")
        if not os.path.isfile(cand_zip) or os.path.abspath(cand_zip) == src_abs:
            continue
        try:
            sib = load_vehicle_source(cand_zip, index_dae_nodes=not cdae_mode)
            if cdae_mode:
                sib.ensure_cdae_nodes_indexed()
                sib_names = sib.all_cdae_mesh_names()
            else:
                sib_names = sib.all_dae_mesh_names()
            other_found |= (remaining & sib_names)
        except Exception:
            continue

    return other_found


def _find_config_entry(src: VehicleSource, config_spec: str) -> ConfigEntry | None:
    if not config_spec or config_spec == _NO_CONFIG_ID:
        return None
    norm_spec = config_spec.replace("\\", "/").strip().lstrip("/").lower()
    stem_spec = norm_spec[:-3] if norm_spec.endswith(".pc") else norm_spec
    for ce in src.all_configs:
        if ce.path.lower() == norm_spec:
            return ce
    for ce in src.all_configs:
        if ce.name.lower() == stem_spec or ce.path.rsplit("/", 1)[-1].lower() == norm_spec:
            return ce
    return None


def _build_part_mesh_slot_lookup(src: VehicleSource) -> dict[str, tuple[str, str]]:
    """Map every non-config mesh name referenced by any part in src to (part_name, slot_description)."""
    # First map slot_type -> friendly slot description from all parts' slot definitions
    st_to_desc: dict[str, str] = {}
    for part in src.part_index.parts.values():
        for sl in part.slots:
            if sl.description:
                st_to_desc.setdefault(sl.name, sl.description)
                for at in sl.allow_types:
                    st_to_desc.setdefault(at, sl.description)

    mesh_to_slot: dict[str, tuple[str, str]] = {}
    for part in src.part_index.parts.values():
        desc = ""
        for st in part.slot_types:
            if st in st_to_desc:
                desc = st_to_desc[st]
                break
        if not desc:
            desc = part.slot_types[0] if part.slot_types else "Other"
        for mref in part.meshes:
            if not mref.is_expr and mref.mesh not in mesh_to_slot:
                mesh_to_slot[mref.mesh] = (part.name, desc)
    return mesh_to_slot


def _import_single_model_from_source(
    context,
    src: VehicleSource,
    entry_path: str,
    target_coll,
    *,
    cdae_mode: bool,
    node_filter: set[str] | None,
    base_dae_settings: ImportSettings | None,
    base_cdae_settings: CdaeImportSettings | None,
    warnings_out: list[str],
    errors_out: list[str],
) -> tuple[bool, list, dict[str, list], list]:
    """Import one .dae or .cdae model from ``src`` into ``target_coll``.

    Returns (ok, mesh_objects, objects_by_node_name, root_objects).
    """
    if not cdae_mode:
        if src.is_zip:
            s = ImportSettings(
                filepath=src.path,
                import_units=base_dae_settings.import_units if base_dae_settings else False,
                custom_normals=base_dae_settings.custom_normals if base_dae_settings else True,
                fast_exact_normals=base_dae_settings.fast_exact_normals if base_dae_settings else False,
                sharp_edges_from_normals=base_dae_settings.sharp_edges_from_normals if base_dae_settings else False,
                sharp_angle_threshold=base_dae_settings.sharp_angle_threshold if base_dae_settings else math.radians(1.0),
                archive_mode="MAIN",
                node_filter=node_filter,
                archive_entry_filter=[entry_path],
                find_chains=base_dae_settings.find_chains if base_dae_settings else False,
                auto_connect=base_dae_settings.auto_connect if base_dae_settings else False,
                fix_orientation=base_dae_settings.fix_orientation if base_dae_settings else False,
                min_chain_length=base_dae_settings.min_chain_length if base_dae_settings else 0,
                keep_bind_info=base_dae_settings.keep_bind_info if base_dae_settings else False,
            )
        else:
            disk_fp = src.disk_paths.get(entry_path) or entry_path
            s = ImportSettings(
                filepath=disk_fp,
                import_units=base_dae_settings.import_units if base_dae_settings else False,
                custom_normals=base_dae_settings.custom_normals if base_dae_settings else True,
                fast_exact_normals=base_dae_settings.fast_exact_normals if base_dae_settings else False,
                sharp_edges_from_normals=base_dae_settings.sharp_edges_from_normals if base_dae_settings else False,
                sharp_angle_threshold=base_dae_settings.sharp_angle_threshold if base_dae_settings else math.radians(1.0),
                archive_mode="MAIN",
                node_filter=node_filter,
                find_chains=base_dae_settings.find_chains if base_dae_settings else False,
                auto_connect=base_dae_settings.auto_connect if base_dae_settings else False,
                fix_orientation=base_dae_settings.fix_orientation if base_dae_settings else False,
                min_chain_length=base_dae_settings.min_chain_length if base_dae_settings else 0,
                keep_bind_info=base_dae_settings.keep_bind_info if base_dae_settings else False,
            )
        imp = DocumentImporter(context, s)
        imp.target_collection = target_coll
        ok = imp.import_document()
        warnings_out.extend(imp.warnings)
        errors_out.extend(imp.errors)
        return ok, list(imp.imported_mesh_objects), dict(imp.imported_objects_by_node_name), list(imp.root_objects)

    # .cdae mode (in-memory read if inside a zip; direct read if loose on disk)
    cdae_bytes: bytes | None = None
    disk_fp = ""
    if src.is_zip:
        orig_name = src.zip_orig_name.get(entry_path)
        if orig_name is None:
            errors_out.append(f"CDAE entry '{entry_path}' not found in '{os.path.basename(src.path)}'")
            return False, [], {}, []
        with zipfile.ZipFile(src.path, "r") as zf:
            cdae_bytes = zf.read(orig_name)
    else:
        disk_fp = src.disk_paths.get(entry_path) or entry_path

    cs = CdaeImportSettings(
        filepath=disk_fp,
        lod_mode=base_cdae_settings.lod_mode if base_cdae_settings else "HIGHEST",
        import_collision=base_cdae_settings.import_collision if base_cdae_settings else False,
        normals_mode=base_cdae_settings.normals_mode if base_cdae_settings else "EXACT",
        merge_distance=base_cdae_settings.merge_distance if base_cdae_settings else 1e-5,
        sharp_angle_threshold=base_cdae_settings.sharp_angle_threshold if base_cdae_settings else math.radians(1.0),
        node_filter=node_filter,
        cdae_bytes=cdae_bytes,
        deselect_existing=False,
    )
    cimp = CdaeImporter(context, cs)
    cimp.target_collection = target_coll
    ok = cimp.import_document()
    warnings_out.extend(cimp.warnings)
    errors_out.extend(cimp.errors)
    return ok, list(cimp.imported_mesh_objects), dict(cimp.imported_objects_by_node_name), list(cimp.root_objects)


def _write_report_text_block(
    vehicle_name: str,
    config_name: str,
    src: VehicleSource,
    res: ResolvedConfig,
    own_count: int,
    common_count: int,
    warnings: list[str],
) -> None:
    title = f"BeamNG import \u2013 {vehicle_name} {config_name}"
    txt = bpy.data.texts.get(title)
    if txt is None:
        txt = bpy.data.texts.new(title)
    txt.clear()

    lines = [
        f"BeamNG Vehicle Import Report: {vehicle_name} [{config_name}]",
        f"Source: {src.path}",
        f"Config: {res.pc_path}",
        f"Main part: {res.main_part or '(none)'}",
        f"Resolved parts: {len(res.parts)}",
        f"Imported meshes: {own_count + common_count} ({own_count} own, {common_count} common.zip)",
        f"Path keys: {res.path_keys_used}/{res.path_keys_total} used",
        "",
    ]
    if res.missing_parts:
        lines.append(f"Missing parts ({len(res.missing_parts)}):")
        for mp in res.missing_parts:
            lines.append(f"  - {mp}")
        lines.append("")
    if res.other_zip_meshes:
        lines.append(f"Meshes in other vehicle zips ({len(res.other_zip_meshes)}):")
        for om in res.other_zip_meshes:
            lines.append(f"  - {om}")
        lines.append("")
    if res.missing_meshes:
        lines.append(f"Missing meshes ({len(res.missing_meshes)}):")
        for mm in res.missing_meshes:
            lines.append(f"  - {mm}")
        lines.append("")
    if res.unused_path_keys:
        lines.append(f"Unused / stale slot path keys ({len(res.unused_path_keys)}):")
        for uk in res.unused_path_keys:
            lines.append(f"  - {uk}")
        lines.append("")
    if warnings:
        lines.append(f"Warnings ({len(warnings)}):")
        for w in warnings:
            lines.append(f"  - {w}")
        lines.append("")

    txt.write("\n".join(lines))


def execute_beamng_import(
    context,
    *,
    filepath: str,
    cdae_mode: bool = False,
    beamng_config: str = "",
    beamng_parts_mode: str = "ALL",
    archive_models: str = "",
    use_common: bool = True,
    spread_out: bool = False,
    base_dae_settings: ImportSettings | None = None,
    base_cdae_settings: CdaeImportSettings | None = None,
) -> tuple[bool, str, list[str], list[str]]:
    """Shared execution entry point for both interactive picker and scripted operator calls."""
    warnings: list[str] = []
    errors: list[str] = []

    alc = context.view_layer.active_layer_collection
    active_coll = alc.collection if alc is not None else context.scene.collection

    src = load_vehicle_source(filepath, index_dae_nodes=not cdae_mode)
    if cdae_mode:
        src.ensure_cdae_nodes_indexed()
    warnings.extend(src.warnings)

    models_list = src.cdae_models if cdae_mode else src.dae_models
    explicit_models = [p.strip() for p in archive_models.split(";") if p.strip()] if archive_models else []
    src_bname = os.path.basename(src.path)

    # Deselect existing objects once
    for ob in context.view_layer.objects:
        ob.select_set(False)

    # Validate explicit beamng_config instead of silently falling back to ALL
    ce: ConfigEntry | None = None
    if beamng_config and beamng_config != _NO_CONFIG_ID:
        ce = _find_config_entry(src, beamng_config)
        if ce is None:
            errors.append(f"BeamNG config '{beamng_config}' not found in '{src_bname}'")
            return False, "", warnings, errors

    # ---- Mode 1: No config / browsing models (e.g. common.zip or parts_mode == "ALL") ----
    if ce is None or beamng_parts_mode == "ALL":
        if explicit_models:
            want_ci = {p.replace("\\", "/").lstrip("/").lower() for p in explicit_models}
            selected = [m for m in models_list if m.path.lower() in want_ci]
            if not selected:
                errors.append(f"None of the selected models were found in '{src_bname}'")
                return False, "", warnings, errors
        else:
            selected = list(models_list)
            if not selected:
                kind = ".cdae" if cdae_mode else ".dae"
                errors.append(f"No {kind} files found in '{src_bname}'")
                return False, "", warnings, errors

        stem_counts: dict[str, int] = {}
        for m in selected:
            st = os.path.splitext(m.path.rsplit("/", 1)[-1])[0].lower()
            stem_counts[st] = stem_counts.get(st, 0) + 1

        groups: list[tuple[list, list]] = []
        total_meshes = 0
        any_ok = False
        for m in selected:
            if len(selected) > 1:
                stem = os.path.splitext(m.path.rsplit("/", 1)[-1])[0] or "Model"
                if stem_counts.get(stem.lower(), 0) > 1 and "/" in m.path:
                    parent_seg = m.path.rsplit("/", 2)[-2]
                    cname = f"{parent_seg}_{stem}"
                else:
                    cname = stem
                target = bpy.data.collections.new(cname)
                active_coll.children.link(target)
            else:
                target = active_coll

            ok, mesh_obs, _by_node, roots = _import_single_model_from_source(
                context,
                src,
                m.path,
                target,
                cdae_mode=cdae_mode,
                node_filter=None,
                base_dae_settings=base_dae_settings,
                base_cdae_settings=base_cdae_settings,
                warnings_out=warnings,
                errors_out=errors,
            )
            if ok:
                any_ok = True
                total_meshes += len(mesh_obs)
                for ob in mesh_obs:
                    ob["beamng_source"] = f"{src_bname}:{m.path}"
                groups.append((roots, mesh_obs))

        if spread_out and len(groups) > 1:
            _spread_model_groups(context, groups)

        info = f"{src_bname}: imported {len(groups)} model(s), {total_meshes} mesh(es)"
        return any_ok, info, warnings, errors

    # ---- Mode 2: Vehicle configuration import (CONFIG or CONFIG_PLUS_HIDDEN) ----
    assert ce.pc is not None
    common_src: VehicleSource | None = None
    if use_common:
        if src.common_path:
            common_src = load_vehicle_source(src.common_path, index_dae_nodes=not cdae_mode)
            if cdae_mode:
                common_src.ensure_cdae_nodes_indexed()
        elif src_bname.lower() != "common.zip":
            warnings.append("Parts from common.zip are unavailable: configs will be incomplete")

    own_mesh_names = src.all_cdae_mesh_names() if cdae_mode else src.all_dae_mesh_names()
    common_mesh_names = (
        (common_src.all_cdae_mesh_names() if cdae_mode else common_src.all_dae_mesh_names())
        if common_src is not None
        else set()
    )
    common_parts = common_src.part_index.parts if common_src is not None else {}

    res = resolve_config(
        ce.pc,
        src.part_index.parts,
        common_parts,
        model_dir=ce.vehicle or None,
        own_meshes=own_mesh_names,
        common_meshes=common_mesh_names,
        other_zip_meshes=set(),
        fallback_on_missing_part=True,
    )

    # Check sibling zips for cross-vehicle meshes so they are reported as other_zip, not missing
    if res.missing_meshes:
        other_hits = _detect_other_zip_meshes(src, res.missing_meshes, cdae_mode=cdae_mode)
        if other_hits:
            res.other_zip_meshes = sorted(set(res.other_zip_meshes) | other_hits)
            res.missing_meshes = [m for m in res.missing_meshes if m not in other_hits]
            for rm in res.meshes:
                if rm.name in other_hits and rm.source_category == "missing":
                    rm.source_category = "other_zip"

    warnings.extend(res.warnings)

    # Build lookup of first ResolvedMesh per mesh name (primary beats alternative)
    mesh_meta: dict[str, ResolvedMesh] = {}
    for rm in res.meshes:
        if rm.source_category not in ("own", "common"):
            continue
        prev = mesh_meta.get(rm.name)
        if prev is None or (prev.is_alternative and not rm.is_alternative):
            mesh_meta[rm.name] = rm

    own_primary = {nm for nm, rm in mesh_meta.items() if rm.source_category == "own" and not rm.is_alternative}
    own_alt_expr = {nm for nm, rm in mesh_meta.items() if rm.source_category == "own" and rm.is_alternative}
    com_primary = {nm for nm, rm in mesh_meta.items() if rm.source_category == "common" and not rm.is_alternative}
    com_alt_expr = {nm for nm, rm in mesh_meta.items() if rm.source_category == "common" and rm.is_alternative}

    veh_name = ce.vehicle or os.path.splitext(src_bname)[0]
    root_coll = bpy.data.collections.new(f"{veh_name} [{ce.name}]")
    active_coll.children.link(root_coll)
    sub_cache: dict[str, Any] = {}
    alt_sub_cache: dict[str, Any] = {}

    def _get_needs_placement_coll():
        return _ensure_subcollection(root_coll, "Needs placement", sub_cache)

    def _get_alternatives_coll():
        c = _ensure_subcollection(root_coll, "Alternatives", sub_cache)
        _set_collection_excluded(context, c, True)
        return c

    def _get_alt_slot_coll(slot_desc: str):
        parent = _get_alternatives_coll()
        cname = slot_desc or "Other"
        c = _ensure_subcollection(parent, cname, alt_sub_cache)
        _set_collection_excluded(context, c, True)
        return c

    def _get_unreferenced_coll():
        c = _ensure_subcollection(root_coll, "Unreferenced", sub_cache)
        _set_collection_excluded(context, c, True)
        return c

    def _tag_object(ob, rm: ResolvedMesh | None, source_str: str, fallback_part: str = "") -> None:
        ob["beamng_part"] = rm.part_name if rm else fallback_part
        ob["beamng_slot_path"] = rm.slot_path if rm else ""
        ob["beamng_config"] = ce.name
        ob["beamng_source"] = source_str
        if rm and (rm.needs_placement or rm.evaluated_modifiers or rm.modifiers):
            mods_payload = rm.evaluated_modifiers or rm.modifiers
            ob["beamng_modifiers"] = json.dumps(mods_payload, sort_keys=True)

    # Determine which own model files to import
    own_file_map = (
        src.cdae_files_for_meshes(own_primary | own_alt_expr)
        if cdae_mode
        else src.dae_files_for_meshes(own_primary | own_alt_expr)
    )
    if explicit_models:
        want_ci = {p.replace("\\", "/").lstrip("/").lower() for p in explicit_models}
        own_models_to_import = [m for m in models_list if m.path.lower() in want_ci]
        if not own_models_to_import:
            active_coll.children.unlink(root_coll)
            bpy.data.collections.remove(root_coll)
            errors.append(f"None of the selected models were found in '{src_bname}'")
            return False, "", warnings, errors
    elif beamng_parts_mode == "CONFIG_PLUS_HIDDEN":
        own_models_to_import = list(models_list)
    else:
        own_models_to_import = [m for m in models_list if m.path in own_file_map]

    part_slot_lookup = _build_part_mesh_slot_lookup(src) if beamng_parts_mode == "CONFIG_PLUS_HIDDEN" else {}

    imported_own_names: set[str] = set()
    imported_common_names: set[str] = set()
    groups_for_spread: list[tuple[list, list]] = []

    for m in own_models_to_import:
        if beamng_parts_mode == "CONFIG":
            want_here = own_file_map.get(m.path, set()) - imported_own_names
            if not want_here:
                continue
            nf: set[str] | None = want_here
        else:
            nf = None
        model_stem = os.path.splitext(m.path.rsplit("/", 1)[-1])[0] or veh_name
        model_coll = _ensure_subcollection(root_coll, model_stem, sub_cache)

        ok, mesh_obs, by_node, roots = _import_single_model_from_source(
            context,
            src,
            m.path,
            model_coll,
            cdae_mode=cdae_mode,
            node_filter=nf,
            base_dae_settings=base_dae_settings,
            base_cdae_settings=base_cdae_settings,
            warnings_out=warnings,
            errors_out=errors,
        )
        if not ok:
            continue
        groups_for_spread.append((roots, mesh_obs))

        for node_nm, obs in by_node.items():
            rm = mesh_meta.get(node_nm)
            src_tag = f"{src_bname}:{m.path}"
            for ob in obs:
                if rm is not None and rm.source_category == "own":
                    imported_own_names.add(node_nm)
                    _tag_object(ob, rm, src_tag)
                    if rm.is_alternative:
                        _move_object_to_collection(ob, _get_alternatives_coll())
                    elif rm.needs_placement:
                        _move_object_to_collection(ob, _get_needs_placement_coll())
                elif beamng_parts_mode == "CONFIG_PLUS_HIDDEN":
                    slot_info = part_slot_lookup.get(node_nm)
                    if slot_info is not None:
                        p_nm, s_desc = slot_info
                        _tag_object(ob, None, src_tag, fallback_part=p_nm)
                        _move_object_to_collection(ob, _get_alt_slot_coll(s_desc))
                    else:
                        _tag_object(ob, None, src_tag, fallback_part="")
                        _move_object_to_collection(ob, _get_unreferenced_coll())
                else:
                    _tag_object(ob, rm, src_tag)

        # Remove empty model sub-collection if every mesh moved into Needs placement / Alternatives
        if len(model_coll.objects) == 0 and len(model_coll.children) == 0:
            root_coll.children.unlink(model_coll)
            bpy.data.collections.remove(model_coll)
            sub_cache.pop(model_stem, None)

    # Import common.zip meshes referenced by the config (Phase 1 -> Needs placement / Alternatives)
    if common_src is not None and (com_primary or com_alt_expr):
        com_file_map = (
            common_src.cdae_files_for_meshes(com_primary | com_alt_expr)
            if cdae_mode
            else common_src.dae_files_for_meshes(com_primary | com_alt_expr)
        )
        com_bname = os.path.basename(common_src.path)
        for com_entry in sorted(com_file_map.keys()):
            want_here = com_file_map[com_entry] - imported_common_names
            if not want_here:
                continue
            np_coll = _get_needs_placement_coll()
            ok, mesh_obs, by_node, roots = _import_single_model_from_source(
                context,
                common_src,
                com_entry,
                np_coll,
                cdae_mode=cdae_mode,
                node_filter=want_here,
                base_dae_settings=base_dae_settings,
                base_cdae_settings=base_cdae_settings,
                warnings_out=warnings,
                errors_out=errors,
            )
            if not ok:
                continue
            for node_nm, obs in by_node.items():
                rm = mesh_meta.get(node_nm)
                src_tag = f"{com_bname}:{com_entry}"
                if rm is not None:
                    imported_common_names.add(node_nm)
                for ob in obs:
                    _tag_object(ob, rm, src_tag)
                    if rm is not None and rm.is_alternative:
                        _move_object_to_collection(ob, _get_alternatives_coll())
                    else:
                        _move_object_to_collection(ob, np_coll)

    if spread_out and len(groups_for_spread) > 1:
        _spread_model_groups(context, groups_for_spread)

    total_imp = len(imported_own_names) + len(imported_common_names)
    if total_imp == 0 and len(root_coll.objects) == 0 and len(root_coll.children) == 0:
        active_coll.children.unlink(root_coll)
        bpy.data.collections.remove(root_coll)
        errors.append(f"{veh_name} {ce.name}: no matching meshes found in selected model(s)")
        return False, "", warnings, errors

    _write_report_text_block(
        veh_name,
        ce.name,
        src,
        res,
        len(imported_own_names),
        len(imported_common_names),
        warnings,
    )

    summary = (
        f"{veh_name} {ce.name}: {len(res.parts)} parts, {total_imp} meshes "
        f"({len(imported_own_names)} {src_bname}, {len(imported_common_names)} common.zip), "
        f"{len(res.missing_meshes)} missing"
    )
    if res.other_zip_meshes:
        summary += f", {len(res.other_zip_meshes)} in other vehicle zip(s)"
    return True, summary, warnings, errors


# ---------------------------------------------------------------------------
# Picker UI PropertyGroup, UIList, and Operators
# ---------------------------------------------------------------------------

class BeamNGModelItem(bpy.types.PropertyGroup):
    path: StringProperty(name="Path")
    display_name: StringProperty(name="Model")
    size_label: StringProperty(name="Size")
    status_label: StringProperty(name="Status")
    selected: BoolProperty(name="Import", default=True)
    used_by_config: BoolProperty(name="Used", default=False)


class COLLADA_UL_beamng_models(bpy.types.UIList):
    bl_idname = "COLLADA_UL_beamng_models"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        row = layout.row(align=True)
        row.prop(item, "selected", text="")
        row.label(text=item.display_name)
        sub = row.row(align=True)
        sub.alignment = "RIGHT"
        if item.status_label:
            sub.label(text=item.status_label)
        sub.label(text=item.size_label)

    def filter_items(self, context, data, propname):
        items = getattr(data, propname)
        flt = getattr(data, "model_filter", "").strip().lower()
        flags = []
        for it in items:
            if not flt or flt in it.path.lower() or flt in it.display_name.lower():
                flags.append(self.bitflag_filter_item)
            else:
                flags.append(0)
        return flags, []


def _format_size(size_bytes: int) -> str:
    if size_bytes >= 1_000_000:
        return f"{size_bytes / 1_000_000:.1f} MB"
    if size_bytes >= 1_000:
        return f"{size_bytes / 1_000:.0f} KB"
    return f"{size_bytes} B"


def _get_vehicle_items(self, context):
    global _ENUM_VEHICLES_CACHE
    items: list[tuple[str, str, str]] = []
    if self.filepath and os.path.exists(self.filepath):
        src = load_vehicle_source(self.filepath, index_dae_nodes=False)
        for vname in sorted(src.vehicles.keys(), key=str.lower):
            items.append((vname, vname, f"Vehicle '{vname}'"))
    if not items:
        items.append(("__NONE__", "(none)", "No BeamNG vehicle folder detected"))
    _ENUM_VEHICLES_CACHE = items
    return _ENUM_VEHICLES_CACHE


def _get_config_items(self, context):
    global _ENUM_CONFIGS_CACHE
    items: list[tuple[str, str, str]] = [
        (_NO_CONFIG_ID, "(all parts, no config)", "Import selected models without filtering by a .pc config"),
    ]
    if self.filepath and os.path.exists(self.filepath):
        src = load_vehicle_source(self.filepath, index_dae_nodes=False)
        vinfo = src.vehicles.get(self.vehicle_name)
        configs = vinfo.configs if vinfo is not None else src.all_configs
        for ce in configs:
            tip = ce.config_type if ce.config_type else ce.path
            items.append((ce.path, ce.name, tip))
    _ENUM_CONFIGS_CACHE = items
    return _ENUM_CONFIGS_CACHE


def _update_model_ticks_for_config(op) -> None:
    if not op.filepath or not os.path.exists(op.filepath):
        return
    cdae_mode = bool(op.cdae_mode)
    src = load_vehicle_source(op.filepath, index_dae_nodes=not cdae_mode)
    if cdae_mode:
        src.ensure_cdae_nodes_indexed()

    if op.config_path == _NO_CONFIG_ID or op.parts_mode == "ALL":
        for item in op.models:
            item.used_by_config = False
            item.status_label = ""
        return

    ce = _find_config_entry(src, op.config_path)
    if ce is None or ce.pc is None:
        return

    common_src = None
    if op.use_common and src.common_path:
        common_src = load_vehicle_source(src.common_path, index_dae_nodes=False)
    common_parts = common_src.part_index.parts if common_src is not None else {}
    own_names = src.all_cdae_mesh_names() if cdae_mode else src.all_dae_mesh_names()
    res = resolve_config(
        ce.pc,
        src.part_index.parts,
        common_parts,
        model_dir=ce.vehicle or None,
        own_meshes=own_names,
        common_meshes=set(),
        other_zip_meshes=set(),
    )
    used_names = {m.name for m in res.meshes if m.source_category == "own"}
    used_files = (
        set(src.cdae_files_for_meshes(used_names).keys())
        if cdae_mode
        else set(src.dae_files_for_meshes(used_names).keys())
    )
    for item in op.models:
        used = item.path in used_files
        item.used_by_config = used
        item.selected = used or op.parts_mode == "CONFIG_PLUS_HIDDEN"
        item.status_label = "(used by config)" if used else "(not used)"


def _on_vehicle_changed(self, context):
    if not self.filepath or not os.path.exists(self.filepath):
        return
    src = load_vehicle_source(self.filepath, index_dae_nodes=False)
    default_ce = src.default_config_for_vehicle(self.vehicle_name)
    self.config_path = default_ce.path if default_ce is not None else _NO_CONFIG_ID
    _update_model_ticks_for_config(self)


def _on_config_or_mode_changed(self, context):
    _update_model_ticks_for_config(self)


class WM_OT_collada_beamng_select_models(bpy.types.Operator):
    """Select or deselect all visible models in the BeamNG archive picker"""

    bl_idname = "wm.collada_beamng_select_models"
    bl_label = "Select Models"
    bl_options = {"INTERNAL"}

    select_all: BoolProperty(default=True)

    def execute(self, context):
        op = _ACTIVE_PICKER_OP
        if op is not None:
            flt = op.model_filter.strip().lower()
            for it in op.models:
                if not flt or flt in it.path.lower() or flt in it.display_name.lower():
                    it.selected = self.select_all
        return {"FINISHED"}


_ACTIVE_PICKER_OP: Any = None


class WM_OT_collada_beamng_picker(bpy.types.Operator):
    """Choose BeamNG vehicle configuration and models to import from an archive or folder (experimental)"""

    bl_idname = "wm.collada_beamng_picker"
    bl_label = "Import BeamNG Archive [Experimental]"
    bl_options = {"UNDO", "INTERNAL"}

    filepath: StringProperty(options={"HIDDEN"})
    cdae_mode: BoolProperty(default=False, options={"HIDDEN"})

    # Forwarded base import settings
    import_units: BoolProperty(default=False, options={"HIDDEN"})
    custom_normals: BoolProperty(default=True, options={"HIDDEN"})
    fast_exact_normals: BoolProperty(default=False, options={"HIDDEN"})
    sharp_edges_from_normals: BoolProperty(default=False, options={"HIDDEN"})
    sharp_angle_threshold: bpy.props.FloatProperty(default=math.radians(1.0), options={"HIDDEN"})
    cdae_lod_mode: StringProperty(default="HIGHEST", options={"HIDDEN"})
    cdae_import_collision: BoolProperty(default=False, options={"HIDDEN"})
    cdae_normals_mode: StringProperty(default="EXACT", options={"HIDDEN"})
    cdae_merge_distance: bpy.props.FloatProperty(default=1e-5, options={"HIDDEN"})

    vehicle_name: EnumProperty(
        name="Vehicle",
        items=_get_vehicle_items,
        update=_on_vehicle_changed,
    )
    config_path: EnumProperty(
        name="Config",
        items=_get_config_items,
        update=_on_config_or_mode_changed,
    )
    parts_mode: EnumProperty(
        name="Parts",
        items=(
            ("CONFIG", "Only this config", "Import only meshes used by the resolved config"),
            (
                "CONFIG_PLUS_HIDDEN",
                "Config + other parts hidden",
                "Import everything, putting non-config meshes into excluded Alternatives collections",
            ),
            ("ALL", "Everything", "Import all selected models without config filtering"),
        ),
        default="CONFIG",
        update=_on_config_or_mode_changed,
    )
    use_common: BoolProperty(
        name="Use parts from common.zip",
        description="Resolve slots and load shared meshes from sibling common.zip",
        default=True,
        update=_on_config_or_mode_changed,
    )
    model_filter: StringProperty(
        name="Filter",
        description="Filter models by path substring",
        default="",
    )
    spread_out: BoolProperty(
        name="Spread selected models out side by side",
        description="Lay out each imported model on a grid instead of stacking at the origin",
        default=False,
    )
    models: CollectionProperty(type=BeamNGModelItem)
    active_model_index: IntProperty(default=0)
    common_status: StringProperty(default="", options={"HIDDEN"})

    def _load_saved_choices(self, context) -> dict[str, Any]:
        raw = getattr(context.scene, "collada_beamng_last_choices", "")
        if not raw:
            return {}
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                return data.get(os.path.basename(self.filepath).lower(), {})
        except Exception:
            pass
        return {}

    def _save_choices(self, context) -> None:
        raw = getattr(context.scene, "collada_beamng_last_choices", "")
        try:
            data = json.loads(raw) if raw else {}
            if not isinstance(data, dict):
                data = {}
        except Exception:
            data = {}
        data[os.path.basename(self.filepath).lower()] = {
            "vehicle_name": self.vehicle_name,
            "config_path": self.config_path,
            "parts_mode": self.parts_mode,
            "use_common": bool(self.use_common),
            "spread_out": bool(self.spread_out),
            "selected_models": [it.path for it in self.models if it.selected],
        }
        try:
            context.scene.collada_beamng_last_choices = json.dumps(data)
        except Exception:
            pass

    def invoke(self, context, event):
        global _ACTIVE_PICKER_OP
        _ACTIVE_PICKER_OP = self

        cdae_mode = bool(self.cdae_mode)
        src = load_vehicle_source(self.filepath, index_dae_nodes=not cdae_mode)
        if cdae_mode:
            src.ensure_cdae_nodes_indexed()

        self.common_status = f"found: {src.common_path}" if src.common_path else "not found"

        # Populate CollectionProperty strictly in invoke() (spec 6.2)
        self.models.clear()
        model_entries: list[ModelFileEntry] = src.cdae_models if cdae_mode else src.dae_models
        prefix_strip = f"vehicles/{next(iter(src.vehicles))}/" if len(src.vehicles) == 1 else ""
        for entry in sorted(model_entries, key=lambda e: (e.folder.lower(), e.path.lower())):
            it = self.models.add()
            it.path = entry.path
            disp = entry.path
            if prefix_strip and disp.lower().startswith(prefix_strip.lower()):
                disp = disp[len(prefix_strip) :]
            it.display_name = disp
            it.size_label = _format_size(entry.size_bytes)
            it.selected = len(model_entries) <= 8  # Don't pre-tick all 183 models in common.zip

        saved = self._load_saved_choices(context)

        veh_keys = sorted(src.vehicles.keys(), key=str.lower)
        if veh_keys:
            if saved.get("vehicle_name") in veh_keys:
                self.vehicle_name = saved["vehicle_name"]
            else:
                self.vehicle_name = veh_keys[0]

            default_ce = src.default_config_for_vehicle(self.vehicle_name)
            valid_cfgs = {_NO_CONFIG_ID} | {c.path for c in src.vehicles[self.vehicle_name].configs}
            if saved.get("config_path") in valid_cfgs:
                self.config_path = saved["config_path"]
            elif default_ce is not None:
                self.config_path = default_ce.path
            else:
                self.config_path = _NO_CONFIG_ID
        else:
            self.vehicle_name = "__NONE__"
            self.config_path = _NO_CONFIG_ID

        if saved.get("parts_mode") in ("CONFIG", "CONFIG_PLUS_HIDDEN", "ALL"):
            self.parts_mode = saved["parts_mode"]
        else:
            self.parts_mode = "CONFIG" if self.config_path != _NO_CONFIG_ID else "ALL"

        if "use_common" in saved:
            self.use_common = bool(saved["use_common"])
        if "spread_out" in saved:
            self.spread_out = bool(saved["spread_out"])

        _update_model_ticks_for_config(self)
        if self.config_path == _NO_CONFIG_ID and isinstance(saved.get("selected_models"), list):
            sel_set = set(saved["selected_models"])
            for it in self.models:
                it.selected = it.path in sel_set

        return context.window_manager.invoke_props_dialog(self, width=540)

    def draw(self, context):
        layout = self.layout
        bname = os.path.basename(self.filepath)
        layout.label(text=f"Import from {bname} [Experimental]", icon="FILE_ARCHIVE")

        if len(_ENUM_VEHICLES_CACHE) > 1:
            row = layout.row(align=True)
            row.prop(self, "vehicle_name")
            row.prop(self, "config_path")
        elif _ENUM_VEHICLES_CACHE and _ENUM_VEHICLES_CACHE[0][0] != "__NONE__":
            row = layout.row(align=True)
            row.label(text=f"Vehicle: {self.vehicle_name}")
            row.prop(self, "config_path")

        if self.config_path != _NO_CONFIG_ID:
            row = layout.row()
            row.prop(self, "parts_mode", expand=True)
            layout.label(
                text="Experimental: parts with jbeam placement are grouped under 'Needs placement'",
                icon="INFO",
            )

        row = layout.row(align=True)
        row.prop(self, "use_common")
        row.label(text=f"({self.common_status})")

        box = layout.box()
        hdr = box.row(align=True)
        hdr.label(text=f"Models in archive ({len(self.models)})", icon="MESH_DATA")
        hdr.prop(self, "model_filter", text="", icon="VIEWZOOM")
        op_all = hdr.operator("wm.collada_beamng_select_models", text="All")
        op_all.select_all = True
        op_none = hdr.operator("wm.collada_beamng_select_models", text="None")
        op_none.select_all = False

        box.template_list(
            "COLLADA_UL_beamng_models",
            "",
            self,
            "models",
            self,
            "active_model_index",
            rows=min(max(len(self.models), 3), 10),
        )

        layout.prop(self, "spread_out")

    def execute(self, context):
        selected_list = [it.path for it in self.models if it.selected]
        if len(self.models) > 0 and not selected_list:
            self.report({"ERROR"}, "No models selected to import")
            return {"CANCELLED"}
        self._save_choices(context)
        selected_paths = ";".join(selected_list)

        base_dae = ImportSettings(
            filepath=self.filepath,
            import_units=self.import_units,
            custom_normals=self.custom_normals,
            fast_exact_normals=self.fast_exact_normals,
            sharp_edges_from_normals=self.sharp_edges_from_normals,
            sharp_angle_threshold=self.sharp_angle_threshold,
        )
        base_cdae = CdaeImportSettings(
            filepath=self.filepath,
            lod_mode=self.cdae_lod_mode,
            import_collision=self.cdae_import_collision,
            normals_mode=self.cdae_normals_mode,
            merge_distance=self.cdae_merge_distance,
            sharp_angle_threshold=self.sharp_angle_threshold,
        )

        ok, summary, warnings, errors = execute_beamng_import(
            context,
            filepath=self.filepath,
            cdae_mode=bool(self.cdae_mode),
            beamng_config="" if self.config_path == _NO_CONFIG_ID else self.config_path,
            beamng_parts_mode=self.parts_mode,
            archive_models=selected_paths,
            use_common=bool(self.use_common),
            spread_out=bool(self.spread_out),
            base_dae_settings=base_dae,
            base_cdae_settings=base_cdae,
        )

        from ..collada_utils import report_capped

        report_capped(self, "WARNING", warnings)

        if ok:
            if summary:
                self.report({"INFO"}, summary)
            return {"FINISHED"}
        report_capped(self, "ERROR", errors, fallback="BeamNG archive import failed")
        return {"CANCELLED"}


PICKER_CLASSES = (
    BeamNGModelItem,
    COLLADA_UL_beamng_models,
    WM_OT_collada_beamng_select_models,
    WM_OT_collada_beamng_picker,
)


def register_picker() -> None:
    for cls in PICKER_CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.collada_beamng_last_choices = StringProperty(
        name="BeamNG Last Archive Choices",
        default="",
        options={"HIDDEN"},
    )


def unregister_picker() -> None:
    if hasattr(bpy.types.Scene, "collada_beamng_last_choices"):
        del bpy.types.Scene.collada_beamng_last_choices
    for cls in reversed(PICKER_CLASSES):
        bpy.utils.unregister_class(cls)
