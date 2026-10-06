# -*- mode: python ; coding: utf-8 -*-
# 构建脚本: 去掉 openpyxl 的可选重依赖(numpy/pandas/scipy/PIL/lxml/matplotlib)
# 以及用不到的 Qt 模块, 显著瘦身。

a = Analysis(
    ['../发票助手Qt.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # openpyxl 的可选依赖, 程序里根本用不到(全部有 try/except 或延迟导入)
        'numpy', 'pandas', 'scipy', 'PIL', 'lxml', 'matplotlib',
        'contourpy', 'fontTools', 'dateutil', 'pytz', 'tzdata',
        # 连带被上面拖进来的
        'IPython', 'jax', 'sklearn', 'sympy', 'networkx', 'patsy',
        'statsmodels', 'seaborn', 'plotly', 'pytest', 'setuptools',
        'tkinter', 'PyQt5', 'PyQt6', 'qtpy', 'PySide2',
    ],
    noarchive=False,
    optimize=0,
)

# ---- 去掉用不到的二进制(Qt Quick/Qml/Pdf/Network/OpenGL/虚拟键盘等) ----
_DROP = (
    'opengl32sw',
    'qt6qml', 'qt6quick', 'qt6pdf', 'qt6network', 'qt6opengl',
    'qt6svg', 'qt6virtualkeyboard', 'qt6positioning',
    '\\qtqml.pyd', '\\qtquick.pyd', '\\qtnetwork.pyd', '\\qtsvg.pyd',
    '\\qtopengl.pyd', '\\qtpdf.pyd', '\\qtvirtualkeyboard.pyd', '\\qtdbus.pyd',
    'qtvirtualkeyboardplugin', 'qnetworklistmanager', 'plugins\\tls\\',
    'qsvgicon', 'imageformats\\qsvg', 'imageformats\\qpdf', 'qdirect2d',
    'pylupdate', 'pyrcc', 'pyside6-uic', 'shiboken6\\scripts',
)


def _keep(items):
    out = []
    for it in items:
        name = it[0].replace('/', '\\').lower()
        if any(k in name for k in _DROP):
            continue
        out.append(it)
    return out


a.binaries = _keep(a.binaries)
a.datas = _keep(a.datas)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='发票助手',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='fapiao.ico',
)
