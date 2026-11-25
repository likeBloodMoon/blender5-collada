# Spec: BeamNG archive picker and vehicle-config import (target v1.5.0)

Repo: `likeBloodMoon/blender5-collada` (baseline: v1.4.4, commit `7d3e59f`)
Author of this spec: Claude, for Kristóf. Implementer: coding agent working in `C:\Users\krist\3d`.

## 0. Hard rules for the implementer

1. **The BeamNG install is read-only.** `C:\Steam\steamapps\common\BeamNG.drive` may be opened and read (zip files opened with `zipfile` in read mode), never written to, extracted into, renamed or "fixed". Anything you produce goes under `tests\_local_beamng\` in the repo (add it to `.gitignore` before the first run).
2. **No game content in git.** Do not commit any `.dae`, `.cdae`, `.jbeam`, `.pc`, texture, or report that lists BeamNG mesh/part names. Synthetic fixtures only in CI, same rule as `tests/_local_cdae/` today.
3. **Do not touch the BeamNG user/mods folder** (under `%LOCALAPPDATA%\BeamNG.drive\`) except to read it if Kristóf asks for mod testing.
4. **Every number in section 2 is a baseline you must reproduce first** (section 7, step 1). If you get different numbers, stop and report why before writing feature code: the game may have updated, or your parser differs.
5. Report back using the template in section 8. Mark anything you could not verify as unverified; do not guess.

---

## 1. Problem

Two things go wrong when importing BeamNG content today (v1.4.4):

- **`common.zip`** holds 183 separate `.dae` models (67 wheels, 21 loads, 14 tyres, 14 at root, 8 pickup, 6 lights, …), all authored at the origin. "All models (collections)" imports all of them stacked on top of each other.
- **A car's `.dae` contains every variant of every part.** `covet.dae` has 449 mesh nodes (7 hood-related: `covet_hood`, `covet_hood_cut`, `covet_hood_sport`, …). Importing it gives every hood, bumper, etc. variant overlapping.

Goal: let the user **choose what to import from an archive**, and **import a car as one real configuration** (`.pc` file), with every `.jbeam` and `.pc` detected automatically.

---

## 2. Facts measured on Kristóf's game folder (24 Nov 2026)

Measured with the reference scripts in `beamng_reference/` (section 9) over `C:\Steam\steamapps\common\BeamNG.drive\content\vehicles\*.zip`:

| What | Count |
|---|---|
| Vehicle zips | 126 |
| `.jbeam` files | 5,294 — **all parse** with the lenient parser (0 failures) |
| Parts defined | 21,336 (no duplicate part names inside a zip) |
| `.pc` configs | 1,767 — all parse; 1,760 have `"format": 2`, **7 are legacy** (no `format`, no `parts` key: the whole file *is* the slot map, plus a `vars` key) |
| `.dae` / `.cdae` files | 373 / 373 |
| Slot rows | 23,049 in the newer `slots2` table, 2,750 in the old `slots` table; no part has both |
| `slotType` | a string for 21,194 parts, **a list** for 142 parts |
| Parts with a duplicated slot name in their own slot table | 16 (e.g. `pickup_frame_upfit_heavy` lists `linelock` twice) |
| `.pc` part entries keyed by **slot path** (`/a/b/c/`) instead of slot name | 2,044 entries; 2,038 match a real path when walking the tree; 6 are stale (4 `miramar` configs with `…/miramar_licenseplate_R_ute/`, 2 `simple_traffic` lansdale configs with `…/simple_traffic_lansdale_licenseplate_R/`) |
| `.pc` files with `mainPartName` | 1,062 (some also `mainPartPath`) |
| Vehicle `info.json` with `default_pc` | 88 of 125 (the Covet has none) |
| `info_<config>.json` `Configuration` | a translation key such as `vehiclesData.covet.13s_M.Configuration`, not readable text |
| Configs whose main part was found | **1,767 / 1,767** |
| Configs naming a part that exists nowhere | 72 (stale data in the game itself, e.g. `etki_doordetent_FL` in 31 configs). Value `"none"` appears as a choice in 140 configs and means *empty slot*; no part is called `none` |
| Mesh references over all resolved configs | 194,757: **85.8 %** in the car's own zip (167,017), **14.0 %** in `common.zip` (27,306), 47 in another car's zip (`etk800` ↔ `etkc` engine-bay meshes, `tub` → `autobello`), **387 (0.2 %) exist nowhere** (41 distinct names, stale game data, e.g. `md_series_steeringbox_cabover`) |
| Props with mesh `SPOTLIGHT` | 145 — a light, not a mesh. Ignore |
| Parts whose flexbody/prop mesh is a **Lua expression** (`$=…`) | 108 parts; **400 of 1,767 configs** contain at least one |
| Flexbody rows (non-common) with non-identity `pos`/`rot`/`scale` | 14,770 of 37,245 (40 %); 782 more use expressions there. `common.zip`: 5,772 moved, 407 expressions, 770 identity |
| `.cdae` object names vs `.dae` node names (12 files from 5 cars) | 1,449 / 1,451 identical (`pickup_toolbox_lB/rB` exist only in the `.cdae`) |
| Flexbody/prop mesh names matched by `.dae` **node `name`** vs `instance_geometry name` | of 19,958 distinct names: node name matches 19,907, instance name 19,795; **112 match only by node name**, 0 only by instance name, 51 match neither (stale) |

Expression shapes seen (string literals replaced by `'S'`): `$='S'..(round(random(4)))` (80×), `$= $components.hasDiffuserCutout ~= true and 'S' or 'S'` (19×), `$= 'S' .. random(1,16)` (11×), `$=$components.bodyType.hatch == true and 'S' or 'S'` (10×), `$=$components.driveshaftMeshA` (6×), `$= $scale < 1.5 and 'S' or 'S'` (8×).

Example: Covet `13s_M.pc` resolves to **109 parts and 145 meshes** (136 in `covet.zip`, 9 in `common.zip`) out of 486 node names across the three Covet `.dae` files, with exactly one hood mesh (`covet_hood`). Without `common.zip` loaded, resolution is incomplete because many slots hang below common parts (seats, lights, brakes, wheels). **`common.zip` is required for resolution even when its meshes are not imported.**

---

## 3. User experience

### 3.1 Flow
1. **File → Import → Collada** (and drag-and-drop) accept `.zip` as well as `.dae/.zae/.kmz`. Fix the menu label to "Collada (.dae, .zae, .kmz, .zip)" and add `.zip` to `IO_FH_collada.bl_file_extensions`. The `.cdae` file handler gets the same picker for `.zip` (see 3.4).
2. If the archive has **more than one `.dae` or any `.pc` file** and the operator was run interactively, open the **picker dialog** (second step, `invoke_props_dialog`). Otherwise import directly (unchanged behaviour).
3. Scripted calls never pop a dialog: every picker choice is also an operator property (section 6.3).

### 3.2 Picker dialog
```
┌ Import from covet.zip ─────────────────────────────────────────┐
│ Vehicle: covet                    Config: [ 13s_M                ▼]
│ Parts:   (•) Only this config  ( ) Config + other parts hidden  ( ) Everything
│ [x] Use parts from common.zip   (found: …\vehicles\common.zip)
│ ─ Models in archive ───────────────────────────── [filter____] [All][None]
│ [x] covet.dae                          50.2 MB   (used by config)
│ [ ] 3wheel/covet_3wheel.dae             …       (not used by config)
│ [ ] trailer/covet_trailer.dae           …
│ [ ] Spread selected models out side by side
│                                   [ Import ]  [ Cancel ]
└────────────────────────────────────────────────────────────────┘
```
- **Config list:** "(all parts, no config)" + every `.pc` belonging to the detected vehicle, labelled with the `.pc` file name (e.g. `13s_M`). The `Configuration` field in `info_<config>.json` is a translation key (`vehiclesData.covet.13s_M.Configuration`), so it cannot be shown as-is. The tooltip can show `Config Type` from that file (e.g. `Factory`). Default selection: `info.json` `"default_pc"` when present (88 of 125 vehicles), otherwise the first `.pc` alphabetically.
- **Parts mode:**
  - *Only this config*: import only meshes used by the resolved config.
  - *Config + other parts hidden*: import everything, but meshes not in the config go into excluded collections grouped by slot (`Alternatives / <slot description>`). Meshes not referenced by any part go into `Unreferenced`.
  - *Everything*: today's behaviour.
- **Models list:** every `.dae` with size, grouped by folder (`wheels/`, `loads/`, …) for big archives like `common.zip`. When a config is chosen, models it uses are pre-ticked and labelled; untouched models are unticked. Filter box matches path substrings.
- **Spread out:** place each imported model's root objects on a grid (bounding-box width + 0.5 m gap) instead of all at the origin. Off by default; useful for browsing `common.zip`.
- Remember the last choices per archive name in the scene (`bpy.types.Scene` property) so re-importing is one click.

### 3.3 Result in the scene
```
Collection (active)
└─ covet  [13s_M]
   ├─ covet            ← meshes from covet.dae used by the config
   ├─ common           ← meshes taken from common.zip (see 5.6 about placement)
   └─ Alternatives (excluded, only in "Config + other parts hidden")
      ├─ Hood          covet_hood_cut, covet_hood_sport, …
      └─ …
