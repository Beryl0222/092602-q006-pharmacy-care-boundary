"""健康陪伴边界台的需求级测试。

约定：固定时钟 NOW=2026-10-02T09:00:00Z；标准世界里合规 comp、
A 店药师 ph-a（全事项资质）、顾客 c1，授权 v1 覆盖全部事项与短信渠道。
"""
import os
import tempfile
import unittest

from pharmacy_care_boundary import domain as d
from pharmacy_care_boundary import policy
from pharmacy_care_boundary.policy import DiagnosisProhibited
from pharmacy_care_boundary.service import Actor, BoundaryError, Service
from pharmacy_care_boundary.store import Store, UpdateBlocked

NOW = "2026-10-02T09:00:00+00:00"
ALL_SCOPES = ["med_reminder", "adherence", "med_plan", "supply",
              "risk_review", "referral", "handoff"]


def make_service(path: str = ":memory:") -> Service:
    return Service(Store(path, clock=lambda: NOW), clock=lambda: NOW)


def bootstrap(service: Service) -> dict:
    comp = Actor("comp-1", d.ROLE_COMPLIANCE)
    service.grant_consent(comp, {"customer_id": "c1", "scopes": ALL_SCOPES,
                                 "channels": ["sms", "app"]})
    service.register_pharmacist(comp, {
        "pharmacist_id": "ph-a", "store_id": "store-A", "license_no": "LA",
        "scopes": ALL_SCOPES, "valid_from": "2026-01-01T00:00:00+00:00",
        "valid_to": "2027-01-01T00:00:00+00:00"})
    service.register_pharmacist(comp, {
        "pharmacist_id": "ph-b", "store_id": "store-B", "license_no": "LB",
        "scopes": ["med_reminder", "adherence", "supply", "handoff"],
        "valid_from": "2026-01-01T00:00:00+00:00",
        "valid_to": "2027-01-01T00:00:00+00:00"})
    return {
        "comp": comp,
        "pharma": Actor("u-a", d.ROLE_PHARMACIST, pharmacist_id="ph-a"),
        "pharmb": Actor("u-b", d.ROLE_PHARMACIST, pharmacist_id="ph-b"),
        "sales": Actor("sales-1", d.ROLE_SALES),
        "customer": Actor("c1", d.ROLE_CUSTOMER),
    }


def make_plan(service: Service, pharma: Actor, plan_id: str = "plan-1",
              customer: str = "c1", store: str = "store-A") -> dict:
    return service.create_plan(pharma, {
        "plan_id": plan_id, "customer_id": customer, "store_id": store,
        "medications": ["二甲双胍"]})


class 双重覆盖测试(unittest.TestCase):
    def setUp(self):
        self.s = make_service()
        self.w = bootstrap(self.s)

    def test_资质与授权都覆盖才能处理(self):
        make_plan(self.s, self.w["pharma"])
        out = self.s.schedule_reminder(self.w["pharma"], {
            "reminder_id": "r1", "plan_id": "plan-1", "medication": "二甲双胍",
            "scheduled_at": "2026-10-03T08:00:00+00:00", "channel": "sms"})
        self.assertEqual(out["reminder_status"], d.REMINDER_PENDING)

    def test_授权未覆盖事项被拒(self):
        # 新顾客只授权提醒，未授权建计划
        self.s.grant_consent(self.w["comp"], {"customer_id": "c2",
                                              "scopes": ["med_reminder"], "channels": ["sms"]})
        with self.assertRaises(BoundaryError) as ctx:
            make_plan(self.s, self.w["pharma"], "p2", customer="c2")
        self.assertEqual(policy.SKIP_CONSENT_SCOPE, str(ctx.exception).split("：")[-1])

    def test_资质未覆盖事项被拒(self):
        self.s.register_pharmacist(self.w["comp"], {
            "pharmacist_id": "ph-limited", "store_id": "store-A", "license_no": "LL",
            "scopes": ["med_reminder"], "valid_from": "2026-01-01T00:00:00+00:00",
            "valid_to": "2027-01-01T00:00:00+00:00"})
        with self.assertRaises(BoundaryError) as ctx:
            make_plan(self.s, Actor("u-l", d.ROLE_PHARMACIST, pharmacist_id="ph-limited"),
                      "p3")
        self.assertEqual(policy.SKIP_CREDENTIAL_SCOPE, str(ctx.exception).split("：")[-1])

    def test_资质过期被拒(self):
        self.s.register_pharmacist(self.w["comp"], {
            "pharmacist_id": "ph-exp", "store_id": "store-A", "license_no": "LE",
            "scopes": ALL_SCOPES, "valid_from": "2020-01-01T00:00:00+00:00",
            "valid_to": "2021-01-01T00:00:00+00:00"})
        with self.assertRaises(BoundaryError) as ctx:
            make_plan(self.s, Actor("u-e", d.ROLE_PHARMACIST, pharmacist_id="ph-exp"), "p4")
        self.assertEqual(policy.SKIP_CREDENTIAL_EXPIRED, str(ctx.exception).split("：")[-1])

    def test_渠道未被授权覆盖被拒(self):
        make_plan(self.s, self.w["pharma"])
        with self.assertRaises(BoundaryError) as ctx:
            self.s.schedule_reminder(self.w["pharma"], {
                "reminder_id": "r2", "plan_id": "plan-1", "medication": "二甲双胍",
                "scheduled_at": "2026-10-03T08:00:00+00:00", "channel": "call"})
        self.assertEqual(policy.SKIP_CONSENT_CHANNEL, str(ctx.exception).split("：")[-1])


