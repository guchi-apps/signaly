'use strict'

// ── State ────────────────────────────────────────────────────────────────────

function apiUrl(path) {
  return '/' + path.replace(/^\//, '')
}

// API 呼び出しはすべてここを通す。Supabase のアクセストークンを Authorization に載せ、
// 401（＝更新もできないほど古い／取り消された）ならログイン画面へ戻す。
// 403 は「許可されていないアカウント」なので、ログイン画面に理由を出す。
async function authFetch(url, options = {}) {
  const res = await SignalyAuth.fetch(url, options)
  if (res.status === 401) {
    // 初回訪問（まだログインしていない）と、失効したのとで文言を分ける。
    // 未ログインの人に「有効期限が切れました」と出すと何が起きたのか伝わらない。
    const hadToken = await SignalyAuth.getAccessToken()
    showLoginOverlayOnce(hadToken ? EXPIRED_MESSAGE : '')
  } else if (res.status === 403) {
    // 許可リストに無いアカウント。Supabase 側にはログインできてしまうため、
    // ここでサインアウトしないと再ログインしても同じアカウントで素通りして 403 に戻る。
    showLoginOverlayOnce(FORBIDDEN_MESSAGE)
    void SignalyAuth.signOut()
  }
  return res
}

// 確認ダイアログの「エラー非表示→ボタン無効化→実行→成功/失敗処理→再有効化」という
// 定型処理を共通化する。run には showError を渡すので、API失敗時は
// showError(message) を呼んで return すればよい（例外を投げると通信エラー扱いになる）。
async function runConfirmAction({ confirmBtn, cancelBtn, errorEl, extraButtons = [], run }) {
  errorEl.hidden = true
  errorEl.textContent = ''
  const buttons = [confirmBtn, cancelBtn, ...extraButtons]
  for (const btn of buttons) btn.disabled = true

  const showError = (message) => {
    errorEl.textContent = message
    errorEl.hidden = false
  }

  try {
    await run(showError)
  } catch {
    showError('ネットワークエラーが発生しました')
  } finally {
    for (const btn of buttons) btn.disabled = false
  }
}

function channelFromQuery() {
  return new URLSearchParams(location.search).get('channel')
}

function isPushDeepLink() {
  return new URLSearchParams(location.search).get('src') === 'push'
}

function notificationIdFromQuery() {
  return new URLSearchParams(location.search).get('id')
}

function resolveStartupChannel() {
  const params = new URLSearchParams(location.search)
  const urlChannel = params.get('channel')
  const saved = loadLastChannel()

  if (isPushDeepLink() && urlChannel) return urlChannel
  return saved ?? urlChannel
}

function clearPushDeepLinkMarker() {
  if (!isPushDeepLink() && !notificationIdFromQuery()) return
  const url = new URL(location.href)
  url.searchParams.delete('src')
  url.searchParams.delete('id')
  history.replaceState(null, '', url)
}

function updatePageUrl(channelName) {
  const url = new URL(location.href)
  if (channelName) {
    url.searchParams.set('channel', channelName)
  } else {
    url.searchParams.delete('channel')
  }
  history.replaceState(null, '', url)
}

const HIGHLIGHT_FADE_MS = 8000

const LAST_READ_KEY = 'signaly-last-read'
const LAST_CHANNEL_KEY = 'signaly-last-channel'
const UNREAD_KEY = 'signaly-unread'
const PUSH_DISABLED_KEY = 'signaly-push-disabled'
const CHANNEL_TREE_KEY = 'signaly-channel-tree'
const COLLAPSED_GROUPS_KEY = 'signaly-collapsed-groups'
const SOURCE_FILTER_KEY = 'signaly-source-filter'
// 送信元が未設定の通知を指す擬似的な名前（backend/main.py の SOURCE_NONE と揃える）
const SOURCE_NONE = '-'
const UNGROUPED_SECTION_ID = '__ungrouped__'
const UNREAD_POLL_MS = 15000
const NEW_CARD_FADE_MS = 60000
// 長時間セッションやリアルタイム受信・「もっと読み込む」の繰り返しでカードが
// 際限なく蓄積し続けないよう、表示件数に上限を設けて古いカードから間引く（#99）。
// 履歴APIの1ページ200件（最大500件）より十分大きく取り、数回の読み込みでは
// 間引かれないようにしている。
const MAX_FEED_CARDS = 600

let activeChannel = null
let channelsByName = {}
let eventSource = null
let unread = loadUnread()  // channel_name -> count
let lastReadAt = loadLastReadAt()  // channel_name -> timestamp (ms)
const seenIds = new Set()
let pollTimer = null
let unreadPollTimer = null
let pushSubscribed = false
let pendingNewCount = 0
let pendingHighlightId = null
let notificationSettings = { channels: {}, groups: {} }
let notificationPrefsReady = false
let feedOldestTimestamp = null
let feedOldestId = null
let feedHasMore = false
let feedLoadingMore = false
let feedLoadMoreEl = null
let sourceFilters = loadSourceFilters()  // channel_name -> 選択中の送信元（null = すべて）
let activeSource = null
let channelSources = []

// ── DOM ──────────────────────────────────────────────────────────────────────

const channelList = document.getElementById('channel-list')
const feed = document.getElementById('feed')
const feedStickyDate = document.getElementById('feed-sticky-date')
const feedState = document.getElementById('feed-state')
const feedStateText = document.getElementById('feed-state-text')
const feedStateSpinner = document.getElementById('feed-state-spinner')
const feedStateRetry = document.getElementById('feed-state-retry')
const emptyState = document.getElementById('empty-state')
const newNotifBanner = document.getElementById('new-notif-banner')
const toastEl = document.getElementById('toast')
const toastMessageEl = document.getElementById('toast-message')
const toastCloseBtn = document.getElementById('toast-close')
const channelTitle = document.getElementById('channel-title')
const channelSettingsHeaderBtn = document.getElementById('channel-settings-header-btn')
const sourceFilterEl = document.getElementById('source-filter')
const notificationsBtn = document.getElementById('notifications-btn')
const notificationsBadge = document.getElementById('notifications-badge')
const statusEl = document.getElementById('status')
const sidebar = document.getElementById('sidebar')
const sidebarToggle = document.getElementById('sidebar-toggle')
const sidebarBackdrop = document.getElementById('sidebar-backdrop')

const mobileSidebarMq = window.matchMedia('(max-width: 767px)')

function isMobileSidebar() {
  return mobileSidebarMq.matches
}

function setSidebarOpen(open) {
  if (!isMobileSidebar()) {
    sidebar.classList.remove('sidebar--open')
    sidebarBackdrop.classList.remove('visible')
    sidebarBackdrop.hidden = true
    sidebarBackdrop.setAttribute('aria-hidden', 'true')
    sidebarToggle?.setAttribute('aria-expanded', 'false')
    sidebarToggle?.setAttribute('aria-label', 'メニューを開く')
    return
  }

  sidebar.classList.toggle('sidebar--open', open)
  sidebarBackdrop.classList.toggle('visible', open)
  sidebarBackdrop.hidden = !open
  sidebarBackdrop.setAttribute('aria-hidden', open ? 'false' : 'true')
  sidebarToggle?.setAttribute('aria-expanded', open ? 'true' : 'false')
  sidebarToggle?.setAttribute('aria-label', open ? 'メニューを閉じる' : 'メニューを開く')
}

function closeSidebar() {
  setSidebarOpen(false)
}

sidebarToggle?.addEventListener('click', () => {
  setSidebarOpen(!sidebar.classList.contains('sidebar--open'))
})

sidebarBackdrop?.addEventListener('click', closeSidebar)

document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') closeSidebar()
})

mobileSidebarMq.addEventListener('change', () => {
  if (!isMobileSidebar()) closeSidebar()
})

// ── 画面左半分の右スワイプでサイドバーを開く ──────────────────────────────────

const EDGE_SWIPE_THRESHOLD = 40

let edgeSwipeStart = null

document.addEventListener('touchstart', (e) => {
  if (!isMobileSidebar() || sidebar.classList.contains('sidebar--open')) return
  const touch = e.touches[0]
  if (touch.clientX > window.innerWidth / 2) return
  edgeSwipeStart = { x: touch.clientX, y: touch.clientY }
}, { passive: true })

document.addEventListener('touchmove', (e) => {
  if (!edgeSwipeStart) return
  const touch = e.touches[0]
  const dx = touch.clientX - edgeSwipeStart.x
  const dy = touch.clientY - edgeSwipeStart.y
  if (Math.abs(dy) > Math.abs(dx)) {
    edgeSwipeStart = null
    return
  }
  if (dx > EDGE_SWIPE_THRESHOLD) {
    setSidebarOpen(true)
    edgeSwipeStart = null
  }
}, { passive: true })

document.addEventListener('touchend', () => {
  edgeSwipeStart = null
})

// ── Helpers ──────────────────────────────────────────────────────────────────

function parseTimestamp(isoString) {
  const s = String(isoString ?? '')
  if (!s) return NaN
  // MySQL 経由で tz なし UTC が返る場合は UTC として解釈する
  if (/^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}/.test(s) && !(/[Zz]$|[+-]\d{2}:\d{2}$/.test(s))) {
    return Date.parse(s.replace(' ', 'T') + 'Z')
  }
  return Date.parse(s)
}

function getDateKey(isoString) {
  const ts = parseTimestamp(isoString)
  if (Number.isNaN(ts)) return ''
  const d = new Date(ts)
  const pad = (n) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
}

function formatDateFromKey(dateKey) {
  if (!dateKey) return ''
  const [y, m, d] = dateKey.split('-')
  return `${y}/${m}/${d}`
}

function formatNotificationTime(isoString) {
  const ts = parseTimestamp(isoString)
  if (Number.isNaN(ts)) return ''
  const d = new Date(ts)
  const pad = (n) => String(n).padStart(2, '0')
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}

function createDateDivider(dateKey) {
  const el = document.createElement('div')
  el.className = 'feed-date-divider'
  el.dataset.date = dateKey
  el.setAttribute('role', 'separator')
  el.textContent = formatDateFromKey(dateKey)
  return el
}

function updateStickyFeedDate() {
  if (!feedStickyDate || !feed) return
  const cards = [...feed.querySelectorAll('.notif-card')]
  if (!cards.length) {
    feedStickyDate.hidden = true
    return
  }
  const feedRect = feed.getBoundingClientRect()
  let dateKey = cards[0].dataset.date
  for (const card of cards) {
    if (card.getBoundingClientRect().bottom > feedRect.top + 4) {
      dateKey = card.dataset.date
      break
    }
  }
  feedStickyDate.textContent = formatDateFromKey(dateKey)
  feedStickyDate.hidden = false
}

function setStatus(state) {
  statusEl.className = `status status--${state}`
  const titles = { connected: '接続中', connecting: '接続中…', disconnected: '切断' }
  statusEl.title = titles[state] ?? state
}

let feedStateRetryFn = null

function showChannelListLoading() {
  channelList.innerHTML = '<div class="loading-text"><span class="loading-spinner" aria-hidden="true"></span>読み込み中…</div>'
}

function showChannelListError(detail, retryFn) {
  clearChannelListRefreshHint()
  channelList.innerHTML = ''
  const wrap = document.createElement('div')
  wrap.className = 'loading-text loading-text--error'

  const message = document.createElement('p')
  message.className = 'loading-text-message'
  message.textContent = '読み込みに失敗しました'

  const detailEl = document.createElement('p')
  detailEl.className = 'loading-text-detail'
  detailEl.textContent = detail

  wrap.append(message, detailEl)

  if (retryFn) {
    const btn = document.createElement('button')
    btn.type = 'button'
    btn.className = 'loading-retry-btn'
    btn.textContent = '再試行'
    btn.addEventListener('click', retryFn)
    wrap.appendChild(btn)
  }

  channelList.appendChild(wrap)
}

function clearChannelListRefreshHint() {
  document.getElementById('channel-refresh-hint')?.remove()
}

function showChannelListRefreshHint(detail, retryFn) {
  clearChannelListRefreshHint()
  const hint = document.createElement('div')
  hint.id = 'channel-refresh-hint'
  hint.className = 'channel-refresh-hint'
  hint.textContent = `更新できませんでした（${detail}）`
  if (retryFn) {
    const btn = document.createElement('button')
    btn.type = 'button'
    btn.className = 'channel-refresh-hint-btn'
    btn.textContent = '再試行'
    btn.addEventListener('click', retryFn)
    hint.appendChild(btn)
  }
  channelList.parentElement?.insertBefore(hint, channelList)
}

function showFeedLoading() {
  if (emptyState) emptyState.hidden = true
  if (!feedState) return
  feedState.hidden = false
  feedState.className = 'feed-state feed-state--loading'
  if (feedStateText) feedStateText.textContent = '読み込み中…'
  if (feedStateSpinner) feedStateSpinner.hidden = false
  if (feedStateRetry) feedStateRetry.hidden = true
  feedStateRetryFn = null
}

function showFeedError(message, retryFn) {
  if (emptyState) emptyState.hidden = true
  if (!feedState) return
  feedState.hidden = false
  feedState.className = 'feed-state feed-state--error'
  if (feedStateText) feedStateText.textContent = message
  if (feedStateSpinner) feedStateSpinner.hidden = true
  if (feedStateRetry) {
    feedStateRetry.hidden = !retryFn
    feedStateRetryFn = retryFn || null
  }
}

function hideFeedState() {
  if (feedState) feedState.hidden = true
  feedStateRetryFn = null
}

feedStateRetry?.addEventListener('click', () => {
  const fn = feedStateRetryFn
  if (fn) fn()
})

function loadLastReadAt() {
  try {
    const raw = localStorage.getItem(LAST_READ_KEY)
    if (!raw) return {}
    const data = JSON.parse(raw)
    const out = {}
    for (const [k, v] of Object.entries(data)) {
      const n = Number(v)
      if (!Number.isNaN(n)) out[k] = n
    }
    return out
  } catch {
    return {}
  }
}

function saveLastReadAt() {
  try {
    localStorage.setItem(LAST_READ_KEY, JSON.stringify(lastReadAt))
  } catch {
    // quota exceeded 等は無視
  }
}

function loadUnread() {
  try {
    const raw = localStorage.getItem(UNREAD_KEY)
    if (!raw) return {}
    const data = JSON.parse(raw)
    const out = {}
    for (const [k, v] of Object.entries(data)) {
      const n = Number(v)
      if (n > 0) out[k] = n
    }
    return out
  } catch {
    return {}
  }
}

function saveUnread() {
  try {
    const data = {}
    for (const [k, v] of Object.entries(unread)) {
      const n = Number(v)
      if (n > 0) data[k] = n
    }
    if (Object.keys(data).length) {
      localStorage.setItem(UNREAD_KEY, JSON.stringify(data))
    } else {
      localStorage.removeItem(UNREAD_KEY)
    }
  } catch {
    // quota exceeded 等は無視
  }
}

function setChannelUnread(channelName, count) {
  const n = Math.max(0, Number(count) || 0)
  if (n > 0) {
    unread[channelName] = n
  } else {
    delete unread[channelName]
  }
  saveUnread()
}

// 他端末でのチャンネル削除・統合・改名は、この端末が起動中でなければ知りようがない
// （SSEはチャンネル選択中のみ張られ、チャンネル一覧の変更を配らない）。そのため
// renderChannelTree() のたびに、サーバーの現在の一覧に無いチャンネル名の未読件数を
// localStorage から取り除く。放置すると totalUnread() が拾い続け、ベルとホーム画面の
// バッジが実在しないチャンネルの未読分だけ恒久的に食い違ったまま残る（#236）。
function pruneStaleReadState(validNames) {
  const valid = new Set(validNames)
  let unreadChanged = false
  for (const name of Object.keys(unread)) {
    if (!valid.has(name)) {
      delete unread[name]
      unreadChanged = true
    }
  }
  if (unreadChanged) saveUnread()

  let lastReadChanged = false
  for (const name of Object.keys(lastReadAt)) {
    if (!valid.has(name)) {
      delete lastReadAt[name]
      lastReadChanged = true
    }
  }
  if (lastReadChanged) saveLastReadAt()
}

function loadLastChannel() {
  try {
    return localStorage.getItem(LAST_CHANNEL_KEY) || null
  } catch {
    return null
  }
}

function saveLastChannel(name) {
  try {
    if (name) {
      localStorage.setItem(LAST_CHANNEL_KEY, name)
    } else {
      localStorage.removeItem(LAST_CHANNEL_KEY)
    }
  } catch {
    // quota exceeded 等は無視
  }
}

function saveChannelTreeCache(data) {
  try {
    localStorage.setItem(CHANNEL_TREE_KEY, JSON.stringify(data))
  } catch {
    // quota exceeded 等は無視
  }
}

function loadChannelTreeCache() {
  try {
    const raw = localStorage.getItem(CHANNEL_TREE_KEY)
    return raw ? JSON.parse(raw) : null
  } catch {
    return null
  }
}

function clearChannelTreeCache() {
  try {
    localStorage.removeItem(CHANNEL_TREE_KEY)
  } catch {
    // ignore
  }
}

function loadCollapsedGroups() {
  try {
    const raw = localStorage.getItem(COLLAPSED_GROUPS_KEY)
    return raw ? new Set(JSON.parse(raw)) : new Set()
  } catch {
    return new Set()
  }
}

