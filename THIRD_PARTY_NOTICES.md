# Third-Party Notices

This extension is licensed under **GPL-2.0-or-later**. The BeamNG `.cdae` (v31) support includes data and format conventions derived from the following permissively licensed third-party projects:

## Torque3D (MIT License)

- **Source**: https://github.com/TorqueGameEngines/Torque3D
- **Used in**: `io_scene_collada/cdae/normal_table.py` (`TSMesh::smU8ToNormalTable`), and struct/encoding conventions in `io_scene_collada/cdae/reader.py` and `io_scene_collada/cdae/importer.py` (`TSShape`, `TSMesh`, `TSDrawPrimitive`, `Quat16`, `ColorI`, `TSIntegerSet`).
- **License**:

```text
Copyright (c) 2012 GarageGames, LLC

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to
deal in the Software without restriction, including without limitation the
rights to use, copy, modify, merge, publish, distribute, sublicense, and/or
sell copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS
IN THE SOFTWARE.
```
