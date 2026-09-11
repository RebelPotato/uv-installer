# Offline uv installer

Send `dist/uv-setup.exe` to a Windows computer, run it, confirm the installation, and open a new terminal. The installer carries uv 0.12.13 for both x64 and ARM64, together with its own Python runtime. Installation does not download anything or require Python to be installed first.

The implementation is in **`installer.py`**. It uses the Python standard library and native Windows message boxes. `uv-setup.spec` contains the PyInstaller build configuration; tests are separate.

## Installation behavior

The default is the same as uv's standalone Windows installer: install for the current user in `%USERPROFILE%\.local\bin`, prepend that directory to the user's PATH if absent, and write `%LOCALAPPDATA%\uv\uv-receipt.json`. The executable requests no administrator elevation. It installs `uv.exe`, `uvx.exe`, and `uvw.exe`.

The launcher is 32-bit x86 so Windows can run it on both x64 and ARM64. `IsWow64Process2` identifies the native Windows architecture, and the installer selects the corresponding native uv ZIP. The build checks the upstream SHA-256 hashes, and installation checks the selected archive again before using it. Failed binary replacement restores the previous binaries.

The wrapper honors `UV_INSTALL_DIR`, the legacy `CARGO_DIST_FORCE_INSTALL_DIR`, `XDG_BIN_HOME`, and `XDG_DATA_HOME` for directory selection, including the standalone installer's Cargo-home layout compatibility. It also honors `XDG_CONFIG_HOME`, `UV_NO_MODIFY_PATH`, `UV_DISABLE_UPDATE`, `UV_UNMANAGED_INSTALL`, and `GITHUB_PATH`. Like the PowerShell installer, a nonempty disable variable counts as enabled, even if its value is `0`.

For unattended installation, run `uv-setup.exe --silent`. Use a process launcher that waits for completion: exit code 0 means success and 1 means installation failed. Interactive mode displays error details; the windowed executable has no console output in silent mode. The download command is unavailable in the packaged executable.

Installation metadata retains normal `uv self update` support. Offline upgrades use a newly bundled installer. Installing Python versions or packages with uv afterward requires separately available package files, a cache, or an internal service.

## Build with uv

Build on Windows with uv installed. `.python-version` pins uv-managed **CPython 3.13.15 x86**, and `uv.lock` pins the build/test dependencies, including PyInstaller 6.22.2. The spec refuses a 64-bit Python build so it cannot accidentally produce an x64-only launcher.

The initial Python/dependency setup and payload preparation need network access on the preparation machine. `--prepare` respects proxy environment variables. This workspace uses the following PowerShell commands:

```powershell
$env:HTTP_PROXY = "http://127.0.0.1:7897"
$env:HTTPS_PROXY = $env:HTTP_PROXY
uv sync --locked
uv run --locked installer.py --prepare
uv run --offline --locked pyinstaller --noconfirm uv-setup.spec
uv run --offline --locked python -m pytest -q
```

The output is **`dist/uv-setup.exe`**, approximately 42 MiB. Only that file is needed on the target computer. uv, Python, and PyInstaller license notices are included inside it. The build is unsigned.

If the preparation machine cannot download uv, supply these files in `payloads/` through your approved transfer process:

- `uv-x86_64-pc-windows-msvc.zip` from uv 0.12.13.
- `uv-aarch64-pc-windows-msvc.zip` from the same release.
- `LICENSE-MIT` and `LICENSE-APACHE` from that release's source tree.

The interpreter and locked build dependencies must also be available locally. With those inputs prepared, the PyInstaller command runs offline. Downloaded inputs, virtual environments, and generated artifacts are excluded from jj snapshots through `.gitignore`.

To bundle a different uv version, update `_UV_VERSION`, both hashes in `_ARCHIVES`, and the executable list and `_DIST_VERSION` if the upstream installer changed. Remove the old cached ZIPs from `payloads/`, prepare the new payloads, and rebuild. The reference installer for this release is [uv 0.12.13's Windows installer](https://astral.sh/uv/0.12.13/install.ps1); archive checksums are published alongside the [release assets](https://github.com/astral-sh/uv/releases/tag/0.12.13).

## Validation

The 23 installation tests cover directory precedence, native architecture selection under emulation, an isolated Windows registry PATH update, actual installation/reinstallation, receipt behavior, inherited file permissions, cancellation, corrupt archives, and rollback after a failed replacement. The three bundle tests inspect the executable and its embedded payloads, run the packaged installer twice with Python removed from PATH and unreachable network proxies, and check that packaged `--prepare` is rejected. Bundle tests skip until the executable has been built.

The packaged executable was also exercised through the native Windows confirmation and success dialogs, installing to a temporary location. Validation ran on Windows 11 x64 using the 32-bit launcher. ARM64 payload architecture and selection are verified, but execution on an ARM64 computer has not been tested. Windows 10 and a clean Windows VM with networking disabled were unavailable; invalid-proxy tests do not replace that final deployment check.
