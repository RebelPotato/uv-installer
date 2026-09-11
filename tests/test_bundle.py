"""Run after building; these tests execute the delivered file, not the source."""

import hashlib
import json
import os
import struct
import subprocess
import winreg
from pathlib import Path

import pytest
from PyInstaller.archive.readers import CArchiveReader

import installer

_BUNDLE = Path(__file__).resolve().parents[1] / "dist" / "uv-setup.exe"
pytestmark = pytest.mark.skipif(not _BUNDLE.exists(), reason="Build uv-setup.exe first")
_PE_HEADER_OFFSET = 0x3C
_PE_SIGNATURE_BYTES = 4
_IMAGE_FILE_MACHINE_I386 = 0x014C


def test_bundle_contains_x86_launcher_and_both_verified_payloads():
    with _BUNDLE.open("rb") as executable:
        executable.seek(_PE_HEADER_OFFSET)
        pe_offset = struct.unpack("<I", executable.read(4))[0]
        executable.seek(pe_offset + _PE_SIGNATURE_BYTES)
        assert struct.unpack("<H", executable.read(2))[0] == _IMAGE_FILE_MACHINE_I386
    archive = CArchiveReader(str(_BUNDLE))
    for name, digest in installer._ARCHIVES.values():
        data = archive.extract(f"payloads\\{name}")
        assert hashlib.sha256(data).hexdigest() == digest
    assert "python313.dll" in archive.toc
    assert "licenses\\python\\LICENSE.txt" in archive.toc
    assert "licenses\\uv\\LICENSE-MIT" in archive.toc


def test_packaged_install_without_python_on_path(tmp_path):
    # Invalid proxies exercise unavailable network access without changing host networking.
    environment = {
        name: value for name, value in os.environ.items()
        if not name.startswith(("UV_", "PYTHON", "XDG_", "CARGO_", "GITHUB_"))
    }
    destination = tmp_path / "installation with spaces" / "bin"
    environment.update(
        PATH=str(Path(os.environ["SYSTEMROOT"]) / "System32"),
        UV_INSTALL_DIR=str(destination),
        UV_NO_MODIFY_PATH="1",
        LOCALAPPDATA=str(tmp_path / "metadata"),
        HTTP_PROXY="http://127.0.0.1:9",
        HTTPS_PROXY="http://127.0.0.1:9",
        ALL_PROXY="http://127.0.0.1:9",
        NO_PROXY="",
    )
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
        original_path = winreg.QueryValueEx(key, "Path")
    for _ in range(2):
        completed = subprocess.run([str(_BUNDLE), "--silent"], env=environment, timeout=45)
        assert completed.returncode == 0
        installed = subprocess.run(
            [str(destination / "uv.exe"), "--version"], env=environment,
            capture_output=True, text=True, check=True, timeout=15,
        )
        assert installed.stdout.startswith(f"uv {installer._UV_VERSION}")
        receipt = json.loads((tmp_path / "metadata/uv/uv-receipt.json").read_text())
        assert receipt["install_prefix"] == str(destination)
        assert receipt["modify_path"] is False
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
        assert winreg.QueryValueEx(key, "Path") == original_path


def test_packaged_installer_rejects_build_download_command():
    completed = subprocess.run([str(_BUNDLE), "--silent", "--prepare"], timeout=30)
    assert completed.returncode == 2
