# -*- coding: utf-8 -*-
"""右下明细: 点任意一列 -> 右上显示对应发票原件"""
import glob
import io
import os
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)                    # 仓库根目录
TMP = os.environ.get('TEMP') or HERE            # 临时目录(测试产物放这)
TEST_DIR = os.environ.get(                      # 测试发票目录(按需覆盖)
    'FAPIAO_TEST_DIR', r'C:\Users\yqh\Desktop\nj542发票')

os.environ['FAPIAO_HIST'] = os.path.join(TMP, 'hist_dtl.json')
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


def pump(sec):
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < sec:
        app.processEvents()
        time.sleep(0.02)


app = M.QApplication(sys.argv)
w = M.MainWindow()
w.show()

# 造一个文件名里带 & 的发票(老实现靠文件名文本匹配, 会被 _esc 转义坑到)
src_dir = os.path.join(TMP, 'detail_src')
shutil.rmtree(src_dir, ignore_errors=True)
os.makedirs(src_dir)
srcs = sorted(glob.glob(os.path.join(TEST_DIR, '*.pdf')))[:3]
paths = []
for i, p in enumerate(srcs):
    name = 'A&B 测试%d.pdf' % i if i == 0 else os.path.basename(p)
    dst = os.path.join(src_dir, name)
    shutil.copy(p, dst)
    paths.append(dst)

w.add_paths(paths, join=True)
t0 = time.time()
while time.time() - t0 < 60:
    app.processEvents()
    if len(w.reimb_files()) == 3 and not any(f['parsing'] for f in w.files):
        break
    time.sleep(0.02)
pump(0.4)
check('3 张都进报销', len(w.reimb_files()) == 3, len(w.reimb_files()))
ids = [f['id'] for f in w.reimb_files()]

dt = w.detail_table
n_rows = dt.rowCount()
check('明细表有数据', n_rows >= 4, n_rows)   # 3 行商品 + 合计行


def fid_of(row):
    return dt.item(row, 0).data(Qt.ItemDataRole.UserRole)


# ---- 平铺: 点任意列都跳原件 ----
for col in (0, 2, 4, 8):
    w._detail_clicked(0, col)
    pump(0.1)
    check('点第 %d 列 → 预览对应发票' % col,
          w._cur_id == fid_of(0) and w._cur_id in ids,
          ('col', col, 'cur', w._cur_id, 'fid', fid_of(0)))

# ---- 第二行 / 第三行 ----
for row in (1, 2):
    w._detail_clicked(row, 0)
    pump(0.1)
    check('点第 %d 行 → 跟着跳' % row, w._cur_id == fid_of(row),
          (row, w._cur_id, fid_of(row)))

# ---- 文件名带 & 的那张能被找到 ----
amp = next((f for f in w.files if '&' in f['name']), None)
row_amp = next((r for r in range(dt.rowCount() - 1) if fid_of(r)
                == (amp['id'] if amp else -1)), -1)
w._detail_clicked(row_amp, 0)
pump(0.1)
check('文件名带 & 的也能定位', amp is not None and row_amp >= 0
      and w._cur_id == amp['id'],
      (amp['name'] if amp else None, row_amp, w._cur_id))

# ---- 合计行: 不跳转、不崩 ----
before = w._cur_id
w._detail_clicked(dt.rowCount() - 1, 0)
w._detail_clicked(dt.rowCount() - 1, 8)
pump(0.1)
check('合计行点击不改变预览', w._cur_id == before, w._cur_id)

# ---- 按商品汇总模式 ----
w.toggle_byitem()
pump(0.3)
g_rows = dt.rowCount()
check('汇总模式也有数据', g_rows >= 3, g_rows)
gfid = fid_of(0)
w._detail_clicked(0, 4)
pump(0.1)
check('汇总模式点行也跳原件', w._cur_id == gfid and w._cur_id in ids,
      (w._cur_id, gfid))
# 汇总后 src 可能被清空(多张发票同名商品), 但仍然要能跳
multi = next((r for r in range(dt.rowCount() - 1)
              if dt.item(r, 8) is not None and dt.item(r, 8).text() == ''
              and fid_of(r)), -1)
if multi >= 0:
    w._detail_clicked(multi, 8)
    pump(0.1)
    check('多张合并的行(来源为空)也能跳', w._cur_id == fid_of(multi),
          (multi, w._cur_id, fid_of(multi)))
else:
    check('多张合并的行(来源为空)也能跳', True, '本组无合并行, 跳过')

w.toggle_byitem()
pump(0.2)

# ---- 预览状态跟左侧文件表联动高亮 ----
w._detail_clicked(0, 0)
pump(0.1)
f_cur = w.by_id(w._cur_id)
check('预览的是列表里的文件', f_cur is not None, f_cur['name'] if f_cur else None)
check('预览文件名显示在右上', bool(w.lbl_pvfile.text()),
      w.lbl_pvfile.text())

ok = sum(results)
print('=' * 44)
print('PASS %d / %d' % (ok, len(results)))
sys.exit(0 if ok == len(results) else 1)
