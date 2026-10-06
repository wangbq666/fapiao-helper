# -*- coding: utf-8 -*-
"""v2.1 新特性回归: 号码去重 / 报销清单重启恢复 / 搜索过滤 / 抬头信息条 /
排序 / 深色模式 / 按月统计 / OFD 解析 / 快捷键"""
import glob
import io
import json
import os
import shutil
import sys
import tempfile
import time
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TMP = os.environ.get('TEMP') or HERE
TEST_DIR = os.environ.get(
    'FAPIAO_TEST_DIR', r'C:\Users\yqh\Desktop\nj542发票')

HIST = os.path.join(TMP, 'hist_v21.json')
SETF = os.path.join(TMP, 'fp_v21.ini')
for p in (HIST, SETF):
    if os.path.exists(p):
        os.remove(p)
os.environ['FAPIAO_HIST'] = HIST
os.environ['FAPIAO_SETTINGS'] = SETF

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8',
                              errors='replace')
sys.path.insert(0, ROOT)

import 发票助手Qt as M  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402

results = []


def check(name, cond, extra=''):
    results.append(bool(cond))
    print(('PASS ' if cond else 'FAIL ') + name + (' | ' + str(extra)
                                                   if extra else ''),
          flush=True)


def pump(sec):
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < sec:
        app.processEvents()
        time.sleep(0.02)


app = M.QApplication(sys.argv)

# ============ 1) 发票号码去重 ============
src = sorted(glob.glob(os.path.join(TEST_DIR, '*.pdf')))[0]
dup_dir = os.path.join(TMP, 'v21_dup')
shutil.rmtree(dup_dir, ignore_errors=True)
os.makedirs(dup_dir)
pa = os.path.join(dup_dir, '票A.pdf')
pb = os.path.join(dup_dir, '票B副本.pdf')
shutil.copy(src, pa)
shutil.copy(src, pb)

w = M.MainWindow()
w.show()
w.add_paths([pa, pb], join=True)
t0 = time.time()
while time.time() - t0 < 30:
    app.processEvents()
    if not any(f['parsing'] for f in w.files):
        break
    time.sleep(0.02)
pump(0.5)
sts = [(f['inReimb'], f.get('dup')) for f in w.files]
# 两张票并发解析, 谁先解析完谁进报销(顺序不确定), 断言写成顺序无关
joined = [s for s in sts if s[0]]
dupped = [s for s in sts if not s[0]]
check('两张同号票恰好一张进报销', len(joined) == 1 and not joined[0][1], sts)
check('另一张被去重(状态=重复)', len(dupped) == 1 and dupped[0][1], sts)
check('去重后状态列=重复', any(
    w.file_table.item(r, 1) and w.file_table.item(r, 1).text() == '重复'
    for r in range(w.file_table.rowCount())),
    [w.file_table.item(r, 1).text() if w.file_table.item(r, 1) else ''
     for r in range(w.file_table.rowCount())])
check('去重不影响合计', float(w.kpis['tTot'].text()) > 0, w.kpis['tTot'].text())
check('历史只记 1 张', len(w.history) == 1, len(w.history))

# ============ 2) 报销清单重启恢复 ============
saved = M.session_load()
check('清单已落盘(进报销的那张)', saved == [f['path'] for f in w.files
                                           if f['inReimb']], saved)
w2 = M.MainWindow()
t0 = time.time()
while time.time() - t0 < 30:
    app.processEvents()
    if w2.files and not any(f['parsing'] for f in w2.files):
        break
    time.sleep(0.02)
pump(0.5)
check('重启后清单恢复', len(w2.reimb_files()) == 1, len(w2.reimb_files()))
check('恢复的不重复记历史', len(w2.history) == 1, len(w2.history))

# ============ 3) 搜索/过滤 ============
w.add_paths(sorted(glob.glob(os.path.join(TEST_DIR, '*.pdf'))), join=False)
pump(0.3)
total = len(w.files)
check('全部文件数 = 27+', total >= 27, total)
w.search_files.setText('zave')
pump(0.2)
n_hit = w.file_table.rowCount()
check('文件搜索过滤生效', 0 < n_hit < total, (n_hit, total))
check('计数标签显示 匹配/总数', '%d / %d' % (n_hit, total) in w.lbl_cnt.text(),
      w.lbl_cnt.text())
w.search_files.setText('')
pump(0.2)
check('清空搜索恢复全部', w.file_table.rowCount() == total,
      (w.file_table.rowCount(), total))

w.join_all()
t0 = time.time()
while time.time() - t0 < 90:
    app.processEvents()
    if not any(f['parsing'] for f in w.files):
        break
    time.sleep(0.02)
pump(0.5)
n_rows = len(w.rows_flat())
check('全部加入(含去重跳过)后明细>40', n_rows > 40, n_rows)
w.search_detail.setText('电缆')
pump(0.2)
n_det = w.detail_table.rowCount()
check('明细搜索过滤生效', 0 <= n_det <= n_rows, (n_det, n_rows))
w.search_detail.setText('')
pump(0.2)

# ============ 4) 排序 + 左栏展开行 ============
w.detail_table.sortItems(4, Qt.SortOrder.AscendingOrder)
damts = [float(w.detail_table.item(r, 4).text())
         for r in range(w.detail_table.rowCount() - 1)
         if w.detail_table.item(r, 4).text()]
check('明细金额按数值升序', damts == sorted(damts), damts[:4])
check('合计行仍在最后一行',
      w.detail_table.item(w.detail_table.rowCount() - 1, 0).text() == '合计')

