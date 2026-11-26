# SPDX-FileCopyrightText: 2026 Blender5-Collada Authors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""BeamNG v1.5 PBR shared node group ('BeamNG v1.5 Material') and material builder."""

from __future__ import annotations

from typing import TYPE_CHECKING

import bpy
import numpy as np

from ..beamng_materials import BeamngMaterialSpec, BeamngStageSpec
from .resolve import PcPaint, srgb_to_linear

if TYPE_CHECKING:
    from bpy.types import Image, Material, NodeTree, Object

    from .materials_ctx import BeamngMaterialContext

NODE_GROUP_NAME = "BeamNG v1.5 Material"
DEFAULT_GLASS_OPACITY = 0.35


def _new_group_socket(
    ntree: NodeTree,
    name: str,
    in_out: str,
    socket_type: str,
    default=None,
    min_val: float | None = None,
    max_val: float | None = None,
):
    sock = ntree.interface.new_socket(name=name, in_out=in_out, socket_type=socket_type)
    if default is not None and hasattr(sock, "default_value"):
        sock.default_value = default
    if min_val is not None and hasattr(sock, "min_value"):
        sock.min_value = min_val
    if max_val is not None and hasattr(sock, "max_value"):
        sock.max_value = max_val
    return sock


def get_or_create_beamng_v15_node_group() -> NodeTree:
    """Return the shared 'BeamNG v1.5 Material' shader node group, creating it if needed."""
    existing = bpy.data.node_groups.get(NODE_GROUP_NAME)
    if existing is not None and existing.bl_idname == "ShaderNodeTree":
        return existing

    ng = bpy.data.node_groups.new(NODE_GROUP_NAME, "ShaderNodeTree")

    # Group interface sockets
    _new_group_socket(ng, "BSDF", "OUTPUT", "NodeSocketShader")

    _new_group_socket(ng, "Base Color", "INPUT", "NodeSocketColor", default=(1.0, 1.0, 1.0, 1.0))
    _new_group_socket(ng, "Base Color Factor", "INPUT", "NodeSocketColor", default=(1.0, 1.0, 1.0, 1.0))
    _new_group_socket(ng, "Palette Mask", "INPUT", "NodeSocketColor", default=(0.0, 0.0, 0.0, 1.0))
    _new_group_socket(ng, "Use Palette", "INPUT", "NodeSocketFloat", default=0.0, min_val=0.0, max_val=1.0)
    _new_group_socket(ng, "Instance Diffuse", "INPUT", "NodeSocketFloat", default=0.0, min_val=0.0, max_val=1.0)
    _new_group_socket(ng, "Ambient Occlusion", "INPUT", "NodeSocketFloat", default=1.0, min_val=0.0, max_val=1.0)
    _new_group_socket(ng, "Metallic", "INPUT", "NodeSocketFloat", default=0.0, min_val=0.0, max_val=1.0)
    _new_group_socket(ng, "Roughness", "INPUT", "NodeSocketFloat", default=0.5, min_val=0.0, max_val=1.0)
    _new_group_socket(ng, "Coat Weight", "INPUT", "NodeSocketFloat", default=0.0, min_val=0.0, max_val=1.0)
    _new_group_socket(ng, "Coat Roughness", "INPUT", "NodeSocketFloat", default=0.05, min_val=0.0, max_val=1.0)
    _new_group_socket(ng, "Normal", "INPUT", "NodeSocketVector", default=(0.0, 0.0, 0.0))
    _new_group_socket(ng, "Has Normal", "INPUT", "NodeSocketFloat", default=0.0, min_val=0.0, max_val=1.0)
    _new_group_socket(ng, "Alpha", "INPUT", "NodeSocketFloat", default=1.0, min_val=0.0, max_val=1.0)
    _new_group_socket(ng, "Emissive", "INPUT", "NodeSocketColor", default=(0.0, 0.0, 0.0, 1.0))

    nodes = ng.nodes
    links = ng.links

    gi = nodes.new("NodeGroupInput")
    gi.location = (-1200, 0)
    go = nodes.new("NodeGroupOutput")
    go.location = (800, 0)

    bsdf = nodes.new("ShaderNodeBsdfPrincipled")
    bsdf.location = (450, 0)
    links.new(bsdf.outputs["BSDF"], go.inputs["BSDF"])

    # Helper to create Object Attribute reader nodes
    def _obj_attr(attr_name: str, loc: tuple[float, float]):
        n = nodes.new("ShaderNodeAttribute")
        n.attribute_type = "OBJECT"
        n.attribute_name = attr_name
        n.location = loc
        return n

    p1_col = _obj_attr("beamng_paint1", (-900, 500))
    p2_col = _obj_attr("beamng_paint2", (-900, 340))
    p3_col = _obj_attr("beamng_paint3", (-900, 180))

    p1_met = _obj_attr("beamng_paint1_metallic", (-900, -100))
    p2_met = _obj_attr("beamng_paint2_metallic", (-900, -240))
    p3_met = _obj_attr("beamng_paint3_metallic", (-900, -380))

    p1_rgh = _obj_attr("beamng_paint1_roughness", (-900, -560))
    p2_rgh = _obj_attr("beamng_paint2_roughness", (-900, -700))
    p3_rgh = _obj_attr("beamng_paint3_roughness", (-900, -840))

    p1_coat = _obj_attr("beamng_paint1_clearcoat", (-900, -1020))
    p2_coat = _obj_attr("beamng_paint2_clearcoat", (-900, -1160))
    p3_coat = _obj_attr("beamng_paint3_clearcoat", (-900, -1300))

    p1_crgh = _obj_attr("beamng_paint1_clearcoat_roughness", (-900, -1480))
    p2_crgh = _obj_attr("beamng_paint2_clearcoat_roughness", (-900, -1620))
    p3_crgh = _obj_attr("beamng_paint3_clearcoat_roughness", (-900, -1760))

    # Separate palette mask R/G/B and gate by Use Palette (plus Instance Diffuse for paint 1)
    sep = nodes.new("ShaderNodeSeparateColor")
    sep.mode = "RGB"
    sep.location = (-900, 0)
    links.new(gi.outputs["Palette Mask"], sep.inputs["Color"])

    def _math(op: str, loc: tuple[float, float], clamp: bool = False):
        n = nodes.new("ShaderNodeMath")
        n.operation = op
        n.use_clamp = clamp
        n.location = loc
        return n

    w1_pal = _math("MULTIPLY", (-680, 60), clamp=True)
    links.new(sep.outputs["Red"], w1_pal.inputs[0])
    links.new(gi.outputs["Use Palette"], w1_pal.inputs[1])

    w1_final = _math("MAXIMUM", (-500, 60), clamp=True)
    links.new(w1_pal.outputs[0], w1_final.inputs[0])
    links.new(gi.outputs["Instance Diffuse"], w1_final.inputs[1])

    w2_final = _math("MULTIPLY", (-680, -80), clamp=True)
    links.new(sep.outputs["Green"], w2_final.inputs[0])
    links.new(gi.outputs["Use Palette"], w2_final.inputs[1])

    w3_final = _math("MULTIPLY", (-680, -220), clamp=True)
    links.new(sep.outputs["Blue"], w3_final.inputs[0])
    links.new(gi.outputs["Use Palette"], w3_final.inputs[1])

    # Base color * Base Color Factor
    def _mix_rgba(blend_type: str, loc: tuple[float, float], clamp_factor: bool = True):
        n = nodes.new("ShaderNodeMix")
        n.data_type = "RGBA"
        n.blend_type = blend_type
        n.clamp_factor = clamp_factor
        n.location = loc
        return n

    bc_mul = _mix_rgba("MULTIPLY", (-500, 420))
    bc_mul.inputs["Factor"].default_value = 1.0
    links.new(gi.outputs["Base Color"], bc_mul.inputs["A"])
    links.new(gi.outputs["Base Color Factor"], bc_mul.inputs["B"])

    # Multiply paint 1, paint 2, paint 3 by their palette weights
    c1 = _mix_rgba("MULTIPLY", (-300, 420))
    links.new(w1_final.outputs[0], c1.inputs["Factor"])
    links.new(bc_mul.outputs["Result"], c1.inputs["A"])
    links.new(p1_col.outputs["Color"], c1.inputs["B"])

    c2 = _mix_rgba("MULTIPLY", (-120, 420))
    links.new(w2_final.outputs[0], c2.inputs["Factor"])
    links.new(c1.outputs["Result"], c2.inputs["A"])
    links.new(p2_col.outputs["Color"], c2.inputs["B"])

    c3 = _mix_rgba("MULTIPLY", (60, 420))
    links.new(w3_final.outputs[0], c3.inputs["Factor"])
    links.new(c2.outputs["Result"], c3.inputs["A"])
    links.new(p3_col.outputs["Color"], c3.inputs["B"])

    # Multiply Ambient Occlusion into final base color
    ao_mul = _mix_rgba("MULTIPLY", (240, 420))
    ao_mul.inputs["Factor"].default_value = 1.0
    links.new(c3.outputs["Result"], ao_mul.inputs["A"])
    links.new(gi.outputs["Ambient Occlusion"], ao_mul.inputs["B"])
    links.new(ao_mul.outputs["Result"], bsdf.inputs["Base Color"])

    # Helper to blend a scalar channel with paint 1/2/3 values under w1/w2/w3
    def _blend_scalar_paints(base_sock, p1_node, p2_node, p3_node, y: float):
        m1 = nodes.new("ShaderNodeMix")
        m1.data_type = "FLOAT"
        m1.clamp_factor = True
        m1.location = (-300, y)
        links.new(w1_final.outputs[0], m1.inputs["Factor"])
        links.new(base_sock, m1.inputs["A"])
        links.new(p1_node.outputs["Fac"], m1.inputs["B"])

        m2 = nodes.new("ShaderNodeMix")
        m2.data_type = "FLOAT"
        m2.clamp_factor = True
        m2.location = (-120, y)
        links.new(w2_final.outputs[0], m2.inputs["Factor"])
        links.new(m1.outputs["Result"], m2.inputs["A"])
        links.new(p2_node.outputs["Fac"], m2.inputs["B"])

        m3 = nodes.new("ShaderNodeMix")
        m3.data_type = "FLOAT"
        m3.clamp_factor = True
        m3.location = (60, y)
        links.new(w3_final.outputs[0], m3.inputs["Factor"])
        links.new(m2.outputs["Result"], m3.inputs["A"])
        links.new(p3_node.outputs["Fac"], m3.inputs["B"])
        return m3.outputs["Result"]

    links.new(_blend_scalar_paints(gi.outputs["Metallic"], p1_met, p2_met, p3_met, -160), bsdf.inputs["Metallic"])
    links.new(_blend_scalar_paints(gi.outputs["Roughness"], p1_rgh, p2_rgh, p3_rgh, -580), bsdf.inputs["Roughness"])
    links.new(_blend_scalar_paints(gi.outputs["Coat Weight"], p1_coat, p2_coat, p3_coat, -1040), bsdf.inputs["Coat Weight"])
    links.new(
        _blend_scalar_paints(gi.outputs["Coat Roughness"], p1_crgh, p2_crgh, p3_crgh, -1500),
        bsdf.inputs["Coat Roughness"],
    )

    # Normal: mix between Geometry.Normal and Normal input using Has Normal
    geom = nodes.new("ShaderNodeNewGeometry")
    geom.location = (-120, -1860)
    nrm_mix = nodes.new("ShaderNodeMix")
    nrm_mix.data_type = "VECTOR"
    nrm_mix.clamp_factor = True
    nrm_mix.location = (160, -1860)
    links.new(gi.outputs["Has Normal"], nrm_mix.inputs["Factor"])
    links.new(geom.outputs["Normal"], nrm_mix.inputs["A"])
    links.new(gi.outputs["Normal"], nrm_mix.inputs["B"])
    links.new(nrm_mix.outputs["Result"], bsdf.inputs["Normal"])

    # Alpha and Emission
    links.new(gi.outputs["Alpha"], bsdf.inputs["Alpha"])
    links.new(gi.outputs["Emissive"], bsdf.inputs["Emission Color"])
    bsdf.inputs["Emission Strength"].default_value = 1.0

    return ng


