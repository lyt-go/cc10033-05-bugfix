"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。
新增跨模块数据通道：pipeline_log 记录跨模块流转日志，service_registry 维护服务引用。
"""
from __future__ import annotations

import time
from typing import Any

from app.seed import SEED_ROWS


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        self._pipeline_log: list[dict[str, Any]] = []
        self._service_registry: dict[str, Any] = {}
        self._metrics: dict[str, list[dict[str, Any]]] = {}
        self._event_queue: list[dict[str, Any]] = []
        # 样本版本台账：Schema匹配送检的每个样本都是不可变版本，
        # 质量抽检、数据导出只引用具体 sample_id，杜绝拿旧结论放行新样本。
        self._sample_versions: dict[str, dict[str, Any]] = {}
        self._sample_seq = 0
        # 字段确认记录：(sample_id, field) 维度，确认只对实际核过的值有效。
        self._field_confirmations: dict[tuple[str, str], dict[str, Any]] = {}
        # 受理幂等：request_id -> 首次受理结论，重送直接返回同一次结论。
        self._idempotent_responses: dict[str, dict[str, Any]] = {}
        # 导出采纳记录：export_id -> 放行时采用的样本与字段值快照。
        self._export_adoptions: dict[int, dict[str, Any]] = {}

    # ── 基础存取 ──────────────────────────────

    def module_names(self) -> list[str]:
        return sorted(self._tables)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def find_by(self, module: str, field: str, value: Any) -> list[dict[str, Any]]:
        return [row for row in self.rows(module) if str(row.get(field, "")) == str(value)]

    def upsert(self, module: str, entry: dict[str, Any]) -> dict[str, Any]:
        rows = self.rows(module)
        eid = entry.get("id")
        if eid is not None:
            for i, row in enumerate(rows):
                if row.get("id") == eid:
                    rows[i].update(entry)
                    return rows[i]
        max_id = max((int(r.get("id", 0)) for r in rows), default=0)
        entry["id"] = max_id + 1
        rows.append(entry)
        return entry

    # ── 跨模块管道日志 ─────────────────────────

    def pipeline_trace(
        self,
        from_module: str,
        to_module: str,
        from_id: int,
        to_id: int,
        action: str,
        detail: dict[str, Any] | None = None,
    ) -> None:
        """记录跨模块数据流日志，用于审计和题位源码锚点。"""
        self._pipeline_log.append({
            "ts": time.time(),
            "iso": "",  # 预留，调用方按需覆盖
            "from_module": from_module,
            "to_module": to_module,
            "from_id": from_id,
            "to_id": to_id,
            "action": action,
            "detail": detail or {},
        })

    def pipeline_log(self) -> list[dict[str, Any]]:
        return list(self._pipeline_log)

    # ── 指标收集 ──────────────────────────────

    def record_metric(self, module: str, metric_name: str, value: float, tags: dict[str, str] | None = None) -> None:
        self._metrics.setdefault(module, []).append({
            "ts": time.time(),
            "name": metric_name,
            "value": value,
            "tags": tags or {},
        })

    def metrics(self, module: str | None = None) -> list[dict[str, Any]]:
        if module:
            return list(self._metrics.get(module, []))
        result = []
        for mod_metrics in self._metrics.values():
            result.extend(mod_metrics)
        return result

    # ── 事件队列（异步编排预留） ─────────────────

    def enqueue_event(self, event_type: str, payload: dict[str, Any]) -> None:
        self._event_queue.append({"type": event_type, "payload": payload, "ts": time.time()})

    def dequeue_event(self) -> dict[str, Any] | None:
        return self._event_queue.pop(0) if self._event_queue else None

    def pending_events(self) -> int:
        return len(self._event_queue)

    # ── 拓扑概览 ──────────────────────────────

    def get_pipeline_topology(self) -> dict[str, list[str]]:
        """返回跨模块数据流拓扑，用于可视化题位。"""
        edges = {}
        for log in self._pipeline_log:
            fm = log["from_module"]
            tm = log["to_module"]
            edges.setdefault(fm, [])
            if tm not in edges[fm]:
                edges[fm].append(tm)
        return edges

    # ── 样本版本台账（Schema匹配 → 质量抽检 → 数据导出 共享链路） ──

    def register_sample_version(
        self,
        *,
        batch: str,
        schema_id: int,
        config_version: int,
        fields: dict[str, Any],
        sources: dict[str, Any],
        raw_sample: dict[str, Any],
        note: str = "",
    ) -> dict[str, Any]:
        """登记一个不可变的送检样本版本。

        同批次再次送检会生成新版本号，并把旧版本标记为 superseded，
        旧版本及其确认记录仍然保留，供运营回看仍在复核的字段。
        """
        self._sample_seq += 1
        sample_id = f"SMP-{batch}-{self._sample_seq:04d}"
        prior = self.latest_sample_version(batch)
        version_no = (prior["version"] + 1) if prior else 1
        sample = {
            "sample_id": sample_id,
            "batch": batch,
            "version": version_no,
            "schema_id": schema_id,
            "config_version": config_version,
            # fields/sources 做拷贝固化，后续配置更正不会回改已送检的样本
            "fields": dict(fields),
            "sources": dict(sources),
            "raw_sample": dict(raw_sample),
            "note": note,
            "status": "送检中",
            "claimed_by": None,
            "claimed_quality_id": None,
            "claimed_at": None,
            "superseded_by": None,
            "created_ts": time.time(),
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        self._sample_versions[sample_id] = sample
        if prior is not None and prior.get("superseded_by") is None:
            prior["superseded_by"] = sample_id
            if prior["status"] in ("送检中", "已领取", "复核中"):
                prior["status"] = "已被替代"
        return sample

    def find_sample_version(self, sample_id: str) -> dict[str, Any] | None:
        return self._sample_versions.get(sample_id)

    def sample_versions(self, batch: str | None = None) -> list[dict[str, Any]]:
        rows = list(self._sample_versions.values())
        if batch is not None:
            rows = [row for row in rows if row["batch"] == batch]
        return sorted(rows, key=lambda row: (row["batch"], row["version"]))

    def latest_sample_version(self, batch: str) -> dict[str, Any] | None:
        rows = self.sample_versions(batch)
        return rows[-1] if rows else None

    def claim_sample_version(
        self, sample_id: str, operator: str, quality_check_id: int
    ) -> tuple[dict[str, Any] | None, str]:
        """把样本领给某个抽检任务/运营；同一时刻一份样本只能在一个单子上。"""
        sample = self.find_sample_version(sample_id)
        if sample is None:
            return None, "样本不存在或已归档"
        holder = sample.get("claimed_quality_id")
        if holder is not None and holder != quality_check_id:
            return sample, f"样本已被抽检任务 {holder} 领走"
        sample["claimed_by"] = operator
        sample["claimed_quality_id"] = quality_check_id
        sample["claimed_at"] = time.time()
        if sample["status"] in ("送检中",):
            sample["status"] = "已领取"
        return sample, "领取成功"

    def confirm_field(
        self,
        sample_id: str,
        field: str,
        *,
        operator: str,
        expected_value: Any = None,
        passed: bool = True,
        remark: str = "",
    ) -> tuple[dict[str, Any] | None, str, bool]:
        """确认样本上的某个字段。

        返回 (记录, 消息, 是否本次新建)。确认只对登记时的字段值生效：
        传入 expected_value 且与样本固化值不一致时拒绝，旧确认不能挪用到新样本。
        """
        sample = self.find_sample_version(sample_id)
        if sample is None:
            return None, "样本不存在或已归档", False
        if field not in sample["fields"]:
            return None, f"样本 {sample_id} 不含字段 {field}", False
        actual_value = sample["fields"][field]
        if expected_value is not None and str(expected_value) != str(actual_value):
            return None, (
                f"字段值已变更：确认针对 {expected_value!r}，"
                f"该样本当前固化值为 {actual_value!r}，请按最新送检版重新核对"
            ), False
        key = (sample_id, field)
        existing = self._field_confirmations.get(key)
        if existing is not None:
            return existing, "该字段已确认，返回首次受理结论", False
        record = {
            "sample_id": sample_id,
            "batch": sample["batch"],
            "field": field,
            "value": actual_value,
            "source": sample["sources"].get(field),
            "passed": bool(passed),
            "operator": operator,
            "remark": remark,
            "request_id": None,
            "confirmed_ts": time.time(),
            "confirmed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        self._field_confirmations[key] = record
        if sample["status"] in ("已领取", "送检中"):
            sample["status"] = "复核中"
        return record, "字段确认已受理", True

    def field_confirmations(self, sample_id: str) -> list[dict[str, Any]]:
        return [
            dict(rec)
            for rec in self._field_confirmations.values()
            if rec["sample_id"] == sample_id
        ]

    def remember_idempotent(self, request_id: str, conclusion: dict[str, Any]) -> dict[str, Any]:
        """登记幂等受理结论；同一 request_id 重送时原样返回。"""
        if request_id not in self._idempotent_responses:
            stored = dict(conclusion)
            stored["request_id"] = request_id
            stored["accepted_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            self._idempotent_responses[request_id] = stored
        return self._idempotent_responses[request_id]

    def idempotent_response(self, request_id: str) -> dict[str, Any] | None:
        return self._idempotent_responses.get(request_id)

    def record_export_adoption(self, export_id: int, adoption: dict[str, Any]) -> None:
        self._export_adoptions[export_id] = dict(adoption)

    def export_adoption(self, export_id: int) -> dict[str, Any] | None:
        return self._export_adoptions.get(export_id)

    # ── 看板概览 ──────────────────────────────

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}


store = Store()