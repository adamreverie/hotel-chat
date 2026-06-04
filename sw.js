// Favvi Service Worker — handles push notifications

self.addEventListener('install', event => {
    self.skipWaiting();
});

self.addEventListener('activate', event => {
    event.waitUntil(clients.claim());
});

// Handle incoming push notifications
self.addEventListener('push', event => {
    const data = event.data ? event.data.json() : {};

    const title   = data.title   || 'New Request — Favvi';
    const options = {
        body:    data.body    || 'A new guest request has arrived.',
        icon:    data.icon    || '/static/icon-192.png',
        badge:   data.badge   || '/static/icon-192.png',
        tag:     data.tag     || 'favvi-request',
        data:    data.url     ? { url: data.url } : {},
        actions: [
            { action: 'view',    title: "I'm on it!" },
            { action: 'dismiss', title: 'Dismiss' }
        ],
        requireInteraction: true,  // stays on screen until tapped
        vibrate: [200, 100, 200]
    };

    event.waitUntil(
        self.registration.showNotification(title, options)
    );
});

// Handle notification tap — open the dashboard
self.addEventListener('notificationclick', event => {
    event.notification.close();

    if (event.action === 'dismiss') return;

    const url = event.notification.data && event.notification.data.url
        ? event.notification.data.url
        : '/';

    event.waitUntil(
        clients.matchAll({ type: 'window', includeUncontrolled: true }).then(clientList => {
            // If dashboard already open, focus it
            for (const client of clientList) {
                if (client.url.includes(url) && 'focus' in client) {
                    return client.focus();
                }
            }
            // Otherwise open it
            if (clients.openWindow) {
                return clients.openWindow(url);
            }
        })
    );
});