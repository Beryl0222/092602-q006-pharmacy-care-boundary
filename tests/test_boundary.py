"""健康陪伴边界台的规则测试。

覆盖：资质×授权双覆盖、跨店撤回拦截、计划不可改写、风险阈值自动暂停、
销售无权关闭异常、重复反馈幂等、同标识异内容隔离、门店交接续跑、
三类角色视图、最小事实保留，以及"无自行生成诊断结论"边界。
"""
import unittest

from pharmacy_care_boundary import domain as d
from pharmacy_care_boundary.domain import (
    Actor,
    BoundaryError,
    Credential,
    PermissionDenied,
    ScopeNotCovered,
)
from pharmacy_care_boundary.service import Service
from pharmacy_care_boundary.store import Store

T0 = "2026-10-01T08:00:00+00:00"
T1 = "2026-10-02T08:00:00+00:00"
T2 = "2026-10-03T08:00:00+00:00"
T3 = "2026-10-04T08:00:00+00:00"
VALID_FROM = "2026-01-01T00:00:00+00:00"
VALID_TO = "2026-12-31T00:00:00+00:00"
ALL_MATTERS = list(d.MATTERS)

STORE_A = "store-A"
STORE_B = "store-B"
CUST = "cust-1"


def 合规() -> Actor:
    return Actor(actor_id="compliance-1", role=d.ROLE_COMPLIANCE)


def 药师A(matters=None) -> Actor:
    return Actor(
        actor_id="pharm-A", role=d.ROLE_PHARMACIST,
        pharmacist_id="pharm-A", store_id=STORE_A,
    )


def 药师B() -> Actor:
    return Actor(
        actor_id="pharm-B", role=d.ROLE_PHARMACIST,
        pharmacist_id="pharm-B", store_id=STORE_B,
    )


def 顾客() -> Actor:
    return Actor(actor_id=CUST, role=d.ROLE_CUSTOMER)


def 销售() -> Actor:
    return Actor(actor_id="sales-1", role=d.ROLE_SALES)


