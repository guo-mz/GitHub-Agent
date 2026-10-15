/** 轻量 Markdown → HTML，避免界面露出 ## / ** 等原始符号 */

function escapeHtml(s: string): string {
  return s
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
}

export function renderMarkdown(src: string): string {
  if (!src) return ''
  const lines = src.replace(/\r\n/g, '\n').split('\n')
  const out: string[] = []
  let inList = false

  const flushList = () => {
    if (inList) {
      out.push('</ul>')
      inList = false
    }
  }

  for (const raw of lines) {
    const line = raw.trimEnd()
    const bullet = line.match(/^[-*]\s+(.+)$/)
    if (bullet) {
      if (!inList) {
        out.push('<ul>')
        inList = true
      }
      out.push(`<li>${inline(escapeHtml(bullet[1]))}</li>`)
      continue
    }
    flushList()

    const h3 = line.match(/^###\s+(.+)$/)
    if (h3) {
      out.push(`<h3>${inline(escapeHtml(h3[1]))}</h3>`)
      continue
    }
    const h2 = line.match(/^##\s+(.+)$/)
    if (h2) {
      out.push(`<h2>${inline(escapeHtml(h2[1]))}</h2>`)
      continue
    }
    const h1 = line.match(/^#\s+(.+)$/)
    if (h1) {
      out.push(`<h2>${inline(escapeHtml(h1[1]))}</h2>`)
      continue
    }
    if (!line.trim()) {
      out.push('<div class="gap"></div>')
      continue
    }
    out.push(`<p>${inline(escapeHtml(line))}</p>`)
  }
  flushList()
  return out.join('')
}

function inline(s: string): string {
  return s
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/`([^`]+)`/g, '<code>$1</code>')
}
