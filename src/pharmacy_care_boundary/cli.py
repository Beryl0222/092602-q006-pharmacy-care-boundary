"""药店健康陪伴边界台的本地命令入口。

* ``validate <file>``  ：兼容既有契约，登记一条基础记录。
* ``scenario``         ：在内存中跑完端到端事故场景，输出每一步的边界决策。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from . import domain as d
from .domain import Actor, Credential
from .service import Service
from .store import Store

T0 = "2026-10-01T08:00:00+00:00"
T1 = "2026-10-02T08:00:00+00:00"
T2 = "2026-10-03T08:00:00+00:00"
T3 = "2026-10-04T08:00:00+00:00"
T4 = "2026-10-05T08:00:00+00:00"
VALID_FROM = "2026-01-01T00:00:00+00:00"
VALID_TO = "2026-12-31T00:00:00+00:00"

STORE_A = "store-A"
STORE_B = "store-B"


def _dump(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


def build_scenario() -> tuple[Service, list[dict[str, object]]]:
    service = Service(Store())
    trace: list[dict[str, object]] = []

    compliance = Actor("compliance-1", d.ROLE_COMPLIANCE)
    pharm_a = Actor("pharm-A", d.ROLE_PHARMACIST, "pharm-A", STORE_A)
    pharm_b = Actor("pharm-B", d.ROLE_PHARMACIST, "pharm-B", STORE_B)
    sales = Actor("sales-1", d.ROLE_SALES)
    customer = Actor("cust-1", d.ROLE_CUSTOMER)

    def step(title: str, result: object) -> None:
        trace.append({"step": title, "result": result})

    # 0) 合规登记两家门店药师的执业资质
    service.register_credential(
        compliance, Credential("pharm-A", STORE_A, tuple(d.MATTERS), VALID_FROM, VALID_TO)
    )
    service.register_credential(
        compliance, Credential("pharm-B", STORE_B, tuple(d.MATTERS), VALID_FROM, VALID_TO)
    )

    # 1) 顾客在 A 店授予覆盖全部事项的授权；A 店药师凭处方导入药品计划
    service.grant_consent(compliance, "cust-1", list(d.MATTERS), STORE_A, at=T0)
    plan = service.import_plan(pharm_a, "cust-1", "prescription", "RX-2026-0777", at=T0)
    step("导入处方药品计划", plan)

    # 2) A、B 两店各自排期到期提醒
    service.schedule_task(
        pharm_a, "cust-1", plan["plan_id"], d.MATTER_MEDICATION_REMINDER, T1,
        STORE_A, task_id="reminder-A",
    )
    service.schedule_task(
        pharm_b, "cust-1", plan["plan_id"], d.MATTER_MEDICATION_REMINDER, T2,
        STORE_B, task_id="reminder-B",
    )

    # 3) 第 2 天 A 店提醒正常发出（资质 × 授权双覆盖）
    step("第2天到期处理（撤回前）", service.run_due(compliance, T1))

    # 4) 顾客撤回授权 —— 原始事故点：撤回必须在全连锁即时生效
    step("顾客撤回授权", service.withdraw_consent(customer, "cust-1", note="不再接受随访", at=T1))

    # 5) 第 3 天 B 店的提醒必须被拦截，并留下未提醒原因
    step("第3天到期处理（撤回后，B店不得发送）", service.run_due(compliance, T2))
    step("B店提醒最终状态",
         service.store.get_task("reminder-B").__dict__)

    # 6) 撤回后法规要求保留的最小事实仅合规可见
    step("合规视角（最小事实）", service.compliance_view(compliance, "cust-1"))
    step("审计链校验", service.store.verify_chain())

    # ---- 第二位顾客：风险阈值、销售无权关闭、门店交接续跑 ----
    c2 = "cust-2"
    customer2 = Actor(c2, d.ROLE_CUSTOMER)
    service.grant_consent(compliance, c2, list(d.MATTERS), STORE_A, at=T0)
    plan2 = service.import_plan(pharm_a, c2, "doctor_order", "ORD-2026-0313", at=T0)
    service.schedule_task(
        pharm_a, c2, plan2["plan_id"], d.MATTER_MEDICATION_REMINDER, T3,
        STORE_A, task_id="reminder-A2",
    )
    batch = service.create_batch(pharm_a, c2, plan2["plan_id"], STORE_A, at=T1)

    # 依从反馈命中风险阈值：自动暂停 + 建议转诊 + 转人工
    triage = service.record_feedback(
        pharm_a, c2, plan2["plan_id"], "服药后出现皮疹（顾客自述）", "fb-c2-1",
        signals=["adverse_reaction"], at=T1,
    )
    step("依从反馈命中风险阈值", triage)

    # 销售目标负责人尝试关闭异常 —— 必须被拒绝
    denied: object
    try:
        service.close_anomaly(sales, triage["anomaly_id"], "季度冲量，先关掉")
        denied = {"allowed": True}
    except Exception as exc:  # noqa: BLE001 - 场景需要展示拒绝原因
        denied = {"allowed": False, "reason": str(exc)}
    step("销售尝试关闭异常", denied)

    # 人工接管后合规关闭，转诊推进
    service.human_takeover(pharm_a, triage["anomaly_id"], at=T2)
    service.close_anomaly(compliance, triage["anomaly_id"], "已协助转诊就医", at=T2)
    service.update_referral(pharm_a, triage["referral_id"], d.REFERRAL_IN_PROGRESS, at=T2)
    service.resume_tasks(compliance, c2, at=T2)

    # A 店发起交接，B 店承接：到期任务与保供责任继续
    handover = service.open_handover(pharm_a, c2, STORE_B, at=T2)
    step("A店发起到B店交接", handover)
    step("B店承接交接（任务续跑、保供责任转移）",
         service.accept_handover(pharm_b, handover["handover_id"], at=T3))
    step("第4天到期处理（由B店双覆盖药师发送）", service.run_due(compliance, T4))
    step("保供批次当前责任", service.store.get_batch(batch["batch_id"]).__dict__)

    # ---- 第三位顾客：同一标识出现不同健康内容，先隔离再隐私复核 ----
    c3 = "cust-3"
    service.grant_consent(compliance, c3, list(d.MATTERS), STORE_A, at=T0)
    plan3 = service.import_plan(pharm_a, c3, "prescription", "RX-2026-0888", at=T0)
    service.schedule_task(
        pharm_a, c3, plan3["plan_id"], d.MATTER_MEDICATION_REMINDER, T3,
        STORE_A, task_id="reminder-A3",
    )
    service.ingest_health_content(pharm_a, c3, "高血压随访记录", STORE_A, at=T0)
    quarantined = service.ingest_health_content(
        pharm_b, c3, "糖尿病随访记录（同一标识、不同内容）", STORE_B, at=T1
    )
    step("同一标识出现不同健康内容→隔离", quarantined)
    step("合规隐私复核（以首次内容为准并解除隔离）",
         service.review_quarantine(compliance, quarantined["case_id"], d.REVIEW_FIRST_WINS, at=T2))

    step("顾客2视角（计划来源/转诊进度/保供责任）", service.customer_view(customer2, c2))
    step("全链审计校验", service.store.verify_chain())
    return service, trace


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "validate":
        payload = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
        print(_dump(Service().register(payload)))
        return 0
    if len(sys.argv) == 2 and sys.argv[1] == "scenario":
        _, trace = build_scenario()
        print(_dump({"service": "pharmacy_care_boundary", "scenario": trace}))
        return 0
    print(_dump(Service().health()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
