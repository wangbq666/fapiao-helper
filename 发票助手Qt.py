# -*- coding: utf-8 -*-
"""
发票助手 - 桌面版 (PySide6 / Qt)
================================
不依赖浏览器的独立窗口应用:
  左侧   : 选择文件夹列出文件, 单击看原件, 加入/移出/删除, 支持拖 PDF 进来
  右上   : 发票原件预览 —— 滚轮缩放 / 按住拖动 / 横竖滚动条 / 缩放滑块
  右下   : 报销明细表 + KPI 合计 + 按商品汇总 / 导出 CSV / 清空报销

解析复用 发票解析.py, PDF 渲染用 PyMuPDF, 全程本地不联网。
"""
import csv
import datetime
import json
import math
import os
import re
import sys
import threading
import time as _time

_T0 = _time.perf_counter()  # 冷启动计时起点(越靠前越准)

def _fitz():
    """延迟导入 PyMuPDF(约 0.11s), 让窗口先出来。"""
    import fitz as _m
    return _m

from PySide6.QtCore import (QEasingCurve, QObject, QMimeData, QPointF,
                            QPropertyAnimation, QRectF, QRunnable, Qt,
                            QSettings, QThreadPool, QTimer, Signal)
from PySide6.QtGui import (QBrush, QColor, QFont, QGuiApplication, QIcon,
                           QImage, QKeySequence, QPainter, QPainterPath, QPen,
                           QPixmap, QShortcut)
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QDialog, QFileDialog,
    QFormLayout, QFrame, QGraphicsOpacityEffect, QGraphicsScene,
    QGraphicsPixmapItem, QGraphicsView, QGridLayout, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMessageBox,
    QPushButton, QSlider, QSplitter, QStackedWidget, QTableWidget,
    QTableWidgetItem,
    QVBoxLayout, QWidget,
)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

MIME_ROWS = 'application/x-invoice-rows'

# ---------------- 本机设置(只存本地, 不联网) ----------------
# 测试可设 FAPIAO_SETTINGS=<ini路径> 把设置重定向, 避免污染本机注册表
SETTINGS = (QSettings(os.environ['FAPIAO_SETTINGS'],
                      QSettings.Format.IniFormat)
            if os.environ.get('FAPIAO_SETTINGS')
            else QSettings('FapiaoHelper', 'FapiaoHelper'))

# ---------------- 主题(浅色/深色) ----------------
THEMES = {
    'light': {
        'selRow': '#eaf2ff', 'totalRow': '#fffdf3', 'green': '#1a7f37',
        'red': '#cf222e', 'warn': '#9a6700', 'gray': '#8a9199',
        'dim': '#b1b6bb', 'accent': '#1f6feb', 'pvbg': '#33383f',
    },
    'dark': {
        'selRow': '#1f3a5f', 'totalRow': '#2a3040', 'green': '#3fb950',
        'red': '#f85149', 'warn': '#d29922', 'gray': '#8b949e',
        'dim': '#6e7681', 'accent': '#4493f8', 'pvbg': '#12151a',
    },
}
THEME = THEMES['light']


def _cfg_dark():
    return str(SETTINGS.value('dark', '0')) in ('1', 'true', 'True')


# ---------------- 报销清单持久化(重启恢复当前报销) ----------------
def session_save(paths):
    if os.environ.get('FAPIAO_NO_SESSION'):
        return
    SETTINGS.setValue('reimb_paths', '\n'.join(paths))
    SETTINGS.sync()


def session_load():
    if os.environ.get('FAPIAO_NO_SESSION'):
        return []
    v = SETTINGS.value('reimb_paths', '') or ''
    return [p for p in str(v).split('\n') if p]


def _dl_dir():
    """每台电脑的「下载」文件夹(默认 C:\\Users\\<用户名>\\Downloads)。"""
    d = ''
    try:
        import ctypes

        class GUID(ctypes.Structure):
            _fields_ = [('Data1', ctypes.c_uint32),
                        ('Data2', ctypes.c_uint16),
                        ('Data3', ctypes.c_uint16),
                        ('Data4', ctypes.c_ubyte * 8)]

        # FOLDERID_Downloads
        fid = GUID(0x374DE290, 0x123F, 0x4565,
                   (0x91, 0x64, 0x39, 0xC4, 0x92, 0x5E, 0x46, 0x7B))
        path = ctypes.c_wchar_p()
        if (ctypes.windll.shell32.SHGetKnownFolderPath(
                ctypes.byref(fid), 0, None, ctypes.byref(path)) == 0
                and path.value):
            d = path.value
            ctypes.windll.ole32.CoTaskMemFree(path)
    except Exception:  # noqa: BLE001
        d = ''
    if not d:
        d = os.path.join(os.path.expanduser('~'), 'Downloads')
    if not os.path.isdir(d):
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            d = os.path.expanduser('~')
    return d


def cfg_get(key, fallback=None):
    v = SETTINGS.value(key, fallback or '')
    v = str(v).strip() if v else ''
    return v if v and os.path.isdir(v) else (fallback or '')


def cfg_dir():
    """选择文件对话框的起始目录(默认「下载」)。"""
    return cfg_get('invoice_dir', _dl_dir())


def cfg_outdir():
    """CSV / 报告的默认保存目录(默认「下载」)。"""
    return cfg_get('out_dir', _dl_dir())


def cfg_reset():
    SETTINGS.clear()
    SETTINGS.sync()


# ---------------- 历史记录(只存加入报销的, 按加入那天分组, 跨重启保留) ----------
HIST_FILE = os.environ.get('FAPIAO_HIST') or os.path.join(
    os.environ.get('LOCALAPPDATA') or os.path.expanduser('~'),
    'FapiaoHelper', 'history.json')


def hist_load():
    try:
        with open(HIST_FILE, encoding='utf-8') as fh:
            items = json.load(fh).get('items') or []
        return [x for x in items
                if isinstance(x, dict) and x.get('day') and x.get('path')]
    except Exception:  # noqa: BLE001
        return []


def hist_save(items):
    try:
        os.makedirs(os.path.dirname(HIST_FILE), exist_ok=True)
        tmp = HIST_FILE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as fh:
            json.dump({'v': 1, 'items': items}, fh, ensure_ascii=False)
        os.replace(tmp, HIST_FILE)
    except Exception:  # noqa: BLE001
        pass


def _rate_num(rate):
    """把 '13%' / '1%' / 0.13 / '免税' / None 转成小数税率。"""
    if rate is None:
        return 0.0
    if isinstance(rate, (int, float)):
        v = float(rate)
        return v if v < 1 else v / 100.0
    s = str(rate).strip()
    if not s:
        return 0.0
    if s.endswith('%'):
        try:
            return float(s[:-1]) / 100.0
        except ValueError:
            return 0.0
    try:
        v = float(s)
    except ValueError:
        return 0.0
    return v if v < 1 else v / 100.0


def _fmt(n):
    return '' if n is None else '%.2f' % float(n)


def _esc(s):
    return '' if s is None else str(s)


def _summary_row(items):
    """把商品明细拼成一行摘要 (发票级 CSV 的'商品摘要'列)."""
    parts = []
    for it in items:
        name = it.get('名称') or ''
        if name.startswith('*') and name.count('*') >= 2:
            name = name.split('*', 2)[2]
        q = it.get('数量')
        if q:
            qs = str(int(q)) if float(q) == int(q) else ('%g' % q)
            seg = '%s×%s' % (name, qs)
        else:
            seg = name
        if it.get('金额') is not None:
            seg += ' %.2f' % it['金额']
        parts.append(seg)
    return '；'.join(parts)


def _elide_name(name, head=24, tail=12):
    """文件名过长时中间省略, 保留扩展名。"""
    name = str(name)
    if len(name) <= head + tail + 1:
        return name
    return '%s…%s' % (name[:head], name[-tail:])


def _warn_badge_pixmap():
    """打不开文件时显示的警告徽标: 红圈 + 感叹号。"""
    S = 56
    img = QImage(S, S, QImage.Format.Format_ARGB32)
    img.fill(QColor(0, 0, 0, 0))
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(QPen(QColor('#ffffff'), 4.0))
    p.setBrush(QColor('#e5484d'))
    p.drawEllipse(QRectF(4, 4, S - 8, S - 8))
    p.setPen(QColor('#ffffff'))
    f = QFont()
    f.setPixelSize(34)
    f.setBold(True)
    p.setFont(f)
    p.drawText(QRectF(4, 2, S - 8, S - 8),
               Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
               '!')
    p.end()
    return QPixmap.fromImage(img)


def _placeholder_pixmap():
    """空白预览时显示的大图标: 一沓发票本子(纯 QPainter 绘制)。"""
    W, H = 150, 164
    img = QImage(W, H, QImage.Format.Format_ARGB32)
    img.fill(QColor(0, 0, 0, 0))
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)

    def page(dx, dy, fill, border, w=104, h=128):
        p.setPen(QPen(QColor(border), 1.6))
        p.setBrush(QColor(fill))
        p.drawRoundedRect(QRectF(14 + dx, 12 + dy, w, h), 9, 9)

    # 后面两页(深色底上的浅灰层叠)
    page(26, 22, '#454b55', '#565d68')
    page(13, 11, '#565d68', '#6a727e')

    # 最上面一页(浅色纸张)
    page(0, 0, '#f4f6f9', '#d9dee5')
    x0, y0, w, h = 14.0, 12.0, 104.0, 128.0

    def bar(bx, by, bw, color, r=4.0, bh=8.0):
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(color))
        p.drawRoundedRect(QRectF(bx, by, bw, bh), r, r)

    bar(x0 + 12, y0 + 14, 46, '#1f6feb')          # 标题色带
    p.setPen(QPen(QColor('#e4e8ee'), 1.4))
    p.drawLine(int(x0 + 12), int(y0 + 34), int(x0 + w - 12), int(y0 + 34))
    bar(x0 + 12, y0 + 46, 68, '#d7dde4')          # 明细行
    bar(x0 + 12, y0 + 62, 78, '#d7dde4')
    bar(x0 + 12, y0 + 78, 56, '#d7dde4')
    bar(x0 + 12, y0 + 98, 40, '#9fc0ff')          # 合计行
    bar(x0 + w - 56, y0 + 98, 44, '#c9d1db')

    # ¥ 徽标(压在右下角)
    p.setPen(QPen(QColor('#1f6feb'), 3.0))
    p.setBrush(QColor('#1f6feb'))
    p.drawEllipse(QRectF(94, 118, 44, 44))
    p.setPen(QColor('#ffffff'))
    f = QFont()
    f.setPixelSize(26)
    f.setBold(True)
    p.setFont(f)
    p.drawText(QRectF(94, 118, 44, 44),
               Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
               '¥')
    p.end()
    return QPixmap.fromImage(img)


# ======================================================================
#  图标工厂: 全部 QPainter 手绘(不同 Windows 版本上 emoji 渲染不一致)
# ======================================================================
def _icon(kind, size=18, color='#8a9199'):
    img = QImage(size, size, QImage.Format.Format_ARGB32)
    img.fill(QColor(0, 0, 0, 0))
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    c = QColor(color)
    pen = QPen(c, max(1.4, size * 0.08))
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    s = float(size)

    def bars(x0, y0, w, h, r=1.5):
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(c)
        p.drawRoundedRect(QRectF(x0, y0, w, h), r, r)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)

    if kind == 'doc':                    # 单页文档 + 文字行
        r = QRectF(s * 0.16, s * 0.10, s * 0.68, s * 0.80)
        p.drawRoundedRect(r, s * 0.10, s * 0.10)
        for k, fy in enumerate((0.32, 0.52, 0.72)):
            y = r.y() + r.height() * fy
            p.drawLine(QPointF(r.x() + r.width() * 0.22, y),
                       QPointF(r.x() + r.width() * (0.72 if k < 2 else 0.78), y))
    elif kind == 'receipt':              # 锯齿底边小票
        path = QPainterPath()
        x0, y0, w, h = s * 0.20, s * 0.12, s * 0.60, s * 0.76
        path.moveTo(x0, y0)
        path.lineTo(x0 + w, y0)
        path.lineTo(x0 + w, y0 + h - s * 0.10)
        zig = w / 4
        for i in range(4):
            path.lineTo(x0 + w - zig * (i + 0.5), y0 + h)
            path.lineTo(x0 + w - zig * (i + 1), y0 + h - s * 0.10)
        path.closeSubpath()
        p.drawPath(path)
        for fy in (0.36, 0.56):
            p.drawLine(QPointF(x0 + w * 0.25, y0 + h * fy),
                       QPointF(x0 + w * 0.75, y0 + h * fy))
    elif kind == 'clock':
        p.drawEllipse(QRectF(s * 0.14, s * 0.14, s * 0.72, s * 0.72))
        p.drawLine(QPointF(s * 0.50, s * 0.30), QPointF(s * 0.50, s * 0.52))
        p.drawLine(QPointF(s * 0.50, s * 0.52), QPointF(s * 0.66, s * 0.62))
    elif kind == 'gear':                 # 滑杆式设置图标(比齿轮好画)
        for i, fy in enumerate((0.28, 0.50, 0.72)):
            y = s * fy
            p.drawLine(QPointF(s * 0.18, y), QPointF(s * 0.82, y))
            kx = (0.62, 0.34, 0.66)[i]
            bars(s * kx - s * 0.09, y - s * 0.09, s * 0.18, s * 0.18, s * 0.09)
    elif kind == 'moon':
        path = QPainterPath()
        path.addEllipse(QRectF(s * 0.16, s * 0.10, s * 0.74, s * 0.74))
        path.addEllipse(QRectF(s * 0.36, s * 0.00, s * 0.68, s * 0.80))
        path.setFillRule(Qt.FillRule.OddEvenFill)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(c)
        p.drawPath(path)
    elif kind == 'sun':
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(c)
        p.drawEllipse(QRectF(s * 0.32, s * 0.32, s * 0.36, s * 0.36))
        p.setPen(pen)
        for a in range(8):
            rad = math.pi / 4 * a
            p.drawLine(QPointF(s * 0.5 + math.cos(rad) * s * 0.30,
                               s * 0.5 + math.sin(rad) * s * 0.30),
                       QPointF(s * 0.5 + math.cos(rad) * s * 0.42,
                               s * 0.5 + math.sin(rad) * s * 0.42))
    elif kind == 'plus':
        p.drawLine(QPointF(s * 0.5, s * 0.20), QPointF(s * 0.5, s * 0.80))
        p.drawLine(QPointF(s * 0.20, s * 0.5), QPointF(s * 0.80, s * 0.5))
    elif kind == 'search':
        p.drawEllipse(QRectF(s * 0.16, s * 0.16, s * 0.46, s * 0.46))
        p.drawLine(QPointF(s * 0.58, s * 0.58), QPointF(s * 0.82, s * 0.82))
    elif kind == 'chart':
        base = s * 0.82
        for bx, bh in ((0.20, 0.28), (0.44, 0.50), (0.68, 0.38)):
            bars(s * bx - s * 0.07, base - s * bh, s * 0.14, s * bh)
    p.end()
    return QIcon(QPixmap.fromImage(img))


