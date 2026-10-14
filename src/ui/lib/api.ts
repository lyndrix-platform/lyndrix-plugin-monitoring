const PLUGIN_ID_PREFIX = 'lyndrix.plugin.state_monitoring'
const TOKEN_KEY = 'lyndrix_token'

function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY)
}

const REQUEST_TIMEOUT_MS = 30_000

/** Runtime status the core sends with a 503 while a plugin is warming up
 *  (header X-Plugin-Runtime, see core ctx.set_status). */
export interface PluginRuntime {
  phase: 'starting' | 'ready' | 'degraded' | 'maintenance'
  message: string | null
  progress: { done: number; total: number } | null
  blocking: boolean
}

export class ApiError extends Error {
  code: 'timeout' | 'runtime' | 'http' | 'network'
  status?: number
  runtime?: PluginRuntime
  constructor(message: string, code: ApiError['code'], status?: number, runtime?: PluginRuntime) {
    super(message)
    this.name = 'ApiError'
    this.code = code
    this.status = status
    this.runtime = runtime
  }
}

function navigate(path: string): void {
  // Use the History API so we stay inside the SPA shell instead of forcing a
  // full document reload via window.location.
  window.history.pushState({}, '', path)
  window.dispatchEvent(new PopStateEvent('popstate'))
}

async function apiFetch<T>(path: string, init: RequestInit = {}): Promise<T> {
  const token = getToken()
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
    ...(init.headers as Record<string, string> | undefined),
  }

  // Abort the request if the backend hangs so callers never wait forever.
  const controller = new AbortController()
  const timeout = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS)
  let res: Response
  try {
    res = await fetch(path, { ...init, headers, signal: controller.signal })
  } catch (err) {
    // A bare DOMException reads "signal is aborted without reason" — give the
    // UI something it can translate instead.
    if (err instanceof DOMException && err.name === 'AbortError') {
      throw new ApiError(`Request timed out after ${REQUEST_TIMEOUT_MS / 1000}s`, 'timeout')
    }
    throw new ApiError(err instanceof Error ? err.message : String(err), 'network')
  } finally {
    clearTimeout(timeout)
  }

  if (res.status === 401) {
    localStorage.removeItem(TOKEN_KEY)
    navigate('/login')
    throw new Error('Nicht autorisiert')
  }

  if (!res.ok) {
    let msg = `HTTP ${res.status}`
    try {
      const body = (await res.json()) as { detail?: string }
      msg = body.detail ?? msg
    } catch {
      /* ignore parse error */
    }
    // 503 from the core's runtime gate: the plugin is in a blocking warm-up /
    // maintenance window. Carry the status so the UI can show progress.
    if (res.status === 503) {
      let runtime: PluginRuntime | undefined
      try {
        const raw = res.headers.get('X-Plugin-Runtime')
        if (raw) runtime = JSON.parse(raw) as PluginRuntime
      } catch {
        /* header absent or malformed */
      }
      if (runtime) throw new ApiError(msg, 'runtime', 503, runtime)
    }
    throw new ApiError(msg, 'http', res.status)
  }

  return res.json() as Promise<T>
}

export const pluginApi = {
  get: <T>(subpath: string) =>
    apiFetch<T>(`/api/plugins/${PLUGIN_ID_PREFIX}/${subpath}`),

  post: <T>(subpath: string, body?: unknown) =>
    apiFetch<T>(`/api/plugins/${PLUGIN_ID_PREFIX}/${subpath}`, {
      method: 'POST',
      body: body !== undefined ? JSON.stringify(body) : undefined,
    }),
}
