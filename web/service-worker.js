'use strict';

// The server fingerprints only the generic shell and public static assets.
// Live HTML contains an in-memory session token and must never enter a cache.
const CACHE_PREFIX = 'mcp-skills-shell-';
const CACHE_NAME = CACHE_PREFIX + '__SHELL_REVISION__';
const STATIC_PATHS = [
  '/offline.html', '/app.js', '/styles.css', '/theme.js', '/pwa.js',
  '/manifest.webmanifest', '/icons/icon.svg', '/icons/icon-192.png',
  '/icons/icon-512.png', '/icons/icon-maskable-512.png', '/icons/apple-touch-icon.png'
];
const STATIC_SET = new Set(STATIC_PATHS);
const NAVIGATION_PATHS = new Set(['/', '/index.html', '/offline.html']);

self.addEventListener('install', event => {
  event.waitUntil((async () => {
    const cache = await caches.open(CACHE_NAME);
    await cache.addAll(STATIC_PATHS.map(path => new Request(path, {cache: 'reload'})));
    // A new worker uses the same static-only policy; no page reload or mutation
    // is triggered when it takes control.
    await self.skipWaiting();
  })());
});

self.addEventListener('activate', event => {
  event.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(keys.filter(key => key.startsWith(CACHE_PREFIX) && key !== CACHE_NAME).map(key => caches.delete(key)));
    await self.clients.claim();
  })());
});

async function offlineFallback() {
  const cache = await caches.open(CACHE_NAME);
  return (await cache.match('/offline.html')) || new Response(
    'Open MCP & Skills while the local helper is running to prepare offline access.',
    {status: 503, headers: {'Content-Type': 'text/plain; charset=utf-8'}}
  );
}

self.addEventListener('fetch', event => {
  const request = event.request;
  const url = new URL(request.url);
  // API reads, session tokens, mutations, third-party traffic and query-bearing
  // requests always go directly to the network. There is no offline write queue.
  if (request.method !== 'GET' || url.origin !== self.location.origin ||
      url.pathname.startsWith('/api/') || url.search) return;

  if (request.mode === 'navigate' && NAVIGATION_PATHS.has(url.pathname)) {
    event.respondWith((async () => {
      try {
        const response = await fetch(request);
        return response.status >= 500 ? offlineFallback() : response;
      } catch {
        return offlineFallback();
      }
    })());
    return;
  }
  if (!STATIC_SET.has(url.pathname)) return;
  event.respondWith((async () => {
    const cache = await caches.open(CACHE_NAME);
    try {
      const response = await fetch(request);
      if (response.ok && response.type !== 'opaque') await cache.put(url.pathname, response.clone());
      return response;
    } catch {
      return (await cache.match(url.pathname)) || new Response('This app asset is unavailable offline.', {status: 503});
    }
  })());
});