function saveCollapsedGroups() {
  try {
    localStorage.setItem(COLLAPSED_GROUPS_KEY, JSON.stringify([...collapsedGroups]))
  } catch {
    // quota exceeded 等は無視
  }
}

// ── 送信元フィルタ ────────────────────────────────────────────────────────────
// チャンネルを用途別に統合すると1本の中に複数アプリの通知が混ざる。
// どの送信元を見ているかは端末ごとの好みなので、サーバーではなく localStorage に持つ。

function loadSourceFilters() {
  try {
    const raw = localStorage.getItem(SOURCE_FILTER_KEY)
    return raw ? JSON.parse(raw) : {}
  } catch {
    return {}
  }
}

function saveSourceFilters() {
  try {
    localStorage.setItem(SOURCE_FILTER_KEY, JSON.stringify(sourceFilters))
  } catch {
    // quota exceeded 等は無視
  }
}

function setSourceFilter(channelName, source) {
  if (source) sourceFilters[channelName] = source
  else delete sourceFilters[channelName]
  saveSourceFilters()
}

function sourceLabel(name) {
  return name === SOURCE_NONE ? '送信元なし' : name
}

function renderSourceFilter() {
  if (!sourceFilterEl) return
  sourceFilterEl.innerHTML = ''

  // 送信元が1種類しか無いチャンネルでは絞り込む相手がいないので出さない
  if (channelSources.length < 2) {
    sourceFilterEl.hidden = true
    return
  }
  sourceFilterEl.hidden = false

  const total = channelSources.reduce((sum, item) => sum + item.count, 0)
  const chips = [{ name: null, label: 'すべて', count: total }].concat(
    channelSources.map((item) => ({
      name: item.name,
      label: sourceLabel(item.name),
      count: item.count,
    })),
  )

  for (const chip of chips) {
    const btn = document.createElement('button')
    btn.type = 'button'
    btn.className = 'source-chip'
    btn.setAttribute('aria-pressed', String(chip.name === activeSource))
    btn.textContent = chip.label

    const count = document.createElement('span')
    count.className = 'source-chip-count'
    count.textContent = String(chip.count)
    btn.appendChild(count)

    btn.addEventListener('click', () => applySourceFilter(chip.name))
    sourceFilterEl.appendChild(btn)
  }
}

async function refreshChannelSources(channelName) {
  const channel = channelsByName[channelName]
  if (!channel) {
    channelSources = []
    renderSourceFilter()
    return
  }
  try {
    const res = await fetch(apiUrl(`api/channels/${channel.id}/sources`))
    if (!res.ok || activeChannel !== channelName) return
    const data = await res.json()
    channelSources = Array.isArray(data.sources) ? data.sources : []
  } catch {
    channelSources = []
  }
  if (activeChannel !== channelName) return
  // 保存済みの絞り込み先が消えている場合（統合・削除の後）は「すべて」へ戻す
  if (activeSource && !channelSources.some((item) => item.name === activeSource)) {
    activeSource = null
    setSourceFilter(channelName, null)
  }
  renderSourceFilter()
}

async function reloadActiveFeed() {
  if (!activeChannel) return
  const channelName = activeChannel
  exitNotifSelectMode()
  feed.innerHTML = ''
  if (feedStickyDate) feedStickyDate.hidden = true
  if (emptyState) emptyState.hidden = true
  seenIds.clear()
  await loadHistory(channelName, lastReadAt[channelName], 0)
}

async function applySourceFilter(source) {
  if (!activeChannel || activeSource === source) return
  activeSource = source
  setSourceFilter(activeChannel, source)
  renderSourceFilter()
  await reloadActiveFeed()
}

function matchesSourceFilter(entry) {
  if (!activeSource) return true
  return (entry.source || SOURCE_NONE) === activeSource
}

// 新着1件ごとにサーバーへ数え直しに行くとリクエストが増えるので、
// 既知の送信元は手元で加算し、初めて見る送信元のときだけ取り直す。
function noteIncomingSource(entry) {
  const name = entry.source || SOURCE_NONE
  const known = channelSources.find((item) => item.name === name)
  if (known) {
    known.count += 1
    renderSourceFilter()
    return
  }
  void refreshChannelSources(entry.channel)
}

function toggleGroupCollapsed(groupId) {
  if (collapsedGroups.has(groupId)) {
    collapsedGroups.delete(groupId)
  } else {
    collapsedGroups.add(groupId)
  }
  saveCollapsedGroups()
}

function showAuthenticatedShell() {
  SignalySettings.showAuthenticated()
  if (addGroupBtn) addGroupBtn.hidden = false
  if (reorderModeBtn) reorderModeBtn.hidden = false
  if (markAllReadBtn) markAllReadBtn.hidden = false
}

let toastTimer = null

function hideToast() {
  if (!toastEl) return
  toastEl.hidden = true
  clearTimeout(toastTimer)
}

function showToast(message) {
  if (!toastEl || !toastMessageEl) return
  toastMessageEl.textContent = message
  toastEl.hidden = false
  clearTimeout(toastTimer)
  toastTimer = setTimeout(hideToast, 2000)
}

toastCloseBtn?.addEventListener('click', hideToast)

function markChannelRead(channelName, timestampMs = Date.now()) {
  const hadUnread = (unread[channelName] || 0) > 0
  // selectChannel は SSE 接続と履歴取得を並行して走らせるため、どちらの markChannelRead が
  // 先に完了するかは保証されない。後から完了した側の時刻が古いと既読位置を巻き戻してしまい、
  // 画面には表示済みの通知がポーリングのたびに未読として再カウントされ続けるため、
  // 既読位置は常に前進のみ（新しい方を採用）とする（#232）。
  const readAt = Math.max(lastReadAt[channelName] || 0, timestampMs)
  lastReadAt[channelName] = readAt
  saveLastReadAt()
  setChannelUnread(channelName, 0)
  updateBadge(channelName)
  updateDocumentTitle()
  // この端末に出したままの OS 通知を閉じ、未読があったときだけ他端末へも伝える（#216）
  void closeShownNotifications(channelName, readAt)
  if (hadUnread) queueReadBroadcast(channelName, readAt)
}

// ── 既読の端末間同期（#216）──────────────────────────────────────────────────
// 既読は端末ごとの localStorage にしか無いため、PC で読んでもスマホのロック画面には
// 通知が残り続ける。表示済みの OS 通知を閉じられるのは、それを出した端末だけなので、
// 「どのチャンネルをいつまで読んだか」をサーバー経由で本人の他端末へ配って閉じさせる。

// 通知を出さない Push の無駄打ちを避けるため、続けざまの既読操作は1通にまとめる
const READ_BROADCAST_DEBOUNCE_MS = 500

let pendingReadBroadcast = {}  // channel_name -> timestamp (ms)
let readBroadcastTimer = null
// 他端末から降ってきた既読を適用している最中は、そのまま送り返さない
let applyingRemoteRead = false

async function closeShownNotifications(channelName, untilMs) {
  if (!channelName || !('serviceWorker' in navigator)) return
  try {
    const reg = await navigator.serviceWorker.ready
    if (!reg.getNotifications) return
    for (const notification of await reg.getNotifications()) {
      const data = notification.data || {}
      if (data.channel !== channelName) continue
      // 既読にした後に届いた通知まで消さないよう、通知自身の時刻で絞る
      const ts = data.ts ? Date.parse(data.ts) : NaN
      if (untilMs && !Number.isNaN(ts) && ts > untilMs) continue
      notification.close()
    }
  } catch {
    // getNotifications 非対応など
  }
}

function queueReadBroadcast(channelName, timestampMs) {
  if (applyingRemoteRead) return
  const current = pendingReadBroadcast[channelName]
  if (current === undefined || timestampMs > current) {
    pendingReadBroadcast[channelName] = timestampMs
  }
  clearTimeout(readBroadcastTimer)
  readBroadcastTimer = setTimeout(() => void flushReadBroadcast(), READ_BROADCAST_DEBOUNCE_MS)
}

async function flushReadBroadcast() {
  const channels = Object.entries(pendingReadBroadcast)
    .map(([channel, until]) => ({ channel, until }))
  pendingReadBroadcast = {}
  if (!channels.length) return
  try {
    // 自分自身は既に閉じてあるので、送信元の端末は宛先から外す
    const sub = await getBrowserPushSubscription()
    await authFetch(apiUrl('api/read'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ channels, endpoint: sub?.endpoint || null }),
    })
  } catch {
    // 既読はローカルに残るため、配れなくてもこの端末の表示は正しい
  }
}

function applyRemoteRead(channels) {
  if (!Array.isArray(channels) || !channels.length) return
  applyingRemoteRead = true
  try {
    for (const item of channels) {
      const name = item?.channel
      const until = Number(item?.until) || 0
      if (!name || !until) continue
      if ((lastReadAt[name] || 0) >= until) continue
      markChannelRead(name, until)
    }
  } finally {
    applyingRemoteRead = false
  }
}

// アプリのバッジ・タブタイトル・右上ベルマークの件数は、通知を有効にしている
// チャンネルの未読だけを数える（#241）。サイドバーのチャンネル別バッジ・グループの
// バッジはここを通らず、今まで通り全ての未読を数える。
function totalUnread() {
  let total = 0
  for (const [name, count] of Object.entries(unread)) {
    if (!countsTowardUnreadBadge(name)) continue
    total += count || 0
  }
  return total
}

function updateDocumentTitle() {
  const total = totalUnread()
  document.title = total > 0 ? `(${total > 99 ? '99+' : total}) Signaly` : 'Signaly'
  void updateAppBadge(total)
  updateNotificationsBadge(total)
}

function updateNotificationsBadge(total = totalUnread()) {
  if (!notificationsBadge) return
  notificationsBadge.textContent = total > 99 ? '99+' : String(total)
  notificationsBadge.hidden = total === 0
}

async function updateAppBadge(total = totalUnread()) {
  try {
    if ('setAppBadge' in navigator) {
      if (total > 0) {
        await navigator.setAppBadge(total)
      } else if ('clearAppBadge' in navigator) {
        await navigator.clearAppBadge()
      }
    }
  } catch {
    // 通知未許可・非対応環境など
  }
  const sw = navigator.serviceWorker?.controller
  if (sw) sw.postMessage({ type: 'sync-app-badge', count: total })
}

// ── Notification card ────────────────────────────────────────────────────────

// 強調変換に巻き込まれないよう、生成済みHTMLを一時退避するときの目印（#131）
const STASH_MARK = /@@SIGNALY-STASH-(\d+)@@/g

function renderFieldValue(raw) {
  // 生成済みHTML（<code>・<a>の開始タグ）に含まれる `_` や `*` が後続の強調変換に
  // 巻き込まれないよう、いったんプレースホルダへ退避してから最後に戻す。
  // 強調変換の対象を「エスケープ済みの本文」だけに限定するのが目的（#131）。
  const stashed = []
  const stash = (html) => `@@SIGNALY-STASH-${stashed.push(html) - 1}@@`

  const escaped = String(raw)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(STASH_MARK, '') // 入力に目印と同じ文字列があっても混同しないよう除去する
  return escaped
    .replace(/`([^`\n]+)`/g, (_m, code) => stash(`<code>${code}</code>`))
    .replace(
      /\[([^\]]+)\]\((https?:\/\/[^)]+)\)/g,
      (_m, label, url) =>
        `${stash(`<a href="${url.replace(/"/g, '&quot;')}" target="_blank" rel="noopener noreferrer">`)}${label}</a>`
    )
    .replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>')
    .replace(/__([^_\n]+)__/g, '<strong>$1</strong>')
    .replace(/(?<!\*)\*([^*\n]+)\*(?!\*)/g, '<em>$1</em>')
    .replace(/(?<!_)_([^_\n]+)_(?!_)/g, '<em>$1</em>')
    .replace(/:rocket:/g, '🚀')
    .replace(STASH_MARK, (_m, i) => stashed[Number(i)])
}

const NOTIF_DELETE_ICON = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
  <polyline points="3 6 5 6 21 6"/>
  <path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/>
  <path d="M10 11v6"/>
  <path d="M14 11v6"/>
  <path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/>
</svg>`

const notificationDeleteDialog = document.getElementById('notification-delete-dialog')
const notificationDeleteError = document.getElementById('notification-delete-error')
const notificationDeleteCancel = document.getElementById('notification-delete-cancel')
const notificationDeleteConfirm = document.getElementById('notification-delete-confirm')

let pendingDeleteNotificationId = null

function openNotificationDeleteDialog(id) {
  pendingDeleteNotificationId = id
  notificationDeleteError.hidden = true
  notificationDeleteError.textContent = ''
  notificationDeleteConfirm.disabled = false
  notificationDeleteCancel.disabled = false
  SignalyDialog.open(notificationDeleteDialog, { focusEl: notificationDeleteCancel })
}

function closeNotificationDeleteDialog() {
  SignalyDialog.close(notificationDeleteDialog)
  pendingDeleteNotificationId = null
  notificationDeleteError.hidden = true
  notificationDeleteError.textContent = ''
}

notificationDeleteCancel?.addEventListener('click', closeNotificationDeleteDialog)

notificationDeleteDialog?.addEventListener('click', (e) => {
  if (e.target === notificationDeleteDialog) closeNotificationDeleteDialog()
})

document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && notificationDeleteDialog?.classList.contains('open')) {
    closeNotificationDeleteDialog()
  }
})

function deleteNotificationsRequest(ids) {
  return authFetch(apiUrl('api/notifications'), {
    method: 'DELETE',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ids }),
  })
}

notificationDeleteConfirm?.addEventListener('click', () => {
  const id = pendingDeleteNotificationId
  if (!id) return

  runConfirmAction({
    confirmBtn: notificationDeleteConfirm,
    cancelBtn: notificationDeleteCancel,
    errorEl: notificationDeleteError,
    run: async (showError) => {
      const res = await deleteNotificationsRequest([id])
      if (!res.ok) {
        showError('削除に失敗しました')
        return
      }
      removeNotificationCard(id)
      removeUnreadListRow(id)
      closeNotificationDeleteDialog()
      showToast('削除しました')
    },
  })
})

function deleteNotification(id) {
  openNotificationDeleteDialog(id)
}

function removeNotificationCard(id) {
  seenIds.delete(id)
  const wasSelected = selectedNotifIds.delete(id)
  const card = feed.querySelector(`.notif-card[data-id="${CSS.escape(id)}"]`)
  if (!card) {
    if (wasSelected && notifSelectMode) updateNotifSelectBar()
    return
  }
  const dateKey = card.dataset.date
  card.remove()
  if (dateKey && !feed.querySelector(`.notif-card[data-date="${CSS.escape(dateKey)}"]`)) {
    feed.querySelector(`.feed-date-divider[data-date="${CSS.escape(dateKey)}"]`)?.remove()
  }
  if (emptyState && !feed.querySelector('.notif-card')) {
    emptyState.hidden = false
  }
  if (wasSelected && notifSelectMode) updateNotifSelectBar()
}

// #feed の .notif-card が MAX_FEED_CARDS を超えたら、末尾（最も古い側）から
// 間引く。サーバー側のデータは消えないため、チャンネルを開き直せば再度表示できる。
function trimFeedOverflow() {
  const cards = feed.querySelectorAll('.notif-card')
  const excess = cards.length - MAX_FEED_CARDS
  if (excess <= 0) return
  for (let i = cards.length - excess; i < cards.length; i++) {
    const card = cards[i]
    const id = card.dataset.id
    seenIds.delete(id)
    selectedNotifIds.delete(id)
    const dateKey = card.dataset.date
    card.remove()
    if (dateKey && !feed.querySelector(`.notif-card[data-date="${CSS.escape(dateKey)}"]`)) {
      feed.querySelector(`.feed-date-divider[data-date="${CSS.escape(dateKey)}"]`)?.remove()
    }
  }
  if (notifSelectMode) updateNotifSelectBar()
}

function removeUnreadListRow(id) {
  const row = notificationsListEl?.querySelector(`.search-result[data-id="${CSS.escape(id)}"]`)
  if (!row) return
  const channelName = row.dataset.channel
  row.remove()
  if (!notificationsListEl.querySelector('.search-result')) {
    renderNotificationsMessage('未読メッセージはありません', 'search-empty')
  }
  if (channelName && unread[channelName] > 0) {
    setChannelUnread(channelName, unread[channelName] - 1)
    updateAllBadges()
  }
}

async function deleteUnreadNotification(id) {
  try {
    const res = await deleteNotificationsRequest([id])
    if (!res.ok) {
      showToast('削除に失敗しました')
      return
    }
    removeNotificationCard(id)
    removeUnreadListRow(id)
    showToast('削除しました')
  } catch {
    showToast('ネットワークエラーが発生しました')
  }
}

function restoreSwipedCard(content) {
  content.style.transform = ''
}

// ベルマーク通知一覧のスワイプ削除用（確認なし即削除）。
async function swipeDeleteNotification(id, content) {
  try {
    const res = await deleteNotificationsRequest([id])
    if (!res.ok) {
      showToast('削除に失敗しました')
      restoreSwipedCard(content)
      return
    }
    removeNotificationCard(id)
    removeUnreadListRow(id)
    showToast('削除しました')
  } catch {
    showToast('ネットワークエラーが発生しました')
    restoreSwipedCard(content)
  }
}

