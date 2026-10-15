const KEY = 'ga_recent_repos'

export function rememberRepo(repo: string) {
  const r = (repo || '').trim()
  if (!r || !r.includes('/')) return
  try {
    const raw = localStorage.getItem(KEY)
    const list: string[] = raw ? JSON.parse(raw) : []
    const next = [r, ...list.filter((x) => x !== r)].slice(0, 8)
    localStorage.setItem(KEY, JSON.stringify(next))
  } catch {
    localStorage.setItem(KEY, JSON.stringify([r]))
  }
}