class _SortItem(QTableWidgetItem):
    """带隐藏排序键的单元格: 表头点击排序时按 key 比, 而不是显示文本。"""

    def __init__(self, text, key=None):
        super().__init__(text)
        self._key = key

    def __lt__(self, other):
        # 注意: 不能调 super().__lt__(), PySide6 上会段错误
        a = self._key if self._key is not None else self.text()
        b = getattr(other, '_key', None)
        if b is None:
            b = other.text() if hasattr(other, 'text') else ''
        try:
            return a < b
        except TypeError:
            return str(a) < str(b)


# ======================================================================
#  PDF 预览视图: 滚轮缩放 / 按住拖动 / 原生滚动条 / 高清重渲染
#  (文档句柄常驻复用; 重渲染走后台线程, 只重画视口内的页)
# ======================================================================
class RenderSignals(QObject):
    done = Signal(str, int, float, QImage)      # path, pageIdx, scale, img


class RenderTask(QRunnable):
    """后台渲染一页: 渲染期间 GUI 线程不被阻塞。"""

    def __init__(self, view, path, idx, scale):
        super().__init__()
        self.view, self.path, self.idx, self.scale = view, path, idx, scale
        self.sig = RenderSignals()

    def run(self):
        img = self.view._render_page(self.idx, self.scale)
        self.sig.done.emit(self.path, self.idx, self.scale, img)


class PDFView(QGraphicsView):
    zoomChanged = Signal(float)
    hasDoc = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self._pages = []
        self._path = None
        self._doc = None                 # 常驻文档句柄(避免每次渲染重开文件)
        self._doc_lock = threading.Lock()
        self._inflight = set()           # 正在后台渲染的 (idx, scale)
        self._fit = 1.0
        self._zoom = 1.0
        self._panning = False
        self._pan_last = None
        self._fit_pending = None      # load() 期间暂存的页面最大宽

        self._hint_lbl = QLabel('', self.viewport())
        self._hint_lbl.setObjectName('pvempty')
        self._hint_lbl.setAlignment(Qt.AlignmentFlag.AlignHCenter |
                                    Qt.AlignmentFlag.AlignVCenter)
        self._hint_lbl.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._hint_lbl.setWordWrap(True)
        self._hint_lbl.hide()

        # 空白时的大图标: 一沓发票本子(纯代码绘制, 不依赖图片资源)
        self._ph_pixmap = _placeholder_pixmap()
        self._ph_lbl = QLabel(self.viewport())
        self._ph_lbl.setPixmap(self._ph_pixmap)
        self._ph_lbl.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._ph_lbl.hide()

        # 打不开文件时叠加的警告徽标
        self._warn_lbl = QLabel(self.viewport())
        self._warn_lbl.setPixmap(_warn_badge_pixmap())
        self._warn_lbl.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._warn_lbl.hide()

        self._rt = QTimer(self)
        self._rt.setSingleShot(True)
        self._rt.setInterval(200)
        self._rt.timeout.connect(self._rerender)

        # 滚动时把新滚进视口的"脏页"补渲染(80ms 去抖)
        self._vt = QTimer(self)
        self._vt.setSingleShot(True)
        self._vt.setInterval(80)
        self._vt.timeout.connect(self._ensure_visible)

        self.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        self.setBackgroundBrush(QColor(THEME['pvbg']))
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.SmartViewportUpdate)
        sb = self.verticalScrollBar()
        sb.valueChanged.connect(lambda _: self._vt.start())

    def set_warn(self, on):
        """打不开文件时显示警告徽标。"""
        self._warn_lbl.setVisible(bool(on))
        if on:
            self._warn_lbl.raise_()
        self._layout_hint()

    @property
    def hint(self):
        return self._hint_lbl.text()

    @hint.setter
    def hint(self, s):
        self._hint_lbl.setText(s or '')
        self._hint_lbl.setVisible(bool(s))
        self._ph_lbl.setVisible(bool(s))
        self._hint_lbl.raise_()
        self._ph_lbl.raise_()
        self._layout_hint()

    def _layout_hint(self):
        vp = self.viewport().rect()
        if not self._ph_lbl.isVisible():
            self._hint_lbl.setGeometry(vp)
            return

        pm = self._ph_pixmap
        iw, ih = pm.width(), pm.height()
        gap, text_h = 10, 46
        # 视口太矮时整体等比缩小图标
        if ih + gap + text_h + 8 > vp.height():
            k = max(0.45, (vp.height() - gap - text_h - 8) / float(ih))
            iw, ih = int(iw * k), int(ih * k)
            self._ph_lbl.setPixmap(pm.scaled(
                iw, ih, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation))
        else:
            self._ph_lbl.setPixmap(pm)

        top = max(4, (vp.height() - ih - gap - text_h) // 2)
        x = (vp.width() - iw) // 2
        self._ph_lbl.setGeometry(x, top, iw, ih)
        y2 = top + ih + gap
        self._hint_lbl.setGeometry(0, y2, vp.width(),
                                   max(text_h, vp.height() - y2))
        if self._warn_lbl.isVisible():
            self._warn_lbl.setGeometry(x + iw - 40, max(0, top - 8), 56, 56)

    # ---------------- 文档 ----------------
    def clear(self):
        # 先等后台渲染用完句柄再关文档, 避免渲染线程访问已关闭的文档
        with self._doc_lock:
            doc, self._doc = self._doc, None
        if doc:
            try:
                doc.close()
            except Exception:  # noqa: BLE001
                pass
        self._scene.clear()
        self._pages = []
        self._inflight.clear()
        self._path = None
        self._zoom = 1.0
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.hasDoc.emit(False)

    def load(self, path):
        self.clear()
        try:
            fz = _fitz()
            doc = fz.open(path)
            rects = [(p.rect.width, p.rect.height) for p in doc]
        except Exception:
            return False
        if not rects:
            doc.close()
            return False
        self._path = path
        self._doc = doc                    # 句柄常驻, 重渲染不再重开文件
        self._fit_pending = max(w for w, _ in rects)
        self._compute_fit()
        gap = 14.0
        s0 = self._render_scale()
        y = 0.0
        for i, (w, h) in enumerate(rects):
            img = self._render_page(i, s0)
            if img.isNull():
                continue
            item = QGraphicsPixmapItem(QPixmap.fromImage(img))
            item.setTransformationMode(Qt.TransformationMode.SmoothTransformation)
            item.setPos(0, y)
            item.setScale(1.0 / s0)
            self._scene.addItem(item)
            self._pages.append({'w': w, 'h': h, 'scale': s0, 'item': item,
                                'idx': i, 'dirty': False})
            y += h + gap
        if not self._pages:
            return False
        self._scene.setSceneRect(0, 0,
                                 max(p['w'] for p in self._pages),
                                 max(0.0, y - gap))
        self._apply_transform()
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.hint = ''
        self.viewport().update()
        self.hasDoc.emit(True)
        return True

    def _dpr(self):
        return float(self.devicePixelRatio())

    def _compute_fit(self):
        w = self._fit_pending if self._fit_pending else (
            max(p['w'] for p in self._pages) if self._pages else 0)
        if w <= 0:
            return
        self._fit = max(0.05, (self.viewport().width() - 40) / w)

    def _render_scale(self):
        return max(0.3, min(6.0, self._fit * self._zoom * self._dpr()))

    def _render_page(self, idx, scale):
        try:
            with self._doc_lock:
                if self._doc is None:
                    return QImage()
                pix = self._doc[idx].get_pixmap(
                    matrix=_fitz().Matrix(scale, scale))
            return QImage(pix.samples, pix.width, pix.height, pix.stride,
                          QImage.Format.Format_RGB888).copy()
        except Exception:  # noqa: BLE001
            return QImage()

    def _visible_idx(self):
        """当前视口能看到的页码集合(只重渲染可见页, 控内存)。"""
        try:
            vr = self.mapToScene(self.viewport().rect()).boundingRect()
        except Exception:  # noqa: BLE001
            return set(range(len(self._pages)))
        return {p['idx'] for p in self._pages
                if p['item'].sceneBoundingRect().intersects(vr)}

    def _apply_transform(self):
        self.resetTransform()
        k = self._fit * self._zoom
        self.scale(k, k)

    def set_zoom(self, z, emit=True):
        z = max(0.25, min(4.0, float(z)))
        if abs(z - self._zoom) < 1e-6:
            return
        self._zoom = z
        self._apply_transform()
        if emit:
            self.zoomChanged.emit(z)
        self._rt.start()

    def fit_width(self):
        if not self._pages:
            return
        self._fit_pending = None
        self._compute_fit()
        self._zoom = 1.0
        self._apply_transform()
        self.zoomChanged.emit(1.0)
        self._rt.start()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._layout_hint()
        if self._pages:
            self._fit_pending = None
            self._compute_fit()
            self._apply_transform()
            self._rt.start()

    def _rerender(self):
        """高清重渲染: 可见页投给后台线程, 视口外先标脏, 滚过去再补。"""
        if not self._pages or self._doc is None:
            return
        target = self._render_scale()
        vis = self._visible_idx()
        for p in self._pages:
            if abs(p['scale'] - target) < 0.12:
                p['dirty'] = False
                continue
            if (p['idx'], round(target, 3)) in self._inflight:
                continue
            if p['idx'] not in vis:
                p['dirty'] = True
                continue
            self._start_render(p, target)

    def _start_render(self, p, target):
        self._inflight.add((p['idx'], round(target, 3)))
        task = RenderTask(self, self._path, p['idx'], target)
        task.sig.done.connect(self._on_rendered)
        QThreadPool.globalInstance().start(task)

    def _on_rendered(self, path, idx, scale, img):
        self._inflight.discard((idx, round(scale, 3)))
        if self._doc is None or path != self._path or img.isNull():
            return
        p = next((q for q in self._pages if q['idx'] == idx), None)
        if p is None:
            return
        target = self._render_scale()
        # 只在结果确实比现有贴图更接近目标缩放时才替换
        if abs(scale - target) <= abs(p['scale'] - target):
            p['item'].setPixmap(QPixmap.fromImage(img))
            p['item'].setScale(1.0 / scale)
            p['scale'] = scale
            p['dirty'] = abs(scale - target) >= 0.12
        if not self._vt.isActive() and any(q['dirty'] for q in self._pages):
            self._rt.start()       # 一页画完, 顺带看看还有没有别的可见脏页

    def _ensure_visible(self):
        """滚动/缩放后补渲染视口内的脏页。"""
        if not self._pages or self._doc is None:
            return
        target = self._render_scale()
        vis = self._visible_idx()
        for p in self._pages:
            if (p['idx'] in vis and p['dirty']
                    and (p['idx'], round(target, 3)) not in self._inflight):
                self._start_render(p, target)

    # ---------------- 交互 ----------------
    def wheelEvent(self, e):
        if not self._pages:
            super().wheelEvent(e)
            return
        d = e.angleDelta().y()
        if d == 0:
            super().wheelEvent(e)
            return
        self.set_zoom(self._zoom * (1.12 if d > 0 else 1 / 1.12))
        e.accept()

    def mousePressEvent(self, e):
        if self._pages and e.button() == Qt.MouseButton.LeftButton:
            self._panning = True
            self._pan_last = e.position()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            e.accept()
            return
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self._panning and self._pan_last is not None:
            d = e.position() - self._pan_last
            self._pan_last = e.position()
            self.horizontalScrollBar().setValue(
                int(self.horizontalScrollBar().value() - d.x()))
            self.verticalScrollBar().setValue(
                int(self.verticalScrollBar().value() - d.y()))
            e.accept()
            return
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        if self._panning:
            self._panning = False
            self._pan_last = None
            self.setCursor(Qt.CursorShape.OpenHandCursor)
            e.accept()
            return
        super().mouseReleaseEvent(e)


# ======================================================================
#  左侧文件表: 桌面 PDF 可拖入, 行可拖到右下明细区
# ======================================================================
class FileTable(QTableWidget):
    filesDropped = Signal(list)
    COLS = ['文件', '状态', '金额', '操作']

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setColumnCount(len(self.COLS))
        self.setHorizontalHeaderLabels(self.COLS)
        self.verticalHeader().setVisible(False)
        self.verticalHeader().setDefaultSectionSize(36)
        self.setShowGrid(False)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setAlternatingRowColors(True)
        self.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.setAcceptDrops(True)
        self.setDragEnabled(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragOnly)
        hdr = self.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for c in (1, 2, 3):
            hdr.setSectionResizeMode(c, QHeaderView.ResizeMode.ResizeToContents)
        # 空态提示(列表没文件时居中显示)
        self._hint = QLabel('点「＋选择文件」添加发票\n也可以把 PDF/OFD 拖到这里',
                            self.viewport())
        self._hint.setObjectName('filehint')
        self._hint.setAlignment(Qt.AlignmentFlag.AlignHCenter |
                                Qt.AlignmentFlag.AlignVCenter)
        self._hint.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._hint.hide()

    def set_empty(self, empty):
        if empty:
            self._hint.show()
            self._layout_hint()
            self._hint.raise_()
        else:
            self._hint.hide()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if self._hint.isVisible():
            self._layout_hint()

    def _layout_hint(self):
        self._hint.setGeometry(0, 0, self.viewport().width(),
                               self.viewport().height())

    def mimeTypes(self):
        return [MIME_ROWS]

    def mimeData(self, indexes):
        rows = sorted({i.row() for i in indexes})
        md = QMimeData()
        md.setData(MIME_ROWS, ','.join(str(r) for r in rows).encode('utf-8'))
        md.setText(','.join(str(r) for r in rows))
        return md

    def supportedDragActions(self):
        return Qt.DropAction.CopyAction

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls() or e.mimeData().hasFormat(MIME_ROWS):
            e.acceptProposedAction()

    def dragMoveEvent(self, e):
        if e.mimeData().hasUrls() or e.mimeData().hasFormat(MIME_ROWS):
            e.acceptProposedAction()

    def dropEvent(self, e):
        if e.mimeData().hasUrls():
            paths = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
            paths = [p for p in paths
                     if p.lower().endswith(('.pdf', '.ofd'))]
            if paths:
                self.filesDropped.emit(paths)
                e.acceptProposedAction()


class DetailTable(QTableWidget):
    rowsDropped = Signal(list)
    filesDropped = Signal(list)

    COLS = ['商品名称', '规格型号', '数量', '单价', '金额', '税率', '税额',
            '价税合计', '来源']

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setColumnCount(len(self.COLS))
        self.setHorizontalHeaderLabels(self.COLS)
        self.verticalHeader().setVisible(False)
        self.verticalHeader().setDefaultSectionSize(34)
        self.setShowGrid(False)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.setAlternatingRowColors(True)
        self.setAcceptDrops(True)
        self.setSortingEnabled(True)
        hdr = self.horizontalHeader()
        for c in (1, 2, 3, 4, 5, 6, 7):
            hdr.setSectionResizeMode(c, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(8, QHeaderView.ResizeMode.Interactive)
        hdr.resizeSection(8, 210)

    def dragEnterEvent(self, e):
        if e.mimeData().hasFormat(MIME_ROWS) or e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dragMoveEvent(self, e):
        if e.mimeData().hasFormat(MIME_ROWS) or e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        if e.mimeData().hasFormat(MIME_ROWS):
            txt = bytes(e.mimeData().data(MIME_ROWS)).decode('utf-8')
            rows = [int(x) for x in txt.split(',') if x]
            if rows:
                self.rowsDropped.emit(rows)
            e.acceptProposedAction()
            return
        paths = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
        paths = [p for p in paths
                 if p.lower().endswith(('.pdf', '.ofd'))]
        if paths:
            self.filesDropped.emit(paths)
            e.acceptProposedAction()


# ======================================================================
#  后台解析线程
# ======================================================================
class ParseSignals(QObject):
    done = Signal(str, dict)


class ParseTask(QRunnable):
    def __init__(self, path):
        super().__init__()
        self.path = path
        self.sig = ParseSignals()

    def run(self):
        from 发票解析 import extract_invoice  # 延迟导入, 冷启动少 0.1s+
        try:
            inv = extract_invoice(self.path)
        except Exception as e:  # noqa: BLE001
            inv = {'文件名': os.path.basename(self.path), '金额': None,
                   '税额': None, '价税合计': None, '备注': '解析失败: %s' % e,
                   '明细': []}
        self.sig.done.emit(self.path, inv)


# ======================================================================
#  设置对话框
# ======================================================================
class SettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('settings')
        self.setWindowTitle('设置')
        self.setModal(True)
        self.setFixedWidth(470)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 14)
        lay.setSpacing(12)

        t = QLabel('设置')
        t.setObjectName('dlgTitle')
        lay.addWidget(t)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight |
                               Qt.AlignmentFlag.AlignVCenter)
        form.setContentsMargins(0, 0, 0, 0)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(12)

        # 1) 默认发票目录
        self.ed_dir = QLineEdit(cfg_dir())
        row1 = QHBoxLayout()
        row1.setSpacing(6)
        row1.addWidget(self.ed_dir, 1)
        b1 = QPushButton('浏览…')
        b1.setObjectName('ghost')
        b1.clicked.connect(lambda: self._browse(self.ed_dir))
        row1.addWidget(b1)
        form.addRow('默认发票目录', row1)

        # 2) 报告保存目录
        self.ed_out = QLineEdit(cfg_outdir())
        row2 = QHBoxLayout()
        row2.setSpacing(6)
        row2.addWidget(self.ed_out, 1)
        b2 = QPushButton('浏览…')
        b2.setObjectName('ghost')
        b2.clicked.connect(lambda: self._browse(self.ed_out))
        row2.addWidget(b2)
        form.addRow('报告保存目录', row2)

        # 3) 价格是否含税
        self.ck_incl = QCheckBox('价格按含税价计算')
        self.ck_incl.setChecked(
            str(SETTINGS.value('price_incl', '0')) in ('1', 'true', 'True'))
        form.addRow('价格口径', self.ck_incl)
        lay.addLayout(form)

        note = QLabel(
            '说明：价格默认不含税（与票面“金额”一致，直接使用）。\n'
            '勾选“价格按含税价计算”后，明细里的金额被视为含税价，按税率反推：\n'
            '  不含税金额 = 含税价 ÷ (1 + 税率)\n'
            '  税额 = 含税价 − 不含税金额\n'
            '  价税合计 = 含税价\n'
            '合计、按商品汇总、CSV 与 Excel 报告会同时按该口径重算。')
        note.setObjectName('tip')
        note.setWordWrap(True)
        lay.addWidget(note)

        tip = QLabel('设置只保存在本机, 文件始终在本地解析。')
        tip.setObjectName('tip')
        lay.addWidget(tip)
        lay.addStretch(1)

        btns = QHBoxLayout()
        btns.setSpacing(8)
        b_reset = QPushButton('重置默认设置')
        b_reset.setObjectName('ghost')
        b_reset.clicked.connect(self._reset)
        btns.addWidget(b_reset)
        btns.addStretch(1)
        b_cancel = QPushButton('取消')
        b_cancel.setObjectName('ghost')
        b_cancel.clicked.connect(self.reject)
        btns.addWidget(b_cancel)
        b_ok = QPushButton('保存')
        b_ok.clicked.connect(self.accept)
        btns.addWidget(b_ok)
        lay.addLayout(btns)

    def _browse(self, ed):
        d = QFileDialog.getExistingDirectory(self, '选择文件夹', ed.text())
        if d:
            ed.setText(d)

    def _reset(self):
        SETTINGS.clear()
        SETTINGS.sync()
        self.ed_dir.setText(_dl_dir())
        self.ed_out.setText(_dl_dir())
        self.ck_incl.setChecked(False)

    def values(self):
        d = self.ed_dir.text().strip() or _dl_dir()
        o = self.ed_out.text().strip() or d
        return d, o, self.ck_incl.isChecked()


