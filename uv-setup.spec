import struct
import sys
from importlib.metadata import distribution
from pathlib import Path

sys.path.insert(0, SPECPATH)
import installer

# A 32-bit launcher can start on both Windows x64 and Windows ARM64.
_X86_POINTER_BYTES = 4
if struct.calcsize("P") != _X86_POINTER_BYTES:
    raise SystemExit("Build with the 32-bit Python pinned in .python-version: uv run pyinstaller uv-setup.spec")

payloads = Path(SPECPATH) / "payloads"
datas = [(str(installer._archive_path(arch)), "payloads") for arch in installer._Architecture]
datas += [(str(payloads / name), "licenses/uv") for name in ("LICENSE-MIT", "LICENSE-APACHE")]
datas.append((str(Path(sys.base_prefix) / "LICENSE.txt"), "licenses/python"))
pyinstaller = distribution("pyinstaller")
copying = next(path for path in pyinstaller.files if path.name == "COPYING.txt")
datas.append((str(pyinstaller.locate_file(copying)), "licenses/pyinstaller"))

analysis = Analysis(
    [str(Path(SPECPATH) / "installer.py")],
    pathex=[SPECPATH],
    datas=datas,
    excludes=["pytest", "PyInstaller"],
)
executable = EXE(
    PYZ(analysis.pure),
    analysis.scripts,
    analysis.binaries,
    analysis.datas,
    [],
    name="uv-setup",
    console=False,
    upx=False,
    uac_admin=False,
)