function createCard(entry, { isNew = false } = {}) {
  const card = document.createElement('div')
  card.className = 'notif-card' + (isNew ? ' notif-card--new' : '')
  card.dataset.level = entry.level || 'info'
  card.dataset.id = entry.id
  card.dataset.date = getDateKey(entry.timestamp)

  const swipeAction = document.createElement('div')
  swipeAction.className = 'swipe-action'
  swipeAction.textContent = '削除'
  card.appendChild(swipeAction)

  const content = document.createElement('div')
  content.className = 'notif-card-content'
  card.appendChild(content)

  if (entry.color) {
    content.style.borderLeftColor = entry.color
  }

  const header = document.createElement('div')
  header.className = 'notif-header'

  const checkbox = document.createElement('input')
  checkbox.type = 'checkbox'
  checkbox.className = 'notif-card-checkbox'
  checkbox.setAttribute('aria-label', 'この通知を選択')
  checkbox.checked = selectedNotifIds.has(entry.id)
  header.appendChild(checkbox)

  const title = document.createElement('span')
  title.className = 'notif-title'
  title.innerHTML = renderFieldValue(entry.title || entry.channel)

  const time = document.createElement('span')
  time.className = 'notif-time'
  time.textContent = formatNotificationTime(entry.timestamp)

  header.appendChild(title)

  if (entry.source) {
    const source = document.createElement('span')
    source.className = 'notif-source'
    source.textContent = entry.source
    source.title = `送信元: ${entry.source}`
    header.appendChild(source)
  }

  if (isNew) {
    const badge = document.createElement('span')
    badge.className = 'notif-new-badge'
    badge.textContent = '新着'
    header.appendChild(badge)
  }

  header.appendChild(time)

  const deleteBtn = document.createElement('button')
  deleteBtn.type = 'button'
  deleteBtn.className = 'notif-delete-btn'
  deleteBtn.title = '通知を削除'
  deleteBtn.setAttribute('aria-label', '通知を削除')
  deleteBtn.innerHTML = NOTIF_DELETE_ICON
  deleteBtn.addEventListener('click', (e) => {
    e.stopPropagation()
    void deleteNotification(entry.id)
  })
  header.appendChild(deleteBtn)

  content.appendChild(header)

  if (entry.message) {
    const msg = document.createElement('div')
    msg.className = 'notif-message'
    msg.innerHTML = renderFieldValue(entry.message)
    content.appendChild(msg)
  }

  if (entry.fields && entry.fields.length > 0) {
    const fieldsEl = document.createElement('div')
    fieldsEl.className = 'embed-fields'
    for (const f of entry.fields) {
      const fieldEl = document.createElement('div')
      fieldEl.className = f.inline ? 'embed-field embed-field--inline' : 'embed-field'

      const nameEl = document.createElement('div')
      nameEl.className = 'embed-field-name'
      nameEl.textContent = f.name

      const valueEl = document.createElement('div')
      valueEl.className = 'embed-field-value'
      valueEl.innerHTML = renderFieldValue(String(f.value))

      fieldEl.appendChild(nameEl)
      fieldEl.appendChild(valueEl)
      fieldsEl.appendChild(fieldEl)
    }
    content.appendChild(fieldsEl)
  }

  return card
}

function clearNewCardHighlight(card) {
  card.classList.remove('notif-card--new')
  card.querySelector('.notif-new-badge')?.remove()
}

function dismissNewHighlights() {
  feed.querySelectorAll('.notif-card--new').forEach(clearNewCardHighlight)
  pendingNewCount = 0
  if (newNotifBanner) newNotifBanner.hidden = true
}

function scheduleNewCardFade(card) {
  setTimeout(() => {
    if (card.isConnected) clearNewCardHighlight(card)
  }, NEW_CARD_FADE_MS)
}

function highlightNotificationCard(id, { retries = 5 } = {}) {
  if (!id || !feed) return false

  const card = feed.querySelector(`.notif-card[data-id="${CSS.escape(id)}"]`)
  if (!card) {
    if (retries > 0) {
      setTimeout(() => highlightNotificationCard(id, { retries: retries - 1 }), 200)
    }
    return false
  }

  feed.querySelectorAll('.notif-card--highlight').forEach((c) => {
    c.classList.remove('notif-card--highlight')
  })

  card.classList.add('notif-card--highlight')
  card.scrollIntoView({ behavior: 'smooth', block: 'center' })
  clearPushDeepLinkMarker()

  setTimeout(() => {
    if (card.isConnected) card.classList.remove('notif-card--highlight')
  }, HIGHLIGHT_FADE_MS)

  return true
}

function consumeNotificationHighlightId() {
  const id = pendingHighlightId || notificationIdFromQuery()
  pendingHighlightId = null
  return id
}

async function handleNotificationNavigation({ channel, id, url } = {}) {
  const params = url ? new URL(url, location.origin).searchParams : null
  const targetChannel = channel || params?.get('channel')
  const targetId = id || params?.get('id')
  if (!targetChannel) return

  if (targetId) pendingHighlightId = targetId

  if (activeChannel === targetChannel) {
    const highlightId = consumeNotificationHighlightId()
    if (highlightId) highlightNotificationCard(highlightId)
    else clearPushDeepLinkMarker()
    return
  }

  const nextUrl = new URL(location.href)
  nextUrl.searchParams.set('channel', targetChannel)
  if (targetId) nextUrl.searchParams.set('id', targetId)
  nextUrl.searchParams.set('src', 'push')
  history.replaceState(null, '', nextUrl)

  await selectChannel(targetChannel)
}

// ── Search ───────────────────────────────────────────────────────────────────

const SEARCH_DEBOUNCE_MS = 300

const searchBtn = document.getElementById('search-btn')
const searchDialog = document.getElementById('search-dialog')
const searchClose = document.getElementById('search-close')
const searchInput = document.getElementById('search-input')
const searchResultsEl = document.getElementById('search-results')
const searchScopeEl = document.getElementById('search-scope')
const searchScopeBtns = searchScopeEl ? Array.from(searchScopeEl.querySelectorAll('.search-scope-btn')) : []

let searchDebounceTimer = null
let searchRequestId = 0
let searchScope = 'all'

function renderSearchMessage(text, className = 'search-hint') {
  searchResultsEl.innerHTML = ''
  const p = document.createElement('p')
  p.className = className
  p.textContent = text
  searchResultsEl.appendChild(p)
}

function searchExcerpt(entry) {
  return String(entry.message || '').replace(/\s+/g, ' ').trim().slice(0, 140)
}

function createResultRow(entry, onSelect, { deletable = false } = {}) {
  const row = document.createElement('div')
  row.className = 'search-result'
  row.dataset.id = entry.id
  row.dataset.channel = entry.channel

  let content = row
  if (deletable) {
    const swipeAction = document.createElement('div')
    swipeAction.className = 'swipe-action'
    swipeAction.textContent = '削除'
    row.appendChild(swipeAction)

    content = document.createElement('div')
    content.className = 'search-result-content'
    row.appendChild(content)
  }

  const main = document.createElement('button')
  main.type = 'button'
  main.className = 'search-result-main'

  const header = document.createElement('div')
  header.className = 'search-result-header'

  const channelEl = document.createElement('span')
  channelEl.className = 'search-result-channel'
  // 統合したチャンネルではチャンネル名だけでは発信元が分からないので送信元を添える
  channelEl.textContent = entry.source
    ? `#${entry.channel} · ${entry.source}`
    : `#${entry.channel}`

  const timeEl = document.createElement('span')
  timeEl.className = 'search-result-time'
  timeEl.textContent = `${formatDateFromKey(getDateKey(entry.timestamp))} ${formatNotificationTime(entry.timestamp)}`

  header.appendChild(channelEl)
  header.appendChild(timeEl)
  main.appendChild(header)

  if (entry.title) {
    const titleEl = document.createElement('div')
    titleEl.className = 'search-result-title'
    titleEl.textContent = entry.title
    main.appendChild(titleEl)
  }

  const excerpt = searchExcerpt(entry)
  if (excerpt) {
    const excerptEl = document.createElement('div')
    excerptEl.className = 'search-result-excerpt'
    excerptEl.textContent = excerpt
    main.appendChild(excerptEl)
  }

  main.addEventListener('click', () => {
    onSelect()
    void handleNotificationNavigation({ channel: entry.channel, id: entry.id })
  })

  content.appendChild(main)

  if (deletable) {
    const deleteBtn = document.createElement('button')
    deleteBtn.type = 'button'
    deleteBtn.className = 'search-result-delete'
    deleteBtn.title = '通知を削除'
    deleteBtn.setAttribute('aria-label', '通知を削除')
    deleteBtn.innerHTML = NOTIF_DELETE_ICON
    deleteBtn.addEventListener('click', (e) => {
      e.stopPropagation()
      void deleteUnreadNotification(entry.id)
    })
    content.appendChild(deleteBtn)
  }

  return row
}

function createSearchResultRow(entry) {
  return createResultRow(entry, closeSearchDialog)
}

function renderSearchResults(results) {
  searchResultsEl.innerHTML = ''
  if (!results.length) {
    renderSearchMessage('一致するメッセージが見つかりませんでした', 'search-empty')
    return
  }
  for (const entry of results) {
    searchResultsEl.appendChild(createSearchResultRow(entry))
  }
}

function updateSearchScopeUI() {
  for (const btn of searchScopeBtns) {
    const scope = btn.dataset.scope
    btn.classList.toggle('active', scope === searchScope)
    btn.disabled = scope === 'channel' && !activeChannel
  }
}

async function runSearch(query) {
  const requestId = ++searchRequestId
  try {
    const params = new URLSearchParams({ q: query })
    if (searchScope === 'channel' && activeChannel) params.set('channel', activeChannel)
    const res = await authFetch(apiUrl(`api/search?${params.toString()}`))
    if (requestId !== searchRequestId) return
    if (!res.ok) {
      renderSearchMessage('検索に失敗しました', 'search-error')
      return
    }
    const data = await res.json()
    if (requestId !== searchRequestId) return
    renderSearchResults(data.results || [])
  } catch {
    if (requestId !== searchRequestId) return
    renderSearchMessage('ネットワークエラーが発生しました', 'search-error')
  }
}

function openSearchDialog() {
  if (!searchDialog) return
  closeSidebar()
  if (searchScope === 'channel' && !activeChannel) searchScope = 'all'
  updateSearchScopeUI()
  SignalyDialog.open(searchDialog, { focusEl: searchInput })
  if (!searchInput.value.trim()) renderSearchMessage('キーワードを入力してください')
}

function closeSearchDialog() {
  SignalyDialog.close(searchDialog)
}

channelSettingsHeaderBtn?.addEventListener('click', () => {
  if (activeChannel) openChannelSettings(activeChannel)
})

searchBtn?.addEventListener('click', openSearchDialog)
searchClose?.addEventListener('click', closeSearchDialog)
searchDialog?.addEventListener('click', (e) => {
  if (e.target === searchDialog) closeSearchDialog()
})

searchInput?.addEventListener('input', () => {
  const query = searchInput.value.trim()
  searchRequestId++
  clearTimeout(searchDebounceTimer)
  if (!query) {
    renderSearchMessage('キーワードを入力してください')
    return
  }
  renderSearchMessage('検索中…')
  searchDebounceTimer = setTimeout(() => runSearch(query), SEARCH_DEBOUNCE_MS)
})

for (const btn of searchScopeBtns) {
  btn.addEventListener('click', () => {
    const scope = btn.dataset.scope
    if (scope === searchScope || btn.disabled) return
    searchScope = scope
    updateSearchScopeUI()
    const query = searchInput.value.trim()
    if (!query) return
    searchRequestId++
    clearTimeout(searchDebounceTimer)
    renderSearchMessage('検索中…')
    void runSearch(query)
  })
}

document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && searchDialog?.classList.contains('open')) {
    closeSearchDialog()
  }
  if (e.key === 'Escape' && notificationsDialog?.classList.contains('open')) {
    closeNotificationsDialog()
  }
})

// ── Notifications (unread) ──────────────────────────────────────────────────

const NOTIFICATIONS_HISTORY_LIMIT = 200

const notificationsDialog = document.getElementById('notifications-dialog')
const notificationsClose = document.getElementById('notifications-close')
const notificationsMarkAllRead = document.getElementById('notifications-mark-all-read')
const notificationsListEl = document.getElementById('notifications-list')

let notificationsRequestId = 0

function renderNotificationsMessage(text, className = 'search-hint') {
  notificationsListEl.innerHTML = ''
  const p = document.createElement('p')
  p.className = className
  p.textContent = text
  notificationsListEl.appendChild(p)
}

function renderNotificationsList(entries) {
  notificationsListEl.innerHTML = ''
  if (!entries.length) {
    renderNotificationsMessage('未読メッセージはありません', 'search-empty')
    return
  }
  for (const entry of entries) {
    notificationsListEl.appendChild(createResultRow(entry, closeNotificationsDialog, { deletable: true }))
  }
}

async function fetchUnreadForChannel(name) {
  try {
    const res = await authFetch(apiUrl(`api/history/${name}?limit=${NOTIFICATIONS_HISTORY_LIMIT}`))
    if (!res.ok) return []
    const { logs } = await res.json()
    const since = lastReadAt[name]
    return since === undefined ? logs : logs.filter(e => parseTimestamp(e.timestamp) > since)
  } catch {
    return []
  }
}

async function loadUnreadMessages() {
  const requestId = ++notificationsRequestId
  // 件数（バッジ・ベルマーク）と中身を一致させるため、通知を無効にしたチャンネルは並べない（#241）
  const names = allChannelNames().filter(name => unread[name] > 0 && countsTowardUnreadBadge(name))
  if (!names.length) {
    renderNotificationsList([])
    return
  }
  renderNotificationsMessage('読み込み中…')
  const lists = await Promise.all(names.map(fetchUnreadForChannel))
  if (requestId !== notificationsRequestId) return
  const entries = lists.flat().sort((a, b) => parseTimestamp(b.timestamp) - parseTimestamp(a.timestamp))
  renderNotificationsList(entries)
}

function openNotificationsDialog() {
  if (!notificationsDialog) return
  closeSidebar()
  SignalyDialog.open(notificationsDialog)
  void loadUnreadMessages()
}

function closeNotificationsDialog() {
  SignalyDialog.close(notificationsDialog)
}

notificationsBtn?.addEventListener('click', openNotificationsDialog)
notificationsClose?.addEventListener('click', closeNotificationsDialog)
notificationsDialog?.addEventListener('click', (e) => {
  if (e.target === notificationsDialog) closeNotificationsDialog()
})
notificationsMarkAllRead?.addEventListener('click', () => {
  markAllChannelsRead()
  closeNotificationsDialog()
})

function updateNewNotifBanner() {
  if (!newNotifBanner || pendingNewCount <= 0) return
  const scrolled = feed.scrollTop > 40
  newNotifBanner.hidden = !scrolled
  newNotifBanner.textContent = `新着 ${pendingNewCount} 件`
}

function prependCard(entry, { isNew = false } = {}) {
  if (seenIds.has(entry.id)) return
  seenIds.add(entry.id)

  const dateKey = getDateKey(entry.timestamp)
  const card = createCard(entry, { isNew })
  // 履歴の一括読み込みでは付与しない（多数のカードが同時にアニメーションし、
  // iOS Safari で描画が崩れるため）。ライブ受信の1件挿入時のみ付与する。
  if (isNew) card.classList.add('notif-card--enter')

  const next = feed.firstChild
  if (next?.classList.contains('notif-card') && next.dataset.date !== dateKey) {
    feed.insertBefore(createDateDivider(next.dataset.date), next)
  }
  feed.insertBefore(card, feed.firstChild)
  if (emptyState) emptyState.hidden = true

  if (isNew) {
    if (feed.scrollTop > 40) {
      pendingNewCount++
      updateNewNotifBanner()
    } else {
      feed.scrollTop = 0
    }
    scheduleNewCardFade(card)
  }
  updateStickyFeedDate()
  trimFeedOverflow()
}

// ── Channel list ─────────────────────────────────────────────────────────────

const CHANNEL_SETTINGS_ICON = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
  <circle cx="12" cy="12" r="3"/>
  <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/>
</svg>`

const ADD_ICON = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" aria-hidden="true">
  <line x1="12" y1="5" x2="12" y2="19"/>
  <line x1="5" y1="12" x2="19" y2="12"/>
</svg>`

const DRAG_HANDLE_ICON = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true">
  <line x1="8" y1="6" x2="8" y2="18"/>
  <line x1="16" y1="6" x2="16" y2="18"/>
</svg>`

const REORDER_DONE_ICON = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
  <polyline points="20 6 9 17 4 12"/>
</svg>`

const GROUP_COLLAPSE_ICON = `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
  <polyline points="9 6 15 12 9 18"/>
</svg>`

let channelGroups = []
let channelUngrouped = []
let groupsById = {}
let reorderMode = false
let lastChannelTree = null
let collapsedGroups = loadCollapsedGroups()

function createReorderHandle() {
  const handle = document.createElement('span')
  handle.className = 'reorder-handle'
  handle.innerHTML = DRAG_HANDLE_ICON
  return handle
}

function allChannelNames() {
  const names = []
  for (const group of channelGroups) {
    for (const channel of group.channels || []) names.push(channel.name)
  }
  for (const channel of channelUngrouped) names.push(channel.name)
  return names
}

