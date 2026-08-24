'use strict'

const THEME_KEY = 'signaly-theme'

const SignalySettings = {
  apiUrl: (path) => '/' + String(path).replace(/^\//, ''),
  // 既定は素の fetch だが、app.js から Authorization 付き・401 検知つきのものを注入する
  apiFetch: (url, options) => SignalyAuth.fetch(url, options),
  closeSidebar: () => {},
  notifications: null,

  init(options = {}) {
    if (options.apiUrl) this.apiUrl = options.apiUrl
    if (options.apiFetch) this.apiFetch = options.apiFetch
    if (options.closeSidebar) this.closeSidebar = options.closeSidebar
    if (options.notifications) this.notifications = options.notifications

    this.setupTheme()
    this.setupDialog()
    this.setupChangelog()
    this.renderChangelog()
    this.setupApiKeySection()
  },

  showAuthenticated() {
    const btn = document.getElementById('settings-btn')
    if (btn) btn.hidden = false
  },

  updateSettingsBtnState() {
    const btn = document.getElementById('settings-btn')
    if (!btn) return
    const notif = this.notifications
    if (notif?.isSupported?.() && Notification.permission === 'granted') {
      btn.classList.add('notif-active')
    } else {
      btn.classList.remove('notif-active')
    }
  },

  // ── Theme ──────────────────────────────────────────────────────────────────

  setupTheme() {
    const saved = localStorage.getItem(THEME_KEY) || 'system'
    this.applyTheme(saved)

    window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
      if (this.getTheme() === 'system') this.applyTheme('system')
    })

    document.querySelectorAll('.theme-segment-btn').forEach((btn) => {
      btn.addEventListener('click', () => {
        this.applyTheme(btn.dataset.theme)
      })
    })
  },

  getTheme() {
    return localStorage.getItem(THEME_KEY) || 'system'
  },

  applyTheme(theme) {
    document.documentElement.setAttribute('data-theme', theme)
    localStorage.setItem(THEME_KEY, theme)
    document.querySelectorAll('.theme-segment-btn').forEach((btn) => {
      const active = btn.dataset.theme === theme
      btn.classList.toggle('active', active)
      btn.setAttribute('aria-checked', active ? 'true' : 'false')
    })
  },

  // ── Settings dialog ────────────────────────────────────────────────────────

  setupDialog() {
    const settingsBtn = document.getElementById('settings-btn')
    const settingsDialog = document.getElementById('settings-dialog')
    const settingsClose = document.getElementById('settings-close')
    const logoutBtn = document.getElementById('settings-logout-btn')
    const versionText = document.getElementById('settings-version-text')
    const changelogBtn = document.getElementById('settings-changelog-btn')
    const reloadBtn = document.getElementById('settings-reload-btn')
    const changelogOverlay = document.getElementById('settings-changelog-overlay')
    const changelogClose = document.getElementById('settings-changelog-close')

    settingsBtn?.addEventListener('click', (e) => {
      e.preventDefault()
      e.stopPropagation()
      this.openSettingsDialog()
    })

    settingsClose?.addEventListener('click', () => this.closeSettingsDialog())

    settingsDialog?.addEventListener('click', (e) => {
      if (e.target === settingsDialog) this.closeSettingsDialog()
    })

    logoutBtn?.addEventListener('click', async () => {
      logoutBtn.disabled = true
      // Supabase 側のセッション破棄と、SSE 用 Cookie の削除の両方を行う
      await SignalyAuth.signOut()
      location.reload()
    })

    changelogBtn?.addEventListener('click', () => this.openSettingsChangelog())
    reloadBtn?.addEventListener('click', () => location.reload())
    changelogClose?.addEventListener('click', () => this.closeSettingsChangelog())
    changelogOverlay?.addEventListener('click', (e) => {
      if (e.target === changelogOverlay) this.closeSettingsChangelog()
    })

    if (typeof APP_VERSION !== 'undefined' && versionText) {
      versionText.textContent = `v${APP_VERSION}`
    }

    this.setupNotifSection()

    document.addEventListener('keydown', (e) => {
      if (e.key !== 'Escape') return
      if (changelogOverlay && !changelogOverlay.hidden) {
        this.closeSettingsChangelog()
        return
      }
      if (settingsDialog?.classList.contains('open')) {
        this.closeSettingsDialog()
      }
    })
  },

  openSettingsChangelog() {
    const overlay = document.getElementById('settings-changelog-overlay')
    if (!overlay) return
    overlay.hidden = false
  },

  closeSettingsChangelog() {
    const overlay = document.getElementById('settings-changelog-overlay')
    if (!overlay) return
    overlay.hidden = true
  },

  openSettingsDialog() {
    const dialog = document.getElementById('settings-dialog')
    if (!dialog) return
    this.closeSidebar()
    this.closeSettingsChangelog()
    this.renderNotifSettings()
    this.loadApiKeys()
    SignalyDialog.open(dialog)
    const body = dialog.querySelector('.settings-body')
    if (body) body.scrollTop = 0
  },

  closeSettingsDialog() {
    this.closeSettingsChangelog()
    SignalyDialog.close(document.getElementById('settings-dialog'))
  },

  // ── Notification settings ──────────────────────────────────────────────────

  setNotifActionBtn(btn, { icon, label }) {
    if (!btn) return
    const iconEl = btn.querySelector('.notif-btn-icon')
    const labelEl = btn.querySelector('.notif-btn-label')
    if (iconEl) iconEl.className = `notif-btn-icon notif-btn-icon--${icon}`
    if (labelEl) labelEl.textContent = label
  },

  setupNotifSection() {
    const section = document.getElementById('settings-notif-section')
    if (!section) return

    if (!this.notifications?.isSupported?.()) {
      section.hidden = true
      return
    }

    document.getElementById('notif-enable-btn')?.addEventListener('click', async () => {
      const btn = document.getElementById('notif-enable-btn')
      const resultEl = document.getElementById('notif-test-result')
      btn.disabled = true
      if (resultEl) resultEl.hidden = true
      try {
        await Notification.requestPermission()
        let result = { ok: false }
        if (Notification.permission === 'granted') {
          result = await this.notifications.subscribePush(true)
        }
        this.updateSettingsBtnState()
        this.renderNotifSettings()
        this.notifications.onStateChange?.()
        if (resultEl && Notification.permission === 'granted') {
          if (result?.ok) {
            resultEl.textContent = 'バックグラウンド通知を有効にしました。'
            resultEl.className = 'notif-test-result notif-test-result--ok'
            resultEl.hidden = false
          } else if (result?.message) {
            resultEl.textContent = result.message
            resultEl.className = 'notif-test-result notif-test-result--error'
            resultEl.hidden = false
          }
        }
      } finally {
        btn.disabled = false
      }
    })

    document.getElementById('notif-reregister-btn')?.addEventListener('click', async () => {
      const btn = document.getElementById('notif-reregister-btn')
      const resultEl = document.getElementById('notif-test-result')
      btn.disabled = true
      if (resultEl) resultEl.hidden = true
      try {
        const result = await this.notifications.subscribePush(true)
        this.updateSettingsBtnState()
        this.renderNotifSettings()
        this.notifications.onStateChange?.()
        if (resultEl) {
          if (result?.ok) {
            resultEl.textContent = 'バックグラウンド通知を有効にしました。'
            resultEl.className = 'notif-test-result notif-test-result--ok'
            resultEl.hidden = false
          } else if (result?.message) {
            resultEl.textContent = result.message
            resultEl.className = 'notif-test-result notif-test-result--error'
            resultEl.hidden = false
          }
        }
      } finally {
        btn.disabled = false
      }
    })

    document.getElementById('notif-disable-btn')?.addEventListener('click', async () => {
      const btn = document.getElementById('notif-disable-btn')
      btn.disabled = true
      try {
        await this.notifications.unsubscribePush()
        this.updateSettingsBtnState()
        this.renderNotifSettings()
        this.notifications.onStateChange?.()
      } finally {
        btn.disabled = false
      }
    })

    document.getElementById('notif-test-btn')?.addEventListener('click', async () => {
      const btn = document.getElementById('notif-test-btn')
      const resultEl = document.getElementById('notif-test-result')
      if (!btn || !resultEl) return

      btn.disabled = true
      resultEl.hidden = true
      try {
        const result = await this.notifications.sendTest?.()
        if (result?.ok) {
          resultEl.textContent = result.message || (
            result.mode === 'push'
              ? 'テスト通知を送信しました。端末に届くか確認してください。'
              : 'テスト通知を表示しました。見えていれば OK です。'
          )
          resultEl.className = result.mode === 'local' && result.message
            ? 'notif-test-result notif-test-result--warn'
            : 'notif-test-result notif-test-result--ok'
        } else {
          resultEl.textContent = result?.message || 'テストに失敗しました'
          resultEl.className = 'notif-test-result notif-test-result--error'
        }
        resultEl.hidden = false
      } finally {
        btn.disabled = false
      }
    })
  },

  detectMobilePlatform() {
    const ua = navigator.userAgent
    if (/iPhone|iPad|iPod/.test(ua)) return 'ios'
    if (/Android/.test(ua)) return 'android'
    return 'desktop'
  },

  osSettingsHintText() {
    const platform = this.detectMobilePlatform()
    if (platform === 'ios') {
      return '設定 → 通知 → Signaly で「通知を許可」をオンにしてください。ホーム画面に追加した PWA から開いている必要があります。'
    }
    if (platform === 'android') {
      return '設定 → アプリ → Signaly → 通知 で通知をオンにしてください。'
    }
    return 'ブラウザのアドレスバー左のアイコン → サイトの設定 → 通知 で許可してください。'
  },

  renderNotifSettings() {
    const section = document.getElementById('settings-notif-section')
    if (!section || section.hidden) return

    const permEl = document.getElementById('notif-status-permission')
    const pushEl = document.getElementById('notif-status-push')
    const messageEl = document.getElementById('notif-settings-message')
    const enableBtn = document.getElementById('notif-enable-btn')
    const pushBlock = document.getElementById('notif-actions')
    const reregisterBtn = document.getElementById('notif-reregister-btn')
    const disableBtn = document.getElementById('notif-disable-btn')
    const testBtn = document.getElementById('notif-test-btn')
    const testResult = document.getElementById('notif-test-result')
    const osHint = document.getElementById('notif-os-hint')
    const osHintText = document.getElementById('notif-os-hint-text')
  if (!permEl || !pushEl || !messageEl || !enableBtn || !pushBlock || !reregisterBtn || !disableBtn || !testBtn || !testResult || !osHint || !osHintText) return

    const permission = Notification.permission
    const canPush = this.notifications?.pushSupported?.() ?? false
    const pushSubscribed = this.notifications?.getPushSubscribed?.() ?? false

    permEl.textContent = permission === 'granted' ? '許可済み'
      : permission === 'denied' ? 'ブロック中' : '未設定'
    permEl.className = permission === 'granted' ? 'notif-status--ok'
      : permission === 'denied' ? 'notif-status--bad' : 'notif-status--warn'

    if (!canPush) {
      pushEl.textContent = '非対応'
      pushEl.className = 'notif-status--bad'
    } else if (pushSubscribed) {
      pushEl.textContent = '有効（バックグラウンド対応）'
      pushEl.className = 'notif-status--ok'
    } else if (permission === 'granted') {
      pushEl.textContent = '未登録'
      pushEl.className = 'notif-status--warn'
    } else {
      pushEl.textContent = '—'
      pushEl.className = ''
    }

    enableBtn.hidden = permission !== 'default'
    if (permission === 'granted' && canPush) {
      reregisterBtn.hidden = false
      this.setNotifActionBtn(reregisterBtn, {
        icon: pushSubscribed ? 'refresh' : 'on',
        label: pushSubscribed ? '再登録' : '有効',
      })
      disableBtn.hidden = !pushSubscribed
      this.setNotifActionBtn(disableBtn, { icon: 'off', label: '無効' })
    } else {
      reregisterBtn.hidden = true
      disableBtn.hidden = true
    }

    testBtn.hidden = permission !== 'granted'
    pushBlock.hidden = reregisterBtn.hidden && disableBtn.hidden && testBtn.hidden
    osHint.hidden = permission !== 'denied'
    osHintText.textContent = this.osSettingsHintText()
    testResult.hidden = true

    if (permission === 'granted' && pushSubscribed) {
      messageEl.textContent = 'アプリを閉じていても通知が届きます。届かない場合は再登録してください。'
    } else if (permission === 'granted') {
      messageEl.textContent = '端末の通知は許可されています。Push を登録するとバックグラウンドでも届きます。'
    } else if (permission === 'denied') {
      messageEl.textContent = '通知がブロックされています。下の手順で端末の設定から許可してください。'
    } else {
      messageEl.textContent = '「通知を許可する」を押すと、端末の確認画面が開きます。'
    }
  },

  // ── API キー ───────────────────────────────────────────────────────────────

  setupApiKeySection() {
    const form = document.getElementById('api-key-form')
    const nameInput = document.getElementById('api-key-name')
    const errorEl = document.getElementById('api-key-error')
    const createdEl = document.getElementById('api-key-created')
    const createdValueEl = document.getElementById('api-key-created-value')
    const createdCopyBtn = document.getElementById('api-key-created-copy')

    form?.addEventListener('submit', async (e) => {
      e.preventDefault()
      const name = nameInput?.value.trim()
      if (!name) return

      if (errorEl) errorEl.hidden = true
      const submitBtn = form.querySelector('button[type="submit"]')
      submitBtn.disabled = true

      try {
        const res = await this.apiFetch(this.apiUrl('api/keys'), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name }),
        })
        const data = await res.json().catch(() => ({}))
        if (!res.ok) {
          if (errorEl) {
            errorEl.textContent = data.detail || '作成に失敗しました'
            errorEl.hidden = false
          }
          return
        }
        nameInput.value = ''
        if (createdEl && createdValueEl) {
          createdEl.hidden = false
          createdValueEl.value = data.key
        }
        this.loadApiKeys()
      } catch {
        if (errorEl) {
          errorEl.textContent = 'ネットワークエラーが発生しました'
          errorEl.hidden = false
        }
      } finally {
        submitBtn.disabled = false
      }
    })

    createdCopyBtn?.addEventListener('click', async () => {
      try {
        await navigator.clipboard.writeText(createdValueEl.value)
        createdCopyBtn.textContent = 'コピー済み'
        setTimeout(() => { createdCopyBtn.textContent = 'コピー' }, 2000)
      } catch {
        createdValueEl.select()
        document.execCommand('copy')
      }
    })
  },

  async loadApiKeys() {
    const list = document.getElementById('api-key-list')
    if (!list) return
    try {
      const res = await this.apiFetch(this.apiUrl('api/keys'))
      if (!res.ok) return
      const { keys } = await res.json()
      list.innerHTML = ''
      if (!keys.length) {
        list.innerHTML = '<li class="api-key-empty">API キーはまだありません</li>'
        return
      }
      for (const key of keys) {
        const li = document.createElement('li')
        li.className = 'api-key-item'

        const nameEl = document.createElement('span')
        nameEl.className = 'api-key-item-name'
        nameEl.textContent = key.name

        const prefixEl = document.createElement('span')
        prefixEl.className = 'api-key-item-prefix'
        prefixEl.textContent = `${key.key_prefix}…`

        const deleteBtn = document.createElement('button')
        deleteBtn.type = 'button'
        deleteBtn.className = 'api-key-delete'
        deleteBtn.dataset.id = key.id
        deleteBtn.title = '削除'
        deleteBtn.textContent = '削除'
        deleteBtn.addEventListener('click', async () => {
          if (!confirm(`「${key.name}」を削除しますか？`)) return
          await this.apiFetch(this.apiUrl(`api/keys/${key.id}`), { method: 'DELETE' })
          this.loadApiKeys()
        })

        li.appendChild(nameEl)
        li.appendChild(prefixEl)
        li.appendChild(deleteBtn)
        list.appendChild(li)
      }
    } catch {
      // サイレントに無視
    }
  },

  // ── Changelog ──────────────────────────────────────────────────────────────

  setupChangelog() {
    const changelogDialog = document.getElementById('changelog-dialog')
    const changelogClose = document.getElementById('changelog-close')

    changelogClose?.addEventListener('click', () => {
      changelogDialog?.classList.remove('open')
    })

    changelogDialog?.addEventListener('click', (e) => {
      if (e.target === changelogDialog) changelogDialog.classList.remove('open')
    })
  },

  renderChangelog() {
    const entries = typeof APP_CHANGELOG !== 'undefined' ? APP_CHANGELOG : []
    for (const listId of ['changelog-list', 'settings-changelog-list']) {
      const list = document.getElementById(listId)
      if (!list) continue
      list.innerHTML = ''
      for (const entry of entries) {
        const section = document.createElement('div')
        section.className = 'cl-entry'

        const heading = document.createElement('div')
        heading.className = 'cl-heading'
        const ver = document.createElement('span')
        ver.className = 'cl-version'
        ver.textContent = `v${entry.version}`
        const date = document.createElement('span')
        date.className = 'cl-date'
        date.textContent = entry.date || ''
        heading.appendChild(ver)
        heading.appendChild(date)

        const ul = document.createElement('ul')
        ul.className = 'cl-changes'
        for (const change of entry.changes) {
          const li = document.createElement('li')
          li.textContent = change
          ul.appendChild(li)
        }

        section.appendChild(heading)
        section.appendChild(ul)
        list.appendChild(section)
      }
    }
  },
}
