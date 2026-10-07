"""药店健康陪伴边界台的本地命令入口。

子命令：
  health                 健康检查
  validate <json>        兼容旧契约：登记一条基础记录
  demo                   在内存库中演示边界台完整故事（授权→计划→提醒→
                         反馈→风险暂停→交接→撤回→三视图）
"""
import json
import sys
from pathlib import Path

from . import domain as d
from .service import Actor, Service
from .store import Store

NOW = "2026-10-07T09:00:00+00:00"
ALL_SCOPES = ["med_reminder", "adherence", "med_plan", "supply",
              "risk_review", "referral", "handoff"]


def _demo() -> dict:
    service = Service(Store(clock=lambda: NOW), clock=lambda: NOW)
    comp = Actor("comp-1", d.ROLE_COMPLIANCE)
    pharma = Actor("u-001", d.ROLE_PHARMACIST, pharmacist_id="ph-1")
    pharmb = Actor("u-002", d.ROLE_PHARMACIST, pharmacist_id="ph-2")
    customer = Actor("cust-001", d.ROLE_CUSTOMER)

    service.grant_consent(comp, {"customer_id": "cust-001", "scopes": ALL_SCOPES,
                                 "channels": ["sms", "app"]})
    service.register_pharmacist(comp, {
        "pharmacist_id": "ph-1", "store_id": "store-A", "license_no": "LIC-A-001",
        "scopes": ALL_SCOPES, "valid_from": "2026-01-01T00:00:00+00:00",
        "valid_to": "2027-01-01T00:00:00+00:00"})
    service.register_pharmacist(comp, {
        "pharmacist_id": "ph-2", "store_id": "store-B", "license_no": "LIC-B-002",
        "scopes": ["med_reminder", "adherence", "supply", "handoff"],
        "valid_from": "2026-01-01T00:00:00+00:00",
        "valid_to": "2027-01-01T00:00:00+00:00"})
    plan = service.create_plan(pharma, {
        "plan_id": "plan-001", "customer_id": "cust-001", "store_id": "store-A",
        "medications": ["二甲双胍"]})
    service.schedule_reminder(pharma, {
        "reminder_id": "rem-001", "plan_id": "plan-001", "medication": "二甲双胍",
        "scheduled_at": "2026-10-07T08:00:00+00:00", "channel": "sms"})
    dispatched = service.run_due()
    receipt = service.record_feedback(customer, {
        "plan_id": "plan-001", "content_code": "taken", "detail": "顾客自述已服药",
        "idempotency_key": "fb-001"})
    risk = service.record_feedback(customer, {
        "plan_id": "plan-001", "content_code": "side_effect",
        "detail": "顾客自述服药后皮疹", "idempotency_key": "fb-002"})
    service.close_signal(pharma, risk["signal_id"], "已建议顾客联系原处方医师")
    service.initiate_handoff(pharma, {
        "handoff_id": "ho-001", "plan_id": "plan-001", "to_store_id": "store-B",
        "to_pharmacist_id": "ph-2", "effective_at": "2026-10-08T00:00:00+00:00",
        "note": "顾客迁居，转 B 店承接"})
    handoff = service.complete_handoff(pharmb, "ho-001")
    customer_view = service.customer_view(customer, "cust-001")

    return {
        "plan_source": plan["plan_source"],
        "dispatch": dispatched,
        "receipt_no": receipt["receipt_no"],
        "risk_action": risk["auto_action"],
        "referral": {"id": risk["referral_id"], "target": risk["target"]},
        "handoff": handoff,
        "customer_sees": {
            "plans": len(customer_view["plans"]),
            "reminders": len(customer_view["reminders"]),
            "referrals": len(customer_view["referrals"]),
            "medical_boundary": customer_view["medical_boundary"],
        },
        "audit_chain_valid": service.store.verify_audit_chain(),
    }


def main() -> int:
    if len(sys.argv) >= 2 and sys.argv[1] == "health":
        print(json.dumps(Service().health(), ensure_ascii=False, sort_keys=True))
        return 0
    if len(sys.argv) == 3 and sys.argv[1] == "validate":
        payload = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
        print(json.dumps(Service().register(payload), ensure_ascii=False, sort_keys=True))
        return 0
    if len(sys.argv) == 2 and sys.argv[1] == "demo":
        print(json.dumps(_demo(), ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    print(__doc__)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
