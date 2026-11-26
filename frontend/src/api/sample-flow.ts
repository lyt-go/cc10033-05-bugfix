import { request } from './client'

/**
 * Schema匹配 → 质量抽检 → 数据导出 样本链路专用接口。
 * 与后端 app/routers/sample_flow.py 一一对应。
 */

export type PreviewField = {
  field: string
  value: unknown
  source: string
  similarity: number
  via: 'manual_mapping' | 'name_fallback'
}

export type SampleFieldView = {
  field: string
  value: unknown
  source: string
  confirmed: boolean
  confirmed_by: string | null
  confirmed_at: string | null
  passed: boolean | null
}

export type SampleView = {
  sample_id: string
  batch: string
  version: number
  config_version: number
  status: string
  claimed_by: string | null
  claimed_quality_id: number | null
  superseded_by: string | null
  created_at: string
  note: string
  fields: SampleFieldView[]
  progress: { total: number; confirmed: number; passed: number }
}

export type ReviewDetail = {
  quality_check_id: number
  任务编号: string
  batch: string
  status: string
  抽检结论: string
  held_sample: SampleView | null
  latest_sample: SampleView | null
  config_corrected: boolean
  review_log: { action: string; sample_id?: string; field?: string }[]
}

async function postJson<T>(path: string, body: Record<string, unknown>): Promise<T> {
  const response = await request(path, { method: 'POST', body: JSON.stringify(body) })
  return (await response.json()) as T
}

async function getJson<T>(path: string): Promise<T> {
  const response = await request(path)
  if (!response.ok) throw new Error(`接口返回 ${response.status}`)
  return (await response.json()) as T
}

export function defineMapping(schemaId: number, mapping: Record<string, string>) {
  return postJson<{ ok: boolean; config_version: number; mapping: Record<string, string> }>(
    `/api/sample-flow/schema/${schemaId}/mapping`,
    { mapping },
  )
}

export function previewMatch(schemaId: number, sample: Record<string, unknown>) {
  return postJson<{ ok: boolean; preview: {
    config_version: number
    sample_keys: string[]
    matched: Record<string, PreviewField>
    missing: string[]
    previewed_at: string
  } }>(`/api/sample-flow/schema/${schemaId}/preview`, { sample })
}

export function submitSample(
  schemaId: number,
  batch: string,
  sample: Record<string, unknown>,
  note = '',
) {
  return postJson<{ ok: boolean; sample: SampleView & { raw_sample: Record<string, unknown> } }>(
    `/api/sample-flow/schema/${schemaId}/submit`,
    { batch, sample, note },
  )
}

export function availableSamples(qualityId: number) {
  return getJson<{ ok: boolean; held_sample_id: string | null; latest_sample_id: string | null; samples: SampleView[] }>(
    `/api/sample-flow/quality/${qualityId}/samples`,
  )
}

export function claimSample(qualityId: number, sampleId: string, operator: string) {
  return postJson<{ ok: boolean; detail: ReviewDetail }>(
    `/api/sample-flow/quality/${qualityId}/claim`,
    { sample_id: sampleId, operator },
  )
}

export function reviewDetail(qualityId: number) {
  return getJson<{ ok: boolean; detail: ReviewDetail }>(
    `/api/sample-flow/quality/${qualityId}/detail`,
  )
}

export function confirmField(
  qualityId: number,
  sampleId: string,
  field: string,
  expectedValue: unknown,
  requestId?: string,
) {
  return postJson<{
    ok: boolean
    message: string
    accepted_once?: boolean
    replayed?: boolean
    status?: string
    confirmation?: SampleFieldView
    progress?: { total: number; confirmed: number; passed: number }
  }>(`/api/sample-flow/quality/${qualityId}/confirm`, {
    sample_id: sampleId,
    field,
    expected_value: expectedValue,
    request_id: requestId ?? null,
  })
}

export function submitReview(qualityId: number, requestId?: string) {
  return postJson<{ ok: boolean; message: string; status?: string; sample_id?: string }>(
    `/api/sample-flow/quality/${qualityId}/submit-review`,
    { request_id: requestId ?? null },
  )
}

export function applyExport(qualityId: number, applicant: string) {
  return postJson<{
    ok: boolean
    message: string
    need_rerun_check?: boolean
    sample_id?: string
    idempotent?: boolean
    entry?: { id: number; 任务编号: string; status: string }
    adoption?: { sample_id: string; fields: Record<string, unknown>; sources: Record<string, string> }
  }>('/api/sample-flow/export/apply', { quality_check_id: qualityId, applicant })
}

export function exportTrace(exportId: number) {
  return getJson<{ ok: boolean; trace: {
    export_id: number
    adopted_sample_id: string | null
    adoption: { sample_id: string; config_version: number; fields: Record<string, unknown>; sources: Record<string, string> } | null
  } }>(`/api/sample-flow/export/${exportId}/trace`)
}