# ======================================================================
#  主窗口
# ======================================================================
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle('发票助手')
        self._apply_default_size()
        self.setWindowIcon(self._make_icon())
        self.files = []
        self._seq = 0
        self._by_item = False
        self.price_incl = str(SETTINGS.value('price_incl', '0')) in ('1', 'true', 'True')
        self.history = hist_load()     # 历史记录(跨重启保留)
        self._hist_on = False          # 当前是否在历史视图
        self._hist_day = None          # 选中的那天
        self._hist_days = []           # 日期列表(新→旧)
        self._suspend = 0          # >0 时暂停表格刷新(批量操作用)
        self._defer = QTimer(self)  # 解析完成的刷新合并, 一次事件循环只刷一遍
        self._defer.setSingleShot(True)
        self._defer.setInterval(120)
        self._defer.timeout.connect(self._deferred_refresh)
        self._cur_id = None
        self._expanded_fid = None     # 左栏点开的发票(展开行显示号码/日期)
        self._pool = QThreadPool.globalInstance()
        self._restoring = False       # 恢复报销清单时不重复记历史
        self._build_ui()
        self._sync_incl_btn()
        self._sync_hist_btn()
        self._apply_theme(_cfg_dark())
        self.refresh_files()
        self.refresh_detail()
        self._setup_shortcuts()
        self._restore_session()

    def _restore_session(self):
        """恢复上次退出时的报销清单(已不在磁盘的文件自动跳过)。"""
        paths = [p for p in session_load() if os.path.isfile(p)]
        session_save(paths)          # 先把缺文件的条目从清单里清掉
        if paths:
            self._restoring = True
            try:
                self.add_paths(paths, join=True)
            finally:
                self._restoring = False

    def _setup_shortcuts(self):
        QShortcut(QKeySequence('Ctrl+O'), self, activated=self.pick_files)
        QShortcut(QKeySequence('Ctrl+F'), self, activated=self._focus_search)
        QShortcut(QKeySequence('Escape'), self, activated=self._esc_back)
        # Del 只在文件表获得焦点时触发, 不干扰搜索框里的文本编辑
        sc = QShortcut(QKeySequence('Delete'), self.file_table)
        sc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        sc.activated.connect(self._del_current_row)

    def _focus_search(self):
        (self.search_hist if self._hist_on else self.search_files).setFocus()
        (self.search_hist if self._hist_on else self.search_files).selectAll()

    def _esc_back(self):
        if self._hist_on:
            self.toggle_history()

    def _del_current_row(self):
        r = self.file_table.currentRow()
        it = self.file_table.item(r, 0) if r >= 0 else None
        if it:
            self.remove_by_id(it.data(Qt.ItemDataRole.UserRole))

    def _apply_theme(self, dark):
        """切主题: 重生成 QSS + 刷新所有在代码里上色的单元格。"""
        global THEME
        THEME = THEMES['dark' if dark else 'light']
        QApplication.instance().setStyleSheet(build_qss(dark))
        self.btn_theme.setToolTip('切换到%s色模式'
                                  % ('浅' if dark else '深'))
        self.btn_theme.setIcon(_icon('sun' if dark else 'moon', 17, '#ffffff'))
        self.pdf_view.setBackgroundBrush(QColor(THEME['pvbg']))
        self.refresh_files()
        self.refresh_detail()
        if self._hist_on:
            self._hist_refresh_items()

    def toggle_theme(self):
        dark = not _cfg_dark()
        SETTINGS.setValue('dark', '1' if dark else '0')
        SETTINGS.sync()
        self._apply_theme(dark)

    def _apply_default_size(self):
        """默认 1080x720 居中; 不锁死尺寸, 右上角最大化/拉伸都可用。"""
        scr = self.screen() or QGuiApplication.primaryScreen()
        av = scr.availableGeometry()
        w, h = min(1080, av.width()), min(720, av.height())
        self.setMinimumSize(880, 560)
        self.resize(w, h)
        self.move(av.x() + (av.width() - w) // 2,
                  av.y() + (av.height() - h) // 2)

    @staticmethod
    def _make_icon(size=64):
        """应用图标: 白底圆角卡片 + 蓝色大 ¥。"""
        img = QImage(size, size, QImage.Format.Format_ARGB32)
        img.fill(QColor(0, 0, 0, 0))
        p = QPainter(img)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        m = size * 0.04
        p.setPen(QPen(QColor('#cfe0ff'), max(1.0, size * 0.055)))
        p.setBrush(QColor('#ffffff'))
        p.drawRoundedRect(QRectF(m, m, size - 2 * m, size - 2 * m),
                          size * 0.24, size * 0.24)
        f = QFont()
        f.setPixelSize(int(size * 0.80))
        f.setBold(True)
        p.setFont(f)
        p.setPen(QColor('#1f6feb'))
        p.drawText(QRectF(0, -size * 0.02, size, size),
                   Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
                   '¥')
        p.end()
        return QIcon(QPixmap.fromImage(img))

    # ---------------- 构建界面 ----------------
    def _build_ui(self):
        root = QWidget()
        self.setCentralWidget(root)
        vbox = QVBoxLayout(root)
        vbox.setContentsMargins(0, 0, 0, 0)
        vbox.setSpacing(0)
        vbox.addWidget(self._header())

        sp = QSplitter(Qt.Orientation.Horizontal)
        sp.setChildrenCollapsible(False)
        sp.addWidget(self._left_panel())
        sp.addWidget(self._right_panel())
        sp.setSizes([350, 730])
        sp.setStretchFactor(0, 0)
        sp.setStretchFactor(1, 1)
        vbox.addWidget(sp, 1)

    def _header(self):
        bar = QFrame()
        bar.setObjectName('header')
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(18, 0, 18, 0)
        lay.setSpacing(14)
        ic = QLabel()
        ic.setPixmap(_icon('receipt', 22, '#ffffff').pixmap(22, 22))
        ic.setStyleSheet('background: transparent;')
        lay.addWidget(ic)
        t = QLabel('发票助手')
        t.setObjectName('title')
        lay.addWidget(t)
        self.lbl_sum = QLabel('报销 0 张 ｜ 价税合计 0.00')
        self.lbl_sum.setObjectName('sum')
        lay.addWidget(self.lbl_sum)
        lay.addStretch(1)
        self.btn_hist = QPushButton('历史记录')
        self.btn_hist.setObjectName('headbtn')
        self.btn_hist.setCheckable(True)
        self.btn_hist.setIcon(_icon('clock', 15, '#ffffff'))
        self.btn_hist.setToolTip('按天查看已确认报销的发票（只存加入报销的）')
        self.btn_hist.clicked.connect(self.toggle_history)
        lay.addWidget(self.btn_hist)
        tip = QLabel('本地解析 · 文件不上传')
        tip.setObjectName('headtip')
        lay.addWidget(tip)
        self.btn_theme = QPushButton()
        self.btn_theme.setObjectName('gear')
        self.btn_theme.setFixedSize(30, 30)
        self.btn_theme.setToolTip('切换深色模式')
        self.btn_theme.clicked.connect(self.toggle_theme)
        lay.addWidget(self.btn_theme)
        gear = QPushButton()
        gear.setObjectName('gear')
        gear.setFixedSize(30, 30)
        gear.setIcon(_icon('gear', 17, '#ffffff'))
        gear.setToolTip('设置')
        gear.clicked.connect(self.open_settings)
        lay.addWidget(gear)
        bar.setFixedHeight(52)
        return bar

    def open_settings(self):
        d = SettingsDialog(self)
        if d.exec():
            inv_dir, out_dir, incl = d.values()
            SETTINGS.setValue('invoice_dir', inv_dir)
            SETTINGS.setValue('out_dir', out_dir)
            SETTINGS.sync()
            self.set_price_incl(incl)
            self.statusBar().showMessage('设置已保存', 3000)

    def _left_panel(self):
        pan = QFrame()
        pan.setObjectName('panel')
        lay = QVBoxLayout(pan)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(9)
        self.left_stack = QStackedWidget()
        self.left_stack.addWidget(self._file_page())      # 0: 文件列表
        self.left_stack.addWidget(self._history_page())   # 1: 历史记录
        lay.addWidget(self.left_stack, 1)
        return pan

    @staticmethod
    def _search_box(placeholder):
        ed = QLineEdit()
        ed.setObjectName('searchbox')
        ed.setPlaceholderText(placeholder)
        ed.setClearButtonEnabled(True)
        ed.addAction(_icon('search', 14, '#8a9199'),
                     QLineEdit.ActionPosition.LeadingPosition)
        return ed

    def _file_page(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(9)

        h = QHBoxLayout()
        h.addWidget(QLabel('文件'))
        h.itemAt(0).widget().setObjectName('panelTitle')
        tip = QLabel('单击看原件 · 拖进来即加入报销')
        tip.setObjectName('tip')
        h.addWidget(tip)
        h.addStretch(1)
        lay.addLayout(h)

        self.search_files = self._search_box(
            '搜索: 文件名 / 发票号码 / 销售方 / 日期')
        self.search_files.textChanged.connect(lambda _: self.refresh_files())
        lay.addWidget(self.search_files)

        r1 = QHBoxLayout()
        r1.setSpacing(7)
        self.btn_addfile = QPushButton('选择文件')
        self.btn_addfile.setIcon(_icon('plus', 14, '#ffffff'))
        self.btn_addfile.clicked.connect(self.pick_files)
        r1.addWidget(self.btn_addfile)
        self.lbl_cnt = QLabel('0 个')
        self.lbl_cnt.setObjectName('badge')
        r1.addWidget(self.lbl_cnt)
        lay.addLayout(r1)

        r2 = QHBoxLayout()
        r2.setSpacing(7)
        b = QPushButton('全部加入')
        b.clicked.connect(self.join_all)
        r2.addWidget(b)
        b = QPushButton('全部移出')
        b.setObjectName('ghost')
        b.clicked.connect(self.leave_all)
        r2.addWidget(b)
        b = QPushButton('全部删除')
        b.setObjectName('ghost')
        b.clicked.connect(self.remove_all)
        r2.addWidget(b)
        lay.addLayout(r2)

        self.file_table = FileTable()
        self.file_table.filesDropped.connect(lambda ps: self.add_paths(ps, join=True))
        self.file_table.cellClicked.connect(self.on_file_clicked)
        lay.addWidget(self.file_table, 1)
        return page

    def on_file_clicked(self, row, col):
        """点文件行: 右上预览 + 在该行下方展开发票号码/开票日期(再点收起)。"""
        it = self.file_table.item(row, 0)
        if not it:                     # 展开行或空行
            return
        fid = it.data(Qt.ItemDataRole.UserRole)
        f = self.by_id(fid)
        if not f:
            return
        self._expanded_fid = None if self._expanded_fid == fid else fid
        self.preview(f)

    def _history_page(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)

        h = QHBoxLayout()
        t = QLabel('历史记录')
        t.setObjectName('panelTitle')
        h.addWidget(t)
        self.lbl_hist_cnt = QLabel('0 张')
        self.lbl_hist_cnt.setObjectName('tip')
        h.addWidget(self.lbl_hist_cnt)
        h.addStretch(1)
        b = QPushButton('月度统计')
        b.setObjectName('ghost')
        b.clicked.connect(self.hist_monthly)
        h.addWidget(b)
        b = QPushButton('清空')
        b.setObjectName('ghost')
        b.clicked.connect(self.hist_clear)
        h.addWidget(b)
        lay.addLayout(h)

        self.search_hist = self._search_box('搜索: 文件名 / 号码 / 销售方')
        self.search_hist.textChanged.connect(
            lambda _: self._hist_refresh_items())
        lay.addWidget(self.search_hist)

        tip = QLabel('按加入报销的那天分组 · 只存确认报销的')
        tip.setObjectName('tip')
        lay.addWidget(tip)

        d1 = QLabel('选择日期')
        d1.setObjectName('subhead')
        lay.addWidget(d1)
        self.hist_days = QListWidget()
        self.hist_days.setObjectName('histdays')
        self.hist_days.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection)
        self.hist_days.setVerticalScrollMode(
            QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.hist_days.currentRowChanged.connect(self._hist_select_day)
        lay.addWidget(self.hist_days, 1)

        d2 = QLabel('当天发票')
        d2.setObjectName('subhead')
        lay.addWidget(d2)
        self.hist_items = QTableWidget()
        self.hist_items.setObjectName('histitems')
        self.hist_items.setColumnCount(3)
        self.hist_items.setHorizontalHeaderLabels(['文件', '金额', ''])
        hh = self.hist_items.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        hh.resizeSection(2, 82)
        hh.setStretchLastSection(False)
        self.hist_items.verticalHeader().setVisible(False)
        self.hist_items.verticalHeader().setDefaultSectionSize(34)
        self.hist_items.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers)
        self.hist_items.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        self.hist_items.setVerticalScrollMode(
            QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.hist_items.cellClicked.connect(self._hist_click)
        lay.addWidget(self.hist_items, 2)
        return page

    # ---------------- 历史记录 ----------------
    def toggle_history(self):
        self._hist_on = not self._hist_on
        self.left_stack.setCurrentIndex(1 if self._hist_on else 0)
        self._fade_stack()
        self._sync_hist_btn()
        if self._hist_on:
            self._hist_refresh_days()
            self.statusBar().showMessage(
                '历史记录: 按加入报销的那天分组', 3000)
        else:
            self.refresh_files()

    def _fade_stack(self):
        """切换文件/历史视图时来一点淡入, 硬切太生硬。"""
        eff = QGraphicsOpacityEffect(self.left_stack)
        self.left_stack.setGraphicsEffect(eff)
        anim = QPropertyAnimation(eff, b'opacity', self)
        anim.setDuration(150)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        anim.setStartValue(0.35)
        anim.setEndValue(1.0)
        anim.finished.connect(lambda: self.left_stack.setGraphicsEffect(None))
        anim.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)

    def hist_monthly(self):
        """按月合计: 月份 / 张数 / 价税合计(来自历史记录)。"""
        months = {}
        for x in self.history:
            m = (x.get('day') or '')[:7]
            if not m:
                continue
            agg = months.setdefault(m, [0, 0.0])
            agg[0] += 1
            t = x.get('total')
            if isinstance(t, (int, float)):
                agg[1] += t
        dlg = QDialog(self)
        dlg.setWindowTitle('按月统计')
        dlg.setModal(True)
        dlg.setMinimumWidth(360)
        lay = QVBoxLayout(dlg)
        table = QTableWidget(len(months) + (1 if months else 0), 3, dlg)
        table.setHorizontalHeaderLabels(['月份', '张数', '价税合计'])
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setAlternatingRowColors(True)
        hdr = table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        for r, m in enumerate(sorted(months, reverse=True)):
            n, tot = months[m]
            table.setItem(r, 0, QTableWidgetItem(m))
            it = QTableWidgetItem(str(n))
            it.setTextAlignment(Qt.AlignmentFlag.AlignRight
                                | Qt.AlignmentFlag.AlignVCenter)
            table.setItem(r, 1, it)
            it = QTableWidgetItem('%.2f' % tot)
            it.setForeground(QBrush(QColor(THEME['green'])))
            it.setTextAlignment(Qt.AlignmentFlag.AlignRight
                                | Qt.AlignmentFlag.AlignVCenter)
            table.setItem(r, 2, it)
        if months:
            r = table.rowCount() - 1
            tot_all = sum(v[1] for v in months.values())
            n_all = sum(v[0] for v in months.values())
            labels = ['合计', str(n_all), '%.2f' % tot_all]
            for c, v in enumerate(labels):
                it = QTableWidgetItem(v)
                it.setFont(QFont('', -1, QFont.Weight.Bold))
                it.setBackground(QBrush(QColor(THEME['totalRow'])))
                if c:
                    it.setTextAlignment(Qt.AlignmentFlag.AlignRight
                                        | Qt.AlignmentFlag.AlignVCenter)
                table.setItem(r, c, it)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        lay.addWidget(table)
        if not months:
            tip = QLabel('还没有历史记录')
            tip.setObjectName('tip')
            tip.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            lay.addWidget(tip)
        b = QPushButton('关闭')
        b.setObjectName('ghost')
        b.clicked.connect(dlg.accept)
        lay.addWidget(b, 0, Qt.AlignmentFlag.AlignHCenter)
        dlg.resize(380, min(200 + 30 * len(months), 560))
        dlg.exec()

    def _sync_hist_btn(self):
        if self._hist_on:
            self.btn_hist.setText('返回文件')
            self.btn_hist.setChecked(True)
        else:
            n = len(self.history)
            self.btn_hist.setText('历史记录' + (' %d' % n if n else ''))
            self.btn_hist.setChecked(False)

    def _hist_add(self, f):
        """只把加入报销成功的发票记进历史, 按“加入的那天”分组(存本地 JSON)。"""
        inv = f.get('inv')
        if not inv or inv.get('价税合计') is None:
            return
        day = datetime.date.today().isoformat()
        path = f.get('path')
        self.history = [x for x in self.history
                        if not (x.get('path') == path and x.get('day') == day)]
        self.history.append({
            'day': day,
            'ts': int(_time.time()),
            'path': path,
            'name': f.get('name') or os.path.basename(path or ''),
            'seller': inv.get('销售方') or '',
            'code': inv.get('发票号码') or '',
            'date': inv.get('开票日期') or '',
            'total': inv.get('价税合计'),
        })
        hist_save(self.history)
        self._sync_hist_btn()
        if self._hist_on:
            self._hist_refresh_days()

    def _hist_refresh_days(self):
        counts = {}
        for x in self.history:
            counts[x['day']] = counts.get(x['day'], 0) + 1
        self._hist_days = sorted(counts, reverse=True)     # 新 → 旧
        self.lbl_hist_cnt.setText('%d 张 · %d 天'
                                  % (len(self.history), len(self._hist_days)))
        self.hist_days.blockSignals(True)
        self.hist_days.clear()
        if not self._hist_days:
            it = QListWidgetItem('还没有历史记录\n(加入报销后自动记录)')
            it.setFlags(Qt.ItemFlag.NoItemFlags)
            self.hist_days.addItem(it)
        else:
            for day in self._hist_days:
                it = QListWidgetItem('%s · %d 张' % (day, counts[day]))
                it.setData(Qt.ItemDataRole.UserRole, day)
                self.hist_days.addItem(it)
        self.hist_days.blockSignals(False)
        idx = (self._hist_days.index(self._hist_day)
               if self._hist_day in self._hist_days else 0)
        self.hist_days.setCurrentRow(idx if self._hist_days else -1)

    def _hist_select_day(self, row):
        if row < 0 or row >= len(self._hist_days):
            self._hist_day = None
            self.hist_items.setRowCount(0)
            return
        self._hist_day = self._hist_days[row]
        self._hist_refresh_items()

    def _hist_items_of_day(self):
        rows = [x for x in self.history if x.get('day') == self._hist_day]
        rows.sort(key=lambda x: -(x.get('ts') or 0))
        return rows

    def _hist_refresh_items(self):
        rows = self._hist_items_of_day()
        kw = (self.search_hist.text().strip().lower()
              if hasattr(self, 'search_hist') else '')
        self.hist_items.setRowCount(0)
        for x in rows:
            if kw:
                hay = ' '.join((x.get('name') or '', x.get('seller') or '',
                                x.get('code') or '')).lower()
                if kw not in hay:
                    continue
            r = self.hist_items.rowCount()
            self.hist_items.insertRow(r)
            name = x.get('name') or os.path.basename(x.get('path') or '')
            it = QTableWidgetItem(name)
            it.setIcon(_icon('doc', 16, '#98a2ac'))
            it.setData(Qt.ItemDataRole.UserRole, x.get('path'))
            it.setToolTip('%s\n销售方: %s\n发票号码: %s\n开票日期: %s\n加入报销: %s'
                          % (x.get('path') or '', x.get('seller') or '-',
                             x.get('code') or '-', x.get('date') or '-',
                             x.get('day') or '-'))
            self.hist_items.setItem(r, 0, it)

            total = x.get('total')
            c1 = QTableWidgetItem(('%.2f' % total)
                                  if isinstance(total, (int, float)) else '')
            c1.setForeground(QBrush(QColor('#1a7f37')))
            c1.setTextAlignment(Qt.AlignmentFlag.AlignRight
                                | Qt.AlignmentFlag.AlignVCenter)
            self.hist_items.setItem(r, 1, c1)

            box = QWidget()
            bh = QHBoxLayout(box)
            bh.setContentsMargins(6, 0, 6, 0)
            bh.setSpacing(4)
            path = x.get('path')
            f = next((p for p in self.files if p['path'] == path), None)
            if f and f['inReimb']:
                b2 = QPushButton('已在报销')
                b2.setObjectName('tiny')
                b2.setEnabled(False)
            else:
                b2 = QPushButton('加入')
                b2.setObjectName('tiny')
                b2.clicked.connect(lambda _=False, p=path: self._hist_join(p))
            bh.addWidget(b2)
            self.hist_items.setCellWidget(r, 2, box)

    def _hist_click(self, row, col):
        it = self.hist_items.item(row, 0)
        if not it:
            return
        path = it.data(Qt.ItemDataRole.UserRole)
        name = it.text()
        f = next((p for p in self.files if p['path'] == path), None)
        if not f:
            f = {'id': None, 'name': name, 'path': path, 'inReimb': False,
                 'inv': None, 'parsing': False}
        self.preview(f)          # 右上角照常展示选中的 PDF

    def _hist_join(self, path):
        if not path or not os.path.isfile(path):
            QMessageBox.warning(self, '加入报销',
                                '原文件已经不在了:\n%s' % path)
            return
        self.add_paths([path], join=True)
        self.statusBar().showMessage('已加入报销: %s'
                                     % os.path.basename(path), 3000)
        self._hist_refresh_items()

    def hist_clear(self):
        if not self.history:
            return
        yes = QMessageBox.StandardButton.Yes
        no = QMessageBox.StandardButton.No
        if QMessageBox.question(
                self, '清空历史',
                '确定清空全部 %d 条历史记录吗?\n(不影响磁盘上的 PDF, '
                '也不影响当前报销明细)' % len(self.history),
                yes | no, no) != yes:
            return
        self.history = []
        hist_save(self.history)
        self._hist_day = None
        self._sync_hist_btn()
        self._hist_refresh_days()

    def _right_panel(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)
        sp = QSplitter(Qt.Orientation.Vertical)
        sp.setChildrenCollapsible(False)
        sp.addWidget(self._preview_panel())
        sp.addWidget(self._detail_panel())
        sp.setSizes([360, 310])
        sp.setStretchFactor(0, 1)
        sp.setStretchFactor(1, 1)
        lay.addWidget(sp, 1)
        return w

    def _preview_panel(self):
        pan = QFrame()
        pan.setObjectName('panel')
        lay = QVBoxLayout(pan)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        bar = QFrame()
        bar.setObjectName('pvbar')
        h = QHBoxLayout(bar)
        h.setContentsMargins(14, 8, 14, 8)
        h.setSpacing(10)
        self.lbl_pvname = QLabel('发票原件')
        self.lbl_pvname.setObjectName('pvname')
        h.addWidget(self.lbl_pvname)
        self.lbl_pvfile = QLabel('')
        self.lbl_pvfile.setObjectName('pvfile')
        h.addWidget(self.lbl_pvfile, 1)
        h.addWidget(QLabel('缩放'))
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(25, 400)
        self.slider.setValue(100)
        self.slider.setFixedWidth(170)
        self.slider.valueChanged.connect(
            lambda v: self.pdf_view.set_zoom(v / 100.0, emit=False))
        h.addWidget(self.slider)
        self.lbl_zoom = QLabel('100%')
        self.lbl_zoom.setObjectName('zoomval')
        self.lbl_zoom.setAlignment((Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter))
        h.addWidget(self.lbl_zoom)
        lay.addWidget(bar)

        # 抬头信息条: 销售方 / 开票日期 / 发票号码(预览时更新)
        meta = QFrame()
        meta.setObjectName('pvbar')
        mh = QHBoxLayout(meta)
        mh.setContentsMargins(14, 5, 14, 5)
        self.lbl_meta = QLabel('')
        self.lbl_meta.setObjectName('pvmeta')
        self.lbl_meta.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        mh.addWidget(self.lbl_meta)
        mh.addStretch(1)
        meta.hide()
        lay.addWidget(meta)
        self.meta_bar = meta

        self.pdf_view = PDFView()
        self.pdf_view.zoomChanged.connect(self._on_zoom)
        self.pdf_view.hasDoc.connect(self._on_hasdoc)
        lay.addWidget(self.pdf_view, 1)
        self.pdf_view.hint = ('单击左侧文件，在这里查看发票原件\n'
                              '滚轮缩放 · 按住左键拖动 · 滑块调缩放')
        return pan

    def _update_meta(self, f):
        """右上抬头信息: 销售方 · 开票日期 · 发票号码。"""
        inv = (f or {}).get('inv') or {}
        parts = []
        if inv.get('销售方'):
            parts.append(inv['销售方'])
        if inv.get('开票日期'):
            parts.append(inv['开票日期'])
        if inv.get('发票号码'):
            parts.append('No.' + str(inv['发票号码']))
        if inv.get('备注'):
            parts.append('⚠ ' + inv['备注'])
        self.lbl_meta.setText(' ｜ '.join(parts))
        self.meta_bar.setVisible(bool(parts))

    def _on_hasdoc(self, has):
        pass

    def _on_zoom(self, z):
        self.slider.blockSignals(True)
        self.slider.setValue(int(round(z * 100)))
        self.slider.blockSignals(False)
        self.lbl_zoom.setText('%d%%' % round(z * 100))

    def _detail_panel(self):
        pan = QFrame()
        pan.setObjectName('panel')
        lay = QVBoxLayout(pan)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        head = QFrame()
        head.setObjectName('detailbar')
        h = QHBoxLayout(head)
        h.setContentsMargins(14, 9, 14, 9)
        h.setSpacing(8)
        t = QLabel('报销明细')
        t.setObjectName('panelTitle')
        h.addWidget(t)
        self.lbl_items = QLabel('0 项')
        self.lbl_items.setObjectName('tip')
        h.addWidget(self.lbl_items)
        h.addStretch(1)
        self.search_detail = self._search_box('搜索: 商品 / 规格 / 来源')
        self.search_detail.setFixedWidth(210)
        self.search_detail.textChanged.connect(lambda _: self.refresh_detail())
        h.addWidget(self.search_detail)
        self.btn_byitem = QPushButton('按商品汇总')
        self.btn_byitem.setObjectName('ghost')
        self.btn_byitem.clicked.connect(self.toggle_byitem)
        h.addWidget(self.btn_byitem)
        self.btn_incl = QPushButton('价格含税')
        self.btn_incl.setObjectName('ghost')
        self.btn_incl.setToolTip(
            '价格默认不含税。\n勾选后按含税价反推:\n'
            '不含税金额 = 含税价 ÷ (1+税率)\n税额 = 含税价 − 不含税金额\n'
            '价税合计 = 含税价')
        self.btn_incl.clicked.connect(self.toggle_price_incl)
        h.addWidget(self.btn_incl)
        b = QPushButton('清空报销')
        b.setObjectName('ghost')
        b.clicked.connect(self.leave_all)
        h.addWidget(b)
        b = QPushButton('导出 CSV')
        b.setObjectName('ghost')
        b.clicked.connect(self.export_csv)
        h.addWidget(b)
        b = QPushButton('生成报告')
        b.clicked.connect(self.generate_report)
        h.addWidget(b)
        lay.addWidget(head)

        kpi_wrap = QFrame()
        kpi_wrap.setObjectName('kpiwrap')
        g = QGridLayout(kpi_wrap)
        g.setContentsMargins(14, 10, 14, 10)
        g.setSpacing(10)
        self.kpis = {}
        spec = [('金额(不含税)', 'tAmt', ''), ('税额', 'tTax', ''),
                ('价税合计', 'tTot', 'accent'), ('发票张数', 'tInv', 'green'),
                ('明细行数', 'tRows', '')]
        for i, (label, key, cls) in enumerate(spec):
            card = QFrame()
            card.setObjectName('kpi')
            if cls:
                card.setProperty('kind', cls)
            cv = QVBoxLayout(card)
            cv.setContentsMargins(14, 7, 14, 7)
            cv.setSpacing(1)
            k = QLabel(label)
            k.setObjectName('kpik')
            cv.addWidget(k)
            v = QLabel('0')
            v.setObjectName('kpiv')
            cv.addWidget(v)
            self.kpis[key] = v
            g.addWidget(card, 0, i)
        g.setColumnStretch(len(spec), 1)
        lay.addWidget(kpi_wrap)

        self.detail_table = DetailTable()
        self.detail_table.rowsDropped.connect(self.join_rows)
        self.detail_table.filesDropped.connect(lambda ps: self.add_paths(ps, True))
        self.detail_table.cellClicked.connect(self._detail_clicked)
        lay.addWidget(self.detail_table, 1)
        return pan

    # =================================================================
    #  文件管理
    # =================================================================
    def pick_files(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, '选择发票文件', cfg_dir(),
            '发票文件 (*.pdf *.ofd);;所有文件 (*)')
        if paths:
            SETTINGS.setValue('invoice_dir',
                              os.path.dirname(os.path.abspath(paths[0])))
            SETTINGS.sync()
        self.add_paths(paths, join=True)

    def add_paths(self, paths, join=True):
        self._batch_begin()
        try:
            for p in paths:
                if p.lower().endswith(('.pdf', '.ofd')) and os.path.isfile(p):
                    self._push(os.path.basename(p), p, join)
        finally:
            self._batch_end()

    def _push(self, name, path, join=False):
        exist = next((f for f in self.files if f['path'] == path), None)
        if exist:
            if join and not exist['inReimb']:
                self.join(exist)
            return
        self._seq += 1
        f = {'id': self._seq, 'name': name, 'path': path,
             'inReimb': False, 'inv': None, 'parsing': False, 'dup': ''}
        self.files.append(f)
        if join:
            self.join(f)

    def by_id(self, fid):
        return next((f for f in self.files if f['id'] == fid), None)

    def _file_match(self, f):
        kw = self.search_files.text().strip().lower()
        if not kw:
            return True
        inv = f.get('inv') or {}
        hay = ' '.join((f.get('name') or '', str(inv.get('发票号码') or ''),
                        str(inv.get('销售方') or ''),
                        str(inv.get('开票日期') or ''))).lower()
        return kw in hay

    def refresh_files(self):
        if self._suspend:
            return
        cur = None
        f_cur = self.by_id(self._cur_id)
        if f_cur:
            cur = f_cur['path']
        self.file_table.setRowCount(0)
        shown = 0
        for f in self.files:
            if not self._file_match(f):
                continue
            shown += 1
            r = self.file_table.rowCount()
            self.file_table.insertRow(r)
            inv = f.get('inv') or {}

            it = QTableWidgetItem(f['name'])
            it.setIcon(_icon('doc', 16, '#98a2ac'))
            it.setData(Qt.ItemDataRole.UserRole, f['id'])
            it.setToolTip(f['path'])
            if f['path'] == cur:
                it.setBackground(QColor(THEME['selRow']))
            self.file_table.setItem(r, 0, it)

            if f['parsing']:
                st, sc = '解析中', THEME['gray']
            elif f.get('dup'):
                st, sc = '重复', THEME['warn']
            elif f['inReimb']:
                st, sc = '报销', THEME['green']
            else:
                st, sc = '未加入', THEME['gray']
            c1 = QTableWidgetItem(st)
            c1.setForeground(QBrush(QColor(sc)))
            self.file_table.setItem(r, 1, c1)

            if f.get('dup'):
                val, sc = '', THEME['warn']
            elif f['inReimb']:
                if f['inv'] is None or f['inv'].get('价税合计') is None:
                    val, sc = '失败', THEME['red']
                else:
                    val, sc = _fmt(f['inv'].get('价税合计')), THEME['green']
            else:
                val, sc = '', THEME['dim']
            try:
                _vkey = float(val)          # 金额列按数值排序(负数/失败行退回文本)
            except ValueError:
                _vkey = None
            c2 = _SortItem(val, key=_vkey)
            c2.setForeground(QBrush(QColor(sc)))
            c2.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.file_table.setItem(r, 2, c2)

            box = QWidget()
            bh = QHBoxLayout(box)
            bh.setContentsMargins(6, 0, 6, 0)
            bh.setSpacing(5)
            if f['inReimb']:
                b = QPushButton('移出')
                b.setObjectName('tiny')
                b.clicked.connect(lambda _=False, x=f['id']: self.leave_by_id(x))
            else:
                b = QPushButton('加入')
                b.setObjectName('tiny')
                b.clicked.connect(lambda _=False, x=f['id']: self.join_by_id(x))
            bh.addWidget(b)
            b.setMinimumWidth(46)
            d = QPushButton('×')
            d.setObjectName('del')
            d.setFixedWidth(26)
            d.setToolTip('删除此文件')
            d.clicked.connect(lambda _=False, x=f['id']: self.remove_by_id(x))
            bh.addWidget(d)
            bh.addStretch(1)
            self.file_table.setCellWidget(r, 3, box)

            # 点击过的发票在此行下方展开一行, 显示发票号码 / 开票日期
            if f['id'] == self._expanded_fid:
                er = self.file_table.rowCount()
                self.file_table.insertRow(er)
                if inv.get('发票号码'):
                    detail = '发票号码：%s ｜ 开票日期：%s' % (
                        inv.get('发票号码'), inv.get('开票日期') or '未知')
                elif f['parsing']:
                    detail = '解析中…'
                else:
                    detail = '未解析(加入报销后自动解析)'
                eit = QTableWidgetItem('↳ ' + detail)
                eit.setForeground(QBrush(QColor(THEME['gray'])))
                self.file_table.setItem(er, 0, eit)
                self.file_table.setSpan(er, 0, 1, len(FileTable.COLS))

        self.lbl_cnt.setText(('%d / %d 个' % (shown, len(self.files)))
                             if shown != len(self.files)
                             else '%d 个' % len(self.files))
        self.file_table.set_empty(not self.files)

    # ---------- 加入 / 移出 / 删除 ----------
    def join_by_id(self, fid):
        f = self.by_id(fid)
        if f:
            self.join(f)

    def leave_by_id(self, fid):
        f = self.by_id(fid)
        if f:
            f['inReimb'] = False
            self.refresh_files()
            self.refresh_detail()
            self._sync_session()

    def remove_by_id(self, fid):
        f = self.by_id(fid)
        if not f:
            return
        self.files = [x for x in self.files if x['id'] != fid]
        if fid == self._cur_id:
            self.clear_preview()
        self.refresh_files()
        self.refresh_detail()
        self._sync_session()

    def join(self, f):
        if f['inReimb'] or f['parsing']:
            return
        f['parsing'] = True
        f['dup'] = ''                  # 重新加入时清掉旧的重复标记
        self.refresh_files()
        task = ParseTask(f['path'])
        task.sig.done.connect(self._on_parsed)
        self._pool.start(task)

    def _on_parsed(self, path, inv):
        hit = None
        for f in self.files:
            if f['path'] == path:
                f['parsing'] = False
                f['inv'] = inv
                hit = f
                break
        if hit:
            # 发票号码去重: 同一张票(改了文件名再拖进来)只允许进一次报销
            num = (inv or {}).get('发票号码')
            dup = None
            if num:
                dup = next((x for x in self.files if x is not hit
                            and x['inReimb']
                            and (x.get('inv') or {}).get('发票号码') == num),
                           None)
            if dup:
                hit['inReimb'] = False
                hit['dup'] = num
                self.statusBar().showMessage(
                    '重复发票: 与「%s」是同一张(号码相同), 已跳过'
                    % os.path.basename(dup['path']), 8000)
            else:
                hit['inReimb'] = True
                hit['dup'] = ''
                if not self._restoring:
                    self._hist_add(hit)   # 只保存加入报销的 -> 历史
                if hit['id'] == self._cur_id:
                    self._update_meta(hit)
        self._sync_session()
        self._queue_refresh()

    def _sync_session(self):
        """报销成员变化时把清单落盘(启动恢复期间不保存, 防止覆盖)。"""
        if not self._restoring:
            session_save([f['path'] for f in self.reimb_files()])

    # ---------- 批量刷新闸: 一次操作只重建一次表格 ----------
    def _batch_begin(self):
        self._suspend += 1

    def _batch_end(self):
        self._suspend = max(0, self._suspend - 1)
        if self._suspend == 0:
            self.refresh_files()
            self.refresh_detail()

    def _queue_refresh(self):
        """解析连续完成时合并界面刷新, 避免一次性加载多张 PDF 卡顿。"""
        if self._suspend:
            return
        if not self._defer.isActive():
            self._defer.start()

    def _deferred_refresh(self):
        if self._suspend:
            return
        self.refresh_files()
        self.refresh_detail()

    def join_all(self):
        self._batch_begin()
        try:
            for f in list(self.files):
                self.join(f)
        finally:
            self._batch_end()

    def leave_all(self):
        for f in self.files:
            f['inReimb'] = False
        self.refresh_files()
        self.refresh_detail()
        self._sync_session()

    def remove_all(self):
        """清空文件列表(只从列表移除, 不动磁盘上的 PDF)。"""
        if not self.files:
            return
        yes = QMessageBox.StandardButton.Yes
        no = QMessageBox.StandardButton.No
        if QMessageBox.question(
                self, '全部删除',
                '确定把列表中的 %d 个文件全部删除吗?\n(仅从列表移除, 不删除磁盘文件)'
                % len(self.files),
                yes | no, no) != yes:
            return
        self.files = []
        self.clear_preview()
        self.refresh_files()
        self.refresh_detail()
        self._sync_session()

    def join_rows(self, rows):
        self._batch_begin()
        try:
            for r in rows:
                it = self.file_table.item(r, 0)
                if it:
                    f = self.by_id(it.data(Qt.ItemDataRole.UserRole))
                    if f:
                        self.join(f)
        finally:
            self._batch_end()

    # =================================================================
    #  预览
    # =================================================================
    def preview_row(self, row):
        it = self.file_table.item(row, 0)
        if it:
            f = self.by_id(it.data(Qt.ItemDataRole.UserRole))
            if f:
                self.preview(f)

    def preview(self, f):
        self._cur_id = f['id']
        self.lbl_pvfile.setText(_elide_name(f['name']))
        self.lbl_pvfile.setToolTip(f['path'])
        self._update_meta(f)
        if str(f.get('path') or '').lower().endswith('.ofd'):
            # OFD 只解析不渲染(没有内置渲染器), 界面上给个说明
            self.pdf_view.clear()
            self.pdf_view.set_warn(False)
            self.lbl_pvfile.setProperty('error', False)
            self._repolish(self.lbl_pvfile)
            self.pdf_view.hint = ('OFD 发票暂不支持预览\n'
                                  '数据已正常解析, 合计与明细照常统计')
            self.pdf_view.viewport().update()
            self._on_zoom(1.0)
            return
        ok = self.pdf_view.load(f['path'])
        self.pdf_view.set_warn(not ok)
        self.lbl_pvfile.setProperty('error', not ok)
        self._repolish(self.lbl_pvfile)
        if not ok:
            self.pdf_view.hint = ('打不开这个文件\n'
                                  '请确认它是可以正常读取的 PDF')
            self.pdf_view.viewport().update()
        self._on_zoom(1.0)
        self.refresh_files()

    @staticmethod
    def _repolish(w):
        st = w.style()
        st.unpolish(w)
        st.polish(w)

    def clear_preview(self):
        self._cur_id = None
        self.pdf_view.clear()
        self.lbl_pvfile.setText('')
        self.lbl_pvfile.setProperty('error', False)
        self._repolish(self.lbl_pvfile)
        self.lbl_meta.setText('')
        self.meta_bar.hide()
        self.pdf_view.set_warn(False)
        self.pdf_view.hint = ('单击左侧文件，在这里查看发票原件\n'
                              '滚轮缩放 · 按住左键拖动 · 滑块调缩放')
        self.pdf_view.viewport().update()
        self._on_zoom(1.0)

    # =================================================================
    #  明细
    # =================================================================
    def reimb_files(self):
        return [f for f in self.files if f['inReimb']]

    def rows_flat(self):
        out = []
        for f in self.reimb_files():
            for it in (f.get('inv') or {}).get('明细', []):
                row = dict(it)
                row['src'] = f['name']
                row['fid'] = f['id']      # 带上文件 id, 明细行点击能直接跳原件
                row['inv'] = f['inv']
                if self.price_incl:
                    self._to_tax_inclusive(row)
                out.append(row)
        return out

    @staticmethod
    def _to_tax_inclusive(row):
        """'金额'按含税价处理: 不含税 = 含税价÷(1+税率), 税额 = 含税价−不含税。"""
        base = row.get('金额')
        if base is None:
            return row
        rate = _rate_num(row.get('税率'))
        net = round(base / (1.0 + rate), 2) if rate > 0 else round(base, 2)
        row['金额'] = net
        row['税额'] = round(base - net, 2)
        p = row.get('单价')
        if p is not None and rate > 0:
            row['单价'] = round(p / (1.0 + rate), 4)
        return row

    def rows_grouped(self):
        m = {}
        for r in self.rows_flat():
            k = ''.join((r.get('名称') or '').split())
            if k not in m:
                m[k] = {'名称': r.get('名称'), '规格': '', '数量': 0, '单价': None,
                        '金额': 0, '税率': r.get('税率'), '税额': 0,
                        'src': r.get('src'), 'fid': r.get('fid'), 'n': 0}
            g = m[k]
            g['金额'] = round(g['金额'] + (r.get('金额') or 0), 2)
            g['税额'] = round(g['税额'] + (r.get('税额') or 0), 2)
            g['数量'] = round(g['数量'] + (r.get('数量') or 0), 2)
            g['n'] += 1
            if not g['规格'] and r.get('规格'):
                g['规格'] = r['规格']
            if g['n'] > 1:
                g['src'] = ''
            else:
                g['单价'] = r.get('单价')
        return sorted(m.values(), key=lambda x: -x['金额'])

    def refresh_detail(self):
        if self._suspend:
            return
        all_rows = self.rows_grouped() if self._by_item else self.rows_flat()
        kw = self.search_detail.text().strip().lower()
        rows = all_rows
        if kw:
            rows = [r for r in all_rows
                    if kw in ' '.join((str(r.get('名称') or ''),
                                       str(r.get('规格') or ''),
                                       str(r.get('src') or ''))).lower()]
            self.lbl_items.setText('%d / %d 项(过滤)' % (len(rows), len(all_rows)))
        else:
            self.lbl_items.setText('%d 项' % len(all_rows))
        self.detail_table.setSortingEnabled(False)   # 填充期间不能实时重排
        self.detail_table.setRowCount(0)
        for r in rows:
            rr = self.detail_table.rowCount()
            self.detail_table.insertRow(rr)
            has = r.get('金额') is not None and r.get('税额') is not None
            vals = [_esc(r.get('名称')), _esc(r.get('规格')),
                    '' if r.get('数量') is None else str(r.get('数量')),
                    _fmt(r.get('单价')), _fmt(r.get('金额')),
                    _esc(r.get('税率')), _fmt(r.get('税额')),
                    '%.2f' % (r['金额'] + r['税额']) if has else '',
                    _esc(r.get('src'))]
            neg = r.get('金额') is not None and r['金额'] < 0
            for c, v in enumerate(vals):
                key = v
                if c in (2, 3, 4, 6, 7):     # 数值列: 按数值排序而非文本
                    try:
                        key = float(v)
                    except ValueError:
                        key = v
                it = _SortItem(v, key=key)
                if c in (2, 3, 4, 6, 7):
                    it.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                if neg and c == 4:
                    it.setForeground(QBrush(QColor(THEME['red'])))
                if c == 0:
                    it.setData(Qt.ItemDataRole.UserRole, r.get('fid'))
                    it.setToolTip('点击这一行, 右上角显示这张发票的原件')
                if c == 8:
                    it.setForeground(QBrush(QColor(THEME['gray'])))
                    it.setToolTip('点击查看原件')
                self.detail_table.setItem(rr, c, it)

        amt = tax = tot = 0.0
        n = 0
        if self.price_incl:
            # 价格含税: 明细金额=含税价, 反推后的不含税金额/税额即为口径
            for r in self.rows_flat():
                if r.get('金额') is not None:
                    amt += r['金额']
                if r.get('税额') is not None:
                    tax += r['税额']
            amt, tax = round(amt, 2), round(tax, 2)
            tot = round(amt + tax, 2)
            n = sum(1 for f in self.reimb_files()
                    if f.get('inv') and f['inv'].get('价税合计') is not None)
        else:
            for f in self.reimb_files():
                inv = f.get('inv')
                if inv and inv.get('价税合计') is not None:
                    amt += inv.get('金额') or 0
                    tax += inv.get('税额') or 0
                    tot += inv['价税合计']
                    n += 1
        self.kpis['tAmt'].setText('%.2f' % amt)
        self.kpis['tTax'].setText('%.2f' % tax)
        self.kpis['tTot'].setText('%.2f' % tot)
        self.kpis['tInv'].setText(str(n))
        self.kpis['tRows'].setText(str(len(all_rows)))
        self.lbl_sum.setText('报销 %d 张 ｜ 价税合计 %.2f' % (n, tot))

        if rows:
            rr = self.detail_table.rowCount()
            self.detail_table.insertRow(rr)
            fa = round(sum(r.get('金额') or 0 for r in rows), 2)
            ft = round(sum(r.get('税额') or 0 for r in rows), 2)
            vals = ['合计', '', '', '', '%.2f' % fa, '', '%.2f' % ft,
                    '%.2f' % (fa + ft), '']
            for c, v in enumerate(vals):
                it = _SortItem(v, key=(math.inf if c in (2, 3, 4, 6, 7)
                                       else '\uffff'))
                it.setFont(QFont('', -1, QFont.Weight.Bold))
                it.setBackground(QBrush(QColor(THEME['totalRow'])))
                if c in (4, 6, 7):
                    it.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                self.detail_table.setItem(rr, c, it)
        self.detail_table.setSortingEnabled(True)

    def _detail_clicked(self, row, col):
        """明细里点任意一格 -> 右上角显示这条商品所在发票的原件。"""
        it = self.detail_table.item(row, 0)
        if not it:
            return
        fid = it.data(Qt.ItemDataRole.UserRole)
        f = self.by_id(fid) if fid else None
        if f is None and col == 8:
            # 兜底: 旧行为按“来源”列的文件名匹配
            s = self.detail_table.item(row, 8)
            name = s.text() if s else ''
            f = next((x for x in self.files if x['name'] == name), None)
        if f:
            self.preview(f)

    def toggle_price_incl(self):
        self.set_price_incl(not self.price_incl)

    def set_price_incl(self, on):
        """价格口径切换: 默认不含税; 打开后按含税价反推金额/税额。"""
        on = bool(on)
        changed = on != self.price_incl
        self.price_incl = on
        SETTINGS.setValue('price_incl', '1' if on else '0')
        SETTINGS.sync()
        self._sync_incl_btn()
        if changed:
            self.refresh_detail()
            self.statusBar().showMessage(
                '价格口径: %s' % ('含税价(按税率反推不含税金额与税额)'
                                  if on else '不含税'), 4000)

    def _sync_incl_btn(self):
        self.btn_incl.setProperty('active', self.price_incl)
        self._repolish(self.btn_incl)
        self.btn_incl.setText('价格含税 ✓' if self.price_incl else '价格含税')

    def toggle_byitem(self):
        self._by_item = not self._by_item
        self.btn_byitem.setText('按发票明细' if self._by_item else '按商品汇总')
        self.btn_byitem.setProperty('active', self._by_item)
        self._repolish(self.btn_byitem)
        self.refresh_detail()

    # =================================================================
    #  CSV
    # =================================================================
    def export_csv(self):
        rows = self.rows_flat()
        if not rows:
            self.statusBar().showMessage('还没有加入报销的发票', 4000)
            return
        default = os.path.join(
            cfg_outdir(),
            '报销商品明细_%s.csv' % datetime.date.today().isoformat())
        path, _ = QFileDialog.getSaveFileName(self, '导出 CSV', default,
                                              'CSV 文件 (*.csv)')
        if not path:
            return
        SETTINGS.setValue('out_dir', os.path.dirname(os.path.abspath(path)))
        SETTINGS.sync()
        head = ['来源文件', '发票号码', '开票日期', '销售方', '商品摘要', '商品名称',
                '规格型号', '数量', '单价', '金额(不含税)', '税率', '税额',
                '价税合计', '备注']
        with open(path, 'w', newline='', encoding='utf-8-sig') as fh:
            w = csv.writer(fh)
            w.writerow(head)
            done = set()
            for r in rows:
                v = r['inv']
                key = id(v)
                summ = _summary_row(v.get('明细', [])) if key not in done else ''
                done.add(key)
                w.writerow([
                    r['src'], v.get('发票号码', ''), v.get('开票日期', ''),
                    v.get('销售方', ''), summ, r.get('名称'), r.get('规格'),
                    '' if r.get('数量') is None else r.get('数量'),
                    '' if r.get('单价') is None else r.get('单价'),
                    _fmt(r.get('金额')), r.get('税率') or '', _fmt(r.get('税额')),
                    ('%.2f' % (r['金额'] + r['税额'])
                     if r.get('金额') is not None and r.get('税额') is not None else ''),
                    v.get('备注', ''),
                ])
            fa = round(sum(r.get('金额') or 0 for r in rows), 2)
            ft = round(sum(r.get('税额') or 0 for r in rows), 2)
            w.writerow(['合计', '', '', '', '', '', '', '', '', '%.2f' % fa, '',
                        '%.2f' % ft, '%.2f' % (fa + ft), ''])
        self.statusBar().showMessage('已导出: %s' % path, 6000)

    # =================================================================
    #  生成 Excel 报告
    # =================================================================
    def generate_report(self):
        rows = self.rows_flat()
        if not rows:
            self.statusBar().showMessage('还没有加入报销的发票', 4000)
            return
        default = os.path.join(
            cfg_outdir(),
            '报销报告_%s.xlsx' % datetime.date.today().isoformat())
        path, _ = QFileDialog.getSaveFileName(self, '生成报告', default,
                                              'Excel 工作簿 (*.xlsx)')
        if not path:
            return
        SETTINGS.setValue('out_dir', os.path.dirname(os.path.abspath(path)))
        SETTINGS.sync()
        if not path.lower().endswith('.xlsx'):
            path += '.xlsx'
        try:
            self._write_xlsx(path, rows)
        except ImportError:
            QMessageBox.warning(self, '缺少组件',
                                '生成 Excel 报告需要 openpyxl。\n'
                                '请先安装: pip install openpyxl')
            return
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, '生成失败', '写出报告时出错:\n%s' % e)
            return
        self.statusBar().showMessage('已生成报告: %s' % path, 8000)

    @staticmethod
    def _sheet_table(ws, header, widths, rows, num_cols=(), start=1):
        """写一张带样式的表: 蓝底白字表头 + 细边框 + 自动列宽。"""
        from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
        from openpyxl.utils import get_column_letter

        fill = PatternFill('solid', fgColor='1F6FEB')
        hfont = Font(name='微软雅黑', size=11, bold=True, color='FFFFFF')
        cfont = Font(name='微软雅黑', size=11)
        side = Side(style='thin', color='DDE2E8')
        bd = Border(left=side, right=side, top=side, bottom=side)
        halign = Alignment(horizontal='center', vertical='center')
        ralign = Alignment(horizontal='right', vertical='center')
        lalign = Alignment(horizontal='left', vertical='center')

        r = start
        for c, name in enumerate(header, 1):
            cell = ws.cell(row=r, column=c, value=name)
            cell.fill, cell.font = fill, hfont
            cell.alignment, cell.border = halign, bd
        for row in rows:
            r += 1
            for c, v in enumerate(row, 1):
                cell = ws.cell(row=r, column=c, value=v)
                cell.font, cell.border = cfont, bd
                if c in num_cols and isinstance(v, (int, float)):
                    cell.number_format = '#,##0.00'
                    cell.alignment = ralign
                else:
                    cell.alignment = lalign if c == 1 else halign
        for c, w in enumerate(widths, 1):
            ws.column_dimensions[get_column_letter(c)].width = w
        return r

    def _write_xlsx(self, path, rows):
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font

        now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        files = self.reimb_files()
        tot_amt = round(sum(r.get('金额') or 0 for r in rows), 2)
        tot_tax = round(sum(r.get('税额') or 0 for r in rows), 2)
        tot_all = round(tot_amt + tot_tax, 2)

        wb = Workbook()

        # ---- Sheet 1: 汇总 ----
        ws = wb.active
        ws.title = '汇总'
        ws.sheet_view.showGridLines = False
        ws.merge_cells('A1:D1')
        t = ws['A1']
        t.value = '发票报销报告'
        t.font = Font(name='微软雅黑', size=18, bold=True, color='1F6FEB')
        t.alignment = Alignment(horizontal='left', vertical='center')
        ws.row_dimensions[1].height = 30
        ws['A2'] = '生成时间: %s' % now
        ws['A2'].font = Font(name='微软雅黑', size=10, color='8A9199')
        self._sheet_table(
            ws, ['项目', '数值'], [22, 16],
            [['金额(不含税)', tot_amt], ['税额', tot_tax],
             ['价税合计', tot_all],
             ['发票张数', len(files)], ['明细行数', len(rows)]],
            num_cols=(2,), start=4)
        ws['A11'] = '本表由发票助手自动生成, 数据来源于所选发票 PDF 的本地解析。'
        ws['A11'].font = Font(name='微软雅黑', size=10, color='8A9199')

        # ---- Sheet 2: 发票清单 ----
        ws2 = wb.create_sheet('发票清单')
        ws2.sheet_view.showGridLines = False
        inv_rows = []
        for i, f in enumerate(files, 1):
            v = f.get('inv') or {}
            inv_rows.append([
                i, f['name'], v.get('发票号码', ''), v.get('开票日期', ''),
                v.get('销售方', ''),
                v.get('金额'), v.get('税额'), v.get('价税合计'),
                v.get('备注', ''),
            ])
        inv_rows.append(['合计', '', '', '', '', tot_amt, tot_tax, tot_all, ''])
        last = self._sheet_table(
            ws2, ['序号', '文件名', '发票号码', '开票日期', '销售方',
                  '金额(不含税)', '税额', '价税合计', '备注'],
            [6, 30, 20, 13, 34, 14, 12, 13, 24],
            inv_rows, num_cols=(6, 7, 8))
        for c in range(1, 10):
            ws2.cell(row=last, column=c).font = Font(
                name='微软雅黑', size=11, bold=True)
        ws2.freeze_panes = 'A2'

        # ---- Sheet 3: 商品明细 ----
        ws3 = wb.create_sheet('商品明细')
        ws3.sheet_view.showGridLines = False
        det = []
        for r in rows:
            has = r.get('金额') is not None and r.get('税额') is not None
            det.append([
                r['src'], r.get('名称'), r.get('规格'),
                r.get('数量'), r.get('单价'), r.get('金额'),
                r.get('税率'), r.get('税额'),
                (round(r['金额'] + r['税额'], 2) if has else None),
            ])
        det.append(['合计', '', '', '', '', tot_amt, '', tot_tax, tot_all])
        last = self._sheet_table(
            ws3, ['来源文件', '商品名称', '规格型号', '数量', '单价',
                  '金额(不含税)', '税率', '税额', '价税合计'],
            [30, 30, 30, 9, 11, 14, 9, 11, 13],
            det, num_cols=(5, 6, 8, 9))
        for c in range(1, 10):
            ws3.cell(row=last, column=c).font = Font(
                name='微软雅黑', size=11, bold=True)
        ws3.freeze_panes = 'A2'

        # ---- Sheet 4: 按商品汇总 ----
        ws4 = wb.create_sheet('按商品汇总')
        ws4.sheet_view.showGridLines = False
        grp = self.rows_grouped()
        grp_rows = [[g.get('名称'), g.get('规格'), g.get('数量'),
                     g.get('金额'), g.get('税额'),
                     round((g.get('金额') or 0) + (g.get('税额') or 0), 2),
                     g.get('n')] for g in grp]
        grp_rows.append(['合计', '', '', tot_amt, tot_tax, tot_all, len(rows)])
        last = self._sheet_table(
            ws4, ['商品名称', '规格型号', '数量', '金额(不含税)', '税额',
                  '价税合计', '出现次数'],
            [34, 30, 10, 14, 12, 13, 11],
            grp_rows, num_cols=(4, 5, 6))
        for c in range(1, 8):
            ws4.cell(row=last, column=c).font = Font(
                name='微软雅黑', size=11, bold=True)
        ws4.freeze_panes = 'A2'

        wb.save(path)