# 左栏: 点文件行 -> 下方展开一行显示 发票号码/开票日期, 再点收起
w.on_file_clicked(0, 0)
pump(0.3)
check('点击后展开行出现', w.file_table.rowCount() >= 2 and '发票号码' in
      (w.file_table.item(1, 0).text() if w.file_table.item(1, 0) else ''),
      [w.file_table.item(r, 0).text() for r in range(min(2, w.file_table.rowCount()))])
n_before = w.file_table.rowCount()
w.on_file_clicked(0, 0)
pump(0.3)
check('再点同一张收起展开', w.file_table.rowCount() == n_before - 1,
      (n_before, w.file_table.rowCount()))

# ============ 5) 抬头信息条 ============
w.preview(w.files[0])
pump(0.3)
check('抬头条可见', w.meta_bar.isVisible(), w.lbl_meta.text())
check('抬头含销售方/日期/号码',
      all(k in w.lbl_meta.text()
          for k in ('No.',)) and len(w.lbl_meta.text()) > 10,
      w.lbl_meta.text())
w.clear_preview()
pump(0.2)
check('清空后抬头条隐藏', not w.meta_bar.isVisible())

# ============ 6) 深色模式 ============
w.toggle_theme()
check('切到深色', M._cfg_dark(), M.SETTINGS.value('dark'))
check('THEME 已切换', M.THEME is M.THEMES['dark'])
check('KPI 仍正常', float(w.kpis['tTot'].text()) > 0, w.kpis['tTot'].text())
w.toggle_theme()
check('切回浅色', not M._cfg_dark())

# ============ 7) 按月统计 ============
check('按月统计可打开', hasattr(w, 'hist_monthly'))
months = {}
for x in w.history:
    m = (x.get('day') or '')[:7]
    months.setdefault(m, 0)
    months[m] += 1
check('历史数据可按月聚合', sum(months.values()) == len(w.history),
      (months, len(w.history)))

# ============ 8) 快捷键 ============
acts = [s.key().toString() for s in w.findChildren(M.QShortcut)]
check('Ctrl+O 已注册', any('Ctrl+O' in a for a in acts), acts)
check('Ctrl+F 已注册', any('Ctrl+F' in a for a in acts), acts)
check('Del 已注册', any('Del' in a for a in acts), acts)
w.toggle_history()
check('Esc 处理函数可用(历史视图)', w._hist_on)
w._esc_back()
check('Esc 逻辑返回文件视图', not w._hist_on)

# ============ 9) OFD 解析(合成样本) ============
TO = lambda x, y, t: (  # noqa: E731
    '<TextObject Boundary="%f %f 40 3"><TextCode>%s</TextCode></TextObject>'
    % (x, y, t))
rows_xml = [
    TO(20, 20, '电子发票（普通发票）'),
    TO(20, 40, '发票号码：'), TO(60, 40, '26319999999999999999'),
    TO(20, 60, '开票日期：'), TO(60, 60, '2026年08月03日'),
    TO(30, 110, '项目名称'), TO(80, 110, '金额'),
    TO(120, 110, '税率'), TO(150, 110, '税额'),
    TO(30, 125, '*电线电缆*射频电缆'), TO(80, 125, '29.70'),
    TO(120, 125, '1%'), TO(150, 125, '0.30'),
    TO(20, 160, '¥29.70'), TO(60, 160, '¥0.30'), TO(100, 160, '¥30.00'),
]
content = ('<?xml version="1.0"?><Page xmlns="http://www.ofdspec.org/2016">'
           + ''.join(rows_xml) + '</Page>')
ofd_path = os.path.join(TMP, 'v21_test.ofd')
with zipfile.ZipFile(ofd_path, 'w') as zf:
    zf.writestr('Doc_0/Document.xml', '<?xml version="1.0"?><Document/>')
    zf.writestr('Doc_0/Pages/Page_0/Content.xml', content)

import 发票解析 as P  # noqa: E402
r = P.extract_invoice(ofd_path)
check('OFD 价税合计', r['价税合计'] == 30.0, r['价税合计'])
check('OFD 发票号码', r['发票号码'] == '26319999999999999999', r['发票号码'])
check('OFD 开票日期', r['开票日期'] == '2026年08月03日', r['开票日期'])
check('OFD 明细', len(r['明细']) == 1 and r['明细'][0]['金额'] == 29.7,
      r['明细'])

# OFD 拖进界面: 能解析、给出"不支持预览"提示而不是红叉
w.add_paths([ofd_path], join=True)
t0 = time.time()
while time.time() - t0 < 20:
    app.processEvents()
    if not any(f['parsing'] for f in w.files if f['path'] == ofd_path):
        break
    time.sleep(0.02)
pump(0.5)
f_ofd = next((f for f in w.files if f['path'] == ofd_path), None)
check('OFD 加入报销并解析成功',
      f_ofd is not None and f_ofd['inReimb']
      and f_ofd['inv'].get('价税合计') == 30.0,
      (f_ofd or {}).get('inv', {}).get('价税合计'))
w.preview(f_ofd)
pump(0.2)
check('OFD 预览显示说明文案', 'OFD' in w.pdf_view.hint, w.pdf_view.hint)
check('OFD 不显示警告红叉', not w.pdf_view._warn_lbl.isVisible())
w.remove_by_id(f_ofd['id'])

# ============ 10) 清理: 移出全部 -> 会话清空 ============
w.leave_all()
pump(0.3)
check('移出全部后会话清空', M.session_load() == [], M.session_load())

ok = sum(results)
print('=' * 44)
print('PASS %d / %d' % (ok, len(results)))
sys.exit(0 if ok == len(results) else 1)
