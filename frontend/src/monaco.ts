/** 通过 CDN 加载 Monaco，避免 Vite worker 打包坑。 */

declare global {
  interface Window {
    require?: any
    monaco?: any
  }
}

const MONACO_BASE = 'https://cdn.jsdelivr.net/npm/monaco-editor@0.52.2/min/vs'

let loading: Promise<any> | null = null

function loadScript(src: string): Promise<void> {
  return new Promise((resolve, reject) => {
    const existed = document.querySelector(`script[src="${src}"]`)
    if (existed) {
      resolve()
      return
    }
    const s = document.createElement('script')
    s.src = src
    s.async = true
    s.onload = () => resolve()
    s.onerror = () => reject(new Error(`Failed to load ${src}`))
    document.head.appendChild(s)
  })
}

export async function loadMonaco(): Promise<any> {
  if (window.monaco) return window.monaco
  if (loading) return loading
  loading = (async () => {
    await loadScript(`${MONACO_BASE}/loader.js`)
    const req = window.require
    req.config({ paths: { vs: MONACO_BASE } })
    await new Promise<void>((resolve, reject) => {
      req(['vs/editor/editor.main'], () => resolve(), reject)
    })
    return window.monaco
  })()
  return loading
}

export function languageFromPath(path: string): string {
  const ext = path.split('.').pop()?.toLowerCase() || ''
  const map: Record<string, string> = {
    ts: 'typescript',
    tsx: 'typescript',
    js: 'javascript',
    jsx: 'javascript',
    vue: 'html',
    py: 'python',
    md: 'markdown',
    json: 'json',
    yml: 'yaml',
    yaml: 'yaml',
    css: 'css',
    scss: 'scss',
    html: 'html',
    sh: 'shell',
    sql: 'sql',
    go: 'go',
    rs: 'rust',
    java: 'java',
    toml: 'ini',
    env: 'ini',
    txt: 'plaintext',
  }
  return map[ext] || 'plaintext'
}
