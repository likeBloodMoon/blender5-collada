# SPDX-FileCopyrightText: 2009-2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Complete Collada (.dae) importer faithfully ported from Blender 4.5.1 for Blender 5.0+."""

from __future__ import annotations

import math
import os
import tempfile
import urllib.parse
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import bpy
from mathutils import Euler, Matrix, Quaternion, Vector

from .collada_utils import (
    create_fcurve_on_channelbag,
    dae_flat_to_matrix,
    ensure_action_and_channelbag,
    fov_to_focallength,
    set_editbone_property_matrix,
)

if TYPE_CHECKING:
    from bpy.types import Context, Material, Mesh, Object, Scene


MINIMUM_BONE_LENGTH = 0.000001


@dataclass
class ImportSettings:
    filepath: str = ""
    import_units: bool = False
    custom_normals: bool = True
    find_chains: bool = False
    auto_connect: bool = False
    fix_orientation: bool = False
    min_chain_length: int = 0
    keep_bind_info: bool = False


def _strip_ns(tag: str) -> str:
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag


def _find_child(elem: ET.Element, name: str) -> ET.Element | None:
    for ch in elem:
        if _strip_ns(ch.tag) == name:
            return ch
    return None


def _find_children(elem: ET.Element, name: str) -> list[ET.Element]:
    return [ch for ch in elem if _strip_ns(ch.tag) == name]


def _parse_floats(text: str | None) -> list[float]:
    if not text:
        return []
    return [float(x) for x in text.split()]


def _parse_ints(text: str | None) -> list[int]:
    if not text:
        return []
    return [int(x) for x in text.split()]


def _parse_strings(text: str | None) -> list[str]:
    if not text:
        return []
    return text.split()


def _parse_extra_blender(elem: ET.Element) -> dict[str, str]:
    """Extract all <extra><technique profile="blender"> child elements as tag->text."""
    result: dict[str, str] = {}
    for extra in _find_children(elem, "extra"):
        for tech in _find_children(extra, "technique"):
            if tech.attrib.get("profile") == "blender":
                for ch in tech:
                    result[_strip_ns(ch.tag)] = ch.text or ""
    return result


def _uri_to_local_id(uri: str | None) -> str:
    if not uri:
        return ""
    if uri.startswith("#"):
        return uri[1:]
    return uri


def _uri_to_native_path(uri: str) -> str:
    if not uri:
        return ""
    if uri.startswith("file:///"):
        p = urllib.parse.unquote(uri[8:])
        if len(p) >= 2 and p[1] == ":":
            return p.replace("/", os.sep)
        return "/" + p
    if uri.startswith("file://"):
        return urllib.parse.unquote(uri[7:])
    return urllib.parse.unquote(uri)


@dataclass
class _SkinData:
    controller_id: str
    geometry_id: str
    bind_shape_matrix: Matrix
    joint_names: list[str]
    inv_bind_matrices: list[Matrix]
    vcounts: list[int]
    v_pairs: list[tuple[int, float]]  # flattened (joint_idx, weight)
    ob_arm: Object | None = None


@dataclass
class _MorphData:
    controller_id: str
    geometry_id: str
    target_geom_ids: list[str]
    weights: list[float]


@dataclass
class _AnimChannelData:
    target: str
    times: list[float]
    values: list[float]
    stride: int
    interps: list[str]
    in_tangents: list[float]
    out_tangents: list[float]


