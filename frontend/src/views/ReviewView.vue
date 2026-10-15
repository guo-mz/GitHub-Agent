<template>
  <section class="review">
    <div class="page-bar surface">
      <div class="info">
        <div class="title-row">
          <h1 class="page-title">代码审查</h1>
          <StatusBadge :status="risk || 'high'" />
          <StatusBadge status="awaiting_approval" />
        </div>
        <p class="page-hint">
          {{
            isDiffApprove
              ? '工作区 Diff 确认'
              : isPublish
                ? '发布审批（创建分支/PR）'
                : operation || '高危写操作'
          }}
          · {{ repo || '—' }}
          <span v-if="branchInfo"> · {{ branchInfo }}</span>
          <span v-if="isDiffApprove"> · kind=diff_approve（不创建 PR）</span>
          <span v-else-if="isPublish"> · kind=publish（需独立批准）</span>
        </p>
        <p class="version mono" v-if="diffFingerprint">
          变更版本 {{ diffFingerprint }}
          <span v-if="stale" class="stale">· 审批内容已变化，请重新审查后再确认</span>
        </p>
      </div>
      <div class="stats" v-if="stats.files">
        <div class="stat">
          <span class="k">文件</span>
          <span class="v mono">{{ stats.files }}</span>
        </div>
        <div class="stat">
          <span class="k">新增</span>
          <span class="v mono add">+{{ stats.additions }}</span>
        </div>
        <div class="stat">
          <span class="k">删除</span>
          <span class="v mono del">-{{ stats.deletions }}</span>
        </div>
      </div>
    </div>

    <EmptyState
      v-if="!hasApproval"
      icon="pr"
      title="当前没有待审查的变更"
      description="工作区编码完成后的 Diff 确认，或 create_pr / merge 发布审批，会跳转到此页。"
    >
      <RouterLink class="btn btn-primary" to="/chat">返回任务执行</RouterLink>
      <RouterLink class="btn" to="/approvals">查看待审批列表</RouterLink>
    </EmptyState>

    <template v-else>
      <div v-if="isPublish && testHint" class="test-banner surface">
        <strong>Testing</strong>
        <span class="mono">{{ testHint }}</span>
      </div>
      <div class="workspace surface">
        <ChangedFileTree :files="files" :selected="selected" @select="selected = $event" />
        <DiffViewer :files="files" :selected="selected" :stats="stats" />
      </div>

      <div class="action-bar surface">
        <textarea
          v-model="comment"
          class="field remark"
          rows="2"
          placeholder="审批备注（可选）"
          :disabled="busy"
        />
        <div class="ops">
          <RouterLink class="btn" to="/chat">返回修改</RouterLink>
          <a
            v-if="compareUrl"
            class="btn"
            :href="compareUrl"
            target="_blank"
            rel="noreferrer"
          >
            查看完整 Diff
          </a>
          <button class="btn btn-danger" type="button" :disabled="busy" @click="reject">
            拒绝变更
          </button>
          <button class="btn btn-success" type="button" :disabled="busy || stale" @click="approve">
            {{
              busy
                ? '提交中…'
                : isDiffApprove
                  ? '确认 Diff（不创建 PR）'
                  : isPublish
                    ? '批准发布并创建 PR'
                    : '确认并创建 PR'
            }}
          </button>
        </div>
      </div>
      <p v-if="error" class="err">{{ error }}</p>
    </template>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { RouterLink, useRouter } from 'vue-router'
import ChangedFileTree from '../components/diff/ChangedFileTree.vue'
import DiffViewer from '../components/diff/DiffViewer.vue'
import EmptyState from '../components/ui/EmptyState.vue'
import StatusBadge from '../components/ui/StatusBadge.vue'
import { useChatStore } from '../stores/chat'
import { parseUnifiedDiff, summarizeDiff } from '../utils/diff'

const store = useChatStore()
const router = useRouter()

const comment = ref('')
const busy = ref(false)
const error = ref('')
const selected = ref('')
const reviewedFingerprint = ref('')