```
Every imported mesh object gets custom properties: `beamng_part`, `beamng_slot_path`, `beamng_config`, `beamng_source` (`zip:path/in/zip.dae`). Later tools (vehicle report, part switching) build on these.

### 3.4 `.cdae`
Same picker and filtering for `.cdae` files inside a zip and for a loose `.cdae` next to its jbeam files. `.cdae` object names equal `.dae` node names (section 2), so the same name filter applies.

---

## 4. Detection (automatic, no user setup)

### 4.1 Archive or folder scan (`VehicleSource`)
Input is either a `.zip` or a loose `.dae/.cdae` on disk. Build one index:
- **Inside a zip:** list all entries once (`infolist()`), normalise paths with the existing `_norm_archive_path`, case-insensitive lookup. Never extract to disk.
- **Loose file:** treat `<dae dir>` as the vehicle folder; walk up until a folder named `vehicles` is found (max 4 levels) to locate siblings and `common`.
- **Vehicle folders:** every `vehicles/<model>/` directory that contains at least one `.jbeam`. A zip can contain more than one (`simple_traffic.zip` has 254 configs and 21 `.dae`; `eu_trailers.zip` has 4 `.dae`). Show a Vehicle dropdown when there is more than one.
- **`common.zip`:** look for `common.zip` in the same directory as the archive (game layout: `content\vehicles\common.zip`). For a loose folder, look for `vehicles/common/` beside it or `common.zip` in the parent. If missing, warn once ("Parts from common.zip are unavailable: configs will be incomplete") and continue.
- **Cache** the parsed index per (path, size, mtime) in memory for the Blender session. `common.zip` has 880 jbeam files, 4,173 parts and 183 `.dae` (728 MB). Indexing it (jbeam + `.dae` node names) took about 6.5 s in the survey, so do it once.

### 4.2 Parsing
- Use **one** lenient parser for `.jbeam`, `.pc`, `info*.json` and `main.materials.json`. Merge it with the v1.4.4 lenient JSON in `beamng_materials.py`; do not keep two. Reference: `beamng_reference/jbeam_lenient.py` (tokenizer + recursive descent, commas optional everywhere, `//`, `/* */` and `#` comments, `1.` / `.5` / `+1`, bare `true/false/null/FLT_MAX/-FLT_MAX/nan/inf`, unquoted keys, BOM, last duplicate key wins, errors carry line and column).
- Do not use regex comma insertion. It failed on 13 of the Covet's 157 jbeam files during exploration; the tokenizer parses all 5,294.
- Read files as UTF-8 with `errors="replace"`.