# ======================================================================
#  样式: 单一模板 + 浅色/深色两套调色板(build_qss 生成)
# ======================================================================
QSS_TMPL = """
* { font-family: "Microsoft YaHei UI"; }
QMainWindow { background: %(win)s; }
QWidget { background: transparent; color: %(text)s; }
QFrame#header { background: qlineargradient(x1:0,y1:0,x2:1,y2:0,
    stop:0 %(head0)s, stop:1 %(head1)s);
    border-bottom: 1px solid rgba(0,0,0,24); }
QLabel#title { color: #fff; font-size: 16px; font-weight: bold; background: transparent; }
QLabel#sum { color: #fff; font-size: 13px; background: rgba(255,255,255,42);
    border-radius: 10px; padding: 5px 14px; }
QLabel#headtip { color: rgba(255,255,255,175); font-size: 12px;
    background: transparent; }
QPushButton#gear { background: rgba(255,255,255,54); border: none;
    border-radius: 15px; color: #fff; font-size: 15px; padding: 0; }
QPushButton#gear:hover { background: rgba(255,255,255,110); }
QPushButton#gear:pressed { background: rgba(0,0,0,45); }
QPushButton#headbtn { background: rgba(255,255,255,44); color: #fff;
    border: 1px solid rgba(255,255,255,110); border-radius: 7px;
    padding: 5px 13px; font-size: 13px; min-height: 18px; }
QPushButton#headbtn:hover { background: rgba(255,255,255,88); }
QPushButton#headbtn:checked { background: #fff; color: %(head0)s;
    border-color: #fff; font-weight: bold; }
QDialog#settings { background: %(dlg)s; }
QDialog { background: %(dlg)s; }
QMessageBox { background: %(dlg)s; }
QMessageBox QLabel { color: %(text)s; background: transparent;
    font-size: 13px; }
QMessageBox QLabel#titleBarLabel { font-weight: bold; }
QLabel#dlgTitle { font-size: 16px; font-weight: bold; color: %(kpitext)s;
    background: transparent; }
QLineEdit { background: %(linebg)s; color: %(text)s; border: 1px solid %(lineborder)s;
    border-radius: 6px; padding: 6px 9px; selection-background-color: %(acc)s; }
QLineEdit:focus { border-color: %(acc)s; }
QFrame#panel { background: %(panel)s; border: 1px solid %(border)s; border-radius: 10px; }
QLabel#panelTitle { font-size: 14px; font-weight: bold; color: %(kpitext)s;
    background: transparent; }
QLabel#tip { color: %(sub)s; font-size: 12px; background: transparent; }
QLabel#subhead { color: %(statustext)s; font-size: 12px; font-weight: bold;
    background: transparent; }
QListWidget#histdays { background: %(panel)s; border: 1px solid %(border)s;
    border-radius: 9px; padding: 4px; outline: none; font-size: 13px; }
QListWidget#histdays::item { padding: 8px 9px; border-radius: 7px;
    color: %(text)s; }
QListWidget#histdays::item:hover { background: %(hover)s; }
QListWidget#histdays::item:selected { background: %(acc)s; color: #fff; }
QTableWidget#histitems { border: 1px solid %(border)s; border-radius: 9px; }
QLabel#badge { color: %(badgetext)s; font-size: 12px; background: %(badbg)s;
    border-radius: 9px; padding: 2px 9px; }
QPushButton { background: qlineargradient(x1:0,y1:0,x2:0,y2:1,
    stop:0 %(btn0)s, stop:1 %(btn1)s); color: #fff; border: none;
    border-radius: 7px; padding: 6px 13px; min-height: 20px; }
QPushButton:hover { background: %(btnhover)s; }
QPushButton:pressed { background: %(btnpress)s; }
QPushButton:focus { outline: none; }
QPushButton[active="true"] { background: %(btn1)s; }
QPushButton[active="true"]:hover { background: %(btnhover)s; }
QPushButton#ghost { background: %(ghostbg)s; color: %(ghosttext)s; border: 1px solid %(ghostborder)s; }
QPushButton#ghost:hover { background: %(ghosthover)s; border-color: %(ghostbordh)s; }
QPushButton#tiny { padding: 3px 9px; border-radius: 6px; }
QPushButton#tiny#ghost, QPushButton#tiny.ghost {
    background: %(ghostbg)s; color: %(ghosttext)s; border: 1px solid %(ghostborder)s; }
QPushButton#tiny.ghost:hover { background: %(ghosthover)s; }
QPushButton#del { background: transparent; color: %(sub)s; font-size: 15px;
    padding: 0; border: none; border-radius: 5px; }
QPushButton#del:hover { background: %(delbg)s; color: %(delred)s; }
QTableWidget { background: %(panel)s; alternate-background-color: %(altrow)s;
    border: none; gridline-color: %(grid)s; font-size: 13px; }
QTableWidget::item { padding: 4px; }
QTableWidget::item:hover { background: %(hover)s; }
QTableWidget::item:selected { background: %(sel)s; color: %(text)s; }
QHeaderView::section { background: %(hdrbg)s; color: %(hdrtext)s; font-size: 12px;
    font-weight: bold; border: none; border-bottom: 1px solid %(hdrline)s;
    padding: 7px; }
QHeaderView::section:hover { background: %(hover)s; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 0; }
QScrollBar::handle:vertical { background: %(sb)s; border-radius: 5px;
    min-height: 30px; }
QScrollBar::handle:vertical:hover { background: %(sbh)s; }
QScrollBar:horizontal { background: transparent; height: 10px; margin: 0; }
QScrollBar::handle:horizontal { background: %(sb)s; border-radius: 5px;
    min-width: 30px; }
QScrollBar::handle:horizontal:hover { background: %(sbh)s; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }
QFrame#pvbar { background: %(pvbar)s; border-bottom: 1px solid %(pvline)s; }
QLabel#pvname { font-weight: bold; color: %(ghosttext)s; background: transparent; }
QLabel#pvfile { color: %(sub)s; font-size: 12px; background: transparent;
    padding: 2px 6px; }
QLabel#pvfile[error="true"] { color: %(delred)s; font-weight: bold; }
QLabel#pvmeta { color: %(statustext)s; font-size: 12px; background: transparent; }
QLabel#zoomval { color: %(zoomtext)s; background: %(zoombg)s; border-radius: 9px;
    padding: 2px 4px; min-width: 48px; }
QSlider::groove:horizontal { height: 5px; border-radius: 5px; background: %(sb)s; }
QSlider::sub-page:horizontal { background: %(acc)s; border-radius: 5px; }
QSlider::handle:horizontal { background: #fff; border: 2px solid %(acc)s;
    width: 14px; height: 14px; border-radius: 8px; margin: -6px 0; }
QLabel#pvempty { color: %(hinttext)s; font-size: 14px; background: transparent; }
QLabel#filehint { color: %(hinttext)s; font-size: 13px; background: transparent; }
QFrame#detailbar { background: %(pvbar)s; border-bottom: 1px solid %(pvline)s; }
QFrame#kpiwrap { background: %(kpiwrap)s; border-bottom: 1px solid %(pvline)s; }
QFrame#kpi { background: %(kpibg)s; border: 1px solid %(kpiborder)s; border-radius: 8px; }
QLabel#kpik { color: %(sub)s; font-size: 11px; background: transparent; }
QLabel#kpiv { color: %(kpitext)s; font-size: 16px; font-weight: bold;
    background: transparent; }
QFrame#kpi[kind="accent"] QLabel#kpiv { color: %(acc)s; }
QFrame#kpi[kind="green"] QLabel#kpiv { color: %(green)s; }
QSplitter::handle { background: transparent; }
QStatusBar { background: %(statusbg)s; color: %(statustext)s; }
"""


