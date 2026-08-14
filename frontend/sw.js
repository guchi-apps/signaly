'use strict'

const BADGE_CACHE = 'signaly-meta-v1'

async function setAppBadgeCount(count) {
  if (!('setAppBadge' in self.registration)) return
  const cache = await caches.open(BADGE_CACHE)
  const n = Number(count) || 0
  if (n > 0) {
    await cache.put('app-badge-count', new Response(String(n)))
    await self.registration.setAppBadge(n)
  } else {
    await cache.delete('app-badge-count')
    if ('clearAppBadge' in self.registration) {
      await self.registration.clearAppBadge()
    }
  }
}

async function incrementAppBadgeCount() {
  if (!('setAppBadge' in self.registration)) return
  const cache = await caches.open(BADGE_CACHE)
  const res = await cache.match('app-badge-count')
  const count = (res ? parseInt(await res.text(), 10) : 0) + 1
  await setAppBadgeCount(count)
}

self.addEventListener('message', (event) => {
  const data = event.data
  if (!data || data.type !== 'sync-app-badge') return
  event.waitUntil(setAppBadgeCount(data.count))
})

// キャッシュを全て削除して再起動する
self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.keys().then((keys) => Promise.all(keys.map((k) => caches.delete(k))))
  )
  self.skipWaiting()
})

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) => Promise.all(keys.map((k) => caches.delete(k))))
  )
  self.clients.claim()
})

// ── Web Push（アプリ終了中も通知）────────────────────────────────────────────

self.addEventListener('push', (event) => {
  let data = { title: 'Signaly', body: '', url: './' }
  if (event.data) {
    try {
      data = { ...data, ...event.data.json() }
    } catch {
      data.body = event.data.text()
    }
  }

  event.waitUntil(
    (async () => {
      const clients = await self.clients.matchAll({ type: 'window', includeUncontrolled: true })
      const focusedVisible = clients.filter(
        (c) => c.focused && c.visibilityState === 'visible'
      )

      // 操作中の前面タブだけページへ渡す（チャンネル設定を反映）。それ以外は SW が表示する。
      if (focusedVisible.length > 0) {
        for (const client of focusedVisible) {
          client.postMessage({ type: 'push-notification', data })
        }
        return
      }

      if (!clients.length) await incrementAppBadgeCount()

      await self.registration.showNotification(data.title || 'Signaly', {
        body: data.body || '',
        icon: 'icon-192.png?v=1.5.10',
        badge: 'icon-192.png?v=1.5.10',
        tag: data.id || undefined,
        data: {
          url: data.url || './',
          channel: data.channel || '',
          id: data.id || '',
        },
      })
    })()
  )
})

self.addEventListener('notificationclick', (event) => {
  event.notification.close()
  const data = event.notification.data || {}
  const targetUrl = data.url || './'

  event.waitUntil(
    (async () => {
      const clients = await self.clients.matchAll({
        type: 'window',
        includeUncontrolled: true,
      })
      for (const client of clients) {
        if (!('focus' in client)) continue
        await client.focus()
        client.postMessage({
          type: 'notification-click',
          url: targetUrl,
          channel: data.channel || '',
          id: data.id || '',
        })
        return
      }
      await self.clients.openWindow(targetUrl)
    })()
  )
})

// fetch ハンドラは置かない。
// 登録すると iOS PWA のコールドスタート時に SW 起動完了まで
// HTML / API 取得がブロックされ、数秒〜10秒の白画面になる。
// Push / バッジ同期だけが必要なので、ネットワークはブラウザに任せる。
