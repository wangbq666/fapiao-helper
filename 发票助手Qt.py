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
import os
import sys

import fitz
from PySide6.QtCore import QObject, QMimeData, QRunnable, Qt, QThreadPool, QTimer, Signal
from PySide6.QtGui import (QBrush, QColor, QFont, QIcon,
                           QImage, QPainter, QPixmap)
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QFileDialog, QFrame, QGraphicsScene,
    QGraphicsPixmapItem, QGraphicsView, QGridLayout, QHBoxLayout, QHeaderView,
    QLabel, QMainWindow,
    QPushButton, QSlider, QSplitter, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from 发票解析 import extract_invoice  # noqa: E402

MIME_ROWS = 'application/x-invoice-rows'
CLIP = 'C:/Users/yqh/Desktop/nj542发票'


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


# ======================================================================
#  PDF 预览视图: 滚轮缩放 / 按住拖动 / 原生滚动条 / 高清重渲染
# ======================================================================
class PDFView(QGraphicsView):
    zoomChanged = Signal(float)
    hasDoc = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self._pages = []
        self._path = None
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

        self._rt = QTimer(self)
        self._rt.setSingleShot(True)
        self._rt.setInterval(200)
        self._rt.timeout.connect(self._rerender)

        self.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        self.setBackgroundBrush(QColor('#33383f'))
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.SmartViewportUpdate)

    @property
    def hint(self):
        return self._hint_lbl.text()

    @hint.setter
    def hint(self, s):
        self._hint_lbl.setText(s or '')
        self._hint_lbl.setVisible(bool(s))
        self._hint_lbl.raise_()
        self._layout_hint()

    def _layout_hint(self):
        self._hint_lbl.setGeometry(self.viewport().rect())

    # ---------------- 文档 ----------------
    def clear(self):
        self._scene.clear()
        self._pages = []
        self._path = None
        self._zoom = 1.0
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.hasDoc.emit(False)

    def load(self, path):
        self.clear()
        try:
            doc = fitz.open(path)
            rects = [(p.rect.width, p.rect.height) for p in doc]
            doc.close()
        except Exception:
            return False
        if not rects:
            return False
        self._path = path
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
            self._pages.append({'w': w, 'h': h, 'scale': s0, 'item': item, 'idx': i})
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
            doc = fitz.open(self._path)
            pix = doc[idx].get_pixmap(matrix=fitz.Matrix(scale, scale))
            doc.close()
            return QImage(pix.samples, pix.width, pix.height, pix.stride,
                          QImage.Format.Format_RGB888).copy()
        except Exception:
            return QImage()

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
        if not self._pages or not self._path:
            return
        target = self._render_scale()
        for p in self._pages:
            if abs(p['scale'] - target) < 0.12:
                continue
            img = self._render_page(p['idx'], target)
            if img.isNull():
                continue
            p['item'].setPixmap(QPixmap.fromImage(img))
            p['item'].setScale(1.0 / target)
            p['scale'] = target

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

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setColumnCount(4)
        self.setHorizontalHeaderLabels(['文件', '状态', '金额', '操作'])
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
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)

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
            paths = [p for p in paths if p.lower().endswith('.pdf')]
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
        self.setHorizontalHeaderLabels(self.COLS)
        self.verticalHeader().setVisible(False)
        self.verticalHeader().setDefaultSectionSize(34)
        self.setShowGrid(False)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.setAlternatingRowColors(True)
        self.setAcceptDrops(True)
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
        paths = [p for p in paths if p.lower().endswith('.pdf')]
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
        try:
            inv = extract_invoice(self.path)
        except Exception as e:  # noqa: BLE001
            inv = {'文件名': os.path.basename(self.path), '金额': None,
                   '税额': None, '价税合计': None, '备注': '解析失败: %s' % e,
                   '明细': []}
        self.sig.done.emit(self.path, inv)


