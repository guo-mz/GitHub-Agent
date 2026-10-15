<template>
  <section class="repo page-scroll">
    <div class="page-header">
      <div>
        <h1 class="page-title">GitHub 仓库</h1>
        <p class="page-hint">
          拉取仓库树；索引后写入混合 RAG；可构建代码依赖图谱（GraphRAG）做跨文件问答。
        </p>
      </div>
      <RouterLink class="btn btn-primary" to="/">在此仓库新建任务</RouterLink>
    </div>

    <div class="panel">
      <div class="row primary-row">
        <input v-model="ownerRepo" class="field mono grow" placeholder="owner/repo" />
        <input v-model="branch" class="field mono branch" placeholder="main" />
        <button class="btn btn-primary" :disabled="loading" @click="load">
          {{ loading ? '加载中…' : '加载仓库树' }}
        </button>
        <button class="btn" :disabled="loading" @click="bootstrap">初始化空仓库</button>
      </div>
      <div class="row secondary-row">
        <span class="group-label">增强</span>
        <button class="btn" :disabled="loading" @click="indexReadme">索引 README → RAG</button>
        <button class="btn" :disabled="loading" @click="buildGraph">构建代码图谱</button>
      </div>
      <div class="meta-row">
        <input
          v-model="filter"
          class="field"
          placeholder="过滤路径，例如 README 或 src/"
          :disabled="!nodes.length"
        />
        <span class="count mono">{{ filtered.length }} / {{ nodes.length }}</span>
      </div>
    </div>

    <p v-if="notice" class="ok">{{ notice }}</p>
    <p v-if="error" class="err">{{ error }}</p>

    <GraphCanvas
      v-if="graphNodes.length || graphBuilt"
      :nodes="graphNodes"
      :edges="graphEdges"
      :truncated="graphTruncated"
    />
    <p v-else class="graph-hint">构建代码图谱后将在此展示依赖画布。</p>

    <ul v-if="filtered.length" class="tree mono">
      <li v-for="n in filtered" :key="n.path" :class="n.type">
        <span class="badge">{{ n.type === 'dir' ? 'dir' : 'file' }}</span>
        <span class="path">{{ n.path }}</span>
        <span v-if="n.size != null" class="size">{{ n.size }} B</span>
      </li>
    </ul>
    <div v-else-if="!loading" class="empty">
      {{ nodes.length ? '没有匹配的路径' : '暂无数据，先加载仓库或初始化 README' }}
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'
import GraphCanvas from '../components/GraphCanvas.vue'
import {
  buildRepoGraph,
  fetchRepoGraphViz,
  fetchRepoTree,
  http,
  indexRepoFile,
} from '../api'
import { useChatStore } from '../stores/chat'

const chat = useChatStore()
const ownerRepo = ref(chat.repo || 'guo-mz/-')
const branch = ref(ownerRepo.value.includes('Hello-World') ? 'master' : 'main')
const nodes = ref<{ path: string; type: string; sha?: string; size?: number }[]>([])
const loading = ref(false)
const error = ref('')
const notice = ref('')
const filter = ref('')
const graphNodes = ref<{ id: string; label: string; type?: string; path?: string }[]>([])
const graphEdges = ref<{ source: string; target: string; rel?: string }[]>([])
const graphTruncated = ref(false)
const graphBuilt = ref(false)

const filtered = computed(() => {
  const q = filter.value.trim().toLowerCase()
  const list = q ? nodes.value.filter((n) => n.path.toLowerCase().includes(q)) : nodes.value
  return list.slice(0, 500)
})

function parseOwnerRepo() {
  const [owner, repo] = ownerRepo.value.split('/')
  if (!owner || !repo) throw new Error('请输入 owner/repo')
  return { owner, repo }
}

function errText(e: any): string {
  const d = e?.response?.data?.detail
  const raw = typeof d === 'string' ? d : ''
  if (raw.includes('not accessible by personal access token') || raw.includes('403')) {
    return 'Token 权限不够（缺 Contents 读写）。请重新生成细粒度令牌，勾选该仓库的 Contents: Read and write。'
  }
  if (typeof d === 'string') return d
  if (e?.code === 'ERR_NETWORK') {
    const base = import.meta.env.VITE_API_BASE || 'http://127.0.0.1:8000'
    return `连不上后端，请确认 ${base} 已启动`
  }
  return e?.message || String(e)
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const { owner, repo } = parseOwnerRepo()
    chat.setRepo(`${owner}/${repo}`)
    const data = await fetchRepoTree(owner, repo, branch.value || 'main')
    nodes.value = data.nodes || []
    if (!nodes.value.length) error.value = '仓库里暂时没有文件'
  } catch (e: any) {
    error.value = errText(e)
    nodes.value = []
  } finally {
    loading.value = false
  }
}