class DocumentImporter:
    """Complete Collada (.dae) DocumentImporter ported from Blender 4.5.1."""

    def __init__(self, context: Context, settings: ImportSettings) -> None:
        self.context = context
        self.scene: Scene = context.scene
        self.view_layer = context.view_layer
        self.settings = settings
        self.dae_dir = os.path.dirname(os.path.abspath(settings.filepath))

        # Asset info
        self.up_axis: str = "Z_UP"
        self.unit_name: str = "meter"
        self.unit_meter: float = 1.0
        self.import_from_version: str = ""

        # Parsed library maps
        self.images_by_id: dict[str, bpy.types.Image] = {}
        self.effects_by_id: dict[str, ET.Element] = {}
        self.materials_by_id: dict[str, Material] = {}
        self.cameras_by_id: dict[str, bpy.types.Camera] = {}
        self.lights_by_id: dict[str, bpy.types.Light] = {}
        self.meshes_by_id: dict[str, Mesh] = {}
        self.mesh_mat_prims: dict[str, dict[str, list[tuple[int, int]]]] = {}
        self.skins_by_id: dict[str, _SkinData] = {}
        self.morphs_by_id: dict[str, _MorphData] = {}
        self.lib_nodes_by_id: dict[str, ET.Element] = {}
        self.animations: list[_AnimChannelData] = []

        # Scene instantiation state
        self.node_elem_by_id: dict[str, ET.Element] = {}
        self.object_by_node_id: dict[str, Object] = {}
        self.node_elem_by_id: dict[str, ET.Element] = {}
        self.object_by_geom_id: dict[str, Object] = {}
        self.imported_mesh_objects: list[Object] = []
        self.root_objects: list[Object] = []
        self._loaded_images_from_archive: list = []

        # Armature tracking
        self.root_joint_nodes: list[tuple[ET.Element, Object]] = []  # (joint_elem, ob_arm)
        self.joint_elem_by_sid: dict[str, ET.Element] = {}
        self.joint_elem_by_id: dict[str, ET.Element] = {}
        self.joint_arm_by_id: dict[str, Object] = {}
        self.joint_bone_name_by_id: dict[str, str] = {}
        self.joint_parent_elem_by_id: dict[str, ET.Element | None] = {}

    def _extract_archive_dae(self, archive_path: str, tmpdir: str) -> str | None:
        """Safely extract a .zae / .kmz / .zip archive and return the primary .dae path."""
        base_real = os.path.realpath(tmpdir)
        extracted_daes: list[str] = []
        manifest_path: str | None = None

        with zipfile.ZipFile(archive_path, "r") as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                target = os.path.realpath(os.path.join(tmpdir, info.filename))
                if not (target == base_real or target.startswith(base_real + os.sep)):
                    continue
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with zf.open(info, "r") as src, open(target, "wb") as dst:
                    dst.write(src.read())
                low = info.filename.lower()
                if low == "manifest.xml" or low.endswith("/manifest.xml"):
                    manifest_path = target
                elif low.endswith(".dae"):
                    extracted_daes.append(target)

        if manifest_path is not None:
            try:
                mroot = ET.parse(manifest_path).getroot()
                dae_root_el = _find_child(mroot, "dae_root")
                if dae_root_el is not None and dae_root_el.text:
                    rel = _uri_to_native_path(dae_root_el.text.strip())
                    cand = os.path.realpath(os.path.join(os.path.dirname(manifest_path), rel))
                    if cand.startswith(base_real + os.sep) and os.path.isfile(cand):
                        return cand
            except Exception:
                pass

        return extracted_daes[0] if extracted_daes else None

    def import_document(self) -> bool:
        ext = os.path.splitext(self.settings.filepath)[1].lower()
        if ext in (".zae", ".kmz", ".zip"):
            with tempfile.TemporaryDirectory(prefix="blender_collada_") as tmpdir:
                dae_path = self._extract_archive_dae(self.settings.filepath, tmpdir)
                if dae_path is None:
                    print(f"Collada import: No .dae file found inside archive {self.settings.filepath}")
                    return False
                self.dae_dir = os.path.dirname(dae_path)
                ok = self._import_dae_file(dae_path, pack_images=True)
                return ok
        return self._import_dae_file(self.settings.filepath, pack_images=False)

    def _import_dae_file(self, dae_path: str, pack_images: bool = False) -> bool:
        try:
            tree = ET.parse(dae_path)
        except Exception as e:
            print(f"Collada import: Failed to parse XML: {e}")
            return False

        root = tree.getroot()
        if _strip_ns(root.tag) != "COLLADA":
            print("Collada import: Root element is not <COLLADA>")
            return False

        # Deselect all existing objects
        for ob in self.view_layer.objects:
            ob.select_set(False)

        # 1. Asset
        asset_elem = _find_child(root, "asset")
        if asset_elem is not None:
            self._read_asset(asset_elem)

        # 2. Libraries
        for lib in _find_children(root, "library_images"):
            self._read_library_images(lib)
        for lib in _find_children(root, "library_effects"):
            self._read_library_effects(lib)
        for lib in _find_children(root, "library_materials"):
            self._read_library_materials(lib)
        for lib in _find_children(root, "library_cameras"):
            self._read_library_cameras(lib)
        for lib in _find_children(root, "library_lights"):
            self._read_library_lights(lib)
        for lib in _find_children(root, "library_geometries"):
            self._read_library_geometries(lib)
        for lib in _find_children(root, "library_controllers"):
            self._read_library_controllers(lib)
        for lib in _find_children(root, "library_nodes"):
            for n in _find_children(lib, "node"):
                nid = n.attrib.get("id", "")
                if nid:
                    self.lib_nodes_by_id[nid] = n
        for lib in _find_children(root, "library_animations"):
            self._read_library_animations(lib)

        # 3. Apply units if requested
        if self.settings.import_units:
            us = self.scene.unit_settings
            if self.unit_name in ("millimeter", "centimeter", "decimeter", "meter", "kilometer"):
                us.system = "METRIC"
            elif self.unit_name in ("inch", "foot", "yard"):
                us.system = "IMPERIAL"
            else:
                us.system = "NONE"
            us.scale_length = self.unit_meter

        # 4. Visual Scenes
        for lib in _find_children(root, "library_visual_scenes"):
            for vscene in _find_children(lib, "visual_scene"):
                for node in _find_children(vscene, "node"):
                    self._process_node(node, None, None)

        # 5. Optimize material assignments (move identical object mats to mesh data)
        self._optimize_material_assignments()

        # 6. Build Armatures, link skins, build shape keys
        self._make_armatures()
        self._make_shape_keys()

        # 7. Import Animations
        self._apply_animations()

        # 8. Match scale & up-axis rotation on root objects
        self._apply_unit_and_up_axis()

        if pack_images:
            for ima in self._loaded_images_from_archive:
                try:
                    ima.pack()
                except Exception:
                    pass

        self.view_layer.update()
        return True

    # ------------------------------------------------------------------
    # <asset>
    # ------------------------------------------------------------------

    def _read_asset(self, asset: ET.Element) -> None:
        unit = _find_child(asset, "unit")
        if unit is not None:
            self.unit_name = unit.attrib.get("name", "meter").lower()
            try:
                self.unit_meter = float(unit.attrib.get("meter", "1.0"))
            except ValueError:
                self.unit_meter = 1.0
        up = _find_child(asset, "up_axis")
        if up is not None and up.text:
            self.up_axis = up.text.strip().upper()
        contrib = _find_child(asset, "contributor")
        if contrib is not None:
            tool = _find_child(contrib, "authoring_tool")
            if tool is not None and tool.text and tool.text.startswith("Blender "):
                parts = tool.text[len("Blender "):].split(" ", 1)
                if parts:
                    self.import_from_version = parts[0]

    def _get_up_axis_matrix(self) -> Matrix:
        if self.up_axis == "X_UP":
            return Matrix.Rotation(-math.pi / 2.0, 4, "Y")
        if self.up_axis == "Y_UP":
            return Matrix.Rotation(math.pi / 2.0, 4, "X")
        return Matrix.Identity(4)

    def _get_unit_scale_matrix(self) -> Matrix:
        us = self.scene.unit_settings
        bl_scale = 1.0 if us.system == "NONE" else us.scale_length
        factor = self.unit_meter / bl_scale if bl_scale else 1.0
        return Matrix.Diagonal((factor, factor, factor, 1.0))

    def _apply_unit_and_up_axis(self) -> None:
        rot_m = self._get_up_axis_matrix()
        scale_m = self._get_unit_scale_matrix() if not self.settings.import_units else Matrix.Identity(4)
        if rot_m == Matrix.Identity(4) and scale_m == Matrix.Identity(4):
            return
        for ob in self.root_objects:
            if ob.parent is None:
                mat = rot_m @ scale_m @ ob.matrix_basis
                ob.matrix_basis = mat

    # ------------------------------------------------------------------
    # <library_images>
    # ------------------------------------------------------------------

    def _resolve_image_path(self, raw_uri: str) -> str | None:
        native = _uri_to_native_path(raw_uri)
        # Normalize embedded backslashes so Windows-authored DAEs resolve on Linux/macOS
        norm = native.replace("\\", os.sep)
        bname = os.path.basename(norm)
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
        for cand in candidates:
            if cand and os.path.isfile(cand):
                return cand
        return None

    def _read_library_images(self, lib: ET.Element) -> None:
        for img_elem in _find_children(lib, "image"):
            img_id = img_elem.attrib.get("id", "")
            init_from = _find_child(img_elem, "init_from")
            if init_from is None or not init_from.text:
                continue
            workpath = self._resolve_image_path(init_from.text.strip())
            if workpath is None:
                continue
            try:
                ima = bpy.data.images.load(workpath, check_existing=True)
                self.images_by_id[img_id] = ima
                self._loaded_images_from_archive.append(ima)
            except Exception as e:
                print(f"Collada import: Cannot load image {workpath}: {e}")

    # ------------------------------------------------------------------
    # <library_effects> & <library_materials>
    # ------------------------------------------------------------------

    def _read_library_effects(self, lib: ET.Element) -> None:
        for eff in _find_children(lib, "effect"):
            eid = eff.attrib.get("id", "")
            if eid:
                self.effects_by_id[eid] = eff

    def _read_library_materials(self, lib: ET.Element) -> None:
        for mat_elem in _find_children(lib, "material"):
            mat_id = mat_elem.attrib.get("id", "")
            mat_name = mat_elem.attrib.get("name") or mat_id
            ma = bpy.data.materials.new(name=mat_name)
            self.materials_by_id[mat_id] = ma

            inst_eff = _find_child(mat_elem, "instance_effect")
            if inst_eff is not None:
                eff_id = _uri_to_local_id(inst_eff.attrib.get("url"))
                eff_elem = self.effects_by_id.get(eff_id)
                if eff_elem is not None:
                    self._apply_effect_to_material(eff_elem, ma)

    def _apply_effect_to_material(self, eff_elem: ET.Element, ma: Material) -> None:
        profile = _find_child(eff_elem, "profile_COMMON")
        if profile is None:
            return

        # Parse <newparam> surface & sampler2D mappings
        surface_to_img: dict[str, str] = {}
        sampler_to_surface: dict[str, str] = {}
        for np in _find_children(profile, "newparam"):
            sid = np.attrib.get("sid", "")
            surf = _find_child(np, "surface")
            if surf is not None:
                init_from = _find_child(surf, "init_from")
                if init_from is not None and init_from.text:
                    surface_to_img[sid] = init_from.text.strip()
            samp = _find_child(np, "sampler2D")
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
            es_sock = shader_node.inputs.get("Emission Strength")
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
                ior_val = float(fl.text.strip())
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
                        val = float(fl.text.strip())
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
                rval = float(fl.text.strip())
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

    # ------------------------------------------------------------------
    # <library_cameras>
    # ------------------------------------------------------------------

    def _read_library_cameras(self, lib: ET.Element) -> None:
        for cam_elem in _find_children(lib, "camera"):
            cam_id = cam_elem.attrib.get("id", "")
            cam_name = cam_elem.attrib.get("name") or cam_id
            cam = bpy.data.cameras.new(name=cam_name)

            extras = _parse_extra_blender(cam_elem)
            if "shiftx" in extras:
                cam.shift_x = float(extras["shiftx"])
            if "shifty" in extras:
                cam.shift_y = float(extras["shifty"])
            if "dof_distance" in extras:
                cam.dof.focus_distance = float(extras["dof_distance"])

            optics = _find_child(cam_elem, "optics")
            if optics is not None:
                tc = _find_child(optics, "technique_common")
                if tc is not None:
                    persp = _find_child(tc, "perspective")
                    ortho = _find_child(tc, "orthographic")
                    if persp is not None:
                        cam.type = "PERSP"
                        zn = _find_child(persp, "znear")
                        zf = _find_child(persp, "zfar")
                        if zn is not None and zn.text:
                            cam.clip_start = float(zn.text)
                        if zf is not None and zf.text:
                            cam.clip_end = float(zf.text)
                        xfov_el = _find_child(persp, "xfov")
                        yfov_el = _find_child(persp, "yfov")
                        ar_el = _find_child(persp, "aspect_ratio")
                        if xfov_el is not None and xfov_el.text:
                            cam.lens = fov_to_focallength(math.radians(float(xfov_el.text)), cam.sensor_width)
                        elif yfov_el is not None and yfov_el.text:
                            yfov = math.radians(float(yfov_el.text))
                            if ar_el is not None and ar_el.text:
                                aspect = float(ar_el.text)
                                xfov = 2.0 * math.atan(aspect * math.tan(yfov * 0.5))
                                cam.lens = fov_to_focallength(xfov, cam.sensor_width)
                            else:
                                cam.lens = fov_to_focallength(yfov, cam.sensor_width)
                    elif ortho is not None:
                        cam.type = "ORTHO"
                        zn = _find_child(ortho, "znear")
                        zf = _find_child(ortho, "zfar")
                        if zn is not None and zn.text:
                            cam.clip_start = float(zn.text)
                        if zf is not None and zf.text:
                            cam.clip_end = float(zf.text)
                        xmag_el = _find_child(ortho, "xmag")
                        ymag_el = _find_child(ortho, "ymag")
                        ar_el = _find_child(ortho, "aspect_ratio")
                        if xmag_el is not None and xmag_el.text:
                            cam.ortho_scale = float(xmag_el.text) * 2.0
                        elif ymag_el is not None and ymag_el.text:
                            ymag = float(ymag_el.text)
                            if ar_el is not None and ar_el.text:
                                cam.ortho_scale = 2.0 * ymag * float(ar_el.text)
                            else:
                                cam.ortho_scale = ymag

            self.cameras_by_id[cam_id] = cam

    # ------------------------------------------------------------------
    # <library_lights>
    # ------------------------------------------------------------------

    def _read_library_lights(self, lib: ET.Element) -> None:
        int_to_type = {0: "POINT", 1: "SUN", 2: "SPOT", 4: "AREA"}
        int_to_shape = {0: "SQUARE", 1: "RECTANGLE", 4: "DISK", 5: "ELLIPSE"}

        for la_elem in _find_children(lib, "light"):
            la_id = la_elem.attrib.get("id", "")
            la_name = la_elem.attrib.get("name") or la_id
            extras = _parse_extra_blender(la_elem)

            if extras and "type" in extras:
                la_type = int_to_type.get(int(extras["type"]), "POINT")
                la = bpy.data.lights.new(name=la_name, type=la_type)
                la_key = la.name
                la.color = (
                    float(extras.get("red", 1.0)),
                    float(extras.get("green", 1.0)),
                    float(extras.get("blue", 1.0)),
                )
                if "energy" in extras:
                    la.energy = float(extras["energy"])
                if "mode" in extras:
                    mode = int(extras["mode"])
                    la.use_shadow = bool(mode & 1)
                    if hasattr(la, "normalize"):
                        la.normalize = not bool(mode & (1 << 22))
                if "radius" in extras and hasattr(la, "shadow_soft_size"):
                    la.shadow_soft_size = float(extras["radius"])
                if "clipend" in extras and hasattr(la, "cutoff_distance"):
                    la.cutoff_distance = float(extras["clipend"])
                # Match DocumentImporter.cpp:999-1008: write spot & area DNA fields
                # regardless of final light type so round-trips preserve DNA values.
                if "spotsize" in extras or "spotblend" in extras:
                    if la.type != "SPOT":
                        la.type = "SPOT"
                        la = bpy.data.lights[la_key]
                    if "spotsize" in extras:
                        la.spot_size = math.radians(float(extras["spotsize"]))
                    if "spotblend" in extras:
                        la.spot_blend = float(extras["spotblend"])
                if "area_shape" in extras or "area_size" in extras or "area_sizey" in extras:
                    if la.type != "AREA":
                        la.type = "AREA"
                        la = bpy.data.lights[la_key]
                    if "area_shape" in extras:
                        la.shape = int_to_shape.get(int(extras["area_shape"]), "RECTANGLE")
                    if "area_size" in extras:
                        la.size = float(extras["area_size"])
                    if "area_sizey" in extras and la.shape in ("RECTANGLE", "ELLIPSE"):
                        la.size_y = float(extras["area_sizey"])
                if la.type != la_type:
                    la.type = la_type
                    la = bpy.data.lights[la_key]
            else:
                tc = _find_child(la_elem, "technique_common")
                la_type = "POINT"
                sub = None
                if tc is not None:
                    for tag, btype in (
                        ("directional", "SUN"),
                        ("spot", "SPOT"),
                        ("point", "POINT"),
                        ("ambient", "SUN"),
                    ):
                        sub = _find_child(tc, tag)
                        if sub is not None:
                            la_type = btype
                            break
                la = bpy.data.lights.new(name=la_name, type=la_type)
                la.energy = 1.0
                if sub is not None:
                    col_el = _find_child(sub, "color")
                    if col_el is not None:
                        c = _parse_floats(col_el.text)
                        if len(c) >= 3:
                            la.color = (c[0], c[1], c[2])
                    if la_type == "SPOT":
                        fa = _find_child(sub, "falloff_angle")
                        fe = _find_child(sub, "falloff_exponent")
                        if fa is not None and fa.text:
                            la.spot_size = math.radians(float(fa.text))
                        if fe is not None and fe.text:
                            la.spot_blend = float(fe.text)

            self.lights_by_id[la_id] = la

    # ------------------------------------------------------------------
    # <library_geometries>
    # ------------------------------------------------------------------

    def _read_library_geometries(self, lib: ET.Element) -> None:
        for geom_elem in _find_children(lib, "geometry"):
            geom_id = geom_elem.attrib.get("id", "")
            geom_name = geom_elem.attrib.get("name") or geom_id
            mesh_elem = _find_child(geom_elem, "mesh")
            if mesh_elem is None:
                continue
            self._read_mesh(geom_id, geom_name, mesh_elem)

    def _read_mesh(self, geom_id: str, geom_name: str, mesh_elem: ET.Element) -> None:
        # Parse all <source> elements
        sources: dict[str, tuple[list[float], int, str]] = {}
        for src in _find_children(mesh_elem, "source"):
            sid = src.attrib.get("id", "")
            sname = src.attrib.get("name") or sid
            fa = _find_child(src, "float_array")
            vals = _parse_floats(fa.text) if fa is not None else []
            stride = 3
            tc = _find_child(src, "technique_common")
            if tc is not None:
                acc = _find_child(tc, "accessor")
                if acc is not None and "stride" in acc.attrib:
                    stride = int(acc.attrib["stride"])
            sources[sid] = (vals, stride, sname)

        # Parse <vertices>
        verts_elem = _find_child(mesh_elem, "vertices")
        verts_id = ""
        pos_source_id = ""
        vert_normal_src_id = ""
        if verts_elem is not None:
            verts_id = verts_elem.attrib.get("id", "")
            for inp in _find_children(verts_elem, "input"):
                sem = inp.attrib.get("semantic", "")
                if sem == "POSITION":
                    pos_source_id = _uri_to_local_id(inp.attrib.get("source"))
                elif sem == "NORMAL":
                    vert_normal_src_id = _uri_to_local_id(inp.attrib.get("source"))

        pos_vals, pos_stride, _ = sources.get(pos_source_id, ([], 3, ""))
        if pos_stride <= 0:
            pos_stride = 3
        num_verts = len(pos_vals) // pos_stride

        # Collect primitives
        face_vert_indices: list[list[int]] = []
        face_sharp: list[bool] = []
        loop_normals: list[tuple[float, float, float]] = []
        has_all_loop_normals = True
        uv_sets: dict[str, list[tuple[float, float]]] = {}
        uv_set_order: list[str] = []
        col_sets: dict[str, list[tuple[float, float, float, float]]] = {}
        col_set_order: list[str] = []
        loose_edges: list[tuple[int, int]] = []
        mat_prims: dict[str, list[tuple[int, int]]] = {}  # material_symbol -> [(start_face, count)]

        def extract_vcolname(src_id: str, src_name: str) -> str:
            if src_name and src_name != src_id:
                return src_name
            idx = src_id.find("-mesh-colors-")
            if idx != -1:
                return src_id[idx + 13:]
            return src_id

        for child in mesh_elem:
            tag = _strip_ns(child.tag)
            if tag not in ("triangles", "polylist", "polygons", "trifans", "tristrips", "lines"):
                continue

            inputs: list[tuple[str, str, int, int]] = []
            max_offset = 0
            for inp in _find_children(child, "input"):
                sem = inp.attrib.get("semantic", "")
                src_id = _uri_to_local_id(inp.attrib.get("source"))
                off = int(inp.attrib.get("offset", "0"))
                iset = int(inp.attrib.get("set", "0"))
                inputs.append((sem, src_id, off, iset))
                if off > max_offset:
                    max_offset = off
            num_inputs = max_offset + 1

            vert_off = -1
            norm_off = -1
            norm_src_id = vert_normal_src_id
            uv_inputs: list[tuple[str, int]] = []
            col_inputs: list[tuple[str, str, int]] = []

            for sem, src_id, off, iset in inputs:
                if sem == "VERTEX":
                    vert_off = off
                elif sem == "NORMAL":
                    norm_off = off
                    norm_src_id = src_id
                elif sem == "TEXCOORD":
                    if src_id not in uv_sets:
                        uv_sets[src_id] = []
                        uv_set_order.append(src_id)
                    uv_inputs.append((src_id, off))
                elif sem == "COLOR":
                    sname = sources[src_id][2] if src_id in sources else src_id
                    cname = extract_vcolname(src_id, sname)
                    if cname not in col_sets:
                        col_sets[cname] = []
                        col_set_order.append(cname)
                    col_inputs.append((cname, src_id, off))

            if vert_off < 0:
                continue

            if tag == "lines":
                p_el = _find_child(child, "p")
                if p_el is not None:
                    p_ints = _parse_ints(p_el.text)
                    cnt = len(p_ints) // (2 * num_inputs)
                    for i in range(cnt):
                        v0 = p_ints[(2 * i) * num_inputs + vert_off]
                        v1 = p_ints[(2 * i + 1) * num_inputs + vert_off]
                        loose_edges.append((v0, v1))
                continue

            # Face primitives
            mat_sym = child.attrib.get("material", "")
            prim_start_face = len(face_vert_indices)
            prim_face_count = 0

            # Build list of polygons as lists of tuple-starts into p_ints
            poly_chunks: list[tuple[list[int], int, int]] = []  # (p_ints, start_vert_elem, vcount)
            if tag == "triangles":
                p_el = _find_child(child, "p")
                if p_el is not None:
                    p_ints = _parse_ints(p_el.text)
                    tri_count = len(p_ints) // (3 * num_inputs)
                    for t in range(tri_count):
                        poly_chunks.append((p_ints, t * 3, 3))
            elif tag == "polylist":
                vc_el = _find_child(child, "vcount")
                p_el = _find_child(child, "p")
                if vc_el is not None and p_el is not None:
                    vcounts = _parse_ints(vc_el.text)
                    p_ints = _parse_ints(p_el.text)
                    cursor = 0
                    for vc in vcounts:
                        if vc >= 3:
                            poly_chunks.append((p_ints, cursor, vc))
                        cursor += abs(vc)
            elif tag == "polygons":
                for p_el in _find_children(child, "p"):
                    p_ints = _parse_ints(p_el.text)
                    vc = len(p_ints) // num_inputs
                    if vc >= 3:
                        poly_chunks.append((p_ints, 0, vc))
                for ph_el in _find_children(child, "ph"):
                    p_el = _find_child(ph_el, "p")
                    if p_el is not None:
                        p_ints = _parse_ints(p_el.text)
                        vc = len(p_ints) // num_inputs
                        if vc >= 3:
                            poly_chunks.append((p_ints, 0, vc))
            elif tag == "trifans":
                for p_el in _find_children(child, "p"):
                    p_ints = _parse_ints(p_el.text)
                    vc = len(p_ints) // num_inputs
                    if vc >= 3:
                        for k in range(1, vc - 1):
                            tri_p = (
                                p_ints[0:num_inputs]
                                + p_ints[k * num_inputs:(k + 1) * num_inputs]
                                + p_ints[(k + 1) * num_inputs:(k + 2) * num_inputs]
                            )
                            poly_chunks.append((tri_p, 0, 3))
            elif tag == "tristrips":
                for p_el in _find_children(child, "p"):
                    p_ints = _parse_ints(p_el.text)
                    vc = len(p_ints) // num_inputs
                    if vc >= 3:
                        for k in range(2, vc):
                            if (k % 2) == 0:
                                i0, i1, i2 = k - 2, k - 1, k
                            else:
                                i0, i1, i2 = k - 1, k - 2, k
                            tri_p = (
                                p_ints[i0 * num_inputs:(i0 + 1) * num_inputs]
                                + p_ints[i1 * num_inputs:(i1 + 1) * num_inputs]
                                + p_ints[i2 * num_inputs:(i2 + 1) * num_inputs]
                            )
                            poly_chunks.append((tri_p, 0, 3))

            norm_vals, norm_stride, _ = sources.get(norm_src_id, ([], 3, ""))
            if norm_stride <= 0:
                norm_stride = 3

            for p_ints, vstart, vc in poly_chunks:
                fverts: list[int] = []
                fnorms: list[Vector] = []
                for j in range(vc):
                    base = (vstart + j) * num_inputs
                    vi = p_ints[base + vert_off]
                    fverts.append(vi)

                    if norm_off >= 0 and norm_vals:
                        ni = p_ints[base + norm_off]
                        nb = ni * norm_stride
                        nv = Vector((norm_vals[nb], norm_vals[nb + 1], norm_vals[nb + 2]))
                        if nv.length > 0.0:
                            nv.normalize()
                        fnorms.append(nv)
                    elif vert_normal_src_id and norm_vals:
                        nb = vi * norm_stride
                        nv = Vector((norm_vals[nb], norm_vals[nb + 1], norm_vals[nb + 2]))
                        if nv.length > 0.0:
                            nv.normalize()
                        fnorms.append(nv)

                # Pad any existing UV/color sets that aren't in this primitive
                cur_uv_srcs = {s for s, _ in uv_inputs}
                for u_id in uv_set_order:
                    if u_id not in cur_uv_srcs:
                        uv_sets[u_id].extend([(0.0, 0.0)] * vc)
                for u_id, u_off in uv_inputs:
                    u_vals, u_stride, _ = sources.get(u_id, ([], 2, ""))
                    if u_stride <= 0:
                        u_stride = 2
                    for j in range(vc):
                        base = (vstart + j) * num_inputs
                        ui = p_ints[base + u_off] * u_stride
                        if ui + 1 < len(u_vals):
                            uv_sets[u_id].append((u_vals[ui], u_vals[ui + 1]))
                        else:
                            uv_sets[u_id].append((0.0, 0.0))

                cur_col_names = {cn for cn, _, _ in col_inputs}
                for cn in col_set_order:
                    if cn not in cur_col_names:
                        col_sets[cn].extend([(1.0, 1.0, 1.0, 1.0)] * vc)
                for cn, c_src_id, c_off in col_inputs:
                    c_vals, c_stride, _ = sources.get(c_src_id, ([], 3, ""))
                    if c_stride <= 0:
                        c_stride = 3
                    for j in range(vc):
                        base = (vstart + j) * num_inputs
                        ci = p_ints[base + c_off] * c_stride
                        if ci + 2 < len(c_vals):
                            a = c_vals[ci + 3] if (c_stride >= 4 and ci + 3 < len(c_vals)) else 1.0
                            col_sets[cn].append((c_vals[ci], c_vals[ci + 1], c_vals[ci + 2], a))
                        else:
                            col_sets[cn].append((1.0, 1.0, 1.0, 1.0))

                face_vert_indices.append(fverts)
                prim_face_count += 1

                if fnorms and len(fnorms) == vc:
                    is_flat = True
                    n0 = fnorms[0]
                    for nk in fnorms[1:]:
                        dp = n0.dot(nk)
                        if dp < 0.99999 or dp > 1.00001:
                            is_flat = False
                            break
                    face_sharp.append(is_flat)
                    for nv in fnorms:
                        loop_normals.append((nv.x, nv.y, nv.z))
                else:
                    face_sharp.append(True)
                    has_all_loop_normals = False

            if prim_face_count > 0:
                mat_prims.setdefault(mat_sym, []).append((prim_start_face, prim_face_count))

        # Construct Blender Mesh
        me = bpy.data.meshes.new(name=geom_name)
        verts_co = [
            (pos_vals[i * pos_stride], pos_vals[i * pos_stride + 1], pos_vals[i * pos_stride + 2] if pos_stride >= 3 else 0.0)
            for i in range(num_verts)
        ]
        me.from_pydata(verts_co, loose_edges, face_vert_indices)

        # Smooth/flat face flags
        if face_sharp and len(face_sharp) == len(me.polygons):
            for pi, is_flat in enumerate(face_sharp):
                me.polygons[pi].use_smooth = not is_flat

        # UVs
        for u_id in uv_set_order:
            uv_list = uv_sets[u_id]
            if len(uv_list) == len(me.loops):
                uv_layer = me.uv_layers.new(name=u_id)
                flat_uvs = [c for uv in uv_list for c in uv]
                uv_layer.data.foreach_set("uv", flat_uvs)

        # Byte Color Attributes
        for cn in col_set_order:
            c_list = col_sets[cn]
            if len(c_list) == len(me.loops):
                cattr = me.color_attributes.new(name=cn, type="BYTE_COLOR", domain="CORNER")
                for li, col in enumerate(c_list):
                    cattr.data[li].color_srgb = col

        # Custom normals
        if self.settings.custom_normals and has_all_loop_normals and len(loop_normals) == len(me.loops) and len(loop_normals) > 0:
            try:
                me.normals_split_custom_set(loop_normals)
            except Exception as e:
                print(f"Collada import: Could not set custom normals on {geom_name}: {e}")

        me.update()
        self.meshes_by_id[geom_id] = me
        self.mesh_mat_prims[geom_id] = mat_prims

    # ------------------------------------------------------------------
    # <library_controllers>
    # ------------------------------------------------------------------

    def _read_library_controllers(self, lib: ET.Element) -> None:
        for ctrl in _find_children(lib, "controller"):
            cid = ctrl.attrib.get("id", "")
            skin = _find_child(ctrl, "skin")
            if skin is not None:
                self._read_skin_controller(cid, skin)
            morph = _find_child(ctrl, "morph")
            if morph is not None:
                self._read_morph_controller(cid, morph)

    def _read_skin_controller(self, cid: str, skin: ET.Element) -> None:
        geom_id = _uri_to_local_id(skin.attrib.get("source"))
        bsm_el = _find_child(skin, "bind_shape_matrix")
        bsm = dae_flat_to_matrix(_parse_floats(bsm_el.text)) if bsm_el is not None else Matrix.Identity(4)

        name_sources: dict[str, list[str]] = {}
        float_sources: dict[str, list[float]] = {}
        for src in _find_children(skin, "source"):
            sid = src.attrib.get("id", "")
            na = _find_child(src, "Name_array")
            if na is None:
                na = _find_child(src, "IDREF_array")
            if na is not None:
                name_sources[sid] = _parse_strings(na.text)
            fa = _find_child(src, "float_array")
            if fa is not None:
                float_sources[sid] = _parse_floats(fa.text)

        joint_names: list[str] = []
        inv_bind_mats: list[Matrix] = []
        joints_el = _find_child(skin, "joints")
        if joints_el is not None:
            for inp in _find_children(joints_el, "input"):
                sem = inp.attrib.get("semantic", "")
                sid = _uri_to_local_id(inp.attrib.get("source"))
                if sem == "JOINT":
                    joint_names = name_sources.get(sid, [])
                elif sem == "INV_BIND_MATRIX":
                    floats = float_sources.get(sid, [])
                    for i in range(len(floats) // 16):
                        inv_bind_mats.append(dae_flat_to_matrix(floats[i * 16:(i + 1) * 16]))

        vcounts: list[int] = []
        v_pairs: list[tuple[int, float]] = []
        vw_el = _find_child(skin, "vertex_weights")
        if vw_el is not None:
            j_off = 0
            w_off = 1
            w_src = ""
            max_off = 0
            for inp in _find_children(vw_el, "input"):
                sem = inp.attrib.get("semantic", "")
                off = int(inp.attrib.get("offset", "0"))
                if off > max_off:
                    max_off = off
                if sem == "JOINT":
                    j_off = off
                elif sem == "WEIGHT":
                    w_off = off
                    w_src = _uri_to_local_id(inp.attrib.get("source"))
            stride = max_off + 1
            weights = float_sources.get(w_src, [])
            vc_el = _find_child(vw_el, "vcount")
            v_el = _find_child(vw_el, "v")
            vcounts = _parse_ints(vc_el.text) if vc_el is not None else []
            v_ints = _parse_ints(v_el.text) if v_el is not None else []
            total_pairs = sum(vcounts)
            for i in range(total_pairs):
                base = i * stride
                if base + max_off < len(v_ints):
                    ji = v_ints[base + j_off]
                    wi = v_ints[base + w_off]
                    w = weights[wi] if 0 <= wi < len(weights) else 0.0
                    v_pairs.append((ji, w))

        self.skins_by_id[cid] = _SkinData(
            controller_id=cid,
            geometry_id=geom_id,
            bind_shape_matrix=bsm,
            joint_names=joint_names,
            inv_bind_matrices=inv_bind_mats,
            vcounts=vcounts,
            v_pairs=v_pairs,
        )

    def _read_morph_controller(self, cid: str, morph: ET.Element) -> None:
        geom_id = _uri_to_local_id(morph.attrib.get("source"))
        idref_sources: dict[str, list[str]] = {}
        float_sources: dict[str, list[float]] = {}
        for src in _find_children(morph, "source"):
            sid = src.attrib.get("id", "")
            ia = _find_child(src, "IDREF_array")
            if ia is None:
                ia = _find_child(src, "Name_array")
            if ia is not None:
                idref_sources[sid] = _parse_strings(ia.text)
            fa = _find_child(src, "float_array")
            if fa is not None:
                float_sources[sid] = _parse_floats(fa.text)

        targets_el = _find_child(morph, "targets")
        target_ids: list[str] = []
        weights: list[float] = []
        if targets_el is not None:
            for inp in _find_children(targets_el, "input"):
                sem = inp.attrib.get("semantic", "")
                sid = _uri_to_local_id(inp.attrib.get("source"))
                if sem == "MORPH_TARGET":
                    target_ids = idref_sources.get(sid, [])
                elif sem == "MORPH_WEIGHT":
                    weights = float_sources.get(sid, [])

        self.morphs_by_id[cid] = _MorphData(
            controller_id=cid,
            geometry_id=geom_id,
            target_geom_ids=target_ids,
            weights=weights,
        )

    # ------------------------------------------------------------------
    # <library_visual_scenes> Node Processing
    # ------------------------------------------------------------------

    def _read_node_matrix(self, node: ET.Element) -> Matrix:
        mat = Matrix.Identity(4)
        for ch in node:
            tag = _strip_ns(ch.tag)
            if tag == "matrix":
                vals = _parse_floats(ch.text)
                if len(vals) == 16:
                    return dae_flat_to_matrix(vals)
            elif tag == "translate":
                vals = _parse_floats(ch.text)
                if len(vals) >= 3:
                    mat = mat @ Matrix.Translation((vals[0], vals[1], vals[2]))
            elif tag == "rotate":
                vals = _parse_floats(ch.text)
                if len(vals) >= 4:
                    axis = Vector((vals[0], vals[1], vals[2]))
                    if axis.length > 0.0:
                        mat = mat @ Matrix.Rotation(math.radians(vals[3]), 4, axis)
            elif tag == "scale":
                vals = _parse_floats(ch.text)
                if len(vals) >= 3:
                    mat = mat @ Matrix.Diagonal((vals[0], vals[1], vals[2], 1.0))
        return mat

    def _node_is_decomposed(self, node: ET.Element) -> bool:
        for ch in node:
            if _strip_ns(ch.tag) == "matrix":
                return False
        return True

    def _add_object_to_scene(self, name: str, data) -> Object:
        ob = bpy.data.objects.new(name=name, object_data=data)
        self.scene.collection.objects.link(ob)
        ob.select_set(True)
        self.view_layer.objects.active = ob
        return ob

    def _apply_material_bindings(self, inst_elem: ET.Element, ob: Object, geom_id: str) -> None:
        bm = _find_child(inst_elem, "bind_material")
        if bm is None:
            return
        tc = _find_child(bm, "technique_common")
        if tc is None:
            return
        mat_prims = self.mesh_mat_prims.get(geom_id, {})
        mesh: Mesh = ob.data
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
            for start_f, count_f in mat_prims.get(sym, ()):
                for fi in range(start_f, start_f + count_f):
                    if fi < len(mesh.polygons):
                        mesh.polygons[fi].material_index = idx

    def _process_node(
        self, node: ET.Element, parent_node: ET.Element | None, parent_ob: Object | None
    ) -> None:
        node_id = node.attrib.get("id", "")
        node_name = node.attrib.get("name") or node_id or "Node"
        node_type = node.attrib.get("type", "NODE").upper()
        if node_id:
            self.node_elem_by_id[node_id] = node

        child_nodes = _find_children(node, "node")

        if node_type == "JOINT":
            if parent_node is None:
                arm_data = bpy.data.armatures.new(name="Armature")
                parent_ob = self._add_object_to_scene("Armature", arm_data)
                self.root_objects.append(parent_ob)
            if parent_node is None or parent_node.attrib.get("type", "NODE").upper() != "JOINT":
                if parent_ob is not None and parent_ob.type == "ARMATURE":
                    self.root_joint_nodes.append((node, parent_ob))
            return

        inst_geoms = _find_children(node, "instance_geometry")
        inst_ctrls = _find_children(node, "instance_controller")
        inst_cams = _find_children(node, "instance_camera")
        inst_lights = _find_children(node, "instance_light")
        inst_nodes = _find_children(node, "instance_node")

        created: list[Object] = []

        for ig in inst_geoms:
            gid = _uri_to_local_id(ig.attrib.get("url"))
            me = self.meshes_by_id.get(gid)
            if me is not None:
                ob = self._add_object_to_scene(node_name, me)
                self._apply_material_bindings(ig, ob, gid)
                self.object_by_geom_id[gid] = ob
                self.imported_mesh_objects.append(ob)
                created.append(ob)

        for ic in inst_ctrls:
            cid = _uri_to_local_id(ic.attrib.get("url"))
            gid = ""
            if cid in self.skins_by_id:
                gid = self.skins_by_id[cid].geometry_id
            elif cid in self.morphs_by_id:
                gid = self.morphs_by_id[cid].geometry_id
            me = self.meshes_by_id.get(gid)
            if me is not None:
                ob = self._add_object_to_scene(node_name, me)
                self._apply_material_bindings(ic, ob, gid)
                self.object_by_geom_id[gid] = ob
                self.imported_mesh_objects.append(ob)
                created.append(ob)

        for icam in inst_cams:
            cam_id = _uri_to_local_id(icam.attrib.get("url"))
            cam = self.cameras_by_id.get(cam_id)
            if cam is not None:
                created.append(self._add_object_to_scene(node_name, cam))

        for ila in inst_lights:
            la_id = _uri_to_local_id(ila.attrib.get("url"))
            la = self.lights_by_id.get(la_id)
            if la is not None:
                created.append(self._add_object_to_scene(node_name, la))

        for ino in inst_nodes:
            target_id = _uri_to_local_id(ino.attrib.get("url"))
            src_node = self.lib_nodes_by_id.get(target_id)
            if src_node is not None:
                self._process_node(src_node, parent_node, parent_ob)

        if not created and not inst_nodes:
            is_arm = any(c.attrib.get("type", "NODE").upper() == "JOINT" for c in child_nodes)
            if is_arm:
                arm_data = bpy.data.armatures.new(name=node_name)
                ob = self._add_object_to_scene(node_name, arm_data)
                self._apply_armature_collections(node, arm_data)
            else:
                ob = self._add_object_to_scene(node_name, None)
            created.append(ob)

        mat = self._read_node_matrix(node)
        for ob in created:
            ob.matrix_basis = mat
            if parent_ob is not None:
                ob.parent = parent_ob
            else:
                self.root_objects.append(ob)
            if node_id:
                self.object_by_node_id[node_id] = ob
                self.node_elem_by_id[node_id] = node

        primary_ob = created[0] if created else parent_ob
        for ch in child_nodes:
            self._process_node(ch, node, primary_ob)

    def _apply_armature_collections(self, node: ET.Element, arm_data) -> None:
        extras = _parse_extra_blender(node)
        if not extras:
            return
        all_colls = [c for c in extras.get("collections", "").split("\n") if c]
        vis_colls = set(c for c in extras.get("visible_collections", "").split("\n") if c)
        for name in all_colls:
            bc = arm_data.collections.new(name)
            bc.is_visible = name in vis_colls
        active_name = extras.get("active_collection", "")
        if active_name and active_name in arm_data.collections:
            arm_data.collections.active = arm_data.collections[active_name]

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

    # ------------------------------------------------------------------
    # Armatures, Skinning & Shape Keys
    # ------------------------------------------------------------------

    def _make_armatures(self) -> None:
        # Index all joint nodes under root_joint_nodes
        def index_joints(jnode: ET.Element, ob_arm: Object, parent_j: ET.Element | None) -> None:
            jid = jnode.attrib.get("id", "")
            jsid = jnode.attrib.get("sid", "")
            jname = jnode.attrib.get("name") or jid or jsid
            if jid:
                self.joint_elem_by_id[jid] = jnode
                self.joint_arm_by_id[jid] = ob_arm
                self.joint_bone_name_by_id[jid] = jname
                self.joint_parent_elem_by_id[jid] = parent_j
            if jsid:
                self.joint_elem_by_sid[jsid] = jnode
            for ch in _find_children(jnode, "node"):
                if ch.attrib.get("type", "NODE").upper() == "JOINT":
                    index_joints(ch, ob_arm, jnode)

        for rj, ob_arm in self.root_joint_nodes:
            index_joints(rj, ob_arm, None)

        # Build lookup from joint_elem -> inv_bind_mat across all skins
        joint_inv_bind: dict[int, Matrix] = {}
        for skin in self.skins_by_id.values():
            for jname, ibm in zip(skin.joint_names, skin.inv_bind_matrices):
                je = self.joint_elem_by_sid.get(jname) or self.joint_elem_by_id.get(jname)
                if je is not None:
                    joint_inv_bind[id(je)] = ibm
                    if skin.ob_arm is None:
                        jid = je.attrib.get("id", "")
                        skin.ob_arm = self.joint_arm_by_id.get(jid)

        # Group root joints by armature object
        arms_to_roots: dict[int, tuple[Object, list[ET.Element]]] = {}
        for rj, ob_arm in self.root_joint_nodes:
            entry = arms_to_roots.get(ob_arm.as_pointer())
            if entry is None:
                arms_to_roots[ob_arm.as_pointer()] = (ob_arm, [rj])
            else:
                entry[1].append(rj)

        for ob_arm, rjoints in arms_to_roots.values():
            self._build_armature_edit_bones(ob_arm, rjoints, joint_inv_bind)

        # Link skins to meshes
        for skin in self.skins_by_id.values():
            ob_mesh = self.object_by_geom_id.get(skin.geometry_id)
            ob_arm = skin.ob_arm
            if ob_mesh is None or ob_arm is None:
                continue
            mod = ob_mesh.modifiers.new(name="Armature", type="ARMATURE")
            mod.object = ob_arm
            mod.use_vertex_groups = True
            if ob_mesh.parent != ob_arm:
                ob_mesh.parent = ob_arm
                if ob_mesh in self.root_objects:
                    self.root_objects.remove(ob_mesh)
            ob_mesh.matrix_basis = skin.bind_shape_matrix

            # Create vertex groups
            vgroups = []
            for jname in skin.joint_names:
                je = self.joint_elem_by_sid.get(jname) or self.joint_elem_by_id.get(jname)
                bname = (je.attrib.get("name") or je.attrib.get("id") or jname) if je is not None else jname
                vgroups.append(ob_mesh.vertex_groups.new(name=bname))

            cursor = 0
            for vi, vc in enumerate(skin.vcounts):
                for _ in range(vc):
                    if cursor < len(skin.v_pairs):
                        ji, w = skin.v_pairs[cursor]
                        if 0 <= ji < len(vgroups) and w > 0.0:
                            vgroups[ji].add([vi], w, "REPLACE")
                    cursor += 1

    def _build_armature_edit_bones(
        self, ob_arm: Object, root_joints: list[ET.Element], joint_inv_bind: dict[int, Matrix]
    ) -> None:
        self.view_layer.objects.active = ob_arm
        bpy.ops.object.mode_set(mode="EDIT")
        arm = ob_arm.data
        arm_world_inv = ob_arm.matrix_world.inverted()

        leaf_bone_length = float("inf")
        ebone_extras: dict[str, dict[str, str]] = {}
        ebone_has_tail: set[str] = set()
        ebone_chain_len: dict[str, int] = {}

        def create_bone_rec(jnode: ET.Element, parent_eb, parent_mat: Matrix | None, sibcount: int) -> int:
            nonlocal leaf_bone_length
            bname = jnode.attrib.get("name") or jnode.attrib.get("id") or "Bone"
            eb = arm.edit_bones.new(bname)
            extras = _parse_extra_blender(jnode)
            ebone_extras[eb.name] = extras

            ibm = joint_inv_bind.get(id(jnode))
            if ibm is not None:
                joint_bind_mat = ibm.inverted()
                mat = arm_world_inv @ joint_bind_mat
                bone_is_skinned = True
            else:
                local_m = self._read_node_matrix(jnode)
                mat = (parent_mat @ local_m) if parent_mat is not None else local_m
                joint_bind_mat = mat
                bone_is_skinned = False

            if parent_eb is not None:
                eb.parent = parent_eb

            # Collections
            for cname in (c for c in extras.get("collections", "").split("\n") if c):
                if cname in arm.collections:
                    arm.collections[cname].assign(eb)

            # Connect (must be set BEFORE eb.head so setting eb.use_connect=True
            # does not snap eb.head to the parent's placeholder tail!)
            has_tail = "tip_x" in extras and "tip_y" in extras and "tip_z" in extras
            has_connect = "connect" in extras
            connect_type = extras.get("connect", "") in ("1", "true", "True") if has_connect else False
            if has_tail and not has_connect:
                has_connect = True
                connect_type = False
            if not has_connect and self.settings.auto_connect:
                connect_type = sibcount == 1
            eb.use_connect = connect_type

            # Head & Tail
            eb.head = mat.translation
            if has_tail:
                tip = Vector((float(extras["tip_x"]), float(extras["tip_y"]), float(extras["tip_z"])))
                eb.tail = eb.head + tip
                ebone_has_tail.add(eb.name)
            else:
                eb.tail = eb.head + Vector((0.0, 0.5, 0.0))

            # Roll
            if "roll" in extras:
                eb.roll = float(extras["roll"])
            else:
                _, roll = bpy.types.Bone.AxisRollFromMatrix(mat.to_3x3())
                eb.roll = roll

            if bone_is_skinned and self.settings.keep_bind_info:
                rest_m = self._read_node_matrix(jnode)
                set_editbone_property_matrix(eb, "bind_mat", joint_bind_mat)
                set_editbone_property_matrix(eb, "rest_mat", rest_m)

            if parent_eb is not None:
                if eb.use_connect:
                    parent_eb.tail = eb.head.copy()
                    ebone_has_tail.add(parent_eb.name)
                dist = (eb.head - parent_eb.head).length
                if dist < leaf_bone_length and dist > MINIMUM_BONE_LENGTH:
                    leaf_bone_length = dist

            child_joints = [
                ch for ch in _find_children(jnode, "node")
                if ch.attrib.get("type", "NODE").upper() == "JOINT"
            ]
            max_cl = 0
            for ch in child_joints:
                cl = create_bone_rec(ch, eb, mat, len(child_joints))
                if cl > max_cl:
                    max_cl = cl
            chain_len = max_cl + 1
            ebone_chain_len[eb.name] = chain_len
            return chain_len

        for rj in root_joints:
            create_bone_rec(rj, None, None, len(root_joints))

        # Connect bone chains if find_chains is requested (ArmatureImporter.cpp:280-358)
        if self.settings.find_chains:
            UNLIMITED_CHAIN_MAX = 10**9

            def connect_bone_chains(parent_eb, max_chain_len: int) -> None:
                children = list(parent_eb.children)
                dominant_child = None
                maxlen = 0
                if children:
                    for ch in children:
                        cl = ebone_chain_len.get(ch.name, 1)
                        if cl <= max_chain_len:
                            if cl > maxlen:
                                dominant_child = ch
                                maxlen = cl
                            elif cl == maxlen:
                                dominant_child = None
                if dominant_child is not None:
                    if not dominant_child.use_connect:
                        vec = dominant_child.head - parent_eb.head
                        if vec.length_squared > MINIMUM_BONE_LENGTH:
                            parent_eb.tail = dominant_child.head.copy()
                            ebone_has_tail.add(parent_eb.name)
                            if ebone_chain_len.get(parent_eb.name, 1) >= self.settings.min_chain_length:
                                dominant_child.use_connect = True
                    for ch in children:
                        connect_bone_chains(ch, UNLIMITED_CHAIN_MAX)
                elif maxlen > 1 and maxlen > self.settings.min_chain_length:
                    connect_bone_chains(parent_eb, maxlen - 1)
                else:
                    for ch in children:
                        connect_bone_chains(ch, UNLIMITED_CHAIN_MAX)

            for eb in list(arm.edit_bones):
                if eb.parent is None:
                    connect_bone_chains(eb, UNLIMITED_CHAIN_MAX)

        # Fix leaf bones
        default_leaf_len = 1.0 if leaf_bone_length == float("inf") else leaf_bone_length
        for eb in arm.edit_bones:
            is_leaf = not any(c.use_connect for c in eb.children)
            if is_leaf and eb.name not in ebone_has_tail:
                if self.settings.fix_orientation and eb.parent is not None:
                    vec = eb.head - eb.parent.head
                    if vec.length_squared < MINIMUM_BONE_LENGTH:
                        vec = eb.parent.tail - eb.parent.head
                else:
                    vec = eb.tail - eb.head
                if vec.length > 0.0:
                    vec.normalize()
                    eb.tail = eb.head + vec * default_leaf_len

        bpy.ops.object.mode_set(mode="OBJECT")

        # Set pose bone rotation mode (EUL if decomposed else QUAT)
        def set_rotmode_rec(jnode: ET.Element) -> None:
            bname = jnode.attrib.get("name") or jnode.attrib.get("id") or "Bone"
            pchan = ob_arm.pose.bones.get(bname)
            if pchan is not None:
                pchan.rotation_mode = "XYZ" if self._node_is_decomposed(jnode) else "QUATERNION"
            for ch in _find_children(jnode, "node"):
                if ch.attrib.get("type", "NODE").upper() == "JOINT":
                    set_rotmode_rec(ch)

        for rj in root_joints:
            set_rotmode_rec(rj)

    def _make_shape_keys(self) -> None:
        for morph in self.morphs_by_id.values():
            ob = self.object_by_geom_id.get(morph.geometry_id)
            if ob is None or ob.type != "MESH":
                continue
            if ob.data.shape_keys is None:
                ob.shape_key_add(name="Basis", from_mix=False)
            for idx, tid in enumerate(morph.target_geom_ids):
                tmesh = self.meshes_by_id.get(tid)
                if tmesh is None or len(tmesh.vertices) != len(ob.data.vertices):
                    continue
                sk = ob.shape_key_add(name=tmesh.name, from_mix=False)
                for vi, v in enumerate(tmesh.vertices):
                    sk.data[vi].co = v.co
                if idx < len(morph.weights):
                    sk.value = morph.weights[idx]

    # ------------------------------------------------------------------
    # <library_animations>
    # ------------------------------------------------------------------

    def _read_library_animations(self, lib: ET.Element) -> None:
        for anim in _find_children(lib, "animation"):
            self._read_animation_elem(anim)

    def _read_animation_elem(self, anim: ET.Element) -> None:
        # Recurse nested <animation> elements
        for sub in _find_children(anim, "animation"):
            self._read_animation_elem(sub)

        float_sources: dict[str, tuple[list[float], int]] = {}
        name_sources: dict[str, list[str]] = {}
        for src in _find_children(anim, "source"):
            sid = src.attrib.get("id", "")
            stride = 1
            tc = _find_child(src, "technique_common")
            if tc is not None:
                acc = _find_child(tc, "accessor")
                if acc is not None and "stride" in acc.attrib:
                    stride = int(acc.attrib["stride"])
            fa = _find_child(src, "float_array")
            if fa is not None:
                float_sources[sid] = (_parse_floats(fa.text), stride)
            na = _find_child(src, "Name_array")
            if na is not None:
                name_sources[sid] = _parse_strings(na.text)

        samplers: dict[str, dict[str, str]] = {}
        for samp in _find_children(anim, "sampler"):
            sid = samp.attrib.get("id", "")
            smap: dict[str, str] = {}
            for inp in _find_children(samp, "input"):
                smap[inp.attrib.get("semantic", "")] = _uri_to_local_id(inp.attrib.get("source"))
            samplers[sid] = smap

        for ch in _find_children(anim, "channel"):
            samp_id = _uri_to_local_id(ch.attrib.get("source"))
            target = ch.attrib.get("target", "")
            smap = samplers.get(samp_id)
            if not smap or not target:
                continue
            times, _ = float_sources.get(smap.get("INPUT", ""), ([], 1))
            values, stride = float_sources.get(smap.get("OUTPUT", ""), ([], 1))
            interps = name_sources.get(smap.get("INTERPOLATION", ""), [])
            in_tan, _ = float_sources.get(smap.get("IN_TANGENT", ""), ([], 2))
            out_tan, _ = float_sources.get(smap.get("OUT_TANGENT", ""), ([], 2))
            if times and values:
                self.animations.append(
                    _AnimChannelData(
                        target=target,
                        times=times,
                        values=values,
                        stride=stride,
                        interps=interps,
                        in_tangents=in_tan,
                        out_tangents=out_tan,
                    )
                )

    def _apply_animations(self) -> None:
        if not self.animations:
            return
        fps = self.scene.render.fps / self.scene.render.fps_base

        for ch in self.animations:
            if "/" not in ch.target:
                continue
            target_id, prop_part = ch.target.split("/", 1)
            axis_part = ""
            if "." in prop_part:
                prop_name, axis_part = prop_part.split(".", 1)
            else:
                prop_name = prop_part

            # Check if target is a joint node
            if target_id in self.joint_arm_by_id:
                ob_arm = self.joint_arm_by_id[target_id]
                bname = self.joint_bone_name_by_id[target_id]
                jnode = self.joint_elem_by_id[target_id]
                if ch.stride == 16:
                    self._apply_joint_matrix_animation(ob_arm, bname, jnode, ch, fps)
                else:
                    self._apply_decomposed_animation(
                        ob_arm, f'pose.bones["{bname}"].', prop_name, axis_part, ch, fps, node_elem=jnode
                    )
                continue

            # Check if target is an object node
            ob = self.object_by_node_id.get(target_id)
            if ob is not None:
                if ch.stride == 16:
                    self._apply_object_matrix_animation(ob, ch, fps)
                else:
                    self._apply_decomposed_animation(
                        ob, "", prop_name, axis_part, ch, fps, node_elem=self.node_elem_by_id.get(target_id)
                    )
                continue

            # Check camera
            if target_id in self.cameras_by_id:
                cam = self.cameras_by_id[target_id]
                _, cbag = ensure_action_and_channelbag(cam)
                if prop_name == "xfov":
                    fcu = create_fcurve_on_channelbag(cbag, "lens", 0)
                    vals = [fov_to_focallength(math.radians(v), cam.sensor_width) for v in ch.values]
                    self._populate_fcurve(fcu, ch.times, vals, ch.interps, fps)
                elif prop_name == "znear":
                    fcu = create_fcurve_on_channelbag(cbag, "clip_start", 0)
                    self._populate_fcurve(fcu, ch.times, ch.values, ch.interps, fps)
                elif prop_name == "zfar":
                    fcu = create_fcurve_on_channelbag(cbag, "clip_end", 0)
                    self._populate_fcurve(fcu, ch.times, ch.values, ch.interps, fps)
                elif prop_name == "xmag":
                    fcu = create_fcurve_on_channelbag(cbag, "ortho_scale", 0)
                    self._populate_fcurve(fcu, ch.times, [v * 2.0 for v in ch.values], ch.interps, fps)
                continue

            # Check light
            if target_id in self.lights_by_id:
                la = self.lights_by_id[target_id]
                _, cbag = ensure_action_and_channelbag(la)
                if prop_name == "color":
                    axis_idx = {"R": 0, "G": 1, "B": 2}.get(axis_part.upper(), -1)
                    if axis_idx >= 0:
                        fcu = create_fcurve_on_channelbag(cbag, "color", axis_idx)
                        self._populate_fcurve(fcu, ch.times, ch.values, ch.interps, fps)
                elif prop_name in ("falloff_angle", "fall_off_angle"):
                    fcu = create_fcurve_on_channelbag(cbag, "spot_size", 0)
                    self._populate_fcurve(fcu, ch.times, [math.radians(v) for v in ch.values], ch.interps, fps)
                elif prop_name in ("falloff_exponent", "fall_off_exponent"):
                    fcu = create_fcurve_on_channelbag(cbag, "spot_blend", 0)
                    self._populate_fcurve(fcu, ch.times, ch.values, ch.interps, fps)

    def _populate_fcurve(
        self,
        fcu,
        times: list[float],
        values: list[float],
        interps: list[str],
        fps: float,
        in_tan: list[float] | None = None,
        out_tan: list[float] | None = None,
        is_angle: bool = False,
    ) -> None:
        count = min(len(times), len(values))
        fcu.keyframe_points.add(count)
        for i in range(count):
            kp = fcu.keyframe_points[i]
            kp.co = (times[i] * fps, values[i])
            interp = interps[i] if i < len(interps) else "LINEAR"
            if interp == "BEZIER":
                kp.interpolation = "BEZIER"
                if in_tan and 2 * i + 1 < len(in_tan):
                    hy = math.radians(in_tan[2 * i + 1]) if is_angle else in_tan[2 * i + 1]
                    kp.handle_left = (in_tan[2 * i] * fps, hy)
                if out_tan and 2 * i + 1 < len(out_tan):
                    hy = math.radians(out_tan[2 * i + 1]) if is_angle else out_tan[2 * i + 1]
                    kp.handle_right = (out_tan[2 * i] * fps, hy)
            elif interp == "STEP":
                kp.interpolation = "CONSTANT"
            else:
                kp.interpolation = "LINEAR"
        fcu.update()

    def _apply_object_matrix_animation(self, ob: Object, ch: _AnimChannelData, fps: float) -> None:
        _, cbag = ensure_action_and_channelbag(ob)
        ob.rotation_mode = "QUATERNION"
        locs: list[list[float]] = [[], [], []]
        quats: list[list[float]] = [[], [], [], []]
        scales: list[list[float]] = [[], [], []]
        for i in range(len(ch.times)):
            m = dae_flat_to_matrix(ch.values[i * 16:(i + 1) * 16])
            loc, quat, scale = m.decompose()
            for a in range(3):
                locs[a].append(loc[a])
                scales[a].append(scale[a])
            for a in range(4):
                quats[a].append(quat[a])

        for a in range(4):
            fcu = create_fcurve_on_channelbag(cbag, "rotation_quaternion", a)
            self._populate_fcurve(fcu, ch.times, quats[a], ch.interps, fps)
        for a in range(3):
            fcu = create_fcurve_on_channelbag(cbag, "location", a)
            self._populate_fcurve(fcu, ch.times, locs[a], ch.interps, fps)
        for a in range(3):
            fcu = create_fcurve_on_channelbag(cbag, "scale", a)
            self._populate_fcurve(fcu, ch.times, scales[a], ch.interps, fps)

    def _apply_joint_matrix_animation(
        self, ob_arm: Object, bname: str, jnode: ET.Element, ch: _AnimChannelData, fps: float
    ) -> None:
        bone = ob_arm.data.bones.get(bname)
        pchan = ob_arm.pose.bones.get(bname)
        if bone is None or pchan is None:
            return
        _, cbag = ensure_action_and_channelbag(ob_arm)
        pchan.rotation_mode = "QUATERNION"

        # Compute rest matrices (AnimationImporter::apply_matrix_curves formula: iR @ M @ iR_dae @ R)
        rest_local = self._read_node_matrix(jnode)
        irest_dae = rest_local.inverted()
        rest = bone.matrix_local.copy()
        irest = rest.inverted()

        prefix = f'pose.bones["{bname}"].'
        locs: list[list[float]] = [[], [], []]
        quats: list[list[float]] = [[], [], [], []]
        scales: list[list[float]] = [[], [], []]

        # Parent rest matrix
        parent_rest = bone.parent.matrix_local.copy() if bone.parent else Matrix.Identity(4)

        for i in range(len(ch.times)):
            matfra = dae_flat_to_matrix(ch.values[i * 16:(i + 1) * 16])
            temp = parent_rest @ matfra
            mat = irest @ temp @ (parent_rest @ rest_local).inverted() @ rest
            loc, quat, scale = mat.decompose()
            for a in range(3):
                locs[a].append(loc[a])
                scales[a].append(scale[a])
            for a in range(4):
                quats[a].append(quat[a])

        for a in range(4):
            fcu = create_fcurve_on_channelbag(cbag, f"{prefix}rotation_quaternion", a)
            self._populate_fcurve(fcu, ch.times, quats[a], ch.interps, fps)
        for a in range(3):
            fcu = create_fcurve_on_channelbag(cbag, f"{prefix}location", a)
            self._populate_fcurve(fcu, ch.times, locs[a], ch.interps, fps)
        for a in range(3):
            fcu = create_fcurve_on_channelbag(cbag, f"{prefix}scale", a)
            self._populate_fcurve(fcu, ch.times, scales[a], ch.interps, fps)

    def _resolve_transform_sid(self, node_elem: ET.Element | None, prop_name: str) -> tuple[str, int]:
        """Resolve a transform sid on node_elem to (kind, rot_axis_idx) for third-party DCCs."""
        if node_elem is not None:
            for child in node_elem:
                if child.attrib.get("sid") == prop_name:
                    tag = _strip_ns(child.tag)
                    if tag == "translate":
                        return ("location", -1)
                    if tag == "scale":
                        return ("scale", -1)
                    if tag == "rotate":
                        vals = _parse_floats(child.text)
                        if len(vals) >= 3:
                            ax = Vector((abs(vals[0]), abs(vals[1]), abs(vals[2])))
                            if ax.x >= ax.y and ax.x >= ax.z and ax.x > 0.5:
                                return ("rotation", 0)
                            if ax.y >= ax.x and ax.y >= ax.z and ax.y > 0.5:
                                return ("rotation", 1)
                            if ax.z > 0.5:
                                return ("rotation", 2)
        if prop_name in ("location", "translate"):
            return ("location", -1)
        if prop_name == "scale":
            return ("scale", -1)
        if prop_name in ("rotationX", "rotateX"):
            return ("rotation", 0)
        if prop_name in ("rotationY", "rotateY"):
            return ("rotation", 1)
        if prop_name in ("rotationZ", "rotateZ"):
            return ("rotation", 2)
        return ("", -1)

    def _apply_decomposed_animation(
        self,
        ob: Object,
        prefix: str,
        prop_name: str,
        axis_part: str,
        ch: _AnimChannelData,
        fps: float,
        node_elem: ET.Element | None = None,
    ) -> None:
        _, cbag = ensure_action_and_channelbag(ob)
        axis_map = {"X": 0, "Y": 1, "Z": 2}
        kind, rot_axis = self._resolve_transform_sid(node_elem, prop_name)
        if kind == "location":
            if ch.stride == 3:
                for a in range(3):
                    vals = [ch.values[i * 3 + a] for i in range(len(ch.times))]
                    fcu = create_fcurve_on_channelbag(cbag, f"{prefix}location", a)
                    self._populate_fcurve(fcu, ch.times, vals, ch.interps, fps)
            else:
                a = axis_map.get(axis_part.upper(), 0)
                fcu = create_fcurve_on_channelbag(cbag, f"{prefix}location", a)
                self._populate_fcurve(
                    fcu, ch.times, ch.values, ch.interps, fps, ch.in_tangents, ch.out_tangents
                )
        elif kind == "scale":
            if ch.stride == 3:
                for a in range(3):
                    vals = [ch.values[i * 3 + a] for i in range(len(ch.times))]
                    fcu = create_fcurve_on_channelbag(cbag, f"{prefix}scale", a)
                    self._populate_fcurve(fcu, ch.times, vals, ch.interps, fps)
            else:
                a = axis_map.get(axis_part.upper(), 0)
                fcu = create_fcurve_on_channelbag(cbag, f"{prefix}scale", a)
                self._populate_fcurve(
                    fcu, ch.times, ch.values, ch.interps, fps, ch.in_tangents, ch.out_tangents
                )
        elif kind == "rotation" and rot_axis >= 0:
            vals = [math.radians(v) for v in ch.values]
            fcu = create_fcurve_on_channelbag(cbag, f"{prefix}rotation_euler", rot_axis)
            self._populate_fcurve(
                fcu, ch.times, vals, ch.interps, fps, ch.in_tangents, ch.out_tangents, is_angle=True
            )
