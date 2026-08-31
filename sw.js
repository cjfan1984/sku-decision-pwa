const CACHE = 'sku-decision-pwa-encrypted-v4';
const DATA_CACHE = 'sku-decision-pwa-data-v1';
const SHELL = ['./', './index.html', './manifest.webmanifest', './icon.svg', './icon-192.png', './icon-512.png'];
const DATA_FILES = new Set(['app.enc.json', 'pwa-data-version.json']);
let pairRefresh;

const scoped = name => new URL(name, self.registration.scope).href;
const versioned = (name, sha) => scoped(`${name}?offline=${sha}`);

async function cachePair(raw, manifest) {
  const digest = [...new Uint8Array(await crypto.subtle.digest('SHA-256', raw))]
    .map(value => value.toString(16).padStart(2, '0')).join('');
  if (digest !== manifest.envelopeSha256) throw new Error('data-integrity');
  const cache = await caches.open(DATA_CACHE);
  await Promise.all([
    cache.put(versioned('app.enc.json', digest), new Response(raw.slice(0), { headers: { 'Content-Type': 'application/json' } })),
    cache.put(versioned('pwa-data-version.json', digest), new Response(JSON.stringify(manifest), { headers: { 'Content-Type': 'application/json' } }))
  ]);
  await cache.put(scoped('pwa-data-current.json'), new Response(JSON.stringify({ sha: digest }), { headers: { 'Content-Type': 'application/json' } }));
  const keys = await cache.keys();
  await Promise.all(keys.filter(request => {
    const url = new URL(request.url);
    return url.searchParams.has('offline') && url.searchParams.get('offline') !== digest;
  }).map(request => cache.delete(request)));
  return {
    'app.enc.json': new Response(raw.slice(0), { headers: { 'Content-Type': 'application/json' } }),
    'pwa-data-version.json': new Response(JSON.stringify(manifest), { headers: { 'Content-Type': 'application/json' } })
  };
}

async function refreshPair() {
  if (!pairRefresh) {
    pairRefresh = (async () => {
      const [appResponse, manifestResponse] = await Promise.all([
        fetch(scoped('app.enc.json'), { cache: 'no-store' }),
        fetch(scoped('pwa-data-version.json'), { cache: 'no-store' })
      ]);
      if (!appResponse.ok || !manifestResponse.ok) throw new Error('data-network');
      const [raw, manifest] = await Promise.all([appResponse.arrayBuffer(), manifestResponse.json()]);
      return cachePair(raw, manifest);
    })().finally(() => { pairRefresh = null; });
  }
  return pairRefresh;
}

async function offlineData(name) {
  const cache = await caches.open(DATA_CACHE);
  const pointer = await cache.match(scoped('pwa-data-current.json'));
  if (!pointer) return undefined;
  const { sha } = await pointer.json();
  return cache.match(versioned(name, sha));
}

self.addEventListener('install', event => {
  event.waitUntil(caches.open(CACHE).then(cache => cache.addAll(SHELL.map(url => new Request(url, { cache: 'reload' })))))
  self.skipWaiting();
});

self.addEventListener('activate', event => {
  event.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(keys
      .filter(key => key.startsWith('sku-decision-pwa-') && key !== CACHE && key !== DATA_CACHE)
      .map(key => caches.delete(key)));
    await self.clients.claim();
  })());
});

self.addEventListener('fetch', event => {
  if (event.request.method !== 'GET') return;
  const requestUrl = new URL(event.request.url);
  const name = [...DATA_FILES].find(file => requestUrl.origin === self.location.origin && requestUrl.pathname === new URL(file, self.registration.scope).pathname);
  if (name) {
    event.respondWith(refreshPair().then(pair => pair[name].clone()).catch(() => offlineData(name).then(response => response || Response.error())));
    return;
  }
  if (event.request.mode === 'navigate') {
    event.respondWith(fetch(event.request).then(async response => {
      if (response.ok) {
        const cache = await caches.open(CACHE);
        await cache.put('./index.html', response.clone());
        return response;
      }
      return (await caches.match('./index.html')) || response;
    }).catch(() => caches.match('./index.html')));
    return;
  }
  event.respondWith((async () => {
    try {
      const response = await fetch(event.request);
      if (!response.ok) return (await caches.match(event.request)) || response;
      const cache = await caches.open(CACHE);
      await cache.put(event.request, response.clone());
      return response;
    } catch (_) {
      return (await caches.match(event.request)) || Response.error();
    }
  })());
});
