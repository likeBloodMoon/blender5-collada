# blender5-collada Roadmap → 2.0

**Goal for 2.0:** import a BeamNG vehicle or mod exactly the way the game builds it. That means the right parts for the chosen `.pc` config, every part in its in-game position (no more wheels or steering wheels at 0,0,0), correct materials, and a clean collection hierarchy.

**Positioning:** compete on **correctness**, **validation**, and **"installs from Blender's own extension list and works everywhere"**. Raw C++ speed is not where to compete; keep the optional accelerator for speed parity.

Products:
- **blender5-collada** (this repo): free, GPL, listed on extensions.blender.org.
- **Parts Configurator**: a separate paid add-on (~€3 on Ko-fi/Patreon), GPL, kept in a private repo. See the last section.

### How the order is decided
1. **Silent data loss first.** A bug that drops something without a warning beats one that gives a visibly wrong result, which beats a missing feature.
2. **Regressions from Blender 4.5.1 before inherited bugs.** People switching from 4.5.1 expect at least the same result.
3. **Only tiny fixes go in before the code split.** Anything bigger waits until `import_collada.py` and `export_collada.py` are split, so no PR mixes moved code with changed code and nothing has to be fixed twice.
4. **Every bug fix adds its repro script from the issue as a regression test.**

---

