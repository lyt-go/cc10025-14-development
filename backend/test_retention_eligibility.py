"""保留期回收资格场景测试：三分区申请、豁免冲突、重试不重复回收。

场景（全部使用固定时钟，日期 2026-11-22）：

    10:00  提交一份覆盖三个分区的回收申请
           A、B 两个分区数据截止 2026-10-23 10:00，保留 30 天 → 10 点已到期
           C 分区数据截止 2026-10-23 11:00，保留 30 天 → 11 点才到期
    10:30  对 A 分区新增豁免，随后发起回收
           A 被豁免、C 未到期，只有 B 应被释放
    之后   解除豁免，重新打开任务重试
           A 与 C 到期，B 已清理不重复回收

额外覆盖：规则事后修改不改写已落库的资格证据；不同资产 / 不同适用时段的规则不混用；
豁免生效前的评估不受影响；整份申请待复核时停止自动释放。
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(__file__))

from app import clock
from app.services import retention_engine as engine
from app.services import service_for

RED = "\033[91m"
GREEN = "\033[92m"
RESET = "\033[0m"

passed = 0
failed = 0

ASSET = "ASSE-RECLAIM"
OTHER_ASSET = "ASSE-OTHER"
RULE = "RETE-RC01"
DAY = datetime(2026, 10, 23, tzinfo=timezone.utc)


def check(desc, condition, detail=""):
    global passed, failed
    if condition:
        print(f"  {GREEN}PASS{RESET} {desc}")
        passed += 1
        return True
    print(f"  {RED}FAIL{RESET} {desc} {detail}")
    failed += 1
    return False


def at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 11, 22, hour, minute, tzinfo=timezone.utc)


def make_rule(retention_days: str = "30", asset: str = ASSET,
              rule_id: str = RULE, version: str = "v1",
              period_start: str = "", period_end: str = "") -> dict:
    rows = service_for("retention_rule")
    entry, missing = rows.create_entry({
        "规则编号": rule_id,
        "目标资产": asset,
        "保留天数": retention_days,
        "归档路径": f"archive://{asset}",
        "压缩策略": "ZSTD",
        "豁免条件": "",
        "执行频率": "daily",
        "规则版本": version,
        "适用开始": period_start,
        "适用结束": period_end,
    })
    assert entry is not None, missing
    return entry


def make_partitions(asset: str = ASSET, tag: str = "") -> tuple[dict, dict, dict]:
    base = {
        "asset_id": asset,
        "retention_days": 30,
        "declared_expiry": "2026-11-22T10:00:00+00:00",
    }
    part_a = engine.register_partition(
        partition_key=f"dt=2026-10-23/hour=10a{tag}",
        data_until="2026-10-23T10:00:00+00:00",
        period_start="2026-10-23T10:00:00+00:00",
        period_end="2026-10-23T10:00:00+00:00",
        **base,
    )
    part_b = engine.register_partition(
        partition_key=f"dt=2026-10-23/hour=10b{tag}",
        data_until="2026-10-23T10:00:00+00:00",
        period_start="2026-10-23T10:00:00+00:00",
        period_end="2026-10-23T10:00:00+00:00",
        **base,
    )
    part_c = engine.register_partition(
        partition_key=f"dt=2026-10-23/hour=11{tag}",
        data_until="2026-10-23T11:00:00+00:00",
        period_start="2026-10-23T11:00:00+00:00",
        period_end="2026-10-23T11:00:00+00:00",
        retention_days=30,
        asset_id=asset,
        declared_expiry="2026-11-22T11:00:00+00:00",
    )
    return part_a, part_b, part_c


def by_partition(rows, partition_id):
    for row in rows:
        if str(row.get("分区编号")) == partition_id:
            return row
    return {}


def test_eligibility_basis():
    """到期与豁免校验：同一资产同一适用时段，按规则阈值计算。"""
    print("\n=== 资格明细：到期阈值与豁免判定 ===")
    svc = service_for("reclaim_job")
    rule = make_rule()
    part_a, part_b, part_c = make_partitions(tag="-basis")

    with clock.use_clock(clock.fixed_clock(at(10, 0))):
        # 规则还没生效时，评估不应拿到可用阈值。
        before = engine.evaluate_partition(part_a, at(9, 59), "申请提交")
        check("规则未生效时分区不可回收",
              before["资格状态"] == engine.NO_RULE, before["资格状态"])

        service_for("retention_rule").activate_rule(rule["id"])
        rows, batch = svc.evaluate(
            _seed_application(svc, [part_a, part_b, part_c]), purpose="申请提交",
            as_of=at(10, 0))
        a = by_partition(rows, part_a["分区编号"])
        c = by_partition(rows, part_c["分区编号"])
        check("A 分区 10 点已到期 → 可回收",
              a.get("资格状态") == engine.ELIGIBLE, a.get("阻断原因"))
        check("A 分区到期时间按 30 天阈值算出 11-22 10:00",
              a.get("到期时间") == "2026-11-22T10:00:00+00:00", a.get("到期时间"))
        check("C 分区 11 点才到期 → 未到期",
              c.get("资格状态") == engine.NOT_DUE, c.get("资格状态"))
        check("资格证据冻结了规则版本",
              (a.get("证据快照") or {}).get("rule", {}).get("规则版本") == "v1")
        check("资格明细只落库一次批次",
              all(row.get("评估批次") == batch for row in rows))

        # 换了资产但分区键相同的规则，不应被误用。
        other_rule = make_rule(retention_days="1", asset=OTHER_ASSET,
                               rule_id="RETE-OTHER", version="v1")
        service_for("retention_rule").activate_rule(other_rule["id"])
        still = engine.evaluate_partition(part_a, at(10, 0), "执行")
        check("其他资产的规则不参与本分区解析",
              still.get("保留天数") == 30, still.get("保留天数"))

        # 结束后停用两条规则，避免影响后续用例。
        service_for("retention_rule").run_action(rule["id"], "停用规则")
        service_for("retention_rule").run_action(other_rule["id"], "停用规则")


def _seed_application(svc, partitions) -> int:
    entry, missing = svc.create_entry({
        "目标资产": ASSET,
        "分区清单": ",".join(str(p["分区编号"]) for p in partitions),
        "回收范围": "超出30天的历史分区",
        "回收方式": "逻辑删除",
    })
    assert entry is not None, missing
    return entry["id"]


def test_exemption_and_receipt():
    """10 点提交、10 点半新增豁免、发起回收；核查豁免分区未被清理。"""
    print("\n=== 豁免冲突：10 点提交，10 点半豁免后执行 ===")
    svc = service_for("reclaim_job")
    rule = make_rule()
    partitions = make_partitions(tag="-exempt")
    part_a = partitions[0]

    with clock.use_clock(clock.fixed_clock(at(10, 0))):
        service_for("retention_rule").activate_rule(rule["id"])
        entry_id = _seed_application(svc, partitions)
        submitted = svc.eligibility(entry_id)
        submitted_a = by_partition(submitted, part_a["分区编号"])
        check("提交时 A 分区即可回收", submitted_a.get("是否可回收") is True)

        exemption = engine.approve_exemption(
            asset_id=ASSET,
            partition_ids=[part_a["分区编号"]],
            effective_at=at(10, 30).isoformat(),
            reason="法务调查保留",
            operator="值班人",
        )
        check("豁免生效时间记录为 10:30",
              exemption["生效时间"] == at(10, 30).isoformat())

    with clock.use_clock(clock.fixed_clock(at(10, 30))):
        entry, message = svc.run_action(entry_id, "确认执行")
        check("执行动作成功", entry is not None, message)

        execution_rows = svc.eligibility(entry_id)
        # 提交批次的证据不能被后面的豁免改写。
        old_a = [
            row for row in execution_rows
            if row["分区编号"] == part_a["分区编号"] and row["评估用途"] == "申请提交"
        ][0]
        check("已落库的提交证据未被豁免改写",
              old_a["是否可回收"] is True and old_a["证据快照"]["exemption"] is None)

        exec_rows = [
            row for row in execution_rows
            if row["评估用途"] == "执行"
            and row["评估批次"] == entry["资格批次"]
        ]
        a = by_partition(exec_rows, part_a["分区编号"])
        b = by_partition(exec_rows, partitions[1]["分区编号"])
        c = by_partition(exec_rows, partitions[2]["分区编号"])
        check("执行时 A 分区为豁免保护",
              a.get("资格状态") == engine.EXEMPTED, a.get("资格状态"))
        check("执行时 C 分区仍未到期",
              c.get("资格状态") == engine.NOT_DUE, c.get("资格状态"))
        check("执行时 B 分区可回收",
              b.get("资格状态") == engine.ELIGIBLE, b.get("资格状态"))

        check("豁免内数据未被清理",
              engine.find_cleaned(ASSET, part_a["分区编号"]) is None)
        check("未到期分区未被清理",
              engine.find_cleaned(ASSET, partitions[2]["分区编号"]) is None)
        released = engine.find_cleaned(ASSET, partitions[1]["分区编号"])
        check("独立到期的 B 分区本次真的释放", released is not None)

        receipts = svc.receipts(entry_id)
        check("执行落库了一条回执", len(receipts) == 1, f"{len(receipts)} 条")
        receipt = receipts[0]
        check("回执释放范围只有 B",
              receipt["释放范围"] == [partitions[1]["分区编号"]], receipt["释放范围"])
        check("回执记录了豁免未处置范围",
              receipt["未处置范围"]["豁免保护"] == [part_a["分区编号"]])
        check("回执记录了未到期未处置范围",
              receipt["未处置范围"]["未到期"] == [partitions[2]["分区编号"]])
        check("回执采用了执行时刻的规则版本",
              receipt["规则版本清单"] == ["v1"], receipt["规则版本清单"])
        check("回执引用了本批次资格编号",
              len(receipt["资格编号"]) == 3 and all(receipt["资格编号"]))
        check("申请上回写了回执编号",
              entry.get("回执编号") == receipt["回执编号"])
        check("部分释放时回执状态为部分释放",
              receipt["回执状态"] == "部分释放", receipt["回执状态"])
        check("分区台账标记 B 已释放",
              engine.find_partition(partitions[1]["分区编号"])["状态"] == "已释放")

    with clock.use_clock(clock.fixed_clock(at(11, 30))):
        # 已完成/执行中时直接重试会被拒绝，必须先重开。
        blocked, blocked_message = svc.run_action(entry_id, "确认执行")
        check("未重新打开时拒绝重试", blocked is None, blocked_message)

        svc.run_action(entry_id, "标记完成")
        reopened, reopen_message = svc.run_action(entry_id, "重新打开")
        check("任务可重新打开", reopened is not None, reopen_message)

    # ---------------------------------------------------------------- 解除豁免后重试
    print("\n=== 解除豁免后重新打开重试 ===")
    with clock.use_clock(clock.fixed_clock(at(11, 30))):
        engine.lift_exemption(exemption["id"], as_of=at(11, 30))
        check("分区 A 的豁免标记已撤销",
              engine.find_partition(part_a["分区编号"])["豁免标记"] == "否")

        entry, message = svc.run_action(entry_id, "确认执行")
        check("重开后可再次执行", entry is not None, message)
        receipts = svc.receipts(entry_id)
        check("产生第二条回执", len(receipts) == 2, f"{len(receipts)} 条")
        latest = receipts[-1]
        previous = receipts[0]

        check("新回执采用执行时刻重新解析的规则",
              latest["规则版本清单"] == ["v1"], latest["规则版本清单"])
        check("新回执释放了 A 与 C",
              set(latest["释放范围"]) == {part_a["分区编号"], partitions[2]["分区编号"]},
              latest["释放范围"])
        check("新回执把已完成清理的 B 记为跳过",
              latest["未处置范围"]["已清理跳过"] == [partitions[1]["分区编号"]],
              latest["未处置范围"])
        check("先前回执未被改写",
              previous["释放范围"] == [partitions[1]["分区编号"]])
        check("B 分区释放记录仍指向第一条回执",
              engine.find_cleaned(ASSET, partitions[1]["分区编号"])["回执编号"]
              == previous["回执编号"])
        check("总释放台账每个分区只有一条",
              len([
                  row for row in engine.store.rows(engine.CLEANED_TABLE)
                  if str(row.get("资产编号")) == ASSET
              ]) == 3)

    # 再执行一次，不应重复回收任何分区。
    with clock.use_clock(clock.fixed_clock(at(12, 0))):
        svc.run_action(entry_id, "标记完成")
        svc.run_action(entry_id, "重新打开")
        entry, message = svc.run_action(entry_id, "确认执行")
        check("全部清理后再次执行不释放任何分区",
              entry is not None and entry.get("影响数据量") == "0 个分区", message)
        latest = svc.receipts(entry_id)[-1]
        check("重复执行回执的释放范围为空", latest["释放范围"] == [], latest["释放范围"])
        check("全部已清理分区进入跳过清单",
              set(latest["未处置范围"]["已清理跳过"]) ==
              {part_a["分区编号"], partitions[1]["分区编号"], partitions[2]["分区编号"]},
              latest["未处置范围"]["已清理跳过"])

    service_for("retention_rule").run_action(rule["id"], "停用规则")


def test_application_level_review():
    """整份申请待复核：申请范围里有无法核对的分区时，不部分释放。"""
    print("\n=== 整份申请待复核 ===")
    svc = service_for("reclaim_job")
    asset = "ASSE-APPREVIEW"
    rule = make_rule(retention_days="30", asset=asset, rule_id="RETE-APPREV")
    due = engine.register_partition(
        asset_id=asset,
        partition_key="dt=2026-10-23/hour=15",
        data_until="2026-10-23T15:00:00+00:00",
        retention_days=30,
        declared_expiry="2026-11-22T10:00:00+00:00",
    )

    with clock.use_clock(clock.fixed_clock(at(15, 0))):
        service_for("retention_rule").activate_rule(rule["id"])
        entry, missing = svc.create_entry({
            "目标资产": asset,
            # 第二个分区没有登记在台账，申请范围无法核对。
            "分区清单": f"{due['分区编号']},PART-NOT-REGISTERED",
        })
        assert entry is not None, missing
        check("未登记分区被标为申请级待复核",
              any(row.get("复核层级") == "申请" for row in entry["资格明细"]))
        entry, message = svc.run_action(entry["id"], "确认执行")
        check("整份申请待复核时不释放任何分区",
              entry is not None and entry.get("影响数据量") == "0 个分区", message)
        check("到期分区本次未被清理",
              engine.find_cleaned(asset, due["分区编号"]) is None)
        receipt = svc.receipts(entry["id"])[-1]
        check("回执状态为整份申请待复核",
              receipt["回执状态"] == "整份申请待复核", receipt["回执状态"])
        check("回执未处置范围包含无法核对的分区",
              receipt["未处置范围"]["待复核"] == [""],
              receipt["未处置范围"]["待复核"])


def test_evidence_immutable_after_rule_change():
    """规则事后被修改，已执行的资格证据不被改写。"""
    print("\n=== 资格证据不随规则修改而改写 ===")
    svc = service_for("reclaim_job")
    asset = "ASSE-IMMUT"
    rule = make_rule(retention_days="30", asset=asset, rule_id="RETE-IMMUT")
    partition = engine.register_partition(
        asset_id=asset,
        partition_key="dt=2026-10-23/hour=12",
        data_until="2026-10-23T12:00:00+00:00",
        retention_days=30,
        declared_expiry="2026-11-22T12:00:00+00:00",
    )

    with clock.use_clock(clock.fixed_clock(at(12, 0))):
        service_for("retention_rule").activate_rule(rule["id"])
        entry, missing = svc.create_entry({
            "目标资产": asset,
            "分区清单": partition["分区编号"],
        })
        assert entry is not None, missing
        snapshot_before = svc.eligibility(entry["id"])[0]
        check("提交时按 30 天阈值可回收",
              snapshot_before["是否可回收"] is True, snapshot_before["阻断原因"])
        frozen_evidence = snapshot_before["证据快照"]

    # 事后把保留天数改成 60 天，并以新版本生效。
    with clock.use_clock(clock.fixed_clock(at(12, 30))):
        rule["保留天数"] = "60"
        rule["规则版本"] = "v2"
        service_for("retention_rule").activate_rule(rule["id"])

        stored = svc.eligibility(entry["id"])
        old = [row for row in stored if row["评估批次"] == snapshot_before["评估批次"]][0]
        check("旧证据保留 30 天阈值",
              old["证据快照"]["threshold"]["保留天数"] == 30,
              old["证据快照"]["threshold"]["保留天数"])
        check("旧证据的规则版本仍为 v1",
              old["证据快照"]["rule"]["规则版本"] == "v1")
        check("旧证据整体对象未被原地修改",
              old["证据快照"] == frozen_evidence)

    with clock.use_clock(clock.fixed_clock(at(12, 30))):
        rows, batch = svc.evaluate(entry["id"], purpose="执行", as_of=at(12, 30))
        current = rows[0]
        check("新评估按最新版本 v2 的 60 天阈值",
              current["规则版本"] == "v2" and current["保留天数"] == 60,
              f"{current['规则版本']}/{current['保留天数']}")
        check("按 60 天重算后分区不再到期",
              current["资格状态"] == engine.NOT_DUE, current["资格状态"])


def test_review_blocks_release():
    """整份申请待复核时，不自动释放任何分区。"""
    print("\n=== 待复核阻断：声明到期时间与规则阈值不一致 ===")
    svc = service_for("reclaim_job")
    asset = "ASSE-REVIEW"
    rule = make_rule(retention_days="30", asset=asset, rule_id="RETE-REVIEW")
    partition = engine.register_partition(
        asset_id=asset,
        partition_key="dt=2026-10-23/hour=13",
        data_until="2026-10-23T13:00:00+00:00",
        retention_days=30,
        # 声明到期时间比阈值算出来的晚一天，属于范围冲突。
        declared_expiry="2026-11-23T13:00:00+00:00",
    )

    with clock.use_clock(clock.fixed_clock(at(13, 0))):
        service_for("retention_rule").activate_rule(rule["id"])
        entry, missing = svc.create_entry({
            "目标资产": asset,
            "分区清单": partition["分区编号"],
        })
        assert entry is not None, missing
        entry, message = svc.run_action(entry["id"], "确认执行")
        check("申请进入待复核", entry is not None and entry.get("abnormal") is True, message)
        receipt = svc.receipts(entry["id"])[-1]
        check("回执状态为待复核", receipt["回执状态"] == "待复核", receipt["回执状态"])
        check("待复核分区未释放", engine.find_cleaned(asset, partition["分区编号"]) is None)
        check("待复核分区进入未处置范围",
              receipt["未处置范围"]["待复核"] == [partition["分区编号"]])


def test_submission_snapshot_and_execution_rules():
    """同一申请在提交与执行两个时刻各自留证，执行以执行时刻规则为准。"""
    print("\n=== 提交与执行分别取证 ===")
    svc = service_for("reclaim_job")
    asset = "ASSE-TWOPHASE"
    rule = make_rule(retention_days="30", asset=asset, rule_id="RETE-TWO")
    partition = engine.register_partition(
        asset_id=asset,
        partition_key="dt=2026-10-23/hour=14",
        data_until="2026-10-23T14:00:00+00:00",
        retention_days=30,
        declared_expiry="2026-11-22T14:00:00+00:00",
    )

    with clock.use_clock(clock.fixed_clock(at(13, 0))):
        service_for("retention_rule").activate_rule(rule["id"])
        entry, missing = svc.create_entry({
            "目标资产": asset,
            "分区清单": partition["分区编号"],
        })
        assert entry is not None, missing
        submitted = svc.eligibility(entry["id"])[0]
        check("提交时刻未到期 -> 未到期",
              submitted["资格状态"] == engine.NOT_DUE, submitted["资格状态"])
        check("提交时资格批次记录在申请上",
              entry["资格批次"] == submitted["评估批次"])

    with clock.use_clock(clock.fixed_clock(at(14, 0))):
        entry, message = svc.run_action(entry["id"], "确认执行")
        check("执行时刻已到期 -> 释放", entry is not None and
              engine.find_cleaned(asset, partition["分区编号"]) is not None, message)
        batches = [row["评估批次"] for row in svc.eligibility(entry["id"])]
        check("同一申请有两个评估批次",
              len(set(batches)) == 2, batches)


if __name__ == "__main__":
    print("=" * 60)
    print("回收资格明细 / 豁免 / 回执 场景测试")
    print("=" * 60)
    test_eligibility_basis()
    test_exemption_and_receipt()
    test_application_level_review()
    test_evidence_immutable_after_rule_change()
    test_review_blocks_release()
    test_submission_snapshot_and_execution_rules()
    print(f"\n{'=' * 60}")
    print(f"结果: {GREEN}{passed} 通过{RESET} / {RED}{failed} 失败{RESET} / "
          f"{passed + failed} 总计")
    print("=" * 60)
    if failed:
        sys.exit(1)
