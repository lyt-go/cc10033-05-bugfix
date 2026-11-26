"""数据导出领域服务：采集数据批量导出、格式转换与交付路径。

跨模块数据流：
- export_dataset ← quality_check: 质量抽检通过后触发导出

链路修复要点：
- 导出申请按抽检单校验当前最新送检样本：只有该样本全部字段实际确认、
  抽检单处于通过状态才放行；旧样本留下的通过状态不能放行新样本，
  未满足条件时返回「还需补做抽检」，不会生成凭旧状态放行的导出任务。
- 每个导出任务固化放行样本及其实际采用的字段值/来源，已生成的导出
  随时可通过 adoption 追溯回当时采用的值。
- 同一抽检单同一负责人的申请幂等，避免重复申请在导出列表里多出任务。
"""
from __future__ import annotations

import random
import time
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class ExportDatasetService(BaseModuleService):
    MODULE = "export_dataset"
    ENTITY = "导出任务"
    OPERATION_TERM = "采集"
    LIST_FIELDS = ["任务编号", "数据来源", "导出格式", "字段筛选", "行数范围", "压缩方式", "文件路径", "导出时间"]
    REQUIRED_FIELDS = ["任务编号", "数据来源", "导出格式"]
    STATUS_ORDER = ["待执行", "导出中", "已完成", "已失败"]
    ACTION_RULES = {"发起导出": "导出中", "标记完成": "已完成", "标记失败": "已失败"}

    SUPPORTED_FORMATS = ["CSV", "JSON", "Parquet", "Excel", "XML", "Avro"]
    COMPRESSION = ["none", "gzip", "snappy", "lz4", "zstd"]

    def create_entry(self, values: dict[str, Any]) -> tuple:
        entry, missing = super().create_entry(values)
        if entry is None:
            return None, missing
        entry["_export_progress"] = 0.0
        entry["_exported_rows"] = 0
        entry["_export_errors"] = []
        entry["_start_ts"] = 0
        entry["_quality_check_id"] = values.get("_quality_check_id")
        entry["_applicant"] = values.get("_applicant")
        entry["_adopted_sample_id"] = None
        store.record_metric("export_dataset", "export_task_created", 1,
                            {"format": values.get("导出格式", ""),
                             "source": values.get("数据来源", "")})
        return entry, []

    # ── 抽检联动申请（修复链路入口） ───────────────────────────

    def apply_for_export(
        self,
        quality_check_id: int,
        *,
        applicant: str = "导出负责人",
        export_format: str = "CSV",
    ) -> dict[str, Any]:
        """导出负责人按抽检单申请导出。

        返回 ok/消息与任务（若已创建）。负责人能直接看到自己拿到的是哪份
        通过抽检的样本，或为什么还需要补做抽检。
        """
        quality = store.find("quality_check", quality_check_id)
        if quality is None:
            return {"ok": False, "message": f"抽检任务 {quality_check_id} 不存在", "entry": None}
        batch = quality.get("数据批次", "")
        latest = store.latest_sample_version(batch)

        # 幂等：同一抽检单+负责人已申请过，直接回给同一笔任务
        for row in store.rows(self.MODULE):
            if row.get("_quality_check_id") == quality_check_id and row.get("_applicant") == applicant:
                return {
                    "ok": row.get("status") == "已完成",
                    "message": "该申请已受理，返回已登记的导出任务",
                    "entry": row,
                    "sample_id": row.get("_adopted_sample_id"),
                    "idempotent": True,
                }

        if latest is None:
            return {"ok": False, "message": f"批次 {batch} 还没有送检样本，需补做抽检",
                    "entry": None, "sample_id": None}
        confirmations = store.field_confirmations(latest["sample_id"])
        all_confirmed = len(confirmations) >= len(latest["fields"]) and len(latest["fields"]) > 0
        if quality.get("status") != "通过" or not all_confirmed:
            held_id = quality.get("_current_sample_id")
            return {
                "ok": False,
                "entry": None,
                "sample_id": latest["sample_id"],
                "held_sample_id": held_id,
                "message": (
                    f"最新送检样本 {latest['sample_id']}（配置v{latest['config_version']}）"
                    "尚未完成抽检放行，请先领取并确认全部字段后再申请导出"
                ),
                "need_rerun_check": True,
            }

        entry, _ = self.create_entry({
            "任务编号": f"EXPO-Q{quality_check_id}-{latest['version']}",
            "数据来源": f"{batch}/{latest['sample_id']}",
            "导出格式": export_format,
            "字段筛选": ",".join(latest["fields"].keys()),
            "_quality_check_id": quality_check_id,
            "_applicant": applicant,
        })
        entry["_adopted_sample_id"] = latest["sample_id"]
        self.execute_export(entry["id"])
        # 固化实际采用的字段值，已生成的导出可永久查回
        store.record_export_adoption(entry["id"], {
            "export_id": entry["id"],
            "quality_check_id": quality_check_id,
            "batch": batch,
            "sample_id": latest["sample_id"],
            "sample_version": latest["version"],
            "config_version": latest["config_version"],
            "applicant": applicant,
            "fields": dict(latest["fields"]),
            "sources": dict(latest["sources"]),
            "confirmations": store.field_confirmations(latest["sample_id"]),
            "adopted_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        })
        store.pipeline_trace(
            "quality_check", "export_dataset", quality_check_id, entry["id"],
            "quality_passed",
            {"sample_id": latest["sample_id"], "batch": batch, "applicant": applicant},
        )
        return {
            "ok": True,
            "message": f"已按通过抽检的样本 {latest['sample_id']} 放行导出",
            "entry": entry,
            "sample_id": latest["sample_id"],
            "adoption": store.export_adoption(entry["id"]),
        }

    def get_export_trace(self, entry_id: int) -> dict[str, Any] | None:
        """查回导出任务实际采用的样本与字段值。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None
        adoption = store.export_adoption(entry_id)
        return {
            "export_id": entry_id,
            "任务编号": entry.get("任务编号"),
            "status": entry.get("status"),
            "adopted_sample_id": entry.get("_adopted_sample_id"),
            "quality_check_id": entry.get("_quality_check_id"),
            "applicant": entry.get("_applicant"),
            "adoption": adoption,
        }

    def execute_export(self, entry_id: int, data: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
        """执行数据导出。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None
        entry["status"] = "导出中"
        entry["_start_ts"] = time.time()
        fmt = entry.get("导出格式", "CSV")
        if fmt not in self.SUPPORTED_FORMATS:
            entry["status"] = "已失败"
            entry["_export_errors"].append(f"不支持的格式：{fmt}")
            entry["abnormal"] = True
            return entry
        # 模拟导出
        self._simulate_export()
        row_count = data if isinstance(data, int) else random.randint(100, 100000)
        entry["_exported_rows"] = row_count if isinstance(row_count, int) else len(data) if data else 0
        entry["行数范围"] = f"1-{entry['_exported_rows']}"
        entry["文件路径"] = f"/exports/{entry.get('任务编号', 'TASK')}.{fmt.lower()}"
        compression = entry.get("压缩方式", "none")
        if compression != "none":
            entry["文件路径"] += f".{compression}"
        entry["导出时间"] = time.strftime("%Y-%m-%d %H:%M:%S")
        entry["status"] = "已完成"
        entry["pending"] = False
        entry["_export_progress"] = 100.0
        store.record_metric("export_dataset", "export_completed", entry["_exported_rows"],
                            {"format": fmt, "compression": compression})
        return entry

    def export_with_quality_check(self, entry_id: int, quality_check_id: int,
                                  passed: bool) -> dict[str, Any] | None:
        """质量抽检通过后触发导出。跨模块：quality_check → export_dataset

        链路修复后：passed=True 还必须能找到已完整确认的最新样本，
        否则拒绝放行（旧的通过状态不足以导出新样本）。
        """
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None
        if not passed:
            entry["status"] = "已失败"
            entry["abnormal"] = True
            return entry
        quality = store.find("quality_check", quality_check_id)
        batch = quality.get("数据批次", "") if quality else ""
        latest = store.latest_sample_version(batch) if batch else None
        if latest is None:
            entry["status"] = "已失败"
            entry["abnormal"] = True
            entry["_export_errors"].append("没有可追溯的送检样本，拒绝凭旧状态放行")
            return entry
        confirmations = store.field_confirmations(latest["sample_id"])
        if len(confirmations) < len(latest["fields"]) or not latest["fields"]:
            entry["status"] = "已失败"
            entry["abnormal"] = True
            entry["_export_errors"].append(
                f"样本 {latest['sample_id']} 尚未完成全部字段确认，需补做抽检")
            return entry
        entry["_quality_check_id"] = quality_check_id
        entry["_adopted_sample_id"] = latest["sample_id"]
        entry["数据来源"] = f"{batch}/{latest['sample_id']}"
        store.record_export_adoption(entry["id"], {
            "export_id": entry["id"],
            "quality_check_id": quality_check_id,
            "batch": batch,
            "sample_id": latest["sample_id"],
            "sample_version": latest["version"],
            "config_version": latest["config_version"],
            "fields": dict(latest["fields"]),
            "sources": dict(latest["sources"]),
            "confirmations": confirmations,
            "adopted_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        })
        store.pipeline_trace(
            "quality_check", "export_dataset", quality_check_id, entry_id,
            "quality_passed", {"action": "trigger_export",
                                "sample_id": latest["sample_id"]}
        )
        return self.execute_export(entry_id)

    def format_to_bytes(self, entry_id: int) -> dict[str, Any] | None:
        """模拟格式转换，返回文件大小。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None
        sizes = {"CSV": 1.2, "JSON": 1.5, "Parquet": 0.6, "Excel": 2.0, "XML": 2.5, "Avro": 0.4}
        row_factor = entry.get("_exported_rows", 1000) / 1000
        size_mb = sizes.get(entry.get("导出格式", "CSV"), 1.0) * row_factor
        return {"format": entry.get("导出格式"), "rows": entry.get("_exported_rows"),
                "size_mb": round(size_mb, 2), "compression": entry.get("压缩方式", "none")}

    def _simulate_export(self) -> None:
        time.sleep(random.uniform(0.05, 0.2))

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        tasks = store.rows(self.MODULE)
        base["completed_exports"] = sum(1 for t in tasks if t.get("status") == "已完成")
        base["total_rows_exported"] = sum(t.get("_exported_rows", 0) for t in tasks)
        base["traceable_exports"] = sum(1 for t in tasks if t.get("_adopted_sample_id"))
        return base