class 风险与异常测试(unittest.TestCase):
    def setUp(self):
        self.s = make_service()
        self.w = bootstrap(self.s)
        make_plan(self.s, self.w["pharma"])

    def _feedback(self, code, key, detail=""):
        return self.s.record_feedback(self.w["customer"], {
            "plan_id": "plan-1", "content_code": code, "detail": detail,
            "idempotency_key": key})

    def test_达阈值自动建议暂停并转人工(self):
        out = self._feedback("side_effect", "k1", "皮疹")
        self.assertEqual(out["auto_action"], "suggest_pause_and_manual_handoff")
        self.assertEqual(out["signal_status"], d.SIGNAL_PAUSED)
        self.assertTrue(out["referral_id"])
        self.assertEqual(out["referral_status"], d.REFERRAL_SUGGESTED)
        # 计划被暂停、未发送提醒被取消
        self.assertEqual(self.s.store.get_plan("plan-1").status, d.PLAN_PAUSED)

    def test_未达阈值只转人工不暂停(self):
        # 连续漏服 <3 次不升级
        self.assertIsNone(policy.assess_feedback("missed", 1))
        out = self._feedback("missed", "k1")
        self.assertNotIn("signal_id", out)
        # 第三次漏服转人工但不暂停
        self._feedback("missed", "k2")
        out3 = self._feedback("missed", "k3")
        self.assertEqual(out3["auto_action"], "manual_handoff_only")
        self.assertEqual(self.s.store.get_plan("plan-1").status, d.PLAN_ACTIVE)

    def test_销售无权关闭异常(self):
        out = self._feedback("concern", "k1")
        with self.assertRaises(BoundaryError):
            self.s.close_signal(self.w["sales"], out["signal_id"], "销售特批")
        # 信号仍然开着
        self.assertNotEqual(self.s.store.get_signal(out["signal_id"]).status,
                            d.SIGNAL_CLOSED)

    def test_药师关闭异常仍需资质授权双覆盖(self):
        out = self._feedback("side_effect", "k1")
        # ph-b 无 risk_review 资质
        with self.assertRaises(BoundaryError):
            self.s.close_signal(self.w["pharmb"], out["signal_id"], "已处理")
        # 合规可以关闭
        closed = self.s.close_signal(self.w["comp"], out["signal_id"], "复核关闭")
        self.assertEqual(closed["signal_status"], d.SIGNAL_CLOSED)

    def test_转诊进度可更新(self):
        out = self._feedback("side_effect", "k1")
        upd = self.s.update_referral(self.w["pharma"], out["referral_id"],
                                     d.REFERRAL_ACCEPTED, "已预约门诊")
        self.assertEqual(upd["referral_status"], d.REFERRAL_ACCEPTED)


