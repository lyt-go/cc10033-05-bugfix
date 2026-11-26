"""Schema匹配 → 质量抽检 → 数据导出 链路修复验收测试。

验收剧情（同一个数据批次 batch-A）：
1. Schema 配置人工指定 title 取 headline；用同时含 headline/title 的样本预跑，
   预览必须显示实际取到的 headline 值及其来源，而不是同名字段 title 的值。
2. 送检第一份样本（v1，配置 v1），质量抽检单领走 v1，逐字段确认一部分。
3. 配置更正为 title 取 name，再次送检得到 v2（配置 v2）；抽检详情要能
   同时呈现手上的 v1 与最新送检版 v2，并保留 v1 仍在复核的字段回看。
4. v1 的确认与 v2 的送检交错到达：v1 的确认只能落在 v1 字段上，
   不能让 v2 获得放行；此时申请导出必须被拒（需补做抽检），且不产生导出任务。
5. 领取 v2、确认全部字段并提交复核后，导出申请才能放行；
   负责人能看到采用的是哪份样本，已生成导出可查回实际字段值。
6. 同一次确认带相同 request_id 重送两次，返回同一次受理结论。
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services import SERVICES
from app.store import store


BATCH = "batch-acceptance"


@pytest.fixture()
def chain():
    sm = SERVICES["schema_match"]
    qc = SERVICES["quality_check"]
    ed = SERVICES["export_dataset"]
    schema, _ = sm.create_entry({
        "配置编号": "SCHM-ACC", "来源站点": "acceptance.example",
        "目标Schema": "default", "缺失补全策略": "default",
    })
    quality, _ = qc.create_entry({
        "任务编号": "QUAL-ACC", "数据批次": BATCH, "抽检比例": "100%",
        "通过标准": "0.8",
    })
    return sm, qc, ed, schema, quality


def _headline_sample(suffix: str = "v1") -> dict:
    return {
        "headline": f"人工选择的头条-{suffix}",
        "title": f"同名字段标题-{suffix}",
        "name": f"name字段-{suffix}",
        "content": f"正文-{suffix}",
        "author": f"作者-{suffix}",
    }


class TestSchemaPreviewAndMapping:
    def test_preview_shows_actual_value_and_source_for_headline(self, chain):
        sm, _qc, _ed, schema, _quality = chain
        # 人工指定 title 目标字段取 headline 来源字段
        sm.define_mapping(schema["id"], {"title": "headline"})
        preview = sm.preview_match(schema["id"], _headline_sample())
        assert preview is not None
        title_hit = preview["matched"]["title"]
        assert title_hit["source"] == "headline"
        assert title_hit["value"] == "人工选择的头条-v1"
        assert title_hit["via"] == "manual_mapping"
        # 绝不能显示成同名字段 title 的值
        assert title_hit["value"] != "同名字段标题-v1"
        assert schema["_last_preview"]["matched"]["title"]["source"] == "headline"

    def test_field_correspondence_table_keeps_target_to_source(self, chain):
        sm, _qc, _ed, schema, _quality = chain
        sm.define_mapping(schema["id"], {"title": "headline"})
        assert schema["字段对应表"] == '{"title": "headline"}'
        assert schema["_field_mapping"] == {"title": "headline"}

    def test_config_version_increments_on_correction(self, chain):
        sm, _qc, _ed, schema, _quality = chain
        sm.define_mapping(schema["id"], {"title": "headline"})
        assert schema["_config_version"] == 2
        sm.define_mapping(schema["id"], {"title": "name"})
        assert schema["_config_version"] == 3


class TestInterleavedSampleReview:
    def test_full_interleaved_acceptance_flow(self, chain):
        sm, qc, ed, schema, quality = chain

        # ── 第一阶段：headline 配置 + v1 送检 + 领取 + 部分确认 ──
        sm.define_mapping(schema["id"], {"title": "headline"})
        v1 = sm.submit_sample(schema["id"], BATCH, _headline_sample("v1"), note="第一次送检")
        assert v1["version"] == 1
        assert v1["config_version"] == schema["_config_version"]
        assert v1["fields"]["title"] == "人工选择的头条-v1"
        assert v1["sources"]["title"] == "headline"

        claim = qc.claim_sample(quality["id"], v1["sample_id"], operator="运营A")
        assert claim["held_sample"]["sample_id"] == v1["sample_id"]

        # 交错点 A：先确认 v1 的 title 字段
        confirm_v1 = qc.confirm_field(
            quality["id"], v1["sample_id"], "title",
            operator="运营A", expected_value="人工选择的头条-v1",
            request_id="req-confirm-v1-title",
        )
        assert confirm_v1["ok"] is True
        assert confirm_v1["accepted_once"] is True

        # ── 第二阶段：配置更正为 name，再送检 v2 ──
        sm.define_mapping(schema["id"], {"title": "name"})
        v2 = sm.submit_sample(schema["id"], BATCH, _headline_sample("v2"), note="配置更正后送检")
        assert v2["version"] == 2
        assert v2["config_version"] == v1["config_version"] + 1
        assert v2["fields"]["title"] == "name字段-v2"
        assert v2["sources"]["title"] == "name"

        # v1 被标记为已被替代，但确认记录保留可回看
        refreshed_v1 = store.find_sample_version(v1["sample_id"])
        assert refreshed_v1["superseded_by"] == v2["sample_id"]
        assert refreshed_v1["status"] == "已被替代"

        # 抽检详情同时给得出手上样本与最新送检版
        detail = qc.get_review_detail(quality["id"])
        assert detail["held_sample"]["sample_id"] == v1["sample_id"]
        assert detail["latest_sample"]["sample_id"] == v2["sample_id"]
        assert detail["config_corrected"] is True
        # v1 上已复核的字段仍可回看
        v1_view = next(s for s in detail["held_sample"]["fields"] if s["field"] == "title")
        assert v1_view["confirmed"] is True
        assert v1_view["value"] == "人工选择的头条-v1"
        v2_view = next(s for s in detail["latest_sample"]["fields"] if s["field"] == "title")
        assert v2_view["confirmed"] is False

        # 抽检详情与导出选择列表看到的当前可用样本一致（都指向 v2）
        available = qc.available_samples(quality["id"])
        assert available["latest_sample_id"] == v2["sample_id"]
        assert [s["sample_id"] for s in available["samples"]] == [
            v1["sample_id"], v2["sample_id"]]

        # 交错点 B：v1 的迟到确认到达，只能记在 v1 上，v2 不受益
        late_v1 = qc.confirm_field(
            quality["id"], v1["sample_id"], "content",
            operator="运营A", expected_value="正文-v1",
        )
        assert late_v1["ok"] is True
        v2_progress = qc.available_samples(quality["id"])
        v2_info = next(s for s in v2_progress["samples"] if s["sample_id"] == v2["sample_id"])
        assert v2_info["progress"]["confirmed"] == 0

        # 旧确认不能放行新样本：此时申请导出必须被拒，且不新增导出任务
        before = len(store.rows("export_dataset"))
        blocked = ed.apply_for_export(quality["id"], applicant="导出负责人甲")
        after = len(store.rows("export_dataset"))
        assert blocked["ok"] is False
        assert blocked["need_rerun_check"] is True
        assert blocked["sample_id"] == v2["sample_id"]
        assert after == before, "被拒申请不得在导出列表里留下任务"

        # 拿 v1 的固化值之外的值去确认会被拒绝（确认只认实际核过的值）
        bad = qc.confirm_field(
            quality["id"], v1["sample_id"], "title",
            operator="运营A", expected_value="name字段-v2",
        )
        assert bad["ok"] is False
        assert "字段值已变更" in bad["message"]

        # ── 第三阶段：领取 v2，逐字段确认并提交复核 ──
        qc.claim_sample(quality["id"], v2["sample_id"], operator="运营A")
        target_fields = list(v2["fields"].keys())
        for field in target_fields:
            result = qc.confirm_field(
                quality["id"], v2["sample_id"], field,
                operator="运营A", expected_value=v2["fields"][field],
            )
            assert result["ok"] is True
        submit = qc.submit_review(quality["id"], operator="运营A")
        assert submit["ok"] is True
        assert quality["status"] == "通过"
        assert v2["sample_id"] in quality["抽检结论"]

        # ── 第四阶段：导出放行，且可追溯实际字段值 ──
        approval = ed.apply_for_export(quality["id"], applicant="导出负责人甲")
        assert approval["ok"] is True
        assert approval["sample_id"] == v2["sample_id"]
        export_entry = approval["entry"]
        assert export_entry["_adopted_sample_id"] == v2["sample_id"]

        trace = ed.get_export_trace(export_entry["id"])
        assert trace["adoption"]["sample_id"] == v2["sample_id"]
        assert trace["adoption"]["fields"]["title"] == "name字段-v2"
        assert trace["adoption"]["sources"]["title"] == "name"
        assert trace["adoption"]["config_version"] == v2["config_version"]

        # 同一负责人重复申请幂等，不产生第二笔导出任务
        repeat = ed.apply_for_export(quality["id"], applicant="导出负责人甲")
        assert repeat.get("idempotent") is True
        assert repeat["entry"]["id"] == export_entry["id"]

    def test_idempotent_confirmation_replay_returns_same_acceptance(self, chain):
        sm, qc, _ed, schema, quality = chain
        sm.define_mapping(schema["id"], {"title": "headline"})
        sample = sm.submit_sample(schema["id"], BATCH, _headline_sample("idem"))
        qc.claim_sample(quality["id"], sample["sample_id"], operator="运营B")

        first = qc.confirm_field(
            quality["id"], sample["sample_id"], "title",
            operator="运营B", expected_value="人工选择的头条-idem",
            request_id="req-idem-001",
        )
        second = qc.confirm_field(
            quality["id"], sample["sample_id"], "title",
            operator="运营B", expected_value="人工选择的头条-idem",
            request_id="req-idem-001",
        )
        third = qc.confirm_field(
            quality["id"], sample["sample_id"], "title",
            operator="运营B", expected_value="人工选择的头条-idem",
            request_id="req-idem-001",
        )
        assert first["accepted_once"] is True
        assert second["accepted_once"] is False
        assert third["accepted_once"] is False
        assert first["confirmation"]["confirmed_at"] == second["confirmation"]["confirmed_at"]
        assert second["confirmation"] == third["confirmation"]
        # 只落了一条字段确认记录
        assert len(store.field_confirmations(sample["sample_id"])) == 1

    def test_submit_review_blocked_while_holding_superseded_sample(self, chain):
        sm, qc, _ed, schema, quality = chain
        sm.define_mapping(schema["id"], {"title": "headline"})
        v1 = sm.submit_sample(schema["id"], BATCH, _headline_sample("old"))
        qc.claim_sample(quality["id"], v1["sample_id"])
        for field in v1["fields"]:
            qc.confirm_field(quality["id"], v1["sample_id"], field,
                             expected_value=v1["fields"][field])
        # 配置更正并送检新版本
        sm.define_mapping(schema["id"], {"title": "name"})
        v2 = sm.submit_sample(schema["id"], BATCH, _headline_sample("new"))
        result = qc.submit_review(quality["id"])
        assert result["ok"] is False
        assert v2["sample_id"] in result["message"]
        assert quality["status"] != "通过"
