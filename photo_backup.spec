# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 用の設定ファイル（フォルダ配布版）。

    python -m PyInstaller --noconfirm --clean photo_backup.spec

dist\\PhotoBackup\\PhotoBackup.exe が出力されます。フォルダごと配布してください。
起動が速く、ウイルス対策ソフトの誤検知も起きにくい形式です。
1 ファイルにまとめたい場合は photo_backup_onefile.spec を使ってください。

FFmpeg は同梱しないため、動画圧縮を使う場合は別途導入が必要です。
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

# ネイティブ拡張を含むパッケージは丸ごと集める
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
    [],
    exclude_binaries=True,
    name="PhotoBackup",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="PhotoBackup",
)

