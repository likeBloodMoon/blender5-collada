"""Unit and integration tests for UX helpers (#7, #8, #9)."""

from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS_DIR = os.path.join(REPO_ROOT, ".tools")


def _find_blender() -> str:
    env = os.environ.get("BLENDER_5X")
    if env and os.path.exists(env):
        return env
    exe = "blender.exe" if os.name == "nt" else "blender"
    matches = glob.glob(os.path.join(TOOLS_DIR, "blender-5.*", exe))
    if matches:
        return matches[0]
    raise RuntimeError("Could not find Blender 5.x (set BLENDER_5X)")


WORKER_SCRIPT = r'''
import json
import os
import sys

argv = sys.argv[sys.argv.index("--") + 1:]
repo_root, out_json = argv[0], argv[1]
sys.path.insert(0, repo_root)

import bpy
import io_scene_collada
from io_scene_collada.collada_utils import check_target_collection_visibility, report_capped
from io_scene_collada.import_collada import DocumentImporter, ImportSettings
from io_scene_collada.cdae.importer import CdaeImporter, CdaeImportSettings
from io_scene_collada.cdae.exporter import CdaeExporter, CdaeExportSettings


class _DummyOp:
    def __init__(self):
        self.calls = []

    def report(self, level_set, msg):
        self.calls.append((sorted(level_set), msg))


# Test #7: report_capped
op1 = _DummyOp()
report_capped(op1, "WARNING", [])
assert op1.calls == [], op1.calls

op2 = _DummyOp()
report_capped(op2, "ERROR", [], fallback="Default failure")
assert op2.calls == [(["ERROR"], "Default failure")], op2.calls

op3 = _DummyOp()
report_capped(op3, "WARNING", [f"w{i}" for i in range(7)], limit=5)
assert len(op3.calls) == 6, op3.calls
assert op3.calls[:5] == [(["WARNING"], f"w{i}") for i in range(5)]
assert op3.calls[5] == (["WARNING"], "... and 2 more warning(s)"), op3.calls[5]

op4 = _DummyOp()
report_capped(op4, "ERROR", [f"e{i}" for i in range(6)], limit=5)
assert len(op4.calls) == 6 and op4.calls[5] == (["ERROR"], "... and 1 more error(s)"), op4.calls

# Test #8: warn when importing into an excluded or hidden collection (.dae & .cdae)
workdir = os.path.dirname(out_json)
dae_p = os.path.join(workdir, "tiny.dae")
with open(dae_p, "w", encoding="utf-8") as fh:
    fh.write(
        '<?xml version="1.0" encoding="utf-8"?>'
        '<COLLADA xmlns="http://www.collada.org/2005/11/COLLADASchema" version="1.4.1">'
        '<asset><up_axis>Z_UP</up_axis></asset>'
        '<library_geometries><geometry id="M"><mesh>'
        '<source id="P"><float_array id="Pa" count="9">0 0 0 1 0 0 0 1 0</float_array>'
        '<technique_common><accessor source="#Pa" count="3" stride="3">'
        '<param name="X" type="float"/><param name="Y" type="float"/><param name="Z" type="float"/>'
        '</accessor></technique_common></source>'
        '<vertices id="V"><input semantic="POSITION" source="#P"/></vertices>'
        '<triangles count="1"><input semantic="VERTEX" source="#V" offset="0"/><p>0 1 2</p></triangles>'
        '</mesh></geometry></library_geometries>'
        '<library_visual_scenes><visual_scene id="S">'
        '<node id="N" name="N"><instance_geometry url="#M"/></node>'
        '</visual_scene></library_visual_scenes>'
        '<scene><instance_visual_scene url="#S"/></scene></COLLADA>'
    )

# Export a tiny .cdae for testing .cdae import too
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.mesh.primitive_cube_add()
cdae_p = os.path.join(workdir, "tiny.cdae")
assert CdaeExporter(bpy.context, CdaeExportSettings(filepath=cdae_p)).export_document()

# 8a: Normal visible collection -> NO visibility warning
bpy.ops.wm.read_factory_settings(use_empty=True)
imp_vis = DocumentImporter(bpy.context, ImportSettings(filepath=dae_p))
assert imp_vis.import_document()
assert not any("excluded/hidden" in w for w in imp_vis.warnings), imp_vis.warnings

# 8b: Hidden LayerCollection (hide_viewport) -> WARNING on .dae import, plus exclude check
bpy.ops.wm.read_factory_settings(use_empty=True)
c_excl = bpy.data.collections.new("ExcludedColl")
bpy.context.scene.collection.children.link(c_excl)
lc_excl = bpy.context.view_layer.layer_collection.children["ExcludedColl"]
lc_excl.exclude = True
assert check_target_collection_visibility(bpy.context.view_layer, c_excl) == (
    "Imported into 'ExcludedColl', which is excluded/hidden in this view layer"
)
lc_excl.exclude = False
lc_excl.hide_viewport = True
bpy.context.view_layer.active_layer_collection = lc_excl
imp_excl = DocumentImporter(bpy.context, ImportSettings(filepath=dae_p))
assert imp_excl.import_document()
assert any("Imported into 'ExcludedColl', which is excluded/hidden" in w for w in imp_excl.warnings), imp_excl.warnings

# 8c: Hidden Collection / LayerCollection -> WARNING on .cdae import
bpy.ops.wm.read_factory_settings(use_empty=True)
c_hid = bpy.data.collections.new("HiddenColl")
bpy.context.scene.collection.children.link(c_hid)
lc_hid = bpy.context.view_layer.layer_collection.children["HiddenColl"]
c_hid.hide_viewport = True
assert check_target_collection_visibility(bpy.context.view_layer, c_hid) == (
    "Imported into 'HiddenColl', which is excluded/hidden in this view layer"
)
c_hid.hide_viewport = False
lc_hid.hide_viewport = True
bpy.context.view_layer.active_layer_collection = lc_hid
imp_cdae = CdaeImporter(bpy.context, CdaeImportSettings(filepath=cdae_p))
assert imp_cdae.import_document()
assert any("Imported into 'HiddenColl', which is excluded/hidden" in w for w in imp_cdae.warnings), imp_cdae.warnings

# Test #9: restart-after-update version check & manifest tagline
assert io_scene_collada.__version__ != "", "Expected __version__ from blender_manifest.toml"


class _DummyPrefs:
    def __init__(self, v=""):
        self.last_registered_version = v


p_fresh = _DummyPrefs("")
assert io_scene_collada._check_version_update(p_fresh, io_scene_collada.__version__) is None
assert p_fresh.last_registered_version == io_scene_collada.__version__
# Same version again -> no notice
assert io_scene_collada._check_version_update(p_fresh, io_scene_collada.__version__) is None
# Upgrade from older version -> one-time restart notice
p_old = _DummyPrefs("1.5.0")
notice = io_scene_collada._check_version_update(p_old, io_scene_collada.__version__)
assert notice is not None and "restart Blender to finish" in notice, notice
assert p_old.last_registered_version == io_scene_collada.__version__

with open(out_json, "w", encoding="utf-8") as fh:
    json.dump({"ok": True}, fh)
'''


def main() -> int:
    blender = _find_blender()
    print(f"Running UX helper tests using: {blender}")
    workdir = tempfile.mkdtemp(prefix="collada_ux_")
    try:
        worker = os.path.join(workdir, "_worker.py")
        out_json = os.path.join(workdir, "out.json")
        with open(worker, "w", encoding="utf-8") as fh:
            fh.write(WORKER_SCRIPT)
        proc = subprocess.run(
            [blender, "-b", "--factory-startup", "--python-exit-code", "1", "--python", worker, "--", REPO_ROOT, out_json],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if proc.returncode != 0:
            print(proc.stdout[-1500:])
            print(proc.stderr[-1500:])
            return 1
        assert json.load(open(out_json, "r", encoding="utf-8"))["ok"]
        print("  PASS report_capped (#7)")
        return 0
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
