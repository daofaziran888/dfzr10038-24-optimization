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
          <th v-for="column in visibleFields" :key="column">{{ column }}</th>
          <th>当前状态</th>
          <th>可执行动作</th>
          <th v-if="gateEnabled">检查依据 / 成稿进度</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`" :data-status="String(row.status)">
          <td v-for="column in visibleFields" :key="column">{{ row[column] ?? '—' }}</td>
          <td>{{ row.status }}</td>
          <td class="row-actions">
            <button
              v-for="action in rowActions()"
              :key="action"
              class="link"
              type="button"
              :data-testid="`action-${row.id}-${action}`"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
          <td v-if="gateEnabled" class="gate-cell" :data-testid="`gate-${row.id}`">
            <template v-if="gateFor(row)">
              <span class="gate-fact">{{ gateFor(row)?.fact?.['检查事实'] ?? '—' }}</span>
              <span v-if="gateFor(row)?.fact?.['成稿进度']" class="gate-progress">
                {{ gateFor(row)?.fact?.['成稿进度'] }}
              </span>
              <span v-if="gateFor(row)?.released?.length" class="gate-released">
                实际接入：{{ gateFor(row)?.released?.join('、') }}
              </span>
              <span v-if="gateFor(row)?.blocked?.length" class="gate-blocked">
                仍阻断：{{ gateFor(row)?.blocked?.join('、') }}
              </span>
            </template>
            <span v-else>—</span>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="visibleFields.length + (gateEnabled ? 3 : 2)" class="empty-state">
            暂无{{ meta.name }}数据，可先登记{{ meta.entity }}
          </td>
        </tr>
      </tbody>
    </table>

    <p v-if="lastGateMessage" class="gate-message" data-testid="gate-message">
      {{ lastGateMessage }}
    </p>

    <div v-if="actionPrompt" class="modal-backdrop" data-testid="action-modal">
      <form class="modal" @submit.prevent="submitActionParams">
        <h3>{{ pendingAction }}</h3>
        <p class="page-desc">{{ actionHint }}</p>
        <label v-for="field in actionFields" :key="field" class="create-field">
          <span>{{ field }}</span>
          <input v-model="actionParams[field]" :data-testid="`param-${field}`" />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="actionPrompt = false">取消</button>
          <button class="btn primary" data-testid="submit-action" type="submit">
            提交{{ pendingAction }}
          </button>
        </div>
      </form>
    </div>

    <div v-if="leavePrompt" class="modal-backdrop" data-testid="leave-modal">
      <form class="modal" @submit.prevent="submitLeaveReport">
        <h3>本次阻断的成稿进度</h3>
        <p class="page-desc">
          {{ leaveHint }}
        </p>
        <label v-for="field in leaveFields" :key="field" class="create-field">
          <span>{{ field }}</span>
          <input v-model="leaveValues[field]" :data-testid="`leave-${field}`" />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="leavePrompt = false">取消</button>
          <button class="btn primary" data-testid="submit-leave" type="submit">提交恢复申请</button>
        </div>
      </form>
    </div>

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

    <footer class="page-foot">
      <span>共 {{ total }} 条{{ meta.name }}记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'

import {
  createEntry,
  downloadEntries,
  listEntries,
  loadQualityGate,
  moduleMeta,
  runAction as applyAction,
} from '@/api/backend-service'
import type { EntryRow, QualityGateView } from '@/data/types'

const props = defineProps<{ moduleKey: string }>()
const meta = moduleMeta(props.moduleKey)
const rows = ref<EntryRow[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})
const gates = ref<Record<string, QualityGateView>>({})
const lastGateMessage = ref('')
const leavePrompt = ref(false)
const leaveRuleId = ref(0)
const leaveFactId = ref('')
const leaveValues = ref<Record<string, string>>({})
const leaveFields = ['申请分区', '分区证据']

const gateEnabled = computed(() => ['quality_rule', 'report_task'].includes(meta.key))
const gateFields = ['检查事实', '成稿进度', '实际接入范围', '阻断分区', '报告来源']
const visibleFields = computed(() =>
  gateEnabled.value ? meta.fields.filter((field) => !gateFields.includes(field)) : meta.fields,
)
const filterFields = computed(() => visibleFields.value.slice(0, 3))
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

// 质量阻断相关动作：在模块原有动作之外补齐台账侧入口。
const EXTRA_ACTIONS: Record<string, string[]> = {
  quality_rule: ['修复分区', '补齐证据', '申请恢复', '重试写入', '完成成稿', '核对进度', '发起下一批检查'],
  report_task: ['重试写入', '完成成稿', '核对进度'],
}

function rowActions(): string[] {
  if (!gateEnabled.value) return meta.actions
  const seen = new Set<string>()
  return [...meta.actions, ...(EXTRA_ACTIONS[meta.key] ?? [])].filter((action) => {
    if (seen.has(action)) return false
    seen.add(action)
    return true
  })
}

function gateFor(row: EntryRow): QualityGateView | null {
  return gates.value[String(row.id)] ?? null
}

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

const pendingAction = ref('')
const actionRow = ref<EntryRow | null>(null)
const actionPrompt = ref(false)
const actionParams = ref<Record<string, string>>({})
const leaveHint = ref('')

const ACTION_FIELDS: Record<string, string[]> = {
  修复分区: ['分区'],
  补齐证据: ['分区证据'],
  申请恢复: ['申请分区', '分区证据'],
  重试写入: [],
  完成成稿: [],
  核对进度: [],
  发起下一批检查: ['数据批', '检查轮次', '拦截分区', '分区证据'],
}

const ACTION_HINTS: Record<string, string> = {
  修复分区: '只把源数据已修好的分区标记为可证，解除阻断仍需恢复申请。',
  补齐证据: '为当前这一笔阻断补记分区证据，不会重新认领草稿。',
  申请恢复: '只有取得可证依据的分区才会恢复接入；缺证分区继续保持阻断。',
  重试写入: '从原成稿动作按同一份检查事实重试，不会重新认领草稿。',
  完成成稿: '成稿材料齐备后，由特征报告结束本次阻断的成稿。',
  核对进度: '按检查事实与报告的关联核对成稿进度。',
  发起下一批检查: '修复源数据后的新检查会得到独立的检查事实与独立报告。',
}

const actionFields = computed(() => (pendingAction.value ? ACTION_FIELDS[pendingAction.value] ?? [] : []))
const actionHint = computed(() => ACTION_HINTS[pendingAction.value] ?? '')

function runAction(action: string, row: EntryRow) {
  errorMessage.value = ''
  lastGateMessage.value = ''
  pendingAction.value = action
  actionRow.value = row
  actionParams.value = {}
  const fields = ACTION_FIELDS[action]
  if (!fields) {
    // 非台账动作直接执行。
    void executeAction(action, row, {})
    return
  }
  const gate = gateFor(row)
  if (action === '申请恢复') {
    // 默认申请所有仍有可验证证据的分区；缺证分区即使填了也会被台账退回。
    const evidence = (gate?.evidence ?? {}) as Record<string, string>
    actionParams.value['申请分区'] = Object.keys(evidence).join(';')
  }
  if (action === '发起下一批检查') {
    actionParams.value['数据批'] = ''
    actionParams.value['检查轮次'] = ''
  }
  actionPrompt.value = fields.length > 0
  if (!actionPrompt.value) void executeAction(action, row, {})
}

async function submitActionParams() {
  if (!pendingAction.value || !actionRow.value) return
  const action = pendingAction.value
  const row = actionRow.value
  const params = { ...actionParams.value }
  actionPrompt.value = false
  await executeAction(action, row, params)
}

async function executeAction(action: string, row: EntryRow, params: Record<string, string>) {
  const payload = { ...params }
  if (row['检查事实']) payload['检查事实编号'] = String(row['检查事实'])
  try {
    const result = await applyAction(meta.key, Number(row.id), action, payload)
    lastGateMessage.value = result.gate?.message ?? result.message
    if (!result.ok) throw new Error(result.message)
    if (action === '申请恢复') {
      const fact = (result.gate?.fact ?? {}) as Record<string, unknown>
      const missing = (fact['缺证分区'] as string[] | undefined) ?? []
      if (missing.length) {
        leaveRuleId.value = Number(row.id)
        leaveFactId.value = String(fact['检查事实'] ?? row['检查事实'] ?? '')
        leaveValues.value = { 申请分区: payload['申请分区'] ?? '', 分区证据: '' }
        leaveHint.value = `报告尚未补齐（缺 ${missing.join('、')} 的证据）。${
          result.gate?.recovery?.['当班可接入'] ? '可证分区当班即可接入；' : ''
        }缺证分区留待下班继续办理。`
        leavePrompt.value = true
      }
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '操作失败'
  }
}

async function submitLeaveReport() {
  const params: Record<string, string> = { 申请分区: leaveValues.value['申请分区'] ?? '' }
  if (leaveValues.value['分区证据']) params['分区证据'] = leaveValues.value['分区证据']
  if (leaveFactId.value) params['检查事实编号'] = leaveFactId.value
  try {
    const result = await applyAction('quality_rule', leaveRuleId.value, '申请恢复', params)
    lastGateMessage.value = result.gate?.message ?? result.message
    if (!result.ok) throw new Error(result.message)
    leavePrompt.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '恢复申请提交失败'
  }
}

async function reload() {
  errorMessage.value = ''
  try {
    const payload = await listEntries(meta.key, filters.value)
    rows.value = payload.items
    total.value = payload.total
    if (gateEnabled.value) await reloadGates()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '业务列表读取失败'
  }
}

async function reloadGates() {
  const next: Record<string, QualityGateView> = {}
  await Promise.all(rows.value.map(async (row) => {
    try {
      const view = meta.key === 'report_task'
        ? await loadQualityGate({ reportId: Number(row.id) })
        : await loadQualityGate({ ruleId: Number(row.id) })
      if (view.ok !== false) next[String(row.id)] = view
    } catch {
      // 单行检查依据读取失败不阻塞整张表。
    }
  }))
  gates.value = next
}

watch(() => props.moduleKey, () => {
  rows.value = []
  gates.value = {}
  lastGateMessage.value = ''
  void reload()
})

onMounted(reload)
</script>