function createAddBtn(title, onClick) {
  const btn = document.createElement('button')
  btn.type = 'button'
  btn.className = 'add-channel-btn'
  btn.title = title
  btn.setAttribute('aria-label', title)
  btn.innerHTML = ADD_ICON
  btn.addEventListener('click', (e) => {
    e.stopPropagation()
    onClick()
  })
  return btn
}

function createChannelRow(channel) {
  channelsByName[channel.name] = channel

  const row = document.createElement('div')
  row.className = 'channel-row'
  row.dataset.channel = channel.name
  if (channel.id) row.dataset.channelId = channel.id

  if (reorderMode) {
    row.classList.add('channel-row--reorderable')
    row.draggable = true
    row.appendChild(createReorderHandle())
  }

  const btn = document.createElement('button')
  btn.type = 'button'
  btn.className = 'channel-item'

  const label = document.createElement('span')
  label.className = 'channel-item-label'
  label.textContent = channel.name
  btn.appendChild(label)

  if (!reorderMode) {
    const notifIndicator = document.createElement('span')
    notifIndicator.className = 'notif-indicator'
    notifIndicator.setAttribute('aria-hidden', 'true')
    btn.appendChild(notifIndicator)
    applyChannelNotifIndicator(notifIndicator, channel)
  }

  if (!reorderMode) {
    btn.addEventListener('click', () => selectChannel(channel.name))
  }

  row.appendChild(btn)

  if (!reorderMode) {
    const settingsBtn = document.createElement('button')
    settingsBtn.type = 'button'
    settingsBtn.className = 'channel-settings-btn'
    settingsBtn.title = 'チャンネル設定'
    settingsBtn.setAttribute('aria-label', `${channel.name} の設定`)
    settingsBtn.innerHTML = CHANNEL_SETTINGS_ICON
    settingsBtn.addEventListener('click', (e) => {
      e.stopPropagation()
      openChannelSettings(channel.name)
    })
    row.appendChild(settingsBtn)
  }

  return row
}

function createGroupActions(...buttons) {
  const actions = document.createElement('div')
  actions.className = 'channel-group-actions'
  for (const btn of buttons) actions.appendChild(btn)
  return actions
}

function setGroupCollapsedUI(section, btn, groupName, collapsed) {
  section.classList.toggle('channel-group--collapsed', collapsed)
  btn.setAttribute('aria-expanded', String(!collapsed))
  const title = `${groupName} を${collapsed ? '展開する' : '折りたたむ'}`
  btn.title = title
  btn.setAttribute('aria-label', title)
}

function createGroupCollapseToggle(section, sectionId, groupName) {
  const btn = document.createElement('button')
  btn.type = 'button'
  btn.className = 'group-collapse-btn'
  btn.innerHTML = GROUP_COLLAPSE_ICON
  setGroupCollapsedUI(section, btn, groupName, collapsedGroups.has(sectionId))
  btn.addEventListener('click', (e) => {
    e.stopPropagation()
    toggleGroupCollapsed(sectionId)
    setGroupCollapsedUI(section, btn, groupName, collapsedGroups.has(sectionId))
  })
  return btn
}

function createGroupSection(group) {
  const section = document.createElement('section')
  section.className = 'channel-group'
  section.dataset.groupId = group.id

  const header = document.createElement('div')
  header.className = 'channel-group-header'

  const label = document.createElement('span')
  label.className = 'channel-group-label'
  label.textContent = group.name

  const labelWrap = document.createElement('div')
  labelWrap.className = 'channel-group-label-wrap'

  if (reorderMode) {
    section.classList.add('channel-group--reorderable')
    section.draggable = true
    header.appendChild(createReorderHandle())
    labelWrap.appendChild(label)
    header.appendChild(labelWrap)
  } else {
    const collapseBtn = createGroupCollapseToggle(section, group.id, group.name)
    labelWrap.appendChild(collapseBtn)

    const notifIndicator = document.createElement('span')
    notifIndicator.className = 'notif-indicator'
    notifIndicator.setAttribute('aria-hidden', 'true')
    labelWrap.appendChild(notifIndicator)
    applyGroupNotifIndicator(notifIndicator, group.id)
    labelWrap.appendChild(label)

    const groupBadge = document.createElement('span')
    groupBadge.className = 'channel-group-badge'
    groupBadge.hidden = true
    labelWrap.appendChild(groupBadge)

    const settingsBtn = document.createElement('button')
    settingsBtn.type = 'button'
    settingsBtn.className = 'group-settings-btn'
    settingsBtn.title = 'グループ設定'
    settingsBtn.setAttribute('aria-label', `${group.name} の設定`)
    settingsBtn.innerHTML = CHANNEL_SETTINGS_ICON
    settingsBtn.addEventListener('click', (e) => {
      e.stopPropagation()
      openGroupSettings(group.id)
    })

    header.appendChild(labelWrap)
    header.appendChild(createGroupActions(
      settingsBtn,
      createAddBtn('チャンネルを追加', () => openCreateChannelDialog(group.id)),
    ))

    header.classList.add('channel-group-header--clickable')
    header.addEventListener('click', () => {
      toggleGroupCollapsed(group.id)
      setGroupCollapsedUI(section, collapseBtn, group.name, collapsedGroups.has(group.id))
    })
  }

  const list = document.createElement('div')
  list.className = 'channel-group-channels'
  for (const channel of group.channels || []) {
    list.appendChild(createChannelRow(channel))
  }

  section.appendChild(header)
  section.appendChild(list)
  return section
}

function createUngroupedSection() {
  const section = document.createElement('section')
  section.className = 'channel-group channel-group--ungrouped'
  section.dataset.groupId = UNGROUPED_SECTION_ID

  const header = document.createElement('div')
  header.className = 'channel-group-header'

  const label = document.createElement('span')
  label.className = 'channel-group-label'
  label.textContent = '未分類'

  const labelWrap = document.createElement('div')
  labelWrap.className = 'channel-group-label-wrap'

  let collapseBtn = null
  if (!reorderMode) {
    collapseBtn = createGroupCollapseToggle(section, UNGROUPED_SECTION_ID, '未分類')
    labelWrap.appendChild(collapseBtn)
  }
  labelWrap.appendChild(label)

  if (!reorderMode) {
    const groupBadge = document.createElement('span')
    groupBadge.className = 'channel-group-badge'
    groupBadge.hidden = true
    labelWrap.appendChild(groupBadge)
  }
  header.appendChild(labelWrap)

  if (!reorderMode) {
    header.appendChild(createGroupActions(
      createAddBtn('チャンネルを追加', () => openCreateChannelDialog(null)),
    ))
    header.classList.add('channel-group-header--clickable')
    header.addEventListener('click', () => {
      toggleGroupCollapsed(UNGROUPED_SECTION_ID)
      setGroupCollapsedUI(section, collapseBtn, '未分類', collapsedGroups.has(UNGROUPED_SECTION_ID))
    })
  }

  const list = document.createElement('div')
  list.className = 'channel-group-channels'
  for (const channel of channelUngrouped) {
    list.appendChild(createChannelRow(channel))
  }

  section.appendChild(header)
  section.appendChild(list)
  return section
}

function normalizeChannelTree(data) {
  const channels = data?.channels || []
  if (Array.isArray(data?.groups) && Array.isArray(data?.ungrouped)) {
    return {
      groups: data.groups.map(g => ({ ...g, channels: g.channels || [] })),
      ungrouped: data.ungrouped,
      channels,
    }
  }
  return { groups: [], ungrouped: channels, channels }
}

function renderChannelTree(data, selectName = null, options = {}) {
  const { skipSelect = false } = options
  const tree = normalizeChannelTree(data)
  channelList.innerHTML = ''
  channelsByName = {}
  groupsById = {}
  channelGroups = tree.groups
  channelUngrouped = tree.ungrouped

  for (const group of channelGroups) {
    groupsById[group.id] = group
    for (const channel of group.channels || []) {
      channelsByName[channel.name] = channel
    }
  }
  for (const channel of channelUngrouped) {
    channelsByName[channel.name] = channel
  }

  const names = allChannelNames()
  pruneStaleReadState(names)

  if (!channelGroups.length && !names.length) {
    channelList.innerHTML = '<div class="loading-text">グループを作成してチャンネルを追加</div>'
    activeChannel = null
    channelTitle.textContent = 'チャンネルを選択'
    if (channelSettingsHeaderBtn) channelSettingsHeaderBtn.hidden = true
    channelSources = []
    activeSource = null
    renderSourceFilter()
    hideFeedState()
    exitNotifSelectMode()
    updateDocumentTitle()
    return
  }

  for (const group of channelGroups) {
    channelList.appendChild(createGroupSection(group))
  }
  channelList.appendChild(createUngroupedSection())

  if (reorderMode) {
    SignalyReorder.setActive(true)
  }

  if (skipSelect) return

  const target = (selectName && names.includes(selectName) ? selectName : null)
    ?? (activeChannel && names.includes(activeChannel) ? null : names[0] ?? null)

  if (target) {
    selectChannel(target)
  } else if (activeChannel) {
    setActiveChannelRow(activeChannel)
    updateAllBadges()
    hideFeedState()
  } else {
    hideFeedState()
  }
}

function currentTreeSnapshot() {
  return {
    groups: channelGroups,
    ungrouped: channelUngrouped,
    channels: Object.values(channelsByName),
  }
}

function updateReorderModeBtn() {
  const btn = document.getElementById('reorder-mode-btn')
  const hint = document.getElementById('reorder-hint')
  if (!btn) return
  if (reorderMode) {
    btn.title = '並び替えを完了'
    btn.setAttribute('aria-label', '並び替えを完了')
    btn.innerHTML = REORDER_DONE_ICON
    if (hint) hint.hidden = false
  } else {
    btn.title = '並び替え'
    btn.setAttribute('aria-label', '並び替え')
    btn.innerHTML = DRAG_HANDLE_ICON
    if (hint) hint.hidden = true
  }
}

function enterReorderMode() {
  if (reorderMode) return
  reorderMode = true
  lastChannelTree = currentTreeSnapshot()
  renderChannelTree(lastChannelTree, null, { skipSelect: true })
  updateReorderModeBtn()
}

function blurSidebarActionFocus() {
  const el = document.activeElement
  if (el?.closest?.('.channel-section-actions')) {
    el.blur()
  }
}

async function exitReorderMode() {
  if (!reorderMode) return

  const btn = document.getElementById('reorder-mode-btn')
  const layout = SignalyReorder.collectLayout()
  if (btn) {
    btn.blur()
    btn.disabled = true
  }

  try {
    const res = await authFetch(apiUrl('api/channels/layout'), {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(layout),
    })
    const data = await res.json().catch(() => ({}))
    if (!res.ok) {
      alert(parseApiError(data, '並び替えの保存に失敗しました'))
      return
    }
  } catch {
    alert('ネットワークエラーが発生しました')
    return
  } finally {
    if (btn) btn.disabled = false
  }

  reorderMode = false
  SignalyReorder.setActive(false)
  updateReorderModeBtn()
  blurSidebarActionFocus()
  await refreshChannels(activeChannel)
}

SignalyReorder.init(channelList)

const reorderModeBtn = document.getElementById('reorder-mode-btn')
const markAllReadBtn = document.getElementById('mark-all-read-btn')

reorderModeBtn?.addEventListener('click', (e) => {
  e.preventDefault()
  e.stopPropagation()
  if (reorderMode) {
    void exitReorderMode()
  } else {
    enterReorderMode()
  }
})

function markAllChannelsRead() {
  for (const name of allChannelNames()) {
    if (unread[name]) markChannelRead(name)
  }
  showToast('既読にしました')
}

markAllReadBtn?.addEventListener('click', markAllChannelsRead)

function setActiveChannelRow(channelName) {
  channelList.querySelectorAll('.channel-row').forEach(row => {
    row.querySelector('.channel-item')?.classList.toggle(
      'active',
      row.dataset.channel === channelName,
    )
  })
}

function updateBadge(channelName) {
  const row = channelList.querySelector(`.channel-row[data-channel="${channelName}"]`)
  if (!row) return
  const btn = row.querySelector('.channel-item')
  const count = unread[channelName] || 0
  btn.classList.toggle('channel-item--unread', count > 0 && channelName !== activeChannel)
  let badge = btn.querySelector('.badge')
  if (count > 0) {
    if (!badge) {
      badge = document.createElement('span')
      badge.className = 'badge'
      btn.appendChild(badge)
    }
    badge.textContent = count > 99 ? '99+' : String(count)
  } else if (badge) {
    badge.remove()
  }
  updateGroupBadge(channelsByName[channelName]?.group_id || UNGROUPED_SECTION_ID)
}

function groupUnreadTotal(groupId) {
  const names = groupId === UNGROUPED_SECTION_ID
    ? channelUngrouped.map(c => c.name)
    : (groupsById[groupId]?.channels || []).map(c => c.name)
  return names.reduce((sum, name) => sum + (unread[name] || 0), 0)
}

function updateGroupBadge(groupId) {
  const section = channelList.querySelector(`.channel-group[data-group-id="${groupId}"]`)
  if (!section) return
  const badge = section.querySelector(':scope > .channel-group-header .channel-group-badge')
  if (!badge) return
  const count = groupUnreadTotal(groupId)
  badge.textContent = count > 99 ? '99+' : String(count)
  badge.hidden = count === 0
}

function updateAllBadges() {
  for (const name of allChannelNames()) updateBadge(name)
  updateDocumentTitle()
}

// ── Channel selection ─────────────────────────────────────────────────────────

async function selectChannel(name) {
  if (activeChannel === name) {
    closeSidebar()
    return
  }

  exitNotifSelectMode()

  // SSE 接続前に保存（接続後の markChannelRead で上書きされないようにする）
  const sinceLastRead = lastReadAt[name]
  const pendingUnread = unread[name] || 0

  activeChannel = name
  saveLastChannel(name)
  updatePageUrl(name)
  pendingNewCount = 0
  if (newNotifBanner) newNotifBanner.hidden = true

  // UI 更新
  setActiveChannelRow(name)
  updateAllBadges()
  activeSource = sourceFilters[name] || null
  channelSources = []
  renderSourceFilter()
  channelTitle.textContent = `# ${name}`
  if (channelSettingsHeaderBtn) channelSettingsHeaderBtn.hidden = false
  feed.innerHTML = ''
  if (feedStickyDate) feedStickyDate.hidden = true
  if (emptyState) emptyState.hidden = true
  hideFeedState()
  seenIds.clear()
  setStatus('connecting')

  // SSE を先に張り、履歴読み込み中の通知取りこぼしを防ぐ
  void connectSSE(name)
  await loadHistory(name, sinceLastRead, pendingUnread)
  void refreshChannelSources(name)

  closeSidebar()
}

async function loadHistory(channelName, sinceLastRead, pendingUnread = 0) {
  showFeedLoading()
  feedOldestTimestamp = null
  feedOldestId = null
  feedHasMore = false
  feedLoadMoreEl = null
  try {
    const params = new URLSearchParams()
    if (activeSource) params.set('source', activeSource)
    const query = params.toString()
    const res = await authFetch(apiUrl(`api/history/${channelName}${query ? `?${query}` : ''}`))
    if (activeChannel !== channelName) return
    if (!res.ok) {
      showFeedError(
        `読み込みに失敗しました (HTTP ${res.status})`,
        () => loadHistory(channelName, sinceLastRead, pendingUnread),
      )
      return
    }
    const { logs, has_more } = await res.json()
    if (activeChannel !== channelName) return
    hideFeedState()
    if (!logs.length) {
      markChannelRead(channelName)
      if (emptyState) emptyState.hidden = false
      return
    }
    let newestTs = 0
    let prevDateKey = null
    let unreadToMark = sinceLastRead === undefined ? pendingUnread : 0
    // API は新しい順。appendChild で先頭が最新になる
    for (const entry of logs) {
      if (seenIds.has(entry.id)) continue
      seenIds.add(entry.id)
      const dateKey = getDateKey(entry.timestamp)
      if (prevDateKey !== null && prevDateKey !== dateKey) {
        feed.appendChild(createDateDivider(dateKey))
      }
      const ts = parseTimestamp(entry.timestamp)
      const isNew = sinceLastRead !== undefined
        ? ts > sinceLastRead
        : unreadToMark > 0 && unreadToMark--
      const card = createCard(entry, { isNew })
      feed.appendChild(card)
      if (isNew) scheduleNewCardFade(card)
      if (ts > newestTs) newestTs = ts
      prevDateKey = dateKey
    }
    markChannelRead(channelName, newestTs || Date.now())
    const last = logs[logs.length - 1]
    feedOldestTimestamp = last.timestamp
    feedOldestId = last.id
    feedHasMore = Boolean(has_more)
    if (feedHasMore) {
      feedLoadMoreEl = createLoadMoreElement(channelName)
      feed.appendChild(feedLoadMoreEl)
    }
    const highlightId = consumeNotificationHighlightId()
    if (highlightId) {
      requestAnimationFrame(() => highlightNotificationCard(highlightId))
    } else {
      // 最新が上になるよう先頭にスクロール
      feed.scrollTop = 0
    }
    updateStickyFeedDate()
  } catch (err) {
    if (activeChannel !== channelName) return
    const msg = err.name === 'AbortError' ? 'タイムアウト' : 'ネットワークエラー'
    showFeedError(
      `読み込みに失敗しました (${msg})`,
      () => loadHistory(channelName, sinceLastRead, pendingUnread),
    )
  }
}

