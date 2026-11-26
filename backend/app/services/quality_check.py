"""质量抽检领域服务：采集数据质量抽检、样本领取、字段确认与合格判定。

跨模块数据流：
- quality_check ← schema_match: Schema匹配送检的不可变样本版本进入抽检
- quality_check → export_dataset: 仅当当前样本全部字段确认后才放行导出

链路修复要点：
- 抽检任务按数据批次关联样本版本；运营打开单子时能同时看到手上领走的样本
  和最新送检版，配置被更正后仍可回看仍在复核（未被替代前确认）的字段。
- 确认以 (sample_id, field) 为粒度，只对该样本固化的字段值生效；
  旧样本的确认不能让新样本获得放行。
- 带 request_id 的确认支持幂等重送：重放返回同一次受理结论。
"""
from __future__ import annotations

import random
import time
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class QualityCheckService(BaseModuleService):
    MODULE = "quality_check"
    ENTITY = "抽检任务"
    OPERATION_TERM = "采集"
    LIST_FIELDS = ["任务编号", "数据批次", "抽检比例", "检测字段", "通过标准", "合格数量", "不合格数量", "抽检结论"]
    REQUIRED_FIELDS = ["任务编号", "数据批次", "抽检比例"]
    STATUS_ORDER = ["待执行", "执行中", "通过", "未通过"]
    ACTION_RULES = {"发起抽检": "执行中", "标记通过": "通过", "标记未通过": "未通过"}

    CHECK_ITEMS = ["完整性", "准确性", "一致性", "时效性", "唯一性", "格式规范", "业务规则"]

    def create_entry(self, values: dict[str, Any]) -> tuple:
        entry, missing = super().create_entry(values)
        if entry is None:
            return None, missing
        entry["_check_results"] = {}
        entry["_score"] = 0.0
        entry["_executed_at"] = 0.0
        # 当前单子手上的样本版本与复核轨迹
        entry["_current_sample_id"] = None
        entry["_review_log"] = []
        store.record_metric("quality_check", "check_task_created", 1,
                            {"batch": values.get("数据批次", "")})
        return entry, []

    # ── 样本关联与领取 ─────────────────────────────────────────

    def available_samples(self, entry_id: int) -> dict[str, Any] | None:
        """返回该抽检单当前可用的样本版本列表（样本更正后两边同步看到新版）。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None
        batch = entry.get("数据批次", "")
        versions = store.sample_versions(batch)
        latest = store.latest_sample_version(batch)
        return {
            "quality_check_id": entry_id,
            "batch": batch,
            "held_sample_id": entry.get("_current_sample_id"),
            "latest_sample_id": latest["sample_id"] if latest else None,
            "samples": [self._sample_view(version) for version in versions],
        }

    def claim_sample(self, entry_id: int, sample_id: str, operator: str = "运营") -> dict[str, Any] | None:
        """运营把某份送检样本领到自己的抽检单上。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None
        sample, message = store.claim_sample_version(sample_id, operator, entry_id)
        if sample is None:
            return None
        if sample["batch"] != entry.get("数据批次"):
            return None
        entry["_current_sample_id"] = sample_id
        if entry["status"] == "待执行":
            entry["status"] = "执行中"
        entry["_review_log"].append({
            "ts": time.time(), "action": "claim", "sample_id": sample_id,
            "operator": operator, "message": message,
        })
        store.pipeline_trace(
            "quality_check", "quality_check", entry_id, entry_id,
            "claim_sample", {"batch": sample["batch"], "sample_id": sample_id},
        )
        return self.get_review_detail(entry_id)

    def get_review_detail(self, entry_id: int) -> dict[str, Any] | None:
        """抽检详情：手上样本 vs 最新送检版，逐字段列出复核状态与来源。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None
        batch = entry.get("数据批次", "")
        held_id = entry.get("_current_sample_id")
        held = store.find_sample_version(held_id) if held_id else None
        latest = store.latest_sample_version(batch)
        return {
            "quality_check_id": entry_id,
            "任务编号": entry.get("任务编号"),
            "batch": batch,
            "status": entry.get("status"),
            "抽检结论": entry.get("抽检结论"),
            "held_sample": self._sample_view(held) if held else None,
            "latest_sample": self._sample_view(latest) if latest else None,
            "config_corrected": bool(
                held and latest and held["sample_id"] != latest["sample_id"]
                and latest["config_version"] > held["config_version"]
            ),
            "review_log": list(entry.get("_review_log", [])),
        }

    # ── 字段确认（幂等、只认实际核过的值） ─────────────────────

    def confirm_field(
        self,
        entry_id: int,
        sample_id: str,
        field: str,
        *,
        operator: str = "运营",
        expected_value: Any = None,
        passed: bool = True,
        remark: str = "",
        request_id: str | None = None,
    ) -> dict[str, Any] | None:
        """确认样本上的某个字段；request_id 重放时返回同一次受理结论。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None
        if request_id:
            cached = store.idempotent_response(request_id)
            if cached is not None:
                # 重送：返回同一次受理结论，并标明本次没有产生新的受理
                replay = dict(cached)
                replay["accepted_once"] = False
                replay["replayed"] = True
                replay["status"] = entry["status"]
                return replay
        sample = store.find_sample_version(sample_id)
        if sample is None or sample["batch"] != entry.get("数据批次", ""):
            conclusion = {"ok": False, "message": "样本不存在或不属于该抽检批次",
                          "quality_check_id": entry_id}
            if request_id:
                return store.remember_idempotent(request_id, conclusion)
            return conclusion
        record, message, created = store.confirm_field(
            sample_id, field, operator=operator,
            expected_value=expected_value, passed=passed, remark=remark,
        )
        if record is None:
            conclusion = {"ok": False, "message": message, "quality_check_id": entry_id}
            if request_id:
                return store.remember_idempotent(request_id, conclusion)
            return conclusion
        if request_id and created:
            record["request_id"] = request_id
        if created:
            entry["_review_log"].append({
                "ts": time.time(), "action": "confirm_field",
                "sample_id": sample_id, "field": field,
                "operator": operator, "passed": passed,
                "request_id": request_id,
            })
            store.record_metric("quality_check", "field_confirmed", 1,
                                {"sample_id": sample_id, "field": field})
        conclusion = {
            "ok": True,
            "message": message,
            "quality_check_id": entry_id,
            "accepted_once": created,
            "confirmation": record,
            "progress": self._review_progress(sample_id),
        }
        # 只根据当前（最新送检）样本的字段确认情况决定整单是否放行
        self._recompute_status(entry)
        conclusion["status"] = entry["status"]
        if request_id:
            stored = store.remember_idempotent(request_id, conclusion)
            # 重放时仍带上最新状态，但受理结论主体保持首次内容
            stored["status"] = entry["status"]
            return stored
        return conclusion

    def submit_review(
        self, entry_id: int, *, operator: str = "运营", request_id: str | None = None
    ) -> dict[str, Any] | None:
        """送检/复核提交：要求手上样本的全部字段都已实际确认。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None
        if request_id:
            cached = store.idempotent_response(request_id)
            if cached is not None:
                return cached
        batch = entry.get("数据批次", "")
        latest = store.latest_sample_version(batch)
        held_id = entry.get("_current_sample_id")
        if latest is None or held_id is None:
            conclusion = {"ok": False, "message": "尚无送检样本，无法提交复核",
                          "quality_check_id": entry_id, "status": entry["status"]}
            if request_id:
                return store.remember_idempotent(request_id, conclusion)
            return conclusion
        if held_id != latest["sample_id"]:
            conclusion = {
                "ok": False,
                "quality_check_id": entry_id,
                "status": entry["status"],
                "message": (
                    f"手上样本 {held_id} 已不是最新送检版 {latest['sample_id']}，"
                    "请领取最新样本重新核对后再提交"
                ),
            }
            if request_id:
                return store.remember_idempotent(request_id, conclusion)
            return conclusion
        progress = self._review_progress(held_id)
        if progress["confirmed"] < progress["total"]:
            conclusion = {
                "ok": False,
                "quality_check_id": entry_id,
                "status": entry["status"],
                "message": f"仍有 {progress['total'] - progress['confirmed']} 个字段未确认，不能放行",
                "progress": progress,
            }
            if request_id:
                return store.remember_idempotent(request_id, conclusion)
            return conclusion
        entry["status"] = "通过"
        entry["pending"] = False
        entry["抽检结论"] = (
            f"合格(样本{latest['sample_id']}，配置v{latest['config_version']}，"
            f"{progress['confirmed']}/{progress['total']}字段人工确认)"
        )
        entry["_score"] = 1.0
        latest["status"] = "复核通过"
        entry["_review_log"].append({
            "ts": time.time(), "action": "submit_review",
            "sample_id": held_id, "operator": operator, "request_id": request_id,
        })
        store.pipeline_trace(
            "quality_check", "export_dataset", entry_id, 0,
            "review_passed", {"sample_id": held_id, "batch": batch},
        )
        store.record_metric("quality_check", "check_passed", 1,
                            {"sample_id": held_id})
        conclusion = {"ok": True, "message": "复核通过，样本可用于导出放行",
                      "quality_check_id": entry_id, "status": "通过",
                      "sample_id": held_id, "progress": progress}
        if request_id:
            return store.remember_idempotent(request_id, conclusion)
        return conclusion

    # ── 旧入口：模拟评分抽检（无样本版本时保持原有行为） ───────

    def run_check(self, entry_id: int, sample_data: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
        """执行质量抽检，逐项评分。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None
        entry["status"] = "执行中"
        entry["_executed_at"] = time.time()
        sample_size = sample_data if isinstance(sample_data, int) else len(sample_data) if sample_data else random.randint(10, 200)
        # 模拟检测
        results = {}
        low_score_count = 0
        for item in self.CHECK_ITEMS:
            score = random.uniform(0.6, 1.0)
            results[item] = {
                "score": score,
                "passed": score >= 0.8,
                "sample_count": sample_size if isinstance(sample_size, int) else 0,
                "defect_count": random.randint(0, max(1, sample_size // 10)) if isinstance(sample_size, int) else 0,
            }
            if not results[item]["passed"]:
                low_score_count += 1
        overall = sum(r["score"] for r in results.values()) / len(results)
        entry["_check_results"] = results
        entry["_score"] = overall
        entry["合格数量"] = sample_size if isinstance(sample_size, int) else 0
        entry["不合格数量"] = low_score_count
        pass_threshold = float(entry.get("通过标准") or 0.8)
        if overall >= pass_threshold:
            entry["status"] = "通过"
            entry["pending"] = False
            entry["抽检结论"] = f"合格(综合评分{overall:.0%})"
            store.record_metric("quality_check", "check_passed", 1,
                                {"score": f"{overall:.2f}"})
        else:
            entry["status"] = "未通过"
            entry["abnormal"] = True
            entry["抽检结论"] = f"不合格(综合评分{overall:.0%}，{low_score_count}项不达标)"
            store.record_metric("quality_check", "check_failed", 1,
                                {"score": f"{overall:.2f}",
                                 "failed_items": str(low_score_count)})
        return {"entry_id": entry_id, "score": overall, "results": results,
                "status": entry["status"]}

    def trigger_export_if_passed(self, entry_id: int, export_task_id: int) -> dict[str, Any] | None:
        """质量通过后触发导出。跨模块：quality_check → export_dataset"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None or entry.get("status") != "通过":
            return None
        latest = store.latest_sample_version(entry.get("数据批次", ""))
        store.pipeline_trace(
            "quality_check", "export_dataset", entry_id, export_task_id,
            "trigger",
            {"score": entry["_score"],
             "sample_id": latest["sample_id"] if latest else None},
        )
        store.record_metric("quality_check", "export_triggered", 1,
                            {"export_id": str(export_task_id)})
        return entry

    # ── 内部辅助 ───────────────────────────────────────────────

    def _recompute_status(self, entry: dict[str, Any]) -> None:
        """根据当前最新样本的字段确认进度刷新执行态。

        只反映「复核中/执行中」的进度；最终「通过」只能由 submit_review
        在确认手上样本就是最新送检版且字段全部核过后给出，避免旧样本的
        确认把单子自动推成通过、再被新样本搭便车放行。
        """
        batch = entry.get("数据批次", "")
        latest = store.latest_sample_version(batch)
        if latest is None:
            return
        progress = self._review_progress(latest["sample_id"])
        if progress["confirmed"] > 0 and entry["status"] == "待执行":
            entry["status"] = "执行中"

    def _review_progress(self, sample_id: str) -> dict[str, int]:
        sample = store.find_sample_version(sample_id)
        if sample is None:
            return {"total": 0, "confirmed": 0, "passed": 0}
        confirmations = store.field_confirmations(sample_id)
        passed = sum(1 for rec in confirmations if rec.get("passed"))
        return {
            "total": len(sample["fields"]),
            "confirmed": len(confirmations),
            "passed": passed,
        }

    def _sample_view(self, sample: dict[str, Any] | None) -> dict[str, Any] | None:
        if sample is None:
            return None
        confirmations = store.field_confirmations(sample["sample_id"])
        confirmed_fields = {rec["field"]: rec for rec in confirmations}
        fields = []
        for field, value in sample["fields"].items():
            rec = confirmed_fields.get(field)
            fields.append({
                "field": field,
                "value": value,
                "source": sample["sources"].get(field),
                "confirmed": rec is not None,
                "confirmed_by": rec.get("operator") if rec else None,
                "confirmed_at": rec.get("confirmed_at") if rec else None,
                "passed": rec.get("passed") if rec else None,
            })
        return {
            "sample_id": sample["sample_id"],
            "batch": sample["batch"],
            "version": sample["version"],
            "config_version": sample["config_version"],
            "status": sample["status"],
            "claimed_by": sample.get("claimed_by"),
            "claimed_quality_id": sample.get("claimed_quality_id"),
            "superseded_by": sample.get("superseded_by"),
            "created_at": sample.get("created_at"),
            "note": sample.get("note"),
            "fields": fields,
            "progress": self._review_progress(sample["sample_id"]),
        }

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        tasks = store.rows(self.MODULE)
        base["passed_checks"] = sum(1 for t in tasks if t.get("status") == "通过")
        base["failed_checks"] = sum(1 for t in tasks if t.get("status") == "未通过")
        base["avg_score"] = (
            sum(t.get("_score", 0.0) for t in tasks) / max(len(tasks), 1)
        )
        base["samples_in_review"] = sum(
            1 for sample in store.sample_versions()
            if sample["status"] in ("送检中", "已领取", "复核中")
        )
        return base
