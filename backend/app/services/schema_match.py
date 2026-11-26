"""Schema匹配领域服务：采集数据与目标Schema字段映射、补全策略与质量评分。

跨模块数据流：
- schema_match ← result_parse: 解析结果送入Schema匹配
- schema_match → quality_check: 匹配结果（不可变样本版本）送入质量抽检

链路修复要点：
- 人工定义的映射按「目标字段 → 来源字段」保存，字段对应表不再用同名字段顶替，
  避免人工选择 headline 却在详情里显示成 title 等同名字段的值。
- 每次更正映射都会产生新的配置版本；送检样本固化取到的值、来源和配置版本，
  后续更正不会回改已送检样本。
- 预跑（preview）返回每个目标字段实际取到的值和来源，供界面核对。
"""
from __future__ import annotations

import json
import time
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class SchemaMatchService(BaseModuleService):
    MODULE = "schema_match"
    ENTITY = "匹配配置"
    OPERATION_TERM = "采集"
    LIST_FIELDS = ["配置编号", "来源站点", "目标Schema", "映射规则", "字段对应表", "缺失补全策略", "匹配度", "最后验证"]
    REQUIRED_FIELDS = ["配置编号", "来源站点", "目标Schema"]
    STATUS_ORDER = ["草稿", "待验证", "已启用", "已停用"]
    ACTION_RULES = {"提交验证": "待验证", "启用配置": "已启用", "停用配置": "已停用"}
    NEGATIVE_ACTIONS = ["停用配置"]

    def create_entry(self, values: dict[str, Any]) -> tuple:
        entry, missing = super().create_entry(values)
        if entry is None:
            return None, missing
        # 映射规则统一为 {目标字段: 来源字段}
        entry["_field_mapping"] = {}
        entry["_config_version"] = 1
        entry["_mapping_history"] = []
        entry["_last_preview"] = None
        entry["_match_history"] = []
        entry["_total_matches"] = 0
        entry["_successful_matches"] = 0
        store.record_metric("schema_match", "config_created", 1,
                            {"site": values.get("来源站点", ""),
                             "schema": values.get("目标Schema", "")})
        return entry, []

    def define_mapping(self, entry_id: int, mapping_rules: dict[str, str]) -> dict[str, Any] | None:
        """定义目标Schema字段到源字段的映射规则（key=目标字段，value=来源字段）。

        人工映射是字段取值的唯一人工来源；字段对应表如实展示目标→来源，
        不再渲染成同名字段。每次更正递增配置版本，供样本版本溯源。
        """
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None
        normalized = {str(target): str(source) for target, source in mapping_rules.items()}
        entry["_field_mapping"] = normalized
        entry["_config_version"] = int(entry.get("_config_version", 1)) + 1
        entry["映射规则"] = json.dumps(normalized, ensure_ascii=False)
        entry["字段对应表"] = json.dumps(normalized, ensure_ascii=False)
        entry["_mapping_history"].append({
            "config_version": entry["_config_version"],
            "mapping": dict(normalized),
            "changed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        })
        store.record_metric("schema_match", "mapping_defined", len(normalized),
                            {"schema": entry.get("目标Schema", ""),
                             "config_version": str(entry["_config_version"])})
        return entry

    # ── 预跑与送检 ─────────────────────────────────────────────

    def preview_match(self, entry_id: int, sample: dict[str, Any]) -> dict[str, Any] | None:
        """用一份样本预跑当前配置，返回每个目标字段实际取到的值与来源。

        预跑不落任何业务状态，只在配置上保留最近一次预览，供详情页展示。
        """
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None
        matched, missing = self._resolve_fields(entry, sample)
        preview = {
            "schema_id": entry_id,
            "config_version": entry.get("_config_version", 1),
            "sample_keys": sorted(sample.keys()),
            "matched": matched,
            "missing": missing,
            "previewed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        entry["_last_preview"] = preview
        return preview

    def submit_sample(
        self,
        entry_id: int,
        batch: str,
        raw_sample: dict[str, Any],
        *,
        note: str = "",
        operator: str = "system",
    ) -> dict[str, Any] | None:
        """按当前配置匹配样本并送检，生成不可变样本版本进入质量抽检链路。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None
        matched, missing = self._resolve_fields(entry, raw_sample)
        fields = {target: info["value"] for target, info in matched.items()}
        sources = {target: info["source"] for target, info in matched.items()}
        sample = store.register_sample_version(
            batch=batch,
            schema_id=entry_id,
            config_version=int(entry.get("_config_version", 1)),
            fields=fields,
            sources=sources,
            raw_sample=raw_sample,
            note=note,
        )
        store.pipeline_trace(
            "schema_match", "quality_check", entry_id, 0,
            "submit_sample",
            {"batch": batch, "sample_id": sample["sample_id"],
             "version": sample["version"], "config_version": sample["config_version"],
             "missing": missing},
        )
        store.record_metric("schema_match", "sample_submitted", 1,
                            {"batch": batch, "sample_id": sample["sample_id"],
                             "config_version": str(sample["config_version"])})
        return sample

    # ── 兼容旧入口的匹配执行 ───────────────────────────────────

    def match(self, entry_id: int, parsed_fields: dict[str, Any]) -> dict[str, Any]:
        """执行字段匹配和补全（保留原入口，内部统一走显式映射+相似度兜底）。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"matched": {}, "missing": [], "score": 0.0}
        entry["_total_matches"] += 1
        matched, missing = self._resolve_fields(entry, parsed_fields)
        # 补全策略
        for mf in missing:
            strategy = entry.get("缺失补全策略", "default")
            if strategy == "default":
                matched[mf] = {"source": "__default__", "value": "N/A", "similarity": 0.0}
            elif strategy == "fill":
                matched[mf] = {"source": "__fill__", "value": "", "similarity": 0.0}
        needed = self._get_target_fields(entry.get("目标Schema", ""))
        score = len(matched) / max(len(needed), 1)
        entry["匹配度"] = f"{score:.0%}"
        entry["最后验证"] = time.strftime("%Y-%m-%d %H:%M:%S")
        entry["_match_history"].append({
            "ts": time.time(),
            "score": score,
            "matched": len(matched),
            "missing": len(missing),
        })
        if score >= 0.8:
            entry["_successful_matches"] += 1
            store.record_metric("schema_match", "match_successful", 1,
                                {"score": f"{score:.2f}"})
        else:
            store.record_metric("schema_match", "match_low_score", 1,
                                {"score": f"{score:.2f}", "missing": str(len(missing))})
        return {"matched": matched, "missing": missing, "score": score}

    def feed_to_quality_check(self, entry_id: int, quality_check_id: int,
                              match_result: dict[str, Any]) -> dict[str, Any] | None:
        """匹配结果送入质量抽检。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None
        store.pipeline_trace(
            "schema_match", "quality_check", entry_id, quality_check_id,
            "feed", {"score": match_result.get("score", 0)}
        )
        store.record_metric("schema_match", "result_fed_to_quality", 1,
                            {"quality_id": str(quality_check_id)})
        return entry

    # ── 内部解析 ───────────────────────────────────────────────

    def _resolve_fields(
        self, entry: dict[str, Any], sample: dict[str, Any]
    ) -> tuple[dict[str, dict[str, Any]], list[str]]:
        """按「人工显式映射优先、名称相似度兜底」解析目标字段。

        返回 matched: {目标字段: {source, value, similarity, via}} 与 missing 列表。
        显式映射的来源字段在样本中不存在时，才回退到相似度扫描，
        保证人工选择的 headline 不会被同名字段 title 顶替。
        """
        mapping: dict[str, str] = entry.get("_field_mapping", {}) or {}
        needed = self._get_target_fields(entry.get("目标Schema", ""))
        matched: dict[str, dict[str, Any]] = {}
        missing: list[str] = []
        for target_field in needed:
            mapped_source = mapping.get(target_field)
            if mapped_source and mapped_source in sample:
                matched[target_field] = {
                    "source": mapped_source,
                    "value": sample[mapped_source],
                    "similarity": 1.0,
                    "via": "manual_mapping",
                }
                continue
            found = False
            for src_field in sample:
                similarity = self._field_similarity(target_field, src_field)
                if similarity > 0.6:
                    matched[target_field] = {
                        "source": src_field,
                        "value": sample[src_field],
                        "similarity": similarity,
                        "via": "name_fallback",
                    }
                    found = True
                    break
            if not found:
                missing.append(target_field)
        return matched, missing

    def _get_target_fields(self, schema_name: str) -> list[str]:
        defaults = {
            "default": ["title", "content", "publish_time", "author", "source", "category", "tags", "price", "location"],
        }
        return defaults.get(schema_name, defaults["default"])

    def _field_similarity(self, a: str, b: str) -> float:
        """简单字段名相似度。"""
        a, b = a.lower(), b.lower()
        if a == b:
            return 1.0
        if a in b or b in a:
            return 0.7
        common = len(set(a) & set(b))
        return common / max(len(a), len(b)) * 0.5

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        configs = store.rows(self.MODULE)
        base["active_configs"] = sum(1 for c in configs if c.get("status") == "已启用")
        base["total_matches"] = sum(c.get("_total_matches", 0) for c in configs)
        base["match_success_rate"] = (
            sum(c.get("_successful_matches", 0) for c in configs) /
            max(sum(c.get("_total_matches", 0) for c in configs), 1)
        )
        base["sample_versions"] = len(store.sample_versions())
        return base