function createLoadMoreElement(channelName) {
  const wrap = document.createElement('div')
  wrap.className = 'feed-load-more'
  const btn = document.createElement('button')
  btn.type = 'button'
  btn.className = 'feed-load-more-btn'
  btn.textContent = 'もっと読み込む'
  btn.addEventListener('click', () => loadMoreHistory(channelName, wrap, btn))
  wrap.appendChild(btn)
  return wrap
}

async function loadMoreHistory(channelName, wrap, btn) {
  if (feedLoadingMore || !feedHasMore || activeChannel !== channelName) return
  feedLoadingMore = true
  btn.disabled = true
  btn.textContent = '読み込み中…'
  try {
    const params = new URLSearchParams()
    if (feedOldestTimestamp) params.set('before_timestamp', feedOldestTimestamp)
    if (feedOldestId) params.set('before_id', feedOldestId)
    if (activeSource) params.set('source', activeSource)
    const res = await authFetch(apiUrl(`api/history/${channelName}?${params.toString()}`))
    if (activeChannel !== channelName) return
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    const { logs, has_more } = await res.json()
    if (activeChannel !== channelName) return

    let prevDateKey = feed.querySelector('.notif-card:last-of-type')?.dataset.date ?? null
    for (const entry of logs) {
      if (seenIds.has(entry.id)) continue
      seenIds.add(entry.id)
      const dateKey = getDateKey(entry.timestamp)
      if (prevDateKey !== null && prevDateKey !== dateKey) {
        feed.insertBefore(createDateDivider(dateKey), wrap)
      }
      const card = createCard(entry)
      feed.insertBefore(card, wrap)
      prevDateKey = dateKey
    }
    trimFeedOverflow()

    if (logs.length) {
      const last = logs[logs.length - 1]
      feedOldestTimestamp = last.timestamp
      feedOldestId = last.id
    }
    feedHasMore = Boolean(has_more)
    if (!feedHasMore) {
      wrap.remove()
      feedLoadMoreEl = null
    } else {
      btn.disabled = false
      btn.textContent = 'もっと読み込む'
    }
  } catch {
    if (activeChannel !== channelName) return
    btn.disabled = false
    btn.textContent = '読み込みに失敗しました（タップして再試行）'
  } finally {
    feedLoadingMore = false
  }
}

// ── SSE ──────────────────────────────────────────────────────────────────────

function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

function stopUnreadPolling() {
  if (unreadPollTimer) {
    clearInterval(unreadPollTimer)
    unreadPollTimer = null
  }
}

function startUnreadPolling() {
  stopUnreadPolling()
  pollUnreadChannels()
  unreadPollTimer = setInterval(pollUnreadChannels, UNREAD_POLL_MS)
}

async function pollUnreadChannels() {
  const names = allChannelNames()
  if (!names.length) return

  let changed = false
  await Promise.all(names.map(async (name) => {
    if (name === activeChannel) return
    try {
      const res = await authFetch(apiUrl(`api/history/${name}?limit=200`))
      if (!res.ok) return
      const { logs } = await res.json()

      if (lastReadAt[name] === undefined) {
        const timestamps = logs
          .map(e => parseTimestamp(e.timestamp))
          .filter(t => !Number.isNaN(t))
        const storedUnread = unread[name] || 0

        if (storedUnread > 0 && timestamps.length) {
          const sorted = [...timestamps].sort((a, b) => b - a)
          const baselineIdx = Math.min(storedUnread, sorted.length)
          const baseline = baselineIdx < sorted.length
            ? sorted[baselineIdx]
            : sorted[sorted.length - 1] - 1
          lastReadAt[name] = baseline
          saveLastReadAt()
          const count = logs.filter(e => parseTimestamp(e.timestamp) > baseline).length
          if (unread[name] !== count) {
            setChannelUnread(name, count)
            changed = true
          }
          return
        }

        if (timestamps.length) {
          lastReadAt[name] = Math.max(...timestamps)
          saveLastReadAt()
        }
        if (unread[name] !== 0) {
          setChannelUnread(name, 0)
          changed = true
        }
        return
      }

      const since = lastReadAt[name]
      const count = logs.filter(e => parseTimestamp(e.timestamp) > since).length
      if (unread[name] !== count) {
        setChannelUnread(name, count)
        changed = true
      }
    } catch {
      // サイレントに無視
    }
  }))

  if (changed) updateAllBadges()
}

async function pollNewEntries(channelName) {
  if (activeChannel !== channelName) return
  try {
    const pollParams = new URLSearchParams({ limit: '30' })
    if (activeSource) pollParams.set('source', activeSource)
    const res = await authFetch(apiUrl(`api/history/${channelName}?${pollParams.toString()}`))
    if (!res.ok) return
    const { logs } = await res.json()
    const fresh = logs.filter(e => !seenIds.has(e.id) && activeChannel === e.channel)
    let added = false
    // prepend は先頭挿入なので古い順に処理して最新が上に来るようにする
    for (const entry of [...fresh].reverse()) {
      prependCard(entry, { isNew: true })
      noteIncomingSource(entry)
      added = true
    }
    if (added) feed.scrollTop = 0
  } catch {
    // サイレントに無視
  }
}

function startPolling(channelName) {
  stopPolling()
  // SSE がプロキシでバッファされる場合のフォールバック
  pollTimer = setInterval(() => pollNewEntries(channelName), 5000)
}

async function connectSSE(channelName) {
  if (eventSource) {
    eventSource.close()
    eventSource = null
  }
  stopPolling()

  // EventSource は Authorization ヘッダーを付けられないため、
  // 接続前に検証済みトークンと引き換えのセッション Cookie を貼っておく。
  await SignalyAuth.ensureSessionCookie()
  // Cookie を待っている間に別チャンネルへ移っていたら、この接続はもう要らない
  if (activeChannel !== channelName) return

  const url = apiUrl(`api/stream/${channelName}`)
  const es = new EventSource(url)
  eventSource = es

  es.onopen = () => {
    if (activeChannel === channelName) {
      setStatus('connected')
      startPolling(channelName)
    }
  }

  es.onmessage = (event) => {
    let entry
    try {
      entry = JSON.parse(event.data)
    } catch {
      return
    }

    if (activeChannel === entry.channel) {
      if (matchesSourceFilter(entry)) {
        prependCard(entry, { isNew: true })
      }
      markChannelRead(entry.channel, parseTimestamp(entry.timestamp))
      noteIncomingSource(entry)
    } else {
      setChannelUnread(entry.channel, (unread[entry.channel] || 0) + 1)
      updateBadge(entry.channel)
      updateDocumentTitle()
    }

    // デスクトップ通知
    void showDesktopNotification(entry)
  }

  es.addEventListener('delete-bulk', (event) => {
    let data
    try {
      data = JSON.parse(event.data)
    } catch {
      return
    }
    if (activeChannel === channelName) {
      for (const id of data.ids || []) removeNotificationCard(id)
    }
  })

  es.onerror = () => {
    if (activeChannel === channelName) setStatus('disconnected')
    es.close()
    // 5 秒後に再接続
    setTimeout(() => {
      if (activeChannel === channelName) void connectSSE(channelName)
    }, 5000)
  }
}

// ── Notification preferences (channel > group) ───────────────────────────────

async function loadNotificationSettings() {
  try {
    const res = await authFetch(apiUrl('api/notification-settings'))
    if (!res.ok) return false
    notificationSettings = await res.json()
    notificationPrefsReady = true
    refreshNotifIndicators()
    return true
  } catch {
    return false
  }
}

function getChannelNotificationMode(channel) {
  if (!channel?.id) return 'enabled'
  const pref = notificationSettings.channels[channel.id]
  if (pref === true) return 'enabled'
  if (pref === false) return 'disabled'
  if (channel.group_id) return 'inherit'
  return 'enabled'
}

function getGroupNotificationMode(groupId) {
  return notificationSettings.groups[groupId] === false ? 'disabled' : 'enabled'
}

function notifModeTitle(mode, effectiveEnabled) {
  if (mode === 'inherit') {
    return effectiveEnabled
      ? 'グループに従う（通知: 有効）'
      : 'グループに従う（通知: 無効）'
  }
  return mode === 'enabled' ? '通知: 有効' : '通知: 無効'
}

function notifModePreviewTitle(mode) {
  if (mode === 'inherit') return 'グループに従う'
  return mode === 'enabled' ? '有効' : '無効'
}

function notifModePreviewDetail(mode, effectiveEnabled, context) {
  if (mode === 'inherit') {
    return `グループ設定を継承 — 結果: ${effectiveEnabled ? '通知あり' : '通知なし'}`
  }
  if (mode === 'enabled') return `${context} の通知を受け取ります`
  return `${context} の通知を受け取りません`
}

function applySidebarNotifIndicator(el, show, title) {
  el.classList.remove('notif-indicator--enabled', 'notif-indicator--disabled', 'notif-indicator--inherit', 'notif-indicator--muted')
  if (show) {
    el.hidden = false
    el.classList.add('notif-indicator--muted')
    el.title = title || '通知: 無効'
  } else {
    el.hidden = true
    el.removeAttribute('title')
  }
}

function applyChannelNotifIndicator(el, channel) {
  const mode = getChannelNotificationMode(channel)
  const effective = isNotificationEnabled(channel.name)
  applySidebarNotifIndicator(el, !effective, notifModeTitle(mode, effective))
}

function applyGroupNotifIndicator(el, groupId) {
  const mode = getGroupNotificationMode(groupId)
  const effective = mode === 'enabled'
  applySidebarNotifIndicator(el, !effective, notifModeTitle(mode, effective))
}

function refreshNotifIndicators() {
  for (const row of document.querySelectorAll('.channel-row')) {
    const channel = channelsByName[row.dataset.channel]
    const indicator = row.querySelector('.notif-indicator')
    if (channel && indicator) applyChannelNotifIndicator(indicator, channel)
  }
  for (const section of document.querySelectorAll('.channel-group[data-group-id]')) {
    const indicator = section.querySelector('.channel-group-label-wrap .notif-indicator')
    if (indicator) applyGroupNotifIndicator(indicator, section.dataset.groupId)
  }
  // 通知設定はバッジ・ベルマークの件数にも効くため、設定を読み込んだ／変えた時点で数え直す（#241）
  updateDocumentTitle()
}

function setNotifSegmentValue(segmentEl, value) {
  if (!segmentEl) return
  for (const btn of segmentEl.querySelectorAll('.notif-mode-segment-btn')) {
    const active = btn.dataset.value === value
    btn.classList.toggle('active', active)
    btn.setAttribute('aria-pressed', String(active))
  }
}

function getNotifSegmentValue(segmentEl) {
  return segmentEl?.querySelector('.notif-mode-segment-btn.active')?.dataset.value ?? null
}

function setNotifSegmentDisabled(segmentEl, disabled) {
  if (!segmentEl) return
  for (const btn of segmentEl.querySelectorAll('.notif-mode-segment-btn')) {
    btn.disabled = disabled
  }
}

function renderNotifPreview(container, mode, effectiveEnabled, context) {
  if (!container) return
  container.innerHTML = `
    <div class="notif-mode-preview-card notif-mode-preview-card--${mode}">
      <span class="notif-mode-preview-icon" aria-hidden="true"></span>
      <div class="notif-mode-preview-text">
        <strong>${notifModePreviewTitle(mode)}</strong>
        <span>${notifModePreviewDetail(mode, effectiveEnabled, context)}</span>
      </div>
    </div>
  `
}

function channelNotificationSegmentValue(channel) {
  const mode = getChannelNotificationMode(channel)
  if (mode === 'inherit') return 'inherit'
  return mode === 'enabled' ? 'true' : 'false'
}

function groupNotificationSegmentValue(groupId) {
  return getGroupNotificationMode(groupId) === 'disabled' ? 'false' : 'true'
}

function updateChannelNotifSettingsUI(channel) {
  const segment = document.getElementById('channel-settings-notif-segment')
  const preview = document.getElementById('channel-settings-notif-preview')
  const inheritBtn = document.getElementById('channel-notif-inherit-btn')
  const hasGroup = Boolean(channel?.group_id)

  if (inheritBtn) {
    inheritBtn.hidden = !hasGroup
  }
  if (segment) {
    segment.classList.toggle('notif-mode-segment--3', hasGroup)
  }

  const value = channelNotificationSegmentValue(channel)
  setNotifSegmentValue(segment, value)

  const mode = value === 'inherit' ? 'inherit' : value === 'true' ? 'enabled' : 'disabled'
  const effective = isNotificationEnabled(channel.name)
  renderNotifPreview(preview, mode, effective, channel.name)
}

function updateGroupNotifSettingsUI(groupId) {
  const segment = document.getElementById('group-settings-notif-segment')
  const preview = document.getElementById('group-settings-notif-preview')
  const group = groupsById[groupId]
  const value = groupNotificationSegmentValue(groupId)

  setNotifSegmentValue(segment, value)

  const mode = value === 'true' ? 'enabled' : 'disabled'
  renderNotifPreview(preview, mode, mode === 'enabled', group?.name || 'グループ')
}

function isNotificationEnabled(channelName) {
  if (!channelName || !notificationPrefsReady) return false

  const channel = channelsByName[channelName]
  if (!channel?.id) return false

  const channelPref = notificationSettings.channels[channel.id]
  if (channelPref === false) return false
  if (channelPref === true) return true

  const groupId = channel.group_id
  if (groupId && notificationSettings.groups[groupId] === false) {
    return false
  }

  return true
}

// 通知設定は起動後に GET /api/notification-settings を読むまで分からない。
// isNotificationEnabled() は未読込を false として返すため、そのまま件数の判定に使うと
// 起動直後のバッジ・ベルの件数が一瞬 0 になる。読み込み前は「有効」として数え、
// 読み込み・設定変更のたびに refreshNotifIndicators() から数え直す（#241）。
function countsTowardUnreadBadge(channelName) {
  if (!notificationPrefsReady) return true
  return isNotificationEnabled(channelName)
}

function setupNotifSegment(segmentEl, onSelect) {
  if (!segmentEl || segmentEl.dataset.notifBound) return
  segmentEl.dataset.notifBound = '1'

  segmentEl.addEventListener('click', async (e) => {
    const btn = e.target.closest('.notif-mode-segment-btn')
    if (!btn || btn.disabled || btn.hidden) return
    if (btn.classList.contains('active')) return

    const prev = getNotifSegmentValue(segmentEl)
    setNotifSegmentValue(segmentEl, btn.dataset.value)
    setNotifSegmentDisabled(segmentEl, true)

    const ok = await onSelect(btn.dataset.value)
    if (!ok) setNotifSegmentValue(segmentEl, prev)

    setNotifSegmentDisabled(segmentEl, false)
  })
}

// ── Desktop / Push notification ─────────────────────────────────────────────

function urlBase64ToUint8Array(base64String) {
  const padding = '='.repeat((4 - (base64String.length % 4)) % 4)
  const base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/')
  const raw = atob(base64)
  const arr = new Uint8Array(raw.length)
  for (let i = 0; i < raw.length; i++) arr[i] = raw.charCodeAt(i)
  return arr
}

function pushSupported() {
  return 'serviceWorker' in navigator && 'PushManager' in window
}

function isPushDisabledByUser() {
  try {
    return localStorage.getItem(PUSH_DISABLED_KEY) === '1'
  } catch {
    return false
  }
}

function setPushDisabledByUser(disabled) {
  try {
    if (disabled) localStorage.setItem(PUSH_DISABLED_KEY, '1')
    else localStorage.removeItem(PUSH_DISABLED_KEY)
  } catch {
    // private モードなど
  }
}

async function getBrowserPushSubscription() {
  if (!pushSupported()) return null
  const reg = await navigator.serviceWorker.ready
  return reg.pushManager.getSubscription()
}

async function syncPushSubscription() {
  if (!('Notification' in window) || Notification.permission !== 'granted') {
    pushSubscribed = false
    return
  }
  if (!pushSupported()) {
    pushSubscribed = false
    return
  }

  if (isPushDisabledByUser()) {
    try {
      const sub = await getBrowserPushSubscription()
      if (sub) {
        const json = sub.toJSON()
        await authFetch(apiUrl('api/push/unsubscribe'), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ endpoint: json.endpoint, keys: json.keys }),
        }).catch(() => {})
        await sub.unsubscribe()
      }
    } catch {
      // 端末側の解除に失敗しても UI は無効のままにする
    }
    pushSubscribed = false
    return
  }

  try {
    const sub = await getBrowserPushSubscription()
    if (!sub) {
      pushSubscribed = false
      return
    }
    const json = sub.toJSON()
    const res = await authFetch(apiUrl('api/push/subscribe'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        endpoint: json.endpoint,
        keys: json.keys,
      }),
    })
    pushSubscribed = res.ok
  } catch {
    pushSubscribed = false
  }
}

async function parseHttpError(res, fallback) {
  try {
    const data = await res.json()
    return parseApiError(data, fallback)
  } catch {
    return fallback
  }
}