## Done so far
- **1.5.0–1.5.3:** BeamNG config import (experimental), archive picker, `common.zip` lookup, Keep Quads, modifier evaluation mode (#19), keep normals through triangulation (#10), `.cdae` scaled-parent fix (#11), UV/color dedupe (#12), #17 triangulation fix, robustness (#1, #2, #3, #27, #28), CI and supply chain (#4, #5, #22 accuracy gate), UX (#7, #8, #9, #16).

---

## Step 0 — Dependabot PRs *(in progress)*
- [x] #29 `actions/download-artifact` 4.3.0 → 8.0.2 and #33 `actions/upload-artifact` 4.6.2 → 7.0.2: merge **together**, since one workflow uploads and another downloads the same artifacts. Run a full release build on the branch before merging.
- [x] #31 `actions/cache` 4.3.0 → 6.1.0
- [x] #32 `softprops/action-gh-release` 2.6.2 → 3.0.4: check on a draft or test tag that release assets and `SHA256SUMS.txt` still upload.
- Keep the SHA pinning from #5 on every bump.

## 1.5.4 — Regression hotfix (small, before the split)
Everything here is a few lines in the current files, and all of it is either a 4.5.1 regression or a freeze. In this order, smallest first:
- [x] #35 Export ignores unsaved Edit Mode changes: `update_from_editmode()` before building the export set (`.dae` and `.cdae`).
- [x] #36 Hidden objects (H / monitor icon) not exported: drop the `visible_get()` filter in `build_export_set()`; excluded collections stay skipped.
- [x] #37 Export quadratic in object count: free temporary meshes with one `bpy.data.batch_remove()` per export (`.dae` and `.cdae`), and build a parent → children map once instead of `get_children_in_scene()` scanning the view layer. Target: 4,000 objects in under 1.5 s with byte-identical output.
- [x] #46 jbeam expressions can hang: cap total `random()` candidates (e.g. 256), use a set for seen values, catch `RecursionError` in `eval_mesh_expr()`; same depth guard in the lenient JSON parser.
- [x] #34 Bone-parented objects dropped on `.dae` import: after armatures are built, walk non-JOINT child nodes under joints and create them with `parent_type = "BONE"`. The largest of the five; if it turns into more than a contained change, ship the other four as 1.5.4 and move #34 to the first PR after the split.
- [ ] Release 1.5.4 with the 4.5.1 parity and accuracy suites green.

## 1.5.5 — Code split (moves only, no behaviour change)
`import_collada.py` (~3,640 lines, one 69-method class) and `export_collada.py` (~2,450 lines) are split into modules before any more fixes land in them. This makes nothing faster; it makes bugs like #27 (the same accessor code in three places) a fix in one place.

**Rules**
- Move code only: no logic changes, renames or fixes in the same PR. One module per PR.
- On every PR, export the parity fixture set before and after and confirm the files are **byte-identical**, and run every suite: parity, deep verification, the accuracy gate (#22), fuzz and archives.
- `import_collada.py` (`DocumentImporter`, `ImportSettings`) and `export_collada.py` (`DocumentExporter`, `ExportSettings`) stay as thin entry points, so callers and tests don't change.
- No circular imports. Modules share state only through the `DocumentImporter` / `DocumentExporter` object (mixins).
- Append each move commit to `.git-blame-ignore-revs`.
- Run the add-on's own round-trip regression tests from 1.5.4 on every PR too.

**`io_scene_collada/importer/`**

| PR | Module | Contents |
| --- | --- | --- |
| 1 | `xml_parse.py` | DTD rejection, safe XML parsing, namespace helpers, strict array parsers, accessor slicing, `<extra>`, URI/path helpers |
| 2 | `archive.py` | Archive indexing, size and entry caps, manifest, `main.materials.json`, archive images |
| 3 | `materials.py` | Images, effects, materials, BeamNG texture fallback, material binding |
| 4 | `mesh.py` | Geometries, `_read_mesh` (sources, primitives, `<ph>` holes, UVs, colours, normals), sharp edges, duplicate-face handling |
| 5 | `rig.py` | Skin and morph controllers, armatures, bone collections, shape keys |
| 6 | `animation.py` | Animation library, F-curves, object and joint animation |
| 7 | `scene.py` | Asset/units/up-axis, cameras, lights, node transforms, collections, node filtering |

**`io_scene_collada/exporter/`**

| PR | Module | Contents |
| --- | --- | --- |
| 8 | `geometry.py` | Float-row dedupe, geometry export, normals, sources and primitives |
| 9 | `materials.py` | Path to URI, materials, effects, images |
| 10 | `rig.py` | Rest pose, skin and morph controllers, bone hierarchy, `<instance_controller>` |
| 11 | `animation.py` | Axis/channel maps, sampling, object and bone animation |
| 12 | `scene.py` | Asset, cameras, lights, visual scene, nodes, bindings, export-set building |

**Done when**
- [ ] Each file is under ~1,000 lines.
- [ ] All suites green and parity export byte-identical on every PR.
- [ ] `docs/architecture.md`: each module, what it holds, and which Blender 4.5.1 C++ source it came from.
- [ ] #14 Broader ruff rules + mypy, now applied per module (`cdae/` first, then the new `importer/` and `exporter/`).

1.5.5 is an internal release: same behaviour as 1.5.4, so it can be tagged once the split is done, or folded into 1.5.6.

## 1.5.6 — Correctness (inherited bugs, fixed in the new modules)
In this order. The module each fix lands in is in brackets.
- [ ] #39 Float and point-domain color attributes not exported `[exporter/geometry]`: the Blender 5 default color type, so it hits the most users. Convert point → corner and float → byte (or write floats); warn when a layer is skipped.
- [ ] #38 Skin bound to the wrong armature when two rigs share bone names `[importer/rig]`: resolve skin SIDs under that controller's `<skeleton>` instead of the global `joint_elem_by_sid`.
- [ ] #42 Duplicate bone IDs after name sanitizing `[exporter/rig]`: make sanitized IDs and SIDs unique, restore original names from `name` on import.
- [ ] #40 + #41 Shape keys `[exporter/rig]`, one PR: evaluate each key separately when modifiers change the vertex count (#40), and write `Basis + (key − relative_key)` for chained keys (#41).
- [ ] #26 Doubled triangles `[importer/mesh]`: the Double-sided faces option (Keep / Merge / Remove back faces), with exact duplicates defined as identical winding, material, UVs and corner normals, as specified in the issue. Consistent between `.dae` and `.cdae` import.
- [ ] #43 Linked duplicates with different modifiers share one mesh `[exporter/geometry]`: only share a geometry when the evaluated meshes are the same.
- [ ] #47 `.cdae` skinned mesh exported in the current pose with no warning `[cdae/exporter]`: warn; export the rest pose by default.
- [ ] #48 Same axis for Forward and Up accepted `[__init__]`: swap the other axis in the UI (like glTF/FBX) and refuse it from Python.
- [ ] #50 `.cdae` as a Collection Exporter `[__init__]`: `bl_export_operator` + a hidden `collection` property.
- [ ] #44 "Include all Actions" does nothing `[exporter/animation]`: export unassigned actions by temporarily assigning them (with a matching slot) and sampling.
- [ ] #45 Geometry Nodes instances export empty `[exporter/geometry]`: realize instances from `depsgraph.object_instances`, or at least warn.
- [ ] **Profiling pass** after the split, so the profile maps onto modules: `cProfile` on a 1M-tri mesh, a 4,000-object scene and a full BeamNG config import. Known suspect: duplicate-face filtering falls back to a per-face Python loop on mixed tri/quad/n-gon meshes. Write the top functions per case into `docs/profiling.md`.
- [ ] #6 Allow-failure CI job against Blender daily builds.

## Alongside 1.6, when there's a gap
- [ ] #15 Add-on settings page (Preferences → Add-ons): BeamNG path, default export options, cache, warnings. The paid Parts Configurator reads the BeamNG path from here, so this lands before the Configurator ships.
- [ ] #20 **BeamNG Check v1**: pre-export linter (missing materials, names, unapplied transforms, origins at 0,0,0, n-gons, thin or non-manifold colliders, LOD suffixes, multi-material deformGroup meshes, degenerate faces, vertex color layers that won't export). Add the two #26 checks: exact duplicate faces and many double-sided faces on one material.
- [ ] #13 `.cdae` import: read Blenux-written files, with warnings.
- [ ] #49 Keep UV map names, shape key slider ranges, relative keys, roughness and `use_deform` through a `.dae` round trip (Blender `<extra>` profile).
- [ ] #21 Troubleshooting docs: "spiky" meshes, VY node errors, parts at the origin.

## Distribution (after 1.5.6, when the add-on has no known regressions)
- [ ] #23 List the pure-Python build on **extensions.blender.org**. Blenux's C++ build was rejected there.
- [ ] #24 Benchmark page: import/export time and file size against Blenux's published numbers, using the 1.5.6 profiling results.

---

## 1.6 — JBeam core + part collections
- JBeam parser: tolerant JSON, `slots` and `slots2`, part database across the vehicle and `common`.
- Resolve the slot tree from the `.pc`: chosen parts, slot defaults, empty slots.
- `$variables` from the `.pc` and `$=` expressions.
- `nodeOffset` / `nodeMove` passed down the slot hierarchy.
- **One collection per part**, nested by slot. Each collection holds that part's flexbodies and props.
- Custom properties on each part collection: slot path, part name, source jbeam, and the source `.pc`. Never rely on collection names, which have a 63-character limit and get `.001` suffixes.
- **Public API for add-ons**: stable operators (import config, import or remove a single part) + collection custom properties + `API_VERSION`.
- JBeam node debug overlay, GPU-drawn and toggleable, to check how meshes line up with the nodes.

## 1.7 — Flexbody placement
- `pos` / `rot` / `scale`. Rotation is intrinsic Euler **Z → X → Y**.
- Mirrored X `nodeOffset`, using the sign of `pos.x`. Flexbodies without `pos` get no mirroring, the same as in the game. Honour `ignoreNodeOffset`.
- Meshes used more than once (left and right wheels) become **linked duplicates**. Depends on #43 being fixed so they export correctly.
- `materialOverride`.
- *Fixes wheels, tires, hubs, brakes and most body parts.*

## 1.8 — Props + materials in
- Prop frame from `idRef` / `idX` / `idY`, plus `baseRotation`, `baseTranslation` (fraction or metres), `baseTranslationGlobal` and `baseRotationGlobal`. Props are placed in their rest pose.
- *Fixes steering wheels, gauges, needles, pedals and shifters.*
- Light props are skipped, or optionally created as Blender lights.
- **v1.5 PBR material import**: `Stages[0..3]` become a node group with baseColor, roughness, metallic, normal, AO, clearCoat, opacity, emissive and the color palette. `.pc` paint colors are applied.

## 1.9 — Report, validation, materials out (release candidate)
- **Vehicle report**: missing and unused meshes, unresolved node groups, flexbodies likely to give "VY node not found", duplicate node positions, parts still at the origin that the jbeam places elsewhere.
- **BeamNG Check v2**: adds the jbeam-aware checks above, plus mesh density vs node spacing (the cause of spikes).
- **v1.5 material JSON export**: one merged `*.materials.json` keyed by `mapTo`, with `.color/.data/.normal` texture naming. Round-trips with 1.8 import.
- **Roles-style static-prop export**: base00/start01, `Colmesh-1`, LOD suffixes, `nulldetail`, `bb_autobillboard`. Optional decimate LODs and convex-hull collision with the 0.3 m thickness check. Needs childless empties (attach points) kept in `.cdae` export.

## 2.0 — "Loads like the game"
- One-click vehicle import from a folder, zip or `.pc`. The **Experimental** label is dropped.
- Regression scan over **all vanilla vehicles** (`scan_beamng_install.py`): target ≥ 95% of parts placed with no warnings. CI still uses only self-written jbeam fixtures, never game files.
- Top and side views checked against in-game screenshots for a set of reference cars.
- Docs, a demo video (vanilla car imported looking exactly like in-game, materials included), and a refreshed benchmark page.

---

## Parts Configurator (paid add-on, separate private repo)
Requires blender5-collada ≥ 1.6. Talks to it **only through the public operators and collection properties**, never by importing its modules, so the 1.5.5 split doesn't affect it. Checks `API_VERSION` when it registers and shows "Install blender5-collada first" if the core is missing. Reads the BeamNG path from the #15 settings page.

**v1.0** (alongside core 1.6/1.7)
- Sidebar panel styled like BeamNG's Parts Selector: the slot tree with one dropdown per slot, plus search.
- **Gated behind the `.pc` selector**: only available for collections imported from a real config, disabled for "(all parts, no config)".
- A `.pc` fills in every slot. Changing anything shows **"<config> (modified)"**.
- **Apply** imports only what changed: it swaps one part's collection and its children, never the whole car. It's a single undoable operator.
- **Save `.pc`**: writes the current selection out as a config.
- Dropdown options cached at module level, or shown in a UIList, to avoid Blender's dynamic-enum crash.

**v1.1+**
- Tuning sliders for jbeam `$variables`, like the game's Tuning menu.
- Paint presets from `.pc` files.
- Compare two configs (parts added, removed or changed).

**Selling**
- Ko-fi Shop and/or a Patreon tier. Post a changelog with every zip.
- GPL `LICENSE` included in the zip.
- The free add-on stays fully functional, with only a link in the README and no prompts inside Blender.

---

## After 2.0
- Skinned and LOD `.cdae` export; `.cdae` v15.
- deformGroup / breakGroup helpers: tag meshes, preview the damage material.
- "Rebuild quads" on import (tris → quads, keeping exact normals).
- Flexbody deformation preview.
- Export curves, text and NURBS surfaces as meshes (4.5.1 skipped them too; glTF converts them).

## Deliberately not planned
- Full JBeam beam/triangle editing. BeamNG's official JBeam Editor owns that; just read the same files.
- Vehicle animations and bones (BeamNG vehicles don't use them).

## Watch list
- Blenux io_beamng_dae is at 2.1.0 with a materials pipeline, and has teased a vehicle-config browser and a built-in jbeam editor. Re-check the forum thread before each release.
- Grille's Blender_BeamNG_CDAE has announced DAE import.
- Still to test in-game: whether BeamNG accepts Blenux's `.cdae` quirks (`nil` blocks, extra material fields, scale in `nodeAlignedScales`), and whether `.dae` and `.cdae` exported by 1.5.3+ load correctly.

---

## Open issue map
Every open issue, and where it lands.

| Issue | Title (short) | Milestone |
| --- | --- | --- |
| #29, #31, #32, #33 | Dependabot Actions bumps (PRs) | Step 0 |
| #35 | Edit Mode changes not exported | 1.5.4 |
| #36 | Hidden objects not exported | 1.5.4 |
| #37 | Export quadratic in object count | 1.5.4 |
| #46 | jbeam expressions can hang | 1.5.4 |
| #34 | Bone-parented objects dropped on import | 1.5.4 (or first after split) |
| #14 | ruff + mypy | 1.5.5 |
| #39 | Float/point colors not exported | 1.5.6 |
| #38 | Two rigs with same bone names | 1.5.6 |
| #42 | Duplicate bone IDs | 1.5.6 |
| #40, #41 | Shape keys (modifiers, chained keys) | 1.5.6 |
| #26 | Doubled triangles option | 1.5.6 |
| #43 | Linked duplicates with different modifiers | 1.5.6 |
| #47 | `.cdae` skinned pose warning | 1.5.6 |
| #48 | Same axis Forward/Up | 1.5.6 |
| #50 | `.cdae` Collection Exporter | 1.5.6 |
| #44 | Include all Actions | 1.5.6 |
| #45 | Geometry Nodes instances | 1.5.6 |
| #6 | CI on Blender daily builds | 1.5.6 |
| #15 | Settings page | Alongside 1.6 |
| #20 | BeamNG Check v1 | Alongside 1.6 |
| #13 | Read Blenux `.cdae` | Alongside 1.6 |
| #49 | Keep UV names, slider ranges, roughness | Alongside 1.6 |
| #21 | Troubleshooting docs | Alongside 1.6 |
| #23 | extensions.blender.org listing | Distribution |
| #24 | Benchmark page | Distribution |
