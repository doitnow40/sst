// ═══════════════════════════════════════════════════════════════
//  Stock Sector Tracker — Service Worker (PWA)
//  캐시 전략: 정적 자산은 Cache First, GAS API는 Network First
// ═══════════════════════════════════════════════════════════════

const CACHE_NAME   = 'sst-kr-v6';
const STATIC_CACHE = 'sst-static-v6';
const API_CACHE    = 'sst-api-v6';

// 설치 시 사전 캐시할 정적 자산
const PRECACHE_URLS = [
  '/',
  '/index.html',
  '/manifest.json',
  '/icons/icon-192.png',
  '/icons/icon-512.png',
  // 외부 CDN (네트워크 실패 시 오프라인 폴백용)
  'https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js',
  'https://cdn.jsdelivr.net/npm/chartjs-plugin-datalabels@2.2.0/dist/chartjs-plugin-datalabels.min.js',
];

// API 캐시 만료 시간 (밀리초)
const API_CACHE_TTL = 10 * 60 * 1000; // 10분

// ── Install: 정적 자산 사전 캐시 ───────────────────────────────
self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(STATIC_CACHE)
      .then(cache => cache.addAll(PRECACHE_URLS).catch(() => {}))
      .then(() => self.skipWaiting())
  );
});

// ── Activate: 구버전 캐시 정리 ─────────────────────────────────
self.addEventListener('activate', event => {
  const validCaches = [CACHE_NAME, STATIC_CACHE, API_CACHE];
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(
        keys
          .filter(k => !validCaches.includes(k))
          .map(k => caches.delete(k))
      ))
      .then(() => self.clients.claim())
  );
});

// ── Fetch: 요청별 캐시 전략 분기 ──────────────────────────────
self.addEventListener('fetch', event => {
  const { request } = event;
  const url = new URL(request.url);

  // POST 요청(GAS 쓰기 작업)은 캐시 안 함
  if (request.method !== 'GET') return;

  // GAS API → Network First (10분 캐시)
  if (url.hostname === 'script.google.com') {
    event.respondWith(networkFirstWithTTL(request, API_CACHE, API_CACHE_TTL));
    return;
  }

  // Google Fonts → Cache First (장기 캐시)
  if (url.hostname === 'fonts.googleapis.com' || url.hostname === 'fonts.gstatic.com') {
    event.respondWith(cacheFirst(request, STATIC_CACHE));
    return;
  }

  // CDN 스크립트 → Cache First
  if (url.hostname === 'cdnjs.cloudflare.com' || url.hostname === 'cdn.jsdelivr.net') {
    event.respondWith(cacheFirst(request, STATIC_CACHE));
    return;
  }

  // 자체 정적 자산 → Cache First with Network Fallback
  if (url.origin === self.location.origin) {
    event.respondWith(cacheFirst(request, STATIC_CACHE));
    return;
  }
});

// ── 전략 1: Cache First ────────────────────────────────────────
async function cacheFirst(request, cacheName) {
  const cache    = await caches.open(cacheName);
  const cached   = await cache.match(request);
  if (cached) return cached;
  try {
    const response = await fetch(request);
    if (response.ok) cache.put(request, response.clone());
    return response;
  } catch {
    return new Response('오프라인 상태입니다.', {
      status: 503,
      headers: { 'Content-Type': 'text/plain; charset=utf-8' }
    });
  }
}

// ── 전략 2: Network First with TTL ────────────────────────────
async function networkFirstWithTTL(request, cacheName, ttl) {
  const cache      = await caches.open(cacheName);
  const cacheKey   = request.url;

  // TTL 메타 확인
  const metaKey    = cacheKey + '__meta';
  const metaMatch  = await cache.match(metaKey);
  const now        = Date.now();

  if (metaMatch) {
    const meta = await metaMatch.json();
    if (now - meta.ts < ttl) {
      const cached = await cache.match(cacheKey);
      if (cached) return cached;
    }
  }

  // 네트워크 시도
  try {
    const response = await fetch(request);
    if (response.ok) {
      cache.put(cacheKey, response.clone());
      cache.put(metaKey, new Response(JSON.stringify({ ts: now }), {
        headers: { 'Content-Type': 'application/json' }
      }));
    }
    return response;
  } catch {
    // 네트워크 실패 → 만료된 캐시라도 반환
    const stale = await cache.match(cacheKey);
    if (stale) return stale;
    return new Response(JSON.stringify({ ok: false, error: '오프라인: 캐시된 데이터 없음' }), {
      status: 503,
      headers: { 'Content-Type': 'application/json' }
    });
  }
}

// ── 메시지 처리 (캐시 강제 삭제 등) ──────────────────────────
self.addEventListener('message', event => {
  if (event.data?.type === 'SKIP_WAITING') {
    self.skipWaiting();
  }
  if (event.data?.type === 'CLEAR_API_CACHE') {
    caches.delete(API_CACHE).then(() => {
      event.ports[0]?.postMessage({ ok: true });
    });
  }
});