const hasApproval = computed(() => !!store.approval)
const isDiffApprove = computed(
  () =>
    (store.approval?.approval_kind || store.approval?.payload?.approval_kind) === 'diff_approve',
)
const isPublish = computed(() => {
  const kind = store.approval?.approval_kind || store.approval?.payload?.approval_kind
  return kind === 'publish' || (!isDiffApprove.value && kind !== 'plan_confirm')
})
const diffText = computed(
  () =>
    store.approval?.diff ||
    store.approval?.payload?.diff ||
    store.approval?.payload?.workspace_diff?.combined_diff ||
    '',
)
const files = computed(() => parseUnifiedDiff(diffText.value))
const stats = computed(() => summarizeDiff(files.value))
const operation = computed(() => store.approval?.operation || store.approval?.payload?.tool_name)
const risk = computed(() => store.approval?.risk_level || store.approval?.payload?.risk_level)
const repo = computed(() => store.approval?.payload?.repo || store.repo)
const branchInfo = computed(() => {
  const p = store.approval?.payload || {}
  const head = p.head || p.branch || p.target_branch
  const base = p.base || p.base_branch || 'main'
  if (!head) return ''
  return `${head} → ${base}`
})
const compareUrl = computed(() => {
  const p = store.approval?.payload || {}
  if (p.compare_url) return p.compare_url
  const [owner, name] = (repo.value || '').split('/')
  const head = p.head || p.branch
  const base = p.base || 'main'
  if (owner && name && head) return `https://github.com/${owner}/${name}/compare/${base}...${head}`
  return ''
})
const testHint = computed(() => {
  const tr = store.approval?.payload?.test_report || store.taskRun?.test_report || {}
  if (!tr || typeof tr !== 'object') return ''
  if (tr.not_run) return 'status=not_run（无可靠沙箱，未执行仓库测试；发布需人工确认）'
  if (tr.failed) return `status=failed · repair=${tr.repair_count ?? '—'}`
  if (tr.passed) return 'status=passed'
  if (tr.status) return `status=${tr.status}`
  return ''
})

function fingerprint(text: string) {
  let h = 0
  for (let i = 0; i < text.length; i++) h = (Math.imul(31, h) + text.charCodeAt(i)) | 0
  return `v${Math.abs(h).toString(16).slice(0, 8)}`
}

const diffFingerprint = computed(() => (diffText.value ? fingerprint(diffText.value) : ''))
const stale = computed(
  () =>
    !!reviewedFingerprint.value &&
    !!diffFingerprint.value &&
    reviewedFingerprint.value !== diffFingerprint.value,
)

watch(
  files,
  (list) => {
    if (list.length && !list.find((f) => f.path === selected.value)) {
      selected.value = list[0].path
    }
  },
  { immediate: true },
)

watch(
  diffFingerprint,
  (fp) => {
    if (fp && !reviewedFingerprint.value) reviewedFingerprint.value = fp
  },
  { immediate: true },
)

async function approve() {
  if (!store.approval || busy.value || stale.value) return
  // Re-check fingerprint at submit time
  if (reviewedFingerprint.value !== diffFingerprint.value) {
    error.value = '变更版本已更新，请重新审查后再确认。'
    return
  }
  busy.value = true
  error.value = ''
  try {
    await store.decideApproval('approve', comment.value)
    await router.push('/chat')
  } catch (e: any) {
    error.value = e?.message || String(e)
  } finally {
    busy.value = false
  }
}

async function reject() {
  if (!store.approval || busy.value) return
  busy.value = true
  error.value = ''
  try {
    await store.decideApproval('reject', comment.value)
    await router.push('/chat')
  } catch (e: any) {
    error.value = e?.message || String(e)
  } finally {
    busy.value = false
  }
}

onMounted(() => {
  if (diffFingerprint.value) reviewedFingerprint.value = diffFingerprint.value
})
</script>

<style scoped>
.review {
  display: flex;
  flex-direction: column;
  gap: 12px;
  flex: 1;
  min-height: 0;
  height: 100%;
}

.page-bar {
  display: flex;
  justify-content: space-between;
  gap: 16px;
  padding: 16px 18px;
  flex-shrink: 0;
}

.title-row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}

.page-title {
  margin: 0;
}

.page-hint {
  margin: 8px 0 0;
}

.version {
  margin: 6px 0 0;
  font-size: 0.72rem;
  color: var(--muted);
}

.stale {
  color: var(--danger);
  font-weight: 600;
}

.stats {
  display: flex;
  gap: 16px;
  align-items: center;
}

.stat {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 2px;
}

.stat .k {
  font-size: 0.7rem;
  color: var(--muted);
}

.stat .v {
  font-weight: 650;
}

.add {
  color: var(--success);
}

.del {
  color: var(--danger);
}

.test-banner {
  flex-shrink: 0;
  padding: 10px 14px;
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  align-items: baseline;
  font-size: 0.85rem;
}

.test-banner .mono {
  color: var(--muted);
}

.workspace {
  flex: 1;
  min-height: 320px;
  display: grid;
  grid-template-columns: minmax(200px, 260px) 1fr;
  overflow: hidden;
}

.action-bar {
  flex-shrink: 0;
  padding: 12px 14px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.remark {
  resize: vertical;
}

.ops {
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: 8px;
}

@media (max-width: 800px) {
  .workspace {
    grid-template-columns: 1fr;
    min-height: 480px;
  }

  .page-bar {
    flex-direction: column;
  }

  .stats {
    justify-content: flex-start;
  }

  .stat {
    align-items: flex-start;
  }
}
</style>
