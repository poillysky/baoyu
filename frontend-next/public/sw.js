// Service worker for offline shell only — never cache API / media
const CACHE_NAME = 'baoyu-v4';

self.addEventListener('install', (event) => {
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) => {
      // 只清理本应用历史版本的缓存，不碰同源下其他 Cache Storage
      return Promise.all(
        keys
          .filter((key) => key.startsWith('baoyu-') && key !== CACHE_NAME)
          .map((key) => caches.delete(key))
      );
    })
  );
  // clients.claim 纳入同一 waitUntil，确保激活后立即接管页面
  event.waitUntil(self.clients.claim());
});

function shouldBypassCache(url, request) {
  try {
    const u = new URL(url);
    const p = u.pathname || '';
    // API / 封面 / 代理图：始终走网络，避免 iOS PWA 钉死旧 poster
    if (p.startsWith('/api/')) return true;
    if (p.includes('/cover-proxy')) return true;
    // 录制视频回放：绝不缓存，否则破坏 Range 拖动 + 撑爆 Cache Storage
    if (p.startsWith('/recordings/')) return true;
  } catch {
    /* ignore */
  }
  // 带 Range 的请求（视频拖动）不缓存
  if (request && request.headers && request.headers.has('range')) return true;
  return false;
}

/** 只有成功且同源的基础响应才允许写入缓存，404/500/opaque 一律不缓存 */
function isCacheable(response) {
  return response && response.ok && response.type === 'basic';
}

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;
  if (!req.url.startsWith(self.location.origin)) return;
  // 视频/音频资源一律绕过缓存（destination 判定兜底）
  if (req.destination === 'video' || req.destination === 'audio') return;
  if (shouldBypassCache(req.url, req)) return;

  // Navigation: network-first, fall back to cache
  if (req.mode === 'navigate') {
    event.respondWith(
      fetch(req)
        .then((response) => {
          if (isCacheable(response)) {
            const clone = response.clone();
            // cache.put 交给 waitUntil，防 worker 在写入完成前被杀
            event.waitUntil(
              caches.open(CACHE_NAME).then((cache) => cache.put(req, clone))
            );
          }
          return response;
        })
        .catch(() => caches.match(req).then((c) => c || caches.match('/')))
    );
    return;
  }

  // Static assets: stale-while-revalidate
  event.respondWith(
    caches.match(req).then((cached) => {
      const fetchPromise = fetch(req).then((response) => {
        if (isCacheable(response)) {
          const clone = response.clone();
          event.waitUntil(
            caches.open(CACHE_NAME).then((cache) => cache.put(req, clone))
          );
        }
        return response;
      });
      return cached || fetchPromise;
    })
  );
});
