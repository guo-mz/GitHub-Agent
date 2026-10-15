export interface DiffFile {
  path: string
  status: 'added' | 'deleted' | 'modified' | 'renamed' | 'unknown'
  additions: number
  deletions: number
  hunks: string[]
  raw: string
}

export interface DiffStats {
  files: number
  additions: number
  deletions: number
}

/** Parse unified diff text into per-file chunks for file tree / preview. */
export function parseUnifiedDiff(diffText: string): DiffFile[] {
  const text = (diffText || '').trim()
  if (!text) return []

  const blocks = text.split(/(?=^diff --git )/m).filter((b) => b.trim())
  if (!blocks.length && text.includes('@@')) {
    return [
      {
        path: 'changes',
        status: 'modified',
        additions: countLines(text, '+'),
        deletions: countLines(text, '-'),
        hunks: [text],
        raw: text,
      },
    ]
  }

  const files: DiffFile[] = []
  for (const block of blocks.length ? blocks : [text]) {
    const path = extractPath(block)
    const status = extractStatus(block)
    files.push({
      path,
      status,
      additions: countLines(block, '+'),
      deletions: countLines(block, '-'),
      hunks: [block],
      raw: block,
    })
  }
  return files
}

export function summarizeDiff(files: DiffFile[]): DiffStats {
  return {
    files: files.length,
    additions: files.reduce((s, f) => s + f.additions, 0),
    deletions: files.reduce((s, f) => s + f.deletions, 0),
  }
}

function countLines(block: string, prefix: '+' | '-'): number {
  let n = 0
  for (const line of block.split('\n')) {
    if (!line.startsWith(prefix)) continue
    if (prefix === '+' && (line.startsWith('+++') || line.startsWith('+++'))) continue
    if (prefix === '-' && (line.startsWith('---') || line.startsWith('---'))) continue
    if (line.startsWith('+++ ') || line.startsWith('--- ')) continue
    n += 1
  }
  return n
}

function extractPath(block: string): string {
  const git = block.match(/^diff --git a\/(.+?) b\/(.+)$/m)
  if (git) return git[2] || git[1]
  const plus = block.match(/^\+\+\+ [ab]\/(.+)$/m)
  if (plus) return plus[1]
  const minus = block.match(/^--- [ab]\/(.+)$/m)
  if (minus) return minus[1]
  return 'unnamed'
}

function extractStatus(block: string): DiffFile['status'] {
  if (/^new file mode/m.test(block)) return 'added'
  if (/^deleted file mode/m.test(block)) return 'deleted'
  if (/^rename from/m.test(block)) return 'renamed'
  return 'modified'
}

/** Split a file's unified diff into display lines with kind. */
export function toDiffLines(raw: string): { kind: 'add' | 'del' | 'ctx' | 'meta'; text: string }[] {
  return raw.split('\n').map((text) => {
    if (text.startsWith('+++') || text.startsWith('---') || text.startsWith('diff ') || text.startsWith('index ') || text.startsWith('@@')) {
      return { kind: 'meta' as const, text }
    }
    if (text.startsWith('+')) return { kind: 'add' as const, text }
    if (text.startsWith('-')) return { kind: 'del' as const, text }
    return { kind: 'ctx' as const, text }
  })
}
