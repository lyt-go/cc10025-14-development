import { request } from './client'
import { moduleMeta } from '@/data/modules'
import type { ActionResult, EntryRow, OverviewResult, PageResult } from '@/data/types'

export { moduleMeta }

export async function listEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<PageResult> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}?${query}`)
  if (!response.ok) throw new Error('业务数据读取失败')
  return await response.json()
}

export async function createEntry(
  key: string,
  values: Record<string, string>,
): Promise<ActionResult> {
  const response = await request(`/api/${key}`, {
    method: 'POST',
    body: JSON.stringify({ values }),
  })
  return await response.json()
}

export async function runAction(
  key: string,
  id: number,
  action: string,
): Promise<ActionResult> {
  const response = await request(`/api/${key}/${id}/actions`, {
    method: 'POST',
    body: JSON.stringify({ action }),
  })
  return await response.json()
}

export async function downloadEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<void> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}/export?${query}`)
  if (!response.ok) throw new Error('业务数据导出失败')
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${key}.csv`
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}

export async function loadOverview(): Promise<OverviewResult> {
  const response = await request('/api/overview')
  if (!response.ok) throw new Error('运营概览读取失败')
  return await response.json()
}

export type EligibilityRow = {
  id: number
  资格编号: string
  分区编号: string
  分区键: string
  资格状态: string
  是否可回收: boolean
  到期时间: string
  规则编号: string
  规则版本: string
  保留天数: number | null
  评估用途: string
  评估批次: string
  复核层级: string
  阻断原因: string
}

export type ReclaimReceipt = {
  回执编号: string
  回执状态: string
  评估批次: string
  执行时间: string
  释放范围: string[]
  未处置范围: Record<string, string[]>
  规则版本清单: string[]
  [key: string]: unknown
}

export async function listEligibility(id: number): Promise<EligibilityRow[]> {
  const response = await request(`/api/reclaim_job/${id}/eligibility`)
  if (!response.ok) throw new Error('回收资格明细读取失败')
  return (await response.json()).items
}

export async function listReceipts(id: number): Promise<ReclaimReceipt[]> {
  const response = await request(`/api/reclaim_job/${id}/receipts`)
  if (!response.ok) throw new Error('回收回执读取失败')
  return (await response.json()).items
}
