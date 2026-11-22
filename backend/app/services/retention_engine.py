"""保留期与豁免判定内核：分区台账、规则版本、豁免校验、回收资格明细与回执。

这里的约束是回收链路的底线：

1. 到期判断只认「同一资产 + 同一适用时段」解析出来的规则版本，不复用别的资产
   或别的时段的保留天数。
2. 豁免按「批准生效时间 ≤ 评估时刻 < 失效时间」判定。解除豁免只影响之后的判断，
   已经写入的资格证据不会被回头改写。
3. 资格明细只追加不修改；每次评估生成一个批次，证据快照里冻结当时的规则、分区、
   豁免与到期阈值。回执引用资格编号，释放台账独立记录，重试时据此防重复回收。
"""
from __future__ import annotations

import copy
import json
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from app import clock
from app.store import store

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "assets")

# 领域内部表：不作为业务模块出现在运营概览里。
PARTITION_TABLE = "retention_partition"
EXEMPTION_TABLE = "partition_exemption"
RULE_HISTORY_TABLE = "retention_rule_history"
ELIGIBILITY_TABLE = "reclaim_eligibility"
RECEIPT_TABLE = "reclaim_receipt"
CLEANED_TABLE = "reclaim_cleaned"

# 资格状态
ELIGIBLE = "可回收"
EXEMPTED = "豁免保护"
NOT_DUE = "未到期"
CLEANED = "已清理"
UNDER_REVIEW = "待复核"
NO_RULE = "规则不适用"

BOOTSTRAP_TIME = "1970-01-01T00:00:00+00:00"
_bootstrapped = False


# ---------------------------------------------------------------- 时间与取值
def parse_time(value: Any, default: datetime | None = None) -> datetime | None:
    """把 ISO 字符串 / 日期 / datetime 统一成带 UTC 时区的 datetime。"""
    if value is None or value == "":
        return default
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip()
    if not text:
        return default
    candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        try:
            parsed = datetime.strptime(text[:10], "%Y-%m-%d")
        except ValueError:
            return default
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def to_iso(value: Any) -> str:
    parsed = parse_time(value)
    return parsed.isoformat() if parsed else ""


def _version_number(text: Any) -> int:
    digits = "".join(ch for ch in str(text) if ch.isdigit())
    return int(digits) if digits else 0


def _valid_days(value: Any) -> int | None:
    try:
        days = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return days if days >= 0 else None


def _partition_period(partition: dict[str, Any]) -> tuple[datetime | None, datetime | None]:
    anchor = parse_time(partition.get("数据截止时间"))
    start = parse_time(partition.get("适用开始"), anchor)
    end = parse_time(partition.get("适用结束"), anchor)
    return start, end


def _rule_matches_period(rule: dict[str, Any],
                         start: datetime | None,
                         end: datetime | None) -> bool:
    rule_start = parse_time(rule.get("适用开始"))
    rule_end = parse_time(rule.get("适用结束"))
    if rule_start and start and start < rule_start:
        return False
    if rule_end and end and end > rule_end:
        return False
    return True


