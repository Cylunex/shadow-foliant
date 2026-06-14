/* shadow-foliant Service Worker —— 只缓存静态外壳,API 永不缓存(行情/净值/持仓必须实时)。
   策略:静态资源 network-first(在线永远拿最新,顺带写缓存;离线回退缓存) → 既不卡 stale JS,又能离线开 UI。*/
const CACHE = 'sf-shell-v1';

self.addEventListener('install', () => self.skipWaiting());

self.addEventListener('activate', (e) => {
  e.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)));
    await self.clients.claim();
  })());
});

self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET') return;                 // 写操作不碰
  const url = new URL(req.url);
  if (url.origin !== location.origin) return;        // 跨域(CDN:echarts/vue)交给浏览器
  if (url.pathname.includes('/api/')) return;        // ⚠️ 接口永不缓存 → 始终实时
  // 静态外壳:network-first
  e.respondWith((async () => {
    try {
      const resp = await fetch(req);
      if (resp && resp.status === 200) {
        const c = await caches.open(CACHE);
        c.put(req, resp.clone());
      }
      return resp;
    } catch (err) {
      const cached = await caches.match(req);
      if (cached) return cached;
      if (req.mode === 'navigate') {                 // 离线导航兜底:返回缓存首页外壳
        const idx = await caches.match('./') || await caches.match('index.html');
        if (idx) return idx;
      }
      throw err;
    }
  })());
});
