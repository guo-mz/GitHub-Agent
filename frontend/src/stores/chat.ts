import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import {
  cancelTaskRun,
  fetchPendingApprovals,
  fetchTaskRun,
  retryTaskRun,
  saveRepoFile,
  streamSSE,
  submitApproval,
  type TaskRunView,
} from '../api'
import router from '../router'
import { rememberRepo } from '../utils/recent'

export type Role = 'user' | 'agent' | 'system'

export interface ToolCallView {
  id: string
  name: string
  input?: unknown
  output?: string
  status: 'running' | 'done' | 'error'
}

export interface TimelineNode {
  name: string
  label: string
  status: 'enter' | 'done'
}

export interface ResultCard {
  title: string
  pr_url?: string
  compare_url?: string
  history_path?: string
  hint?: string
}

export interface ChatMessage {
  id: string
  role: Role
  content: string
  tools?: ToolCallView[]
  timeline?: TimelineNode[]
  streaming?: boolean
  result?: ResultCard
  intent?: {
    task_type?: string
    confidence?: number
    source?: string
    reason?: string
    slots?: Record<string, unknown>
  }
}

export interface ApprovalPayload {
  thread_id: string
  payload: any
  risk_level?: string
  diff?: string
  operation?: string
  approval_kind?: string
}

export interface PlanConfirmPayload {
  thread_id: string
  payload: any
  approval_kind: 'plan_confirm'
}