### 4.3 Part index
For every part (top-level dict entry in a jbeam file) store: `name`, `file`, `slotType` (normalise to a list), `information.name`, `slots` (normalised from `slots` **or** `slots2`), `meshes` (flexbodies + props), each mesh with its effective modifiers. Pool order: `common` parts first, then the vehicle's parts overwrite them (vehicle wins on name clash).

Table parsing rules (both `slots`, `slots2`, `flexbodies`, `props`):
- Row 0 is the header (list of column names). Following **list** rows are data, **dict** rows are modifiers that apply to all following rows until changed. A dict as the **last element of a data row** applies only to that row.
- `slots` header: `["type","default","description"]` → slot name = `type`.
- `slots2` header: `["name","allowTypes","denyTypes","default","description"]` → slot name = `name`. Use header indices, never fixed positions.
- `flexbodies`: mesh column = header index of `"mesh"` (it was column 0 in all 5,103 flexbody tables sampled from 5 zips, but read the header). `props`: mesh column = header index of `"mesh"`.
- Skip mesh `SPOTLIGHT`. Treat strings starting with `$` as expressions (4.6).
- **Duplicate slot names inside one part:** keep the first, log the rest. *Unverified which one BeamNG keeps; check in game if possible (8. Open questions).*

### 4.4 Choosing the main part
1. If the `.pc` has `mainPartName` and it exists in the pool, use it.
2. Else the part(s) with `"main"` in `slotType`, whose file is under `vehicles/<model>/`. If several, prefer the one named `<model>`, else the alphabetically first. (On the current game every zip has at most one; checked.)

