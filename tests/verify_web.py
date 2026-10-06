# -*- coding: utf-8 -*-
"""JS 版解析器差分验证: 用 pdfjs-dist(node) 跑 发票解析.web.js,
逐张与 发票解析.py 的结果对比(抬头三值/号码/日期/明细行数/明细合计)。
运行: 先 npm install, 再 py tests/verify_web.py(它会调 node)。
"""
import glob
import io
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TEST_DIR = os.environ.get('FAPIAO_TEST_DIR', r'C:\Users\yqh\Desktop\nj542发票')

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8',
                              errors='replace')

NODE_SCRIPT = r'''
import * as pdfjsLib from 'pdfjs-dist/legacy/build/pdf.mjs';
import fs from 'fs';
import path from 'path';
import url from 'url';

const root = path.dirname(url.fileURLToPath(import.meta.url));
const spec = JSON.parse(fs.readFileSync(process.argv[2], 'utf-8'));
globalThis.window = globalThis;          // 解析.js 挂到 window 上
const code = fs.readFileSync(path.join(root, '..', '..', '发票解析.web.js'), 'utf-8');
new Function(code)();                    // 执行解析器, 定义 window.FapiaoWeb
const P = globalThis.FapiaoWeb;

const out = [];
for (const f of spec.files) {
  const data = new Uint8Array(fs.readFileSync(f));
  const doc = await pdfjsLib.getDocument({ data, useSystemFonts: true }).promise;
  const inv = await P.extractInvoice(doc, path.basename(f));
  await doc.destroy();
  out.push(inv);
}
console.log(JSON.stringify(out));
'''

results = []


def check(name, cond, extra=''):
    results.append(bool(cond))
    print(('PASS ' if cond else 'FAIL ') + name + (' | ' + str(extra)
                                                   if extra else ''))


def main():
    os.makedirs(os.path.join(ROOT, 'buildqt'), exist_ok=True)
    tmp = os.path.join(ROOT, 'buildqt', '_webtest')
    os.makedirs(tmp, exist_ok=True)
    node_script = os.path.join(tmp, 'run.mjs')
    spec_file = os.path.join(tmp, 'spec.json')
    out_file = os.path.join(tmp, 'out.json')
    with open(node_script, 'w', encoding='utf-8') as fh:
        fh.write(NODE_SCRIPT)

    pdfs = sorted(glob.glob(os.path.join(TEST_DIR, '*.pdf')))
    check('测试样本 = 27 张', len(pdfs) == 27, len(pdfs))
    with open(spec_file, 'w', encoding='utf-8') as fh:
        json.dump({'files': pdfs}, fh, ensure_ascii=False)

    env = dict(os.environ, NODE_OPTIONS='')
    r = subprocess.run(
        ['node', node_script, spec_file],
        capture_output=True, text=True, encoding='utf-8',
        errors='replace', cwd=ROOT, env=env, timeout=300)
    if r.returncode != 0:
        check('node 跑通', False, r.stderr[-2000:])
        return
    js = json.loads(r.stdout.strip().splitlines()[-1])

    # Python 侧真值
    sys.path.insert(0, ROOT)
    import 发票解析 as P
    py = [P.extract_invoice(p) for p in pdfs]

    check('JS 解析张数一致', len(js) == len(py), (len(js), len(py)))
    for j, p in zip(js, py):
        name = p['文件名']
        ok_fields = all(
            (j.get(k) or '') == (p.get(k) or '') for k in
            ('发票号码', '开票日期', '销售方'))
        ok_amt = (j.get('金额') == p.get('金额') and
                  j.get('税额') == p.get('税额') and
                  j.get('价税合计') == p.get('价税合计'))
        jd, pd_ = j.get('明细') or [], p.get('明细') or []
        def eq(a, b, ws_insensitive=False):
            if a is None or a == '':
                a = None
            if b is None or b == '':
                b = None
            if a is None and b is None:
                return True
            if isinstance(a, (int, float)) and isinstance(b, (int, float)):
                return abs(a - b) < 1e-9
            if isinstance(a, str) and isinstance(b, str):
                if ws_insensitive:
                    return a.replace(' ', '') == b.replace(' ', '')
                return ' '.join(a.split()) == ' '.join(b.split())
            return a == b

        ok_rows = len(jd) == len(pd_)
        diff = ''
        if not ok_rows:
            diff = 'js行数=%d py行数=%d' % (len(jd), len(pd_))
        else:
            for a, b in zip(jd, pd_):
                # 网页版没有 pymupdf 的字体度量: 数量/单价 在窄列抢位或
                # 逐字符流的票上可能丢失/偏差 —— 宽限; 金额字段必须一致
                if not all(eq(a.get(k), b.get(k), ws_insensitive=True)
                           for k in ('名称', '规格')):
                    diff = 'js=%r py=%r' % (a, b)
                    break
                if not all(eq(a.get(k), b.get(k)) for k in
                           ('金额', '税率', '税额')):
                    diff = 'js=%r py=%r' % (a, b)
                    break
        ok_detail = ok_rows and not diff
        check('%s 抬头' % name[:18], ok_fields,
              (j.get('发票号码'), j.get('销售方')[:10],
               p.get('销售方')[:10]))
        check('%s 金额三值' % name[:18], ok_amt,
              (j.get('金额'), j.get('税额'), j.get('价税合计')))
        check('%s 明细' % name[:18], ok_detail, diff)

    tot_js = sum(v['价税合计'] for v in js if v['价税合计'])
    tot_py = sum(v['价税合计'] for v in py if v['价税合计'])
    rows_js = sum(len(v['明细']) for v in js)
    rows_py = sum(len(v['明细']) for v in py)
    check('价税合计总和一致(1707.22)',
          abs(tot_js - tot_py) < 0.005 and abs(tot_py - 1707.22) < 0.01,
          (round(tot_js, 2), round(tot_py, 2)))
    check('明细行数一致(46)', rows_js == rows_py == 46, (rows_js, rows_py))

    print('=' * 44)
    print('PASS %d / %d' % (sum(results), len(results)))
    sys.exit(0 if all(results) else 1)


if __name__ == '__main__':
    main()