class 边界台测试基类(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service(Store())
        self.service.register_credential(
            合规(),
            Credential("pharm-A", STORE_A, tuple(ALL_MATTERS), VALID_FROM, VALID_TO),
        )
        self.service.register_credential(
            合规(),
            Credential("pharm-B", STORE_B, tuple(ALL_MATTERS), VALID_FROM, VALID_TO),
        )
        self.service.grant_consent(
            合规(), CUST, ALL_MATTERS, STORE_A, at=T0
        )

    def 建计划(self, actor=None, customer_id=CUST, at=T0) -> str:
        result = self.service.import_plan(
            actor or 药师A(), customer_id, "prescription", "RX-2026-0001", at=at
        )
        return result["plan_id"]


class 双覆盖测试(边界台测试基类):
    def test_资质与授权都覆盖才能处理记录(self):
        # 无资质药师
        ghost = Actor("ghost", d.ROLE_PHARMACIST, "ghost", STORE_A)
        with self.assertRaises(ScopeNotCovered):
            self.service.import_plan(ghost, CUST, "prescription", "RX-1", at=T0)

        # 资质过期
        self.service.register_credential(
            合规(),
            Credential("pharm-exp", STORE_A, tuple(ALL_MATTERS),
                       "2025-01-01T00:00:00+00:00", "2026-09-01T00:00:00+00:00"),
        )
        expired = Actor("pharm-exp", d.ROLE_PHARMACIST, "pharm-exp", STORE_A)
        with self.assertRaises(ScopeNotCovered):
            self.service.import_plan(expired, CUST, "prescription", "RX-1", at=T1)

        # 授权不含该事项：新顾客只授权用药提醒，药师却做应急保供
        self.service.grant_consent(
            合规(), "cust-2", [d.MATTER_MEDICATION_REMINDER], STORE_A, at=T0
        )
        with self.assertRaises(ScopeNotCovered):
            self.service.create_batch(药师A(), "cust-2", "plan-x", at=T0)

        # 非药师角色直接拒绝
        with self.assertRaises(PermissionDenied):
            self.service.import_plan(销售(), CUST, "prescription", "RX-1", at=T0)

    def test_资质不覆盖具体事项时拒绝(self):
        self.service.register_credential(
            合规(),
            Credential("pharm-narrow", STORE_A,
                       (d.MATTER_MEDICATION_REMINDER,), VALID_FROM, VALID_TO),
        )
        narrow = Actor("pharm-narrow", d.ROLE_PHARMACIST, "pharm-narrow", STORE_A)
        with self.assertRaises(ScopeNotCovered):
            self.service.report_signals(narrow, CUST, ["supply_gap"], at=T0)

    def test_药品计划必须来自处方或医嘱(self):
        with self.assertRaises(BoundaryError):
            self.service.import_plan(药师A(), CUST, "system_guess", "", at=T0)


class 撤回与跨店拦截测试(边界台测试基类):
    def test_撤回前两店均可发送_撤回后全连锁停发(self):
        plan_id = self.建计划()
        self.service.schedule_task(
            药师A(), CUST, plan_id, d.MATTER_MEDICATION_REMINDER, T1, STORE_A
        )
        self.service.schedule_task(
            药师B(), CUST, plan_id, d.MATTER_MEDICATION_REMINDER, T1, STORE_B,
            task_id="task-b",
        )
        sent = self.service.run_due(合规(), T1)
        self.assertEqual({r["decision"] for r in sent}, {"sent"})

        # 第二位顾客复现事故：撤回后另一家门店不得再发
        self.service.grant_consent(合规(), "cust-x", ALL_MATTERS, STORE_A, at=T0)
        px = self.service.import_plan(
            药师A(), "cust-x", "prescription", "RX-X", at=T0
        )["plan_id"]
        self.service.schedule_task(
            药师A(), "cust-x", px, d.MATTER_MEDICATION_REMINDER, T2, STORE_A,
            task_id="xa",
        )
        self.service.schedule_task(
            药师B(), "cust-x", px, d.MATTER_MEDICATION_REMINDER, T2, STORE_B,
            task_id="xb",
        )
        self.service.withdraw_consent(
            Actor("cust-x", d.ROLE_CUSTOMER), "cust-x", at=T1)
        # 撤回在全连锁即时生效：两家门店的未发送任务都带原因落库，
        # 到期处理不会再发出任何一条。
        results = self.service.run_due(合规(), T2)
        self.assertEqual(results, [])
        for task_id in ("xa", "xb"):
            task = self.service.store.get_task(task_id)
            self.assertEqual(task.state, d.TASK_SKIPPED)
            self.assertEqual(task.skip_reason, d.SKIP_CONSENT_WITHDRAWN)

        # 撤回是追加版本，历史授权仍保留可审计
        versions = self.service.store.latest_consent("cust-x")
        self.assertEqual(versions.state, d.CONSENT_WITHDRAWN)
        self.assertEqual(versions.version, 2)

    def test_撤回后药师任何处理都被拒绝(self):
        self.service.withdraw_consent(顾客(), CUST, at=T1)
        with self.assertRaises(ScopeNotCovered):
            self.service.import_plan(药师A(), CUST, "prescription", "RX-2", at=T2)


class 计划不可改写测试(边界台测试基类):
    def test_新信息只调整未发送任务(self):
        plan_id = self.建计划()
        self.service.schedule_task(
            药师A(), CUST, plan_id, d.MATTER_MEDICATION_REMINDER, T1, STORE_A,
            task_id="old-1",
        )
        self.service.schedule_task(
            药师A(), CUST, plan_id, d.MATTER_MEDICATION_REMINDER, T2, STORE_A,
            task_id="old-2",
        )
        self.service.run_due(合规(), T1)  # old-1 已发送并冻结

        outcome = self.service.apply_plan_update(
            药师A(),
            plan_id,
            [{"matter": d.MATTER_MEDICATION_REMINDER, "due_at": T3, "store_id": STORE_A}],
            at=T1,
        )
        self.assertEqual(outcome["adjusted"], ["old-2"])
        self.assertEqual(outcome["sent_untouched"], ["old-1"])

        old1 = self.service.store.get_task("old-1")
        self.assertEqual(old1.state, d.TASK_SENT)
        self.assertEqual(old1.sent_at != "", True)
        old2 = self.service.store.get_task("old-2")
        self.assertEqual(old2.state, d.TASK_SKIPPED)
        self.assertEqual(old2.skip_reason, d.SKIP_PLAN_ADJUSTED)


class 风险阈值测试(边界台测试基类):
    def test_达阈值自动暂停_建议转诊_转人工(self):
        plan_id = self.建计划()
        self.service.schedule_task(
            药师A(), CUST, plan_id, d.MATTER_MEDICATION_REMINDER, T2, STORE_A
        )
        result = self.service.record_feedback(
            药师A(), CUST, plan_id, "服药后出现皮疹", "fb-1",
            signals=["adverse_reaction"], at=T1,
        )
        self.assertEqual(result["action"], "auto_pause_and_escalate")
        anomaly_id = result["anomaly_id"]

        # 未发送任务已暂停，到期处理不会再联系顾客
        self.assertEqual(self.service.run_due(合规(), T2), [])

        # 人工接管
        taken = self.service.human_takeover(药师A(), anomaly_id, at=T2)
        self.assertEqual(taken["state"], d.ANOMALY_ESCALATED)

        # 销售无权关闭；合规可以
        with self.assertRaises(PermissionDenied):
            self.service.close_anomaly(销售(), anomaly_id, "考核季冲量", at=T3)
        closed = self.service.close_anomaly(合规(), anomaly_id, "已协助就医", at=T3)
        self.assertEqual(closed["state"], d.ANOMALY_CLOSED)

    def test_未达阈值仅登记异常_不暂停(self):
        result = self.service.report_signals(
            药师A(), CUST, ["repeated_missed_dose"], at=T1
        )
        self.assertEqual(result["state"], d.ANOMALY_OPEN)
        self.assertLess(result["score"], d.RISK_THRESHOLD)

    def test_人工处置后可恢复提醒(self):
        plan_id = self.建计划()
        self.service.schedule_task(
            药师A(), CUST, plan_id, d.MATTER_MEDICATION_REMINDER, T3, STORE_A,
            task_id="t-resume",
        )
        triage = self.service.report_signals(
            药师A(), CUST, ["overdose"], at=T1
        )
        self.service.human_takeover(药师A(), triage["anomaly_id"], at=T2)
        self.service.close_anomaly(合规(), triage["anomaly_id"], "处置完成", at=T2)
        resumed = self.service.resume_tasks(合规(), CUST, at=T2)
        self.assertIn("t-resume", resumed["resumed"])


class 重复反馈测试(边界台测试基类):
    def test_重复反馈沿用原回执(self):
        plan_id = self.建计划()
        first = self.service.record_feedback(
            药师A(), CUST, plan_id, "已按时服药", "key-1", at=T1
        )
        second = self.service.record_feedback(
            药师A(), CUST, plan_id, "已按时服药（重复提交）", "key-1", at=T1
        )
        self.assertTrue(second["deduped"])
        self.assertEqual(second["receipt"], first["receipt"])


class 隐私隔离测试(边界台测试基类):
    def test_同一标识不同健康内容先隔离再复核(self):
        plan_id = self.建计划()
        self.service.schedule_task(
            药师A(), CUST, plan_id, d.MATTER_MEDICATION_REMINDER, T2, STORE_A,
            task_id="t-q",
        )
        first = self.service.ingest_health_content(
            药师A(), CUST, "高血压随访记录 A", STORE_A, at=T0
        )
        self.assertFalse(first["quarantined"])

        second = self.service.ingest_health_content(
            药师B(), CUST, "糖尿病随访记录 B（同一标识、不同内容）", STORE_B, at=T1
        )
        self.assertTrue(second["quarantined"])

        # 隔离期间到期不发送
        self.assertEqual(self.service.run_due(合规(), T2), [])

        # 药师不能复核，只有合规可以
        with self.assertRaises(PermissionDenied):
            self.service.review_quarantine(药师A(), second["case_id"], d.REVIEW_MERGE)

        reviewed = self.service.review_quarantine(
            合规(), second["case_id"], d.REVIEW_FIRST_WINS, at=T2
        )
        self.assertEqual(reviewed["state"], d.QUARANTINE_REJECTED)
        self.assertIn("t-q", reviewed["resumed"])

        # 复核解除后任务可继续到期
        outcomes = self.service.run_due(合规(), T3)
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(outcomes[0]["decision"], "sent")

    def test_复核确认同一人时合并画像(self):
        self.service.ingest_health_content(药师A(), CUST, "内容一", STORE_A, at=T0)
        held = self.service.ingest_health_content(
            药师A(), CUST, "内容二（更新）", STORE_A, at=T1
        )
        self.service.review_quarantine(合规(), held["case_id"], d.REVIEW_MERGE, at=T2)
        # 合并后再次提交"内容二"不再隔离
        again = self.service.ingest_health_content(
            药师A(), CUST, "内容二（更新）", STORE_A, at=T3
        )
        self.assertFalse(again["quarantined"])


class 门店交接测试(边界台测试基类):
    def test_交接后到期任务与保供责任在新店继续(self):
        plan_id = self.建计划()
        self.service.schedule_task(
            药师A(), CUST, plan_id, d.MATTER_MEDICATION_REMINDER, T2, STORE_A,
            task_id="t-move",
        )
        batch = self.service.create_batch(药师A(), CUST, plan_id, STORE_A, at=T1)

        handover = self.service.open_handover(药师A(), CUST, STORE_B, at=T1)
        # 交接未承接：A 店到期任务不得发出
        blocked = self.service.run_due(合规(), T2)
        self.assertEqual(blocked[0]["reason"], d.SKIP_HANDOVER_PENDING)

        accepted = self.service.accept_handover(药师B(), handover["handover_id"], at=T2)
        self.assertIn("t-move", accepted["continued_tasks"])
        self.assertIn(batch["batch_id"], accepted["moved_batches"])

        # 服务进程恢复后到期任务在 B 店由 B 店药师继续
        outcomes = self.service.run_due(合规(), T3)
        sent = outcomes[0]
        self.assertEqual(sent["decision"], "sent")
        self.assertEqual(sent["handled_by"], "pharm-B")

        # 保供责任已转到 B 店，A 店药师不能再更新
        with self.assertRaises(ScopeNotCovered):
            self.service.update_batch_status(药师A(), batch["batch_id"], d.BATCH_DISPATCHED)
        moved = self.service.update_batch_status(
            药师B(), batch["batch_id"], d.BATCH_DISPATCHED, at=T3
        )
        self.assertEqual(moved["owner_store_id"], STORE_B)


class 角色视图测试(边界台测试基类):
    def test_顾客看到计划来源_未提醒原因_转诊与保供责任(self):
        plan_id = self.建计划()
        self.service.schedule_task(
            药师A(), CUST, plan_id, d.MATTER_MEDICATION_REMINDER, T1, STORE_A,
            task_id="t-view",
        )
        self.service.withdraw_consent(顾客(), CUST, at=T0.replace("08:00", "09:00"))
        self.service.run_due(合规(), T1)

        view = self.service.customer_view(顾客(), CUST)
        self.assertEqual(view["future_contact"], "stopped")
        reminder = next(r for r in view["reminders"] if r["task_id"] == "t-view")
        self.assertEqual(reminder["skip_reason"], d.SKIP_CONSENT_WITHDRAWN)
        self.assertIn("不构成诊断", view["notice"])

    def test_药师视图需要双覆盖(self):
        view = self.service.pharmacist_view(药师A(), CUST, d.MATTER_MEDICATION_REMINDER, T1)
        self.assertEqual(view["consent_version"], 1)
        with self.assertRaises(ScopeNotCovered):
            self.service.pharmacist_view(
                Actor("pharm-c", d.ROLE_PHARMACIST, "pharm-c", STORE_A),
                CUST, d.MATTER_MEDICATION_REMINDER, T1,
            )

    def test_撤回后合规仅见最小事实_健康内容屏蔽(self):
        self.建计划()
        self.service.withdraw_consent(顾客(), CUST, at=T1)
        view = self.service.compliance_view(合规(), CUST)
        self.assertEqual(view["access_mode"], "minimal_facts")
        self.assertEqual(view["health_content"], "[redacted: consent withdrawn]")
        # 仍可查看授权历史与计划来源等法规最小事实
        self.assertEqual(len(view["consent_versions"]), 2)
        self.assertEqual(view["plan_sources"][0]["source_ref"], "RX-2026-0001")
        # 顾客与药师都看不到最小事实视图
        with self.assertRaises(PermissionDenied):
            self.service.compliance_view(销售(), CUST)

    def test_转诊进度对三方可追踪(self):
        triage = self.service.report_signals(
            药师A(), CUST, ["adverse_reaction"], at=T1
        )
        self.service.update_referral(
            药师A(), triage["referral_id"], d.REFERRAL_IN_PROGRESS, at=T2
        )
        view = self.service.customer_view(顾客(), CUST)
        referral = next(r for r in view["referrals"]
                        if r["referral_id"] == triage["referral_id"])
        self.assertEqual(referral["status"], d.REFERRAL_IN_PROGRESS)


class 审计链与诊断边界测试(边界台测试基类):
    def test_所有事件在同一条哈希链上且可校验(self):
        plan_id = self.建计划()
        self.service.schedule_task(
            药师A(), CUST, plan_id, d.MATTER_MEDICATION_REMINDER, T1, STORE_A
        )
        self.service.run_due(合规(), T1)
        report = self.service.store.verify_chain()
        self.assertTrue(report["ok"])
        self.assertGreater(report["length"], 3)
        actions = {e["action"] for e in self.service.store.list_audit(CUST)}
        self.assertIn("plan.import", actions)
        self.assertIn("task.send", actions)

    def test_篡改审计载荷可被发现(self):
        self.建计划()
        self.service.store.connection.execute(
            "UPDATE audit_log SET payload=? WHERE seq=1", ('{"tampered":true}',)
        )
        report = self.service.store.verify_chain()
        self.assertFalse(report["ok"])
        self.assertEqual(report["broken_at"], 1)

    def test_系统文本含自行诊断结论时被拦截(self):
        with self.assertRaises(BoundaryError):
            d.assert_no_generated_diagnosis("根据您的描述，诊断为高血压，请加量服药")
        with self.assertRaises(BoundaryError):
            d.assert_no_generated_diagnosis("建议您停药观察")
        # 正常提醒与免责声明通过
        d.assert_no_generated_diagnosis("用药提醒：请按医嘱用药。" + d.BOUNDARY_NOTICE)

    def test_已发送提醒不包含诊断结论(self):
        plan_id = self.建计划()
        self.service.schedule_task(
            药师A(), CUST, plan_id, d.MATTER_MEDICATION_REMINDER, T1, STORE_A,
            task_id="t-msg",
        )
        outcome = self.service.run_due(合规(), T1)[0]
        self.assertNotIn("诊断为", outcome["message"])
        self.assertTrue(outcome["message"].endswith(d.BOUNDARY_NOTICE))


if __name__ == "__main__":
    unittest.main()
