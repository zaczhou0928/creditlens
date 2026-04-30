<script setup lang="ts">
import { ref } from 'vue'
import { api } from './api'
import type { QueryResult } from './types'
const username = ref('analyst'), password = ref('analyst123')
const query = ref('查询2026年9月各渠道放款金额')
const loggedIn = ref(api.hasSession()), busy = ref(false), error = ref('')
const result = ref<QueryResult | null>(null)
const sessionId = crypto.randomUUID()
async function login() {
  error.value = ''
  try { await api.login(username.value, password.value); loggedIn.value = true }
  catch (e) { error.value = e instanceof Error ? e.message : String(e) }
}
async function run() {
  busy.value = true; error.value = ''
  try { result.value = await api.query(query.value, {}, sessionId) }
  catch (e) { error.value = e instanceof Error ? e.message : String(e) }
  finally { busy.value = false }
}
</script>
<template>
  <main>
    <h1>CreditLens</h1><p>Retail credit analytics workspace</p>
    <form v-if="!loggedIn" @submit.prevent="login">
      <label>Username <input v-model="username" autocomplete="username" /></label>
      <label>Password <input v-model="password" type="password" autocomplete="current-password" /></label>
      <button>Sign in</button>
    </form>
    <form v-else @submit.prevent="run">
      <label>Question <textarea v-model="query" rows="3" /></label>
      <button :disabled="busy">{{ busy ? 'Running...' : 'Run query' }}</button>
    </form>
    <p v-if="error" role="alert">{{ error }}</p>
    <section v-if="result"><h2>{{ result.message }}</h2><pre>{{ JSON.stringify(result, null, 2) }}</pre></section>
  </main>
</template>
