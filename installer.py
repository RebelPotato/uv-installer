"""Build-time payload preparation and offline Windows installation of uv."""

import argparse
import ctypes
import hashlib
import json
import os
import shutil
import subprocess
import sys
import uuid
import winreg
from collections.abc import Iterator
from contextlib import contextmanager
from ctypes import wintypes
from dataclasses import dataclass
from enum import Enum, IntEnum, auto
from pathlib import Path
from urllib.request import urlopen
from zipfile import ZipFile

_UV_VERSION = "0.12.13"
_DIST_VERSION = "0.32.0"
_BINARIES = ("uv.exe", "uvx.exe", "uvw.exe")
_PAYLOADS = Path(__file__).resolve().parent / "payloads"
_DOWNLOAD_TIMEOUT_SECONDS = 45
_VERIFY_TIMEOUT_SECONDS = 15
_BROADCAST_TIMEOUT_MS = 5000
_HWND_BROADCAST = 0xFFFF
_WM_SETTINGCHANGE = 0x001A
_SMTO_ABORTIFHUNG = 0x0002
_MB_YESNO = 0x00000004
_MB_ICONQUESTION = 0x00000020
_MB_ICONINFORMATION = 0x00000040
_MB_ICONERROR = 0x00000010
_IDYES = 6
_IDNO = 7


class _Architecture(IntEnum):
    # PE machine values also identify the native machine in IsWow64Process2.
    X64 = 0x8664
    ARM64 = 0xAA64


_ARCHIVES = {
    _Architecture.X64: (
        "uv-x86_64-pc-windows-msvc.zip",
        "a86c9dc7bad9b03f388583b7187c05fe9951c2e0d392217e8fd43d97787f6ec2",
    ),
    _Architecture.ARM64: (
        "uv-aarch64-pc-windows-msvc.zip",
        "1efb2654b06e7063d4ac1fc9d49a9bda9a6704d82f035b589a2751a592f14151",
    ),
}


class _PathMode(Enum):
    MODIFY = auto()
    KEEP = auto()


class _Dialog(IntEnum):
    CONFIRM = _MB_YESNO | _MB_ICONQUESTION
    SUCCESS = _MB_ICONINFORMATION
    ERROR = _MB_ICONERROR


@dataclass(frozen=True)
class _InstallPlan:
    _directory: Path
    _prefix: Path
    _layout: str
    _path_mode: _PathMode
    _receipt: Path | None
    _ci_path: Path | None


def _archive_path(architecture: _Architecture) -> Path:
    name, expected_hash = _ARCHIVES[architecture]
    path = _PAYLOADS / name
    with path.open("rb") as archive:
        actual_hash = hashlib.file_digest(archive, "sha256").hexdigest()
    if actual_hash != expected_hash:
        raise ValueError(f"The bundled {name} failed its SHA-256 check.")
    return path


def _prepare_payloads() -> None:
    _PAYLOADS.mkdir(parents=True, exist_ok=True)
    release_url = f"https://github.com/astral-sh/uv/releases/download/{_UV_VERSION}"
    source_url = f"https://raw.githubusercontent.com/astral-sh/uv/{_UV_VERSION}"
    downloads = {name: f"{release_url}/{name}" for name, _ in _ARCHIVES.values()}
    downloads.update({name: f"{source_url}/{name}" for name in ("LICENSE-MIT", "LICENSE-APACHE")})

    for name, url in downloads.items():
        destination = _PAYLOADS / name
        if not destination.exists():
            print(f"Downloading {name}...", flush=True)
            temporary = destination.with_suffix(destination.suffix + ".part")
            try:
                with urlopen(url, timeout=_DOWNLOAD_TIMEOUT_SECONDS) as response:
                    with temporary.open("wb") as output:
                        shutil.copyfileobj(response, output)
                temporary.replace(destination)
            finally:
                temporary.unlink(missing_ok=True)

    for architecture in _Architecture:
        with ZipFile(_archive_path(architecture)) as archive:
            for binary in _BINARIES:
                archive.getinfo(binary)
        print(f"Verified uv {_UV_VERSION} for {architecture.name}.")


