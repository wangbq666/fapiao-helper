/* -*- coding: utf-8 -*-
 * 发票解析 —— 浏览器版(与 发票解析.py 同一套「按文字坐标还原明细表」逻辑)
 *
 * 依赖: 页面先加载 pdf.js, 再把已打开的 PDFDocumentProxy 传给 extractInvoice。
 * 纯前端运行, 文件不出本机。
 *
 * 实现要点:
 * - pdf.js 的 item 粒度与 pymupdf 接近(按文本流切分), 整项直接当单元格用;
 *   但有的发票流是逐字符切分、有的把空格丢了 —— 所以按「字符间距」把同一
 *   视觉行里的单元格做归并/补空格(阈值以行内字号为基准)。
 * - 抬头文本用归并后的视觉行; 销售方同时在「原始流顺序」和「视觉行」两份
 *   文本上找, 哪份先找到用哪份。
 */
(function (global) {
  'use strict';

  var COLUMNS = ['项目名称', '规格型号', '单位', '数量', '单价', '金额', '税率', '税额'];
  var FIELDS = { 单位: 'unit', 数量: 'qty', 单价: 'price', 金额: 'amount', 税率: 'rate', 税额: 'tax' };
  var NUM_RE = /^-?[\d,]+(\.\d+)?$/;

  function norm(s) { return String(s == null ? '' : s).replace(/[\s\u3000]/g, ''); }

  function toNum(s) {
    var v = parseFloat(String(s).replace(/,/g, ''));
    return isNaN(v) ? null : Math.round(v * 100) / 100;
  }

  /* 视觉行内: 按字符间距把相邻单元格合并 / 补空格 / 分列。
   * gap > 0.6×字号 → 新单元格; gap > 0.25×字号 → 补一个空格; 其余直接拼接。
   * (列间隙一般 ≥8pt, 词内字距 ≈0, 空格 ≈4.5pt) */
  function compactRow(cells) {
    var hs = cells.map(function (c) { return c.h || 9; }).sort(function (a, b) { return a - b; });
    var hRef = hs[Math.floor(hs.length / 2)] || 9;
    var out = [];
    cells.forEach(function (c) {
      if (!out.length) { out.push(Object.assign({}, c)); return; }
      var prev = out[out.length - 1];
      var gap = c.left - prev.right;
      if (gap > hRef * 0.6) {
        out.push(Object.assign({}, c));
      } else {
        prev.t = prev.t + (gap > hRef * 0.35 ? ' ' : '') + c.t;
        prev.right = Math.max(prev.right, c.right);
        prev.x = (prev.left + prev.right) / 2;
      }
    });
    return out;
  }

  /* 按中心 y 把文字项聚成视觉行, 行内按 x 排序后做间距归并(对应 py 的 _cluster) */
  function cluster(items, tol) {
    tol = tol || 3.0;
    var sorted = items.slice().sort(function (a, b) { return (a.y - b.y) || (a.x - b.x); });
    var rows = [];
    sorted.forEach(function (it) {
      var last = rows[rows.length - 1];
      if (last && it.y - last.y <= tol) last.cells.push(it);
      else rows.push({ y: it.y, cells: [it] });
    });
    rows.forEach(function (r) {
      r.cells.sort(function (a, b) { return a.left - b.left; });
      r.cells = compactRow(r.cells);
      r.text = r.cells.map(function (c) { return c.t; }).join('');
    });
    return rows;
  }

  function blank() {
    return { name: null, spec: null, unit: null, qty: null, price: null, amount: null, rate: null, tax: null };
  }

  /* 与 py 的 _items_from_lines 一致: 从视觉行还原商品明细 */
  function itemsFromLines(lines) {
    if (!lines.length) return [];

    // 1. 定位表头行
    var headerY = null;
    lines.forEach(function (l) {
      if (headerY === null && norm(l.text).indexOf('项目名称') >= 0) headerY = l.y;
    });
    if (headerY === null) return [];

    // 2. 由表头行得到各列中心 x: 在归一化行文本里定位列名, 映射回覆盖的 cell
    //    取平均 x。兼容「单 位」被拆成两个 cell、列名里带空格的情况。
    function colCenter(l, col) {
      var parts = l.cells.map(function (c) { return norm(c.t); });
      var textN = parts.join('');
      var idx = textN.indexOf(col);
      if (idx < 0) return null;
      var pos = 0, xs = [];
      l.cells.forEach(function (c, i) {
        var s = pos, e = pos + parts[i].length;
        pos = e;
        if (e > idx && s < idx + col.length) xs.push(c.x);
      });
      if (!xs.length) return null;
      return xs.reduce(function (a, b) { return a + b; }, 0) / xs.length;
    }
    var centers = {};
    lines.forEach(function (l) {
      if (Math.abs(l.y - headerY) > 4) return;
      COLUMNS.forEach(function (col) {
        if (!(col in centers)) {
          var cx = colCenter(l, col);
          if (cx !== null) centers[col] = cx;
        }
      });
    });
    if (!('项目名称' in centers) || !('金额' in centers)) return [];
    var cols = COLUMNS.filter(function (c) { return c in centers; })
      .map(function (c) { return [c, centers[c]]; });

    // 3. 表头下方的视觉行, 到第一个含「合计」的行为止
    var rows = cluster(
      lines.filter(function (l) { return l.y > headerY; })
        .reduce(function (acc, l) { return acc.concat(l.cells); }, []));
    var endI = rows.length;
    for (var i = 0; i < rows.length; i++) {
      if (norm(rows[i].text).indexOf('合计') >= 0) { endI = i; break; }
    }
    rows = rows.slice(0, endI);

    // 4. 逐视觉行归属到列, 还原明细
    var out = [];
    var pending = null;
    function flush(p) {
      if (p && p.rate !== null && (p.amount !== null || p.tax !== null)) {
        out.push({
          '名称': p.name || '', '规格': p.spec || '', '数量': p.qty,
          '单价': p.price, '金额': p.amount, '税率': p.rate, '税额': p.tax,
        });
      }
      return null;
    }
    rows.forEach(function (row) {
      var vals = {};
      var lastRight = {};      // 各列最近一次收到的 cell 右边界
      var hRef = row.cells.map(function (c) { return c.h || 9; })
        .sort(function (a, b) { return a - b; })[0] || 9;
      row.cells.forEach(function (c) {
        var best = null, bd = Infinity;
        // 「单位」是窄列, 宽文本(>1字)常被它抢走 —— 多字文本不参与单位竞争
        var cands = (norm(c.t).length > 1)
          ? cols.filter(function (p) { return p[0] !== '单位'; })
          : cols;
        cands.forEach(function (pair) {
          var d = Math.abs(pair[1] - c.x);
          if (d < bd) { bd = d; best = pair[0]; }
        });
        var t = c.t;
        if (vals[best] !== undefined &&
            c.left - (lastRight[best] || 0) > (hRef || 9) * 0.25) {
          t = ' ' + t;         // 同列里隔着空隙续上的文本, 补回空格
        }
        vals[best] = (vals[best] || '') + t;
        lastRight[best] = c.right;
      });

      var name = (vals['项目名称'] || '').trim();
      if (name.charAt(0) === '*') {
        pending = flush(pending);
        if (pending === null) pending = blank();
        pending.name = name;
      } else if (name) {
        if (pending === null) { pending = blank(); pending.name = name; }
        else pending.name = (pending.name || '') + name;
      }
      if (pending === null) return;

      var spec = (vals['规格型号'] || '').trim();
      if (spec) pending.spec = (pending.spec || '') + spec;

      COLUMNS.forEach(function (col) {
        var field = FIELDS[col];
        if (!field) return;
        var v = (vals[col] || '').trim();
        if (!v) return;
        if (field === 'unit') { pending.unit = v; return; }
        if (field === 'rate') {
          var m = v.replace('％', '%').match(/[\d.]+/);
          pending.rate = m ? m[0] + '%' : v;
          return;
        }
        var n = NUM_RE.test(v) ? toNum(v) : null;
        if (n !== null) pending[field] = n;
      });
    });
    flush(pending);
    return out;
  }

  /* 与 py 的 _finish 一致: 抬头金额三值识别 + 销售方 + 明细核对。
   * seller 在 rawText 和 rowText 两份文本上各找一次, 谁先找到用谁。 */
  function findSeller(text) {
    var lines = text.split('\n');
    var codeAt = -1;
    lines.forEach(function (l, i) {
      if (/(^|[^0-9A-Za-z])91[0-9A-Z]{16}(?![0-9A-Za-z])/.test(l)) codeAt = i;
    });
    if (codeAt < 0) return '';
    var kws = ['公司', '企业', '店', '科技'];
    var cands = [];
    function push(name, score) {
      name = name.replace(/^[\s:：；;]+/, '').trim();
      if (name.length >= 4) cands.push({ s: score, name: name });
    }
    lines.slice(Math.max(0, codeAt - 6), codeAt + 7).forEach(function (raw) {
      var s = raw.trim();
      if (!s) return;
      var mm = s.match(/^名称[:：]*(.+)$/);
      if (mm) {
        var name = mm[1].trim();
        var sc = 2;
        kws.forEach(function (k) { if (name.indexOf(k) >= 0) sc++; });
        push(name, sc);
      } else if (s.indexOf('名称') < 0 && s.indexOf(':') < 0 && s.indexOf('：') < 0) {
        var sc2 = 0;
        kws.forEach(function (k) { if (s.indexOf(k) >= 0) sc2++; });
        if (sc2 > 0) push(s, sc2);
      }
    });
    if (!cands.length) return '';
    var best = cands[0];
    cands.forEach(function (c) { if (c.s > best.s) best = c; });
    return best.name.replace(/\s+/g, '');
  }

  function finish(base, rowText, rawText, items) {
    var text = rowText;
    var remark = '';
    var ys = [];
    (text.match(/¥\s*([\d,]+\.\d{2})/g) || []).forEach(function (m) {
      ys.push(parseFloat(m.replace(/[¥,\s]/g, '')));
    });
    var m = text.match(/发票号码[:：]*\s*(\d+)/);
    if (!m) m = text.match(/(?<!\d)(2\d{19})(?!\d)/);   // 数电票兜底
    var invNum = m ? (m[2] || m[1]) : '';
    var dm = text.match(/(\d{4})年(\d{2})月(\d{2})日/);
    var date = dm ? dm[0] : '';

    var seller = findSeller(rawText) || findSeller(rowText);

    var vals = ys.slice().sort(function (a, b) { return a - b; });
    var a = null, tx = null, tot = null;
    if (vals.length >= 3) {
      var found = false;
      for (var i = 0; i < vals.length && !found; i++) {
        for (var j = 0; j < vals.length && !found; j++) {
          if (i === j) continue;
          var sum = vals[i] + vals[j];
          for (var k = 0; k < vals.length; k++) {
            if (k === i || k === j) continue;
            if (Math.abs(sum - vals[k]) < 0.01) {
              a = vals[i]; tx = vals[j]; tot = sum;
              if (tx > a) { var tmp = a; a = tx; tx = tmp; }
              found = true; break;
            }
          }
        }
      }
      if (!found) { tot = vals[vals.length - 1]; a = vals[vals.length - 1]; tx = vals[0]; }
      var r2 = function (v) { return Math.round(v * 100) / 100; };
      a = r2(a); tx = r2(tx); tot = r2(tot);
    } else {
      remark = '未找到金额(可能非发票或版式特殊)';
    }

    if (items.length && a !== null) {
      var sAmt = 0, sTax = 0;
      items.forEach(function (it) { sAmt += it['金额'] || 0; sTax += it['税额'] || 0; });
      sAmt = Math.round(sAmt * 100) / 100;
      sTax = Math.round(sTax * 100) / 100;
      if (Math.abs(sAmt - a) > 0.05 || Math.abs(sTax - tx) > 0.05) {
        remark = (remark ? remark + ';' : '') +
          '明细合计' + sAmt.toFixed(2) + '/' + sTax.toFixed(2) +
          ' 与票面' + a.toFixed(2) + '/' + tx.toFixed(2) + ' 不一致';
      }
    }

    return {
      '文件名': base, '发票号码': invNum, '开票日期': date, '销售方': seller,
      '金额': a, '税额': tx, '价税合计': tot, '备注': remark, '明细': items,
    };
  }

  /* pdf.js 的文字项 -> 带左右边界的单元格; y 转成向下为正 */
  function pageCells(tc, pageHeight) {
    var items = [];
    (tc.items || []).forEach(function (it) {
      if (!it.str || !it.str.trim()) return;
      var left = it.transform[4];
      items.push({
        left: left,
        right: left + (it.width || 0),
        x: left + (it.width || 0) / 2,
        y: pageHeight - (it.transform[5] + (it.height || 9) * 0.35),
        h: it.height || 9,
        t: it.str,
      });
    });
    return items;
  }

  /* 主入口: doc = pdf.js PDFDocumentProxy */
  async function extractInvoice(doc, fileName) {
    var items = [];
    var rowTexts = [];
    var rawLines = [];
    try {
      for (var i = 0; i < doc.numPages; i++) {
        var page = await doc.getPage(i + 1);
        var vp = page.getViewport({ scale: 1 });
        var tc = await page.getTextContent();
        var cells = pageCells(tc, vp.height);
        var rows = cluster(cells);
        items = items.concat(itemsFromLines(rows));
        rows.forEach(function (r) { rowTexts.push(r.text); });
        (tc.items || []).forEach(function (it) {
          if (it.str && it.str.trim()) rawLines.push(it.str);
        });
        page.cleanup();
      }
    } catch (e) {
      return {
        '文件名': fileName, '金额': null, '税额': null, '价税合计': null,
        '备注': 'PDF读取失败: ' + e, '明细': [],
      };
    }
    return finish(fileName, rowTexts.join('\n'), rawLines.join('\n'), items);
  }

  global.FapiaoWeb = {
    extractInvoice: extractInvoice,
    itemsFromLines: itemsFromLines,
    cluster: cluster,
  };
})(typeof window !== 'undefined' ? window : globalThis);
