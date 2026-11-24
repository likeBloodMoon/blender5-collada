# BeamNG `.cdae` (v31) Manual In-Game Test Checklist

Use this checklist to verify `.cdae` files exported with **File › Export › BeamNG Cached Collada (.cdae) [Experimental]** inside BeamNG.drive.

> **Note:** BeamNG normally compiles `.cdae` files automatically from `.dae` sources on first load. If a `.cdae` already exists next to the `.dae` or in BeamNG's user cache (`%LOCALAPPDATA%\BeamNG.drive\<version>\temp\...`), clear the cache before testing so BeamNG loads your exported file.

## 1. Static Prop (Single Mesh, UV0, Smooth + Sharp Normals)
- [ ] Export a static prop (e.g. beveled cylinder or crate with sharp edges and UV0) to `.cdae`.
- [ ] Load in BeamNG World Editor (`TSStatic`).
- [ ] Geometry renders at expected position, scale, and orientation.
- [ ] Sharp edges and smooth faces shade identically to the `.dae` version (no inverted or faceted normals).
- [ ] Texture UV mapping is right-side up (V-axis not inverted).
- [ ] Zero errors or warnings in BeamNG's `beamng.log` / console (`~`).

## 2. Vehicle Part with Multiple Materials & Non-Identity Rotation
- [ ] Export a multi-material vehicle part (e.g. steering wheel or hood with paint + chrome/glass materials and rotated transform) to `.cdae`.
- [ ] Load on a vehicle via JBeam `flexbodies`.
- [ ] Every material maps to its `main.materials.json` entry by exact name.
- [ ] Part sits in the exact position and rotation expected (verifies `Quat16` conjugate sign in-game).
- [ ] Normal maps / specular reflections shade cleanly without tangent seams (`mesh.calc_tangents()`).
- [ ] Zero errors in `beamng.log`.

## 3. Mesh with Non-White Vertex Colors & Dual UVs (UV0 + UV1)
- [ ] Export a mesh with sRGB vertex colors (e.g. AO/color-mask) and 2 UV channels (`UVMap`, `UVMap.001`).
- [ ] Load in BeamNG with a material using `vertexColor` and secondary UV (`uv1`).
- [ ] Vertex color brightness matches the `.dae` version (no sRGB/linear washout).
- [ ] Secondary UV layer aligns without V-flip.
- [ ] Zero errors in `beamng.log`.

## 4. Skinned Mesh (`meshType = 1`) — *Not Yet Supported for Export*
- [ ] Skinned `.cdae` export is intentionally deferred until real BeamNG skinned `.cdae` files have been verified against `.dae` counterparts (`initialTransforms` row/column order).
- [ ] Use `.dae` export for skinned meshes in the meantime.