def same_applicable_period(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return _partition_period(left) == _partition_period(right)


# ---------------------------------------------------------------- 规则版本
def bootstrap_rule_history() -> None:
    """把历史上已经处于「已生效」的规则补进版本表。

    只在第一次解析时执行一次；生效时间记到纪元起点，表示这条规则在执行回收之前
    就已经存在，不会被当成「未来才生效」。
    """
    global _bootstrapped
    if _bootstrapped:
        return
    _bootstrapped = True
    history = store.rows(RULE_HISTORY_TABLE)
    known = {(str(row.get("规则编号")), str(row.get("规则版本"))) for row in history}
    for rule in store.rows("retention_rule"):
        if rule.get("status") != "已生效":
            continue
        if _valid_days(rule.get("保留天数")) is None:
            continue
        version = str(rule.get("规则版本") or "v1")
        key = (str(rule.get("规则编号")), version)
        if key in known:
            continue
        history.append(_rule_history_row(rule, "生效", BOOTSTRAP_TIME))
        known.add(key)


def _rule_history_row(rule: dict[str, Any], event: str,
                      effective_at: str) -> dict[str, Any]:
    return {
        "id": store.next_id(RULE_HISTORY_TABLE),
        "规则编号": str(rule.get("规则编号", "")),
        "资产编号": str(rule.get("目标资产", "")),
        "规则版本": str(rule.get("规则版本", "")),
        "保留天数": str(rule.get("保留天数", "")),
        "豁免条件": str(rule.get("豁免条件", "")),
        "适用开始": to_iso(rule.get("适用开始")),
        "适用结束": to_iso(rule.get("适用结束")),
        "状态": "已生效" if event == "生效" else "已停用",
        "事件": event,
        "生效时间": effective_at,
        "记录时间": clock.now_iso(),
    }


def record_rule_event(rule: dict[str, Any], event: str,
                      as_of: datetime | None = None) -> dict[str, Any]:
    """记录一次规则版本事件（生效 / 停用），返回写入的版本快照。"""
    effective = as_of or clock.now()
    version = str(rule.get("规则版本") or "").strip()
    if not version:
        versions = [
            _version_number(row.get("规则版本"))
            for row in store.rows(RULE_HISTORY_TABLE)
            if str(row.get("规则编号")) == str(rule.get("规则编号"))
        ]
        version = f"v{max(versions, default=0) + 1}"
        rule["规则版本"] = version
    row = _rule_history_row(rule, event, effective.isoformat())
    row["规则版本"] = version
    store.rows(RULE_HISTORY_TABLE).append(row)
    return copy.deepcopy(row)


def resolve_rule(asset_id: str, partition: dict[str, Any],
                 as_of: datetime) -> dict[str, Any] | None:
    """解析同一资产、同一适用时段下，评估时刻有效的规则版本。"""
    bootstrap_rule_history()
    start, end = _partition_period(partition)
    candidates: list[dict[str, Any]] = []
    for row in store.rows(RULE_HISTORY_TABLE):
        if str(row.get("资产编号")) != asset_id:
            continue
        if row.get("状态") not in ("已生效", "已停用"):
            continue
        effective = parse_time(row.get("生效时间"))
        if effective is None or effective > as_of:
            continue
        if not _rule_matches_period(row, start, end):
            continue
        candidates.append(row)
    if not candidates:
        return None
    candidates.sort(key=lambda row: (
        parse_time(row.get("生效时间")) or datetime.min.replace(tzinfo=timezone.utc),
        _version_number(row.get("规则版本")),
    ))
    latest = candidates[-1]
    if latest.get("状态") != "已生效":
        return None
    if _valid_days(latest.get("保留天数")) is None:
        return None
    return copy.deepcopy(latest)


# ---------------------------------------------------------------- 分区台账
def _slug(text: str) -> str:
    return re.sub(r"[^0-9A-Za-z_.-]+", "_", text).strip("_") or "PART"


def find_partition(partition_id: str) -> dict[str, Any] | None:
    for row in store.rows(PARTITION_TABLE):
        if str(row.get("分区编号")) == str(partition_id):
            return row
    return None


def partitions_for_asset(asset_id: str) -> list[dict[str, Any]]:
    return [
        row for row in store.rows(PARTITION_TABLE)
        if str(row.get("资产编号")) == str(asset_id)
    ]


def register_partition(
    *,
    asset_id: str,
    partition_key: str,
    data_until: Any,
    retention_days: Any = None,
    period_start: Any = None,
    period_end: Any = None,
    declared_expiry: Any = None,
    partition_id: str | None = None,
) -> dict[str, Any]:
    """登记一个分区；到期时间按「数据截止时间 + 保留天数」给出展示值。

    真正的到期判断会在评估时用当时生效的规则阈值重算，这里只是台账展示。
    """
    asset_id = str(asset_id).strip()
    partition_key = str(partition_key).strip()
    if not asset_id or not partition_key:
        raise ValueError("分区登记缺少资产编号或分区键")
    pid = str(partition_id).strip() if partition_id else (
        f"PART-{_slug(asset_id)}-{_slug(partition_key)}")
    if find_partition(pid) is not None:
        raise ValueError(f"分区 {pid} 已登记")
    data_dt = parse_time(data_until)
    if data_dt is None:
        raise ValueError("分区数据截止时间无法解析")
    days = _valid_days(retention_days)
    expiry = data_dt + timedelta(days=days) if days is not None else None
    start = parse_time(period_start, data_dt)
    end = parse_time(period_end, data_dt)
    row = {
        "id": store.next_id(PARTITION_TABLE),
        "分区编号": pid,
        "资产编号": asset_id,
        "分区键": partition_key,
        "数据截止时间": data_dt.isoformat(),
        "适用开始": start.isoformat() if start else "",
        "适用结束": end.isoformat() if end else "",
        "登记保留天数": str(days) if days is not None else "",
        "到期时间": expiry.isoformat() if expiry else "",
        "声明到期时间": to_iso(declared_expiry),
        "状态": "待回收",
        "豁免标记": "否",
        "已释放时间": "",
        "已释放回执": "",
    }
    store.rows(PARTITION_TABLE).append(row)
    return row


def mark_partition_exempt_flag(partition_id: str, exempt: bool) -> None:
    partition = find_partition(partition_id)
    if partition is not None:
        partition["豁免标记"] = "是" if exempt else "否"


def mark_partition_released(partition_id: str, receipt_id: str,
                            released_at: datetime) -> None:
    partition = find_partition(partition_id)
    if partition is None:
        return
    partition["状态"] = "已释放"
    partition["已释放时间"] = released_at.isoformat()
    partition["已释放回执"] = receipt_id


# ---------------------------------------------------------------- 豁免
def approve_exemption(
    *,
    asset_id: str,
    partition_ids: Iterable[str],
    effective_at: Any = None,
    reason: str = "",
    operator: str = "",
    scope: str = "分区",
) -> dict[str, Any]:
    """批准豁免；生效时间默认取当前时刻，之后才生效的豁免不影响更早的评估。"""
    targets = [str(item) for item in partition_ids]
    if scope != "全部" and not targets:
        raise ValueError("分区豁免必须指定至少一个分区")
    for pid in targets:
        partition = find_partition(pid)
        if partition is None:
            raise ValueError(f"分区 {pid} 不存在")
        if str(partition.get("资产编号")) != str(asset_id):
            raise ValueError(f"分区 {pid} 不属于资产 {asset_id}")
    effective = parse_time(effective_at) or clock.now()
    row = {
        "id": store.next_id(EXEMPTION_TABLE),
        "豁免编号": f"EXEM-{store.next_id(EXEMPTION_TABLE):04d}",
        "资产编号": str(asset_id),
        "豁免范围": scope,
        "分区清单": targets,
        "分区编号": targets[0] if len(targets) == 1 else "",
        "状态": "已批准",
        "生效时间": effective.isoformat(),
        "失效时间": "",
        "批准人": operator,
        "原因": reason,
        "申请编号": "",
        "记录时间": clock.now_iso(),
    }
    store.rows(EXEMPTION_TABLE).append(row)
    if scope == "全部":
        for partition in partitions_for_asset(asset_id):
            partition["豁免标记"] = "是"
    else:
        for pid in targets:
            mark_partition_exempt_flag(pid, True)
    return copy.deepcopy(row)


def lift_exemption(exemption_id: int, as_of: Any = None) -> dict[str, Any] | None:
    """解除豁免：写入失效时间，之后不再受保护；已有资格证据保持原样。"""
    row = store.find(EXEMPTION_TABLE, int(exemption_id))
    if row is None:
        return None
    moment = parse_time(as_of) or clock.now()
    row["状态"] = "已解除"
    row["失效时间"] = moment.isoformat()
    row["解除时间"] = moment.isoformat()
    if row.get("豁免范围") == "全部":
        for partition in partitions_for_asset(str(row.get("资产编号"))):
            partition["豁免标记"] = "否"
    else:
        for pid in row.get("分区清单") or []:
            mark_partition_exempt_flag(str(pid), False)
    return copy.deepcopy(row)


def find_exemption(asset_id: str, partition_id: str,
                   as_of: datetime) -> dict[str, Any] | None:
    for row in store.rows(EXEMPTION_TABLE):
        if str(row.get("资产编号")) != str(asset_id):
            continue
        targets = [str(item) for item in (row.get("分区清单") or [])]
        if row.get("分区编号"):
            targets.append(str(row.get("分区编号")))
        if row.get("豁免范围") != "全部" and str(partition_id) not in targets:
            continue
        effective = parse_time(row.get("生效时间"))
        expires = parse_time(row.get("失效时间"))
        if effective is None or effective > as_of:
            continue
        if expires is not None and as_of >= expires:
            continue
        return copy.deepcopy(row)
    return None


# ---------------------------------------------------------------- 释放台账
def find_cleaned(asset_id: str, partition_id: str) -> dict[str, Any] | None:
    for row in store.rows(CLEANED_TABLE):
        if (str(row.get("资产编号")) == str(asset_id)
                and str(row.get("分区编号")) == str(partition_id)):
            return copy.deepcopy(row)
    return None


def record_cleaned(*, asset_id: str, partition_id: str, partition_key: str,
                   receipt_id: str, application_id: str,
                   released_at: datetime) -> dict[str, Any] | None:
    """登记释放；同一资产同一分区只允许一条，重复调用返回 None（不重复回收）。"""
    if find_cleaned(asset_id, partition_id) is not None:
        return None
    row = {
        "id": store.next_id(CLEANED_TABLE),
        "资产编号": str(asset_id),
        "分区编号": str(partition_id),
        "分区键": str(partition_key),
        "回执编号": str(receipt_id),
        "申请编号": str(application_id),
        "释放时间": released_at.isoformat(),
        "记录时间": clock.now_iso(),
    }
    store.rows(CLEANED_TABLE).append(row)
    return copy.deepcopy(row)


# ---------------------------------------------------------------- 资格评估
def evaluate_partition(partition: dict[str, Any], as_of: datetime,
                       purpose: str) -> dict[str, Any]:
    """按评估时刻的规则与豁免，给出单个分区的回收资格明细（含证据快照）。"""
    asset_id = str(partition.get("资产编号", ""))
    partition_id = str(partition.get("分区编号", ""))
    rule = resolve_rule(asset_id, partition, as_of)
    exemption = find_exemption(asset_id, partition_id, as_of)
    cleaned = find_cleaned(asset_id, partition_id)

    days: int | None = None
    expires: datetime | None = None
    if rule is not None:
        days = _valid_days(rule.get("保留天数"))
        data_until = parse_time(partition.get("数据截止时间"))
        if days is not None and data_until is not None:
            expires = data_until + timedelta(days=days)

    declared = parse_time(partition.get("声明到期时间"))
    flag = str(partition.get("豁免标记") or "否")

    if cleaned is not None:
        status = CLEANED
        reason = f"已在回执 {cleaned.get('回执编号')} 释放，不重复回收"
    elif rule is None or expires is None:
        status = NO_RULE
        reason = "未找到匹配该资产与适用时段的生效保留规则"
    elif as_of < expires:
        status = NOT_DUE
        reason = f"到期时间 {expires.isoformat()} 晚于评估时刻 {as_of.isoformat()}"
    elif declared is not None and declared > expires:
        # 声明到期时间比规则阈值更晚：按规则回收会提前删除仍在声明保护期内的数据，
        # 这类冲突必须人工复核，不能直接释放。
        status = UNDER_REVIEW
        reason = (f"分区声明到期时间 {declared.isoformat()} 晚于规则阈值计算出的 "
                  f"{expires.isoformat()}，需人工复核")
    elif exemption is not None:
        status = EXEMPTED
        reason = f"豁免 {exemption.get('豁免编号')} 在评估时刻生效"
    elif flag == "是":
        status = UNDER_REVIEW
        reason = "分区带豁免标记，但评估时刻没有有效豁免记录，需人工复核"
    else:
        status = ELIGIBLE
        reason = f"数据截止时间加保留阈值 {days} 天后已到期"

    evidence = {
        "as_of": as_of.isoformat(),
        "purpose": purpose,
        "partition": copy.deepcopy(partition),
        "rule": copy.deepcopy(rule),
        "exemption": copy.deepcopy(exemption),
        "cleaned": copy.deepcopy(cleaned),
        "threshold": {
            "保留天数": days,
            "数据截止时间": to_iso(partition.get("数据截止时间")),
            "到期时间": expires.isoformat() if expires else "",
            "声明到期时间": declared.isoformat() if declared else "",
            "豁免标记": flag,
        },
    }
    return {
        "申请编号": "",
        "任务编号": "",
        "评估批次": "",
        "资格编号": "",
        "资产编号": asset_id,
        "分区编号": partition_id,
        "分区键": str(partition.get("分区键", "")),
        "适用开始": to_iso(partition.get("适用开始")),
        "适用结束": to_iso(partition.get("适用结束")),
        "到期时间": expires.isoformat() if expires else "",
        "规则编号": str(rule.get("规则编号", "")) if rule else "",
        "规则版本": str(rule.get("规则版本", "")) if rule else "",
        "保留天数": days,
        "评估时间": as_of.isoformat(),
        "评估用途": purpose,
        "资格状态": status,
        "是否可回收": status == ELIGIBLE,
        "复核层级": "分区" if status == UNDER_REVIEW else "",
        "阻断原因": reason,
        "证据快照": evidence,
    }


def next_batch_id() -> str:
    numbers = [
        _version_number(row.get("评估批次"))
        for row in store.rows(ELIGIBILITY_TABLE)
    ]
    return f"BATCH-{max(numbers, default=0) + 1:04d}"


def make_unresolved_row(*, asset_id: str, partition_id: str,
                        as_of: datetime, purpose: str, reason: str,
                        batch: str, application_id: str,
                        task_id: str) -> dict[str, Any]:
    """分区无法核对（未登记 / 资产不一致）时，按待复核生成一条资格明细。"""
    evidence = {
        "as_of": as_of.isoformat(),
        "purpose": purpose,
        "partition": None,
        "rule": None,
        "exemption": None,
        "cleaned": None,
        "threshold": {},
    }
    return {
        "申请编号": str(application_id),
        "任务编号": str(task_id),
        "评估批次": batch,
        "资格编号": "",
        "资产编号": str(asset_id),
        "分区编号": str(partition_id),
        "分区键": "",
        "适用开始": "",
        "适用结束": "",
        "到期时间": "",
        "规则编号": "",
        "规则版本": "",
        "保留天数": None,
        "评估时间": as_of.isoformat(),
        "评估用途": purpose,
        "资格状态": UNDER_REVIEW,
        "是否可回收": False,
        "复核层级": "申请",
        "阻断原因": reason,
        "证据快照": evidence,
    }


def store_eligibility(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把资格明细追加进库；只追加，不覆盖历史批次。"""
    stored: list[dict[str, Any]] = []
    table = store.rows(ELIGIBILITY_TABLE)
    for row in rows:
        entry = copy.deepcopy(row)
        entry["id"] = store.next_id(ELIGIBILITY_TABLE)
        entry["资格编号"] = f"ELIG-{entry['id']:04d}"
        table.append(entry)
        stored.append(copy.deepcopy(entry))
    return stored


def evaluate_application(
    *,
    application_id: str,
    task_id: str,
    asset_id: str,
    partitions: list[dict[str, Any]],
    as_of: datetime,
    purpose: str,
) -> tuple[list[dict[str, Any]], str]:
    """对一份申请覆盖的分区批量评估，落一批资格明细并返回。"""
    batch = next_batch_id()
    rows: list[dict[str, Any]] = []
    for partition in partitions:
        row = evaluate_partition(partition, as_of, purpose)
        row["申请编号"] = str(application_id)
        row["任务编号"] = str(task_id)
        row["评估批次"] = batch
        rows.append(row)
    return store_eligibility(rows), batch


def list_eligibility(application_id: str | None = None) -> list[dict[str, Any]]:
    rows = store.rows(ELIGIBILITY_TABLE)
    if application_id is None:
        return [copy.deepcopy(row) for row in rows]
    return [
        copy.deepcopy(row) for row in rows
        if str(row.get("申请编号")) == str(application_id)
    ]


# ---------------------------------------------------------------- 回执
def next_receipt_id() -> str:
    return f"RCPT-{store.next_id(RECEIPT_TABLE):04d}"


def store_receipt(receipt: dict[str, Any]) -> dict[str, Any]:
    entry = copy.deepcopy(receipt)
    entry["id"] = store.next_id(RECEIPT_TABLE)
    entry["回执编号"] = str(receipt.get("回执编号") or f"RCPT-{entry['id']:04d}")
    store.rows(RECEIPT_TABLE).append(entry)
    os.makedirs(DATA_DIR, exist_ok=True)
    filename = f"reclaim_receipt_{entry['回执编号']}_{_slug(str(entry.get('任务编号', '')))}.json"
    with open(os.path.join(DATA_DIR, filename), "w", encoding="utf-8") as handle:
        json.dump(entry, handle, ensure_ascii=False, indent=2, default=str)
    return copy.deepcopy(entry)


def list_receipts(application_id: str | None = None) -> list[dict[str, Any]]:
    rows = store.rows(RECEIPT_TABLE)
    if application_id is None:
        return [copy.deepcopy(row) for row in rows]
    return [
        copy.deepcopy(row) for row in rows
        if str(row.get("申请编号")) == str(application_id)
    ]