def _image_has_non_opaque_alpha(ima: Image) -> bool:
    """Return True if an image has any alpha values noticeably below 1.0."""
    try:
        n_px = len(ima.pixels)
        if n_px < 4 or ima.channels < 4:
            return False
        buf = np.empty(n_px, dtype=np.float32)
        ima.pixels.foreach_get(buf)
        alphas = buf[3::4]
        return bool(np.any(alphas < 0.99))
    except Exception:
        return False


def apply_beamng_invis_material(ma: Material) -> None:
    """Make a glowMap 'invis' material (e.g. off dashboard warning lamp) fully transparent."""
    ma.use_nodes = True
    ntree = ma.node_tree
    assert ntree is not None
    ntree.nodes.clear()
    bsdf = ntree.nodes.new("ShaderNodeBsdfPrincipled")
    bsdf.location = (0, 0)
    bsdf.inputs["Alpha"].default_value = 0.0
    out_node = ntree.nodes.new("ShaderNodeOutputMaterial")
    out_node.location = (300, 0)
    ntree.links.new(bsdf.outputs["BSDF"], out_node.inputs["Surface"])
    if hasattr(ma, "surface_render_method"):
        ma.surface_render_method = "BLENDED"
    ma.use_backface_culling = False


def _is_glass_name(*names: str) -> bool:
    for nm in names:
        low = nm.lower()
        if "glass" in low or "windshield" in low:
            return True
    return False


