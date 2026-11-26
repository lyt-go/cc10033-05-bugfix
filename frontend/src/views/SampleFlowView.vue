<template>
  <section class="page sample-flow">
    <header class="page-head">
      <div>
        <h2>样本链路（Schema匹配 → 质量抽检 → 数据导出）</h2>
        <p class="page-desc">
          每次送检生成不可变样本版本；人工映射按「目标字段 ← 来源字段」取值，
          抽检确认只对实际核过的字段值生效，导出只放行当前通过抽检的样本。
        </p>
      </div>
    </header>

    <p v-if="message" class="error-text" data-testid="flow-message">{{ message }}</p>

    <div class="flow-grid">
      <!-- ① Schema 匹配：映射更正 / 预跑 / 送检 -->
      <article class="flow-card" data-testid="panel-schema">
        <h3>① Schema 匹配</h3>
        <label class="flow-field"><span>匹配配置ID</span>
          <input v-model="schemaId" data-testid="schema-id" placeholder="如 1" />
        </label>
        <label class="flow-field"><span>目标字段</span>
          <input v-model="mapTarget" data-testid="map-target" placeholder="如 title" />
        </label>
        <label class="flow-field"><span>来源字段（人工选择）</span>
          <input v-model="mapSource" data-testid="map-source" placeholder="如 headline / name" />
        </label>
        <div class="row-actions">
          <button class="btn" type="button" data-testid="save-mapping" @click="saveMapping">更正映射</button>
        </div>

        <label class="flow-field"><span>预跑/送检样本（JSON）</span>
          <textarea v-model="sampleJson" data-testid="sample-json" rows="6"></textarea>
        </label>
        <div class="row-actions">
          <button class="btn" type="button" data-testid="preview" @click="doPreview">预跑预览</button>
          <button class="btn primary" type="button" data-testid="submit-sample" @click="doSubmit">送检</button>
        </div>
        <label class="flow-field"><span>数据批次</span>
          <input v-model="batch" data-testid="batch" placeholder="同一批次多次送检会生成新版本" />
        </label>

        <div v-if="preview" class="result-box" data-testid="preview-result">
          <h4>预跑结果（配置 v{{ preview.config_version }}）</h4>
          <table class="data-table">
            <thead><tr><th>目标字段</th><th>实际取值</th><th>来源</th><th>取法</th></tr></thead>
            <tbody>
              <tr v-for="(hit, target) in preview.matched" :key="target">
                <td>{{ target }}</td>
                <td>{{ hit.value }}</td>
                <td>{{ hit.source }}</td>
                <td>{{ hit.via === 'manual_mapping' ? '人工映射' : '名称兜底' }}</td>
              </tr>
            </tbody>
          </table>
          <p v-if="preview.missing.length" class="error-text">缺失：{{ preview.missing.join('、') }}</p>
        </div>
      </article>

      <!-- ② 质量抽检：领取 / 手上样本 vs 最新送检版 / 字段确认 -->
      <article class="flow-card" data-testid="panel-quality">
        <h3>② 质量抽检</h3>
        <label class="flow-field"><span>抽检任务ID</span>
          <input v-model="qualityId" data-testid="quality-id" placeholder="如 1" />
        </label>
        <div class="row-actions">
          <button class="btn" type="button" data-testid="load-detail" @click="loadDetail">打开抽检单</button>
        </div>

        <template v-if="detail">
          <p>
            单子状态：<strong>{{ detail.status }}</strong>
            <span v-if="detail.config_corrected" class="warn-tag">配置已更正，请改领最新送检版</span>
          </p>
          <div class="sample-cols">
            <div data-testid="held-sample">
              <h4>手上样本{{ detail.held_sample ? `（v${detail.held_sample.version} / 配置v${detail.held_sample.config_version}）` : '' }}</h4>
              <p v-if="!detail.held_sample" class="empty-state">尚未领取样本</p>
              <template v-else>
                <p>{{ detail.held_sample.sample_id }} · {{ detail.held_sample.status }}</p>
                <table class="data-table">
                  <tbody>
                    <tr v-for="f in detail.held_sample.fields" :key="`h-${f.field}`">
                      <td>{{ f.field }}</td>
                      <td>{{ f.value }}</td>
                      <td>{{ f.source }}</td>
                      <td>{{ f.confirmed ? `已核（${f.confirmed_by}）` : '待核' }}</td>
                    </tr>
                  </tbody>
                </table>
                <button class="btn" type="button" :data-testid="`confirm-held-${detail.held_sample.sample_id}`"
                        @click="confirmHeldFields">按固化值逐字段确认</button>
              </template>
            </div>
            <div data-testid="latest-sample">
              <h4>最新送检版{{ detail.latest_sample ? `（v${detail.latest_sample.version} / 配置v${detail.latest_sample.config_version}）` : '' }}</h4>
              <p v-if="!detail.latest_sample" class="empty-state">暂无送检样本</p>
              <template v-else>
                <p>{{ detail.latest_sample.sample_id }} · {{ detail.latest_sample.status }}</p>
                <table class="data-table">
                  <tbody>
                    <tr v-for="f in detail.latest_sample.fields" :key="`l-${f.field}`">
                      <td>{{ f.field }}</td>
                      <td>{{ f.value }}</td>
                      <td>{{ f.source }}</td>
                      <td>{{ f.confirmed ? `已核（${f.confirmed_by}）` : '待核' }}</td>
                    </tr>
                  </tbody>
                </table>
                <button class="btn primary" type="button" :data-testid="`claim-latest-${detail.latest_sample.sample_id}`"
                        @click="claimLatest">领取最新送检版并逐字段确认</button>
              </template>
            </div>
          </div>
          <div class="row-actions">
            <button class="btn primary" type="button" data-testid="submit-review" @click="doSubmitReview">提交复核</button>
          </div>
        </template>
      </article>

      <!-- ③ 数据导出：申请放行 / 采纳追溯 -->
      <article class="flow-card" data-testid="panel-export">
        <h3>③ 数据导出</h3>
        <label class="flow-field"><span>负责人</span>
          <input v-model="applicant" data-testid="applicant" />
        </label>
        <div class="row-actions">
          <button class="btn primary" type="button" data-testid="apply-export" @click="doApplyExport">申请导出</button>
        </div>
        <div v-if="exportResult" class="result-box" data-testid="export-result">
          <p><strong>{{ exportResult.ok ? '已放行' : '未放行' }}</strong>：{{ exportResult.message }}</p>
          <p v-if="exportResult.sample_id">关联样本：{{ exportResult.sample_id }}</p>
          <p v-if="exportResult.idempotent">该申请此前已受理，未重复生成导出任务。</p>
          <button v-if="exportResult.entry" class="btn" type="button" data-testid="view-trace"
                  @click="doViewTrace(exportResult.entry.id)">查回实际采用的字段值</button>
        </div>
        <div v-if="trace" class="result-box" data-testid="trace-result">
          <h4>导出追溯（{{ trace.adoption?.sample_id }} / 配置v{{ trace.adoption?.config_version }}）</h4>
          <table class="data-table">
            <thead><tr><th>字段</th><th>采用值</th><th>来源</th></tr></thead>
            <tbody>
              <tr v-for="(value, key) in trace.adoption?.fields" :key="key">
                <td>{{ key }}</td>
                <td>{{ value }}</td>
                <td>{{ trace.adoption?.sources[key] }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </article>
    </div>

    <footer class="page-foot">
      <span>样本版本、字段确认与导出采纳均在后端固化，重放请求返回同一次受理结论。</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { ref } from 'vue'

import {
  applyExport,
  claimSample,
  confirmField,
  defineMapping,
  exportTrace,
  previewMatch,
  reviewDetail,
  submitReview,
  submitSample,
  type PreviewField,
  type ReviewDetail,
} from '@/api/sample-flow'

const schemaId = ref('1')
const qualityId = ref('1')
const batch = ref('batch-demo')
const mapTarget = ref('title')
const mapSource = ref('headline')
const applicant = ref('导出负责人')
const sampleJson = ref(JSON.stringify({
  headline: '人工选择的头条',
  title: '同名字段标题',
  name: 'name字段值',
  content: '正文内容',
  author: '作者',
}, null, 2))

const message = ref('')
const preview = ref<{ config_version: number; matched: Record<string, PreviewField>; missing: string[] } | null>(null)
const detail = ref<ReviewDetail | null>(null)
const exportResult = ref<Awaited<ReturnType<typeof applyExport>> | null>(null)
const trace = ref<Awaited<ReturnType<typeof exportTrace>>['trace'] | null>(null)

function fail(error: unknown) {
  message.value = error instanceof Error ? error.message : '操作失败'
}

function parseSample(): Record<string, unknown> {
  return JSON.parse(sampleJson.value) as Record<string, unknown>
}

async function saveMapping() {
  message.value = ''
  try {
    const result = await defineMapping(Number(schemaId.value), { [mapTarget.value]: mapSource.value })
    message.value = `映射已更正到配置 v${result.config_version}：${mapTarget.value} ← ${mapSource.value}`
  } catch (error) {
    fail(error)
  }
}

async function doPreview() {
  message.value = ''
  try {
    const result = await previewMatch(Number(schemaId.value), parseSample())
    preview.value = result.preview
  } catch (error) {
    fail(error)
  }
}

async function doSubmit() {
  message.value = ''
  try {
    const result = await submitSample(Number(schemaId.value), batch.value, parseSample())
    message.value = `已送检样本 ${result.sample.sample_id}（v${result.sample.version}，配置v${result.sample.config_version}）`
  } catch (error) {
    fail(error)
  }
}

async function loadDetail() {
  message.value = ''
  try {
    const result = await reviewDetail(Number(qualityId.value))
    detail.value = result.detail
  } catch (error) {
    fail(error)
  }
}

async function confirmAll(sampleId: string, fields: { field: string; value: unknown }[]) {
  for (const item of fields) {
    // request_id 用 样本+字段 固定，重放同一受理
    const result = await confirmField(
      Number(qualityId.value), sampleId, item.field, item.value,
      `req-${sampleId}-${item.field}`,
    )
    if (!result.ok) throw new Error(result.message)
  }
}

async function confirmHeldFields() {
  if (!detail.value?.held_sample) return
  message.value = ''
  try {
    await confirmAll(detail.value.held_sample.sample_id, detail.value.held_sample.fields)
    await loadDetail()
    message.value = '手上样本的字段已按固化值确认'
  } catch (error) {
    fail(error)
  }
}

async function claimLatest() {
  if (!detail.value?.latest_sample) return
  message.value = ''
  try {
    const latest = detail.value.latest_sample
    const result = await claimSample(Number(qualityId.value), latest.sample_id, '运营')
    detail.value = result.detail
    await confirmAll(latest.sample_id, latest.fields)
    await loadDetail()
    message.value = `已领取并确认最新送检版 ${latest.sample_id}`
  } catch (error) {
    fail(error)
  }
}

async function doSubmitReview() {
  message.value = ''
  try {
    const result = await submitReview(Number(qualityId.value), `sr-${qualityId.value}-${Date.now()}`)
    message.value = result.ok ? result.message : result.message
    await loadDetail()
  } catch (error) {
    fail(error)
  }
}

async function doApplyExport() {
  message.value = ''
  trace.value = null
  try {
    exportResult.value = await applyExport(Number(qualityId.value), applicant.value)
    message.value = exportResult.value.message
  } catch (error) {
    fail(error)
  }
}

async function doViewTrace(exportId: number) {
  message.value = ''
  try {
    const result = await exportTrace(exportId)
    trace.value = result.trace
  } catch (error) {
    fail(error)
  }
}
</script>

<style scoped>
.flow-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(360px, 1fr));
  gap: 16px;
}
.flow-card {
  background: var(--card-bg, #fff);
  border: 1px solid var(--border, #d9dde3);
  border-radius: 10px;
  padding: 16px;
}
.flow-field {
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin-bottom: 10px;
  font-size: 13px;
}
.flow-field textarea {
  font-family: monospace;
}
.sample-cols {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
  margin: 12px 0;
}
.result-box {
  margin-top: 12px;
  border-top: 1px dashed var(--border, #d9dde3);
  padding-top: 10px;
}
.warn-tag {
  margin-left: 8px;
  padding: 2px 8px;
  border-radius: 10px;
  background: #fff3e0;
  color: #b25c00;
  font-size: 12px;
}
</style>
