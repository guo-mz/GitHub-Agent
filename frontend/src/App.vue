<template>
  <div v-if="hideShell" class="login-shell">
    <RouterView />
  </div>
  <div v-else class="app-shell">
    <AppSidebar :mobile-open="mobileOpen" @close="mobileOpen = false" />
    <div class="main-col">
      <AppTopbar @menu="mobileOpen = true" />
      <main class="main">
        <RouterView />
      </main>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { RouterView, useRoute } from 'vue-router'
import AppSidebar from './components/layout/AppSidebar.vue'
import AppTopbar from './components/layout/AppTopbar.vue'

const route = useRoute()
const mobileOpen = ref(false)
const hideShell = computed(() => Boolean(route.meta.hideShell))
</script>

<style scoped>
.login-shell {
  min-height: 100vh;
  min-height: 100dvh;
  background: var(--bg-page);
}

.app-shell {
  display: flex;
  height: 100vh;
  height: 100dvh;
  overflow: hidden;
  background: var(--bg-page);
}

.main-col {
  flex: 1;
  min-width: 0;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.main {
  flex: 1;
  min-width: 0;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  padding: 16px 20px 14px;
}

@media (max-width: 900px) {
  .app-shell {
    height: auto;
    min-height: 100dvh;
    overflow: auto;
  }

  .main-col {
    overflow: visible;
  }

  .main {
    overflow: visible;
    padding: 12px;
  }
}
</style>