class 提醒不可变与幂等测试(unittest.TestCase):
    def setUp(self):
        self.s = make_service()
        self.w = bootstrap(self.s)
        make_plan(self.s, self.w["pharma"])

    def test_已发送提醒不得改写(self):
        self.s.schedule_reminder(self.w["pharma"], {
            "reminder_id": "r1", "plan_id": "plan-1", "medication": "二甲双胍",
            "scheduled_at": "2026-10-02T08:00:00+00:00", "channel": "sms"})
        res = self.s.run_due()
        self.assertEqual(res["sent"], ["r1"])
        with self.assertRaises(UpdateBlocked):
            self.s.store.cancel_reminder("r1", "试图撤回")
        with self.assertRaises(UpdateBlocked):
            self.s.store.block_reminder("r1", "试图拦截")

    def test_新信息只调整未发送计划(self):
        self.s.schedule_reminder(self.w["pharma"], {
            "reminder_id": "old", "plan_id": "plan-1", "medication": "二甲双胍",
            "scheduled_at": "2026-10-10T08:00:00+00:00", "channel": "sms"})
        self.s.schedule_reminder(self.w["pharma"], {
            "reminder_id": "sent", "plan_id": "plan-1", "medication": "二甲双胍",
            "scheduled_at": "2026-10-02T08:00:00+00:00", "channel": "sms"})
        self.s.run_due()
        out = self.s.adjust_pending(self.w["pharma"], "plan-1", {"reminders": [{
            "reminder_id": "new", "medication": "二甲双胍缓释片",
            "scheduled_at": "2026-10-11T08:00:00+00:00", "channel": "sms"}]})
        self.assertEqual(out["cancelled_pending"], ["old"])
        self.assertEqual(out["rescheduled"], ["new"])
        self.assertTrue(out["sent_reminders_untouched"])
        self.assertEqual(self.s.store.get_reminder("sent").status, d.REMINDER_SENT)
        self.assertEqual(self.s.store.get_reminder("old").status, d.REMINDER_CANCELLED)

    def test_重复反馈沿用原回执(self):
        payload = {"plan_id": "plan-1", "content_code": "taken", "detail": "已服",
                   "idempotency_key": "dup-1"}
        first = self.s.record_feedback(self.w["customer"], payload)
        second = self.s.record_feedback(self.w["customer"], payload)
        self.assertFalse(first["duplicate"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(second["receipt_no"], first["receipt_no"])
        self.assertEqual(second["duplicate_of"], first["receipt_no"])
        # 只落了一条反馈
        self.assertEqual(len(self.s.store.list_feedback(plan_id="plan-1")), 1)


class 撤回与保留测试(unittest.TestCase):
    def setUp(self):
        self.s = make_service()
        self.w = bootstrap(self.s)
        make_plan(self.s, self.w["pharma"])
        for rid, ts in [("future-1", "2026-10-10T08:00:00+00:00"),
                        ("future-2", "2026-10-11T08:00:00+00:00"),
                        ("past", "2026-10-01T08:00:00+00:00")]:
            self.s.schedule_reminder(self.w["pharma"], {
                "reminder_id": rid, "plan_id": "plan-1", "medication": "二甲双胍",
                "scheduled_at": ts, "channel": "sms"})
        self.s.run_due()  # past 已发送

    def test_撤回停止未来联系且保留已发送(self):
        out = self.s.revoke_consent(self.w["customer"], "c1", reason="顾客要求")
        self.assertEqual(set(out["cancelled_reminders"]), {"future-1", "future-2"})
        self.assertEqual(self.s.store.get_reminder("past").status, d.REMINDER_SENT)
        for rid in ("future-1", "future-2"):
            self.assertEqual(self.s.store.get_reminder(rid).status, d.REMINDER_CANCELLED)
            self.assertEqual(self.s.store.get_reminder(rid).skip_reason,
                             policy.SKIP_CONSENT_REVOKED)

    def test_撤回后旧授权版本的计划不得继续发送(self):
        self.s.revoke_consent(self.w["customer"], "c1")
        # 重新授权生成 v2；旧计划仍挂 v1，必须经人工复核而不是照发
        self.s.grant_consent(self.w["comp"], {"customer_id": "c1", "scopes": ALL_SCOPES,
                                              "channels": ["sms"]})
        with self.assertRaises(BoundaryError) as ctx:
            self.s.schedule_reminder(self.w["pharma"], {
                "reminder_id": "stale-ver", "plan_id": "plan-1",
                "medication": "二甲双胍",
                "scheduled_at": "2026-10-02T08:30:00+00:00", "channel": "sms"})
        self.assertEqual(policy.SKIP_CONSENT_VERSION, str(ctx.exception).split("：")[-1])

    def test_保留最小事实仅合规可查(self):
        self.s.revoke_consent(self.w["customer"], "c1", reason="x")
        with self.assertRaises(BoundaryError):
            self.s.retained_facts(self.w["pharma"], "c1")
        with self.assertRaises(BoundaryError):
            self.s.retained_facts(self.w["sales"], "c1")
        facts = self.s.retained_facts(self.w["comp"], "c1")
        self.assertEqual(facts["retained_facts"][0]["version"], 1)
        self.assertTrue(facts["retained_facts"][0]["revoked_at"])


class 标识隔离测试(unittest.TestCase):
    def setUp(self):
        self.s = make_service()
        self.w = bootstrap(self.s)

    def test_同一标识不同健康内容先隔离再复核(self):
        self.s.observe_identity(self.w["comp"], "13800000000", "c1", "高血压方案")
        out = self.s.observe_identity(self.w["comp"], "13800000000", "c-other", "糖尿病方案")
        self.assertTrue(out["quarantined"])
        case_id = out["quarantine_case_id"]
        # 隔离期间该顾客的新提醒被拦截
        make_plan(self.s, self.w["pharma"])
        with self.assertRaises(BoundaryError):
            self.s.schedule_reminder(self.w["pharma"], {
                "reminder_id": "r1", "plan_id": "plan-1", "medication": "二甲双胍",
                "scheduled_at": "2026-10-03T08:00:00+00:00", "channel": "sms"})
        # 非合规不能复核
        with self.assertRaises(BoundaryError):
            self.s.review_quarantine(self.w["pharma"], case_id, True, "误报")
        reviewed = self.s.review_quarantine(self.w["comp"], case_id, True,
                                            "号码为家庭成员共用，档案已拆分")
        self.assertEqual(reviewed["status"], d.QUARANTINE_RELEASED)
        # 复核放行后可以继续
        self.s.schedule_reminder(self.w["pharma"], {
            "reminder_id": "r2", "plan_id": "plan-1", "medication": "二甲双胍",
            "scheduled_at": "2026-10-03T08:00:00+00:00", "channel": "sms"})


class 交接与续跑测试(unittest.TestCase):
    def setUp(self):
        self.s = make_service()
        self.w = bootstrap(self.s)
        make_plan(self.s, self.w["pharma"])
        self.s.reserve_supply(self.w["pharma"], {
            "batch_id": "b1", "plan_id": "plan-1", "medication": "二甲双胍",
            "quantity": 2, "responsible_store_id": "store-A",
            "backup_store_id": "store-C", "due_at": "2026-10-07T00:00:00+00:00"})
        self.s.schedule_reminder(self.w["pharma"], {
            "reminder_id": "future", "plan_id": "plan-1", "medication": "二甲双胍",
            "scheduled_at": "2026-10-05T08:00:00+00:00", "channel": "sms"})

    def test_交接转移保供责任且到期任务继续(self):
        self.s.initiate_handoff(self.w["pharma"], {
            "handoff_id": "h1", "plan_id": "plan-1", "to_store_id": "store-B",
            "to_pharmacist_id": "ph-b", "effective_at": "2026-10-03T00:00:00+00:00"})
        out = self.s.complete_handoff(self.w["pharmb"], "h1")
        self.assertEqual(out["transferred_batches"], ["b1"])
        self.assertEqual(self.s.store.get_supply_batch("b1").responsible_store_id,
                         "store-B")
        # 原门店药师不能再操作；承接药师可以
        with self.assertRaises(BoundaryError):
            self.s.schedule_reminder(self.w["pharma"], {
                "reminder_id": "x", "plan_id": "plan-1", "medication": "二甲双胍",
                "scheduled_at": "2026-10-06T08:00:00+00:00", "channel": "sms"})
        self.s.schedule_reminder(self.w["pharmb"], {
            "reminder_id": "y", "plan_id": "plan-1", "medication": "二甲双胍",
            "scheduled_at": "2026-10-06T08:00:00+00:00", "channel": "sms"})

    def test_承接药师资质不足不能交接(self):
        self.s.register_pharmacist(self.w["comp"], {
            "pharmacist_id": "ph-c", "store_id": "store-B", "license_no": "LC",
            "scopes": ["med_reminder"], "valid_from": "2026-01-01T00:00:00+00:00",
            "valid_to": "2027-01-01T00:00:00+00:00"})
        with self.assertRaises(BoundaryError):
            self.s.initiate_handoff(self.w["pharma"], {
                "handoff_id": "h2", "plan_id": "plan-1", "to_store_id": "store-B",
                "to_pharmacist_id": "ph-c", "effective_at": "2026-10-03T00:00:00+00:00"})

    def test_进程中断换实例后续跑(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            first = make_service(path)
            w = bootstrap(first)
            make_plan(first, w["pharma"])
            first.schedule_reminder(w["pharma"], {
                "reminder_id": "r-disk", "plan_id": "plan-1", "medication": "二甲双胍",
                "scheduled_at": "2026-10-02T08:00:00+00:00", "channel": "sms"})
            second = make_service(path)  # 模拟重启
            res = second.run_due()
            self.assertEqual(res["sent"], ["r-disk"])
            self.assertTrue(second.store.verify_audit_chain())
        finally:
            os.unlink(path)

    def test_到期发送前实时阻断并返回原因码(self):
        # 再加一条已到期提醒：资质过期时应 blocked 而非发出
        self.s.schedule_reminder(self.w["pharma"], {
            "reminder_id": "due-now", "plan_id": "plan-1", "medication": "二甲双胍",
            "scheduled_at": "2026-10-02T08:00:00+00:00", "channel": "sms"})
        # 资质过期：把 ph-a 的资质改成过期
        self.s.register_pharmacist(self.w["comp"], {
            "pharmacist_id": "ph-a", "store_id": "store-A", "license_no": "LA",
            "scopes": ALL_SCOPES, "valid_from": "2020-01-01T00:00:00+00:00",
            "valid_to": "2021-01-01T00:00:00+00:00"})
        res = self.s.run_due()
        self.assertEqual(res["sent"], [])
        blocked_ids = {b["reminder_id"]: b["skip_reason"] for b in res["blocked"]}
        self.assertEqual(blocked_ids["due-now"], policy.SKIP_CREDENTIAL_EXPIRED)


class 三视图测试(unittest.TestCase):
    def setUp(self):
        self.s = make_service()
        self.w = bootstrap(self.s)
        make_plan(self.s, self.w["pharma"])
        self.s.schedule_reminder(self.w["pharma"], {
            "reminder_id": "r1", "plan_id": "plan-1", "medication": "二甲双胍",
            "scheduled_at": "2026-10-01T08:00:00+00:00", "channel": "sms"})
        self.s.run_due()
        self.risk = self.s.record_feedback(self.w["customer"], {
            "plan_id": "plan-1", "content_code": "side_effect", "detail": "皮疹",
            "idempotency_key": "k1"})

    def test_顾客视图含计划来源与未提醒原因但无内部信号(self):
        view = self.s.customer_view(self.w["customer"], "c1")
        self.assertIn("plan_source", view["plans"][0])
        self.assertEqual(view["plans"][0]["plan_source"]["kind"], "pharmacist_created")
        self.assertNotIn("signals", view)
        self.assertNotIn("audit", view)
        self.assertIn("medical_boundary", view)
        # 转诊进度对顾客可见
        self.assertTrue(any(r["referral_id"] == self.risk["referral_id"]
                            for r in view["referrals"]))

    def test_顾客只能看本人(self):
        with self.assertRaises(BoundaryError):
            self.s.customer_view(Actor("c-other", d.ROLE_CUSTOMER), "c1")

    def test_药师视图含信号反馈但无审计链(self):
        view = self.s.pharmacist_view(self.w["pharma"], "c1")
        self.assertIn("signals", view)
        self.assertIn("feedback", view)
        self.assertNotIn("audit", view)

    def test_销售视图看不到健康内容(self):
        view = self.s.customer_view  # 销售无顾客视图权限
        with self.assertRaises(BoundaryError):
            self.s.pharmacist_view(self.w["sales"], "c1")
        # 销售角色的字段白名单里没有反馈明细
        self.assertNotIn("detail", policy.filter_view(d.ROLE_SALES, {"detail": "皮疹"}))

    def test_合规视图含完整审计链(self):
        view = self.s.compliance_view(self.w["comp"], "c1")
        self.assertIn("audit", view)
        self.assertTrue(any(e["action"] == "risk.escalate" for e in view["audit"]))


class 审计链与诊断边界测试(unittest.TestCase):
    def test_审计链覆盖全部实体且可验篡(self):
        s = make_service()
        w = bootstrap(s)
        make_plan(s, w["pharma"])
        s.record_feedback(w["customer"], {"plan_id": "plan-1", "content_code": "taken",
                                          "detail": "已服", "idempotency_key": "k"})
        self.assertTrue(s.store.verify_audit_chain())
        # 直接篡改一条载荷 → 断链
        s.store.connection.execute("UPDATE audit_log SET payload=? WHERE seq=1",
                                   ('{"hacked": true}',))
        self.assertFalse(s.store.verify_audit_chain())

    def test_系统响应不得夹带诊断结论(self):
        with self.assertRaises(DiagnosisProhibited):
            policy.assert_no_diagnosis({"note": "本系统诊断为2型糖尿病"})
        with self.assertRaises(DiagnosisProhibited):
            policy.assert_no_diagnosis({"items": [{"x": "AI 诊断：高血压"}]})
        # 顾客自述/外部文书转述允许存在
        policy.assert_no_diagnosis({"detail": "顾客转述社区医院诊断意见"})

    def test_服务出站统一过诊断守卫(self):
        s = make_service()
        w = bootstrap(s)
        make_plan(s, w["pharma"])
        # 任何响应（含对顾客的视图/回执）只要夹带系统诊断断言，出站即拒
        with self.assertRaises(DiagnosisProhibited):
            s.customer_view  # 确保 service 可引用
            Service._out(d.ROLE_CUSTOMER, {
                "receipt_no": "rcpt-1",
                "note": "您被诊断为2型糖尿病，请继续服药",
            })


class 角色边界补充测试(unittest.TestCase):
    def setUp(self):
        self.s = make_service()
        self.w = bootstrap(self.s)
        make_plan(self.s, self.w["pharma"])

    def test_销售不能登记或撤回授权(self):
        with self.assertRaises(BoundaryError):
            self.s.grant_consent(self.w["sales"], {"customer_id": "c1",
                                                   "scopes": ALL_SCOPES, "channels": ["sms"]})
        with self.assertRaises(BoundaryError):
            self.s.revoke_consent(self.w["sales"], "c1")

    def test_顾客只能操作本人授权与反馈(self):
        other = Actor("c-other", d.ROLE_CUSTOMER)
        with self.assertRaises(BoundaryError):
            self.s.revoke_consent(other, "c1")
        with self.assertRaises(BoundaryError):
            self.s.record_feedback(other, {"plan_id": "plan-1", "content_code": "taken",
                                           "idempotency_key": "kx"})

    def test_暂停计划不能新增提醒(self):
        self.s.pause_plan("plan-1", self.w["comp"], "manual")
        with self.assertRaises(BoundaryError) as ctx:
            self.s.schedule_reminder(self.w["pharma"], {
                "reminder_id": "r-blocked", "plan_id": "plan-1",
                "medication": "二甲双胍",
                "scheduled_at": "2026-10-03T08:00:00+00:00", "channel": "sms"})
        self.assertEqual(policy.SKIP_PLAN_PAUSED, str(ctx.exception).split("：")[-1])


if __name__ == "__main__":
    unittest.main()