def _build_stage_shader(
    ntree: NodeTree,
    stage: BeamngStageSpec,
    spec: BeamngMaterialSpec,
    ma: Material,
    mat_ctx: BeamngMaterialContext,
    *,
    stage_idx: int,
    dae_dir: str,
    uv_nodes: dict[int, object],
):
    """Build the minimal shader node(s) for ``stage``.

    - Uses the shared 'BeamNG v1.5 Material' group only when ``colorPaletteMap`` is present.
    - Uses a slim direct Principled BSDF with only Paint 1 Object Attributes for ``instanceDiffuse``.
    - Uses a minimal direct Principled BSDF (zero Attribute nodes, only active maps/factors)
      for plain PBR layers, avoiding EEVEE uber-shader compilation stalls.

    Returns (bsdf_output_socket, upper_mask_socket_or_float).
    """
    nodes = ntree.nodes
    links = ntree.links
    x_off = stage_idx * 950
    y_base = -stage_idx * 300

    def _get_uv_node(uv_idx: int):
        node = uv_nodes.get(uv_idx)
        if node is None:
            node = nodes.new("ShaderNodeUVMap")
            node.uv_map = "UV1" if uv_idx == 1 else "UV0"
            node.location = (-820, 200 if uv_idx == 0 else -200)
            uv_nodes[uv_idx] = node
        return node

    def _load_tex_node(ref: str, label: str, uv_idx: int, colorspace: str, y: float):
        if not ref:
            return None
        ima = mat_ctx.load_texture(ref, dae_dir=dae_dir, colorspace=colorspace)
        if ima is None:
            mat_ctx.record_missing_textures([ref])
            return None
        tn = nodes.new("ShaderNodeTexImage")
        tn.label = label if stage_idx == 0 else f"L{stage_idx} {label}"
        tn.image = ima
        tn.location = (-520 + x_off, y + y_base)
        links.new(_get_uv_node(uv_idx).outputs["UV"], tn.inputs["Vector"])
        return tn

    def _wire_scalar_map(tex_node, factor: float, target_sock, y: float) -> None:
        if tex_node is not None:
            if abs(factor - 1.0) > 1e-4:
                mul = nodes.new("ShaderNodeMath")
                mul.operation = "MULTIPLY"
                mul.use_clamp = True
                mul.location = (-200 + x_off, y + y_base)
                links.new(tex_node.outputs["Color"], mul.inputs[0])
                mul.inputs[1].default_value = factor
                links.new(mul.outputs[0], target_sock)
            else:
                links.new(tex_node.outputs["Color"], target_sock)
        else:
            target_sock.default_value = factor

    def _wire_emissive(em_tex, target_sock) -> bool:
        if em_tex is not None:
            if any(abs(c - 1.0) > 1e-4 for c in stage.emissive_factor):
                em_mul = nodes.new("ShaderNodeMix")
                em_mul.data_type = "RGBA"
                em_mul.blend_type = "MULTIPLY"
                em_mul.inputs["Factor"].default_value = 1.0
                em_mul.location = (-200 + x_off, -1780 + y_base)
                links.new(em_tex.outputs["Color"], em_mul.inputs["A"])
                em_mul.inputs["B"].default_value = (
                    stage.emissive_factor[0],
                    stage.emissive_factor[1],
                    stage.emissive_factor[2],
                    1.0,
                )
                links.new(em_mul.outputs["Result"], target_sock)
            else:
                links.new(em_tex.outputs["Color"], target_sock)
            return True
        if any(c > 1e-4 for c in stage.emissive_factor):
            target_sock.default_value = (
                stage.emissive_factor[0],
                stage.emissive_factor[1],
                stage.emissive_factor[2],
                1.0,
            )
            return True
        return False

    # Load textures needed for this stage
    bc_tex = _load_tex_node(stage.base_color_map, "Base Color", stage.base_color_uv, "sRGB", 460)
    pal_tex = _load_tex_node(stage.color_palette_map, "Color Palette", stage.color_palette_uv, "Non-Color", 180)
    ao_tex = _load_tex_node(stage.ambient_occlusion_map, "Ambient Occlusion", stage.ambient_occlusion_uv, "Non-Color", -100)
    met_tex = _load_tex_node(stage.metallic_map, "Metallic", stage.metallic_uv, "Non-Color", -380)
    rgh_tex = _load_tex_node(stage.roughness_map, "Roughness", stage.roughness_uv, "Non-Color", -660)
    coat_tex = _load_tex_node(stage.clearcoat_map, "Clearcoat", stage.clearcoat_uv, "Non-Color", -940)
    nrm_tex = _load_tex_node(stage.normal_map, "Normal", stage.normal_uv, "Non-Color", -1220)
    em_tex = _load_tex_node(stage.emissive_map, "Emissive", stage.emissive_uv, "sRGB", -1780)

    # Opacity: layer 0 only reads opacity when translucent or alphaTest; upper layers always read as layer mask
    need_opacity = (stage_idx > 0) or spec.translucent or spec.alpha_test
    op_tex = (
        _load_tex_node(stage.opacity_map, "Opacity", stage.opacity_uv, "Non-Color", -1500)
        if need_opacity
        else None
    )

    def _build_opacity_source():
        if op_tex is not None:
            if abs(stage.opacity_factor - 1.0) > 1e-4:
                mul = nodes.new("ShaderNodeMath")
                mul.operation = "MULTIPLY"
                mul.use_clamp = True
                mul.location = (-200 + x_off, -1500 + y_base)
                links.new(op_tex.outputs["Color"], mul.inputs[0])
                mul.inputs[1].default_value = stage.opacity_factor
                return mul.outputs[0], None
            return op_tex.outputs["Color"], None
        if bc_tex is not None and bc_tex.image is not None and _image_has_non_opaque_alpha(bc_tex.image):
            if abs(stage.opacity_factor - 1.0) > 1e-4:
                mul = nodes.new("ShaderNodeMath")
                mul.operation = "MULTIPLY"
                mul.use_clamp = True
                mul.location = (-200 + x_off, -1500 + y_base)
                links.new(bc_tex.outputs["Alpha"], mul.inputs[0])
                mul.inputs[1].default_value = stage.opacity_factor
                return mul.outputs[0], None
            return bc_tex.outputs["Alpha"], None
        if stage.has_opacity_factor and stage.opacity_factor < 0.999:
            return None, stage.opacity_factor
        if stage.base_color_factor[3] < 0.999:
            return None, stage.base_color_factor[3]
        return None, None

    def _wire_layer0_alpha(alpha_sock) -> None:
        if not (spec.translucent or spec.alpha_test):
            # Solid material: ignore layer 0 opacity completely, keep default opaque render method
            alpha_sock.default_value = 1.0
            if spec.double_sided:
                ma.use_backface_culling = False
            return

        op_sock, op_scalar = _build_opacity_source()
        has_layer0_opacity = (op_sock is not None) or (op_scalar is not None)

        if spec.alpha_test and has_layer0_opacity:
            if hasattr(ma, "surface_render_method"):
                ma.surface_render_method = "DITHERED"
            if spec.double_sided:
                ma.use_backface_culling = False
            if op_sock is not None:
                clip = nodes.new("ShaderNodeMath")
                clip.operation = "GREATER_THAN"
                clip.location = (-40 + x_off, -1500 + y_base)
                links.new(op_sock, clip.inputs[0])
                clip.inputs[1].default_value = spec.alpha_ref
                links.new(clip.outputs[0], alpha_sock)
            else:
                alpha_sock.default_value = 1.0 if (op_scalar or 0.0) > spec.alpha_ref else 0.0
        elif spec.translucent and has_layer0_opacity:
            if hasattr(ma, "surface_render_method"):
                ma.surface_render_method = "BLENDED"
            ma.use_backface_culling = False
            if op_sock is not None:
                links.new(op_sock, alpha_sock)
            else:
                alpha_sock.default_value = op_scalar
        elif spec.translucent and _is_glass_name(ma.name, spec.name):
            if hasattr(ma, "surface_render_method"):
                ma.surface_render_method = "BLENDED"
            ma.use_backface_culling = False
            alpha_sock.default_value = DEFAULT_GLASS_OPACITY
            mat_ctx.record_glass_fallback(ma.name)
        else:
            alpha_sock.default_value = 1.0
            if spec.double_sided:
                ma.use_backface_culling = False

    upper_mask = None
    if stage_idx > 0:
        op_sock, op_scalar = _build_opacity_source()
        if op_sock is not None:
            upper_mask = op_sock
        elif op_scalar is not None:
            upper_mask = op_scalar
        else:
            upper_mask = stage.opacity_factor

    # ---- Path A: Palette-masked layer -> shared 'BeamNG v1.5 Material' node group ----
    if pal_tex is not None:
        grp = nodes.new("ShaderNodeGroup")
        grp.node_tree = get_or_create_beamng_v15_node_group()
        grp.label = f"Layer {stage_idx}"
        grp.location = (100 + x_off, y_base)

        if bc_tex is not None:
            links.new(bc_tex.outputs["Color"], grp.inputs["Base Color"])
        grp.inputs["Base Color Factor"].default_value = (
            stage.base_color_factor[0],
            stage.base_color_factor[1],
            stage.base_color_factor[2],
            1.0,
        )
        links.new(pal_tex.outputs["Color"], grp.inputs["Palette Mask"])
        grp.inputs["Use Palette"].default_value = 1.0
        grp.inputs["Instance Diffuse"].default_value = 1.0 if stage.instance_diffuse else 0.0
        if ao_tex is not None:
            links.new(ao_tex.outputs["Color"], grp.inputs["Ambient Occlusion"])
        else:
            grp.inputs["Ambient Occlusion"].default_value = 1.0

        _wire_scalar_map(met_tex, stage.metallic_factor, grp.inputs["Metallic"], -380)
        _wire_scalar_map(rgh_tex, stage.roughness_factor, grp.inputs["Roughness"], -660)
        _wire_scalar_map(coat_tex, stage.clearcoat_factor, grp.inputs["Coat Weight"], -940)
        grp.inputs["Coat Roughness"].default_value = stage.clearcoat_roughness_factor

        if nrm_tex is not None:
            nmap = nodes.new("ShaderNodeNormalMap")
            nmap.space = "TANGENT"
            nmap.uv_map = "UV1" if stage.normal_uv == 1 else "UV0"
            nmap.location = (-200 + x_off, -1220 + y_base)
            links.new(nrm_tex.outputs["Color"], nmap.inputs["Color"])
            links.new(nmap.outputs["Normal"], grp.inputs["Normal"])
            grp.inputs["Has Normal"].default_value = 1.0
        else:
            grp.inputs["Has Normal"].default_value = 0.0

        if stage_idx == 0:
            _wire_layer0_alpha(grp.inputs["Alpha"])
        else:
            grp.inputs["Alpha"].default_value = 1.0

        _wire_emissive(em_tex, grp.inputs["Emissive"])
        return grp.outputs["BSDF"], upper_mask

    # ---- Path B & C: Slim direct Principled BSDF (instanceDiffuse or plain PBR) ----
    bsdf = nodes.new("ShaderNodeBsdfPrincipled")
    bsdf.label = f"Layer {stage_idx}"
    bsdf.location = (100 + x_off, y_base)

    # Base color chain: only create Mix nodes that are actually needed
    bc_fac = stage.base_color_factor[:3]
    has_nonunit_fac = any(abs(c - 1.0) > 1e-4 for c in bc_fac)
    cur_col_sock = None
    cur_col_const = (bc_fac[0], bc_fac[1], bc_fac[2], 1.0)

    if bc_tex is not None:
        if has_nonunit_fac:
            m = nodes.new("ShaderNodeMix")
            m.data_type = "RGBA"
            m.blend_type = "MULTIPLY"
            m.inputs["Factor"].default_value = 1.0
            m.location = (-240 + x_off, 460 + y_base)
            links.new(bc_tex.outputs["Color"], m.inputs["A"])
            m.inputs["B"].default_value = cur_col_const
            cur_col_sock = m.outputs["Result"]
        else:
            cur_col_sock = bc_tex.outputs["Color"]

    if stage.instance_diffuse:
        def _obj_attr(attr_name: str, y: float):
            a = nodes.new("ShaderNodeAttribute")
            a.attribute_type = "OBJECT"
            a.attribute_name = attr_name
            a.location = (-260 + x_off, y + y_base)
            return a

        p1_col = _obj_attr("beamng_paint1", 320)
        if cur_col_sock is not None:
            pm = nodes.new("ShaderNodeMix")
            pm.data_type = "RGBA"
            pm.blend_type = "MULTIPLY"
            pm.inputs["Factor"].default_value = 1.0
            pm.location = (-80 + x_off, 400 + y_base)
            links.new(cur_col_sock, pm.inputs["A"])
            links.new(p1_col.outputs["Color"], pm.inputs["B"])
            cur_col_sock = pm.outputs["Result"]
        elif has_nonunit_fac:
            pm = nodes.new("ShaderNodeMix")
            pm.data_type = "RGBA"
            pm.blend_type = "MULTIPLY"
            pm.inputs["Factor"].default_value = 1.0
            pm.location = (-80 + x_off, 400 + y_base)
            pm.inputs["A"].default_value = cur_col_const
            links.new(p1_col.outputs["Color"], pm.inputs["B"])
            cur_col_sock = pm.outputs["Result"]
        else:
            cur_col_sock = p1_col.outputs["Color"]

        links.new(_obj_attr("beamng_paint1_metallic", -380).outputs["Fac"], bsdf.inputs["Metallic"])
        links.new(_obj_attr("beamng_paint1_roughness", -540).outputs["Fac"], bsdf.inputs["Roughness"])
        links.new(_obj_attr("beamng_paint1_clearcoat", -700).outputs["Fac"], bsdf.inputs["Coat Weight"])
        links.new(_obj_attr("beamng_paint1_clearcoat_roughness", -860).outputs["Fac"], bsdf.inputs["Coat Roughness"])
    else:
        _wire_scalar_map(met_tex, stage.metallic_factor, bsdf.inputs["Metallic"], -380)
        _wire_scalar_map(rgh_tex, stage.roughness_factor, bsdf.inputs["Roughness"], -660)
        _wire_scalar_map(coat_tex, stage.clearcoat_factor, bsdf.inputs["Coat Weight"], -940)
        if coat_tex is not None or stage.clearcoat_factor > 1e-4:
            bsdf.inputs["Coat Roughness"].default_value = stage.clearcoat_roughness_factor

    if ao_tex is not None:
        ao_m = nodes.new("ShaderNodeMix")
        ao_m.data_type = "RGBA"
        ao_m.blend_type = "MULTIPLY"
        ao_m.inputs["Factor"].default_value = 1.0
        ao_m.location = (-40 + x_off, 260 + y_base)
        if cur_col_sock is not None:
            links.new(cur_col_sock, ao_m.inputs["A"])
        else:
            ao_m.inputs["A"].default_value = cur_col_const
        links.new(ao_tex.outputs["Color"], ao_m.inputs["B"])
        links.new(ao_m.outputs["Result"], bsdf.inputs["Base Color"])
    elif cur_col_sock is not None:
        links.new(cur_col_sock, bsdf.inputs["Base Color"])
    else:
        bsdf.inputs["Base Color"].default_value = cur_col_const

    if nrm_tex is not None:
        nmap = nodes.new("ShaderNodeNormalMap")
        nmap.space = "TANGENT"
        nmap.uv_map = "UV1" if stage.normal_uv == 1 else "UV0"
        nmap.location = (-200 + x_off, -1220 + y_base)
        links.new(nrm_tex.outputs["Color"], nmap.inputs["Color"])
        links.new(nmap.outputs["Normal"], bsdf.inputs["Normal"])

    if stage_idx == 0:
        _wire_layer0_alpha(bsdf.inputs["Alpha"])
    else:
        bsdf.inputs["Alpha"].default_value = 1.0

    if _wire_emissive(em_tex, bsdf.inputs["Emission Color"]):
        bsdf.inputs["Emission Strength"].default_value = 1.0
    else:
        bsdf.inputs["Emission Strength"].default_value = 0.0

    return bsdf.outputs["BSDF"], upper_mask