### 4.5 Resolving a config
```
walk(part, path="/"):
    add part; add its meshes (with modifiers)
    for each slot in part.slots (dedup by name, first wins):
        spath = path + slot.name + "/"
        if spath in pc.byPath:   choice = pc.byPath[spath]
        elif slot.name in pc.byName: choice = pc.byName[slot.name]
        else: choice = slot.default
        if choice in ("", "none", None): continue
        if choice not in pool: record missing; try slot.default instead (see note)
        walk(pool[choice], spath)
```
- `pc.byPath` = entries whose key starts with `/`; `pc.byName` = the rest. Legacy `.pc` (no `parts` key): the whole file minus `vars` is `byName`.
- Guard against revisiting the same `(part, path)`. After dedup there were 0 revisits over all configs, but keep the guard.
- **Missing chosen part:** fall back to the slot default and list it in the import report. *Unverified: BeamNG may leave the slot empty instead.* Make this a single, clearly named function so it is easy to change once verified.
- Output `ResolvedConfig`: parts (with slot path and slot description), meshes (name, source part, slot path, modifiers, expression flag), missing parts, missing meshes, stale path keys, warnings.

### 4.6 Lua expressions in mesh names (400 configs)
Do **not** execute Lua or Python `eval`. Implement a small safe evaluator for the observed grammar:
- String literals `'…'`/`"…"`, concatenation `..`, `round(random(n))` / `random(a,b)`, comparisons on `$components.<path>`, `$<var>`, `== ~= < > <= >=`, `true/false/nil`, `and` / `or`, parentheses.
- `$components` values come from the resolved parts' `components` dicts (e.g. `ardente_spoiler_race` has `{"spoiler_R": true, "spoiler_R_liftCoef": 70}`), merged in walk order. *Merge order vs BeamNG is unverified.* `$<var>` values come from `.pc` `vars`, else the `default` column of the parts' `variables` tables (header `name, type, unit, category, default, min, max, …`).
- `random(...)`: do not pick at random. Expand to **all candidates** that exist in the mesh index (for example, `'log_0'..(round(random(9)))` → every existing `log_0N`). Import the first candidate visibly and the others into `Alternatives`.
- If evaluation fails, collect every string literal that is an existing mesh name as candidates, import them like `random`, and warn with the expression text.
- Same evaluator for expression values in `pos`/`rot`/`scale` (1,189 rows). On failure use identity and warn.

