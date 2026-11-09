# Third-party notices

Claude Bridge source code is distributed under the MIT License in `LICENSE`.
That license does not replace the licenses of its dependencies, included runtimes,
or external services.

Refer to the installed version's distribution metadata and license files for the
complete terms and copyright notices. Windows release archives include applicable
license texts and a build-information file identifying the packaged versions.

| Component | Use | Upstream license / notices |
| --- | --- | --- |
| Python | Runtime | [Python license and history](https://docs.python.org/3/license.html) |
| PySide6 / Shiboken6 / Qt | Windows desktop UI | [Qt for Python licensing](https://doc.qt.io/qtforpython-6/licenses.html); the LGPL version 3 option is used for the packaged open-source components, with module-specific terms |
| Pillow | Image preparation | [Pillow license](https://github.com/python-pillow/Pillow/blob/main/LICENSE) |
| qrcode | Device-pairing QR codes | [qrcode license](https://github.com/lincolnloop/python-qrcode/blob/main/LICENSE) |
| FastAPI | Optional mobile backend | [FastAPI license](https://github.com/fastapi/fastapi/blob/master/LICENSE) |
| Uvicorn | Optional HTTP server | [Uvicorn license](https://github.com/encode/uvicorn/blob/main/LICENSE.md) |
| python-multipart | Optional attachment upload handling | [python-multipart license](https://github.com/Kludex/python-multipart/blob/master/LICENSE.txt) |
| PyInstaller | Windows packaging tool | [PyInstaller license and bootloader exception](https://pyinstaller.org/en/stable/license.html) |

Transitive dependencies carry their own licenses. The notices and license texts in
a release archive apply to the exact versions listed in its build information.

## Qt and PySide source and replacement

The Windows build uses a folder distribution with dynamically linked libraries.
Keep the entire extracted folder. Users may replace or rebuild compatible library
components under their respective licenses. The application's MIT license does
not prohibit reverse engineering or debugging needed to modify those libraries.

Corresponding upstream source for the current build's Qt for Python 6.9.1 family
is available from the official repositories; use the packaged build information to
verify the exact version before rebuilding:

- [PySide / Shiboken source, v6.9.1](https://code.qt.io/cgit/pyside/pyside-setup.git/?h=v6.9.1)
- [Qt source, v6.9.1](https://code.qt.io/cgit/qt/qt5.git/?h=v6.9.1)
- [PySide / Shiboken 6.9.1 source archives](https://download.qt.io/official_releases/QtForPython/pyside6/PySide6-6.9.1-src/)
- [Qt 6.9.1 source archives](https://download.qt.io/archive/qt/6.9/6.9.1/single/)
- [Qt for Python build instructions](https://doc.qt.io/qtforpython-6/building_from_source/index.html)
- [Qt build instructions](https://doc.qt.io/qt-6/build-sources.html)

To rebuild the application against a compatible replacement, install or build the
desired PySide6 / Shiboken6 version in a Python virtual environment, install the
other project dependencies, and run:

```powershell
python -m PyInstaller --noconfirm --clean ClaudeBridgePortable.spec
```

Use a separate output directory and distribute the resulting full application
folder, including its required library files and license notices.

FFmpeg / FFprobe, Windows OpenSSH, Ubuntu system packages, Codex CLI, and the Claude
client are external tools; they are not supplied as project source code. If a
distributor chooses to bundle any of them, their separate distribution terms must
also be followed.

OpenAI, Anthropic, Claude, Codex, DeepSeek, Windows, Ubuntu, and other product names
belong to their respective owners. Use of a name here does not imply sponsorship
or affiliation.