def _native_architecture() -> _Architecture:
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.argtypes = []
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.IsWow64Process2.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(wintypes.USHORT), ctypes.POINTER(wintypes.USHORT)
    ]
    kernel.IsWow64Process2.restype = wintypes.BOOL
    process_machine, native_machine = wintypes.USHORT(), wintypes.USHORT()
    if not kernel.IsWow64Process2(
        kernel.GetCurrentProcess(), ctypes.byref(process_machine), ctypes.byref(native_machine)
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return _Architecture(native_machine.value)
    except ValueError:
        raise ValueError("This installer requires x64 or ARM64 Windows.") from None


def _plan_installation(environment: dict[str, str]) -> _InstallPlan:
    home = Path(environment.get("USERPROFILE") or Path.home())
    unmanaged = environment.get("UV_UNMANAGED_INSTALL")
    forced = (
        environment.get("UV_INSTALL_DIR")
        or environment.get("CARGO_DIST_FORCE_INSTALL_DIR")
        or unmanaged
    )
    layout = "flat"
    if forced:
        prefix = Path(forced)
        cargo_home = Path(environment.get("CARGO_HOME") or home / ".cargo")
        if prefix == cargo_home:
            layout = "cargo-home"
    elif environment.get("XDG_BIN_HOME"):
        prefix = Path(environment["XDG_BIN_HOME"])
    elif environment.get("XDG_DATA_HOME"):
        prefix = Path(environment["XDG_DATA_HOME"]) / ".." / "bin"
    else:
        prefix = home / ".local" / "bin"

    # abspath removes '..' without following user-created directory junctions.
    prefix = Path(os.path.abspath(prefix))
    directory = prefix / "bin" if layout == "cargo-home" else prefix
    path_mode = _PathMode.KEEP if unmanaged or environment.get("UV_NO_MODIFY_PATH") else _PathMode.MODIFY
    receipt = None
    if not unmanaged and not environment.get("UV_DISABLE_UPDATE"):
        config_home = environment.get("XDG_CONFIG_HOME") or environment["LOCALAPPDATA"]
        receipt = Path(os.path.abspath(config_home)) / "uv" / "uv-receipt.json"
    ci_path = Path(environment["GITHUB_PATH"]) if environment.get("GITHUB_PATH") else None
    return _InstallPlan(directory, prefix, layout, path_mode, receipt, ci_path)


def _broadcast_environment_change() -> None:
    user = ctypes.WinDLL("user32", use_last_error=True)
    user.SendMessageTimeoutW.argtypes = [
        wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPCWSTR,
        wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t),
    ]
    user.SendMessageTimeoutW.restype = wintypes.LPARAM
    result = ctypes.c_size_t()
    # An unresponsive window must not make an otherwise successful install fail.
    user.SendMessageTimeoutW(
        _HWND_BROADCAST, _WM_SETTINGCHANGE, 0, "Environment",
        _SMTO_ABORTIFHUNG, _BROADCAST_TIMEOUT_MS, ctypes.byref(result),
    )


def _add_user_path(directory: Path) -> None:
    with winreg.CreateKeyEx(
        winreg.HKEY_CURRENT_USER, "Environment",
        access=winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE,
    ) as key:
        try:
            current, _ = winreg.QueryValueEx(key, "Path")
        except FileNotFoundError:
            current = ""
        directories = [entry for entry in current.split(";") if entry]
        if str(directory).casefold() in (entry.casefold() for entry in directories):
            return
        # Keep registry variables unexpanded so future environment changes still apply.
        updated = ";".join([str(directory), *directories])
        winreg.SetValueEx(key, "Path", 0, winreg.REG_EXPAND_SZ, updated)
    _broadcast_environment_change()


