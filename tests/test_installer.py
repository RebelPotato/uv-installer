import ctypes
import json
import os
import socket
import subprocess
import winreg
from pathlib import Path
from unittest.mock import Mock

import pytest

import installer


@pytest.fixture
def environment(tmp_path):
    return {
        "USERPROFILE": str(tmp_path / "user"),
        "LOCALAPPDATA": str(tmp_path / "local"),
    }


@pytest.mark.parametrize(
    ("overrides", "relative_destination"),
    [
        ({}, "user/.local/bin"),
        ({"XDG_DATA_HOME": "data/share"}, "data/bin"),
        ({"XDG_BIN_HOME": "xdg-bin", "XDG_DATA_HOME": "data/share"}, "xdg-bin"),
        ({"UV_INSTALL_DIR": "custom", "XDG_BIN_HOME": "xdg-bin"}, "custom"),
        ({"CARGO_DIST_FORCE_INSTALL_DIR": "legacy"}, "legacy"),
        ({"UV_UNMANAGED_INSTALL": "unmanaged"}, "unmanaged"),
        ({"UV_INSTALL_DIR": "custom", "UV_UNMANAGED_INSTALL": "unmanaged"}, "custom"),
        ({"UV_INSTALL_DIR": "user/.cargo"}, "user/.cargo/bin"),
    ],
)
def test_destination_precedence(environment, tmp_path, overrides, relative_destination):
    environment.update({name: str(tmp_path / path) for name, path in overrides.items()})
    plan = installer._plan_installation(environment)
    assert plan._directory == tmp_path / relative_destination


def test_cargo_layout_and_config_override(environment, tmp_path):
    environment.update(
        UV_INSTALL_DIR=str(tmp_path / "cargo"),
        CARGO_HOME=str(tmp_path / "cargo"),
        XDG_CONFIG_HOME=str(tmp_path / "config"),
    )
    plan = installer._plan_installation(environment)
    assert plan._layout == "cargo-home"
    assert plan._prefix == tmp_path / "cargo"
    assert plan._directory == tmp_path / "cargo/bin"
    assert plan._receipt == tmp_path / "config/uv/uv-receipt.json"


@pytest.mark.parametrize("variable", ["UV_NO_MODIFY_PATH", "UV_UNMANAGED_INSTALL"])
def test_no_path_changes_when_disabled(environment, variable):
    environment[variable] = "0"
    assert installer._plan_installation(environment)._path_mode is installer._PathMode.KEEP


@pytest.mark.parametrize("variable", ["UV_DISABLE_UPDATE", "UV_UNMANAGED_INSTALL"])
def test_receipt_disabled(environment, variable):
    environment[variable] = "1"
    assert installer._plan_installation(environment)._receipt is None


@pytest.mark.parametrize("architecture", list(installer._Architecture))
def test_native_machine_wins_over_emulated_process(monkeypatch, architecture):
    kernel = Mock()

    def query(process, process_machine, native_machine):
        ctypes.cast(process_machine, ctypes.POINTER(ctypes.c_ushort))[0] = 0x014C
        ctypes.cast(native_machine, ctypes.POINTER(ctypes.c_ushort))[0] = architecture
        return True

    kernel.IsWow64Process2.side_effect = query
    monkeypatch.setattr(installer.ctypes, "WinDLL", Mock(return_value=kernel))
    assert installer._native_architecture() is architecture


def test_unsupported_native_machine_is_rejected(monkeypatch):
    kernel = Mock()

    def query(process, process_machine, native_machine):
        ctypes.cast(native_machine, ctypes.POINTER(ctypes.c_ushort))[0] = 0x014C
        return True

    kernel.IsWow64Process2.side_effect = query
    monkeypatch.setattr(installer.ctypes, "WinDLL", Mock(return_value=kernel))
    with pytest.raises(ValueError, match="x64 or ARM64"):
        installer._native_architecture()


def test_path_registry_preserves_variables_and_does_not_duplicate(monkeypatch, tmp_path):
    # A real isolated key exercises registry encoding without touching the user's PATH.
    registry_path = rf"Software\uv-installer-tests\{tmp_path.name}"
    create_key = winreg.CreateKeyEx
    with create_key(winreg.HKEY_CURRENT_USER, registry_path, access=winreg.KEY_ALL_ACCESS) as key:
        winreg.SetValueEx(key, "Path", 0, winreg.REG_EXPAND_SZ, r"%CUSTOM_ROOT%\bin;C:\Existing")

    monkeypatch.setattr(
        installer.winreg,
        "CreateKeyEx",
        lambda root, path, **kwargs: create_key(root, registry_path, **kwargs),
    )
    broadcast = Mock()
    monkeypatch.setattr(installer, "_broadcast_environment_change", broadcast)
    try:
        destination = tmp_path / "install"
        installer._add_user_path(destination)
        installer._add_user_path(Path(str(destination).upper()))
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, registry_path) as key:
            value, kind = winreg.QueryValueEx(key, "Path")
        assert value == str(destination) + r";%CUSTOM_ROOT%\bin;C:\Existing"
        assert kind == winreg.REG_EXPAND_SZ
        broadcast.assert_called_once_with()
    finally:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, registry_path)