def build_qss(dark=False):
    """按主题生成全局 QSS(颜色只在这一处定义)。"""
    if dark:
        C = dict(
            win='#16181d', panel='#1f232b', border='#30363d', text='#d7dde3',
            sub='#8b949e', head0='#17406b', head1='#2469b8',
            btn0='#2a6cc0', btn1='#2260ad', btnhover='#3178d6',
            btnpress='#1c5296',
            altrow='#1a1e25', grid='#262c35', hover='#263241', sel='#1f3a5f',
            hdrbg='#232933', hdrtext='#aab3bd', hdrline='#30363d',
            sb='#3a4149', sbh='#4d5661', pvbar='#1c2129', pvline='#30363d',
            zoombg='#1f3a5f', zoomtext='#79b8ff', kpiwrap='#1a1e25',
            kpibg='#232933', kpiborder='#30363d', kpitext='#e6edf3',
            linebg='#16181d', lineborder='#3a4149',
            ghostbg='#232933', ghosttext='#cdd6df', ghostborder='#3a4149',
            ghosthover='#2a3240', ghostbordh='#4d7cc1',
            badbg='#262c35', badgetext='#9aa4ae', dlg='#1f232b',
            acc='#4493f8', green='#3fb950', delbg='#3a2226', delred='#f85149',
            hinttext='#6e7681', statusbg='#1a1e25', statustext='#9aa4ae',
        )
    else:
        C = dict(
            win='#eef1f6', panel='#ffffff', border='#e3e8ef', text='#24292f',
            sub='#8a9199', head0='#1f6feb', head1='#3a86ff',
            btn0='#2f7dff', btn1='#1f6feb', btnhover='#3a86ff',
            btnpress='#1a5fd0',
            altrow='#fafbfd', grid='#f1f3f6', hover='#f2f7ff', sel='#eaf2ff',
            hdrbg='#f7f9fc', hdrtext='#444c56', hdrline='#e6ebf2',
            sb='#ccd5e0', sbh='#a7b3c4', pvbar='#fbfcfe', pvline='#edf0f4',
            zoombg='#eaf2ff', zoomtext='#1f6feb', kpiwrap='#f7f9fc',
            kpibg='#ffffff', kpiborder='#e6ebf2', kpitext='#1c2430',
            linebg='#ffffff', lineborder='#d7dee7',
            ghostbg='#ffffff', ghosttext='#33404f', ghostborder='#d7dee7',
            ghosthover='#f3f7ff', ghostbordh='#9dc0ff',
            badbg='#f1f4f8', badgetext='#8a9199', dlg='#ffffff',
            acc='#1f6feb', green='#1a7f37', delbg='#ffe9e6', delred='#cf222e',
            hinttext='#aeb4bc', statusbg='#f7f9fc', statustext='#57606a',
        )
    return QSS_TMPL % C


