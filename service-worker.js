const CACHE_NAME = 'gezeiten-amrum-v1';
const DATEIEN_ZUM_CACHEN = [
  'index.html',
  'manifest.json'
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(DATEIEN_ZUM_CACHEN))
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k)))
    )
  );
});

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);

  // API-Aufrufe (Live-Gezeitendaten): immer versuchen, frisch zu laden.
  // Nur wenn offline/kein Netz, auf letzten Cache-Stand zurückfallen.
  if (url.pathname.startsWith('/api/')) {
    event.respondWith(
      fetch(event.request)
        .then((antwort) => {
          const kopie = antwort.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(event.request, kopie));
          return antwort;
        })
        .catch(() => caches.match(event.request))
    );
    return;
  }

  // Alles andere (HTML, Manifest, ...): erst Cache, dann Netz als Fallback
  event.respondWith(
    caches.match(event.request).then((treffer) => treffer || fetch(event.request))
  );
});
