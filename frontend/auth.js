'use strict'

// Supabase Auth（Google ログイン）のフロントエンド側。
//
// このリポジトリにはビルドもバンドラも無いため、supabase-js は esm.sh から動的 import する。
// 接続先（URL・publishable key）はリポジトリへ埋め込まず /api/auth/config から受け取る。
//
// index.html と auth/callback.html の両方から読み込まれ、ページによって相対パスの基準が
// 変わる。このスクリプト自身の URL（常に {アプリルート}/auth.js）を基準にすることで、
// どちらから読まれても同じ場所へ解決できるようにしている。

const SignalyAuth = (() => {
  const SUPABASE_JS_URL = 'https://esm.sh/@supabase/supabase-js@2.111.0'

  const SCRIPT_URL = document.currentScript
    ? document.currentScript.src
    : new URL('auth.js', window.location.href).href
  const ROOT_URL = new URL('.', SCRIPT_URL).href

  let clientPromise = null
  let sessionCookiePromise = null
  let lastCookieToken = null

  function appUrl(path) {
    return new URL(path.replace(/^\//, ''), ROOT_URL).toString()
  }

  async function loadClient() {
    const res = await fetch(appUrl('api/auth/config'))
    if (!res.ok) throw new Error(`Supabase の設定を取得できませんでした (HTTP ${res.status})`)
    const { supabaseUrl, supabasePublishableKey } = await res.json()
    const { createClient } = await import(SUPABASE_JS_URL)
    // Google ログインが implicit flow（#access_token）で返るプロジェクトと
    // PKCE（?code）で返るプロジェクトがあるため、URL の自動検出は既定の有効のままにする。
    // callback.html 側は ?code が付いていたときだけ明示的に交換する。
    return createClient(supabaseUrl, supabasePublishableKey)
  }

  function getClient() {
    if (!clientPromise) {
      clientPromise = loadClient().catch((err) => {
        // 失敗を握ったままにすると以後ずっと同じ失敗を返し続けるため、次回は取り直す
        clientPromise = null
        throw err
      })
    }
    return clientPromise
  }

  async function getSession() {
    try {
      const supabase = await getClient()
      const { data } = await supabase.auth.getSession()
      return data.session || null
    } catch {
      return null
    }
  }

  async function getAccessToken() {
    const session = await getSession()
    return session ? session.access_token : null
  }

  async function authHeaders() {
    const token = await getAccessToken()
    return token ? { Authorization: `Bearer ${token}` } : {}
  }

  /** Authorization ヘッダーを付けて fetch する。API を叩くときは必ずこれを通す。 */
  async function fetchWithAuth(url, options = {}) {
    const headers = { ...(await authHeaders()), ...(options.headers || {}) }
    return fetch(url, { ...options, headers })
  }

  /**
   * SSE 用のセッション Cookie を発行する。
   * EventSource は Authorization ヘッダーを付けられないため、接続前に一度だけ通す。
   * 同じトークンで貼り直す意味は無いので、トークンが変わったときだけ叩く。
   */
  function ensureSessionCookie() {
    return getAccessToken().then((token) => {
      if (!token) return false
      if (token === lastCookieToken && sessionCookiePromise) return sessionCookiePromise
      lastCookieToken = token
      sessionCookiePromise = fetch(appUrl('auth/session'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ access_token: token }),
      })
        .then((res) => res.ok)
        .catch(() => false)
      return sessionCookiePromise
    })
  }

  async function signInWithGoogle() {
    const supabase = await getClient()
    const redirectTo = appUrl('auth/callback')
    const { error } = await supabase.auth.signInWithOAuth({
      provider: 'google',
      options: { redirectTo },
    })
    if (error) throw error
  }

  async function signOut() {
    lastCookieToken = null
    sessionCookiePromise = null
    try {
      const supabase = await getClient()
      await supabase.auth.signOut()
    } catch {
      // Supabase 側が落ちていてもローカルのログアウトは進める
    }
    await fetch(appUrl('auth/logout'), { method: 'POST' }).catch(() => {})
  }

  async function exchangeCodeForSession(code) {
    const supabase = await getClient()
    return supabase.auth.exchangeCodeForSession(code)
  }

  /** トークンが更新されたら SSE 用 Cookie も貼り直す。 */
  async function onAuthStateChange(callback) {
    const supabase = await getClient()
    const { data } = supabase.auth.onAuthStateChange((event, session) => {
      if (event === 'TOKEN_REFRESHED' || event === 'SIGNED_IN') {
        void ensureSessionCookie()
      }
      if (callback) callback(session, event)
    })
    return data.subscription
  }

  // 起動直後に読み込みを始めておく。init() が最初の API を叩くころには import が済んでいる。
  void getClient().catch(() => {})

  return {
    appUrl,
    getSession,
    getAccessToken,
    authHeaders,
    fetch: fetchWithAuth,
    ensureSessionCookie,
    signInWithGoogle,
    signOut,
    exchangeCodeForSession,
    onAuthStateChange,
  }
})()
