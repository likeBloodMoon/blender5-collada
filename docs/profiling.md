# Profiling Pass (v1.5.7)

Measured with Python `cProfile` (sorted by cumulative time) on **Blender 5.3.0** (Windows x64, pure-Python build without native `fastfmt`).

---

## 1. 1M-Triangle Mesh (`708 × 708` Grid → 1,000,000 triangles, 64.3 MB `.dae`)

### Export (`4.18 s`)

| Cum time | Self time | Calls | Module & function |
|---:|---:|---:|---|
| 4.108 s | 0.003 s | 1 | `exporter/geometry.py:_export_geometry_object` |
| 4.104 s | 0.226 s | 1 | `exporter/geometry.py:_write_mesh_xml` |
| 2.567 s | 1.612 s | 36 | `str.join` (XML array assembly) |
| 1.538 s | 0.007 s | 3 | `xml_writer.py:fmt_f32_array` (`_fmt_f32_sanitized`: 0.955 s over 2.51M calls) |
| 1.177 s | 0.002 s | 1 | `exporter/geometry.py:_write_primitive_list` |
| 1.165 s | 0.000 s | 1 | `xml_writer.py:fmt_int_array` |
| 0.753 s | 0.753 s | 7 | Blender `bpy_prop_collection.foreach_get` |
| 0.319 s | 0.057 s | 2 | `exporter/geometry.py:_dedup_float_rows` (`np.unique`: 0.229 s) |

**Takeaway:** Pure-Python float/int formatting (`2.70 s`, 65% of total) is the primary bottleneck on dense meshes — which is why the optional C++ `fastfmt` accelerator targets `fmt_f32_array` / `fmt_int_array`. Outside formatting, `foreach_get` (`0.75 s`) and `_dedup_float_rows` (`0.32 s`) dominate.

### Import (`4.73 s`)

| Cum time | Self time | Calls | Module & function |
|---:|---:|---:|---|
| 4.539 s | 0.003 s | 1 | `importer/mesh.py:_read_library_geometries` |
| 4.536 s | 1.220 s | 1 | `importer/mesh.py:_read_mesh` |
| 1.285 s | 1.285 s | 4 | `numpy.fromstring` |
| 1.024 s | 0.066 s | 1 | `importer/xml_parse.py:_parse_i32_np` (`_fromstring_i32_strict`: 0.958 s) |
| 0.852 s | 0.852 s | 6 | Blender `bpy_prop_collection.foreach_set` |
| 0.769 s | 0.024 s | 3 | `importer/xml_parse.py:_parse_f32_np` (`_fromstring_f32_strict`: 0.745 s) |
| 0.419 s | 0.419 s | 122 | `str.split` |

**Takeaway:** Triangle/quad-only meshes hit the vectorized NumPy path (`np.sort` + `np.unique`), so duplicate-face filtering is fast. Mixed tri/quad/n-gon meshes still fall back to a per-face Python loop in `collada_utils.py:resolve_duplicate_faces()`, which is a good future optimization target if mixed-polygon dense meshes become common.

---

## 2. 4,000-Object Scene (4,000 instanced cubes, 0.81 MB `.dae`)

### Export (`0.59 s` total; `0.40 s` inside `export_current_scene`)

| Cum time | Self time | Calls | Module & function |
|---:|---:|---:|---|
| 0.404 s | 0.002 s | 1 | `export_collada.py:export_current_scene` |
| 0.335 s | 0.008 s | 1 | `exporter/scene.py:_export_visual_scene` |
| 0.324 s | 0.035 s | 4,000 | `exporter/scene.py:_write_node` |
| 0.193 s | 0.012 s | 4,000 | `exporter/scene.py:_write_node_transform_ob` |
| 0.118 s | 0.004 s | 4,000 | `xml_writer.py:append_values` |
| 0.077 s | 0.058 s | 64,001 | `xml_writer.py:fmt_float` |
| 0.050 s | 0.022 s | 12,043 | `xml_writer.py:open_element` |
| 0.025 s | 0.001 s | 1 | `exporter/animation.py:_export_animations` |
| 0.022 s | 0.015 s | 8,002 | `collada_utils.py:translate_id` |

**Takeaway:** Scaling is linear (`O(N)`) after #37, well under the 1.5 s target.

### Import (`0.72 s`)

| Cum time | Self time | Calls | Module & function |
|---:|---:|---:|---|
| 0.696 s | 0.188 s | 1 | `import_collada.py:_import_dae_tree` |
| 0.498 s | 0.044 s | 4,000 | `importer/scene.py:_process_node` |
| 0.318 s | 0.318 s | 4,000 | `importer/scene.py:_add_object_to_scene` |
| 0.065 s | 0.029 s | 24,017 | `importer/xml_parse.py:_find_children` |
| 0.055 s | 0.009 s | 4,000 | `importer/scene.py:_read_node_matrix` |
| 0.039 s | 0.021 s | 56,102 | `importer/xml_parse.py:_strip_ns` |
| 0.024 s | 0.000 s | 1 | `importer/xml_parse.py:_parse_xml_safe` |

**Takeaway:** Import is dominated by Blender object creation/linking (`_add_object_to_scene`, 44%).

---

## 3. Full BeamNG Vehicle Config Import (`covet.zip` `DXi_M`, Full PBR — 486 meshes, 69 materials, `13.50 s`)

| Cum time | Self time | Calls | Module & function |
|---:|---:|---:|---|
| 13.494 s | 0.001 s | 1 | `beamng/picker.py:execute_beamng_import` |
| 8.079 s | 0.002 s | 1,361 | `beamng/lenient_json.py:parse` |
| 8.075 s | 1.662 s | 1.28M/1,361 | `beamng/lenient_json.py:parse_value` |
| 8.072 s | 0.843 s | 201k/1,361 | `beamng/lenient_json.py:_parse_object` |
| 7.904 s | 0.000 s | 2 | `beamng/source.py:load_vehicle_source` (`covet.zip` + `common.zip`) |
| 7.523 s | 0.007 s | 1,037 | `beamng/parts.py:add_jbeam_text` |
| 4.599 s | 0.007 s | 3 | `beamng/picker.py:_import_single_model_from_source` |
| 4.360 s | 0.055 s | 3 | `import_collada.py:_import_dae_tree` |
| 2.951 s | 0.674 s | 486 | `importer/mesh.py:_read_mesh` |
| 2.148 s | 1.802 s | 972,601 | `beamng/lenient_json.py:_parse_string` |

**Takeaway:** **60% (`8.08 s`)** of a full BeamNG config import is spent in `beamng/lenient_json.py` parsing all 1,037 `.jbeam` files across the vehicle zip and `common.zip` (character-by-character Python scanner). Pre-stripping comments/trailing commas with a compiled regex and delegating clean JSON to CPython's C `json.loads()` (with `lenient_json.py` as fallback for BeamNG's unquoted/comma-less edge cases) is the single highest-leverage speedup for 1.6.
