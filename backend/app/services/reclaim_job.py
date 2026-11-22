"""reclaim_job 领域服务：回收清理管理，接收 retention_rule 上游事件。

跨模块数据流：
    retention_rule → reclaim_job: 超过保留期触发回收清理
    reclaim_job → retention_rule: 回执回写分区释放状态

回收资格的证据链：
    提交申请 → 按提交时刻的规则版本 + 豁免记录生成资格明细（批次1）
    确认执行 → 按执行时刻重新评估（批次2），只释放「可回收」分区
    重新打开 → 再次评估，已清理分区的资格为「已清理」，不会被重复释放

每次执行都会落一条回执，同时记录本次释放范围、豁免/未到期/待复核而未处置的范围，
以及采用的规则版本与资格编号，便于值班人核对。
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any

from app import clock
from app.services import retention_engine as engine
from app.services.base import BaseModuleService
from app.store import store

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "assets")

_PARTITION_SPLIT = str.maketrans({"，": ",", "、": ",", ";": ",", "；": ",", " ": ","})


def _split_partitions(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, (list, tuple, set)):
        items = [str(item) for item in raw]
    else:
        items = str(raw).translate(_PARTITION_SPLIT).split(",")
    seen: list[str] = []
    for item in items:
        value = item.strip()
        if value and value not in seen:
            seen.append(value)
    return seen


def _compact(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "资格编号": row.get("资格编号", ""),
        "分区编号": row.get("分区编号", ""),
        "分区键": row.get("分区键", ""),
        "资格状态": row.get("资格状态", ""),
        "是否可回收": bool(row.get("是否可回收")),
        "到期时间": row.get("到期时间", ""),
        "规则编号": row.get("规则编号", ""),
        "规则版本": row.get("规则版本", ""),
        "保留天数": row.get("保留天数"),
        "评估时间": row.get("评估时间", ""),
        "评估批次": row.get("评估批次", ""),
        "复核层级": row.get("复核层级", ""),
        "阻断原因": row.get("阻断原因", ""),
    }


class ReclaimJobService(BaseModuleService):
    MODULE = "reclaim_job"
    ENTITY = "回收任务"
    OPERATION_TERM = "归并"
    LIST_FIELDS = ["任务编号", "目标资产", "分区清单", "回收范围", "回收方式",
                   "影响数据量", "执行时间", "恢复窗口", "任务状态", "回执编号"]
    REQUIRED_FIELDS = ["目标资产", "分区清单"]
    STATUS_ORDER = ["待确认", "执行中", "已完成", "已回滚"]
    ACTION_RULES = {"确认执行": "执行中", "标记完成": "已完成", "发起回滚": "已回滚",
                    "重新打开": "待确认"}
    NEGATIVE_ACTIONS = []

    def __init__(self):
        super().__init__()
        os.makedirs(DATA_DIR, exist_ok=True)

    # ------------------------------------------------------------ 申请登记
    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        """登记回收申请：落库时按当时有效的规则与豁免生成第一批资格明细。"""
        asset_id = str(
            values.get("目标资产") or values.get("asset_id") or ""
        ).strip()
        raw_partitions = (
            values.get("分区清单") or values.get("partitions")
            or values.get("partition_ids")
        )
        partition_ids = _split_partitions(raw_partitions)

        missing: list[str] = []
        if not asset_id:
            missing.append("目标资产")
        if not partition_ids:
            missing.append("分区清单")
        if missing:
            return None, missing

        entry = self._new_application_row(values, asset_id, partition_ids)
        store.rows(self.MODULE).append(entry)
        self.evaluate(entry["id"], purpose="申请提交")
        return entry, []

    def _new_application_row(self, values: dict[str, Any], asset_id: str,
                             partition_ids: list[str]) -> dict[str, Any]:
        next_id = store.next_id(self.MODULE)
        task_id = f"RECL-{next_id:04d}"
        scope = str(values.get("回收范围") or "").strip() or (
            f"过期分区释放：{', '.join(partition_ids)}")
        return {
            "id": next_id,
            "status": "待确认",
            "pending": True,
            "abnormal": False,
            "任务编号": task_id,
            "申请编号": task_id,
            "目标资产": asset_id,
            "分区清单": ",".join(partition_ids),
            "回收范围": scope,
            "回收方式": str(values.get("回收方式") or "逻辑删除"),
            "影响数据量": "待评估",
            "执行时间": "",
            "恢复窗口": str(values.get("恢复窗口") or "7天"),
            "任务状态": "待确认",
            "回执编号": "",
            "资格批次": "",
            "资格明细": [],
            "资格批次记录": [],
        }

    # ------------------------------------------------------------ 资格明细
    def _application_partitions(self, entry: dict[str, Any]) -> list[dict[str, Any] | None]:
        asset_id = str(entry.get("目标资产", ""))
        resolved: list[dict[str, Any] | None] = []
        for key in _split_partitions(entry.get("分区清单")):
            partition = engine.find_partition(key)
            if partition is None:
                for candidate in engine.partitions_for_asset(asset_id):
                    if str(candidate.get("分区键")) == key:
                        partition = candidate
                        break
            resolved.append(partition)
        return resolved

    def evaluate(self, entry_id: int, purpose: str = "执行",
                 as_of: datetime | None = None) -> tuple[list[dict[str, Any]], str]:
        """按评估时刻重算资格，追加一批明细，并把摘要挂到申请上。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return [], ""
        moment = as_of or clock.now()
        asset_id = str(entry.get("目标资产", ""))
        batch = engine.next_batch_id()
        rows: list[dict[str, Any]] = []
        for partition in self._application_partitions(entry):
            if partition is None:
                rows.append(engine.make_unresolved_row(
                    asset_id=asset_id,
                    partition_id="",
                    as_of=moment,
                    purpose=purpose,
                    reason="申请中的分区未在台账登记，无法核对到期规则",
                    batch=batch,
                    application_id=str(entry.get("任务编号", "")),
                    task_id=str(entry.get("任务编号", "")),
                ))
                continue
            if str(partition.get("资产编号")) != asset_id:
                rows.append(engine.make_unresolved_row(
                    asset_id=asset_id,
                    partition_id=str(partition.get("分区编号", "")),
                    as_of=moment,
                    purpose=purpose,
                    reason=(f"分区属于资产 {partition.get('资产编号')}，"
                            f"与申请资产 {asset_id} 不一致"),
                    batch=batch,
                    application_id=str(entry.get("任务编号", "")),
                    task_id=str(entry.get("任务编号", "")),
                ))
                continue
            row = engine.evaluate_partition(partition, moment, purpose)
            row["申请编号"] = str(entry.get("任务编号", ""))
            row["任务编号"] = str(entry.get("任务编号", ""))
            row["评估批次"] = batch
            rows.append(row)

        stored = engine.store_eligibility(rows)
        entry["资格批次"] = batch
        entry["资格明细"] = [_compact(row) for row in stored]
        history = list(entry.get("资格批次记录") or [])
        history.append({
            "评估批次": batch,
            "评估用途": purpose,
            "评估时间": moment.isoformat(),
            "明细": [_compact(row) for row in stored],
        })
        entry["资格批次记录"] = history
        return stored, batch

    def eligibility(self, entry_id: int,
                    batch: str | None = None) -> list[dict[str, Any]]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return []
        rows = engine.list_eligibility(str(entry.get("任务编号", "")))
        if batch:
            rows = [row for row in rows if row.get("评估批次") == batch]
        return rows

    def receipts(self, entry_id: int) -> list[dict[str, Any]]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return []
        return engine.list_receipts(str(entry.get("任务编号", "")))

    # ------------------------------------------------------------ 执行
    def execute_reclaim(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在"
        if entry.get("status") == "已完成":
            return None, (f"{self.ENTITY} {entry_id} 已完成；如确需重试，"
                          f"请先「重新打开」，已清理分区不会被重复回收")
        if entry.get("status") == "执行中":
            return None, (f"{self.ENTITY} {entry_id} 已在执行中；如需按最新豁免/规则"
                          f"重试，请先「重新打开」")
        if entry.get("status") == "已回滚":
            return None, f"{self.ENTITY} {entry_id} 已回滚；如确需重试，请先「重新打开」"

        asset_id = str(entry.get("目标资产", ""))
        moment = clock.now()
        receipt_id = engine.next_receipt_id()
        rows, batch = self.evaluate(entry_id, purpose="执行", as_of=moment)

        # 整份申请待复核：申请范围里有无法核对的分区时，不在范围未定情况下
        # 部分释放，全部留到人工复核之后再执行。
        application_review = any(
            row.get("资格状态") == engine.UNDER_REVIEW
            and row.get("复核层级") == "申请"
            for row in rows
        )
        released: list[dict[str, Any]] = []
        if not application_review:
            for row in rows:
                if row.get("资格状态") != engine.ELIGIBLE:
                    continue
                partition = engine.find_partition(str(row.get("分区编号", "")))
                if partition is None:
                    continue
                record = engine.record_cleaned(
                    asset_id=asset_id,
                    partition_id=str(row.get("分区编号", "")),
                    partition_key=str(row.get("分区键", "")),
                    receipt_id=receipt_id,
                    application_id=str(entry.get("任务编号", "")),
                    released_at=moment,
                )
                if record is None:
                    # 已经清理过：资格应在上一步变成「已清理」，这里再兜一次底。
                    continue
                released.append({"row": row, "record": record})

        receipt_status = self._receipt_status(rows, released, application_review)
        receipt = self._build_receipt(
            entry=entry,
            asset_id=asset_id,
            rows=rows,
            released=released,
            batch=batch,
            moment=moment,
            status=receipt_status,
            receipt_id=receipt_id,
            application_review=application_review,
        )
        stored_receipt = engine.store_receipt(receipt)

        for item in released:
            engine.mark_partition_released(
                str(item["row"].get("分区编号", "")),
                str(stored_receipt["回执编号"]),
                moment,
            )

        entry["回执编号"] = stored_receipt["回执编号"]
        entry["执行时间"] = moment.strftime("%Y-%m-%d %H:%M")
        entry["影响数据量"] = f"{len(released)} 个分区"
        if released:
            entry["status"] = "执行中"
            entry["任务状态"] = "执行中"
            entry["pending"] = True
        else:
            entry["status"] = "待确认"
            # 已经释放过（重试时全部命中「已清理」）保持「执行中」，表示这份申请
            # 先前已经执行完毕，只是本次没有新的可释放分区。
            already_released = any(row.get("资格状态") == engine.CLEANED for row in rows)
            if receipt_status in ("待复核", "整份申请待复核"):
                entry["任务状态"] = "待复核"
            elif already_released:
                entry["status"] = "执行中"
                entry["任务状态"] = "执行中"
            else:
                entry["任务状态"] = "暂无释放"
            entry["pending"] = True
        entry["abnormal"] = receipt_status in ("待复核", "整份申请待复核")

        self._write_reclaim_record(asset_id, entry)
        if released:
            self._update_asset_state(asset_id)

        return entry, self._execution_message(entry, stored_receipt, released)

    def _receipt_status(self, rows: list[dict[str, Any]],
                        released: list[dict[str, Any]],
                        application_review: bool = False) -> str:
        if application_review:
            return "整份申请待复核"
        has_review = any(row.get("资格状态") == engine.UNDER_REVIEW for row in rows)
        if released and has_review:
            return "部分释放待复核"
        if released:
            return "部分释放" if any(
                row.get("资格状态") in (engine.EXEMPTED, engine.NOT_DUE,
                                       engine.NO_RULE)
                for row in rows) else "已释放"
        if has_review:
            return "待复核"
        return "已释放" if any(
            row.get("资格状态") == engine.CLEANED for row in rows) else "无可释放"

    def _build_receipt(self, *, entry: dict[str, Any], asset_id: str,
                       rows: list[dict[str, Any]], released: list[dict[str, Any]],
                       batch: str, moment: datetime, status: str,
                       receipt_id: str = "",
                       application_review: bool = False) -> dict[str, Any]:
        def partitions_with(state: str) -> list[str]:
            return [
                str(row.get("分区编号", ""))
                for row in rows if row.get("资格状态") == state
            ]

        released_ids = [str(item["row"].get("分区编号", "")) for item in released]
        basis = [
            {
                "分区编号": row.get("分区编号", ""),
                "规则编号": row.get("规则编号", ""),
                "规则版本": row.get("规则版本", ""),
                "保留天数": row.get("保留天数"),
                "到期时间": row.get("到期时间", ""),
                "资格状态": row.get("资格状态", ""),
            }
            for row in rows
        ]
        pending_states = {
            "豁免保护": partitions_with(engine.EXEMPTED),
            "未到期": partitions_with(engine.NOT_DUE),
            "待复核": partitions_with(engine.UNDER_REVIEW),
            "规则不适用": partitions_with(engine.NO_RULE),
            "已清理跳过": partitions_with(engine.CLEANED),
        }
        return {
            "回执编号": receipt_id,
            "申请编号": str(entry.get("任务编号", "")),
            "任务编号": str(entry.get("任务编号", "")),
            "资产编号": asset_id,
            "申请范围": _split_partitions(entry.get("分区清单")),
            "释放范围": released_ids,
            "未处置范围": pending_states,
            "回执状态": status,
            "复核层级": "申请" if application_review else "",
            "评估批次": batch,
            "执行用途": "执行",
            "执行时间": moment.isoformat(),
            "规则依据": basis,
            "资格编号": [row.get("资格编号", "") for row in rows],
            "规则版本清单": sorted({
                str(row.get("规则版本")) for row in rows if row.get("规则版本")
            }),
            "记录时间": clock.now_iso(),
        }

    def _execution_message(self, entry: dict[str, Any], receipt: dict[str, Any],
                           released: list[dict[str, Any]]) -> str:
        released_ids = receipt.get("释放范围") or []
        blocked = receipt.get("未处置范围") or {}
        blocked_bits = [
            f"{label} {len(ids)} 个" for label, ids in blocked.items() if ids
        ]
        head = f"{self.ENTITY} {entry.get('id')} 已按资格明细处理"
        if released_ids:
            head += f"，释放 {len(released_ids)} 个分区（{', '.join(released_ids)}）"
        else:
            head += "，本次没有可释放分区"
        if blocked_bits:
            head += "；未处置：" + "、".join(blocked_bits)
        head += f"；回执 {receipt.get('回执编号')}，评估批次 {receipt.get('评估批次')}"
        return head

    # ------------------------------------------------------------ 重开与完成
    def reopen(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        """重新打开回收任务：回到待确认，等待下一次按最新规则重算资格。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在"
        previous = str(entry.get("status", ""))
        entry["status"] = "待确认"
        entry["pending"] = True
        entry["abnormal"] = False
        entry["任务状态"] = "待确认"
        return entry, (f"{self.ENTITY} {entry_id} 已重新打开（原状态：{previous}），"
                       f"再次执行将按当时有效规则重算，已清理分区不会重复释放")

    def complete_reclaim(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在"
        if entry.get("status") != "执行中":
            return None, f"{self.ENTITY} {entry_id} 尚未执行，不能直接标记完成"

        entry["status"] = "已完成"
        entry["pending"] = False
        entry["任务状态"] = "已完成"
        return entry, f"{self.ENTITY} {entry_id} 已完成，释放范围以回执为准"

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        if action == "确认执行":
            return self.execute_reclaim(entry_id)
        if action == "标记完成":
            return self.complete_reclaim(entry_id)
        if action == "重新打开":
            return self.reopen(entry_id)
        return super().run_action(entry_id, action)

    def _write_reclaim_record(self, asset_id: str, entry: dict[str, Any]) -> None:
        task_id = str(entry.get("任务编号", ""))
        filename = f"reclaim_{task_id}_{asset_id}.json"
        path = os.path.join(DATA_DIR, filename)
        record = {
            "task_id": task_id,
            "asset_id": asset_id,
            "scope": str(entry.get("回收范围", "")),
            "partitions": str(entry.get("分区清单", "")),
            "method": str(entry.get("回收方式", "")),
            "data_volume": str(entry.get("影响数据量", "")),
            "execution_time": str(entry.get("执行时间", "")),
            "recovery_window": str(entry.get("恢复窗口", "")),
            "status": str(entry.get("任务状态", "")),
            "receipt_id": str(entry.get("回执编号", "")),
            "updated_at": clock.now_iso(),
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2, default=str)

    def _update_asset_state(self, asset_id: str) -> None:
        """回收执行时标记关联资产为异常待清理。"""
        asset_rows = store.rows("asset_catalog")
        for row in asset_rows:
            if str(row.get("资产编号")) == asset_id:
                row["pending"] = True
                row["最近更新"] = clock.today()
                break
