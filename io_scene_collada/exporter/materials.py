# SPDX-FileCopyrightText: 2009-2026 Blender Authors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Path-to-URI conversion, export-set material collection, effects, images, and materials."""

from __future__ import annotations

import os
import shutil
import urllib.parse

import bpy

from ..collada_utils import (
    get_diffuse_image,
    get_effect_id,
    get_master_shader,
    get_material_id,
)
from ..xml_writer import encode_xml, fmt_f32

def _native_path_to_uri(path: str) -> str:
    """Match COLLADABU::URI::nativePathToUri()."""
    if not path:
        return ""
    p = path.replace("\\", "/")
    if len(p) >= 2 and p[1] == ":":
        return "file:///" + urllib.parse.quote(p, safe="/:")
    return urllib.parse.quote(p, safe="/")


class MaterialsMixin:
    def _iter_materials_in_export_set(self):
        seen: set[int] = set()
        for ob in self.export_set:
            if hasattr(ob, "material_slots"):
                for slot in ob.material_slots:
                    ma = slot.material
                    if ma is None:
                        continue
                    ptr = ma.as_pointer()
                    if ptr not in seen:
                        seen.add(ptr)
                        yield ma, ob
            for ma in getattr(self, "_extra_ob_materials", {}).get(ob.as_pointer(), ()):
                ptr = ma.as_pointer()
                if ptr not in seen:
                    seen.add(ptr)
                    yield ma, ob

    def _export_effects(self) -> None:
        has_any = bool(getattr(self, "_extra_ob_materials", None)) or any(
            slot.material is not None
            for ob in self.scene.objects
            for slot in getattr(ob, "material_slots", ())
        )
        if not has_any:
            return

        self.sw.open_element("library_effects")
        for ma, ob in self._iter_materials_in_export_set():
            eff_id = get_effect_id(self.id_map, ma)
            self.sw.open_element("effect", [("id", eff_id)])
            self.sw.open_element("profile_COMMON")

            shader = get_master_shader(ma)
            image = get_diffuse_image(ma)
            img_key = ""
            if image is not None:
                img_key = self.id_map.translate_id(image.name)
                if img_key not in self.key_image_map:
                    self.key_image_map[img_key] = image

                # <newparam> for surface and sampler2D
                surf_sid = f"{img_key}-surface"
                samp_sid = f"{img_key}-sampler"
                self.sw.open_element("newparam", [("sid", surf_sid)])
                self.sw.open_element("surface", [("type", "2D")])
                self.sw.write_element("init_from", img_key)
                self.sw.close_element()
                self.sw.close_element()

                self.sw.open_element("newparam", [("sid", samp_sid)])
                self.sw.open_element("sampler2D")
                self.sw.write_element("source", surf_sid)
                self.sw.close_element()
                self.sw.close_element()

            # Collect material values matching collada_utils.cpp
            # Base color
            diff_col = [ma.diffuse_color[0], ma.diffuse_color[1], ma.diffuse_color[2], 1.0]
            if shader is not None:
                bc_sock = shader.inputs.get("Base Color")
                if bc_sock is not None:
                    dv = bc_sock.default_value
                    diff_col = [dv[0], dv[1], dv[2], 1.0]

            # Emission
            em_col = [0.0, 0.0, 0.0, 1.0]
            if shader is not None:
                es_sock = shader.inputs.get("Emission Strength")
                ec_sock = shader.inputs.get("Emission Color")
                em_strength = float(es_sock.default_value) if es_sock is not None else 0.0
                if em_strength != 0.0 and ec_sock is not None:
                    dv = ec_sock.default_value
                    fc = [dv[0] * em_strength, dv[1] * em_strength, dv[2] * em_strength]
                    max_c = max(fc[0], fc[1], fc[2])
                    if max_c > 1.0:
                        fc = [c / max_c for c in fc]
                    em_col = [fc[0], fc[1], fc[2], dv[3]]

            # IOR
            ior = -1.0
            if shader is not None:
                ior_sock = shader.inputs.get("IOR")
                if ior_sock is not None:
                    ior = float(ior_sock.default_value)

            # Reflectivity (Metallic)
            reflectivity = float(ma.specular_intensity)
            if shader is not None:
                met_sock = shader.inputs.get("Metallic")
                if met_sock is not None:
                    reflectivity = float(met_sock.default_value)

            # Alpha
            alpha = float(ma.diffuse_color[3])
            if shader is not None:
                a_sock = shader.inputs.get("Alpha")
                if a_sock is not None:
                    alpha = float(a_sock.default_value)

            # Active UV layer name on ob
            active_uv = ""
            if ob.type == "MESH" and ob.data and len(ob.data.uv_layers) > 0:
                uv_act = ob.data.uv_layers.active
                active_uv = uv_act.name if uv_act else ob.data.uv_layers[0].name

            self.sw.open_element("technique", [("sid", "common")])
            self.sw.open_element("lambert")

            # Emission (OpenCOLLADA writes emission, ambient, diffuse, specular, shininess, reflective, reflectivity, transparent, transparency, index_of_refraction)
            self.sw.open_element("emission")
            self.sw.write_element(
                "color",
                f"{fmt_f32(em_col[0])} {fmt_f32(em_col[1])} {fmt_f32(em_col[2])} {fmt_f32(em_col[3])}",
                [("sid", "emission")],
            )
            self.sw.close_element()

            # Diffuse
            self.sw.open_element("diffuse")
            if image is not None:
                self.sw.write_element(
                    "texture",
                    None,
                    [("texture", f"{img_key}-sampler"), ("texcoord", encode_xml(active_uv))],
                )
            else:
                self.sw.write_element(
                    "color",
                    f"{fmt_f32(diff_col[0])} {fmt_f32(diff_col[1])} {fmt_f32(diff_col[2])} {fmt_f32(diff_col[3])}",
                    [("sid", "diffuse")],
                )
            self.sw.close_element()

            # Reflectivity
            if reflectivity > 0.0:
                self.sw.open_element("reflectivity")
                self.sw.write_element("float", fmt_f32(reflectivity), [("sid", "specular")])
                self.sw.close_element()

            # Transparent
            if alpha < 1.0:
                self.sw.open_element("transparent", [("opaque", "A_ONE")])
                self.sw.write_element(
                    "color", f"0 0 0 {fmt_f32(alpha)}", [("sid", "alpha")]
                )
                self.sw.close_element()

            # Index of refraction
            if ior >= 0.0:
                self.sw.open_element("index_of_refraction")
                self.sw.write_element("float", fmt_f32(ior), [("sid", "ior")])
                self.sw.close_element()

            self.sw.close_element()  # lambert
            self.sw.close_element()  # technique
            self.sw.close_element()  # profile_COMMON
            self.sw.close_element()  # effect

        self.sw.close_element()

    def _export_images(self) -> None:
        self.sw.open_element("library_images")
        export_dir = os.path.dirname(os.path.abspath(self.settings.filepath))
        use_copies = self.settings.use_texture_copies

        for img_key, image in self.key_image_map.items():
            is_generated = image.source == "GENERATED"
            is_dirty = image.is_dirty
            is_packed = image.packed_file is not None

            export_file = image.name
            if not os.path.splitext(export_file)[1]:
                ext_map = {
                    "PNG": ".png",
                    "JPEG": ".jpg",
                    "TARGA": ".tga",
                    "BMP": ".bmp",
                    "TIFF": ".tif",
                    "OPEN_EXR": ".exr",
                }
                export_file += ext_map.get(image.file_format, ".png")

            export_path = os.path.join(export_dir, export_file)

            if is_generated or is_dirty or is_packed:
                os.makedirs(export_dir, exist_ok=True)
                try:
                    image.save_render(export_path, scene=self.scene)
                except Exception:
                    try:
                        orig = image.filepath_raw
                        image.filepath_raw = export_path
                        image.save()
                        image.filepath_raw = orig
                    except Exception:
                        continue
                ref_path = export_file
            else:
                src_path = bpy.path.abspath(image.filepath, library=image.library)
                src_path = os.path.normpath(src_path)
                if not os.path.exists(src_path):
                    continue
                if use_copies:
                    os.makedirs(export_dir, exist_ok=True)
                    if os.path.normcase(os.path.abspath(src_path)) != os.path.normcase(
                        os.path.abspath(export_path)
                    ):
                        shutil.copy2(src_path, export_path)
                    ref_path = export_file
                else:
                    ref_path = src_path

            self.sw.open_element("image", [("id", img_key), ("name", img_key)])
            self.sw.write_element("init_from", _native_path_to_uri(ref_path))
            self.sw.close_element()

        self.sw.close_element()

    def _export_materials(self) -> None:
        mats = list(self._iter_materials_in_export_set())
        if not mats:
            return
        self.sw.open_element("library_materials")
        for ma, _ob in mats:
            mat_id = get_material_id(self.id_map, ma)
            mat_name = encode_xml(ma.name)
            eff_id = get_effect_id(self.id_map, ma)
            self.sw.open_element("material", [("id", mat_id), ("name", mat_name)])
            self.sw.write_element("instance_effect", None, [("url", f"#{eff_id}")])
            self.sw.close_element()
        self.sw.close_element()
