# -*- coding: utf-8 -*-
"""
发票解析核心
============
从电子发票(PDF)中提取:
  1. 抬头信息: 发票号码 / 开票日期 / 销售方 / 金额 / 税额 / 价税合计
  2. 商品明细: 名称、规格型号、数量、单价、金额、税率、税额(逐行)

明细通过 PDF 内文字坐标还原表格列, 不依赖文本流顺序。
"""
import os
import re

import fitz

# 明细表列名(按表头文字规范化)
COLUMNS = ['项目名称', '规格型号', '单位', '数量', '单价', '金额', '税率', '税额']
# 明细行归属字段
FIELDS = ['name', 'spec', 'unit', 'qty', 'price', 'amount', 'rate', 'tax']

_NUM = re.compile(r'^-?[\d,]+(\.\d+)?$')


def _norm(s):
    """去掉空格/全角空格, 便于表头匹配."""
    return s.replace(' ', '').replace('\u3000', '').strip()


def _num(s):
    """文本转数字(保留两位小数), 失败返回 None."""
    try:
        return round(float(s.replace(',', '')), 2)
    except Exception:
        return None


def _lines_of(page):
    """取页面所有文本行: (中心x, 中心y, 文本)."""
    out = []
    for b in page.get_text('dict')['blocks']:
        if b.get('type') != 0:
            continue
        for l in b.get('lines', []):
            txt = ''.join(sp['text'] for sp in l.get('spans', []))
            if not txt.strip():
                continue
            x0, y0, x1, y1 = l['bbox']
            out.append(((x0 + x1) / 2, (y0 + y1) / 2, txt))
    return out


def _cluster(lines, tol=3.0):
    """按 y 坐标把文本行聚成视觉行, 行内按 x 排序."""
    rows = []
    for x, y, t in sorted(lines, key=lambda e: (e[1], e[0])):
        if rows and y - rows[-1][0] <= tol:
            rows[-1][1].append((x, t))
        else:
            rows.append([y, [(x, t)]])
    return [(y, sorted(cells)) for y, cells in rows]


def extract_items(doc):
    """从已打开的 fitz 文档提取商品明细, 返回 dict 列表."""
    items = []
    for page in doc:
        items.extend(_items_on_page(page))
    return items


def _items_on_page(page):
    lines = _lines_of(page)
    if not lines:
        return []

    # 1. 定位表头行
    header_y = None
    for x, y, t in lines:
        if '项目名称' in _norm(t):
            header_y = y
            break
    if header_y is None:
        return []

    # 2. 由表头得到各列中心 x
    centers = {}
    for x, y, t in lines:
        if abs(y - header_y) > 4:
            continue
        n = _norm(t)
        for col in COLUMNS:
            if col in n and col not in centers:
                centers[col] = x
    if '项目名称' not in centers or '金额' not in centers:
        return []
    cols = [(c, centers[c]) for c in COLUMNS if c in centers]

    # 3. 按视觉行聚类, 找明细区下边界(表头下方第一个含"合计"的行)
    rows = _cluster([l for l in lines if l[1] > header_y])
    end_i = len(rows)
    for i, (y, cells) in enumerate(rows):
        if '合计' in _norm(''.join(t for _, t in cells)):
            end_i = i
            break
    rows = rows[:end_i]

    # 4. 逐视觉行还原列值
    items = []
    pending = None          # 正在组装的明细行

    def flush(p):
        if p and p.get('rate') is not None and \
                (p.get('amount') is not None or p.get('tax') is not None):
            items.append({
                '名称': p.get('name', ''),
                '规格': p.get('spec', ''),
                '数量': p.get('qty'),
                '单价': p.get('price'),
                '金额': p.get('amount'),
                '税率': p.get('rate'),
                '税额': p.get('tax'),
            })
        return None

    for y, cells in rows:
        # 每个视觉行 -> 按最近列心归属
        vals = {}
        for x, t in cells:
            col = min(cols, key=lambda c: abs(c[1] - x))[0]
            vals[col] = vals.get(col, '') + t

        name = vals.get('项目名称', '').strip()
        if name.startswith('*'):
            # 新明细项开始: 先结算上一行
            pending = flush(pending)
            if pending is None:
                pending = {f: None for f in FIELDS}
            pending['name'] = name
        elif name:
            # 项目名称列的续行(换行折断的名称)
            if pending is None:
                pending = {f: None for f in FIELDS}
                pending['name'] = name
            else:
                pending['name'] = (pending.get('name') or '') + name

        if pending is None:
            continue

        spec = vals.get('规格型号', '').strip()
        if spec:
            pending['spec'] = (pending.get('spec') or '') + spec
        for col, field in (('单位', 'unit'), ('数量', 'qty'), ('单价', 'price'),
                           ('金额', 'amount'), ('税率', 'rate'), ('税额', 'tax')):
            if col in vals and vals[col].strip():
                v = vals[col].strip()
                if field == 'unit':
                    pending['unit'] = v
                elif field == 'rate':
                    m = re.search(r'[\d.]+', v.replace('％', '%'))
                    pending['rate'] = m.group(0) + '%' if m else v
                else:
                    n = _num(v) if _NUM.match(v.strip()) else None
                    if n is not None:
                        pending[field] = n

    flush(pending)
    return items


