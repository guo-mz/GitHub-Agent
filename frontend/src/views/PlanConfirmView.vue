<template>
  <section class="plan page-scroll">
    <div class="page-bar surface">
      <div>
        <h1 class="page-title">确认修改计划</h1>
        <p class="page-hint">
          确认后才会进入编码阶段。拒绝或取消不会修改仓库。此审批与发布 PR 审批相互独立。
        </p>
        <p v-if="taskId" class="mono meta">task {{ taskId }} · kind=plan_confirm</p>
      </div>
      <StatusBadge status="awaiting_approval" label="待确认计划" />
    </div>

    <EmptyState
      v-if="!hasPlan"
      icon="tasks"
      title="当前没有待确认的计划"
      description="提交写/审查类任务后，Analysis 与 Planning Agent 会生成计划并跳转到此页。"
    >
      <RouterLink class="btn btn-primary" to="/chat">返回任务执行</RouterLink>
    </EmptyState>

    <template v-else>
      <div class="grid">
        <article class="surface block">
          <h2>仓库分析</h2>
          <p v-if="stack.length"><strong>技术栈：</strong>{{ stack.join('、') }}</p>
          <p v-if="uncertainties.length"><strong>不确定项：</strong>{{ uncertainties.join('；') }}</p>
          <ul v-if="relevantFiles.length" class="files">
            <li v-for="f in relevantFiles" :key="f.path" class="mono">
              {{ f.path }}
              <span class="muted">{{ f.reason }}</span>
            </li>
          </ul>
          <p v-else class="muted">暂无相关文件摘要</p>
        </article>

        <article class="surface block">
          <h2>{{ plan.title || '修改计划' }}</h2>
          <p v-if="plan.exceeds_request" class="warn">
            计划可能超出需求：{{ plan.exceeds_reason || '请仔细核对' }}
          </p>
          <div v-if="plan.goals?.length" class="sec">
            <h3>目标</h3>
            <ul>
              <li v-for="(g, i) in plan.goals" :key="i">{{ g }}</li>
            </ul>
          </div>
          <div v-if="plan.files_to_change?.length" class="sec">
            <h3>拟修改文件</h3>
            <ul class="mono">
              <li v-for="f in plan.files_to_change" :key="f">{{ f }}</li>
            </ul>
          </div>
          <div v-if="plan.steps?.length" class="sec">
            <h3>实施步骤</h3>
            <ol>
              <li v-for="s in plan.steps" :key="s.step">
                {{ s.action }}
                <span v-if="s.risk" class="muted">（风险：{{ s.risk }}）</span>
              </li>
            </ol>
          </div>
          <div v-if="plan.risks?.length" class="sec">
            <h3>风险</h3>
            <ul>
              <li v-for="(r, i) in plan.risks" :key="i">{{ r }}</li>
            </ul>
          </div>
          <div v-if="plan.verification?.length" class="sec">
            <h3>验证方法</h3>
            <ul>
              <li v-for="(v, i) in plan.verification" :key="i">{{ v }}</li>
            </ul>
          </div>
        </article>
      </div>

      <div class="action-bar surface">
        <textarea
          v-model="comment"
          class="field"
          rows="2"
          placeholder="确认/拒绝备注（可选）"
          :disabled="busy"
        />
        <div class="ops">
          <RouterLink class="btn" to="/chat">返回</RouterLink>
          <button class="btn btn-danger" type="button" :disabled="busy" @click="reject">
            拒绝计划
          </button>
          <button class="btn btn-success" type="button" :disabled="busy" @click="approve">
            {{ busy ? '提交中…' : '确认计划（进入编码阶段）' }}
          </button>
        </div>
        <p v-if="error" class="err">{{ error }}</p>
      </div>
    </template>
  </section>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { RouterLink, useRouter } from 'vue-router'
import EmptyState from '../components/ui/EmptyState.vue'
import StatusBadge from '../components/ui/StatusBadge.vue'
import { useChatStore } from '../stores/chat'

const store = useChatStore()
const router = useRouter()
const comment = ref('')
const busy = ref(false)
const error = ref('')

const payload = computed(() => store.planConfirm?.payload || {})
const plan = computed(() => (payload.value.plan_document || {}) as Record<string, any>)
const analysis = computed(() => (payload.value.analysis_report || {}) as Record<string, any>)
const hasPlan = computed(() => !!store.planConfirm && !!plan.value && Object.keys(plan.value).length > 0)
const taskId = computed(() => store.planConfirm?.thread_id || store.threadId)
const stack = computed(() => (analysis.value.tech_stack as string[]) || [])
const uncertainties = computed(() => (analysis.value.uncertainties as string[]) || [])
const relevantFiles = computed(
  () => (analysis.value.relevant_files as { path: string; reason?: string }[]) || [],
)

async function approve() {
  if (!store.planConfirm || busy.value) return
  busy.value = true
  error.value = ''
  try {
    await store.decidePlanConfirm('approve', comment.value)
    await router.push('/chat')
  } catch (e: any) {
    error.value = e?.response?.data?.detail || e?.message || String(e)
  } finally {
    busy.value = false
  }
}

async function reject() {
  if (!store.planConfirm || busy.value) return
  busy.value = true
  error.value = ''
  try {
    await store.decidePlanConfirm('reject', comment.value)
    await router.push('/chat')
  } catch (e: any) {
    error.value = e?.response?.data?.detail || e?.message || String(e)
  } finally {
    busy.value = false
  }
}
</script>

<style scoped>
.plan {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.page-bar {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  padding: 16px 18px;
}

.meta {
  margin: 8px 0 0;
  font-size: 0.75rem;
  color: var(--muted);
}

.grid {
  display: grid;
  grid-template-columns: 1fr 1.2fr;
  gap: 12px;
}

.block {
  padding: 16px 18px;
}

.block h2 {
  margin: 0 0 12px;
  font-size: 1.05rem;
}

.block h3 {
  margin: 0 0 6px;
  font-size: 0.85rem;
  color: var(--muted);
}

.sec {
  margin-bottom: 14px;
}

.files {
  list-style: none;
  padding: 0;
  margin: 8px 0 0;
}

.files li {
  padding: 6px 0;
  border-bottom: 1px solid var(--line);
  font-size: 0.8rem;
}

.muted {
  color: var(--muted);
  font-weight: 400;
  margin-left: 8px;
}

.warn {
  color: var(--warn);
  background: var(--warn-dim);
  padding: 8px 10px;
  border-radius: 8px;
  font-size: 0.85rem;
}

.action-bar {
  padding: 12px 14px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.ops {
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: 8px;
}

@media (max-width: 860px) {
  .grid {
    grid-template-columns: 1fr;
  }
}
</style>