# ======================================================================
#  主窗口
# ======================================================================
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle('发票助手')
        self.resize(1460, 900)
        self.setWindowIcon(self._make_icon())
        self.files = []
        self._seq = 0
        self._by_item = False
        self._cur_id = None
        self._pool = QThreadPool.globalInstance()
        self._build_ui()
        if os.path.isdir(CLIP):
            self.open_folder(CLIP)

    @staticmethod
    def _make_icon():
        img = QImage(64, 64, QImage.Format.Format_ARGB32)
        img.fill(QColor(0, 0, 0, 0))
        p = QPainter(img)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor('#1f6feb'))
        p.drawRoundedRect(4, 4, 56, 56, 14, 14)
        p.setPen(QColor('white'))
        f = QFont()
        f.setPixelSize(34)
        f.setBold(True)
        p.setFont(f)
        p.drawText(img.rect(), (Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter), '¥')
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
        sp.setSizes([450, 1010])
        vbox.addWidget(sp, 1)

    def _header(self):
        bar = QFrame()
        bar.setObjectName('header')
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(18, 0, 18, 0)
        lay.setSpacing(14)
        t = QLabel('🧾 发票助手')
        t.setObjectName('title')
        lay.addWidget(t)
        self.lbl_folder = QLabel('未打开文件夹')
        self.lbl_folder.setObjectName('folder')
        self.lbl_folder.setMaximumWidth(560)
        lay.addWidget(self.lbl_folder)
        self.lbl_sum = QLabel('报销 0 张 ｜ 价税合计 0.00')
        self.lbl_sum.setObjectName('sum')
        lay.addWidget(self.lbl_sum)
        lay.addStretch(1)
        bar.setFixedHeight(50)
        return bar

    def _left_panel(self):
        pan = QFrame()
        pan.setObjectName('panel')
        lay = QVBoxLayout(pan)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(9)

        h = QHBoxLayout()
        h.addWidget(QLabel('文件'))
        h.itemAt(0).widget().setObjectName('panelTitle')
        tip = QLabel('单击看原件 · 拖进来即加入报销')
        tip.setObjectName('tip')
        h.addWidget(tip)
        h.addStretch(1)
        lay.addLayout(h)

        r1 = QHBoxLayout()
        r1.setSpacing(7)
        self.btn_folder = QPushButton('📂 选择文件夹')
        self.btn_folder.clicked.connect(self.pick_folder)
        r1.addWidget(self.btn_folder)
        self.btn_addfile = QPushButton('＋选择文件')
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
        lay.addLayout(r2)

        self.file_table = FileTable()
        self.file_table.filesDropped.connect(lambda ps: self.add_paths(ps, join=True))
        self.file_table.cellClicked.connect(lambda r, c: self.preview_row(r))
        lay.addWidget(self.file_table, 1)

        self.lbl_others = QLabel('')
        self.lbl_others.setObjectName('others')
        self.lbl_others.setWordWrap(True)
        self.lbl_others.hide()
        lay.addWidget(self.lbl_others)
        return pan

    def _right_panel(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)
        sp = QSplitter(Qt.Orientation.Vertical)
        sp.setChildrenCollapsible(False)
        sp.addWidget(self._preview_panel())
        sp.addWidget(self._detail_panel())
        sp.setSizes([400, 500])
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
        h.addWidget(self.lbl_pvname, 1)
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

        self.pdf_view = PDFView()
        self.pdf_view.zoomChanged.connect(self._on_zoom)
        self.pdf_view.hasDoc.connect(self._on_hasdoc)
        lay.addWidget(self.pdf_view, 1)
        self.pdf_view.hint = ('单击左侧文件，在这里查看发票原件\n'
                              '滚轮缩放 · 按住左键拖动 · 滑块调缩放')
        return pan

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
        self.btn_byitem = QPushButton('按商品汇总')
        self.btn_byitem.setObjectName('ghost')
        self.btn_byitem.clicked.connect(self.toggle_byitem)
        h.addWidget(self.btn_byitem)
        b = QPushButton('清空报销')
        b.setObjectName('ghost')
        b.clicked.connect(self.leave_all)
        h.addWidget(b)
        b = QPushButton('导出 CSV')
        b.clicked.connect(self.export_csv)
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
    def pick_folder(self):
        d = QFileDialog.getExistingDirectory(self, '选择文件夹', CLIP)
        if d:
            self.open_folder(d)

    def open_folder(self, folder):
        try:
            names = os.listdir(folder)
        except Exception as e:  # noqa: BLE001
            self.statusBar().showMessage('打不开文件夹: %s' % e, 5000)
            return
        pdfs = sorted(n for n in names
                      if n.lower().endswith('.pdf')
                      and os.path.isfile(os.path.join(folder, n)))
        others = sorted(n for n in names
                        if os.path.isfile(os.path.join(folder, n))
                        and not n.lower().endswith('.pdf'))
        self.lbl_folder.setText(folder)
        self.lbl_folder.setToolTip(folder)
        for n in pdfs:
            self._push(n, os.path.join(folder, n))
        if others:
            self.lbl_others.setText(
                '其他文件 %d 个: %s%s' % (len(others), '、'.join(others[:6]),
                                     ' …' if len(others) > 6 else ''))
            self.lbl_others.show()
        self.refresh_files()

    def pick_files(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, '选择发票 PDF', CLIP, 'PDF 文件 (*.pdf);;所有文件 (*)')
        self.add_paths(paths, join=True)

    def add_paths(self, paths, join=True):
        for p in paths:
            if p.lower().endswith('.pdf') and os.path.isfile(p):
                self._push(os.path.basename(p), p, join)
        self.refresh_files()

    def _push(self, name, path, join=False):
        exist = next((f for f in self.files if f['path'] == path), None)
        if exist:
            if join and not exist['inReimb']:
                self.join(exist)
            return
        self._seq += 1
        f = {'id': self._seq, 'name': name, 'path': path,
             'inReimb': False, 'inv': None, 'parsing': False}
        self.files.append(f)
        if join:
            self.join(f)

    def by_id(self, fid):
        return next((f for f in self.files if f['id'] == fid), None)

    def refresh_files(self):
        cur = None
        f_cur = self.by_id(self._cur_id)
        if f_cur:
            cur = f_cur['path']
        self.file_table.setRowCount(0)
        for f in self.files:
            r = self.file_table.rowCount()
            self.file_table.insertRow(r)

            it = QTableWidgetItem('📄 ' + f['name'])
            it.setData(Qt.ItemDataRole.UserRole, f['id'])
            it.setToolTip(f['path'])
            if f['path'] == cur:
                it.setBackground(QColor('#eaf2ff'))
            self.file_table.setItem(r, 0, it)

            if f['parsing']:
                st, sc = '解析中', '#8a9199'
            elif f['inReimb']:
                st, sc = '报销', '#1a7f37'
            else:
                st, sc = '未加入', '#8a9199'
            c1 = QTableWidgetItem(st)
            c1.setForeground(QBrush(QColor(sc)))
            self.file_table.setItem(r, 1, c1)

            if f['inReimb']:
                if f['inv'] is None or f['inv'].get('价税合计') is None:
                    val, sc = '失败', '#cf222e'
                else:
                    val, sc = _fmt(f['inv'].get('价税合计')), '#1a7f37'
            else:
                val, sc = '', '#b1b6bb'
            c2 = QTableWidgetItem(val)
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

        self.lbl_cnt.setText('%d 个' % len(self.files))

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

    def remove_by_id(self, fid):
        f = self.by_id(fid)
        if not f:
            return
        self.files = [x for x in self.files if x['id'] != fid]
        if fid == self._cur_id:
            self.clear_preview()
        self.refresh_files()
        self.refresh_detail()

    def join(self, f):
        if f['inReimb'] or f['parsing']:
            return
        f['parsing'] = True
        self.refresh_files()
        task = ParseTask(f['path'])
        task.sig.done.connect(self._on_parsed)
        self._pool.start(task)

    def _on_parsed(self, path, inv):
        for f in self.files:
            if f['path'] == path:
                f['parsing'] = False
                f['inv'] = inv
                f['inReimb'] = True
                break
        self.refresh_files()
        self.refresh_detail()

    def join_all(self):
        for f in list(self.files):
            self.join(f)

    def leave_all(self):
        for f in self.files:
            f['inReimb'] = False
        self.refresh_files()
        self.refresh_detail()

    def join_rows(self, rows):
        for r in rows:
            it = self.file_table.item(r, 0)
            if it:
                f = self.by_id(it.data(Qt.ItemDataRole.UserRole))
                if f:
                    self.join(f)

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
        self.lbl_pvname.setText(f['name'])
        ok = self.pdf_view.load(f['path'])
        if not ok:
            self.pdf_view.hint = '无法打开该文件'
            self.pdf_view.viewport().update()
        self._on_zoom(1.0)
        self.refresh_files()

    def clear_preview(self):
        self._cur_id = None
        self.pdf_view.clear()
        self.lbl_pvname.setText('发票原件')
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
                row['inv'] = f['inv']
                out.append(row)
        return out

    def rows_grouped(self):
        m = {}
        for r in self.rows_flat():
            k = ''.join((r.get('名称') or '').split())
            if k not in m:
                m[k] = {'名称': r.get('名称'), '规格': '', '数量': 0, '单价': None,
                        '金额': 0, '税率': r.get('税率'), '税额': 0,
                        'src': r.get('src'), 'n': 0}
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
        rows = self.rows_grouped() if self._by_item else self.rows_flat()
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
                it = QTableWidgetItem(v)
                if c in (2, 3, 4, 6, 7):
                    it.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                if neg and c == 4:
                    it.setForeground(QBrush(QColor('#cf222e')))
                if c == 8:
                    it.setForeground(QBrush(QColor('#8a9199')))
                    it.setToolTip('点击查看原件')
                self.detail_table.setItem(rr, c, it)

        amt = tax = tot = 0.0
        n = 0
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
        self.kpis['tRows'].setText(str(len(rows)))
        self.lbl_items.setText('%d 项' % len(rows))
        self.lbl_sum.setText('报销 %d 张 ｜ 价税合计 %.2f' % (n, tot))

        if rows:
            rr = self.detail_table.rowCount()
            self.detail_table.insertRow(rr)
            fa = round(sum(r.get('金额') or 0 for r in rows), 2)
            ft = round(sum(r.get('税额') or 0 for r in rows), 2)
            vals = ['合计', '', '', '', '%.2f' % fa, '', '%.2f' % ft,
                    '%.2f' % (fa + ft), '']
            for c, v in enumerate(vals):
                it = QTableWidgetItem(v)
                it.setFont(QFont('', -1, QFont.Weight.Bold))
                it.setBackground(QBrush(QColor('#fffdf3')))
                if c in (4, 6, 7):
                    it.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                self.detail_table.setItem(rr, c, it)

    def _detail_clicked(self, row, col):
        if col != 8:
            return
        it = self.detail_table.item(row, 8)
        if not it:
            return
        f = next((x for x in self.files if x['name'] == it.text()), None)
        if f:
            self.preview(f)

    def toggle_byitem(self):
        self._by_item = not self._by_item
        self.btn_byitem.setText('按发票明细' if self._by_item else '按商品汇总')
        if self._by_item:
            self.btn_byitem.setStyleSheet(
                'background:#1f6feb; color:#fff; border:none; border-radius:7px; '
                'padding:6px 13px;')
        else:
            self.btn_byitem.setStyleSheet('')
        self.refresh_detail()

    # =================================================================
    #  CSV
    # =================================================================
    def export_csv(self):
        rows = self.rows_flat()
        if not rows:
            self.statusBar().showMessage('还没有加入报销的发票', 4000)
            return
        default = '报销商品明细_%s.csv' % datetime.date.today().isoformat()
        path, _ = QFileDialog.getSaveFileName(self, '导出 CSV', default,
                                              'CSV 文件 (*.csv)')
        if not path:
            return
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