def extract_invoice(pdf_path):
    """从单个PDF提取发票信息(含商品明细), 返回 dict."""
    base = os.path.basename(pdf_path)
    try:
        doc = fitz.open(pdf_path)
        text = ''.join(p.get_text() for p in doc)
        items = extract_items(doc)
        doc.close()
    except Exception as e:
        return {'文件名': base, '金额': None, '税额': None, '价税合计': None,
                '备注': 'PDF读取失败: %s' % e, '明细': []}

    ys = re.findall(r'¥\s*([\d,]+\.\d{2})', text)
    num = re.search(r'发票号码[:：]*\s*(\d+)', text)
    date = re.search(r'(\d{4})年(\d{2})月(\d{2})日', text)

    # 销售方: 找以91开头的企业统一社会信用代码, 在其前后窗口内提取名称
    lines = text.splitlines()
    seller = ''
    code_at = None
    for i, l in enumerate(lines):
        if re.search(r'\b(91[0-9A-Z]{16})\b', l):
            code_at = i
    if code_at is not None:
        window = lines[max(0, code_at - 6):code_at + 7]
        kws = ('公司', '企业', '店', '科技')
        cands = []
        for l in window:
            s = l.strip()
            if not s:
                continue
            m = re.match(r'名称[:：]*\s*(.+)', s)
            if m:
                name = m.group(1).strip()
                cands.append((2 + sum(k in name for k in kws), name))
            elif '名称' not in s and ':' not in s and '：' not in s \
                    and sum(k in s for k in kws) > 0:
                cands.append((sum(k in s for k in kws), s))
        if cands:
            seller = max(cands, key=lambda c: c[0])[1]

    vals = sorted(float(y.replace(',', '')) for y in ys)

    a = tx = tot = None
    if len(vals) >= 3:
        # 用 金额 + 税额 == 价税合计 的关系自动识别三值
        found = False
        for i in range(len(vals)):
            for j in range(len(vals)):
                if i == j:
                    continue
                s = vals[i] + vals[j]
                if any(abs(s - vals[k]) < 0.01 for k in range(len(vals))
                       if k != i and k != j):
                    a, tx, tot = vals[i], vals[j], s
                    if tx > a:
                        a, tx = tx, a
                    found = True
                    break
            if found:
                break
        if not found:
            tot, a, tx = max(vals), max(vals), min(vals)
        a, tx, tot = round(a, 2), round(tx, 2), round(tot, 2)
        remark = ''
    else:
        remark = '未找到金额(可能非发票或版式特殊)'

    # 明细核对: 明细金额合计 应等于发票金额
    if items and a is not None:
        s_amt = round(sum(i['金额'] for i in items if i['金额']), 2)
        s_tax = round(sum(i['税额'] for i in items if i['税额']), 2)
        if abs(s_amt - a) > 0.05 or abs(s_tax - tx) > 0.05:
            remark = (remark + ';' if remark else '') + \
                '明细合计%.2f/%.2f 与票面%.2f/%.2f 不一致' % (s_amt, s_tax, a, tx)

    return {
        '文件名': base,
        '发票号码': num.group(1) if num else '',
        '开票日期': date.group(0) if date else '',
        '销售方': seller,
        '金额': a,
        '税额': tx,
        '价税合计': tot,
        '备注': remark,
        '明细': items,
    }
