# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 用の設定ファイル（1 ファイル版）。

    python -m PyInstaller --noconfirm --clean photo_backup_onefile.spec

dist\\PhotoBackup.exe が 1 ファイルで出力されます。
起動のたびに一時フォルダへ展開するため、初回起動に数秒かかります。
一時フォルダへ書き込めない環境（一部の共有フォルダなど）では起動できません。
その場合は photo_backup.spec（フォルダ配布版）を使ってください。
"""

from PyInstaller.utils.hooks import collect_all

datas = []
binaries = []
hiddenimports = [
    "PIL._tkinter_finder",
    "scipy.fftpack",
    "scipy.special._ufuncs_cxx",
    "pywt",
]

for package in ("pillow_heif", "imagehash"):
    pkg_datas, pkg_binaries, pkg_hidden = collect_all(package)
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hidden

a = Analysis(
    ["photo_backup.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["matplotlib", "pandas", "pytest", "IPython", "notebook", "charset_normalizer"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="PhotoBackup",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