def test_offline_install_and_reinstall_keep_existing_files(environment, tmp_path, monkeypatch):
    monkeypatch.setattr(socket, "create_connection", Mock(side_effect=AssertionError("Network used")))
    add_path = Mock()
    monkeypatch.setattr(installer, "_add_user_path", add_path)
    plan = installer._plan_installation(environment)
    plan._directory.mkdir(parents=True)
    unrelated = plan._directory / "another-tool.txt"
    unrelated.write_text("keep me", encoding="utf-8")

    for _ in range(2):
        installer._install(plan, installer._Architecture.X64)
        result = subprocess.run(
            [str(plan._directory / "uv.exe"), "--version"],
            check=True,
            capture_output=True,
            text=True,
        )
        assert result.stdout.startswith(f"uv {installer._UV_VERSION}")
        assert all((plan._directory / name).is_file() for name in installer._BINARIES)
        receipt = json.loads(plan._receipt.read_text(encoding="utf-8"))
        assert receipt["install_prefix"] == str(plan._directory)
        assert receipt["modify_path"] is True
        assert receipt["version"] == installer._UV_VERSION
        assert receipt["binaries"] == list(installer._BINARIES)
        assert receipt["provider"] == {"source": "cargo-dist", "version": "0.32.0"}

    assert unrelated.read_text(encoding="utf-8") == "keep me"
    assert add_path.call_count == 2


def test_unmanaged_install_leaves_path_and_receipt_alone(environment, tmp_path, monkeypatch):
    environment["UV_UNMANAGED_INSTALL"] = str(tmp_path / "unmanaged")
    add_path = Mock()
    monkeypatch.setattr(installer, "_add_user_path", add_path)
    plan = installer._plan_installation(environment)
    installer._install(plan, installer._Architecture.X64)
    assert not (tmp_path / "local/uv/uv-receipt.json").exists()
    add_path.assert_not_called()


def test_corrupt_payload_does_not_touch_existing_install(environment, tmp_path, monkeypatch):
    plan = installer._plan_installation(environment)
    plan._directory.mkdir(parents=True)
    previous = plan._directory / "uv.exe"
    previous.write_bytes(b"previous installation")
    monkeypatch.setattr(installer, "_PAYLOADS", tmp_path)
    name, _ = installer._ARCHIVES[installer._Architecture.X64]
    (tmp_path / name).write_bytes(b"corrupt archive")
    with pytest.raises(ValueError, match="SHA-256"):
        installer._install(plan, installer._Architecture.X64)
    assert previous.read_bytes() == b"previous installation"
    assert not plan._receipt.exists()


def test_failed_replacement_restores_previous_binaries(environment, monkeypatch):
    plan = installer._plan_installation(environment)
    plan._directory.mkdir(parents=True)
    for name in installer._BINARIES:
        (plan._directory / name).write_bytes(b"previous " + name.encode())
    replace = os.replace

    def fail_second_binary(source, destination):
        if Path(source).parent.name == "new" and Path(destination).name == "uvx.exe":
            raise PermissionError("uvx.exe is in use")
        return replace(source, destination)

    monkeypatch.setattr(installer.os, "replace", fail_second_binary)
    with pytest.raises(PermissionError):
        installer._install(plan, installer._Architecture.X64)
    for name in installer._BINARIES:
        assert (plan._directory / name).read_bytes() == b"previous " + name.encode()
    assert not plan._receipt.exists()


def test_installed_files_inherit_destination_permissions(environment, tmp_path, monkeypatch):
    plan = installer._plan_installation(environment)
    plan._directory.mkdir(parents=True)
    plan._receipt.parent.mkdir(parents=True)
    system = Path(os.environ["SYSTEMROOT"]) / "System32"
    # A distinct inherited grant makes staging-directory ACL leakage observable.
    for directory in (plan._directory, plan._receipt.parent):
        subprocess.run(
            [str(system / "icacls.exe"), str(directory), "/grant", "*S-1-5-11:(OI)(CI)(RX)"],
            check=True, capture_output=True,
        )
    monkeypatch.setattr(installer, "_add_user_path", Mock())
    installer._install(plan, installer._Architecture.X64)
    for path in [plan._directory / name for name in installer._BINARIES] + [plan._receipt]:
        escaped = str(path).replace("'", "''")
        security = subprocess.run(
            [str(system / "WindowsPowerShell/v1.0/powershell.exe"), "-NoProfile", "-NonInteractive",
             "-Command", f"(Get-Acl -LiteralPath '{escaped}').Sddl"],
            check=True, capture_output=True, text=True,
        ).stdout
        assert ";;;AU)" in security, f"Missing inherited Authenticated Users grant on {path}"


def test_cancellation_makes_no_installation(monkeypatch):
    monkeypatch.setattr(installer, "_native_architecture", Mock(return_value=installer._Architecture.X64))
    monkeypatch.setattr(installer, "_message", Mock(return_value=installer._IDNO))
    install = Mock()
    monkeypatch.setattr(installer, "_install", install)
    assert installer._main([]) == 0
    install.assert_not_called()