export const useChatStore = defineStore('chat', () => {
  const messages = ref<ChatMessage[]>([])
  const DEFAULT_REPO = 'guo-mz/-'
  const savedRepo = localStorage.getItem('ga_repo')
  const bad = new Set(['owner/repo', 'octocat/Hello-World', '', 'owner/name'])
  const repo = ref(!savedRepo || bad.has(savedRepo) ? DEFAULT_REPO : savedRepo)
  if (bad.has(repo.value)) repo.value = DEFAULT_REPO
  localStorage.setItem('ga_repo', repo.value)

  const threadId = ref<string>(localStorage.getItem('ga_thread') || '')
  const streaming = ref(false)
  const approval = ref<ApprovalPayload | null>(null)
  const planConfirm = ref<PlanConfirmPayload | null>(null)
  const showApprovalModal = ref(false)
  const error = ref('')
  const taskRun = ref<TaskRunView | null>(null)
  const agentEnvelopes = ref<Record<string, unknown>[]>([])
  const taskBusy = ref(false)
  let abortCtrl: AbortController | null = null
  let lastTaskSeq = -1
  let elapsedTimer: number | undefined
  const liveElapsedMs = ref<number | null>(null)

  const taskStatus = computed(() => taskRun.value?.status || (streaming.value ? 'running' : 'ready'))

  function setRepo(r: string) {
    repo.value = r
    localStorage.setItem('ga_repo', r)
    rememberRepo(r)
  }

  function applyTaskStatus(data: any) {
    const view = (data?.data && data.data.id ? data.data : data) as TaskRunView
    if (!view || !view.id) return
    const seq = Number(view.event_seq ?? data?.seq ?? 0)
    // 断线重连 / 重复事件：只接受更新的 seq
    if (seq < lastTaskSeq && taskRun.value?.id === view.id) return
    lastTaskSeq = seq
    taskRun.value = view
    if (view.thread_id) {
      threadId.value = view.thread_id
      localStorage.setItem('ga_thread', view.thread_id)
    }
    // 终态后清掉过期 HITL 按钮（避免「失败了还显示确认计划」）
    if (['succeeded', 'failed', 'cancelled'].includes(view.status || '')) {
      planConfirm.value = null
      approval.value = null
      showApprovalModal.value = false
    }
    tickElapsed()
  }

  function tickElapsed() {
    const t = taskRun.value
    if (!t?.started_at) {
      liveElapsedMs.value = null
      return
    }
    try {
      const t0 = new Date(t.started_at).getTime()
      const t1 = t.ended_at ? new Date(t.ended_at).getTime() : Date.now()
      liveElapsedMs.value = Math.max(0, t1 - t0)
    } catch {
      liveElapsedMs.value = t.elapsed_ms ?? null
    }
  }

  function startElapsedClock() {
    stopElapsedClock()
    tickElapsed()
    elapsedTimer = window.setInterval(tickElapsed, 1000)
  }

  function stopElapsedClock() {
    if (elapsedTimer) {
      window.clearInterval(elapsedTimer)
      elapsedTimer = undefined
    }
  }

  async function restoreTask(id?: string) {
    const key = id || threadId.value
    if (!key) return
    try {
      const { task } = await fetchTaskRun(key)
      lastTaskSeq = -1
      applyTaskStatus({ data: task, seq: task.event_seq })
      if (Array.isArray(task.agent_envelopes) && task.agent_envelopes.length) {
        agentEnvelopes.value = task.agent_envelopes
      }
      // 硬刷新：从 pending 恢复 plan / diff / publish 审批面板
      try {
        const pending = await fetchPendingApprovals()
        const items = (pending?.items || pending || []) as any[]
        const tid = task.thread_id || key
        const hit =
          items.find((it) => it.thread_id === tid || it.task_id === tid) ||
          items.find((it) => it.thread_id === key || it.task_id === key)
        const terminal = ['succeeded', 'failed', 'cancelled'].includes(task.status || '')
        if (hit && !terminal) {
          const kind = hit.approval_kind || hit.payload?.approval_kind || 'publish'
          const payload = hit.payload || hit
          if (kind === 'plan_confirm') {
            planConfirm.value = {
              thread_id: hit.thread_id || tid,
              payload,
              approval_kind: 'plan_confirm',
            }
            approval.value = null
          } else {
            planConfirm.value = null
            approval.value = {
              thread_id: hit.thread_id || tid,
              payload,
              risk_level: hit.risk_level || payload.risk_level,
              diff: hit.diff || payload.diff || payload.workspace_diff?.combined_diff,
              operation: hit.operation || payload.operation || kind,
              approval_kind: kind,
            }
          }
        } else {
          // 终态或无 pending：清掉过期审批入口
          planConfirm.value = null
          approval.value = null
        }
      } catch {
        /* pending 拉取失败不阻断状态恢复 */
      }
      if (
        task.status &&
        !['succeeded', 'failed', 'cancelled', 'awaiting_coding', 'diff_approved'].includes(
          task.status,
        )
      ) {
        startElapsedClock()
      } else {
        stopElapsedClock()
      }
    } catch {
      /* 无历史任务时忽略 */
    }
  }

  function uid() {
    return Math.random().toString(36).slice(2, 10)
  }

  function apiHint(): string {
    const base = import.meta.env.VITE_API_BASE || 'http://127.0.0.1:8000'
    return `没有收到回复。请确认后端 ${base} 已启动，然后点「新会话」再试。`
  }

  /** 必须改 messages 数组里的项，直接改 push 前的普通对象 Vue 不会刷新 */
  function patchMessage(id: string, patch: Partial<ChatMessage>) {
    const i = messages.value.findIndex((m) => m.id === id)
    if (i < 0) return
    const cur = messages.value[i]
    messages.value[i] = {
      ...cur,
      ...patch,
      tools: patch.tools ? [...patch.tools] : cur.tools,
      timeline: patch.timeline ? [...patch.timeline] : cur.timeline,
      result: patch.result !== undefined ? patch.result : cur.result,
    }
  }

  function extractPrMeta(raw: unknown): ResultCard | null {
    const tryParse = (s: string) => {
      try {
        return JSON.parse(s)
      } catch {
        return null
      }
    }
    let obj: any = raw
    if (typeof raw === 'string') {
      const m = raw.match(/https:\/\/github\.com\/[^\s"'<>]+\/pull\/\d+/)
      const parsed = tryParse(raw) || tryParse(raw.replace(/^[\s\S]*?(\{[\s\S]*\})[\s\S]*$/, '$1'))
      obj = parsed || (m ? { html_url: m[0], ok: true } : null)
    }
    if (!obj || typeof obj !== 'object') return null
    const nested = typeof obj.cached === 'string' ? tryParse(obj.cached) : obj.cached
    const src = nested && typeof nested === 'object' ? { ...obj, ...nested } : obj
    const prUrl = src.html_url || src.pr_url || ''
    if (!prUrl || !String(prUrl).includes('github.com')) return null
    const head = src.head || ''
    const base = src.base || 'main'
    const [owner, name] = (repo.value || '').split('/')
    const compare =
      owner && name && head
        ? `https://github.com/${owner}/${name}/compare/${base}...${head}`
        : ''
    return {
      title: src.number ? `PR #${src.number} 已创建` : 'Pull Request 已创建',
      pr_url: prUrl,
      compare_url: compare,
      history_path: '/approvals',
      hint: '回滚：未合并可关 PR 删分支；已合并在 PR 页点 Revert。审批记录见「审批/历史」。',
    }
  }

  function getMessage(id: string): ChatMessage | undefined {
    return messages.value.find((m) => m.id === id)
  }

  function stop() {
    // 前端断开 SSE；同时通知后端取消后续节点
    const id = taskRun.value?.id || threadId.value
    if (id) {
      cancelTaskRun(id).catch(() => {})
    }
    abortCtrl?.abort()
    abortCtrl = null
    streaming.value = false
    stopElapsedClock()
  }

  async function cancelCurrentTask() {
    const id = taskRun.value?.id || threadId.value
    if (!id) {
      stop()
      return
    }
    taskBusy.value = true
    try {
      const res = await cancelTaskRun(id)
      if (res.task) applyTaskStatus({ data: res.task, seq: res.task.event_seq })
      abortCtrl?.abort()
      abortCtrl = null
      streaming.value = false
      stopElapsedClock()
    } catch (e: any) {
      error.value = e?.response?.data?.detail || e?.message || String(e)
    } finally {
      taskBusy.value = false
    }
  }

  async function retryCurrentTask() {
    const id = taskRun.value?.id
    if (!id) return
    taskBusy.value = true
    error.value = ''
    try {
      const res = await retryTaskRun(id)
      if (!res.task) throw new Error('重试创建失败')
      // 新 thread，清空对话后重放原需求
      stop()
      threadId.value = res.task.thread_id
      localStorage.setItem('ga_thread', res.task.thread_id)
      messages.value = []
      approval.value = null
      planConfirm.value = null
      agentEnvelopes.value = []
      lastTaskSeq = -1
      applyTaskStatus({ data: res.task, seq: res.task.event_seq })
      const text = (res.message || taskRun.value?.message || '').trim()
      const r = (res.repo || repo.value || DEFAULT_REPO).trim()
      if (r) setRepo(r)
      if (text) await send(text)
    } catch (e: any) {
      error.value = e?.response?.data?.detail || e?.message || String(e)
    } finally {
      taskBusy.value = false
    }
  }

  async function send(text: string) {
    if (!text.trim() || streaming.value) return
    error.value = ''
    lastTaskSeq = -1
    const userId = uid()
    const agentId = uid()
    messages.value.push({ id: userId, role: 'user', content: text.trim() })
    messages.value.push({
      id: agentId,
      role: 'agent',
      content: '',
      tools: [],
      timeline: [],
      streaming: true,
    })
    streaming.value = true
    abortCtrl = new AbortController()
    startElapsedClock()

    const body: Record<string, unknown> = {
      message: text.trim(),
      repo: repo.value || DEFAULT_REPO,
    }
    if (threadId.value) body.thread_id = threadId.value

    try {
      await streamSSE(
        '/api/chat/stream',
        body,
        (event, data) => {
          if (data.thread_id) {
            threadId.value = data.thread_id
            localStorage.setItem('ga_thread', data.thread_id)
          }
          handleEvent(event, data, agentId)
        },
        abortCtrl.signal,
      )
    } catch (e: any) {
      if (e?.name === 'AbortError') {
        const cur = getMessage(agentId)
        patchMessage(agentId, {
          content: `${cur?.content || ''}\n\n（已停止）`.trim(),
        })
      } else {
        error.value = e?.message || String(e)
        const cur = getMessage(agentId)
        patchMessage(agentId, {
          content: `${cur?.content || ''}\n[错误] ${error.value}`.trim(),
        })
      }
    } finally {
      patchMessage(agentId, { streaming: false })
      streaming.value = false
      abortCtrl = null
      tickElapsed()
      if (
        taskRun.value &&
        ['succeeded', 'failed', 'cancelled', 'awaiting_coding', 'diff_approved'].includes(
          taskRun.value.status,
        )
      ) {
        stopElapsedClock()
      }
      const cur = getMessage(agentId)
      if (!cur?.content?.trim() && !(cur?.timeline || []).length) {
        patchMessage(agentId, { content: apiHint() })
      }
    }
  }

  function handleEvent(event: string, data: any, agentId: string) {
    const ev = event || data.event
    if (ev === 'task_status') {
      applyTaskStatus(data)
      return
    }
    const cur = getMessage(agentId)
    if (!cur) return

    if (ev === 'node') {
      const name = data.node || data.data?.node || 'node'
      const label = data.data?.label || name
      const status = (data.data?.status === 'done' ? 'done' : 'enter') as 'enter' | 'done'
      const intent = data.data?.intent
      const extra: Partial<ChatMessage> = {}
      if (name === 'classify_intent' && intent && typeof intent === 'object') {
        extra.intent = intent
      }
      const timeline = [...(cur.timeline || [])]
      const last = timeline[timeline.length - 1]
      if (last && last.name === name) {
        last.status = status
        last.label = label
      } else {
        timeline.push({ name, label, status })
      }
      // 同一节点 enter→done：合并；过长时保留最近 16 步
      patchMessage(agentId, { timeline: timeline.slice(-16), ...extra })
    } else if (ev === 'thought') {
      if (typeof data.data?.replace === 'string' && data.data.replace.trim()) {
        patchMessage(agentId, { content: data.data.replace })
      } else if (data.data?.delta) {
        patchMessage(agentId, { content: (cur.content || '') + data.data.delta })
      }
    } else if (ev === 'action') {
      const tools = [...(cur.tools || [])]
      tools.push({
        id: uid(),
        name: data.data?.tool || 'tool',
        input: data.data?.input,
        status: 'running',
      })
      patchMessage(agentId, { tools })
    } else if (ev === 'observation') {
      const tools = [...(cur.tools || [])]
      const last = [...tools].reverse().find((t) => t.name === data.data?.tool && t.status === 'running')
      if (last) {
        last.output = data.data?.output
        last.status = 'done'
      } else {
        tools.push({
          id: uid(),
          name: data.data?.tool || 'tool',
          output: data.data?.output,
          status: 'done',
        })
      }
      const card =
        extractPrMeta(data.data?.output) ||
        extractPrMeta(typeof data.data === 'string' ? data.data : null)
      patchMessage(agentId, {
        tools: [...tools],
        ...(card ? { result: card } : {}),
      })
    } else if (ev === 'plan_confirm_needed') {
      const payload = data.data?.payload || {}
      planConfirm.value = {
        thread_id: data.thread_id || data.data?.thread_id || threadId.value,
        payload,
        approval_kind: 'plan_confirm',
      }
      const envs = data.data?.agent_envelopes || payload.agent_envelopes
      if (Array.isArray(envs) && envs.length) {
        agentEnvelopes.value = envs
      }
      patchMessage(agentId, {
        content: `${cur.content || ''}\n\n请确认修改计划后才能进入编码阶段…`.trim(),
      })
      if (router.currentRoute.value.name !== 'plan') {
        router.push('/plan').catch(() => {})
      }
    } else if (ev === 'diff_approve_needed') {
      const payload = data.data?.payload || {}
      const envs = data.data?.agent_envelopes || payload.agent_envelopes
      if (Array.isArray(envs) && envs.length) {
        agentEnvelopes.value = envs
      }
      approval.value = {
        thread_id: data.thread_id || data.data?.thread_id || threadId.value,
        payload,
        risk_level: payload.risk_level || 'high',
        diff: payload.diff || payload.workspace_diff?.combined_diff,
        operation: 'diff_approve',
        approval_kind: 'diff_approve',
      }
      showApprovalModal.value = false
      patchMessage(agentId, {
        content: `${cur.content || ''}\n\n请审查工作区真实 Diff（确认 ≠ 创建 PR）…`.trim(),
      })
      if (router.currentRoute.value.name !== 'review') {
        router.push('/review').catch(() => {})
      }
    } else if (ev === 'approval_needed') {
      const payload = data.data?.payload || {}
      const kind = data.data?.approval_kind || payload.approval_kind || 'publish'
      if (kind === 'plan_confirm') {
        planConfirm.value = {
          thread_id: data.thread_id || threadId.value,
          payload,
          approval_kind: 'plan_confirm',
        }
        if (router.currentRoute.value.name !== 'plan') {
          router.push('/plan').catch(() => {})
        }
        return
      }
      if (kind === 'diff_approve') {
        approval.value = {
          thread_id: data.thread_id || threadId.value,
          payload,
          risk_level: payload.risk_level || 'high',
          diff: payload.diff,
          operation: 'diff_approve',
          approval_kind: 'diff_approve',
        }
        showApprovalModal.value = false
        if (router.currentRoute.value.name !== 'review') {
          router.push('/review').catch(() => {})
        }
        return
      }
      approval.value = {
        thread_id: data.thread_id || threadId.value,
        payload,
        risk_level: payload.risk_level,
        diff: payload.diff,
        operation: payload.tool_name || payload.operation,
        approval_kind: kind,
      }
      showApprovalModal.value = false
      patchMessage(agentId, {
        content: `${cur.content || ''}\n\n需要人工审批发布操作（与计划确认分开）…`.trim(),
      })
      if (router.currentRoute.value.name !== 'review') {
        router.push('/review').catch(() => {})
      }
    } else if (ev === 'done') {
      const answer = (data.data?.final_answer || '').trim()
      const text = (cur.content || '').trim()
      const looksLikeInternal =
        !text ||
        text.startsWith('{') ||
        text.includes('"task_type"') ||
        text.length < 20
      const card =
        extractPrMeta(data.data) ||
        extractPrMeta(answer) ||
        extractPrMeta(text) ||
        cur.result
      const envs = data.data?.agent_envelopes
      if (Array.isArray(envs)) {
        agentEnvelopes.value = envs
      }
      const patch: Partial<ChatMessage> = {}
      if (answer && looksLikeInternal) {
        patch.content = answer
      } else if (answer && !text.includes(answer.slice(0, Math.min(40, answer.length)))) {
        patch.content = `${text}\n\n${answer}`
      }
      if (card) patch.result = card
      if (Object.keys(patch).length) patchMessage(agentId, patch)
    } else if (ev === 'retry') {
      const label =
        data?.data?.label ||
        `瞬时故障，整轮重试 ${data?.data?.attempt || '?'}/${data?.data?.max_attempts || '?'}`
      const timeline = [...(cur.timeline || [])]
      timeline.push({ name: 'retry', label, status: 'enter' })
      patchMessage(agentId, {
        timeline: timeline.slice(-16),
        content: `${cur.content || ''}\n↻ ${label}`.trim(),
      })
    } else if (ev === 'error') {
      const msg =
        data?.data?.message || data?.message || data?.data?.error || 'unknown'
      patchMessage(agentId, {
        content: `${cur.content || ''}\n[错误] ${msg}`.trim(),
      })
      if (data?.data?.cancelled) {
        error.value = msg
      }
    }
  }

  async function decidePlanConfirm(decision: 'approve' | 'reject', comment = '') {
    if (!planConfirm.value) return
    const tid = planConfirm.value.thread_id
    streaming.value = true
    abortCtrl = new AbortController()
    const agentId = uid()
    messages.value.push({
      id: agentId,
      role: 'agent',
      content:
        decision === 'approve'
          ? '已确认计划，进入后续阶段…'
          : '已拒绝计划，不会进入编码，仓库未被修改…',
      tools: [],
      timeline: [],
      streaming: true,
    })
    try {
      startElapsedClock()
      await submitApproval(
        tid,
        decision,
        comment,
        (event, data) => handleEvent(event, data, agentId),
        abortCtrl.signal,
        { approval_kind: 'plan_confirm', task_id: tid },
      )
    } catch (e: any) {
      if (e?.name !== 'AbortError') {
        const cur = getMessage(agentId)
        patchMessage(agentId, {
          content: `${cur?.content || ''}\n[错误] ${e?.message || e}`.trim(),
        })
      }
    } finally {
      patchMessage(agentId, { streaming: false })
      streaming.value = false
      abortCtrl = null
      planConfirm.value = null
    }
  }

  async function decideApproval(decision: 'approve' | 'reject', comment = '') {
    if (!approval.value) return
    const tid = approval.value.thread_id
    const kind = approval.value.approval_kind || 'publish'
    showApprovalModal.value = false
    streaming.value = true
    abortCtrl = new AbortController()
    const agentId = uid()
    const approveMsg =
      kind === 'diff_approve'
        ? '已确认 Diff，进入测试/发布审批…'
        : '已批准发布，继续执行写远程…'
    const rejectMsg =
      kind === 'diff_approve'
        ? '已拒绝 Diff，工作区变更已丢弃，远程未修改…'
        : '已拒绝发布，远程仓库未写入…'
    messages.value.push({
      id: agentId,
      role: 'agent',
      content: decision === 'approve' ? approveMsg : rejectMsg,
      tools: [],
      timeline: [],
      streaming: true,
    })
    try {
      startElapsedClock()
      await submitApproval(
        tid,
        decision,
        comment,
        (event, data) => {
          handleEvent(event, data, agentId)
        },
        abortCtrl.signal,
        { approval_kind: kind, task_id: tid },
      )
    } catch (e: any) {
      if (e?.name !== 'AbortError') {
        const cur = getMessage(agentId)
        patchMessage(agentId, {
          content: `${cur?.content || ''}\n[错误] ${e?.message || e}`.trim(),
        })
      }
    } finally {
      const cur = getMessage(agentId)
      // 从工具输出或正文再扫一次 PR 链接
      if (decision === 'approve' && !cur?.result) {
        for (const t of [...(cur?.tools || [])].reverse()) {
          const card = extractPrMeta(t.output)
          if (card) {
            patchMessage(agentId, { result: card })
            break
          }
        }
        if (!getMessage(agentId)?.result) {
          const card = extractPrMeta(cur?.content)
          if (card) patchMessage(agentId, { result: card })
        }
      }
      patchMessage(agentId, { streaming: false })
      streaming.value = false
      abortCtrl = null
      approval.value = null
    }
  }

  function newThread() {
    stop()
    threadId.value = ''
    localStorage.removeItem('ga_thread')
    messages.value = []
    approval.value = null
    planConfirm.value = null
    agentEnvelopes.value = []
    taskRun.value = null
    lastTaskSeq = -1
    liveElapsedMs.value = null
    stopElapsedClock()
  }

  /** 下载最终回答为 Markdown */
  function downloadAnswer(messageId: string) {
    const msg = getMessage(messageId)
    const text = (msg?.content || '').trim()
    if (!text) return
    const stamp = new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-')
    const blob = new Blob([`# Agent Output\n\n仓库: ${repo.value}\n\n${text}\n`], {
      type: 'text/markdown;charset=utf-8',
    })
    const a = document.createElement('a')
    a.href = URL.createObjectURL(blob)
    a.download = `agent-output-${stamp}.md`
    a.click()
    URL.revokeObjectURL(a.href)
  }

  /** 把最终回答保存到仓库 docs/agent-outputs/ 并可选提 PR */
  async function saveAnswerToRepo(messageId: string, openPr = false) {
    const msg = getMessage(messageId)
    const text = (msg?.content || '').trim()
    if (!text) throw new Error('没有可保存的内容')
    const [owner, name] = (repo.value || DEFAULT_REPO).split('/')
    if (!owner || !name) throw new Error('仓库格式应为 owner/repo')
    const stamp = new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-')
    const path = `docs/agent-outputs/${stamp}.md`
    const content = `# Agent Output\n\n- repo: \`${repo.value}\`\n- thread: \`${threadId.value || '-'}\`\n- at: ${new Date().toISOString()}\n\n---\n\n${text}\n`
    // 远程直写已禁用；调用将返回 403，引导走多 Agent 审批发布
    return saveRepoFile(owner, name, {
      path,
      content,
      message: `docs: save agent output ${stamp}`,
      base_branch: 'main',
      open_pr: false,
      pr_title: `docs: agent output ${stamp}`,
      pr_body: `Saved agent final answer to \`${path}\`.`,
    })
  }

  return {
    messages,
    repo,
    threadId,
    streaming,
    approval,
    planConfirm,
    showApprovalModal,
    error,
    taskRun,
    taskStatus,
    taskBusy,
    liveElapsedMs,
    agentEnvelopes,
    setRepo,
    send,
    stop,
    cancelCurrentTask,
    retryCurrentTask,
    restoreTask,
    decideApproval,
    decidePlanConfirm,
    newThread,
    downloadAnswer,
    saveAnswerToRepo,
  }
})
