async function subscribePush(forceNew = false) {
  if (!pushSupported()) {
    return { ok: false, message: 'この端末は Push 非対応です' }
  }
  if (!('Notification' in window) || Notification.permission !== 'granted') {
    return { ok: false, message: '端末の通知が許可されていません' }
  }

  try {
    setPushDisabledByUser(false)
    await ensureServiceWorkerRegistered()
    const reg = await navigator.serviceWorker.ready

    const keyRes = await authFetch(apiUrl('api/push/vapid-public-key'))
    if (!keyRes.ok) {
      pushSubscribed = false
      return {
        ok: false,
        message: await parseHttpError(keyRes, 'Push 用の公開鍵を取得できませんでした'),
      }
    }
    const { publicKey } = await keyRes.json()
    if (!publicKey) {
      pushSubscribed = false
      return { ok: false, message: 'サーバーから公開鍵を取得できませんでした' }
    }

    let sub = await reg.pushManager.getSubscription()
    if (sub && forceNew) {
      await sub.unsubscribe()
      sub = await reg.pushManager.getSubscription()
      if (sub) {
        await sub.unsubscribe()
        sub = null
      }
    }
    if (!sub) {
      sub = await reg.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: urlBase64ToUint8Array(publicKey),
      })
    }

    const json = sub.toJSON()
    const res = await authFetch(apiUrl('api/push/subscribe'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        endpoint: json.endpoint,
        keys: json.keys,
      }),
    })

    if (!res.ok) {
      pushSubscribed = false
      return {
        ok: false,
        message: await parseHttpError(res, 'Push のサーバー登録に失敗しました'),
      }
    }

    pushSubscribed = true
    return { ok: true }
  } catch (err) {
    pushSubscribed = false
    const message = err?.name === 'NotAllowedError'
      ? 'Push の許可が拒否されました'
      : (err?.message || 'Push 登録に失敗しました')
    return { ok: false, message }
  }
}

async function unsubscribePush() {
  setPushDisabledByUser(true)
  if (!pushSupported()) {
    pushSubscribed = false
    return true
  }
  try {
    const sub = await getBrowserPushSubscription()
    if (sub) {
      const json = sub.toJSON()
      await authFetch(apiUrl('api/push/unsubscribe'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ endpoint: json.endpoint, keys: json.keys }),
      })
      await sub.unsubscribe()
    }
    pushSubscribed = false
    return true
  } catch {
    pushSubscribed = false
    return false
  }
}

function showDesktopNotification(entry) {
  if (!isNotificationEnabled(entry.channel)) return
  if (!('Notification' in window) || Notification.permission !== 'granted') return
  // バックグラウンドでは Web Push（Service Worker）に任せる
  if (document.hidden) return

  const title = entry.title || `# ${entry.channel}`
  const body = entry.message
  const icon = typeof APP_VERSION !== 'undefined'
    ? `icon-192.png?v=${APP_VERSION}`
    : 'icon-192.png'
  const options = {
    body,
    icon,
    tag: entry.id,
    data: {
      url: entry.channel ? `./?channel=${encodeURIComponent(entry.channel)}&src=push` : './',
      channel: entry.channel || '',
      id: entry.id || '',
      // 既読同期で「どこまで消してよいか」を判定するため通知自身の時刻を持たせる（#216）
      ts: entry.timestamp || '',
    },
  }

  const onClick = () => {
    window.focus()
    pendingHighlightId = entry.id
    if (activeChannel !== entry.channel) {
      void selectChannel(entry.channel)
    } else {
      highlightNotificationCard(entry.id)
    }
  }

  void (async () => {
    try {
      if ('serviceWorker' in navigator) {
        const reg = await navigator.serviceWorker.ready
        if (reg.showNotification) {
          await reg.showNotification(title, options)
          return
        }
      }
    } catch {
      // Service Worker 経由が失敗したら Notification API へ
    }
    try {
      const n = new Notification(title, options)
      n.onclick = () => {
        onClick()
        n.close()
      }
    } catch {
      // 非対応・拒否など
    }
  })()
}

const TEST_NOTIFICATION = {
  title: 'Signaly テスト通知',
  body: '通知の受信確認用です。このまま届いていれば OK です。',
  tag: 'signaly-test',
}

function testNotificationIcon() {
  return typeof APP_VERSION !== 'undefined'
    ? `icon-192.png?v=${APP_VERSION}`
    : 'icon-192.png'
}

async function showTestNotificationLocally() {
  const options = {
    body: TEST_NOTIFICATION.body,
    icon: testNotificationIcon(),
    tag: TEST_NOTIFICATION.tag,
  }
  try {
    if ('serviceWorker' in navigator) {
      const reg = await navigator.serviceWorker.ready
      if (reg.showNotification) {
        await reg.showNotification(TEST_NOTIFICATION.title, options)
        return true
      }
    }
  } catch {
    // Service Worker 経由が失敗したら Notification API へ
  }
  try {
    new Notification(TEST_NOTIFICATION.title, options)
    return true
  } catch {
    return false
  }
}

async function requestServerTestPush(subscriptionJson) {
  const res = await authFetch(apiUrl('api/push/test'), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      endpoint: subscriptionJson.endpoint,
      keys: subscriptionJson.keys,
    }),
  })
  if (res.ok) {
    return { ok: true }
  }
  const err = await res.json().catch(() => ({}))
  let detail = err.detail || 'バックグラウンド通知の送信に失敗しました'
  if (Array.isArray(detail)) {
    detail = detail.map((item) => item.msg || String(item)).join(' ')
  } else if (detail && typeof detail === 'object') {
    detail = detail.msg || JSON.stringify(detail)
  }
  if (typeof detail !== 'string') {
    detail = 'バックグラウンド通知の送信に失敗しました'
  }
  const needsReregister = res.status === 404 || res.status === 502
  if (res.status === 404) {
    pushSubscribed = false
    SignalySettings.updateSettingsBtnState()
  }
  return { ok: false, message: detail, needsReregister }
}

async function sendServerTestPush() {
  if (!pushSupported()) {
    return { ok: false, message: 'この端末は Push 非対応です' }
  }

  const reg = await navigator.serviceWorker.ready
  const subResult = await subscribePush(true)
  if (!subResult.ok) {
    return {
      ok: false,
      message: subResult.message || 'Push 登録に失敗しました。「有効」を試してください。',
      needsReregister: true,
    }
  }

  const sub = await reg.pushManager.getSubscription()
  if (!sub) {
    return { ok: false, message: 'Push 登録がありません。「有効」を試してください。', needsReregister: true }
  }

  const result = await requestServerTestPush(sub.toJSON())
  if (result.ok) {
    pushSubscribed = true
    SignalySettings.updateSettingsBtnState()
  }
  return result
}

async function sendTestNotification() {
  if (!('Notification' in window) || Notification.permission !== 'granted') {
    return { ok: false, message: '端末の通知が許可されていません。' }
  }

  const localOk = await showTestNotificationLocally()
  const shouldTryServer = pushSupported()
    && Notification.permission === 'granted'
    && !isPushDisabledByUser()
  let serverResult = null
  if (shouldTryServer) {
    try {
      serverResult = await sendServerTestPush()
      if (!serverResult.ok) {
        SignalySettings.renderNotifSettings?.()
      }
    } catch {
      serverResult = { ok: false, message: 'ネットワークエラー' }
    }
  }

  if (localOk && serverResult?.ok) {
    return {
      ok: true,
      mode: 'both',
      message: '通知を表示しました。バックグラウンドでも届くか、アプリを閉じて確認してください。',
    }
  }
  if (localOk) {
    if (shouldTryServer && serverResult && !serverResult.ok) {
      return {
        ok: true,
        mode: 'local',
        message: serverResult.message
          ? `通知は表示されました。${serverResult.message}`
          : '通知は表示されました。アプリを閉じた状態での通知は届いていない可能性があります。「再登録」を押してから、もう一度テストしてください。',
      }
    }
    return { ok: true, mode: 'local' }
  }
  if (serverResult?.ok) {
    return { ok: true, mode: 'push' }
  }
  return {
    ok: false,
    message: serverResult?.message || '通知の表示に失敗しました',
  }
}

SignalySettings.init({
  apiUrl,
  apiFetch: authFetch,
  closeSidebar,
  notifications: {
    isSupported: () => 'Notification' in window,
    pushSupported,
    getPushSubscribed: () => pushSubscribed,
    subscribePush,
    unsubscribePush,
    sendTest: sendTestNotification,
    onStateChange: () => SignalySettings.updateSettingsBtnState(),
  },
})

// ── Create group ──────────────────────────────────────────────────────────────

const addGroupBtn = document.getElementById('add-group-btn')
const createGroupDialog = document.getElementById('create-group-dialog')
const createGroupForm = document.getElementById('create-group-form')
const createGroupName = document.getElementById('create-group-name')
const createGroupError = document.getElementById('create-group-error')
const createGroupClose = document.getElementById('create-group-close')

function openCreateGroupDialog() {
  createGroupName.value = ''
  createGroupError.hidden = true
  createGroupError.textContent = ''
  SignalyDialog.open(createGroupDialog, { focusEl: createGroupName })
}

function closeCreateGroupDialog() {
  SignalyDialog.close(createGroupDialog)
}

addGroupBtn?.addEventListener('click', openCreateGroupDialog)
createGroupClose?.addEventListener('click', closeCreateGroupDialog)
createGroupDialog?.addEventListener('click', (e) => {
  if (e.target === createGroupDialog) closeCreateGroupDialog()
})

createGroupForm?.addEventListener('submit', async (e) => {
  e.preventDefault()
  const name = createGroupName.value.trim()
  if (!name) return

  createGroupError.hidden = true
  const submitBtn = createGroupForm.querySelector('.create-channel-submit')
  submitBtn.disabled = true

  try {
    const res = await authFetch(apiUrl('api/groups'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name }),
    })
    const data = await res.json().catch(() => ({}))
    if (!res.ok) {
      createGroupError.textContent = parseApiError(data, '作成に失敗しました')
      createGroupError.hidden = false
      return
    }
    closeCreateGroupDialog()
    await refreshChannels()
  } catch {
    createGroupError.textContent = 'ネットワークエラーが発生しました'
    createGroupError.hidden = false
  } finally {
    submitBtn.disabled = false
  }
})

// ── Create channel ────────────────────────────────────────────────────────────

let createChannelGroupId = null
const createChannelDialog = document.getElementById('create-channel-dialog')
const createChannelForm = document.getElementById('create-channel-form')
const createChannelName = document.getElementById('create-channel-name')
const createChannelError = document.getElementById('create-channel-error')
const createChannelClose = document.getElementById('create-channel-close')
const createChannelSuccess = document.getElementById('create-channel-success')
const createChannelTitle = document.getElementById('create-channel-title')
const createChannelSuccessName = document.getElementById('create-channel-success-name')
const createChannelWebhook = document.getElementById('create-channel-webhook')
const createChannelCopy = document.getElementById('create-channel-copy')
const createChannelDone = document.getElementById('create-channel-done')
const createChannelRevealWebhook = document.getElementById('create-channel-reveal-webhook')
const createChannelWebhookSection = document.getElementById('create-channel-webhook-section')

function hideWebhookSection(revealBtn, section, copyBtn = null, revealLabel = 'URL を表示') {
  if (section) section.hidden = true
  if (revealBtn) {
    revealBtn.hidden = false
    revealBtn.textContent = revealLabel
  }
  if (copyBtn) copyBtn.textContent = 'コピー'
}

function resetCreateChannelDialog() {
  createChannelForm.hidden = false
  createChannelSuccess.hidden = true
  createChannelTitle.textContent = 'チャンネルを作成'
  createChannelName.value = ''
  createChannelError.hidden = true
  createChannelError.textContent = ''
  createChannelGroupId = null
  hideWebhookSection(createChannelRevealWebhook, createChannelWebhookSection, createChannelCopy, 'URL をコピー')
}

function openCreateChannelDialog(groupId = null) {
  resetCreateChannelDialog()
  createChannelGroupId = groupId
  if (groupId && groupsById[groupId]) {
    createChannelTitle.textContent = `${groupsById[groupId].name} にチャンネルを作成`
  }
  SignalyDialog.open(createChannelDialog, { focusEl: createChannelName })
}

function closeCreateChannelDialog() {
  SignalyDialog.close(createChannelDialog)
}

createChannelClose?.addEventListener('click', closeCreateChannelDialog)

createChannelDone?.addEventListener('click', closeCreateChannelDialog)

createChannelDialog?.addEventListener('click', (e) => {
  if (e.target === createChannelDialog) closeCreateChannelDialog()
})

createChannelForm?.addEventListener('submit', async (e) => {
  e.preventDefault()
  const name = createChannelName.value.trim()
  if (!name) return

  createChannelError.hidden = true
  const submitBtn = createChannelForm.querySelector('.create-channel-submit')
  submitBtn.disabled = true

  try {
    const payload = { name }
    if (createChannelGroupId) payload.group_id = createChannelGroupId

    const res = await authFetch(apiUrl('api/channels'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    })
    const data = await res.json().catch(() => ({}))

    if (!res.ok) {
      let message = data.detail
      if (Array.isArray(message)) {
        message = message.map(e => e.msg).join(', ')
      } else if (typeof message !== 'string') {
        message = `作成に失敗しました (${res.status})`
      }
      createChannelError.textContent = message
      createChannelError.hidden = false
      return
    }

    createChannelForm.hidden = true
    createChannelSuccess.hidden = false
    createChannelTitle.textContent = 'チャンネルを作成しました'
    createChannelSuccessName.textContent = data.name
    createChannelWebhook.value = data.webhook_url
    hideWebhookSection(createChannelRevealWebhook, createChannelWebhookSection, createChannelCopy, 'URL をコピー')

    const listRes = await authFetch(apiUrl('api/channels'))
    if (listRes.ok) {
      const tree = await listRes.json()
      saveChannelTreeCache(tree)
      renderChannelTree(tree, data.name)
    }
  } catch {
    createChannelError.textContent = 'ネットワークエラーが発生しました'
    createChannelError.hidden = false
  } finally {
    submitBtn.disabled = false
  }
})

createChannelCopy?.addEventListener('click', async () => {
  try {
    await navigator.clipboard.writeText(createChannelWebhook.value)
    createChannelCopy.textContent = 'コピー済み'
    setTimeout(() => { createChannelCopy.textContent = 'コピー' }, 2000)
  } catch {
    createChannelWebhook.select()
    document.execCommand('copy')
  }
})

createChannelRevealWebhook?.addEventListener('click', async () => {
  createChannelWebhookSection.hidden = false
  createChannelRevealWebhook.hidden = true
  try {
    await navigator.clipboard.writeText(createChannelWebhook.value)
    createChannelCopy.textContent = 'コピー済み'
    setTimeout(() => { createChannelCopy.textContent = 'コピー' }, 2000)
  } catch {
    createChannelWebhook.select()
    document.execCommand('copy')
  }
})

function parseApiError(data, fallback) {
  let message = data?.detail
  if (Array.isArray(message)) {
    message = message.map(e => e.msg).join(', ')
  } else if (typeof message !== 'string') {
    message = fallback
  }
  return message
}

async function refreshChannels(selectName = null) {
  const res = await authFetch(apiUrl('api/channels'))
  if (!res.ok) return false
  const data = await res.json()
  saveChannelTreeCache(data)
  renderChannelTree(data, selectName)
  await loadNotificationSettings()
  return true
}

// ── Group settings ────────────────────────────────────────────────────────────

const groupSettingsDialog = document.getElementById('group-settings-dialog')
const groupSettingsClose = document.getElementById('group-settings-close')
const groupSettingsRename = document.getElementById('group-settings-rename')
const groupSettingsRenameBtn = document.getElementById('group-settings-rename-btn')
const groupSettingsError = document.getElementById('group-settings-error')
const groupSettingsDelete = document.getElementById('group-settings-delete')
const groupSettingsNotifSegment = document.getElementById('group-settings-notif-segment')
const groupDeleteDialog = document.getElementById('group-delete-dialog')
const groupDeleteName = document.getElementById('group-delete-name')
const groupDeleteError = document.getElementById('group-delete-error')
const groupDeleteCancel = document.getElementById('group-delete-cancel')
const groupDeleteConfirm = document.getElementById('group-delete-confirm')

let groupSettingsId = null
let groupSettingsOriginalName = null

function resetGroupSettingsDialog() {
  groupSettingsId = null
  groupSettingsOriginalName = null
  groupSettingsError.hidden = true
  groupSettingsError.textContent = ''
  groupSettingsRenameBtn.disabled = false
  groupSettingsDelete.disabled = false
  setNotifSegmentDisabled(groupSettingsNotifSegment, false)
}

function openGroupSettings(groupId) {
  const group = groupsById[groupId]
  if (!group) return

  groupSettingsId = groupId
  groupSettingsOriginalName = group.name
  groupSettingsRename.value = group.name
  updateGroupNotifSettingsUI(groupId)
  groupSettingsError.hidden = true
  closeSidebar()
  SignalyDialog.open(groupSettingsDialog, { focusEl: groupSettingsRename })
  groupSettingsRename?.select()
}

function closeGroupSettingsDialog() {
  SignalyDialog.close(groupSettingsDialog)
  resetGroupSettingsDialog()
}

groupSettingsClose?.addEventListener('click', closeGroupSettingsDialog)

groupSettingsDialog?.addEventListener('click', (e) => {
  if (e.target === groupSettingsDialog && !groupDeleteDialog?.classList.contains('open')) {
    closeGroupSettingsDialog()
  }
})

