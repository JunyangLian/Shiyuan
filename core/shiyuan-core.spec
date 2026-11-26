# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_data_files
from PyInstaller.utils.hooks import collect_dynamic_libs
from PyInstaller.utils.hooks import collect_submodules
from PyInstaller.utils.hooks import collect_all

datas = []
binaries = []
hiddenimports = ['shiyuan.api', 'shiyuan.mcp_server', 'rapidocr.inference_engine.onnxruntime', 'rapidocr.inference_engine.onnxruntime.main', 'rapidocr.inference_engine.onnxruntime.provider_config']
datas += collect_data_files('rapidocr')
datas += collect_data_files('onnxruntime')
binaries += collect_dynamic_libs('onnxruntime')
hiddenimports += collect_submodules('uvicorn')
hiddenimports += collect_submodules('winsdk')
tmp_ret = collect_all('pypdfium2')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


a = Analysis(
    ['sidecar.py'],
    pathex=['.'],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['onnx', 'torch', 'paddle', 'openvino', 'tensorrt', 'jupyter_client', 'ipykernel', 'zmq', 'PyQt5', 'matplotlib', 'pandas', 'tkinter'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='shiyuan-core',
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
    name='shiyuan-core',
)
