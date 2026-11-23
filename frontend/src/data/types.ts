/** 纯前端数据层的公共类型：与全栈版后端返回的结构保持一致，换回后端时页面不用改。 */

export type EntryRow = {
  id: number
  status: string
  pending: boolean
  abnormal: boolean
  [field: string]: string | number | boolean
}

export type ModuleMeta = {
  key: string
  name: string
  entity: string
  desc: string
  fields: string[]
  statuses: string[]
  actions: string[]
  actionTargets: Record<string, string>
  metrics: string[]
}

export type PageResult = {
  items: EntryRow[]
  total: number
  page: number
  size: number
}

export type ActionResult = {
  ok: boolean
  message: string
  gate?: GateResult | null
}

export type GateResult = {
  ok: boolean
  message: string
  fact?: Record<string, unknown>
  report?: Record<string, unknown> | null
  recovery?: Record<string, unknown>
  reused?: boolean
  linked?: boolean
  scope?: string[]
  partitions?: string[]
}

export type QualityGateView = {
  ok?: boolean
  fact?: Record<string, unknown>
  report?: Record<string, unknown>
  evidence?: Record<string, string>
  material?: Record<string, string>
  blocked?: string[]
  released?: string[]
  message?: string
}

export type OverviewResult = {
  cards: { label: string; value: number }[]
  modules: { name: string; created: number; pending: number; abnormal: number }[]
}