# ======================================================================
#  开机动画: 大 ¥ 先压缩再弹开(~3s), 期间后台预加载解析模块
# ======================================================================
class Splash(QWidget):
    finished = Signal()
    DURATION = 1.2       # 动画总时长(秒): 够看清品牌又不用等
    FADE = 0.28          # 淡入/淡出时长(秒)

    def __init__(self):
        super().__init__(None, Qt.WindowType.FramelessWindowHint |
                         Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedSize(460, 470)
        scr = QGuiApplication.primaryScreen().availableGeometry()
        self.move(scr.x() + (scr.width() - 460) // 2,
                  scr.y() + (scr.height() - 470) // 2)
        self._t0 = _time.perf_counter()
        self._t = 0.0
        self._closing = False
        self._close_t = 0.0
        self._emitted = False
        self._skip = False        # 单击跳过动画
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._timer = QTimer(self)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._tick)

    # ---------- 生命周期 ----------
    def start(self):
        self._t0 = _time.perf_counter()
        self.show()
        self.raise_()
        self.activateWindow()
        self._timer.start()

    @property
    def elapsed(self):
        return self._t

    @property
    def skipped(self):
        return self._skip

    def skip(self):
        """单击/回车/空格 -> 立刻结束动画, 进度条打满。"""
        if self._emitted or self._closing:
            return
        self._skip = True
        self._emitted = True
        # 把时间轴整体前移到 DURATION: 之后每帧都从结束点继续, 淡出也能正常收尾
        self._t = self.DURATION
        self._t0 = _time.perf_counter() - self.DURATION
        self.finished.emit()
        self.update()

    def mousePressEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton:
            self.skip()
        super().mousePressEvent(ev)

    def keyPressEvent(self, ev):
        if ev.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter,
                        Qt.Key.Key_Space, Qt.Key.Key_Escape):
            self.skip()
            ev.accept()
            return
        super().keyPressEvent(ev)

    def begin_close(self):
        if not self._closing:
            self._closing = True
            self._close_t = self._t

    def _tick(self):
        self._t = _time.perf_counter() - self._t0
        if not self._emitted and self._t >= self.DURATION:
            self._emitted = True
            self.finished.emit()
        if self._closing and self._t - self._close_t >= self.FADE:
            self._timer.stop()
            self.close()
            self.deleteLater()
            return
        self.update()

    # ---------- 动画曲线 ----------
    @staticmethod
    def _smooth(k):
        k = max(0.0, min(1.0, k))
        return k * k * (3 - 2 * k)

    @staticmethod
    def _spring(u, amp, lam=2.2, w=5.5):
        """带阻尼的弹簧: u=0 时等于 1+amp, 初速度为 0, 平滑回落到 1。"""
        if u <= 0:
            return 1.0 + amp
        c = lam * amp / w          # 让 u=0 处一阶导为 0, 接缝处不折
        return 1.0 + math.exp(-lam * u) * (amp * math.cos(w * u)
                                           + c * math.sin(w * u))

    def _scale(self):
        """返回 (横向, 纵向): 快速压扁 → 阻尼回弹(带一点过冲) → 静止。"""
        if self._skip:
            return 1.0, 1.0          # 跳过 = 立刻静止(弹簧来不及收敛)
        t = self._t
        T = 0.55                                  # 压缩阶段
        if t < T:
            k = self._smooth(t / T)               # ease-in-out, 收尾零速度
            return 1.0 + 0.22 * k, 1.0 - 0.40 * k
        u = t - T
        return self._spring(u, 0.22), self._spring(u, -0.40)

    def _alpha(self):
        if self._closing:
            return max(0.0, 1.0 - (self._t - self._close_t) / self.FADE)
        return min(1.0, self._t / self.FADE)

    # ---------- 绘制 ----------
    def paintEvent(self, ev):
        a = self._alpha()
        if a <= 0:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setOpacity(a)

        # 卡片 + 阴影
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(15, 30, 60, 46))
        p.drawRoundedRect(QRectF(12, 18, 440, 444), 26, 26)
        p.setPen(QPen(QColor('#e3e8ef'), 1.6))
        p.setBrush(QColor('#ffffff'))
        p.drawRoundedRect(QRectF(8, 10, 440, 444), 26, 26)

        # 大 ¥(压缩/展开就是它在变形)
        sx, sy = self._scale()
        p.save()
        p.translate(228, 216)
        p.scale(sx, sy)
        p.setPen(QColor('#1f6feb'))
        f = QFont()
        f.setPixelSize(240)
        f.setBold(True)
        p.setFont(f)
        p.drawText(QRectF(-170, -160, 340, 320),
                   Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
                   '¥')
        p.restore()

        # 标题 + 进度条
        p.setPen(QColor('#57606a'))
        f2 = QFont()
        f2.setPixelSize(16)
        f2.setBold(True)
        p.setFont(f2)
        p.drawText(QRectF(8, 372, 440, 26),
                   Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
                   '发票助手')
        p.setPen(QColor('#8a9199'))
        f3 = QFont()
        f3.setPixelSize(12)
        p.setFont(f3)
        p.drawText(QRectF(8, 396, 440, 20),
                   Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
                   '本地解析 · 正在准备…')
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor('#e8eef7'))
        p.drawRoundedRect(QRectF(130, 426, 200, 8), 4, 4)
        p.setBrush(QColor('#1f6feb'))
        prog = max(0.0, min(1.0, self._t / self.DURATION))
        p.drawRoundedRect(QRectF(130, 426, 200 * prog, 8), 4, 4)

        # 单击跳过提示
        if 0.3 < self._t < self.DURATION:
            p.setPen(QColor('#b1b6bb'))
            f4 = QFont()
            f4.setPixelSize(11)
            p.setFont(f4)
            p.drawText(QRectF(8, 436, 440, 16),
                       Qt.AlignmentFlag.AlignHCenter
                       | Qt.AlignmentFlag.AlignVCenter,
                       '单击跳过')
        p.end()


