/* 发票助手 手机版 Service Worker
 * 1) 缓存应用外壳, 断网也能打开(首次需联网加载 pdf.js CDN)
 * 2) 接收系统分享面板(Web Share Target)发来的 PDF:
 *    POST ./index.html?share-target=1 → 把文件暂存进 Cache → 跳回页面取走 */
const CACHE = 'fapiao-shell-v1';
const SHELL = [
  './',
  './index.html',
  './manifest.webmanifest',
  './icon.svg',
  '../发票解析.web.js',
  'https://cdn.jsdelivr.net/npm/pdfjs-dist@4.10.38/build/pdf.min.mjs',
  'https://cdn.jsdelivr.net/npm/pdfjs-dist@4.10.38/build/pdf.worker.min.mjs',
];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE && k !== 'fapiao-share')
        .map(k => caches.delete(k))))
      .then(() => self.clients.claim()));
});

self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);

  // 分享面板: 接文件 → 存缓存 → 303 回页面
  if (e.request.method === 'POST' && url.search.includes('share-target=1')) {
    e.respondWith((async () => {
      const form = await e.request.formData();
      const files = form.getAll('files').filter(f => f && f.size);
      if (files.length) {
        const cache = await caches.open('fapiao-share');
        await cache.put('__shared__', new Response(files[0]));
      }
      return Response.redirect('./index.html#shared', 303);
    })());
    return;
  }

  // 分享来的文件: 页面用 fetch('__shared__') 取走后即删
  if (url.pathname.endsWith('__shared__')) {
    e.respondWith((async () => {
      const cache = await caches.open('fapiao-share');
      const hit = await cache.match('__shared__');
      if (hit) { await cache.delete('__shared__'); return hit; }
      return new Response('', { status: 404 });
    })());
    return;
  }

  // 应用外壳: 缓存优先
  if (e.request.method === 'GET') {
    e.respondWith((async () => {
      const hit = await caches.match(e.request);
      if (hit) return hit;
      try {
        const res = await fetch(e.request);
        if (res.ok || res.type === 'opaque') {
          const cache = await caches.open(CACHE);
          cache.put(e.request, res.clone());
        }
        return res;
      } catch (err) {
        if (e.request.mode === 'navigate') {
          const shell = await caches.match('./index.html');
          if (shell) return shell;
        }
        throw err;
      }
    })());
  }
});