function openGroupDeleteDialog() {
  if (!groupSettingsOriginalName) return
  groupDeleteName.textContent = groupSettingsOriginalName
  groupDeleteError.hidden = true
  groupDeleteError.textContent = ''
  groupDeleteConfirm.disabled = false
  groupDeleteCancel.disabled = false
  SignalyDialog.open(groupDeleteDialog, { focusEl: groupDeleteCancel })
}

function closeGroupDeleteDialog() {
  SignalyDialog.close(groupDeleteDialog)
  groupDeleteError.hidden = true
  groupDeleteError.textContent = ''
  groupDeleteConfirm.disabled = false
  groupDeleteCancel.disabled = false
}

groupSettingsDelete?.addEventListener('click', openGroupDeleteDialog)
groupDeleteCancel?.addEventListener('click', closeGroupDeleteDialog)
groupDeleteDialog?.addEventListener('click', (e) => {
  if (e.target === groupDeleteDialog) closeGroupDeleteDialog()
})

groupSettingsRenameBtn?.addEventListener('click', async () => {
  const newName = groupSettingsRename.value.trim()
  if (!newName || !groupSettingsId) return
  if (newName === groupSettingsOriginalName) return

  groupSettingsError.hidden = true
  groupSettingsRenameBtn.disabled = true

  try {
    const res = await authFetch(apiUrl(`api/groups/${groupSettingsId}`), {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: newName }),
    })
    const data = await res.json().catch(() => ({}))
    if (!res.ok) {
      groupSettingsError.textContent = parseApiError(data, '変更に失敗しました')
      groupSettingsError.hidden = false
      return
    }

    closeGroupSettingsDialog()
    await refreshChannels(activeChannel)
  } catch {
    groupSettingsError.textContent = 'ネットワークエラーが発生しました'
    groupSettingsError.hidden = false
  } finally {
    groupSettingsRenameBtn.disabled = false
  }
})

groupSettingsNotifSegment && setupNotifSegment(groupSettingsNotifSegment, async (selected) => {
  if (!groupSettingsId) return false

  const enabled = selected === 'true'
  groupSettingsError.hidden = true

  try {
    const res = await authFetch(apiUrl(`api/groups/${groupSettingsId}/notification-setting`), {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled }),
    })
    if (!res.ok) {
      const data = await res.json().catch(() => ({}))
      groupSettingsError.textContent = parseApiError(data, '通知設定の保存に失敗しました')
      groupSettingsError.hidden = false
      return false
    }

    if (enabled) {
      delete notificationSettings.groups[groupSettingsId]
    } else {
      notificationSettings.groups[groupSettingsId] = false
    }
    notificationPrefsReady = true
    updateGroupNotifSettingsUI(groupSettingsId)
    refreshNotifIndicators()
    return true
  } catch {
    groupSettingsError.textContent = 'ネットワークエラーが発生しました'
    groupSettingsError.hidden = false
    return false
  }
})

groupDeleteConfirm?.addEventListener('click', () => {
  if (!groupSettingsId) return

  runConfirmAction({
    confirmBtn: groupDeleteConfirm,
    cancelBtn: groupDeleteCancel,
    errorEl: groupDeleteError,
    run: async (showError) => {
      const res = await authFetch(apiUrl(`api/groups/${groupSettingsId}`), { method: 'DELETE' })
      const data = await res.json().catch(() => ({}))
      if (!res.ok) {
        showError(parseApiError(data, '削除に失敗しました'))
        return
      }

      closeGroupDeleteDialog()
      closeGroupSettingsDialog()
      await refreshChannels(activeChannel)
    },
  })
})

document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && groupSettingsDialog?.classList.contains('open')) {
    if (groupDeleteDialog?.classList.contains('open')) {
      closeGroupDeleteDialog()
    } else {
      closeGroupSettingsDialog()
    }
  }
})

// ── Channel settings ──────────────────────────────────────────────────────────

const channelSettingsDialog = document.getElementById('channel-settings-dialog')
const channelSettingsClose = document.getElementById('channel-settings-close')
const channelSettingsRename = document.getElementById('channel-settings-rename')
const channelSettingsRenameBtn = document.getElementById('channel-settings-rename-btn')
const channelSettingsError = document.getElementById('channel-settings-error')
const channelSettingsWebhook = document.getElementById('channel-settings-webhook')
const channelSettingsCopy = document.getElementById('channel-settings-copy')
const channelSettingsRevealWebhook = document.getElementById('channel-settings-reveal-webhook')
const channelSettingsWebhookSection = document.getElementById('channel-settings-webhook-section')
const channelSettingsWebhookExample = document.getElementById('channel-settings-webhook-example')
const channelSettingsDelete = document.getElementById('channel-settings-delete')
const channelSettingsSelectDelete = document.getElementById('channel-settings-select-delete')
const channelSettingsNotifSegment = document.getElementById('channel-settings-notif-segment')
const channelMergeTarget = document.getElementById('channel-merge-target')
const channelMergeSource = document.getElementById('channel-merge-source')
const channelMergeBtn = document.getElementById('channel-merge-btn')
const channelMergeDialog = document.getElementById('channel-merge-dialog')
const channelMergeOriginName = document.getElementById('channel-merge-origin-name')
const channelMergeTargetName = document.getElementById('channel-merge-target-name')
const channelMergeLabelName = document.getElementById('channel-merge-label-name')
const channelMergeError = document.getElementById('channel-merge-error')
const channelMergeCancel = document.getElementById('channel-merge-cancel')
const channelMergeConfirm = document.getElementById('channel-merge-confirm')
const channelDeleteDialog = document.getElementById('channel-delete-dialog')
const channelDeleteName = document.getElementById('channel-delete-name')
const channelDeleteError = document.getElementById('channel-delete-error')
const channelDeleteCancel = document.getElementById('channel-delete-cancel')
const channelDeleteConfirm = document.getElementById('channel-delete-confirm')
const notifSelectBar = document.getElementById('notif-select-bar')
const notifSelectCount = document.getElementById('notif-select-count')
const notifSelectAll = document.getElementById('notif-select-all')
const notifSelectCancel = document.getElementById('notif-select-cancel')
const notifSelectDelete = document.getElementById('notif-select-delete')
const notifBulkDeleteDialog = document.getElementById('notif-bulk-delete-dialog')
const notifBulkDeleteCount = document.getElementById('notif-bulk-delete-count')
const notifBulkDeleteError = document.getElementById('notif-bulk-delete-error')
const notifBulkDeleteCancel = document.getElementById('notif-bulk-delete-cancel')
const notifBulkDeleteConfirm = document.getElementById('notif-bulk-delete-confirm')

let notifSelectMode = false
const selectedNotifIds = new Set()

let channelSettingsId = null
let channelSettingsOriginalName = null

function resetChannelSettingsDialog() {
  channelSettingsId = null
  channelSettingsOriginalName = null
  channelSettingsError.hidden = true
  channelSettingsError.textContent = ''
  hideWebhookSection(channelSettingsRevealWebhook, channelSettingsWebhookSection, channelSettingsCopy, 'URL をコピー')
  channelSettingsRenameBtn.disabled = false
  channelSettingsDelete.disabled = false
  channelSettingsSelectDelete.disabled = false
  setNotifSegmentDisabled(channelSettingsNotifSegment, false)
  if (channelMergeTarget) channelMergeTarget.innerHTML = ''
  if (channelMergeSource) channelMergeSource.value = ''
  if (channelMergeBtn) channelMergeBtn.disabled = true
}

// 統合先の候補（自分以外の全チャンネル）を並べる。
// 候補が無い＝チャンネルが1本しか無いときは、押しても何も起きないので無効のままにする。
function populateMergeTargets(channelName) {
  if (!channelMergeTarget) return
  channelMergeTarget.innerHTML = ''
  const others = Object.keys(channelsByName)
    .filter((name) => name !== channelName)
    .sort((a, b) => a.localeCompare(b, 'ja'))

  if (!others.length) {
    const option = document.createElement('option')
    option.textContent = '統合できるチャンネルがありません'
    option.value = ''
    channelMergeTarget.appendChild(option)
    channelMergeTarget.disabled = true
    if (channelMergeBtn) channelMergeBtn.disabled = true
    return
  }

  channelMergeTarget.disabled = false
  for (const name of others) {
    const option = document.createElement('option')
    option.value = channelsByName[name].id
    option.textContent = `# ${name}`
    channelMergeTarget.appendChild(option)
  }
  if (channelMergeBtn) channelMergeBtn.disabled = false
}

function openChannelSettings(channelName) {
  const channel = channelsByName[channelName]
  if (!channel) return

  channelSettingsId = channel.id
  channelSettingsOriginalName = channelName
  channelSettingsRename.value = channelName
  updateChannelNotifSettingsUI(channel)
  channelSettingsWebhook.value = channel.webhook_url || ''
  if (channelSettingsWebhookExample) {
    channelSettingsWebhookExample.textContent = `curl -X POST "${channel.webhook_url || ''}" \\
  -H "Content-Type: application/json" \\
  -d '{"content":"デプロイ完了","embeds":[{"title":"v1.2.3","description":"本番に反映しました","color":5763719,"fields":[{"name":"Branch","value":"main","inline":true}]}]}'`
  }
  channelSettingsError.hidden = true
  hideWebhookSection(channelSettingsRevealWebhook, channelSettingsWebhookSection, channelSettingsCopy, 'URL をコピー')
  populateMergeTargets(channelName)
  if (channelMergeSource) channelMergeSource.value = channelName
  closeSidebar()
  SignalyDialog.open(channelSettingsDialog)
}

function closeChannelSettingsDialog() {
  SignalyDialog.close(channelSettingsDialog)
  resetChannelSettingsDialog()
}

channelSettingsClose?.addEventListener('click', closeChannelSettingsDialog)

channelSettingsDialog?.addEventListener('click', (e) => {
  if (
    e.target === channelSettingsDialog
    && !channelDeleteDialog?.classList.contains('open')
    && !channelMergeDialog?.classList.contains('open')
  ) {
    closeChannelSettingsDialog()
  }
})

channelSettingsNotifSegment && setupNotifSegment(channelSettingsNotifSegment, async (selected) => {
  if (!channelSettingsId || !channelSettingsOriginalName) return false

  const enabled = selected === 'inherit' ? null : selected === 'true'
  channelSettingsError.hidden = true

  try {
    const res = await authFetch(apiUrl(`api/channels/${channelSettingsId}/notification-setting`), {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled }),
    })
    if (!res.ok) {
      const data = await res.json().catch(() => ({}))
      channelSettingsError.textContent = parseApiError(data, '通知設定の保存に失敗しました')
      channelSettingsError.hidden = false
      return false
    }

    if (enabled === null) {
      delete notificationSettings.channels[channelSettingsId]
    } else {
      notificationSettings.channels[channelSettingsId] = enabled
    }
    notificationPrefsReady = true

    const channel = channelsByName[channelSettingsOriginalName]
    if (channel) updateChannelNotifSettingsUI(channel)
    refreshNotifIndicators()
    return true
  } catch {
    channelSettingsError.textContent = 'ネットワークエラーが発生しました'
    channelSettingsError.hidden = false
    return false
  }
})

function openChannelDeleteDialog() {
  if (!channelSettingsOriginalName) return
  channelDeleteName.textContent = channelSettingsOriginalName
  channelDeleteError.hidden = true
  channelDeleteError.textContent = ''
  channelDeleteConfirm.disabled = false
  channelDeleteCancel.disabled = false
  SignalyDialog.open(channelDeleteDialog, { focusEl: channelDeleteCancel })
}

function closeChannelDeleteDialog() {
  SignalyDialog.close(channelDeleteDialog)
  channelDeleteError.hidden = true
  channelDeleteError.textContent = ''
  channelDeleteConfirm.disabled = false
  channelDeleteCancel.disabled = false
}

document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && channelDeleteDialog?.classList.contains('open')) {
    closeChannelDeleteDialog()
    return
  }
  if (e.key === 'Escape' && notifBulkDeleteDialog?.classList.contains('open')) {
    closeNotifBulkDeleteDialog()
    return
  }
  if (e.key === 'Escape' && channelSettingsDialog?.classList.contains('open')) {
    closeChannelSettingsDialog()
    return
  }
  if (e.key === 'Escape' && notifSelectMode) {
    exitNotifSelectMode()
  }
})

channelSettingsRevealWebhook?.addEventListener('click', async () => {
  channelSettingsWebhookSection.hidden = false
  channelSettingsRevealWebhook.hidden = true
  try {
    await navigator.clipboard.writeText(channelSettingsWebhook.value)
    channelSettingsCopy.textContent = 'コピー済み'
    setTimeout(() => { channelSettingsCopy.textContent = 'コピー' }, 2000)
  } catch {
    channelSettingsWebhook.select()
    document.execCommand('copy')
  }
})

channelSettingsCopy?.addEventListener('click', async () => {
  try {
    await navigator.clipboard.writeText(channelSettingsWebhook.value)
    channelSettingsCopy.textContent = 'コピー済み'
    setTimeout(() => { channelSettingsCopy.textContent = 'コピー' }, 2000)
  } catch {
    channelSettingsWebhook.select()
    document.execCommand('copy')
  }
})

channelSettingsRenameBtn?.addEventListener('click', async () => {
  const newName = channelSettingsRename.value.trim()
  if (!newName || !channelSettingsId) return
  if (newName === channelSettingsOriginalName) return

  channelSettingsError.hidden = true
  channelSettingsRenameBtn.disabled = true

  try {
    const res = await authFetch(apiUrl(`api/channels/${channelSettingsId}`), {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: newName }),
    })
    const data = await res.json().catch(() => ({}))
    if (!res.ok) {
      channelSettingsError.textContent = parseApiError(data, '変更に失敗しました')
      channelSettingsError.hidden = false
      return
    }

    const oldName = channelSettingsOriginalName
    if (unread[oldName]) {
      setChannelUnread(newName, (unread[newName] || 0) + unread[oldName])
      delete unread[oldName]
      saveUnread()
    }
    if (lastReadAt[oldName]) {
      lastReadAt[newName] = Math.max(lastReadAt[newName] || 0, lastReadAt[oldName])
      delete lastReadAt[oldName]
      saveLastReadAt()
    }

    channelSettingsOriginalName = newName
    channelSettingsId = data.id
    channelsByName[newName] = data

    const wasActive = activeChannel === oldName
    await refreshChannels(wasActive ? newName : activeChannel)
    closeChannelSettingsDialog()
  } catch {
    channelSettingsError.textContent = 'ネットワークエラーが発生しました'
    channelSettingsError.hidden = false
  } finally {
    channelSettingsRenameBtn.disabled = false
  }
})

channelSettingsDelete?.addEventListener('click', () => {
  if (!channelSettingsId || !channelSettingsOriginalName) return
  openChannelDeleteDialog()
})

// ── チャンネルの統合 ──────────────────────────────────────────────────────────

function mergeTargetName() {
  const id = channelMergeTarget?.value
  if (!id) return null
  const entry = Object.entries(channelsByName).find(([, channel]) => channel.id === id)
  return entry ? entry[0] : null
}

function openChannelMergeDialog() {
  const targetName = mergeTargetName()
  if (!channelSettingsOriginalName || !targetName) return
  channelMergeOriginName.textContent = `# ${channelSettingsOriginalName}`
  channelMergeTargetName.textContent = `# ${targetName}`
  channelMergeLabelName.textContent =
    channelMergeSource.value.trim() || channelSettingsOriginalName
  channelMergeError.hidden = true
  channelMergeError.textContent = ''
  channelMergeConfirm.disabled = false
  channelMergeCancel.disabled = false
  SignalyDialog.open(channelMergeDialog, { focusEl: channelMergeCancel })
}

function closeChannelMergeDialog() {
  SignalyDialog.close(channelMergeDialog)
  channelMergeError.hidden = true
  channelMergeError.textContent = ''
  channelMergeConfirm.disabled = false
  channelMergeCancel.disabled = false
}

channelMergeBtn?.addEventListener('click', openChannelMergeDialog)
channelMergeCancel?.addEventListener('click', closeChannelMergeDialog)

channelMergeDialog?.addEventListener('click', (e) => {
  if (e.target === channelMergeDialog) closeChannelMergeDialog()
})

channelMergeConfirm?.addEventListener('click', () => {
  const targetId = channelMergeTarget?.value
  const targetName = mergeTargetName()
  if (!channelSettingsId || !channelSettingsOriginalName || !targetId || !targetName) return

  runConfirmAction({
    confirmBtn: channelMergeConfirm,
    cancelBtn: channelMergeCancel,
    errorEl: channelMergeError,
    extraButtons: [channelMergeBtn, channelSettingsDelete, channelSettingsRenameBtn],
    run: async (showError) => {
      const source = channelMergeSource.value.trim()
      const res = await fetch(apiUrl(`api/channels/${channelSettingsId}/merge`), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ target_channel_id: targetId, source: source || null }),
      })
      const data = await res.json().catch(() => ({}))
      if (!res.ok) {
        showError(parseApiError(data, '統合に失敗しました'))
        return
      }

      // 統合元のチャンネルは消えるので、端末側に残る記録も片付ける。
      // 既読位置は統合先のものを残す（統合元のほうが古いと、統合先の既読分まで
      // 新着として出し直してしまうため）。
      const originName = channelSettingsOriginalName
      if (unread[originName]) {
        setChannelUnread(targetName, (unread[targetName] || 0) + unread[originName])
        delete unread[originName]
        saveUnread()
      }
      delete lastReadAt[originName]
      saveLastReadAt()
      setSourceFilter(originName, null)

      const wasOrigin = activeChannel === originName
      const wasTarget = activeChannel === targetName
      if (wasOrigin && eventSource) {
        eventSource.close()
        eventSource = null
      }
      closeChannelMergeDialog()
      closeChannelSettingsDialog()
      showToast(`# ${originName} を # ${targetName} へ統合しました（${data.moved}件）`)

      if (wasOrigin) {
        // 統合先へ切り替える（selectChannel は同じ名前だと何もしないため一度外す）
        activeChannel = null
        await refreshChannels(targetName)
        return
      }

      await refreshChannels(activeChannel)
      if (wasTarget) {
        // 表示中のチャンネルへ履歴が増えたので読み直す
        await reloadActiveFeed()
        await refreshChannelSources(targetName)
      }
    },
  })
})

