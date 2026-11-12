/* PROSIC BI — service worker (PWA).
 * - Solo guarda la "cáscara" de la app (HTML, JS, CSS, íconos) para que abra rápido y se pueda instalar.
 * - NUNCA guarda respuestas de /api: los datos de Odoo son confidenciales y dependen del usuario.
 * - Sin conexión muestra la app y un aviso; los datos requieren red.
 */
const VERSION = "prosic-bi-v2"; // súbelo al cambiar íconos o archivos de /icons y /brand
const SHELL = ["/", "/index.html", "/manifest.webmanifest", "/brand/favicon.svg", "/icons/icon-192.png", "/icons/icon-512.png"];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(VERSION).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== VERSION).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  const url = new URL(req.url);
  if (req.method !== "GET" || url.origin !== self.location.origin) return; // Google, etc.: directo a la red
  if (url.pathname.startsWith("/api/")) return; // datos: siempre de la red, nunca en caché

  // Navegación (rutas de la SPA): red primero, caché si no hay conexión.
  if (req.mode === "navigate") {
    event.respondWith(
      fetch(req)
        .then((res) => {
          const copy = res.clone();
          caches.open(VERSION).then((c) => c.put("/index.html", copy));
          return res;
        })
        .catch(() => caches.match("/index.html")),
    );
    return;
  }

  // Archivos estáticos con hash (/assets/*) e íconos: caché primero.
  if (url.pathname.startsWith("/assets/") || url.pathname.startsWith("/icons/")) {
    event.respondWith(
      caches.match(req).then(
        (hit) =>
          hit ||
          fetch(req).then((res) => {
            if (res.ok) {
              const copy = res.clone();
              caches.open(VERSION).then((c) => c.put(req, copy));
            }
            return res;
          }),
      ),
    );
  }
});
