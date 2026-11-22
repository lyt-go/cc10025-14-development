<template>
  <section class="page" :data-module="meta.key">
    <header class="page-head">
      <div>
        <h2>{{ meta.name }}</h2>
        <p class="page-desc">{{ meta.desc }}</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">
          登记{{ meta.entity }}
        </button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">
          导出{{ meta.name }}清单
        </button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <p class="status-legend">
      <span v-for="item in statusSummary" :key="item.status" class="legend-item">
        {{ item.status }}：{{ item.count }}
      </span>
    </p>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :data-testid="`filter-${field}`" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" data-testid="query" type="submit">查询</button>
      <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in meta.fields" :key="column">{{ column }}</th>
          <th>当前状态</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`" :data-status="String(row.status)"
          :class="{ 'row-selected': isReclaim && selectedId === Number(row.id) }"
          @click="isReclaim && selectRow(Number(row.id))">
          <td v-for="column in meta.fields" :key="column">{{ row[column] ?? '—' }}</td>
          <td>{{ row.status }}</td>
          <td class="row-actions">
            <button
              v-for="action in meta.actions"
              :key="action"
              class="link"
              type="button"
              :data-testid="`action-${row.id}-${action}`"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="meta.fields.length + 2" class="empty-state">
            暂无{{ meta.name }}数据，可先登记{{ meta.entity }}
          </td>
        </tr>
      </tbody>
    </table>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记{{ meta.entity }}</h3>
        <label v-for="field in meta.fields" :key="field" class="create-field">
          <span>{{ field }}</span>
          <input v-model="createValues[field]" :data-testid="`create-${field}`" required />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showCreate = false">取消</button>
          <button class="btn primary" data-testid="submit-create" type="submit" :disabled="creating">
            {{ creating ? '提交中…' : '确认登记' }}
          </button>
        </div>
      </form>
    </div>

    <div v-if="isReclaim && (eligibilityRows.length || receipts.length)" class="reclaim-audit">
      <section class="audit-block">
        <h3>回收资格明细 <span class="muted">（只追加，不随规则修改改写）</span></h3>
        <table class="data-table">
          <thead>
            <tr>
              <th>资格编号</th><th>分区编号</th><th>资格状态</th><th>到期时间</th>
              <th>规则版本</th><th>保留天数</th><th>评估用途</th><th>复核层级</th><th>阻断原因</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="item in eligibilityRows" :key="item.资格编号 || `${item.分区编号}-${item.评估批次}`">
              <td>{{ item.资格编号 || '—' }}</td>
              <td>{{ item.分区编号 || '—' }}</td>
              <td>{{ item.资格状态 }}</td>
              <td>{{ (item.到期时间 || '—').replace('T', ' ').slice(0, 16) }}</td>
              <td>{{ item.规则版本 || '—' }}</td>
              <td>{{ item.保留天数 ?? '—' }}</td>
              <td>{{ item.评估用途 }}</td>
              <td>{{ item.复核层级 || '—' }}</td>
              <td>{{ item.阻断原因 }}</td>
            </tr>
          </tbody>
        </table>
      </section>
      <section class="audit-block">
        <h3>执行回执 <span class="muted">（本次释放范围与因豁免/待复核未处置的范围）</span></h3>
        <table class="data-table">
          <thead>
            <tr>
              <th>回执编号</th><th>回执状态</th><th>评估批次</th><th>执行时间</th>
              <th>释放范围</th><th>未处置范围</th><th>规则版本</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="item in receipts" :key="item.回执编号">
              <td>{{ item.回执编号 }}</td>
              <td>{{ item.回执状态 }}</td>
              <td>{{ item.评估批次 }}</td>
              <td>{{ (item.执行时间 || '').replace('T', ' ').slice(0, 16) }}</td>
              <td>{{ item.释放范围.join('、') || '—' }}</td>
              <td>
                <span v-for="(ids, label) in item.未处置范围" :key="label">
                  <template v-if="ids.length">{{ label }}：{{ ids.join('、') }}；</template>
                </span>
              </td>
              <td>{{ item.规则版本清单.join('、') || '—' }}</td>
            </tr>
          </tbody>
        </table>
      </section>
    </div>

    <footer class="page-foot">
      <span>共 {{ total }} 条{{ meta.name }}记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import {
  createEntry,
  downloadEntries,
  listEligibility,
  listEntries,
  listReceipts,
  moduleMeta,
  runAction as applyAction,
} from '@/api/backend-service'
import type { EligibilityRow, ReclaimReceipt } from '@/api/backend-service'
import type { EntryRow } from '@/data/types'

const props = defineProps<{ moduleKey: string }>()
const meta = moduleMeta(props.moduleKey)
const isReclaim = meta.key === 'reclaim_job'
const eligibilityRows = ref<EligibilityRow[]>([])
const receipts = ref<ReclaimReceipt[]>([])
const rows = ref<EntryRow[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = meta.fields.slice(0, 3)
const showCreate = ref(false)
const creating = ref(false)
const selectedId = ref(0)
const createValues = ref<Record<string, string>>({})
const stats = computed(() => [
  { label: meta.metrics[0], value: total.value },
  { label: meta.metrics[1], value: rows.value.filter((row) => row.status === meta.statuses[1]).length },
  {
    label: meta.metrics[2],
    value: rows.value.filter((row) => row.status === meta.statuses[meta.statuses.length - 1]).length,
  },
])
const statusSummary = computed(() =>
  meta.statuses.map((status) => ({
    status,
    count: rows.value.filter((row) => String(row.status) === status).length,
  })),
)

function resetFilters() {
  filters.value = {}
  void reload()
}

async function exportRows() {
  errorMessage.value = ''
  try {
    await downloadEntries(meta.key, filters.value)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

function openCreate() {
  createValues.value = Object.fromEntries(meta.fields.map((field) => [field, '']))
  errorMessage.value = ''
  showCreate.value = true
}

async function submitCreate() {
  creating.value = true
  errorMessage.value = ''
  try {
    const result = await createEntry(meta.key, createValues.value)
    if (!result.ok) throw new Error(result.message)
    showCreate.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记失败'
  } finally {
    creating.value = false
  }
}

async function runAction(action: string, row: EntryRow) {
  errorMessage.value = ''
  try {
    const result = await applyAction(meta.key, Number(row.id), action)
    if (!result.ok) throw new Error(result.message)
    if (isReclaim) selectedId.value = Number(row.id)
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '操作失败'
  }
}

function selectRow(id: number) {
  selectedId.value = id
  void loadAudit(id)
}

async function loadAudit(id: number) {
  if (!isReclaim || !id) {
    eligibilityRows.value = []
    receipts.value = []
    return
  }
  try {
    const [eligibility, receiptItems] = await Promise.all([
      listEligibility(id),
      listReceipts(id),
    ])
    eligibilityRows.value = eligibility
    receipts.value = receiptItems
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '资格明细读取失败'
  }
}

async function reload() {
  errorMessage.value = ''
  try {
    const payload = await listEntries(meta.key, filters.value)
    rows.value = payload.items
    total.value = payload.total
    if (isReclaim) {
      const ids = rows.value.map((row) => Number(row.id))
      if (!selectedId.value || !ids.includes(selectedId.value)) {
        selectedId.value = ids[0] ?? 0
      }
      await loadAudit(selectedId.value)
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '业务列表读取失败'
  }
}

onMounted(reload)
</script>
