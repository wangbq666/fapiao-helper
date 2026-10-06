# -*- coding: utf-8 -*-
"""历史记录功能验证: 只存加入报销的 / 按加入当天分组 / 跨重启 / 可重新加入"""
import glob
import io
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)                    # 仓库根目录
TMP = os.environ.get('TEMP') or HERE            # 临时目录(测试产物放这)
TEST_DIR = os.environ.get(                      # 测试发票目录(按需覆盖)
    'FAPIAO_TEST_DIR', r'C:\Users\yqh\Desktop\nj542发票')

HIST = os.path.join(TMP, 'hist_test.json')
if os.path.exists(HIST):
    os.remove(HIST)
os.environ['FAPIAO_HIST'] = HIST
os.environ['FAPIAO_SETTINGS'] = os.path.join(TMP, 'fp_test.ini')
os.environ['FAPIAO_NO_SESSION'] = '1'


sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8',
                              errors='replace')

sys.path.insert(0, ROOT)

import 发票助手Qt as M  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402

results = []


def check(name, cond, extra=''):
    results.append(bool(cond))
    print(('PASS ' if cond else 'FAIL ') + name + (' | ' + str(extra)
                                                   if extra else ''))


app = M.QApplication(sys.argv)
w = M.MainWindow()
w.show()
paths = sorted(glob.glob(os.path.join(TEST_DIR, '*.pdf')))

# 1) 只加入但不报销 → 不进历史
w.add_paths(paths, join=False)
t0 = time.time()
while time.time() - t0 < 5:
    app.processEvents()
    time.sleep(0.02)
check('未加入报销 → 不记历史', len(w.history) == 0, len(w.history))

# 2) 全部加入报销 → 记历史(按今天分组)
w.join_all()
t0 = time.time()
while time.time() - t0 < 60:
    app.processEvents()
    if not any(f['parsing'] for f in w.files):
        break
    time.sleep(0.02)
time.sleep(0.4)
for _ in range(20):
    app.processEvents()
    time.sleep(0.02)

today = __import__('datetime').date.today().isoformat()
check('加入报销后记入历史 = 27', len(w.history) == 27, len(w.history))
check('全部归到今天', {x['day'] for x in w.history} == {today},
      {x['day'] for x in w.history})
check('历史条目含金额', isinstance(w.history[0].get('total'), (int, float)),
      w.history[0].get('total'))
check('JSON 已落盘', os.path.isfile(HIST),
      os.path.getsize(HIST) if os.path.isfile(HIST) else 0)
with open(HIST, encoding='utf-8') as fh:
    disk = json.load(fh)
check('JSON 条数一致', len(disk.get('items') or []) == len(w.history),
      len(disk.get('items') or []))

# 3) 切到历史视图
w.toggle_history()
check('切到历史视图', w.left_stack.currentIndex() == 1,
      w.left_stack.currentIndex())
check('按钮变返回文件', '返回' in w.btn_hist.text(), w.btn_hist.text())
check('日期列表 1 天', w.hist_days.count() == 1, w.hist_days.count())
check('日期文案', '· 27 张' in w.hist_days.item(0).text(),
      w.hist_days.item(0).text())
check('当天发票 27 行', w.hist_items.rowCount() == 27,
      w.hist_items.rowCount())
check('计数标签', '27 张' in w.lbl_hist_cnt.text(), w.lbl_hist_cnt.text())

# 4) 点一条 → 右上预览
w._hist_click(0, 0)
app.processEvents()
check('点击历史 → 预览文件名', bool(w.lbl_pvfile.text()),
      w.lbl_pvfile.text())
check('预览路径正确', os.path.isfile(w.pdf_view._path)
      if hasattr(w.pdf_view, '_path') else True, '')

# 5) 已在报销的按钮禁用
btn = w.hist_items.cellWidget(0, 2).findChild(type(w.btn_addfile))
check('按钮=已在报销且禁用',
      btn is not None and btn.text() == '已在报销' and not btn.isEnabled(),
      (btn.text(), btn.isEnabled()) if btn else None)

# 6) 移出后再看 → 变成「加入」
w.leave_by_id(w.files[0]['id'])
w.toggle_history()
w.toggle_history()          # 重新进入历史会整表刷新
w._hist_refresh_items()
p0 = w.files[0]['path']
row0 = next((i for i in range(w.hist_items.rowCount())
             if w.hist_items.item(i, 0).data(
                 Qt.ItemDataRole.UserRole) == p0),
            -1)
btn2 = (w.hist_items.cellWidget(row0, 2).findChild(type(w.btn_addfile))
        if row0 >= 0 else None)
check('移出后该行按钮=加入',
      btn2 is not None and btn2.text() == '加入',
      (row0, btn2.text()) if btn2 else row0)

# 7) 从历史重新加入
w._hist_join(w.files[0]['path'])
t0 = time.time()
while time.time() - t0 < 20:
    app.processEvents()
    if not w.files[0]['parsing']:
        break
    time.sleep(0.02)
app.processEvents()
check('重新加入成功', w.files[0]['inReimb'], w.files[0]['inReimb'])
check('历史仍 27 条(同日去重)', len(w.history) == 27, len(w.history))

# 8) 切回文件视图
w.toggle_history()
check('切回文件视图', w.left_stack.currentIndex() == 0,
      w.left_stack.currentIndex())

# 9) 跨重启: 新窗口能读到
w2 = M.MainWindow()
check('重启后历史还在', len(w2.history) == 27, len(w2.history))

# 10) 清空
w.history = list(w2.history)
M.hist_save(w.history)
w3 = M.MainWindow()
check('独立实例读同一份', len(w3.history) == 27, len(w3.history))

ok = sum(results)
print('=' * 44)
print('PASS %d / %d' % (ok, len(results)))
sys.exit(0 if ok == len(results) else 1)
