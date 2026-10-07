"""健康陪伴边界台的应用服务。

所有用例都从这里进入；服务层负责编排：双重覆盖校验 → 状态变更 →
审计上链 → 按角色过滤输出。策略判断在 policy.py，持久化在 store.py。

边界声明：本服务输出用药计划、提醒、转诊建议与保供安排，
不输出、也不允许任何响应夹带自行生成的诊断或治疗结论。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

from . import domain as d
from . import policy
from .store import Store


class BoundaryError(Exception):
    """业务规则拒绝（权限不足、状态不允许等）。"""


@dataclass(frozen=True)
class Actor:
    actor_id: str
    role: str
    pharmacist_id: str = ""   # role=pharmacist 时对应资质编号


class Service:
    def __init__(self, store: Store | None = None, clock=d.now_iso) -> None:
        self.store = store or Store()
        self._clock = clock

    def _now(self) -> str:
        return self._clock()

    # ------------------------------------------------------------------
    # 旧版兼容
    # ------------------------------------------------------------------
    def health(self) -> dict[str, str]:
        return {"service": "pharmacy_care_boundary", "status": "ok"}

    def register(self, payload: dict[str, object]) -> dict[str, object]:
        required = ("record_id", "owner_id", "state")
        missing = [name for name in required if not str(payload.get(name, "")).strip()]
        if missing:
            raise ValueError("缺少必要字段：" + "、".join(missing))
        record = d.Record(
            record_id=str(payload["record_id"]), owner_id=str(payload["owner_id"]),
            state=str(payload["state"]), revision=int(payload.get("revision", 1)),
        )
        return self.store.add(record).__dict__.copy()

    def find(self, record_id: str) -> dict[str, object] | None:
        value = self.store.get(record_id)
        return value.__dict__.copy() if value else None

    # ------------------------------------------------------------------
    # 内部小工具
    # ------------------------------------------------------------------
    def _audit(self, actor: Actor, action: str, entity_type: str, entity_id: str,
               payload: dict | None = None) -> None:
        self.store.append_audit(
            actor_id=actor.actor_id, actor_role=actor.role, action=action,
            entity_type=entity_type, entity_id=entity_id, payload=payload,
        )

    def _pharmacist_for(self, actor: Actor) -> d.PharmacistCredential:
        cred = self.store.get_pharmacist(actor.pharmacist_id or actor.actor_id)
        if cred is None:
            raise BoundaryError("药师资质不存在")
        return cred

    def _guard_scope(self, actor: Actor, scope: str, *, customer_id: str,
                     channel: str | None = None, store_id: str | None = None,
                     consent_version: int | None = None) -> d.ConsentGrant:
        """资质 + 授权双重覆盖，缺一即拒绝。"""
        consent = self.store.latest_consent(customer_id)
        pharmacist = self._pharmacist_for(actor) if actor.role == d.ROLE_PHARMACIST else None
        decision = policy.can_handle(
            scope=scope, pharmacist=pharmacist, consent=consent, at=self._now(),
            channel=channel, store_id=store_id, consent_version=consent_version,
        )
        if not decision:
            raise BoundaryError(f"当前事项未被资质与授权同时覆盖：{decision.reason}")
        return consent  # type: ignore[return-value]

    def _current_store_and_pharmacist(self, plan: d.MedicationPlan):
        """经门店交接后，提醒由承接门店与承接药师负责。"""
        handoffs = self.store.list_handoffs(plan_id=plan.plan_id)
        done = [h for h in handoffs if h.status == d.HANDOFF_COMPLETED]
        if done:
            latest = done[-1]
            cred = self.store.get_pharmacist(latest.to_pharmacist_id)
            return latest.to_store_id, cred
        return plan.store_id, self.store.get_pharmacist(plan.pharmacist_id)

    @staticmethod
    def _fingerprint(health_text: str) -> str:
        return hashlib.sha256(health_text.encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def _out(role: str, payload: dict) -> dict:
        """所有出站响应：先过诊断守卫，再按角色裁剪字段。"""
        policy.assert_no_diagnosis(payload)
        return policy.filter_view(role, payload)

    @staticmethod
    def _guard_output(role: str, payload: dict) -> dict:
        """对仅含结构化码、不经字段裁剪的响应，只过诊断守卫。"""
        policy.assert_no_diagnosis(payload)
        return payload

    @staticmethod
    def _project(role: str, view: dict) -> dict:
        """对嵌套三视图：结构键保留，列表内逐条按角色裁剪字段。"""
        policy.assert_no_diagnosis(view)
        projected: dict = {}
        for key, value in view.items():
            if isinstance(value, list):
                projected[key] = [
                    policy.filter_view(role, item) if isinstance(item, dict) else item
                    for item in value
                ]
            else:
                projected[key] = value
        return projected

    # ------------------------------------------------------------------
    # 1) 顾客授权版本
    # ------------------------------------------------------------------
    def grant_consent(self, actor: Actor, payload: dict) -> dict:
        customer_id = str(payload["customer_id"])
        if actor.role not in (d.ROLE_CUSTOMER, d.ROLE_PHARMACIST, d.ROLE_COMPLIANCE):
            raise BoundaryError("销售/系统角色不能登记授权")
        if actor.role == d.ROLE_CUSTOMER and actor.actor_id != customer_id:
            raise BoundaryError("顾客只能登记本人授权")
        scopes = d.normalize_set(payload.get("scopes"))
        channels = d.normalize_set(payload.get("channels"))
        bad_scopes = scopes - d.CANONICAL_SCOPES
        bad_channels = channels - d.CANONICAL_CHANNELS
        if bad_scopes or bad_channels:
            raise BoundaryError(f"非法授权范围/渠道：{sorted(bad_scopes | bad_channels)}")
        previous = self.store.latest_consent(customer_id)
        version = (previous.version + 1) if previous else 1
        grant = d.ConsentGrant(
            customer_id=customer_id, version=version, scopes=scopes, channels=channels,
            granted_at=self._now(),
        )
        self.store.add_consent(grant)
        self._audit(actor, "consent.grant", "consent", f"{customer_id}:v{version}",
                    {"scopes": sorted(scopes), "channels": sorted(channels)})
        return self._out(actor.role, {
            "customer_id": customer_id, "version": version,
            "scopes": sorted(scopes), "channels": sorted(channels),
            "granted_at": grant.granted_at,
        })

    def revoke_consent(self, actor: Actor, customer_id: str, reason: str = "") -> dict:
        if actor.role not in (d.ROLE_CUSTOMER, d.ROLE_PHARMACIST, d.ROLE_COMPLIANCE):
            raise BoundaryError("销售/系统角色不能撤回授权")
        if actor.role == d.ROLE_CUSTOMER and actor.actor_id != customer_id:
            raise BoundaryError("顾客只能撤回本人授权")
        grant = self.store.latest_consent(customer_id)
        if grant is None or not grant.active:
            raise BoundaryError("没有可撤回的有效授权")
        at = self._now()
        self.store.revoke_consent(customer_id, at, reason)
        # 撤回只停止未来联系：未发送提醒一律取消并记录原因；已发送提醒原样保留
        cancelled = []
        for reminder in self.store.list_reminders(customer_id=customer_id):
            if reminder.status == d.REMINDER_PENDING:
                self.store.cancel_reminder(reminder.reminder_id, policy.SKIP_CONSENT_REVOKED)
                cancelled.append(reminder.reminder_id)
        self._audit(actor, "consent.revoke", "consent", f"{customer_id}:v{grant.version}",
                    {"reason": reason, "cancelled_reminders": cancelled})
        return {"customer_id": customer_id, "revoked_at": at,
                "cancelled_reminders": cancelled,
                "note": "未来联系已停止；法规要求保留的最小事实仅合规角色可查"}

    def retained_facts(self, actor: Actor, customer_id: str) -> dict:
        """撤回后法规要求保留的最小事实——只有合规角色可查看。"""
        if actor.role not in policy.RETAINED_FACT_VIEW_ROLES:
            raise BoundaryError("仅合规角色可查看撤回后保留事实")
        versions = []
        # 合规审计需要全版本，直接从审计链之外的 consents 表按版本读取
        row = self.store.latest_consent(customer_id)
        if row is not None:
            latest = row.version
            for version in range(1, latest + 1):
                grant = self.store.consent_version(customer_id, version)
                if grant:
                    versions.append({
                        "customer_id": grant.customer_id, "version": grant.version,
                        "scopes": sorted(grant.scopes),
                        "granted_at": grant.granted_at,
                        "revoked_at": grant.revoked_at,
                        "revoked_reason": grant.revoked_reason,
                    })
        self._audit(actor, "consent.retained_view", "consent", customer_id,
                    {"versions": len(versions)})
        return self._guard_output(actor.role,
                                  {"customer_id": customer_id, "retained_facts": versions})

    # ------------------------------------------------------------------
    # 2) 药师资质
    # ------------------------------------------------------------------
    def register_pharmacist(self, actor: Actor, payload: dict) -> dict:
        if actor.role not in (d.ROLE_COMPLIANCE, d.ROLE_PHARMACIST):
            raise BoundaryError("仅合规或药师本人可登记资质")
        scopes = d.normalize_set(payload.get("scopes"))
        if scopes - d.CANONICAL_SCOPES:
            raise BoundaryError("非法执业范围")
        cred = d.PharmacistCredential(
            pharmacist_id=str(payload["pharmacist_id"]), store_id=str(payload["store_id"]),
            license_no=str(payload["license_no"]), scopes=scopes,
            valid_from=str(payload["valid_from"]), valid_to=str(payload["valid_to"]),
            active=bool(payload.get("active", True)),
        )
        self.store.upsert_pharmacist(cred)
        self._audit(actor, "pharmacist.credential", "pharmacist", cred.pharmacist_id,
                    {"store_id": cred.store_id, "scopes": sorted(cred.scopes)})
        return {"pharmacist_id": cred.pharmacist_id, "store_id": cred.store_id,
                "scopes": sorted(cred.scopes), "valid_to": cred.valid_to}

    # ------------------------------------------------------------------
    # 3) 药品计划
    # ------------------------------------------------------------------
    def create_plan(self, actor: Actor, payload: dict) -> dict:
        if actor.role != d.ROLE_PHARMACIST:
            raise BoundaryError("只有药师可以维护药品计划")
        customer_id = str(payload["customer_id"])
        store_id = str(payload["store_id"])
        medications = tuple(str(m) for m in payload.get("medications", ()))
        if not medications:
            raise BoundaryError("药品计划至少包含一种药品")
        consent = self._guard_scope(
            actor, d.SCOPE_PLAN, customer_id=customer_id, store_id=store_id)
        plan = d.MedicationPlan(
            plan_id=str(payload["plan_id"]), customer_id=customer_id, store_id=store_id,
            pharmacist_id=actor.pharmacist_id or actor.actor_id, medications=medications,
            revision=1, consent_version=consent.version,
            created_at=self._now(), updated_at=self._now(),
        )
        value = self.store.add_plan(plan)
        self._audit(actor, "plan.create", "plan", plan.plan_id,
                    {"medications": list(medications), "consent_version": consent.version})
        return self._out(actor.role, self._plan_dict(value))

    def pause_plan(self, plan_id: str, actor: Actor, reason_code: str) -> None:
        self.store.update_plan_status(plan_id, d.PLAN_PAUSED, self._now())
        # 只动未发送提醒；已发送的不受影响
        for reminder in self.store.list_reminders(plan_id=plan_id):
            if reminder.status == d.REMINDER_PENDING:
                self.store.cancel_reminder(reminder.reminder_id, policy.SKIP_PLAN_PAUSED)
        self._audit(actor, "plan.pause", "plan", plan_id, {"reason": reason_code})

    def _plan_source(self, plan: d.MedicationPlan) -> dict:
        return {
            "kind": "pharmacist_created",
            "pharmacist_id": plan.pharmacist_id,
            "store_id": plan.store_id,
            "consent_version": plan.consent_version,
            "revision": plan.revision,
            "created_at": plan.created_at,
        }

    def _plan_dict(self, plan: d.MedicationPlan) -> dict:
        return {
            "plan_id": plan.plan_id, "customer_id": plan.customer_id,
            "store_id": plan.store_id, "pharmacist_id": plan.pharmacist_id,
            "medications": list(plan.medications), "revision": plan.revision,
            "consent_version": plan.consent_version, "status": plan.status,
            "plan_source": self._plan_source(plan),
        }

    # ------------------------------------------------------------------
    # 4) 提醒调度 / 到期续跑
    # ------------------------------------------------------------------
    def schedule_reminder(self, actor: Actor, payload: dict) -> dict:
        plan = self.store.get_plan(str(payload["plan_id"]))
        if plan is None:
            raise BoundaryError("药品计划不存在")
        if plan.status == d.PLAN_PAUSED:
            raise BoundaryError(policy.SKIP_PLAN_PAUSED)
        if plan.status == d.PLAN_CLOSED:
            raise BoundaryError(policy.SKIP_PLAN_CLOSED)
        if actor.role != d.ROLE_PHARMACIST:
            raise BoundaryError("只有药师可以排定提醒")
        # 交接后以当前承接门店/承接药师为准；_guard_scope 会同时校验
        # 操作者资质门店与当前承接门店一致
        current_store, _ = self._current_store_and_pharmacist(plan)
        self._guard_scope(
            actor, d.SCOPE_MED_REMINDER, customer_id=plan.customer_id,
            channel=str(payload["channel"]), store_id=current_store,
            consent_version=plan.consent_version,
        )
        if self._identifier_quarantined(plan.customer_id):
            raise BoundaryError("关联标识隔离中，暂不能新增提醒")
        reminder = d.Reminder(
            reminder_id=str(payload["reminder_id"]), plan_id=plan.plan_id,
            customer_id=plan.customer_id, medication=str(payload["medication"]),
            scheduled_at=str(payload["scheduled_at"]), channel=str(payload["channel"]),
            plan_revision=plan.revision, consent_version=plan.consent_version,
            created_at=self._now(),
        )
        value = self.store.add_reminder(reminder)
        self._audit(actor, "reminder.schedule", "reminder", value.reminder_id,
                    {"plan_id": plan.plan_id, "scheduled_at": value.scheduled_at})
        return self._out(actor.role, self._reminder_dict(value, plan))

    def run_due(self, actor: Actor = Actor("scheduler", d.ROLE_SYSTEM)) -> dict:
        """发送所有到期提醒。门店交接或进程中断后重跑本方法即可续跑。

        每条提醒发送前重新做双重覆盖判定；不满足则标记 blocked 并记录
        未提醒原因，绝不静默丢弃。
        """
        at = self._now()
        sent, blocked = [], []
        for reminder in self.store.due_reminders(at):
            plan = self.store.get_plan(reminder.plan_id)
            if plan is None:
                continue
            reason = self._dispatch_block_reason(reminder, plan, at)
            if reason:
                self.store.block_reminder(reminder.reminder_id, reason)
                blocked.append({"reminder_id": reminder.reminder_id, "skip_reason": reason})
                self._audit(actor, "reminder.block", "reminder", reminder.reminder_id,
                            {"reason": reason})
                continue
            self.store.mark_reminder_sent(reminder.reminder_id, at)
            sent.append(reminder.reminder_id)
            self._audit(actor, "reminder.send", "reminder", reminder.reminder_id,
                        {"plan_id": reminder.plan_id})
        return {"at": at, "sent": sent, "blocked": blocked}

    def _dispatch_block_reason(self, reminder: d.Reminder, plan: d.MedicationPlan,
                               at: str) -> str:
        if plan.status == d.PLAN_PAUSED:
            return policy.SKIP_PLAN_PAUSED
        if plan.status == d.PLAN_CLOSED:
            return policy.SKIP_PLAN_CLOSED
        if self._identifier_quarantined(reminder.customer_id):
            return policy.SKIP_QUARANTINE
        store_id, cred = self._current_store_and_pharmacist(plan)
        consent = self.store.latest_consent(reminder.customer_id)
        decision = policy.can_handle(
            scope=d.SCOPE_MED_REMINDER, pharmacist=cred, consent=consent, at=at,
            channel=reminder.channel, store_id=store_id,
            consent_version=reminder.consent_version,
        )
        return "" if decision else decision.reason

    def adjust_pending(self, actor: Actor, plan_id: str, changes: dict) -> dict:
        """新信息只调整未发送计划：取消旧的未发送提醒，按新内容重排。

        已发送提醒不在处理范围内，数据库层也会拒绝改写。
        """
        plan = self.store.get_plan(plan_id)
        if plan is None:
            raise BoundaryError("药品计划不存在")
        if actor.role != d.ROLE_PHARMACIST:
            raise BoundaryError("只有药师可以调整计划")
        cancelled, scheduled = [], []
        for reminder in self.store.list_reminders(plan_id=plan_id):
            if reminder.status == d.REMINDER_PENDING:
                self.store.cancel_reminder(reminder.reminder_id, "plan_adjusted")
                cancelled.append(reminder.reminder_id)
            # 已发送：跳过，绝不改写
        for item in changes.get("reminders", ()):
            payload = {"plan_id": plan_id, **item}
            scheduled.append(self.schedule_reminder(actor, payload)["reminder_id"])
        self._audit(actor, "plan.adjust", "plan", plan_id,
                    {"cancelled_pending": cancelled, "rescheduled": scheduled})
        return {"plan_id": plan_id, "cancelled_pending": cancelled,
                "rescheduled": scheduled,
                "sent_reminders_untouched": True}

    def _reminder_dict(self, reminder: d.Reminder, plan: d.MedicationPlan | None = None) -> dict:
        return {
            "reminder_id": reminder.reminder_id, "plan_id": reminder.plan_id,
            "customer_id": reminder.customer_id, "medication": reminder.medication,
            "scheduled_at": reminder.scheduled_at, "channel": reminder.channel,
            "reminder_status": reminder.status, "sent_at": reminder.sent_at,
            "skip_reason": reminder.skip_reason,
            "consent_version": reminder.consent_version,
            "plan_source": self._plan_source(plan) if plan else None,
        }

    # ------------------------------------------------------------------
    # 5) 依从反馈（幂等 + 风险规则）
    # ------------------------------------------------------------------
    def record_feedback(self, actor: Actor, payload: dict) -> dict:
        plan = self.store.get_plan(str(payload["plan_id"]))
        if plan is None:
            raise BoundaryError("药品计划不存在")
        if self._identifier_quarantined(plan.customer_id):
            raise BoundaryError("关联标识隔离中，反馈暂存待隐私复核后处理")
        if actor.role == d.ROLE_CUSTOMER and actor.actor_id != plan.customer_id:
            raise BoundaryError("顾客只能提交本人反馈")
        if actor.role == d.ROLE_PHARMACIST:
            current_store, _ = self._current_store_and_pharmacist(plan)
            self._guard_scope(actor, d.SCOPE_ADHERENCE, customer_id=plan.customer_id,
                              store_id=current_store, consent_version=plan.consent_version)
        elif actor.role != d.ROLE_CUSTOMER:
            raise BoundaryError("反馈只能由顾客本人或药师登记")

        key = str(payload["idempotency_key"])
        # 重复反馈沿用原回执
        existing = self.store.find_feedback_by_key(key)
        if existing is not None:
            self._audit(actor, "feedback.duplicate", "feedback", existing.feedback_id,
                        {"idempotency_key": key, "receipt_no": existing.receipt_no})
            return {"duplicate": True, "feedback_id": existing.feedback_id,
                    "receipt_no": existing.receipt_no,
                    "duplicate_of": existing.receipt_no}

        content_code = str(payload["content_code"])
        miss_count = sum(
            1 for f in self.store.list_feedback(plan_id=plan.plan_id)
            if f.content_code == "missed"
        ) + (1 if content_code == "missed" else 0)
        risk = policy.assess_feedback(content_code, miss_count=miss_count)

        feedback_id = "fb-" + key
        receipt_no = "rcpt-" + key
        feedback = d.AdherenceFeedback(
            feedback_id=feedback_id, customer_id=plan.customer_id, plan_id=plan.plan_id,
            content_code=content_code, detail=str(payload.get("detail", "")),
            reported_at=self._now(), idempotency_key=key, receipt_no=receipt_no,
        )
        self.store.add_feedback(feedback)
        self._audit(actor, "feedback.record", "feedback", feedback_id,
                    {"content_code": content_code, "receipt_no": receipt_no})

        escalation = {}
        if risk is not None:
            escalation = self._raise_risk(actor, plan, risk, feedback_id)

        return self._out(actor.role, {
            "duplicate": False, "feedback_id": feedback_id, "receipt_no": receipt_no,
            "content_code": content_code, "reported_at": feedback.reported_at,
            "detail": feedback.detail, **escalation,
        })

    def _raise_risk(self, actor: Actor, plan: d.MedicationPlan,
                    risk: policy.RiskAssessment, feedback_id: str) -> dict:
        """达到阈值：自动建议立即暂停并转交人工；系统不自行诊疗。"""
        signal_id = f"sig-{feedback_id}"
        status = d.SIGNAL_PAUSED if risk.should_pause else d.SIGNAL_OPEN
        signal = d.AnomalySignal(
            signal_id=signal_id, customer_id=plan.customer_id, plan_id=plan.plan_id,
            rule_code=risk.rule_code, severity=risk.severity, detail=risk.detail,
            status=status, opened_by=actor.actor_id, opened_at=self._now(),
        )
        self.store.add_signal(signal)
        referral_id = ""
        if risk.should_escalate:
            referral_id = f"ref-{signal_id}"
            referral = d.Referral(
                referral_id=referral_id, signal_id=signal_id,
                customer_id=plan.customer_id, target=risk.referral_target,
                reason_code=risk.reason_code, status=d.REFERRAL_SUGGESTED,
                suggested_by="rules-engine", created_at=self._now(),
                progress_note="规则自动建议，待药师人工确认；不构成诊断",
            )
            self.store.add_referral(referral)
        if risk.should_pause:
            self.pause_plan(plan.plan_id, Actor("rules-engine", d.ROLE_SYSTEM),
                            risk.rule_code)
        self._audit(Actor("rules-engine", d.ROLE_SYSTEM), "risk.escalate", "signal",
                    signal_id,
                    {"severity": risk.severity, "paused": risk.should_pause,
                     "referral_id": referral_id, "manual_handoff": True})
        return {
            "signal_id": signal_id, "signal_status": status, "severity": risk.severity,
            "auto_action": "suggest_pause_and_manual_handoff" if risk.should_pause
            else "manual_handoff_only",
            "referral_id": referral_id,
            "referral_status": d.REFERRAL_SUGGESTED if referral_id else "",
            "target": risk.referral_target if referral_id else "",
            "medical_boundary": "药店仅做用药陪伴与转诊建议，不能替代诊断和治疗",
        }

    # ------------------------------------------------------------------
    # 6) 异常信号关闭（销售无权）
    # ------------------------------------------------------------------
    def close_signal(self, actor: Actor, signal_id: str, note: str) -> dict:
        if not policy.can_close_signal(actor.role):
            self._audit(actor, "signal.close_denied", "signal", signal_id,
                        {"reason": "role_not_allowed"})
            raise BoundaryError("销售目标角色无权批准异常关闭")
        signal = self.store.get_signal(signal_id)
        if signal is None:
            raise BoundaryError("异常信号不存在")
        if signal.status == d.SIGNAL_CLOSED:
            raise BoundaryError("异常信号已关闭")
        if actor.role == d.ROLE_PHARMACIST:
            # 关单属于风险处置事项：药师资质与顾客授权都必须覆盖 risk_review
            plan = self.store.get_plan(signal.plan_id)
            current_store, _ = (self._current_store_and_pharmacist(plan)
                                if plan else (None, None))
            self._guard_scope(actor, d.SCOPE_RISK_REVIEW,
                              customer_id=signal.customer_id, store_id=current_store)
        self.store.update_signal(signal_id, d.SIGNAL_CLOSED, actor.actor_id, self._now(), note)
        self._audit(actor, "signal.close", "signal", signal_id, {"note": note})
        return {"signal_id": signal_id, "signal_status": d.SIGNAL_CLOSED,
                "closed_by": actor.actor_id}

    # ------------------------------------------------------------------
    # 7) 转诊进度
    # ------------------------------------------------------------------
    def update_referral(self, actor: Actor, referral_id: str, status: str,
                        note: str = "") -> dict:
        if actor.role != d.ROLE_PHARMACIST:
            raise BoundaryError("转诊进度只能由药师更新")
        referral = self.store.get_referral(referral_id)
        if referral is None:
            raise BoundaryError("转诊记录不存在")
        if status not in {d.REFERRAL_ACCEPTED, d.REFERRAL_COMPLETED, d.REFERRAL_DECLINED}:
            raise BoundaryError("非法转诊状态")
        self.store.update_referral(referral_id, status, self._now(), note)
        self._audit(actor, "referral.update", "referral", referral_id,
                    {"status": status, "note": note})
        return self._guard_output(actor.role, {
            "referral_id": referral_id, "referral_status": status,
            "progress_note": note,
            "medical_boundary": "转诊建议不构成诊断，治疗以接诊医师为准"})

    # ------------------------------------------------------------------
    # 8) 应急保供批次
    # ------------------------------------------------------------------
    def reserve_supply(self, actor: Actor, payload: dict) -> dict:
        plan = self.store.get_plan(str(payload["plan_id"]))
        if plan is None:
            raise BoundaryError("药品计划不存在")
        if actor.role != d.ROLE_PHARMACIST:
            raise BoundaryError("只有药师可以安排应急保供")
        current_store, _ = self._current_store_and_pharmacist(plan)
        responsible_store_id = str(payload.get("responsible_store_id") or current_store)
        if responsible_store_id != current_store:
            raise BoundaryError(policy.SKIP_CREDENTIAL_STORE)
        self._guard_scope(actor, d.SCOPE_SUPPLY, customer_id=plan.customer_id,
                          store_id=responsible_store_id,
                          consent_version=plan.consent_version)
        batch = d.SupplyBatch(
            batch_id=str(payload["batch_id"]), customer_id=plan.customer_id,
            plan_id=plan.plan_id, medication=str(payload["medication"]),
            quantity=int(payload["quantity"]),
            responsible_store_id=responsible_store_id,
            backup_store_id=str(payload["backup_store_id"]),
            status=d.BATCH_RESERVED, due_at=str(payload["due_at"]),
            created_at=self._now(),
        )
        value = self.store.add_supply_batch(batch)
        self._audit(actor, "supply.reserve", "supply_batch", value.batch_id,
                    {"responsible_store_id": value.responsible_store_id,
                     "backup_store_id": value.backup_store_id})
        return self._out(actor.role, self._batch_dict(value))

    def mark_supply_delivered(self, actor: Actor, batch_id: str) -> dict:
        batch = self.store.get_supply_batch(batch_id)
        if batch is None:
            raise BoundaryError("保供批次不存在")
        self.store.update_batch_status(batch_id, d.BATCH_DELIVERED, self._now())
        self._audit(actor, "supply.deliver", "supply_batch", batch_id, {})
        return {"batch_id": batch_id, "supply_status": d.BATCH_DELIVERED}

    @staticmethod
    def _batch_dict(batch: d.SupplyBatch) -> dict:
        return {
            "batch_id": batch.batch_id, "customer_id": batch.customer_id,
            "plan_id": batch.plan_id, "medication": batch.medication,
            "quantity": batch.quantity,
            "responsible_store_id": batch.responsible_store_id,
            "backup_store_id": batch.backup_store_id, "supply_status": batch.status,
            "due_at": batch.due_at, "delivered_at": batch.delivered_at,
        }

    # ------------------------------------------------------------------
    # 9) 门店交接
    # ------------------------------------------------------------------
    def initiate_handoff(self, actor: Actor, payload: dict) -> dict:
        if actor.role not in policy.HANDOFF_ROLES:
            raise BoundaryError("该角色不能发起门店交接")
        plan = self.store.get_plan(str(payload["plan_id"]))
        if plan is None:
            raise BoundaryError("药品计划不存在")
        to_store = str(payload["to_store_id"])
        to_pharmacist = self.store.get_pharmacist(str(payload["to_pharmacist_id"]))
        if to_pharmacist is None:
            raise BoundaryError("承接药师资质不存在")
        effective_at = str(payload["effective_at"])
        # 承接药师的资质必须覆盖继续服务所需事项，且属于承接门店、在有效期内
        required = {d.SCOPE_MED_REMINDER, d.SCOPE_ADHERENCE}
        open_batches = self.store.list_supply_batches(customer_id=plan.customer_id,
                                                      status=d.BATCH_RESERVED)
        if open_batches:
            required.add(d.SCOPE_SUPPLY)
        missing = {s for s in required if not to_pharmacist.covers(s, effective_at, to_store)}
        if missing:
            raise BoundaryError(f"承接药师资质不覆盖：{sorted(missing)}")
        consent = self.store.latest_consent(plan.customer_id)
        if consent is None or not consent.active:
            raise BoundaryError("授权已撤回，不能交接继续服务")
        handoff = d.StoreHandoff(
            handoff_id=str(payload["handoff_id"]), customer_id=plan.customer_id,
            plan_id=plan.plan_id, from_store_id=plan.store_id, to_store_id=to_store,
            to_pharmacist_id=to_pharmacist.pharmacist_id, initiated_by=actor.actor_id,
            status=d.HANDOFF_SCHEDULED, effective_at=effective_at,
            created_at=self._now(),
            note=str(payload.get("note", "")),
        )
        value = self.store.add_handoff(handoff)
        self._audit(actor, "handoff.initiate", "handoff", value.handoff_id,
                    {"from": value.from_store_id, "to": value.to_store_id,
                     "to_pharmacist_id": value.to_pharmacist_id})
        return self._out(actor.role, self._handoff_dict(value))

    def complete_handoff(self, actor: Actor, handoff_id: str) -> dict:
        handoff = self.store.get_handoff(handoff_id)
        if handoff is None:
            raise BoundaryError("交接记录不存在")
        if handoff.status != d.HANDOFF_SCHEDULED:
            raise BoundaryError("交接不在待完成状态")
        at = self._now()
        self.store.complete_handoff(handoff_id, at)
        # 未了结的保供责任随交接转移到承接门店
        transferred = []
        for batch in self.store.list_supply_batches(customer_id=handoff.customer_id):
            if batch.status in {d.BATCH_RESERVED, d.BATCH_IN_TRANSIT}:
                self.store.transfer_batch_responsibility(batch.batch_id, handoff.to_store_id)
                transferred.append(batch.batch_id)
        self._audit(actor, "handoff.complete", "handoff", handoff_id,
                    {"transferred_batches": transferred})
        # 到期任务无需搬运：run_due 按到期时间继续捞取，发送时按最新交接判定责任门店
        return {"handoff_id": handoff_id, "handoff_status": d.HANDOFF_COMPLETED,
                "transferred_batches": transferred, "due_tasks": "continue_on_schedule"}

    @staticmethod
    def _handoff_dict(handoff: d.StoreHandoff) -> dict:
        return {
            "handoff_id": handoff.handoff_id, "customer_id": handoff.customer_id,
            "plan_id": handoff.plan_id, "from_store_id": handoff.from_store_id,
            "to_store_id": handoff.to_store_id,
            "to_pharmacist_id": handoff.to_pharmacist_id,
            "handoff_status": handoff.status, "effective_at": handoff.effective_at,
            "note": handoff.note,
        }

    # ------------------------------------------------------------------
    # 10) 同一标识不同健康内容：先隔离，后隐私复核
    # ------------------------------------------------------------------
    def observe_identity(self, actor: Actor, identifier: str, customer_id: str,
                         health_content: str) -> dict:
        fingerprint = self._fingerprint(health_content)
        at = self._now()
        self.store.add_identity_fragment(identifier, customer_id, fingerprint, at)
        customers = self.store.customers_for_identifier(identifier)
        case_id = ""
        quarantined = False
        existing = self.store.open_quarantine_for(identifier)
        # 同一标识关联到两个不同顾客档案（即不同健康内容）→ 立即隔离
        if len(set(customers)) > 1 and existing is None:
            case = d.QuarantineCase(
                case_id=f"qc-{identifier}", identifier=identifier,
                fragment_count=len(self.store.identity_fragments(identifier)),
                status=d.QUARANTINE_QUARANTINED, created_at=at,
            )
            self.store.add_quarantine_case(case)
            case_id, quarantined = case.case_id, True
            self._audit(actor, "identity.quarantine", "quarantine_case", case.case_id,
                        {"identifier": identifier, "customers": sorted(set(customers))})
        elif existing is not None:
            case_id, quarantined = existing.case_id, True
        return {"identifier": identifier, "customer_id": customer_id,
                "quarantine_case_id": case_id, "quarantined": quarantined}

    def _identifier_quarantined(self, customer_id: str) -> bool:
        for identifier in self.store.identifiers_for_customer(customer_id):
            if self.store.open_quarantine_for(identifier) is not None:
                return True
        return False

    def review_quarantine(self, actor: Actor, case_id: str, release: bool,
                          note: str) -> dict:
        if not policy.can_review_quarantine(actor.role):
            raise BoundaryError("只有合规角色可以做隐私复核")
        case = next((c for c in self.store.list_quarantine() if c.case_id == case_id), None)
        if case is None:
            raise BoundaryError("隔离案卷不存在")
        status = d.QUARANTINE_RELEASED if release else d.QUARANTINE_BLOCKED
        self.store.resolve_quarantine(case_id, status, actor.actor_id, self._now(), note)
        self._audit(actor, "identity.review", "quarantine_case", case_id,
                    {"result": status, "note": note})
        return {"case_id": case_id, "status": status, "reviewed_by": actor.actor_id}

    # ------------------------------------------------------------------
    # 11) 三视图：顾客 / 药师 / 合规（各自权限范围内）
    # ------------------------------------------------------------------
    def _customer_plan_view(self, customer_id: str) -> dict:
        plans = [self._plan_dict(p) for p in self.store.list_plans(customer_id)]
        reminders = [
            self._reminder_dict(r, self.store.get_plan(r.plan_id))
            for r in self.store.list_reminders(customer_id=customer_id)
        ]
        referrals = []
        for sig in self.store.list_signals(customer_id=customer_id):
            referrals += [
                {"referral_id": r.referral_id, "referral_status": r.status,
                 "target": r.target, "progress_note": r.progress_note}
                for r in self.store.list_referrals(customer_id=customer_id, signal_id=sig.signal_id)
            ]
        supplies = [self._batch_dict(b)
                    for b in self.store.list_supply_batches(customer_id=customer_id)]
        handoffs = [self._handoff_dict(h)
                    for h in self.store.list_handoffs(customer_id=customer_id)]
        return {
            "customer_id": customer_id,
            "plans": plans,
            "reminders": reminders,
            "referrals": referrals,
            "supplies": supplies,
            "handoffs": handoffs,
            "medical_boundary": "药店提供用药陪伴与转诊建议，不能替代诊断和治疗",
        }

    def customer_view(self, actor: Actor, customer_id: str) -> dict:
        if actor.role != d.ROLE_CUSTOMER or actor.actor_id != customer_id:
            raise BoundaryError("顾客只能查看本人计划")
        return self._project(d.ROLE_CUSTOMER, self._customer_plan_view(customer_id))

    def pharmacist_view(self, actor: Actor, customer_id: str) -> dict:
        if actor.role != d.ROLE_PHARMACIST:
            raise BoundaryError("仅药师可调用药师视图")
        cred = self._pharmacist_for(actor)
        view = self._customer_plan_view(customer_id)
        # 药师只能看本人承接的顾客；交接完成后以承接门店/承接药师为准
        accessible = False
        for plan in self.store.list_plans(customer_id):
            store_id, responsible = self._current_store_and_pharmacist(plan)
            if store_id == cred.store_id and (
                    responsible is None or responsible.pharmacist_id == cred.pharmacist_id):
                accessible = True
                break
        if not accessible:
            raise BoundaryError("该顾客不在当前药师承接范围内")
        view["signals"] = [
            {"signal_id": s.signal_id, "plan_id": s.plan_id, "rule_code": s.rule_code,
             "severity": s.severity, "signal_status": s.status, "detail": s.detail,
             "opened_by": s.opened_by, "opened_at": s.opened_at,
             "closed_by": s.closed_by, "closure_note": s.closure_note}
            for s in self.store.list_signals(customer_id=customer_id)
        ]
        view["feedback"] = [
            {"feedback_id": f.feedback_id, "plan_id": f.plan_id,
             "content_code": f.content_code, "detail": f.detail,
             "reported_at": f.reported_at, "receipt_no": f.receipt_no}
            for f in self.store.list_feedback(customer_id=customer_id)
        ]
        return self._project(d.ROLE_PHARMACIST, view)

    def compliance_view(self, actor: Actor, customer_id: str) -> dict:
        if actor.role != d.ROLE_COMPLIANCE:
            raise BoundaryError("仅合规角色可调用合规视图")
        view = self._customer_plan_view(customer_id)
        view["signals"] = [vars(s) for s in self.store.list_signals(customer_id=customer_id)]
        view["feedback"] = [vars(f) for f in self.store.list_feedback(customer_id=customer_id)]
        # 收集该顾客的全部关联实体标识，审计链按实体归属过滤
        linked_ids = {customer_id, f"{customer_id}"}
        for p in self.store.list_plans(customer_id):
            linked_ids.update({p.plan_id, f"{customer_id}:v{p.consent_version}"})
        for r in self.store.list_reminders(customer_id=customer_id):
            linked_ids.add(r.reminder_id)
        for f in self.store.list_feedback(customer_id=customer_id):
            linked_ids.update({f.feedback_id, "fb-" + f.idempotency_key})
        for sig in self.store.list_signals(customer_id=customer_id):
            linked_ids.add(sig.signal_id)
        for ref in self.store.list_referrals(customer_id=customer_id):
            linked_ids.add(ref.referral_id)
        for b in self.store.list_supply_batches(customer_id=customer_id):
            linked_ids.add(b.batch_id)
        for h in self.store.list_handoffs(customer_id=customer_id):
            linked_ids.add(h.handoff_id)
        view["audit"] = [
            {"seq": e.seq, "at": e.at, "actor_id": e.actor_id, "actor_role": e.actor_role,
             "action": e.action, "entity_type": e.entity_type, "entity_id": e.entity_id,
             "payload": e.payload, "prev_hash": e.prev_hash, "entry_hash": e.entry_hash}
            for e in self.store.list_audit()
            if e.entity_id in linked_ids
            or self._audit_mentions_customer(e, customer_id)
        ]
        return self._project(d.ROLE_COMPLIANCE, view)

    @staticmethod
    def _audit_mentions_customer(event: d.AuditEvent, customer_id: str) -> bool:
        joined = event.entity_id + " " + str(event.payload)
        return customer_id in joined