def main():
    app = QApplication(sys.argv)
    app.setApplicationName('发票助手')
    app.setWindowIcon(MainWindow._make_icon())
    f = app.font()
    f.setFamilies(['Microsoft YaHei UI', 'Microsoft YaHei'])
    f.setPixelSize(13)
    f.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
    f.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
    app.setFont(f)
    # 全局样式由 MainWindow._apply_theme 按本机保存的主题生成(浅色/深色)

    bench = bool(os.environ.get('FAPIAO_BENCH'))

    # 1) 立刻弹开机动画
    splash = Splash()
    splash.start()

    # 2) 动画播放的同时后台预加载解析模块(PDF 渲染/解析引擎)
    ready = {'done': False}

    def _preload():
        try:
            import fitz  # noqa: F401
            import 发票解析  # noqa: F401
        except Exception:  # noqa: BLE001
            pass
        ready['done'] = True

    threading.Thread(target=_preload, daemon=True).start()

    # 3) 动画放完 且 预加载完成 -> 建主窗口; 最多等 8s 兜底
    box = {'win': None}
    poll = QTimer()
    poll.setInterval(80)

    def _poll():
        waited = _time.perf_counter() - _T0
        # 动画放完(或用户单击跳过) 且 预加载完成 -> 建主窗口; 最多等 8s 兜底
        anim_done = splash.skipped or splash.elapsed >= Splash.DURATION
        load_done = ready['done'] or splash.skipped
        if box['win'] is None and anim_done and (load_done or waited > 8.0):
            box['win'] = MainWindow()
            box['win'].show()
            splash.begin_close()
            if bench:
                print('startup: window %.3f s (preload %s)'
                      % (waited, ready['done']), flush=True)
                QTimer.singleShot(
                    400, lambda: print('startup: ready %.3f s'
                                       % (_time.perf_counter() - _T0),
                                       flush=True))
            poll.stop()

    poll.timeout.connect(_poll)
    poll.start()

    sys.exit(app.exec())


if __name__ == '__main__':
    main()
