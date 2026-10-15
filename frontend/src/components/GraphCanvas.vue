<template>
  <div class="graph-wrap">
    <div class="graph-head">
      <span class="title">依赖图谱</span>
      <span class="meta mono">
        {{ nodes.length }} 节点 · {{ edges.length }} 边
        <template v-if="truncated"> · 已截断</template>
      </span>
    </div>
    <svg
      v-if="layoutNodes.length"
      class="canvas"
      :viewBox="`0 0 ${W} ${H}`"
      role="img"
      aria-label="代码依赖图谱"
    >
      <defs>
        <marker
          id="arrow"
          viewBox="0 0 10 10"
          refX="9"
          refY="5"
          markerWidth="6"
          markerHeight="6"
          orient="auto-start-reverse"
        >
          <path d="M 0 0 L 10 5 L 0 10 z" fill="rgba(98,88,232,0.45)" />
        </marker>
      </defs>
      <line
        v-for="(e, i) in layoutEdges"
        :key="'e' + i"
        :x1="e.x1"
        :y1="e.y1"
        :x2="e.x2"
        :y2="e.y2"
        class="edge"
        marker-end="url(#arrow)"
      />
      <g v-for="n in layoutNodes" :key="n.id" class="node" :class="n.type">
        <circle :cx="n.x" :cy="n.y" :r="n.r" />
        <text :x="n.x" :y="n.y + n.r + 11">{{ n.label }}</text>
      </g>
    </svg>
    <div v-else class="empty">暂无图谱数据，点「构建代码图谱」后展示</div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'

export interface GraphNode {
  id: string
  label: string
  type?: string
  path?: string
}
export interface GraphEdge {
  source: string
  target: string
  rel?: string
}

const props = defineProps<{
  nodes: GraphNode[]
  edges: GraphEdge[]
  truncated?: boolean
}>()

const W = 720
const H = 420

const layoutNodes = computed(() => {
  const list = props.nodes || []
  const n = list.length
  if (!n) return [] as Array<GraphNode & { x: number; y: number; r: number }>
  const cx = W / 2
  const cy = H / 2
  const R = Math.min(W, H) * 0.36
  return list.map((node, i) => {
    const angle = (Math.PI * 2 * i) / n - Math.PI / 2
    const ring = node.type === 'file' ? R : R * 0.72
    return {
      ...node,
      x: cx + Math.cos(angle) * ring,
      y: cy + Math.sin(angle) * ring,
      r: node.type === 'file' ? 8 : 6,
    }
  })
})

const layoutEdges = computed(() => {
  const byId = new Map(layoutNodes.value.map((n) => [n.id, n]))
  return (props.edges || [])
    .map((e) => {
      const a = byId.get(e.source)
      const b = byId.get(e.target)
      if (!a || !b) return null
      return { x1: a.x, y1: a.y, x2: b.x, y2: b.y, rel: e.rel }
    })
    .filter(Boolean) as Array<{ x1: number; y1: number; x2: number; y2: number; rel?: string }>
})
</script>

<style scoped>
.graph-wrap {
  border: 1px solid var(--line);
  border-radius: var(--radius);
  background: var(--bg-panel);
  margin-bottom: 14px;
  overflow: hidden;
  box-shadow: var(--shadow-sm);
}

.graph-head {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  padding: 10px 14px;
  border-bottom: 1px solid var(--line);
}

.title {
  font-weight: 600;
  font-size: 0.92rem;
}

.meta {
  color: var(--muted);
  font-size: 0.72rem;
}

.canvas {
  display: block;
  width: 100%;
  height: auto;
  max-height: 420px;
}

.edge {
  stroke: rgba(98, 88, 232, 0.28);
  stroke-width: 1.2;
}

.node circle {
  fill: rgba(98, 88, 232, 0.14);
  stroke: var(--accent);
  stroke-width: 1.4;
}

.node.file circle {
  fill: rgba(15, 118, 110, 0.12);
  stroke: var(--teal);
}

.node text {
  fill: var(--muted);
  font-size: 9px;
  text-anchor: middle;
  font-family: var(--mono, ui-monospace, monospace);
  pointer-events: none;
}

.empty {
  color: var(--muted);
  text-align: center;
  padding: 36px 16px;
  font-size: 0.88rem;
}
</style>