# ======================================================================
#  样式
# ======================================================================
QSS = """
* { font-family: "Microsoft YaHei UI"; }
QMainWindow { background: #eef1f6; }
QWidget { background: transparent; color: #24292f; }
QFrame#header { background: qlineargradient(x1:0,y1:0,x2:1,y2:0,
    stop:0 #1f6feb, stop:1 #3a86ff); }
QLabel#title { color: #fff; font-size: 16px; font-weight: bold; background: transparent; }
QLabel#folder { color: #fff; font-size: 12px; background: rgba(255,255,255,45);
    border-radius: 10px; padding: 3px 10px; }
QLabel#sum { color: #fff; font-size: 13px; background: rgba(255,255,255,42);
    border-radius: 10px; padding: 5px 14px; }
QFrame#panel { background: #fff; border: 1px solid #e3e8ef; border-radius: 10px; }
QLabel#panelTitle { font-size: 14px; font-weight: bold; color: #1c2430;
    background: transparent; }
QLabel#tip { color: #8a9199; font-size: 12px; background: transparent; }
QLabel#badge { color: #8a9199; font-size: 12px; background: #f1f4f8;
    border-radius: 9px; padding: 2px 9px; }
QLabel#others { color: #9aa0a6; font-size: 11px; border-top: 1px dashed #edf0f4; }
QPushButton { background: qlineargradient(x1:0,y1:0,x2:0,y2:1,
    stop:0 #2f7dff, stop:1 #1f6feb); color: #fff; border: none;
    border-radius: 7px; padding: 6px 13px; }
QPushButton:hover { background: #3a86ff; }
QPushButton:pressed { background: #1a5fd0; }
QPushButton#ghost { background: #fff; color: #33404f; border: 1px solid #d7dee7; }
QPushButton#ghost:hover { background: #f3f7ff; border-color: #9dc0ff; }
QPushButton#tiny { padding: 3px 9px; border-radius: 6px; }
QPushButton#tiny#ghost, QPushButton#tiny.ghost {
    background: #fff; color: #33404f; border: 1px solid #d7dee7; }
QPushButton#tiny.ghost:hover { background: #f3f7ff; }
QPushButton#del { background: transparent; color: #b1b6bb; font-size: 15px;
    padding: 0; border: none; border-radius: 5px; }
QPushButton#del:hover { background: #ffe9e6; color: #cf222e; }
QTableWidget { background: #fff; alternate-background-color: #fafbfd;
    border: none; gridline-color: #f1f3f6; font-size: 13px; }
QTableWidget::item { padding: 4px; }
QTableWidget::item:selected { background: #eaf2ff; color: #24292f; }
QHeaderView::section { background: #f7f9fc; color: #57606a; font-size: 12px;
    border: none; border-bottom: 1px solid #e6ebf2; padding: 7px; }
QScrollBar:vertical { background: transparent; width: 12px; margin: 0; }
QScrollBar::handle:vertical { background: #c3ccd9; border-radius: 6px;
    min-height: 30px; }
QScrollBar::handle:vertical:hover { background: #a7b3c4; }
QScrollBar:horizontal { background: transparent; height: 12px; margin: 0; }
QScrollBar::handle:horizontal { background: #c3ccd9; border-radius: 6px;
    min-width: 30px; }
QScrollBar::handle:horizontal:hover { background: #a7b3c4; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }
QFrame#pvbar { background: #fbfcfe; border-bottom: 1px solid #edf0f4; }
QLabel#pvname { font-weight: bold; color: #33404f; background: transparent; }
QLabel#zoomval { color: #1f6feb; background: #eaf2ff; border-radius: 9px;
    padding: 2px 4px; min-width: 48px; }
QSlider::groove:horizontal { height: 5px; border-radius: 5px; background: #dde3ea; }
QSlider::sub-page:horizontal { background: #1f6feb; border-radius: 5px; }
QSlider::handle:horizontal { background: #fff; border: 2px solid #1f6feb;
    width: 14px; height: 14px; border-radius: 8px; margin: -6px 0; }
QLabel#pvempty { color: #aeb4bc; font-size: 14px; background: transparent; }
QFrame#detailbar { background: #fbfcfe; border-bottom: 1px solid #edf0f4; }
QFrame#kpiwrap { background: #f7f9fc; border-bottom: 1px solid #edf0f4; }
QFrame#kpi { background: #fff; border: 1px solid #e6ebf2; border-radius: 8px; }
QLabel#kpik { color: #8a9199; font-size: 11px; background: transparent; }
QLabel#kpiv { color: #1c2430; font-size: 16px; font-weight: bold;
    background: transparent; }
QFrame#kpi[kind="accent"] QLabel#kpiv { color: #1f6feb; }
QFrame#kpi[kind="green"] QLabel#kpiv { color: #1a7f37; }
QSplitter::handle { background: transparent; }
QStatusBar { background: #f7f9fc; color: #57606a; }
"""


def main():
    app = QApplication(sys.argv)
    app.setApplicationName('发票助手')
    f = app.font()
    f.setFamilies(['Microsoft YaHei UI', 'Microsoft YaHei'])
    f.setPixelSize(13)
    f.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
    f.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
    app.setFont(f)
    app.setStyleSheet(QSS)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == '__main__':
    main()
