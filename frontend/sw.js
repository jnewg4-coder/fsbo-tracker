// FSBO Deal Tracker — Service Worker
// Strategy: network-first for HTML (deploy updates work instantly),
//           cache-first for CDN assets (Leaflet, fonts — rarely change).

const CACHE = 'fsbo-v1';

// CDN assets to pre-cache on install (pinned versions, safe to cache long-term)
const PRECACHE = [
  '/app',
  '/manifest.json',
];

// Patterns that should always use cache-first (CDN libs, fonts)
const CACHE_FIRST_PATTERNS = [
  /unpkg\.com\/leaflet/,
  /fonts\.googleapis\.com/,
  /fonts\.gstatic\.com/,
  /cdn\.tailwindcss\.com/,
];

// Patterns that should never be cached (API calls, dynamic data)
const NETWORK_ONLY_PATTERNS = [
  /\/api\//,
  /fsbo-api-production/,
  /localhost:8000/,
];

// Install: pre-cache the app shell
self.addEventListener('install', (e) => {
  e.waitUntil(
    caches.open(CACHE)
      .then((cache) => cache.addAll(PRECACHE))
      .then(() => self.skipWaiting())
  );
});

// Activate: clean old caches
self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

// Fetch: route by pattern
self.addEventListener('fetch', (e) => {
  const url = e.request.url;

  // Skip non-GET requests
  if (e.request.method !== 'GET') return;

  // Network-only: API calls
  if (NETWORK_ONLY_PATTERNS.some((p) => p.test(url))) return;

  // Cache-first: CDN libs and fonts
  if (CACHE_FIRST_PATTERNS.some((p) => p.test(url))) {
    e.respondWith(
      caches.match(e.request).then((cached) => {
        if (cached) return cached;
        return fetch(e.request).then((resp) => {
          if (resp.ok) {
            const clone = resp.clone();
            caches.open(CACHE).then((c) => c.put(e.request, clone));
          }
          return resp;
        });
      })
    );
    return;
  }

  // Network-first: HTML pages (ensures deploys are instant)
  if (e.request.headers.get('accept')?.includes('text/html')) {
    e.respondWith(
      fetch(e.request)
        .then((resp) => {
          if (resp.ok) {
            const clone = resp.clone();
            caches.open(CACHE).then((c) => c.put(e.request, clone));
          }
          return resp;
        })
        .catch(() => caches.match(e.request) || caches.match('/app'))
    );
    return;
  }

  // Default: stale-while-revalidate for everything else
  e.respondWith(
    caches.match(e.request).then((cached) => {
      const fetched = fetch(e.request).then((resp) => {
        if (resp.ok) {
          const clone = resp.clone();
          caches.open(CACHE).then((c) => c.put(e.request, clone));
        }
        return resp;
      }).catch(() => cached);
      return cached || fetched;
    })
  );
});
