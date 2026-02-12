# -*- mode: python ; coding: utf-8 -*-

a = Analysis(
    ['video_bitrate_viewer.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('lib/ffmpeg.exe', 'lib'),
        ('lib/ffprobe.exe', 'lib'),
    ],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='VideoBitrateViewer',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,  # No console window
    icon=None,      # Add 'icon.ico' if you have one
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    name='VideoBitrateViewer',
)