### 4.7 Mesh lookup
- Match flexbody/prop mesh names against `.dae` **node `name` attribute** (fall back to node `id` when `name` is missing). Parse node attributes in any order: `bollard.dae` writes `name` before `id`, and a regex expecting `id` first misses it.
- Search order: the vehicle's own `.dae` files → `common.zip` `.dae` files → other vehicle zips in the same `vehicles` folder (only when that zip is already indexed or the user opts in; 47 references total).
- A config usually needs several `.dae` files (own + common). The importer must be able to import a **subset of nodes from several `.dae` files** in one operation.

---

## 5. Importer changes

### 5.1 Node filter
Add `ImportSettings.node_filter: set[str] | None`. When set:
- Instantiate a `<node>` if its name is in the set **or** any descendant is (keep ancestors so parent transforms stay correct). Prune all other subtrees.
- Build geometry only for `<geometry>` ids referenced by kept `instance_geometry`/`instance_controller`. This matters for speed: Covet `13s_M` keeps about 136 of 449 nodes.
- Materials and images: create only those referenced by kept geometry.

### 5.2 Repeated meshes
A mesh name can appear in several flexbody rows (e.g. `atv_4x4_tire` four times with `pos.x = ±0.48`). Phase 1: import it once. Phase 2 (5.6): linked duplicates, one per row.

### 5.3 Collections and properties
As in 3.3. Use the active collection as parent (v1.4.2 behaviour).

### 5.4 Report
After import, one INFO line ("Covet 13s_M: 109 parts, 145 meshes (136 covet.zip, 9 common.zip), 0 missing") and WARNINGs capped at 5 + "and N more" (existing pattern). Full report as a text datablock `BeamNG import – <vehicle> <config>` in the .blend.

