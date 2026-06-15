// Chess Rocket — minimal PWA service worker.
//
// Goal: make the app installable ("Add to Home Screen" on iOS / Chrome) and
// fall back to a cached shell when the user is briefly offline. We keep it
// deliberately simple — the dashboard is so dynamic (every move hits the
// backend) that aggressive caching would do more harm than good. So we:
//   1. cache the static shell on install (HTML + voice_control.js + chess
//      pieces from the CDN),
//   2. serve cached shell first when the network is unavailable,
//   3. let everything else go straight to the network.

const CACHE_NAME = 'chess-rocket-v1';
const SHELL_URLS = [
  '/',
  '/tactics',
  '/setup',
  '/voice_control.js',
  '/manifest.webmanifest',
  // chessboard.js + jQuery from the CDNs we already use — these are by
  // far the chunkiest assets, and they barely change.
  'https://unpkg.com/@chrisoakman/chessboardjs@1.0.0/dist/chessboard-1.0.0.min.css',
  'https://unpkg.com/@chrisoakman/chessboardjs@1.0.0/dist/chessboard-1.0.0.min.js',
  'https://code.jquery.com/jquery-3.7.1.min.js',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) =>
      // cache.addAll() rejects atomically if any URL fails — for cross-origin
      // CDN URLs we tolerate failure individually so a flaky CDN doesn't
      // kill the whole install.
      Promise.all(SHELL_URLS.map((u) =>
        cache.add(new Request(u, { credentials: 'omit' })).catch(() => null)
      ))
    )
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  // Drop any old caches from previous deploys.
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  // Only handle GETs; everything else (the API POSTs) goes straight through.
  if (req.method !== 'GET') return;
  // Don't intercept /api/* — those need to be live, no cache.
  const url = new URL(req.url);
  if (url.pathname.startsWith('/api/')) return;

  event.respondWith(
    fetch(req).catch(() =>
      caches.match(req, { ignoreSearch: true }).then((cached) =>
        cached || (req.mode === 'navigate' ? caches.match('/') : Response.error())
      )
    )
  );
});
