# -*- mode: python ; coding: utf-8 -*-
# onedir 版构建脚本: 产物是 distqt\发票助手\ 文件夹(启动快, 免去 onefile
# 每次运行解压到 %TEMP% 的开销)。分发时把整个文件夹打成一个 7z/zip。
# 排除清单与 发票助手Qt.spec 保持一致(改动时两边同步)。

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
        # 纯本地应用: 不联网, ssl/libcrypto(约2MB)可以不要
        'ssl', '_ssl',
    ],
    noarchive=False,
    optimize=0,
)

_DROP = (
    'opengl32sw', 'd3dcompiler_47',
    'qt6qml', 'qt6quick', 'qt6pdf', 'qt6network', 'qt6opengl',
    'qt6svg', 'qt6virtualkeyboard', 'qt6positioning',
    '\\qtqml.pyd', '\\qtquick.pyd', '\\qtnetwork.pyd', '\\qtsvg.pyd',
    '\\qtopengl.pyd', '\\qtpdf.pyd', '\\qtvirtualkeyboard.pyd', '\\qtdbus.pyd',
    'qtvirtualkeyboardplugin', 'qnetworklistmanager', 'plugins\\tls\\',
    'qsvgicon', 'imageformats\\qsvg', 'imageformats\\qpdf', 'qdirect2d',
    'pylupdate', 'pyrcc', 'pyside6-uic', 'shiboken6\\scripts',
    # v2.1 追加: 程序只用 QImage/QPixmap 内存绘图, 不读任何图片文件,
    # 整个 imageformats 插件组(qjpeg/qtiff/qwebp/qicns...)都可以去掉;
    # 翻译 .qm 界面里没有; 这三个平台/输入插件真机用不到。
    'imageformats\\', 'translations\\',
    'libcrypto', 'libssl',
    'qminimal.dll', 'qoffscreen.dll', 'qtuiotouchplugin',
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
    [],
    exclude_binaries=True,
    name='发票助手',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=['qwindows.dll', 'Qt6Core.dll', 'Qt6Gui.dll',
                 'Qt6Widgets.dll', 'VCRUNTIME140.dll',
                 'VCRUNTIME140_1.dll'],
    console=False,
    icon='fapiao.ico',
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=['qwindows.dll', 'Qt6Core.dll', 'Qt6Gui.dll',
                 'Qt6Widgets.dll', 'VCRUNTIME140.dll',
                 'VCRUNTIME140_1.dll'],
    name='发票助手',
)