async function bootstrap() {
  loading.value = true
  error.value = ''
  try {
    const { owner, repo } = parseOwnerRepo()
    await http.post(`/api/repos/${owner}/${repo}/bootstrap`)
    await load()
  } catch (e: any) {
    error.value = errText(e)
  } finally {
    loading.value = false
  }
}

async function indexReadme() {
  loading.value = true
  error.value = ''
  notice.value = ''
  try {
    const { owner, repo } = parseOwnerRepo()
    const data = await indexRepoFile(owner, repo, 'README.md', branch.value || 'main')
    notice.value =
      data.detail ||
      `已索引 README.md → ${data.chunks ?? 0} 个 chunks（${data.rag_mode || 'rag'}），可在对话里问「解释 README」`
  } catch (e: any) {
    const d = e?.response?.data?.detail
    error.value = typeof d === 'string' ? d : errText(e)
  } finally {
    loading.value = false
  }
}

async function loadGraphViz() {
  try {
    const { owner, repo } = parseOwnerRepo()
    const viz = await fetchRepoGraphViz(owner, repo, 60)
    graphNodes.value = viz.nodes || []
    graphEdges.value = viz.edges || []
    graphTruncated.value = !!viz.truncated
    if ((viz.nodes || []).length) graphBuilt.value = true
  } catch {
    graphNodes.value = []
    graphEdges.value = []
    graphTruncated.value = false
  }
}

async function buildGraph() {
  loading.value = true
  error.value = ''
  notice.value = ''
  try {
    const { owner, repo } = parseOwnerRepo()
    const data = await buildRepoGraph(owner, repo, branch.value || 'main')
    const st = data.stats || {}
    notice.value =
      data.detail ||
      `GraphRAG 完成：${data.indexed ?? 0} 文件，节点 ${st.nodes ?? 0}，边 ${st.edges ?? 0}。对话可问「谁依赖 rag_service」`
    graphBuilt.value = true
    await loadGraphViz()
  } catch (e: any) {
    const d = e?.response?.data?.detail
    error.value = typeof d === 'string' ? d : errText(e)
  } finally {
    loading.value = false
  }
}

onMounted(async () => {
  await load()
  await loadGraphViz()
})
</script>

<style scoped>
.panel {
  border: 1px solid var(--line);
  border-radius: var(--radius);
  background: var(--bg-panel);
  padding: 14px;
  margin-bottom: 14px;
}

.row {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  margin-bottom: 10px;
}

.secondary-row {
  align-items: center;
  padding-top: 2px;
  border-top: 1px dashed var(--line);
  margin-top: 2px;
}

.group-label {
  font-size: 0.72rem;
  color: var(--muted);
  letter-spacing: 0.04em;
  text-transform: uppercase;
  margin-right: 4px;
}

.graph-hint {
  color: var(--muted);
  font-size: 0.84rem;
  margin: 0 0 14px;
  padding: 12px 14px;
  border: 1px dashed var(--line);
  border-radius: var(--radius);
}

.meta-row {
  display: flex;
  gap: 10px;
  align-items: center;
}

.grow {
  flex: 1 1 220px;
}

.branch {
  max-width: 140px;
}

.count {
  color: var(--muted);
  white-space: nowrap;
  font-size: 0.78rem;
}

.tree {
  list-style: none;
  margin: 0;
  padding: 0;
  border: 1px solid var(--line);
  border-radius: var(--radius);
  max-height: 60vh;
  overflow: auto;
  background: var(--bg-panel);
  box-shadow: var(--shadow-sm);
}

.tree li {
  display: grid;
  grid-template-columns: 44px 1fr auto;
  gap: 8px;
  align-items: center;
  padding: 9px 12px;
  border-bottom: 1px solid var(--line);
  font-size: 0.82rem;
}

.tree li:hover {
  background: var(--accent-dim);
}

.badge {
  color: var(--accent);
  font-size: 0.7rem;
  text-transform: uppercase;
  letter-spacing: 0.04em;
}

.path {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.size {
  color: var(--muted);
  font-size: 0.72rem;
}

.err {
  color: var(--danger);
  margin: 0 0 12px;
}

.empty {
  color: var(--muted);
  padding: 28px;
  text-align: center;
  border: 1px dashed var(--line);
  border-radius: var(--radius);
}
</style>
