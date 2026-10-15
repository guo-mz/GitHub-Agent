<template>
  <section class="eval page-scroll">
    <div class="head">
      <div>
        <h1 class="page-title">评测 / 幻觉率</h1>
        <p class="page-hint">
          固定样例集：意图准确率 + 金标接地率 + 幻觉检出率；可选 live 真调 LLM 测「当前日期」。
        </p>
        <p class="cite">
          可引用：金标幻觉率 = 1 − 接地率（离线样例，非线上流量）；检出率测计分器是否抓住故意幻觉对照。
        </p>
      </div>
      <div class="actions">
        <label class="live">
          <input v-model="includeLive" type="checkbox" />
          含 live（需 API Key）
        </label>
        <button class="btn btn-primary" :disabled="loading" @click="run">
          {{ loading ? '评测中…' : '跑评测' }}
        </button>
      </div>
    </div>

    <p v-if="error" class="err">{{ error }}</p>

    <div v-if="loading && !metrics" class="cards">
      <div v-for="i in 5" :key="i" class="card sk" />
    </div>

    <div v-else-if="metrics" class="cards">
      <div class="card">
        <div class="k">意图准确率</div>
        <div class="v mono">{{ pct(metrics.intent_accuracy) }}</div>
        <div class="s">n={{ metrics.intent_n }}</div>
      </div>
      <div class="card">
        <div class="k">金标接地率</div>
        <div class="v mono">{{ pct(metrics.golden_groundedness) }}</div>
        <div class="s">n={{ metrics.golden_n }}</div>
      </div>
      <div class="card accent">
        <div class="k">金标幻觉率</div>
        <div class="v mono">{{ pct(metrics.golden_hallucination_rate) }}</div>
        <div class="s">= 1 − 接地率</div>
      </div>
      <div class="card">
        <div class="k">幻觉检出率</div>
        <div class="v mono">{{ pct(metrics.hallucination_detection_rate) }}</div>
        <div class="s">对照样例 n={{ metrics.adversarial_n }}</div>
      </div>
      <div class="card">
        <div class="k">Live 幻觉率</div>
        <div class="v mono">{{ pct(metrics.live_hallucination_rate) }}</div>
        <div class="s">n={{ metrics.live_n }}</div>
      </div>
    </div>

    <p v-if="note" class="note">{{ note }}</p>
    <p v-if="today" class="note mono">today={{ today }} · {{ generatedAt }}</p>

    <table v-if="results.length" class="table">
      <thead>
        <tr>
          <th>结果</th>
          <th>套件</th>
          <th>样例</th>
          <th>说明</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="r in results" :key="r.id">
          <td>
            <span v-if="r.skipped" class="tag skip">skip</span>
            <span v-else-if="r.pass" class="tag ok">pass</span>
            <span v-else class="tag bad">fail</span>
          </td>
          <td class="mono">{{ r.suite }}</td>
          <td>{{ r.title || r.id }}</td>
          <td class="detail">
            <template v-if="r.suite === 'intent'">
              expect={{ r.expect }} · actual={{ r.actual }}
            </template>
            <template v-else-if="r.reasons?.length">
              {{ r.reasons.join('；') }}
            </template>
            <template v-else-if="r.reason">{{ r.reason }}</template>
            <template v-else>—</template>
          </td>
        </tr>
      </tbody>
    </table>
    <div v-else-if="!loading" class="empty">点「跑评测」生成可引用的幻觉率 / 接地率数字。</div>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { runEval } from '../api'

const loading = ref(false)
const error = ref('')
const includeLive = ref(false)
const metrics = ref<Record<string, any> | null>(null)
const results = ref<any[]>([])
const note = ref('')
const today = ref('')
const generatedAt = ref('')

function pct(v: unknown) {
  if (v == null || typeof v !== 'number') return '—'
  return `${(v * 100).toFixed(1)}%`
}

async function run() {
  loading.value = true
  error.value = ''
  try {
    const data = await runEval({ include_live: includeLive.value })
    metrics.value = data.metrics || null
    results.value = data.results || []
    note.value = data.note || ''
    today.value = data.today || ''
    generatedAt.value = data.generated_at || ''
  } catch (e: any) {
    error.value = e?.response?.data?.detail || e?.message || String(e)
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  run()
})
</script>

<style scoped>
.head {
  display: flex;
  justify-content: space-between;
  gap: 16px;
  align-items: flex-start;
  margin-bottom: 16px;
  flex-wrap: wrap;
}

.cite {
  margin: 8px 0 0;
  font-size: 0.78rem;
  color: var(--muted);
  line-height: 1.45;
  max-width: 52rem;
}

.actions {
  display: flex;
  gap: 12px;
  align-items: center;
}

.live {
  display: flex;
  gap: 6px;
  align-items: center;
  color: var(--muted);
  font-size: 0.85rem;
}

.cards {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(140px, 1fr));
  gap: 10px;
  margin-bottom: 14px;
}

.card {
  border: 1px solid var(--line);
  border-radius: var(--radius);
  background: var(--bg-panel);
  padding: 12px;
  min-height: 88px;
}

.card.sk {
  background: linear-gradient(90deg, var(--bg-1) 25%, var(--bg-2) 50%, var(--bg-1) 75%);
  background-size: 200% 100%;
  animation: shimmer 1.2s infinite;
}

@keyframes shimmer {
  0% {
    background-position: 200% 0;
  }
  100% {
    background-position: -200% 0;
  }
}

.card.accent {
  border-color: rgba(98, 88, 232, 0.35);
  background: var(--accent-dim);
}

.k {
  color: var(--muted);
  font-size: 0.75rem;
  margin-bottom: 6px;
}

.v {
  font-size: 1.35rem;
  font-weight: 600;
}

.s {
  margin-top: 4px;
  color: var(--muted);
  font-size: 0.72rem;
}

.note {
  color: var(--muted);
  font-size: 0.82rem;
  margin: 0 0 10px;
}

.table {
  width: 100%;
  border-collapse: collapse;
  font-size: 0.84rem;
}

.table th,
.table td {
  border-bottom: 1px solid var(--line);
  padding: 8px 10px;
  text-align: left;
  vertical-align: top;
}

.table th {
  color: var(--muted);
  font-weight: 500;
}

.detail {
  color: var(--muted);
  max-width: 420px;
  word-break: break-word;
}

.tag {
  display: inline-block;
  padding: 1px 6px;
  border-radius: 4px;
  font-size: 0.72rem;
  font-family: var(--mono, ui-monospace, monospace);
}

.tag.ok {
  color: var(--success);
  border: 1px solid rgba(35, 134, 91, 0.35);
  background: var(--success-dim);
}

.tag.bad {
  color: var(--danger);
  border: 1px solid rgba(240, 113, 120, 0.35);
}

.tag.skip {
  color: var(--muted);
  border: 1px solid var(--line);
}

.empty {
  color: var(--muted);
  padding: 28px;
  text-align: center;
  border: 1px dashed var(--line);
  border-radius: var(--radius);
}
</style>