def apply_beamng_pbr_material(
    ma: Material,
    spec: BeamngMaterialSpec,
    mat_ctx: BeamngMaterialContext,
    *,
    dae_dir: str = "",
) -> None:
    """Build a multi-layer BeamNG v1.5 PBR node tree on ``ma`` from ``spec``."""
    if not spec.stages:
        return

    ma.use_nodes = True
    ntree = ma.node_tree
    assert ntree is not None
    ntree.nodes.clear()

    nodes = ntree.nodes
    links = ntree.links
    uv_nodes: dict[int, object] = {}

    cur_shader, _ = _build_stage_shader(
        ntree, spec.stages[0], spec, ma, mat_ctx, stage_idx=0, dae_dir=dae_dir, uv_nodes=uv_nodes
    )

    for idx in range(1, len(spec.stages)):
        layer_shader, mask = _build_stage_shader(
            ntree, spec.stages[idx], spec, ma, mat_ctx, stage_idx=idx, dae_dir=dae_dir, uv_nodes=uv_nodes
        )
        mix = nodes.new("ShaderNodeMixShader")
        mix.label = f"Composite Layer {idx}"
        mix.location = (400 + idx * 950, -idx * 150)
        if isinstance(mask, (int, float)):
            mix.inputs["Fac"].default_value = max(0.0, min(1.0, float(mask)))
        elif mask is not None:
            links.new(mask, mix.inputs["Fac"])
        else:
            mix.inputs["Fac"].default_value = 1.0
        links.new(cur_shader, mix.inputs[1])
        links.new(layer_shader, mix.inputs[2])
        cur_shader = mix.outputs["Shader"]

    out_node = nodes.new("ShaderNodeOutputMaterial")
    out_node.location = (450 + len(spec.stages) * 950, 0)
    links.new(cur_shader, out_node.inputs["Surface"])


def apply_paint_properties_to_object(ob: Object, paints: list[PcPaint]) -> None:
    """Write beamng_paint1..3 (linear RGBA) and scalar PBR properties onto ``ob``."""
    default_paint = PcPaint()
    for idx in range(3):
        p = paints[idx] if idx < len(paints) else default_paint
        n = idx + 1
        r_lin = srgb_to_linear(p.base_color_srgb[0])
        g_lin = srgb_to_linear(p.base_color_srgb[1])
        b_lin = srgb_to_linear(p.base_color_srgb[2])
        col_key = f"beamng_paint{n}"
        ob[col_key] = (r_lin, g_lin, b_lin, 1.0)
        try:
            ob.id_properties_ui(col_key).update(subtype="COLOR", min=0.0, max=1.0)
        except Exception:
            pass
        ob[f"beamng_paint{n}_metallic"] = float(p.metallic)
        ob[f"beamng_paint{n}_roughness"] = float(p.roughness)
        ob[f"beamng_paint{n}_clearcoat"] = float(p.clearcoat)
        ob[f"beamng_paint{n}_clearcoat_roughness"] = float(p.clearcoat_roughness)
