# blender5-collada Roadmap → 2.0

**Goal for 2.0:** import a BeamNG vehicle or mod exactly the way the game builds it. That means the right parts for the chosen `.pc` config, every part in its in-game position (no more wheels or steering wheels at 0,0,0), correct materials, and a clean collection hierarchy.

**Positioning:** compete on **correctness**, **validation**, and **"installs from Blender's own extension list and works everywhere"**. Raw C++ speed is not where to compete; keep the optional accelerator for speed parity.

Products:
- **blender5-collada** (this repo): free, GPL, listed on extensions.blender.org.
- **Parts Configurator**: a separate paid add-on (~€3 on Ko-fi/Patreon), GPL, kept in a private repo. See the last section.

---

## 1.5 — BeamNG config import (experimental) · *done*
- [x] Archive picker for vehicle zips/folders.
- [x] Detect vehicle `.pc` configs and import one config instead of every part variant.
- [x] `common.zip` part lookup.
- [x] Ship with an **Experimental** label.

## 1.5.x — Fix and stabilise (before any new big feature)
**Accuracy (from round-trip tests against Blenux io_beamng_dae 2.1.0)**
- [x] #10 Keep normals through triangulation: custom normals, non-planar quads, sharp edges *(biggest visible quality issue)*
- [x] #11 `.cdae` export: children of scaled parents misplaced
- [ ] #12 `.dae` export: deduplicate UV/color sources (~40% smaller files)
- [ ] #13 `.cdae` import: read Blenux-written files, with warnings

**Robustness**
- [x] #1 Malformed `.dae` → clean error, not a traceback
- [ ] #2 msgpack unhashable keys + NumPy warning control flow
- [ ] #3 Archive hardening: entry-count cap, reject DTDs

**New, small**
- [ ] **Keep Quads** export option: triangulate n-gons only, keep quads; greyed out for `.cdae`.
- [ ] Modifier evaluation mode on export (none / viewport / render).
- [ ] **BeamNG Check v1**: a pre-export linter and panel button. It flags:
  - missing materials
  - names that aren't snake_case, or duplicate names
  - unapplied transforms on flexbodies
  - props with the origin at 0,0,0
  - n-gons
  - colliders thinner than 0.3 m and non-manifold colliders
  - bad LOD suffixes
  - multi-material deformGroup meshes
  - degenerate faces
- [ ] Troubleshooting docs: "spiky" meshes, VY node errors, parts at the origin.

**Release and CI (one batch before release)**
- [ ] #4 Smoke-test native binaries on Windows and macOS
- [ ] #5 Pin Actions to SHAs, verify Blender downloads, add `SHA256SUMS.txt`
- [ ] #6 Allow-failure CI job against Blender daily builds
- [ ] #14 Broader ruff rules + mypy on `cdae/`
- [ ] #7 Shared warning-cap helper
- [ ] #8 Warn when importing into an excluded or hidden collection
- [ ] #9 Make "restart after update" visible in Preferences
- [ ] Add the accuracy test kit to CI. Gate: custom-normal, non-planar and hierarchy cases < 0.1° / < 0.1 mm.

**Distribution**
- [ ] List the pure-Python build on **extensions.blender.org**. Blenux's C++ build was rejected there.
- [ ] Benchmark page: import/export time and file size against Blenux's published pickup numbers.

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
- Meshes used more than once (left and right wheels) become **linked duplicates**.
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
- **Roles-style static-prop export**: base00/start01, `Colmesh-1`, LOD suffixes, `nulldetail`, `bb_autobillboard`. Optional decimate LODs and convex-hull collision with the 0.3 m thickness check.

## 2.0 — "Loads like the game"
- One-click vehicle import from a folder, zip or `.pc`. The **Experimental** label is dropped.
- Regression scan over **all vanilla vehicles** (`scan_beamng_install.py`): target ≥ 95% of parts placed with no warnings. CI still uses only self-written jbeam fixtures, never game files.
- Top and side views checked against in-game screenshots for a set of reference cars.
- Docs, a demo video (vanilla car imported looking exactly like in-game, materials included), and a refreshed benchmark page.

---

## Parts Configurator (paid add-on, separate private repo)
Requires blender5-collada ≥ 1.6. Talks to it **only through the public operators and collection properties**, never by importing its modules. Checks `API_VERSION` when it registers and shows "Install blender5-collada first" if the core is missing.

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

## Deliberately not planned
- Full JBeam beam/triangle editing. BeamNG's official JBeam Editor owns that; just read the same files.
- Vehicle animations and bones (BeamNG vehicles don't use them).

## Watch list
- Blenux io_beamng_dae is at 2.1.0 with a materials pipeline, and has teased a vehicle-config browser and a built-in jbeam editor. Re-check the forum thread before each release.
- Grille's Blender_BeamNG_CDAE has announced DAE import.
- Still to test in-game: whether BeamNG accepts Blenux's `.cdae` quirks (`nil` blocks, extra material fields, scale in `nodeAlignedScales`).
