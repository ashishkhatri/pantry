// Pantry service worker: makes the app installable, shows push notifications,
// and keeps a copy of the page shell so the app opens even when the server is slow.
// Data always comes from the server (never cached).
const SHELL = 'pantry-shell-v1';
const SHELL_FILES = ['/', '/manifest.webmanifest', '/static/icons/icon-192.png'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(SHELL).then(c => c.addAll(SHELL_FILES)).catch(() => {}).then(() => self.skipWaiting()));
});
self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(k => k !== SHELL).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});
self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  if (url.origin !== self.location.origin || e.request.method !== 'GET') return;
  if (url.pathname === '/' || url.pathname === '/index.html') {
    // Network first, cached page if the server can't be reached.
    e.respondWith(fetch(e.request).then(r => {
      const copy = r.clone(); caches.open(SHELL).then(c => c.put('/', copy)); return r;
    }).catch(() => caches.match('/')));
  }
});

self.addEventListener('push', e => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch (_) { d = { body: e.data && e.data.text() }; }
  e.waitUntil(self.registration.showNotification(d.title || 'Pantry', {
    body: d.body || '',
    icon: '/static/icons/icon-192.png',
    badge: '/static/icons/badge-96.png',
    tag: d.tag || 'pantry',
    renotify: true,
    data: { url: d.url || '/' },
  }));
});

self.addEventListener('notificationclick', e => {
  e.notification.close();
  const target = new URL(e.notification.data && e.notification.data.url || '/', self.location.origin).href;
  e.waitUntil(self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(list => {
    for (const c of list) {
      if (c.url.startsWith(self.location.origin)) { c.navigate(target).catch(() => {}); return c.focus(); }
    }
    return self.clients.openWindow(target);
  }));
});
