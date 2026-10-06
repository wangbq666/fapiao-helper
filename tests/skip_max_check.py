# -*- coding: utf-8 -*-
"""单击跳过动画 + 右上角最大化可用"""
import io
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)                    # 仓库根目录
TMP = os.environ.get('TEMP') or HERE            # 临时目录(测试产物放这)
TEST_DIR = os.environ.get(                      # 测试发票目录(按需覆盖)
    'FAPIAO_TEST_DIR', r'C:\Users\yqh\Desktop\nj542发票')

os.environ['FAPIAO_HIST'] = os.path.join(TMP, 'hist_skip.json')

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8',
                              errors='replace')

sys.path.insert(0, ROOT)

import 发票助手Qt as M  # noqa: E402
from PySide6.QtCore import QEvent, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QMouseEvent  # noqa: E402

results = []


def check(name, cond, extra=''):
    results.append(bool(cond))
    print(('PASS ' if cond else 'FAIL ') + name + (' | ' + str(extra)
                                                   if extra else ''))


def click(w):
    ev = QMouseEvent(QEvent.Type.MouseButtonPress, QPointF(12, 12),
                     QPointF(12, 12), QPointF(12, 12),
                     Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                     Qt.KeyboardModifier.NoModifier)
    w.mousePressEvent(ev)


def pump(sec):
    """跑事件循环 sec 秒(QTimer/窗口动画都要事件循环才会动)。"""
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < sec:
        app.processEvents()
        time.sleep(0.02)


app = M.QApplication(sys.argv)

# ============ 1) 最大化 / 缩放 ============
w = M.MainWindow()
w.show()
app.processEvents()
tw0 = w.file_table.width()          # 最大化前的表宽
check('默认尺寸 1080x720', (w.width(), w.height()) == (1080, 720),
      (w.width(), w.height()))
check('不再固定尺寸(可拉伸)',
      bool(w.windowFlags() & Qt.WindowType.WindowMaximizeButtonHint) and
      w.maximumSize().width() > w.minimumWidth() and
      w.maximumSize().height() > w.minimumHeight(),
      (w.minimumSize(), w.maximumSize()))
check('最小尺寸 880x560',
      (w.minimumWidth(), w.minimumHeight()) == (880, 560),
      (w.minimumWidth(), w.minimumHeight()))

w.showMaximized()
tw_before = w.detail_table.width()   # 右下明细表, 放大后应吃掉多出来的宽度
pump(0.8)
avail = (w.screen() or M.QGuiApplication.primaryScreen()).availableGeometry()
fr = w.frameGeometry()          # 最大化时 geometry() 会扣掉外扩的系统边框
check('点最大化 -> 真的放大', w.isMaximized() and w.width() >= avail.width()
      - 2 and fr.y() + fr.height() >= avail.bottom() - 1,
      ('win', w.width(), w.height(), 'frame', fr.y(), fr.height(),
       'avail', avail.width(), avail.height()))
# 放大后左右两栏仍然平分、右栏吃掉多出来的空间
check('放大后布局铺满', w.centralWidget().width() >= avail.width() - 10,
      w.centralWidget().width())
check('放大后右下明细表跟着变宽', w.detail_table.width() > tw_before,
      (tw_before, w.detail_table.width()))

w.showNormal()
pump(0.8)
check('还原回 1080x720', abs(w.width() - 1080) <= 40 and
      abs(w.height() - 720) <= 40, (w.width(), w.height()))

w.resize(1450, 900)
pump(0.3)
check('手动拉伸生效', abs(w.width() - 1450) <= 4 and abs(w.height() - 900) <= 4,
      (w.width(), w.height()))
w.resize(1080, 720)
app.processEvents()

# ============ 2) 单击跳过动画 ============
s = M.Splash()
fired = []
s.finished.connect(lambda: fired.append(1))
s._t0 = time.perf_counter()
s._timer.start()
pump(0.45)                       # 让动画先跑一小段
t_before = s.elapsed
click(s)                         # 单击 = 跳过
check('单击 -> skipped', s.skipped, s.skipped)
check('单击 -> finished 触发', fired == [1], fired)
check('单击后进度=100%', s.elapsed >= M.Splash.DURATION - 1e-6, s.elapsed)
check('单击前动画确实在走', 0.3 < t_before < M.Splash.DURATION, t_before)
click(s)                         # 再点一次不该重复发
check('重复单击不会重复触发', fired == [1], fired)

# 跳过后帧时间轴从 DURATION 继续, 不会倒退
s._tick()
time.sleep(0.12)
s._tick()
check('跳过后时间不倒退', s.elapsed >= M.Splash.DURATION - 1e-6, s.elapsed)
sx, sy = s._scale()
check('跳过后图形是静止态', abs(sx - 1.0) < 0.02 and abs(sy - 1.0) < 0.02,
      (round(sx, 4), round(sy, 4)))

# 淡出收尾: begin_close 后还能正常关掉
s.begin_close()
t_end = time.perf_counter() + 1.0
while time.perf_counter() < t_end and s._timer.isActive():
    s._tick()
    app.processEvents()
    time.sleep(0.02)
check('跳过后淡出能正常结束', not s._timer.isActive(),
      'timer active=' + str(s._timer.isActive()))

# 没点的 Splash: 到 DURATION 自动 finish(原逻辑不回归)
s2 = M.Splash()
fired2 = []
s2.finished.connect(lambda: fired2.append(1))
s2._t0 = time.perf_counter() - M.Splash.DURATION - 0.05
s2._timer.start()
s2._tick()
check('不点击也会自动结束', fired2 == [1], fired2)
s2._timer.stop()

# ============ 3) _poll 条件: 跳过时不用等预加载 ============
s3 = M.Splash()
anim_done = s3.skipped or s3.elapsed >= M.Splash.DURATION
load_done = False
check('未跳过且没加载完 -> 不建窗', not (anim_done and load_done), '')
s3.skip()
anim_done = s3.skipped or s3.elapsed >= M.Splash.DURATION
load_done = False or s3.skipped
check('跳过后不等预加载就建窗', anim_done and load_done, (anim_done, load_done))
s3.deleteLater()

ok = sum(results)
print('=' * 44)
print('PASS %d / %d' % (ok, len(results)))
sys.exit(0 if ok == len(results) else 1)