### 5.5 Performance budget
- Index of a car zip + `common.zip`: under 10 s cold, under 0.5 s cached (survey on Kristóf's PC: `common.zip` ≈ 6.5 s including `.dae` node names; car zips 0.0–2.7 s).
- Import of one config must not be slower than importing the whole `.dae` in v1.4.4.

### 5.6 Placement of moved and common meshes (Phase 2, not in v1.5.0 unless verified)
40 % of vehicle flexbody rows and most `common.zip` rows carry `pos`/`rot`/`scale`, and wheel and tyre meshes are positioned by wheel nodes at runtime. Exact placement needs semantics that are not verified yet (rotation order, degrees vs radians, pivot, how wheel/hub nodes position common meshes).
- **Phase 1:** import meshes as authored. Put meshes whose effective modifiers are non-identity, and all `common.zip` meshes, in a child collection named `Needs placement`, visible but clearly labelled, and set `beamng_modifiers` (JSON) on each object.
- **Phase 2:** experiment and verify against the game (8. Open questions) before shipping.

---

## 6. Code layout and API

### 6.1 Modules
```
io_scene_collada/beamng/__init__.py
io_scene_collada/beamng/lenient_json.py   # single parser (replaces the one in beamng_materials.py)
io_scene_collada/beamng/source.py         # VehicleSource: zip/folder index, common.zip discovery, cache
io_scene_collada/beamng/parts.py          # table parsing, PartIndex
io_scene_collada/beamng/resolve.py        # main part, walk, ResolvedConfig
io_scene_collada/beamng/expr.py           # safe expression evaluator
io_scene_collada/beamng/picker.py         # dialog operator, UIList, enum caching
```
Keep `beamng_materials.py` but import the parser from `lenient_json.py`. Everything in `beamng/` except `picker.py` must be importable **without `bpy`**, so it can be unit-tested with plain Python and run in the survey.

### 6.2 Blender gotchas
- Dynamic `EnumProperty` items: keep the returned list in a module-level variable. Blender does not hold references, and the labels show garbage otherwise.
- The picker must be a **separate** operator invoked from `execute()` of `wm.collada_import` with `bpy.ops.wm.collada_beamng_picker('INVOKE_DEFAULT', filepath=…)`. The file-browser sidebar cannot rebuild a list per selected file.
- `CollectionProperty` items for the model list live on the picker operator; fill them in `invoke()`, never in `draw()`.
- Drag-and-drop goes through `IO_FH_collada.poll_drop` → `invoke` → picker.

### 6.3 Operator properties (scriptable, no UI)
On `wm.collada_import` (and the `.cdae` importer for zips):
- `beamng_config: str` — `.pc` path inside the archive, `""` = none.
- `beamng_parts_mode: enum {CONFIG, CONFIG_PLUS_HIDDEN, ALL}`, default `ALL` (unchanged behaviour when called from scripts).
- `archive_models: str` — `;`-separated list of `.dae` paths to import (empty = existing `archive_mode` logic).
- `use_common: bool = True`, `spread_out: bool = False`.

---

## 7. Test plan (run all of it and report)

Run on the PC, Windows, with the Blender builds already used for the 4-cell matrix (5.0.1 and 5.3.0). The game folder is `C:\Steam\steamapps\common\BeamNG.drive\content\vehicles`.

### Step 1: reproduce the baseline (before writing feature code)
```powershell
cd C:\Users\krist\3d
python <path>\beamng_reference\survey.py "C:\Steam\steamapps\common\BeamNG.drive\content\vehicles" tests\_local_beamng\survey
python <path>\beamng_reference\resolve.py tests\_local_beamng\survey tests\_local_beamng\resolve_report.json
```
Compare with section 2: 126 zips, 5,294 jbeam / 0 failures, 1,767 configs, 0 configs without a main part, 2,038 of 2,044 path keys used, 72 configs with missing parts, 113 with missing meshes. Report any difference.

### Step 2: unit tests (synthetic, run in CI, no game files)
`tests/test_beamng_parse.py` and `tests/test_beamng_resolve.py`, plain Python:
- Parser: comments (`//`, `/* */`, `#`), trailing commas, **missing commas** (same line and across lines), `1.`, `.5`, `+1`, `FLT_MAX`, `-FLT_MAX`, unquoted keys, `=` as key separator, escapes and `\u`, BOM, duplicate keys (last wins), unterminated string/array → `JbeamError` with correct line and column.
- Tables: header + list rows, dict modifier rows carried forward, trailing dict in a row, `slots` vs `slots2`, header indices not fixed positions.
- Resolver: name keys, path keys (path beats name), `""` and `"none"` as empty, legacy `.pc` without `format`/`parts`, `mainPartName` override, list `slotType`, duplicate slot names, missing part fallback, cycle guard, deep slots under common parts.
- Expressions: every shape in section 2 plus failure fallback.

### Step 3: real-data resolution checks (local only, `tests/_local_beamng/`)
`tests/run_beamng_real.py` (skips with a clear message when the game folder is missing) must check, for **all 1,767 configs**:
- the parser and resolver never raise;
- every resolved mesh is classified as own / common / other-zip / missing / expression;
- the counts match the survey from step 1 exactly (both use the same index code after you port it).

### Step 4: import checks in Blender (both 5.0.1 and 5.3.0, headless, local only)
For each case, import with `beamng_parts_mode='CONFIG'` and assert:

| Case | Why | Assertions |
|---|---|---|
| `covet.zip` + `13s_M.pc` | Kristóf's main car | exactly the resolved own + common meshes exist; exactly one object containing `hood` that is not a decal (`covet_hood`); no `3wheel` objects |
| `covet.zip` + `3wheel.pc` | variant `.dae` in a subfolder | objects come from `3wheel/covet_3wheel.dae` where the config says so |
| `pickup.zip` + `d15_4wd_A.pc` | legacy `.pc` (no `format`) | resolves; mesh count equals resolver |
| `pickup.zip` + `deserttruck_crawler_A.pc` | 6 slot-path keys, deep common slots | all 6 path keys used; race seats present |
| `vivace.zip` (any config with `$components` expressions) | expressions | no raw `$=` names anywhere; candidates in Alternatives |
| `logs.zip` | `random()` expressions | all existing `log_0N` variants imported, first visible |
| `etk800.zip` | cross-vehicle meshes (`etkc_engbaycrap`) | reported as other-zip, not as missing |
| `md_series.zip`, `percheron.zip` | stale mesh references | import succeeds, missing meshes listed in report |
| `common.zip` picker: tick 2 wheel `.dae` files | browsing | only those 2 models imported; `spread_out=True` puts them side by side without overlap |
| `utv.zip` `CONFIG_PLUS_HIDDEN` | hidden alternatives | object count equals `ALL`; non-config objects only in excluded collections |
| `.cdae` path for `covet.zip` + `13s_M.pc` | same filter on `.cdae` | same object names as the `.dae` import (allow the 2 known `.cdae`-only extras on other cars) |

Record time and peak memory per case, and compare with a full `ALL` import of the same `.dae`.

### Step 5: regression
Re-run the full v1.4.4 4-cell matrix (parity, malformed, fuzz, archives, `.cdae` import/export, oracle). Clear `tests/_work` first: stale `.blend` files from another Blender version caused the false 14/19 on 5.0.1 earlier. Better, make `run_parity_tests.py` clear it itself or key it by Blender version.

### Step 6: UI check
If you can drive Blender's UI, open the picker on `covet.zip` and `common.zip` in Blender 5.0.1 and take screenshots. If you cannot, write a 5-line checklist for Kristóf to click through (open picker, change config, filter models, import, check collections).

---

## 8. Report back (template)

```
## BeamNG config import – test report (<date>, commit <sha>)
### Baseline (step 1)
<table: expected vs measured for every number in section 2; explain differences>
### Unit tests (step 2)  <passed/failed counts, failures with file:line>
### Real-data resolution (step 3)  <counts, any exceptions with zip/file/line>
### Blender import cases (step 4)  <one row per case: 5.0.1 / 5.3.0 result, objects, time, peak memory, notes>
### Regression matrix (step 5)  <24 cells>
### Screenshots or UI checklist (step 6)
### Open questions answered / still open
### Anything you changed in this spec and why
```

Open questions to answer if you can find evidence (for example in the game's own Lua sources for the jbeam slot system, read-only; locate them yourself, this spec does not assume a path):
1. Missing chosen part in a `.pc`: does BeamNG use the slot default or leave the slot empty?
2. Duplicate slot names inside one part: first or last wins?
3. Flexbody `pos`/`rot`/`scale`: units, rotation order, pivot, and how wheel and hub flexbodies are placed. (Needed for Phase 2.)
4. What does BeamNG use as the default config for the 37 vehicles whose `info.json` has no `default_pc` (e.g. the Covet)?

---

## 9. Reference implementation (`beamng_reference/`)

Exploration scripts, tested on the full game folder. Use them as a guide and for step 1, not as code to paste.
- `jbeam_lenient.py`: tolerant parser (0 failures on 5,294 jbeam + 1,767 `.pc`).
- `survey.py`: read-only per-zip index (parts, slots, meshes, `.pc`, `.dae` node names).
- `resolve.py`: resolves every config and classifies its meshes. Known simplifications: no expression evaluation (expressions are only counted), no modifiers, `common.zip` hard-coded by name.
