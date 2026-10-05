# -*- coding: utf-8 -*-
"""
发票统计工具
============
一键统计指定文件夹内所有电子发票(PDF)的金额与商品明细。
自动识别发票号码、开票日期、销售方、金额(不含税)、税额、价税合计(含税),
并逐行列出每张发票的报销商品名称、数量、单价、金额、税率、税额。

用法:
  py 发票统计工具.py                    # 统计当前文件夹
  py 发票统计工具.py "D:\发票文件夹"     # 统计指定文件夹
  py 发票统计工具.py "D:\发票文件夹" -r  # 含子文件夹递归扫描

结果: 命令行打印汇总 + 导出 CSV 到目标文件夹。
  1) 发票统计_时间.csv   发票级明细(多一列"商品摘要")
  2) 报销商品明细_时间.csv 商品级明细表(每行一个商品)
"""
import glob
import os
import sys
import csv
import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from 发票解析 import extract_invoice  # noqa: E402


def summary(items):
    """把商品明细拼成一行摘要, 用于发票级 CSV 的"商品摘要"列."""
    parts = []
    for it in items:
        name = it['名称']
        # 去掉 "*类别*" 前缀, 更易读
        if name.startswith('*') and name.count('*') >= 2:
            name = name.split('*', 2)[2]
        seg = '%s×%s' % (name, _qty(it['数量'])) if it['数量'] else name
        if it['金额'] is not None:
            seg += ' %.2f' % it['金额']
        parts.append(seg)
    return '；'.join(parts)


def _qty(q):
    if q is None:
        return ''
    return str(int(q)) if float(q) == int(q) else ('%g' % q)


def scan_folder(folder, recursive=False):
    """扫描文件夹内所有 PDF."""
    pattern = os.path.join(folder, '**', '*.pdf') if recursive \
        else os.path.join(folder, '*.pdf')
    return sorted(glob.glob(pattern, recursive=recursive))


def main():
    args = [a for a in sys.argv[1:] if not a.startswith('-')]
    recursive = any(a in ('-r', '--recursive') for a in sys.argv[1:])

    default_dir = os.path.dirname(sys.executable) if getattr(
        sys, 'frozen', False) else os.getcwd()
    folder = args[0] if args else default_dir
    folder = os.path.abspath(folder)
    if not os.path.isdir(folder):
        print('文件夹不存在: %s' % folder)
        input('按回车键退出...')
        sys.exit(1)

    files = scan_folder(folder, recursive)
    if not files:
        print('文件夹内未找到 PDF 文件: %s' % folder)
        input('按回车键退出...')
        sys.exit(1)

    print('=' * 78)
    print('发票统计  |  文件夹: %s  |  扫描到 %d 个PDF'
          % (folder, len(files)))
    print('=' * 78)

    rows, n_ok, n_fail = [], 0, 0
    total_amt = total_tax = total_tot = 0.0
    item_rows = []          # 商品级明细
    total_item_amt = 0.0
    total_item_tax = 0.0

    for f in files:
        r = extract_invoice(f)
        rows.append(r)
        if r['价税合计'] is not None:
            n_ok += 1
            total_amt += r['金额']
            total_tax += r['税额']
            total_tot += r['价税合计']
            print('%-36s  金额:%9.2f  税额:%7.2f  价税合计:%9.2f'
                  % (r['文件名'][:34], r['金额'], r['税额'], r['价税合计']))
        else:
            n_fail += 1
            print('%-36s  [失败] %s' % (r['文件名'][:34], r['备注']))

        # 商品明细逐行打印
        for it in r['明细']:
            print('      · %-40s 数量%-6s 金额:%9.2f  税额:%7.2f (%s)'
                  % (it['名称'][:38], _qty(it['数量']),
                     it['金额'] or 0.0, it['税额'] or 0.0, it['税率'] or ''))
            item_rows.append([r['文件名'], r['发票号码'], r['开票日期'],
                              r['销售方'], it['名称'], it['规格'],
                              _qty(it['数量']), it['单价'], it['金额'],
                              it['税率'], it['税额']])
            total_item_amt += it['金额'] or 0.0
            total_item_tax += it['税额'] or 0.0

    print('-' * 78)
    print('有效发票: %d 张, 解析失败: %d 个, 商品明细: %d 行'
          % (n_ok, n_fail, len(item_rows)))
    if n_ok:
        print('金额合计(不含税) : %.2f' % total_amt)
        print('税额合计         : %.2f' % total_tax)
        print('价税合计(含税)   : %.2f' % total_tot)
    if item_rows:
        print('明细金额合计     : %.2f (税额 %.2f)'
              % (total_item_amt, total_item_tax))

    stamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')

    # 导出 CSV 1: 发票级(新增"商品摘要"列)
    out_csv = os.path.join(folder, '发票统计_%s.csv' % stamp)
    with open(out_csv, 'w', newline='', encoding='utf-8-sig') as fp:
        w = csv.writer(fp)
        w.writerow(['文件名', '发票号码', '开票日期', '销售方',
                    '金额(不含税)', '税额', '价税合计(含税)', '商品摘要', '备注'])
        for r in rows:
            w.writerow([r['文件名'], r['发票号码'], r['开票日期'], r['销售方'],
                        r['金额'] if r['金额'] is not None else '',
                        r['税额'] if r['税额'] is not None else '',
                        r['价税合计'] if r['价税合计'] is not None else '',
                        summary(r['明细']), r['备注']])
        if n_ok:
            w.writerow([])
            w.writerow(['合计', '', '', '',
                        '%.2f' % total_amt, '%.2f' % total_tax,
                        '%.2f' % total_tot, '', ''])
    print('明细已导出: %s' % out_csv)

    # 导出 CSV 2: 商品级明细表
    if item_rows:
        out_item = os.path.join(folder, '报销商品明细_%s.csv' % stamp)
        with open(out_item, 'w', newline='', encoding='utf-8-sig') as fp:
            w = csv.writer(fp)
            w.writerow(['来源文件', '发票号码', '开票日期', '销售方',
                        '商品名称', '规格型号', '数量', '单价',
                        '金额(不含税)', '税率', '税额', '价税合计'])
            for it in item_rows:
                amt = it[8] if it[8] is not None else 0.0
                tax = it[10] if it[10] is not None else 0.0
                w.writerow(it[:11] + ['%.2f' % (amt + tax)])
            w.writerow(['合计', '', '', '', '', '', '', '',
                        '%.2f' % total_item_amt, '',
                        '%.2f' % total_item_tax,
                        '%.2f' % (total_item_amt + total_item_tax)])
        print('商品明细已导出: %s' % out_item)

    if not os.environ.get('OP_NO_PAUSE'):
        input('按回车键退出...')


if __name__ == '__main__':
    main()
