# SPDX-FileCopyrightText: 2009-2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Images, effects, materials, BeamNG material texture fallback, and material bindings."""

from __future__ import annotations

import os
import xml.etree.ElementTree as ET
from typing import TYPE_CHECKING

import bpy
import numpy as np

from ..beamng_materials import alt_image_refs, lookup_material_color_maps
from .xml_parse import (
    _find_child,
    _find_children,
    _parse_floats,
    _safe_float,
    _uri_to_local_id,
    _uri_to_native_path,
)

if TYPE_CHECKING:
    from bpy.types import Material, Mesh, Object


class MaterialsMixin:
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

    def _read_library_images(self, lib: ET.Element) -> None:
        for img_elem in _find_children(lib, "image"):
            img_id = img_elem.attrib.get("id", "")
            if self._allowed_img_ids is not None and img_id not in self._allowed_img_ids:
                continue
            init_from = _find_child(img_elem, "init_from")
            if init_from is None or not init_from.text:
                continue
            raw_ref = init_from.text.strip()
            if self._beamng_mat_ctx is not None:
                ima = self._beamng_mat_ctx.load_texture(raw_ref, dae_dir=self._archive_dae_dir)
                if ima is not None:
                    self.images_by_id[img_id] = ima
                    continue
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
            if self._beamng_mat_ctx is not None:
                reused = self._beamng_mat_ctx.get_reusable_material(mat_name)
                if reused is not None:
                    self.materials_by_id[mat_id] = reused
                    continue
            ma = bpy.data.materials.new(name=mat_name)
            if self._beamng_mat_ctx is not None:
                self._beamng_mat_ctx.register_material(mat_name, ma)
            self.materials_by_id[mat_id] = ma

            inst_eff = _find_child(mat_elem, "instance_effect")
            if inst_eff is not None:
                eff_id = _uri_to_local_id(inst_eff.attrib.get("url"))
                eff_elem = self.effects_by_id.get(eff_id)
                if eff_elem is not None:
                    self._apply_effect_to_material(eff_elem, ma)

            if self._beamng_mat_ctx is not None and self._beamng_mat_ctx.pbr_mode:
                if self._beamng_mat_ctx.apply_material(ma, mat_name, mat_id, dae_dir=self._archive_dae_dir):
                    continue
            if self._beamng_mat_maps:
                self._try_apply_beamng_material_texture(ma, mat_name, mat_id)

    def _try_apply_beamng_material_texture(self, ma: Material, mat_name: str, mat_id: str) -> None:
        """If a material has no Base Color texture yet, look up colorMap/baseColorMap from main.materials.json."""
        if self._beamng_mat_ctx is not None:
            glow_target = self._beamng_mat_ctx.resolve_glow_target(mat_name, mat_id)
            if glow_target is not None:
                ma["beamng_glow_source"] = glow_target
                refs = lookup_material_color_maps(self._beamng_mat_maps, glow_target, mat_name, mat_id)
            else:
                refs = lookup_material_color_maps(self._beamng_mat_maps, mat_name, mat_id)
        else:
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
        if self._beamng_mat_ctx is not None:
            for ref in refs:
                ima = self._beamng_mat_ctx.load_texture(ref, dae_dir=self._archive_dae_dir)
                if ima is not None:
                    break
            if ima is None:
                self._beamng_mat_ctx.record_missing_textures(refs)
                return
        else:
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

    def _apply_material_bindings(self, inst_elem: ET.Element, ob: Object, geom_id: str) -> None:
        bm = _find_child(inst_elem, "bind_material")
        if bm is None:
            return
        tc = _find_child(bm, "technique_common")
        if tc is None:
            return
        mat_prims = self.mesh_mat_prims.get(geom_id, {})
        mesh: Mesh = ob.data

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