channelDeleteCancel?.addEventListener('click', closeChannelDeleteDialog)

channelDeleteDialog?.addEventListener('click', (e) => {
  if (e.target === channelDeleteDialog) closeChannelDeleteDialog()
})

channelDeleteConfirm?.addEventListener('click', () => {
  if (!channelSettingsId || !channelSettingsOriginalName) return

  runConfirmAction({
    confirmBtn: channelDeleteConfirm,
    cancelBtn: channelDeleteCancel,
    errorEl: channelDeleteError,
    extraButtons: [channelSettingsDelete, channelSettingsRenameBtn],
    run: async (showError) => {
      const res = await authFetch(apiUrl(`api/channels/${channelSettingsId}`), {
        method: 'DELETE',
      })
      const data = await res.json().catch(() => ({}))
      if (!res.ok) {
        showError(parseApiError(data, '削除に失敗しました'))
        return
      }

      const deletedName = channelSettingsOriginalName
      delete unread[deletedName]
      saveUnread()
      delete lastReadAt[deletedName]
      saveLastReadAt()
      closeChannelDeleteDialog()
      closeChannelSettingsDialog()

      if (activeChannel === deletedName && eventSource) {
        eventSource.close()
        eventSource = null
      }

      await refreshChannels(activeChannel === deletedName ? null : activeChannel)
    },
  })
})

function updateNotifSelectBar() {
  notifSelectCount.textContent = `${selectedNotifIds.size}件選択中`
  notifSelectDelete.disabled = selectedNotifIds.size === 0
  const cardIds = [...feed.querySelectorAll('.notif-card')].map((c) => c.dataset.id)
  const allSelected = cardIds.length > 0 && cardIds.every((id) => selectedNotifIds.has(id))
  notifSelectAll.textContent = allSelected ? '選択を解除' : '全て選択'
}

function toggleNotifSelection(id, checked) {
  if (checked) selectedNotifIds.add(id)
  else selectedNotifIds.delete(id)
  const card = feed.querySelector(`.notif-card[data-id="${CSS.escape(id)}"]`)
  card?.classList.toggle('notif-card--selected', checked)
  updateNotifSelectBar()
}

// カードごとにリスナーを持たせず、feed に委譲した1つのリスナーで
// チェックボックスの変更・カードクリックの両方を処理する。
feed.addEventListener('change', (e) => {
  const checkbox = e.target.closest('.notif-card-checkbox')
  if (!checkbox) return
  const card = checkbox.closest('.notif-card')
  if (card) toggleNotifSelection(card.dataset.id, checkbox.checked)
})

feed.addEventListener('click', (e) => {
  if (e.target.closest('.notif-card-checkbox')) return
  if (!notifSelectMode) return
  const card = e.target.closest('.notif-card')
  if (!card) return
  const checkbox = card.querySelector('.notif-card-checkbox')
  if (!checkbox) return
  checkbox.checked = !checkbox.checked
  toggleNotifSelection(card.dataset.id, checkbox.checked)
})

// ── カードの左スワイプ ──────────────────────────────────────────────────────

const SWIPE_DELETE_THRESHOLD = 80

// cardSelector: 通知1件分の外枠（data-id を持つ要素）
// contentSelector: 実際にスライドさせる内側レイヤー（この裏に .swipe-action が見える）
function attachSwipeToDelete(container, cardSelector, contentSelector, onThresholdExceeded) {
  let swipe = null

  container.addEventListener('touchstart', (e) => {
    if (notifSelectMode) return
    const card = e.target.closest(cardSelector)
    if (!card) return
    const content = card.querySelector(contentSelector)
    if (!content) return
    const touch = e.touches[0]
    swipe = { card, content, id: card.dataset.id, startX: touch.clientX, startY: touch.clientY, dx: 0, dragging: false }
  }, { passive: true })

  container.addEventListener('touchmove', (e) => {
    if (!swipe) return
    const touch = e.touches[0]
    const dx = touch.clientX - swipe.startX
    const dy = touch.clientY - swipe.startY

    if (!swipe.dragging) {
      if (Math.abs(dx) < 10 && Math.abs(dy) < 10) return
      if (Math.abs(dy) > Math.abs(dx)) {
        swipe = null
        return
      }
      swipe.dragging = true
      swipe.content.classList.add('is-swiping')
    }

    e.preventDefault()
    swipe.dx = Math.min(0, dx)
    swipe.content.style.transform = `translateX(${swipe.dx}px)`
  }, { passive: false })

  container.addEventListener('touchend', () => {
    if (!swipe) return
    const { card, content, id, dx, dragging } = swipe
    swipe = null
    if (!dragging) return
    content.classList.remove('is-swiping')

    if (-dx > SWIPE_DELETE_THRESHOLD) {
      onThresholdExceeded(id, card, content)
    } else {
      content.style.transform = ''
    }
  })

  container.addEventListener('touchcancel', () => {
    if (swipe?.dragging) {
      swipe.content.classList.remove('is-swiping')
      swipe.content.style.transform = ''
    }
    swipe = null
  })
}

// #feed: 閾値を超えたら元の位置に戻し、確認ダイアログを開く（削除確定は既存のダイアログ操作）
attachSwipeToDelete(feed, '.notif-card', '.notif-card-content', (id, card, content) => {
  content.style.transform = ''
  deleteNotification(id)
})

// ベルマークの通知一覧: 確認なしで即削除（既存の削除ボタンと同じ経路）
notificationsListEl && attachSwipeToDelete(notificationsListEl, '.search-result', '.search-result-content', (id, card, content) => {
  content.style.transform = 'translateX(-100%)'
  void swipeDeleteNotification(id, content)
})

function enterNotifSelectMode() {
  if (notifSelectMode) return
  notifSelectMode = true
  selectedNotifIds.clear()
  feed.classList.add('notif-select-mode')
  updateNotifSelectBar()
  notifSelectBar.hidden = false
}

function exitNotifSelectMode() {
  if (!notifSelectMode) return
  notifSelectMode = false
  selectedNotifIds.clear()
  feed.classList.remove('notif-select-mode')
  feed.querySelectorAll('.notif-card-checkbox').forEach((cb) => { cb.checked = false })
  feed.querySelectorAll('.notif-card--selected').forEach((c) => c.classList.remove('notif-card--selected'))
  notifSelectBar.hidden = true
}

channelSettingsSelectDelete?.addEventListener('click', async () => {
  if (!channelSettingsId || !channelSettingsOriginalName) return
  const targetChannel = channelSettingsOriginalName
  closeChannelSettingsDialog()
  if (activeChannel !== targetChannel) {
    await selectChannel(targetChannel)
  }
  enterNotifSelectMode()
})

notifSelectAll?.addEventListener('click', () => {
  const cards = [...feed.querySelectorAll('.notif-card')]
  const allSelected = cards.length > 0 && cards.every((c) => selectedNotifIds.has(c.dataset.id))
  for (const card of cards) {
    const checkbox = card.querySelector('.notif-card-checkbox')
    if (checkbox) checkbox.checked = !allSelected
    card.classList.toggle('notif-card--selected', !allSelected)
    if (allSelected) selectedNotifIds.delete(card.dataset.id)
    else selectedNotifIds.add(card.dataset.id)
  }
  updateNotifSelectBar()
})

notifSelectCancel?.addEventListener('click', exitNotifSelectMode)

notifSelectDelete?.addEventListener('click', () => {
  if (selectedNotifIds.size === 0) return
  openNotifBulkDeleteDialog()
})

function openNotifBulkDeleteDialog() {
  notifBulkDeleteCount.textContent = String(selectedNotifIds.size)
  notifBulkDeleteError.hidden = true
  notifBulkDeleteError.textContent = ''
  notifBulkDeleteConfirm.disabled = false
  notifBulkDeleteCancel.disabled = false
  SignalyDialog.open(notifBulkDeleteDialog, { focusEl: notifBulkDeleteCancel })
}

function closeNotifBulkDeleteDialog() {
  SignalyDialog.close(notifBulkDeleteDialog)
  notifBulkDeleteError.hidden = true
  notifBulkDeleteError.textContent = ''
  notifBulkDeleteConfirm.disabled = false
  notifBulkDeleteCancel.disabled = false
}

notifBulkDeleteCancel?.addEventListener('click', closeNotifBulkDeleteDialog)

notifBulkDeleteDialog?.addEventListener('click', (e) => {
  if (e.target === notifBulkDeleteDialog) closeNotifBulkDeleteDialog()
})

notifBulkDeleteConfirm?.addEventListener('click', () => {
  const ids = [...selectedNotifIds]
  if (ids.length === 0) return

  runConfirmAction({
    confirmBtn: notifBulkDeleteConfirm,
    cancelBtn: notifBulkDeleteCancel,
    errorEl: notifBulkDeleteError,
    run: async (showError) => {
      const res = await deleteNotificationsRequest(ids)
      const data = await res.json().catch(() => ({}))
      if (!res.ok) {
        showError(parseApiError(data, '削除に失敗しました'))
        return
      }

      for (const id of ids) removeNotificationCard(id)
      closeNotifBulkDeleteDialog()
      exitNotifSelectMode()
      showToast(`${data.deleted}件削除しました`)
    },
  })
})

// ── Auth ──────────────────────────────────────────────────────────────────────
//
// ログインは Supabase Auth（Google）が行う。ここが持つのはログイン画面の出し入れと、
// 期限切れ・拒否を受けてログインへ戻す導線だけ。

const loginOverlay = document.getElementById('login-overlay')
const loginBtn = document.getElementById('login-btn')
const loginError = document.getElementById('login-error')

const FORBIDDEN_MESSAGE = 'このGoogleアカウントではログインできません'
const EXPIRED_MESSAGE = 'ログインの有効期限が切れました。もう一度ログインしてください'

// auth/callback.html が ?authError= を付けて戻してくる
const AUTH_ERROR_MESSAGES = {
  denied: 'ログインがキャンセルされました',
  failed: 'ログインに失敗しました。もう一度お試しください',
}

let loginOverlayShown = false

function showLoginError(message) {
  if (!loginError) return
  loginError.textContent = message || ''
  loginError.hidden = !message
}

function showLoginOverlay(message) {
  loginOverlayShown = true
  stopPolling()
  stopUnreadPolling()
  if (eventSource) {
    eventSource.close()
    eventSource = null
  }
  clearChannelTreeCache()
  channelList.innerHTML = ''
  hideFeedState()
  loginOverlay.classList.add('visible')
  showLoginError(message)
}

/**
 * ログインへ戻す。同時に走っている複数のリクエストが同じ 401 を返すため、
 * 最初の1回だけ反応する（あとから来た空文言で表示済みの理由を消さないため）。
 */
function showLoginOverlayOnce(message) {
  if (loginOverlayShown) return
  showLoginOverlay(message)
}

/** URL から ?authError= を取り出し、履歴に残さず消す。 */
function consumeAuthErrorParam() {
  const params = new URLSearchParams(window.location.search)
  const code = params.get('authError')
  if (!code) return ''
  params.delete('authError')
  const query = params.toString()
  const url = window.location.pathname + (query ? `?${query}` : '') + window.location.hash
  window.history.replaceState(null, '', url)
  return AUTH_ERROR_MESSAGES[code] || AUTH_ERROR_MESSAGES.failed
}

loginBtn?.addEventListener('click', async () => {
  loginBtn.disabled = true
  showLoginError('')
  try {
    // 成功時はそのまま Google の同意画面へ遷移するため、ここへは戻ってこない
    await SignalyAuth.signInWithGoogle()
  } catch {
    loginBtn.disabled = false
    showLoginError('ログインを開始できませんでした。通信環境を確認してください')
  }
})

// ── Service Worker（PWA 自動更新）────────────────────────────────────────────

let swUpdatePending = false
let swRefreshing = false
let swRegistrationPromise = null

function setupServiceWorkerAutoUpdate(registration) {
  registration.addEventListener('updatefound', () => {
    const worker = registration.installing
    if (!worker) return
    worker.addEventListener('statechange', () => {
      if (worker.state === 'installed' && navigator.serviceWorker.controller) {
        swUpdatePending = true
      }
    })
  })

  const checkForUpdates = () => {
    registration.update().catch(() => {})
  }

  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) checkForUpdates()
  })
  window.addEventListener('focus', checkForUpdates)
  setInterval(checkForUpdates, 60 * 60 * 1000)

  // 起動直後はチャンネル取得を優先し、SW 更新チェックは後回しにする
  setTimeout(checkForUpdates, 5000)
}

function ensureServiceWorkerRegistered() {
  if (!('serviceWorker' in navigator)) return Promise.resolve(null)
  if (!swRegistrationPromise) {
    swRegistrationPromise = registerServiceWorker()
  }
  return swRegistrationPromise
}

async function registerServiceWorker() {
  if (!('serviceWorker' in navigator)) return null

  navigator.serviceWorker.addEventListener('message', (event) => {
    const msg = event.data
    if (!msg) return
    if (msg.type === 'notification-click') {
      void handleNotificationNavigation(msg)
      return
    }
    if (msg.type === 'read-sync') {
      applyRemoteRead(msg.channels)
      return
    }
    if (msg.type === 'push-notification') {
      const data = msg.data || {}
      showDesktopNotification({
        id: data.id,
        channel: data.channel,
        title: data.title,
        message: data.body,
        timestamp: data.ts,
      })
    }
  })

  navigator.serviceWorker.addEventListener('controllerchange', () => {
    if (!swUpdatePending || swRefreshing) return
    swRefreshing = true
    location.reload()
  })

  try {
    const registration = await navigator.serviceWorker.register('/sw.js', {
      updateViaCache: 'none',
    })
    setupServiceWorkerAutoUpdate(registration)
    return registration
  } catch {
    // 未対応ブラウザなど
    return null
  }
}

// ── Init ──────────────────────────────────────────────────────────────────────

async function init() {

  clearChannelListRefreshHint()

  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) void updateAppBadge()
  })

  const authError = consumeAuthErrorParam()
  if (authError) {
    showLoginOverlay(authError)
    return
  }

  // SW 登録は初回 iOS PWA で遅くなりがちなので起動をブロックしない
  void ensureServiceWorkerRegistered()

  // トークンが更新されたら SSE 用 Cookie も貼り直す。ログアウト（他タブ含む）も拾う。
  void SignalyAuth.onAuthStateChange((session, event) => {
    if (event === 'SIGNED_OUT') showLoginOverlayOnce('ログアウトしました')
  }).catch(() => {})

  const cachedTree = loadChannelTreeCache()
  const startupChannel = resolveStartupChannel()
  if (cachedTree) {
    showAuthenticatedShell()
    renderChannelTree(cachedTree, startupChannel)
    updateAllBadges()
    await loadNotificationSettings()
    startUnreadPolling()
  } else {
    showChannelListLoading()
    showFeedLoading()
  }

  const controller = new AbortController()
  const timeout = setTimeout(() => controller.abort(), 8000)
  try {
    const res = await authFetch(apiUrl('api/channels'), { signal: controller.signal })
    clearTimeout(timeout)
    if (res.status === 401 || res.status === 403) {
      // authFetch がログイン画面を出しているので、ここでは起動処理を止めるだけ
      return
    }
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    const data = await res.json()
    clearChannelListRefreshHint()
    saveChannelTreeCache(data)
    showAuthenticatedShell()
    renderChannelTree(data, startupChannel)
    clearPushDeepLinkMarker()
    await loadNotificationSettings()
    startUnreadPolling()
    void syncPushSubscription().catch(() => {
      pushSubscribed = false
    }).finally(() => {
      SignalySettings.updateSettingsBtnState()
    })
  } catch (err) {
    clearTimeout(timeout)
    if (cachedTree) {
      const msg = err.name === 'AbortError' ? 'タイムアウト' : err.message
      showChannelListRefreshHint(msg, init)
      startUnreadPolling()
      void loadNotificationSettings()
      return
    }
    const msg = err.name === 'AbortError' ? 'タイムアウト' : err.message
    showChannelListError(msg, init)
    showFeedError('チャンネル一覧の読み込みに失敗しました', init)
  }
}

feed?.addEventListener('scroll', () => {
  updateStickyFeedDate()
  if (feed.scrollTop <= 40 && pendingNewCount > 0) {
    dismissNewHighlights()
  } else {
    updateNewNotifBanner()
  }
})

newNotifBanner?.addEventListener('click', () => {
  feed.scrollTo({ top: 0, behavior: 'smooth' })
  dismissNewHighlights()
})

init()