def _verify_uv(executable: Path) -> None:
    result = subprocess.run(
        [str(executable), "--version"], check=True, capture_output=True, text=True,
        timeout=_VERIFY_TIMEOUT_SECONDS, creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if result.stdout.split()[:2] != ["uv", _UV_VERSION]:
        raise ValueError("The installed uv executable reported an unexpected version.")


@contextmanager
def _staging_directory(parent: Path, prefix: str) -> Iterator[Path]:
    # Installed files must inherit the destination ACL, not owner-only temporary permissions.
    directory = parent / f"{prefix}{uuid.uuid4().hex}"
    directory.mkdir()
    try:
        yield directory
    finally:
        shutil.rmtree(directory)


def _write_receipt(plan: _InstallPlan) -> None:
    if plan._receipt is None:
        return
    receipt = {
        "binaries": list(_BINARIES), "binary_aliases": {}, "cdylibs": [], "cstaticlibs": [],
        "install_layout": plan._layout, "install_prefix": str(plan._prefix),
        "modify_path": plan._path_mode is _PathMode.MODIFY,
        "provider": {"source": "cargo-dist", "version": _DIST_VERSION},
        "source": {"app_name": "uv", "name": "uv", "owner": "astral-sh", "release_type": "github"},
        "version": _UV_VERSION,
    }
    plan._receipt.parent.mkdir(parents=True, exist_ok=True)
    with _staging_directory(plan._receipt.parent, ".uv-receipt-") as temporary:
        staged = temporary / "uv-receipt.json"
        staged.write_text(json.dumps(receipt) + "\n", encoding="utf-8")
        os.replace(staged, plan._receipt)


def _install(plan: _InstallPlan, architecture: _Architecture) -> None:
    archive_path = _archive_path(architecture)
    plan._directory.mkdir(parents=True, exist_ok=True)
    with _staging_directory(plan._directory, ".uv-setup-") as scratch:
        staged, backup = scratch / "new", scratch / "old"
        staged.mkdir()
        backup.mkdir()
        with ZipFile(archive_path) as archive:
            for name in _BINARIES:
                with archive.open(name) as source, (staged / name).open("wb") as output:
                    shutil.copyfileobj(source, output)
        _verify_uv(staged / "uv.exe")

        for name in _BINARIES:
            destination = plan._directory / name
            if destination.exists():
                shutil.copy2(destination, backup / name)
        replaced = []
        try:
            for name in _BINARIES:
                os.replace(staged / name, plan._directory / name)
                replaced.append(name)
            _verify_uv(plan._directory / "uv.exe")
        except (OSError, subprocess.SubprocessError, ValueError):
            # A locked executable must not leave the other binaries partially upgraded.
            for name in reversed(replaced):
                destination = plan._directory / name
                if (backup / name).exists():
                    os.replace(backup / name, destination)
                else:
                    destination.unlink()
            raise

    _write_receipt(plan)
    if plan._path_mode is _PathMode.MODIFY:
        if plan._ci_path:
            with plan._ci_path.open("a", encoding="utf-8") as output:
                output.write(str(plan._directory) + "\n")
        _add_user_path(plan._directory)


def _message(text: str, kind: _Dialog) -> int:
    user = ctypes.WinDLL("user32", use_last_error=True)
    user.MessageBoxW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT]
    user.MessageBoxW.restype = ctypes.c_int
    return user.MessageBoxW(None, text, "uv setup", kind)


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install the bundled uv release without network access.")
    parser.add_argument("--silent", action="store_true", help="Install without dialogs; exit 0 on success, 1 on failure.")
    if not getattr(sys, "frozen", False):
        parser.add_argument("--prepare", action="store_true", help="Download pinned build inputs; requires network access.")
    arguments = parser.parse_args(argv)
    try:
        if getattr(arguments, "prepare", False):
            _prepare_payloads()
            return 0
        architecture = _native_architecture()
        plan = _plan_installation(dict(os.environ))
        if not arguments.silent:
            prompt = f"Install uv {_UV_VERSION}?\n\nLocation: {plan._directory}"
            if _message(prompt, _Dialog.CONFIRM) != _IDYES:
                return 0
        _install(plan, architecture)
        message = f"uv {_UV_VERSION} is installed.\n\nLocation: {plan._directory}"
        if plan._path_mode is _PathMode.MODIFY:
            message += "\n\nOpen a new terminal to use uv."
        if arguments.silent:
            if sys.stdout:
                print(message)
        else:
            _message(message, _Dialog.SUCCESS)
        return 0
    except Exception as error:
        message = f"Could not finish installing uv.\n\n{error}"
        if isinstance(error, PermissionError):
            message += "\n\nClose any programs using uv and check that you can write to the installation folder."
        if arguments.silent or getattr(arguments, "prepare", False):
            if sys.stderr:
                print(message, file=sys.stderr)
        else:
            _message(message, _Dialog.ERROR)
        return 1


if __name__ == "__main__":
    sys.exit(_main())
