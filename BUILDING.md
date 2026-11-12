# Windows build

The Windows binary is built on a GitHub-hosted Windows x64 runner.

## Automatic build

A push to `main` or a manual run from **Actions → Build Windows EXE** builds:

- `MCBE-StructureEditor.exe` (64-bit, one-file, GUI mode)
- `MCBE-StructureEditor-windows-x64.zip` (EXE + Windows instructions)

The ZIP is uploaded as a 30-day Actions artifact.

## GitHub Release

Push a tag beginning with `v` (for example `v0.1.0`). The same workflow builds the Windows ZIP and attaches it to a GitHub Release.

## Build locally on Windows

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-build.txt
python -m unittest discover -s tests -v
python -m PyInstaller --noconfirm --clean --onefile --windowed --name MCBE-StructureEditor mcstructure_merger_v2_features_bundle.py
```

The resulting executable is written to `dist\MCBE-StructureEditor.exe`.
