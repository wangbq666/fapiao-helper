# -*- coding: utf-8 -*-
"""
发票助手 - 本地网页应用
========================
双击运行(或打包成 exe 后运行), 自动打开浏览器界面:

  左侧   : 输入/浏览文件夹, 正常显示文件列表
  右上   : 拖拽框, 支持从桌面拖 PDF, 也支持从左侧列表拖文件进来
  右下   : 实时显示报销商品明细(名称/金额/税率/税额)与合计

接口:
  GET  /                 界面
  POST /api/scan         {"path": "...", "recursive": false} -> 文件列表
  POST /api/parse        {"paths": [...]}  -> 解析结果
  POST /api/upload?name= 原始文件字节 -> 解析结果
"""
import glob
import json
import os
import re
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fitz  # noqa: E402  (pymupdf, 预览渲染用)
from 发票解析 import extract_invoice  # noqa: E402

PORT = int(os.environ.get('FP_PORT', '8765'))
APP_DIR = os.path.dirname(os.path.abspath(__file__))
_imgcache = {}  # (路径, mtime, 页码, 缩放) -> PNG 字节


def _json(obj):
    return json.dumps(obj, ensure_ascii=False).encode('utf-8')


class Handler(BaseHTTPRequestHandler):
    # ---------- 基础 ----------
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype='application/json; charset=utf-8'):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get('Content-Length') or 0)
        return self.rfile.read(n) if n else b''

    def _json_body(self):
        try:
            return json.loads(self._body().decode('utf-8'))
        except Exception:
            return {}

    # ---------- 路由 ----------
    def do_POST(self):
        path = urlparse(self.path).path
        try:
            if path == '/api/scan':
                self._scan()
            elif path == '/api/parse':
                self._parse()
            elif path == '/api/upload':
                self._upload()
            elif path == '/api/save':
                self._save()
            else:
                self._send(404, _json({'error': 'not found'}))
        except Exception as e:  # noqa: BLE001
            self._send(500, _json({'error': '%s' % e}))

    def do_GET(self):
        path = urlparse(self.path).path
        try:
            if path in ('/', '/index.html'):
                with open(os.path.join(APP_DIR, chr(32593)+chr(39029)+chr(29256)+chr(26087), 'index.html'), 'rb') as f:
                    self._send(200, f.read(), 'text/html; charset=utf-8')
            elif path == '/api/file':
                self._file()
            elif path == '/api/pages':
                self._pages()
            elif path == '/api/pageimg':
                self._pageimg()
            else:
                self._send(404, _json({'error': 'not found'}))
        except Exception as e:  # noqa: BLE001
            self._send(500, _json({'error': '%s' % e}))

    # ---------- 预览 ----------
    def _file(self):
        q = parse_qs(urlparse(self.path).query)
        p = os.path.abspath((q.get('path') or [''])[0])
        if not os.path.isfile(p) or not p.lower().endswith(('.pdf', '.png', '.jpg', '.jpeg')):
            self._send(404, _json({'error': '文件不存在'}))
            return
        with open(p, 'rb') as f:
            body = f.read()
        ctype = 'application/pdf' if p.lower().endswith('.pdf') else 'image/jpeg'
        self.send_response(200)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def _pages(self):
        """返回 PDF 每页尺寸(点), 供前端排版."""
        q = parse_qs(urlparse(self.path).query)
        p = os.path.abspath((q.get('path') or [''])[0])
        if not os.path.isfile(p) or not p.lower().endswith('.pdf'):
            self._send(404, _json({'error': '文件不存在'}))
            return
        doc = fitz.open(p)
        pages = [{'w': pg.rect.width, 'h': pg.rect.height} for pg in doc]
        doc.close()
        self._send(200, _json({'pages': pages}))

    def _pageimg(self):
        """按缩放比例把 PDF 某页渲染成 PNG (带缓存)."""
        q = parse_qs(urlparse(self.path).query)
        p = os.path.abspath((q.get('path') or [''])[0])
        n = int((q.get('page') or ['0'])[0])
        zoom = float((q.get('zoom') or ['1'])[0])
        zoom = max(0.2, min(6.0, zoom))
        if not os.path.isfile(p) or not p.lower().endswith('.pdf'):
            self._send(404, _json({'error': '文件不存在'}))
            return
        key = (p, os.path.getmtime(p), n, round(zoom, 2))
        body = _imgcache.get(key)
        if body is None:
            doc = fitz.open(p)
            if n < 0 or n >= len(doc):
                doc.close()
                self._send(404, _json({'error': '页码超界'}))
                return
            pix = doc[n].get_pixmap(matrix=fitz.Matrix(zoom, zoom))
            doc.close()
            body = pix.tobytes('png')
            if len(_imgcache) > 60:
                _imgcache.clear()
            _imgcache[key] = body
        self.send_response(200)
        self.send_header('Content-Type', 'image/png')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'private, max-age=3600')
        self.end_headers()
        self.wfile.write(body)

    def _save(self):
        """把拖进来的文件字节存到临时目录, 返回可预览/可解析的路径."""
        q = parse_qs(urlparse(self.path).query)
        name = (q.get('name') or ['未命名.pdf'])[0]
        raw = self._body()
        if not raw:
            self._send(200, _json({'error': '空文件'}))
            return
        safe = re.sub(r'[\\/:*?"<>|]', '_', os.path.basename(name))[:80]
        folder = os.path.join(APP_DIR, '_files')
        os.makedirs(folder, exist_ok=True)
        dst = os.path.join(folder, safe)
        i = 1
        while os.path.exists(dst):
            dst = os.path.join(folder, '%d_%s' % (i, safe))
            i += 1
        with open(dst, 'wb') as f:
            f.write(raw)
        self._send(200, _json({'path': dst}))

    # ---------- 接口 ----------
    def _scan(self):
        d = self._json_body()
        folder = (d.get('path') or '').strip().strip('"')
        folder = os.path.expandvars(os.path.expanduser(folder))
        folder = os.path.abspath(folder)
        if not os.path.isdir(folder):
            self._send(200, _json({'error': '文件夹不存在: %s' % folder}))
            return
        pattern = os.path.join(folder, '**', '*.pdf') if d.get('recursive') \
            else os.path.join(folder, '*.pdf')
        files = sorted(glob.glob(pattern, recursive=bool(d.get('recursive'))))
        others = sorted(
            f for f in os.listdir(folder)
            if os.path.isfile(os.path.join(folder, f))
            and not f.lower().endswith('.pdf'))
        self._send(200, _json({
            'folder': folder,
            'pdfs': [{'name': os.path.basename(f), 'path': f} for f in files],
            'others': others,
        }))

    def _parse(self):
        paths = self._json_body().get('paths') or []
        out = []
        for p in paths:
            try:
                out.append(extract_invoice(p))
            except Exception as e:  # noqa: BLE001
                out.append({'文件名': os.path.basename(p), '金额': None,
                            '税额': None, '价税合计': None,
                            '备注': '解析失败: %s' % e, '明细': []})
        self._send(200, _json(out))

    def _upload(self):
        q = parse_qs(urlparse(self.path).query)
        name = (q.get('name') or ['未命名.pdf'])[0]
        raw = self._body()
        tmp = os.path.join(APP_DIR, '_upload_tmp.pdf')
        with open(tmp, 'wb') as f:
            f.write(raw)
        try:
            r = extract_invoice(tmp)
            r['文件名'] = name
        except Exception as e:  # noqa: BLE001
            r = {'文件名': name, '金额': None, '税额': None, '价税合计': None,
                 '备注': '解析失败: %s' % e, '明细': []}
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
        self._send(200, _json(r))


def main():
    server = ThreadingHTTPServer(('127.0.0.1', PORT), Handler)
    url = 'http://127.0.0.1:%d/' % PORT
    print('发票助手已启动: %s' % url)
    threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
