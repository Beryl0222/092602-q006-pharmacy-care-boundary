"""健康陪伴边界台应用服务。

一条主线贯穿全部方法：

* 药师处理任何记录前，必须同时通过"执业资质覆盖当前事项"与"顾客当前
  授权版本覆盖当前事项"两道检查；
* 风险信号达到规则阈值时，系统自动暂停未来提醒、建议转诊并转交人工，
  销售角色无权关闭异常；
* 新信息只调整尚未发送的计划任务，已发送提醒作为历史事实冻结；
* 撤回授权立即在全连锁停止未来联系，法规要求保留的最小事实仅合规可见；
* 同一标识出现不同健康内容先隔离、后隐私复核；
* 任何响应都不包含系统自行生成的诊断或治疗结论。
"""
from __future__ import annotations

import sqlite3
import uuid
from dataclasses import replace

from . import domain as d
from .domain import (
    ADJUSTABLE_TASK_STATES,
    ANOMALY_AUTO_PAUSED,
    ANOMALY_CLOSED,
    ANOMALY_CLOSER_ROLES,
    ANOMALY_ESCALATED,
    ANOMALY_OPEN,
    BOUNDARY_NOTICE,
    BATCH_ALLOCATED,
    BATCH_PLANNED,
    CONSENT_GRANTED,
    CONSENT_WITHDRAWN,
    HANDOVER_ACCEPTED,
    HANDOVER_PENDING,
    MATTER_ADHERENCE_REVIEW,
    MATTER_COUNSELING,
    MATTER_EMERGENCY_SUPPLY,
    MATTER_MEDICATION_REMINDER,
    MATTER_REFERRAL,
    QUARANTINE_HELD,
    QUARANTINE_REJECTED,
    QUARANTINE_RELEASED,
    REFERRAL_IN_PROGRESS,
    REFERRAL_SUGGESTED,
    REVIEW_FIRST_WINS,
    REVIEW_MERGE,
    REVIEW_REJECT_DUPLICATE,
    SKIP_CONSENT_WITHDRAWN,
    SKIP_HANDOVER_PENDING,
    SKIP_PLAN_ADJUSTED,
    SKIP_QUARANTINED,
    SKIP_SCOPE_NOT_COVERED,
    TASK_PAUSED,
    TASK_SCHEDULED,
    TASK_SENT,
    TASK_SKIPPED,
    Actor,
    AdherenceFeedback,
    Anomaly,
    BoundaryError,
    ConsentVersion,
    Credential,
    IdentityProfile,
    MedicationPlan,
    PermissionDenied,
    QuarantineCase,
    Referral,
    ReminderTask,
    ScopeNotCovered,
    StoreHandover,
    SupplyBatch,
    assert_no_generated_diagnosis,
    now_iso,
    risk_reached,
    risk_score,
    stable_hash,
)
from .store import Store

_PLAN_SOURCES = frozenset({"prescription", "doctor_order"})
_ACTIVE_BATCH_STATES = frozenset({BATCH_PLANNED, BATCH_ALLOCATED})


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


class Service:
    def __init__(self, store: Store | None = None) -> None:
        self.store = store or Store()

    def health(self) -> dict[str, str]:
        return {"service": "pharmacy_care_boundary", "status": "ok"}

    # ------------------------------------------------------------------
    # 兼容既有契约
    # ------------------------------------------------------------------
    def register(self, payload: dict[str, object]) -> dict[str, object]:
        required = ("record_id", "owner_id", "state")
        missing = [name for name in required if not str(payload.get(name, "")).strip()]
        if missing:
            raise ValueError("缺少必要字段：" + "、".join(missing))
        record = d.Record(
            record_id=str(payload["record_id"]),
            owner_id=str(payload["owner_id"]),
            state=str(payload["state"]),
            revision=int(payload.get("revision", 1)),
        )
        return self.store.add(record).__dict__.copy()

    def find(self, record_id: str) -> dict[str, object] | None:
        value = self.store.get(record_id)
        return value.__dict__.copy() if value else None

    # ------------------------------------------------------------------
    # 药师资质
    # ------------------------------------------------------------------
    def register_credential(self, actor: Actor, credential: Credential) -> dict[str, object]:
        self._require_role(actor, d.ROLE_COMPLIANCE)
        self.store.upsert_credential(credential, actor_id=actor.actor_id)
        return {
            "pharmacist_id": credential.pharmacist_id,
            "store_id": credential.store_id,
            "matters": list(credential.matters),
            "valid_to": credential.valid_to,
            "revoked": credential.revoked,
        }

    # ------------------------------------------------------------------
    # 顾客授权版本（授予 / 撤回均为追加新版本，永不删除历史）
    # ------------------------------------------------------------------
    def grant_consent(
        self,
        actor: Actor,
        customer_id: str,
        scopes: list[str] | tuple[str, ...],
        store_id: str,
        note: str = "",
        at: str | None = None,
    ) -> dict[str, object]:
        if actor.role not in (d.ROLE_CUSTOMER, d.ROLE_COMPLIANCE):
            raise PermissionDenied("仅顾客本人或合规角色可登记授权")
        if actor.role == d.ROLE_CUSTOMER and actor.actor_id != customer_id:
            raise PermissionDenied("不能替其他顾客授予授权")
        scopes = tuple(scopes)
        unknown = [s for s in scopes if s not in d.MATTERS]
        if unknown:
            raise BoundaryError("授权范围包含未知事项：" + "、".join(unknown))
        moment = at or now_iso()
        latest = self.store.latest_consent(customer_id)
        version = (latest.version + 1) if latest else 1
        consent = ConsentVersion(
            customer_id=customer_id,
            version=version,
            state=CONSENT_GRANTED,
            scopes=scopes,
            granted_store_id=store_id,
            created_at=moment,
            note=note,
        )
        self.store.add_consent(consent)
        return self._consent_view(consent)

    def withdraw_consent(
        self,
        actor: Actor,
        customer_id: str,
        note: str = "",
        at: str | None = None,
    ) -> dict[str, object]:
        if actor.role not in (d.ROLE_CUSTOMER, d.ROLE_COMPLIANCE):
            raise PermissionDenied("仅顾客本人或合规角色可撤回授权")
        if actor.role == d.ROLE_CUSTOMER and actor.actor_id != customer_id:
            raise PermissionDenied("不能替其他顾客撤回授权")
        moment = at or now_iso()
        latest = self.store.latest_consent(customer_id)
        version = (latest.version + 1) if latest else 1

        def mutate(conn: sqlite3.Connection) -> None:
            self.store._insert_consent_conn(
                conn,
                ConsentVersion(
                    customer_id=customer_id,
                    version=version,
                    state=CONSENT_WITHDRAWN,
                    scopes=(),
                    granted_store_id=actor.store_id or (latest.granted_store_id if latest else ""),
                    created_at=moment,
                    note=note,
                ),
            )
            # 撤回立即在全连锁停止未来联系：所有未发送任务取消。
            for task in self.store.list_tasks(
                customer_id=customer_id, states=(TASK_SCHEDULED, TASK_PAUSED)
            ):
                self.store._save_task_conn(
                    conn,
                    replace(
                        task,
                        state=TASK_SKIPPED,
                        skip_reason=SKIP_CONSENT_WITHDRAWN,
                        revision=task.revision + 1,
                    ),
                )

        self.store.eventful(
            actor.actor_id,
            "consent.withdraw",
            {
                "customer_id": customer_id,
                "version": version,
                "state": CONSENT_WITHDRAWN,
                "future_contact_stopped": True,
            },
            mutate,
        )
        saved = self.store.latest_consent(customer_id)
        return self._consent_view(saved)

    @staticmethod
    def _consent_view(consent: ConsentVersion) -> dict[str, object]:
        return {
            "customer_id": consent.customer_id,
            "version": consent.version,
            "state": consent.state,
            "scopes": list(consent.scopes),
            "granted_store_id": consent.granted_store_id,
            "created_at": consent.created_at,
        }

    # ------------------------------------------------------------------
    # 覆盖判定：资质 AND 当前授权版本，两者都覆盖当前事项
    # ------------------------------------------------------------------
    def _active_credential(
        self, pharmacist_id: str, matter: str, at: str
    ) -> Credential | None:
        credential = self.store.get_credential(pharmacist_id)
        if credential is None or credential.revoked:
            return None
        if not (credential.valid_from <= at <= credential.valid_to):
            return None
        if matter not in credential.matters:
            return None
        return credential

    def _cover(
        self, pharmacist_id: str, customer_id: str, matter: str, at: str
    ) -> tuple[Credential | None, ConsentVersion | None]:
        credential = self._active_credential(pharmacist_id, matter, at)
        consent = self.store.latest_consent(customer_id)
        consent_ok = (
            consent is not None
            and consent.state == CONSENT_GRANTED
            and matter in consent.scopes
            and consent.created_at <= at
        )
        return (credential if credential else None), (consent if consent_ok else None)

    def _assert_covered(
        self, actor: Actor, customer_id: str, matter: str, at: str
    ) -> tuple[Credential, ConsentVersion]:
        self._require_role(actor, d.ROLE_PHARMACIST)
        if not actor.pharmacist_id:
            raise ScopeNotCovered("发起方缺少药师身份")
        credential, consent = self._cover(
            actor.pharmacist_id, customer_id, matter, at
        )
        if credential is None:
            raise ScopeNotCovered(
                f"药师 {actor.pharmacist_id} 的执业资质不覆盖事项 {matter}"
            )
        if consent is None:
            current = self.store.latest_consent(customer_id)
            if current is not None and current.state == CONSENT_WITHDRAWN:
                raise ScopeNotCovered("顾客已撤回授权，不得处理该记录")
            raise ScopeNotCovered(
                f"顾客当前授权版本不覆盖事项 {matter}"
            )
        return credential, consent

    @staticmethod
    def _require_role(actor: Actor, role: str) -> None:
        if actor.role != role:
            raise PermissionDenied(f"该操作仅面向角色 {role}")

    def _covering_pharmacist_at(
        self, store_id: str, customer_id: str, matter: str, at: str
    ) -> Credential | None:
        """为系统调度寻找门店内"资质+授权"双覆盖的药师。"""
        consent = self.store.latest_consent(customer_id)
        if consent is None or consent.state != CONSENT_GRANTED or matter not in consent.scopes:
            return None
        rows = self.store.connection.execute(
            "SELECT pharmacist_id FROM credentials WHERE store_id=? AND revoked=0",
            (store_id,),
        ).fetchall()
        for row in rows:
            credential = self._active_credential(row["pharmacist_id"], matter, at)
            if credential is not None:
                return credential
        return None

    # ------------------------------------------------------------------
    # 药品计划：来源仅限处方/医嘱，系统不自创
    # ------------------------------------------------------------------
    def import_plan(
        self,
        actor: Actor,
        customer_id: str,
        source_kind: str,
        source_ref: str,
        at: str | None = None,
        plan_id: str | None = None,
    ) -> dict[str, object]:
        moment = at or now_iso()
        self._assert_covered(actor, customer_id, MATTER_MEDICATION_REMINDER, moment)
        if source_kind not in _PLAN_SOURCES or not source_ref.strip():
            raise BoundaryError("药品计划必须来自处方(prescription)或医嘱(doctor_order)")
        plan = MedicationPlan(
            plan_id=plan_id or _new_id("plan"),
            customer_id=customer_id,
            source_kind=source_kind,
            source_ref=source_ref.strip(),
            created_at=moment,
        )

        def mutate(conn: sqlite3.Connection) -> None:
            self.store.add_plan_conn(conn, plan)

        self.store.eventful(
            actor.actor_id,
            "plan.import",
            {
                "customer_id": customer_id,
                "plan_id": plan.plan_id,
                "source_kind": plan.source_kind,
                "source_ref": plan.source_ref,
            },
            mutate,
        )
        return {
            "plan_id": plan.plan_id,
            "customer_id": plan.customer_id,
            "source_kind": plan.source_kind,
            "source_ref": plan.source_ref,
        }

    def schedule_task(
        self,
        actor: Actor,
        customer_id: str,
        plan_id: str,
        matter: str,
        due_at: str,
        store_id: str = "",
        task_id: str | None = None,
    ) -> dict[str, object]:
        """排期一条未来提醒。仅核对事项合法性；发送时再做双覆盖判定。"""
        if actor.role not in (d.ROLE_PHARMACIST, d.ROLE_COMPLIANCE):
            raise PermissionDenied("仅药师或合规角色可排期提醒")
        if matter not in d.MATTERS:
            raise BoundaryError(f"未知事项：{matter}")
        plan = self.store.get_plan(plan_id)
        if plan is None or plan.customer_id != customer_id:
            raise BoundaryError("药品计划不存在或不属于该顾客")
        task = ReminderTask(
            task_id=task_id or _new_id("task"),
            plan_id=plan_id,
            customer_id=customer_id,
            matter=matter,
            due_at=due_at,
            state=TASK_SCHEDULED,
            store_id=store_id or actor.store_id,
        )

        def mutate(conn: sqlite3.Connection) -> None:
            self.store._insert_task_conn(conn, task)

        self.store.eventful(
            actor.actor_id,
            "task.schedule",
            {
                "customer_id": customer_id,
                "task_id": task.task_id,
                "plan_id": plan_id,
                "matter": matter,
                "due_at": due_at,
                "store_id": task.store_id,
            },
            mutate,
        )
        return self._task_view(task)

    # ------------------------------------------------------------------
    # 新信息只调整未发送计划：已发送任务一律拒绝改写
    # ------------------------------------------------------------------
    def apply_plan_update(
        self,
        actor: Actor,
        plan_id: str,
        new_tasks: list[dict[str, object]],
        at: str | None = None,
    ) -> dict[str, object]:
        moment = at or now_iso()
        plan = self.store.get_plan(plan_id)
        if plan is None:
            raise BoundaryError("药品计划不存在")
        self._assert_covered(actor, plan.customer_id, MATTER_MEDICATION_REMINDER, moment)
        created: list[str] = []
        adjusted: list[str] = []
        frozen: list[str] = []

        def mutate(conn: sqlite3.Connection) -> None:
            for task in self.store.list_tasks(customer_id=plan.customer_id):
                if task.plan_id != plan_id:
                    continue
                if task.state == TASK_SENT:
                    # 已发送提醒是历史事实：原样保留，绝不改写。
                    frozen.append(task.task_id)
                    continue
                if task.state in ADJUSTABLE_TASK_STATES:
                    adjusted.append(task.task_id)
                    self.store._save_task_conn(
                        conn,
                        replace(
                            task,
                            state=TASK_SKIPPED,
                            skip_reason=SKIP_PLAN_ADJUSTED,
                            revision=task.revision + 1,
                        ),
                    )
            for item in new_tasks:
                task = ReminderTask(
                    task_id=str(item.get("task_id") or _new_id("task")),
                    plan_id=plan_id,
                    customer_id=plan.customer_id,
                    matter=str(item["matter"]),
                    due_at=str(item["due_at"]),
                    state=TASK_SCHEDULED,
                    store_id=str(item.get("store_id") or actor.store_id),
                )
                if task.matter not in d.MATTERS:
                    raise BoundaryError(f"未知事项：{task.matter}")
                created.append(task.task_id)
                self.store._insert_task_conn(conn, task)

        self.store.eventful(
            actor.actor_id,
            "plan.update",
            {
                "customer_id": plan.customer_id,
                "plan_id": plan_id,
                "adjusted_unsent_tasks": adjusted,
                "scheduled_new_tasks": created,
                "sent_tasks_untouched": frozen,
            },
            mutate,
        )
        return {
            "plan_id": plan_id,
            "adjusted": adjusted,
            "scheduled": created,
            "sent_untouched": frozen,
        }

    # ------------------------------------------------------------------
    # 到期处理：系统按双覆盖逐任务决定 发送 / 不发送(及原因)
    # ------------------------------------------------------------------
    def run_due(self, actor: Actor, at: str) -> list[dict[str, object]]:
        if actor.role not in (d.ROLE_PHARMACIST, d.ROLE_COMPLIANCE):
            raise PermissionDenied("仅药师或合规角色可运行到期处理")
        results: list[dict[str, object]] = []
        due = self.store.list_tasks(states=(TASK_SCHEDULED,), due_at_or_before=at)
        for task in due:
            results.append(self._dispatch(task, actor, at))
        return results

    def _dispatch(self, task: ReminderTask, actor: Actor, at: str) -> dict[str, object]:
        consent = self.store.latest_consent(task.customer_id)

        # 1) 撤回：全连锁停发
        if consent is None or consent.state == CONSENT_WITHDRAWN:
            return self._skip_task(task, SKIP_CONSENT_WITHDRAWN, actor)

        # 2) 同标识异内容隔离中
        if self.store.list_open_quarantine(task.customer_id):
            return self._skip_task(task, SKIP_QUARANTINED, actor)

        # 3) 门店交接尚未承接：本店不得继续发出
        if task.store_id and self.store.pending_handover_for_store(
            task.customer_id, task.store_id
        ):
            return self._skip_task(task, SKIP_HANDOVER_PENDING, actor)

        # 4) 资质 × 授权双覆盖
        credential = self._covering_pharmacist_at(
            task.store_id, task.customer_id, task.matter, at
        )
        if credential is None:
            return self._skip_task(task, SKIP_SCOPE_NOT_COVERED, actor)

        # 5) 达阈值风险的顾客，任务已在信号登记时暂停，不会走到这里；
        #    兜底再查一次未关闭异常。
        if self._has_open_risk(task.customer_id):
            return self._pause_task(task, actor)

        return self._send_task(task, credential.pharmacist_id, actor)

    def _send_task(
        self, task: ReminderTask, pharmacist_id: str, actor: Actor
    ) -> dict[str, object]:
        plan = self.store.get_plan(task.plan_id)
        message = self._reminder_message(task, plan)
        assert_no_generated_diagnosis(message)
        sent = replace(
            task,
            state=TASK_SENT,
            pharmacist_id=pharmacist_id,
            sent_at=now_iso(),
            revision=task.revision + 1,
        )

        def mutate(conn: sqlite3.Connection) -> None:
            self.store._save_task_conn(conn, sent)

        self.store.eventful(
            actor.actor_id,
            "task.send",
            {
                "customer_id": task.customer_id,
                "task_id": task.task_id,
                "plan_id": task.plan_id,
                "matter": task.matter,
                "store_id": task.store_id,
                "handled_by": pharmacist_id,
            },
            mutate,
        )
        return {
            "task_id": task.task_id,
            "customer_id": task.customer_id,
            "decision": TASK_SENT,
            "handled_by": pharmacist_id,
            "message": message,
        }

    def _skip_task(self, task: ReminderTask, reason: str, actor: Actor) -> dict[str, object]:
        skipped = replace(
            task,
            state=TASK_SKIPPED,
            skip_reason=reason,
            revision=task.revision + 1,
        )

        def mutate(conn: sqlite3.Connection) -> None:
            self.store._save_task_conn(conn, skipped)

        self.store.eventful(
            actor.actor_id,
            "task.skip",
            {
                "customer_id": task.customer_id,
                "task_id": task.task_id,
                "matter": task.matter,
                "store_id": task.store_id,
                "reason": reason,
            },
            mutate,
        )
        return {
            "task_id": task.task_id,
            "customer_id": task.customer_id,
            "decision": TASK_SKIPPED,
            "reason": reason,
        }

    def _pause_task(self, task: ReminderTask, actor: Actor) -> dict[str, object]:
        paused = replace(task, state=TASK_PAUSED, revision=task.revision + 1)

        def mutate(conn: sqlite3.Connection) -> None:
            self.store._save_task_conn(conn, paused)

        self.store.eventful(
            actor.actor_id,
            "task.auto_pause",
            {
                "customer_id": task.customer_id,
                "task_id": task.task_id,
                "reason": "risk_threshold",
                "handed_to_human": True,
            },
            mutate,
        )
        return {
            "task_id": task.task_id,
            "customer_id": task.customer_id,
            "decision": TASK_PAUSED,
            "reason": "risk_threshold",
        }

    @staticmethod
    def _reminder_message(task: ReminderTask, plan: MedicationPlan | None) -> str:
        source = plan.source_ref if plan else "未知计划"
        if task.matter == MATTER_MEDICATION_REMINDER:
            body = f"用药提醒：您的药品计划（来源编号 {source}）在 {task.due_at} 到期，请按医嘱用药。"
        elif task.matter == MATTER_ADHERENCE_REVIEW:
            body = f"依从回访：药品计划 {source} 的例行回访时间为 {task.due_at}，请告知近期用药情况。"
        elif task.matter == MATTER_EMERGENCY_SUPPLY:
            body = f"应急保供提醒：与计划 {source} 相关的保供安排在 {task.due_at} 有更新，请留意门店通知。"
        elif task.matter == MATTER_REFERRAL:
            body = f"转诊协助提醒：您在 {task.due_at} 有一项转诊协助进度可查询。"
        else:
            body = f"用药咨询提醒：您预约的 {task.due_at} 用药咨询即将开始。"
        return body + BOUNDARY_NOTICE

    # ------------------------------------------------------------------
    # 依从反馈：重复反馈沿用原回执；可附风险信号
    # ------------------------------------------------------------------
    def record_feedback(
        self,
        actor: Actor,
        customer_id: str,
        plan_id: str,
        content: str,
        idempotency_key: str,
        signals: list[str] | tuple[str, ...] = (),
        at: str | None = None,
    ) -> dict[str, object]:
        moment = at or now_iso()
        self._assert_covered(actor, customer_id, MATTER_ADHERENCE_REVIEW, moment)
        # 反馈原文允许引述顾客自述或既有医嘱；系统不会据此补全任何结论。
        existing = self.store.get_feedback_by_key(idempotency_key)
        if existing is not None:
            # 重复反馈沿用原回执，不产生第二条记录。
            return {
                "deduped": True,
                "receipt": {
                    "feedback_id": existing.feedback_id,
                    "received_at": existing.received_at,
                },
            }

        feedback = AdherenceFeedback(
            feedback_id=_new_id("feedback"),
            customer_id=customer_id,
            plan_id=plan_id,
            content=content,
            received_at=moment,
            idempotency_key=idempotency_key,
        )
        score = risk_score(list(signals))
        triage = score >= d.RISK_THRESHOLD
        anomaly_id = _new_id("anomaly") if triage else ""
        referral_id = _new_id("referral") if triage else ""

        def mutate(conn: sqlite3.Connection) -> None:
            self.store._insert_feedback_conn(conn, feedback)
            if triage:
                self._write_triage(conn, customer_id, list(signals), score,
                                   anomaly_id, referral_id, actor.actor_id, moment)

        self.store.eventful(
            actor.actor_id,
            "feedback.record",
            {
                "customer_id": customer_id,
                "plan_id": plan_id,
                "feedback_id": feedback.feedback_id,
                "idempotency_key": idempotency_key,
                "risk_score": score,
                "auto_pause_and_escalate": triage,
                "anomaly_id": anomaly_id,
                "referral_id": referral_id,
            },
            mutate,
        )
        result: dict[str, object] = {
            "deduped": False,
            "receipt": {
                "feedback_id": feedback.feedback_id,
                "received_at": feedback.received_at,
            },
            "risk_score": score,
        }
        if triage:
            result["action"] = "auto_pause_and_escalate"
            result["anomaly_id"] = anomaly_id
            result["referral_id"] = referral_id
            result["advice"] = "已达风险阈值：立即暂停未来提醒并转交人工，建议尽快线下就医。"
        return result

    # ------------------------------------------------------------------
    # 风险信号登记 → 达阈值自动暂停 + 建议转诊 + 转人工
    # ------------------------------------------------------------------
    def report_signals(
        self,
        actor: Actor,
        customer_id: str,
        signals: list[str] | tuple[str, ...],
        at: str | None = None,
    ) -> dict[str, object]:
        moment = at or now_iso()
        self._assert_covered(actor, customer_id, MATTER_COUNSELING, moment)
        score = risk_score(list(signals))
        if not risk_reached(list(signals)):
            anomaly_id = _new_id("anomaly")
            anomaly = Anomaly(
                anomaly_id=anomaly_id,
                customer_id=customer_id,
                signals=tuple(signals),
                score=score,
                state=ANOMALY_OPEN,
                opened_by=actor.actor_id,
                opened_at=moment,
            )

            def mutate_low(conn: sqlite3.Connection) -> None:
                self.store._insert_anomaly_conn(conn, anomaly)

            self.store.eventful(
                actor.actor_id,
                "anomaly.open",
                {
                    "customer_id": customer_id,
                    "anomaly_id": anomaly_id,
                    "signals": list(signals),
                    "score": score,
                    "threshold": d.RISK_THRESHOLD,
                },
                mutate_low,
            )
            return {"anomaly_id": anomaly_id, "state": ANOMALY_OPEN, "score": score}

        anomaly_id = _new_id("anomaly")
        referral_id = _new_id("referral")

        def mutate(conn: sqlite3.Connection) -> None:
            self._write_triage(conn, customer_id, list(signals), score,
                               anomaly_id, referral_id, actor.actor_id, moment)

        self.store.eventful(
            actor.actor_id,
            "anomaly.triage",
            {
                "customer_id": customer_id,
                "anomaly_id": anomaly_id,
                "referral_id": referral_id,
                "signals": list(signals),
                "score": score,
                "threshold": d.RISK_THRESHOLD,
                "auto_pause": True,
                "handed_to_human": True,
            },
            mutate,
        )
        return {
            "anomaly_id": anomaly_id,
            "referral_id": referral_id,
            "state": ANOMALY_AUTO_PAUSED,
            "score": score,
            "action": "auto_pause_and_escalate",
            "advice": "已达风险阈值：立即暂停未来提醒并转交人工，建议尽快线下就医。",
        }

    def _write_triage(
        self,
        conn: sqlite3.Connection,
        customer_id: str,
        signals: list[str],
        score: int,
        anomaly_id: str,
        referral_id: str,
        actor_id: str,
        moment: str,
    ) -> None:
        self.store._insert_anomaly_conn(
            conn,
            Anomaly(
                anomaly_id=anomaly_id,
                customer_id=customer_id,
                signals=tuple(signals),
                score=score,
                state=ANOMALY_AUTO_PAUSED,
                opened_by=actor_id,
                opened_at=moment,
            ),
        )
        self.store._insert_referral_conn(
            conn,
            Referral(
                referral_id=referral_id,
                customer_id=customer_id,
                anomaly_id=anomaly_id,
                target_kind="medical_institution",
                status=REFERRAL_SUGGESTED,
                created_at=moment,
            ),
        )
        # 仅暂停"未发送"任务；已发送历史不动。
        for task in self.store.list_tasks(
            customer_id=customer_id, states=(TASK_SCHEDULED,)
        ):
            self.store._save_task_conn(
                conn, replace(task, state=TASK_PAUSED, revision=task.revision + 1)
            )

    def _has_open_risk(self, customer_id: str) -> bool:
        rows = self.store.connection.execute(
            "SELECT 1 FROM anomalies WHERE customer_id=? AND state IN (?,?) LIMIT 1",
            (customer_id, ANOMALY_OPEN, ANOMALY_AUTO_PAUSED),
        ).fetchall()
        return bool(rows)

    def human_takeover(self, actor: Actor, anomaly_id: str, at: str | None = None) -> dict[str, object]:
        """人工接管：双覆盖药师或合规可执行。"""
        moment = at or now_iso()
        anomaly = self.store.get_anomaly(anomaly_id)
        if anomaly is None:
            raise BoundaryError("异常不存在")
        if actor.role == d.ROLE_PHARMACIST:
            self._assert_covered(actor, anomaly.customer_id, MATTER_COUNSELING, moment)
        elif actor.role != d.ROLE_COMPLIANCE:
            raise PermissionDenied("仅药师或合规角色可接管异常")
        if anomaly.state not in (ANOMALY_OPEN, ANOMALY_AUTO_PAUSED):
            return {"anomaly_id": anomaly_id, "state": anomaly.state}
        updated = replace(anomaly, state=ANOMALY_ESCALATED)

        def mutate(conn: sqlite3.Connection) -> None:
            self.store._save_anomaly_conn(conn, updated)

        self.store.eventful(
            actor.actor_id,
            "anomaly.takeover",
            {"customer_id": anomaly.customer_id, "anomaly_id": anomaly_id},
            mutate,
        )
        return {"anomaly_id": anomaly_id, "state": ANOMALY_ESCALATED}

    def resume_tasks(self, actor: Actor, customer_id: str, at: str | None = None) -> dict[str, object]:
        """人工处置后恢复未来提醒；已暂停任务回到未发送队列。"""
        moment = at or now_iso()
        if actor.role == d.ROLE_PHARMACIST:
            self._assert_covered(actor, customer_id, MATTER_COUNSELING, moment)
        elif actor.role != d.ROLE_COMPLIANCE:
            raise PermissionDenied("仅药师或合规角色可恢复提醒")
        resumed: list[str] = []

        def mutate(conn: sqlite3.Connection) -> None:
            for task in self.store.list_tasks(
                customer_id=customer_id, states=(TASK_PAUSED,)
            ):
                resumed.append(task.task_id)
                self.store._save_task_conn(
                    conn, replace(task, state=TASK_SCHEDULED, revision=task.revision + 1)
                )

        self.store.eventful(
            actor.actor_id,
            "task.resume",
            {"customer_id": customer_id, "tasks": resumed},
            mutate,
        )
        return {"customer_id": customer_id, "resumed": resumed}

    def close_anomaly(
        self, actor: Actor, anomaly_id: str, note: str, at: str | None = None
    ) -> dict[str, object]:
        """关闭异常：合规角色专属。销售目标负责人明确无权。"""
        moment = at or now_iso()
        if actor.role == d.ROLE_SALES:
            raise PermissionDenied("销售目标负责人无权批准异常关闭")
        if actor.role not in ANOMALY_CLOSER_ROLES:
            raise PermissionDenied("仅合规角色可关闭异常")
        anomaly = self.store.get_anomaly(anomaly_id)
        if anomaly is None:
            raise BoundaryError("异常不存在")
        closed = replace(
            anomaly,
            state=ANOMALY_CLOSED,
            closed_by=actor.actor_id,
            closed_at=moment,
            closure_note=note,
        )

        def mutate(conn: sqlite3.Connection) -> None:
            self.store._save_anomaly_conn(conn, closed)

        self.store.eventful(
            actor.actor_id,
            "anomaly.close",
            {
                "customer_id": anomaly.customer_id,
                "anomaly_id": anomaly_id,
                "note": note,
            },
            mutate,
        )
        return {"anomaly_id": anomaly_id, "state": ANOMALY_CLOSED, "closed_by": actor.actor_id}

    # ------------------------------------------------------------------
    # 转诊进度
    # ------------------------------------------------------------------
    def update_referral(
        self, actor: Actor, referral_id: str, status: str, at: str | None = None
    ) -> dict[str, object]:
        moment = at or now_iso()
        referral = self.store.get_referral(referral_id)
        if referral is None:
            raise BoundaryError("转诊记录不存在")
        if status not in (
            REFERRAL_SUGGESTED,
            d.REFERRAL_ACCEPTED,
            REFERRAL_IN_PROGRESS,
            d.REFERRAL_DONE,
            d.REFERRAL_DECLINED,
        ):
            raise BoundaryError(f"未知转诊状态：{status}")
        if actor.role == d.ROLE_PHARMACIST:
            self._assert_covered(actor, referral.customer_id, MATTER_REFERRAL, moment)
        elif actor.role != d.ROLE_COMPLIANCE:
            raise PermissionDenied("仅药师或合规角色可更新转诊进度")
        updated = replace(referral, status=status, updated_at=moment)

        def mutate(conn: sqlite3.Connection) -> None:
            self.store._save_referral_conn(conn, updated)

        self.store.eventful(
            actor.actor_id,
            "referral.update",
            {
                "customer_id": referral.customer_id,
                "referral_id": referral_id,
                "status": status,
            },
            mutate,
        )
        return {"referral_id": referral_id, "status": status}

    # ------------------------------------------------------------------
    # 应急保供批次：责任随门店交接转移
    # ------------------------------------------------------------------
    def create_batch(
        self,
        actor: Actor,
        customer_id: str,
        plan_id: str,
        owner_store_id: str = "",
        at: str | None = None,
    ) -> dict[str, object]:
        moment = at or now_iso()
        self._assert_covered(actor, customer_id, MATTER_EMERGENCY_SUPPLY, moment)
        if self.store.get_plan(plan_id) is None:
            raise BoundaryError("药品计划不存在")
        batch = SupplyBatch(
            batch_id=_new_id("batch"),
            customer_id=customer_id,
            plan_id=plan_id,
            status=BATCH_PLANNED,
            owner_store_id=owner_store_id or actor.store_id,
            created_at=moment,
        )

        def mutate(conn: sqlite3.Connection) -> None:
            self.store._insert_batch_conn(conn, batch)

        self.store.eventful(
            actor.actor_id,
            "batch.create",
            {
                "customer_id": customer_id,
                "batch_id": batch.batch_id,
                "plan_id": plan_id,
                "owner_store_id": batch.owner_store_id,
                "status": BATCH_PLANNED,
            },
            mutate,
        )
        return {
            "batch_id": batch.batch_id,
            "owner_store_id": batch.owner_store_id,
            "status": batch.status,
        }

    def update_batch_status(
        self, actor: Actor, batch_id: str, status: str, at: str | None = None
    ) -> dict[str, object]:
        moment = at or now_iso()
        batch = self.store.get_batch(batch_id)
        if batch is None:
            raise BoundaryError("保供批次不存在")
        if status not in (BATCH_PLANNED, BATCH_ALLOCATED, d.BATCH_DISPATCHED, d.BATCH_DELIVERED):
            raise BoundaryError(f"未知保供批次状态：{status}")
        if actor.role == d.ROLE_PHARMACIST:
            self._assert_covered(actor, batch.customer_id, MATTER_EMERGENCY_SUPPLY, moment)
            if credential := self.store.get_credential(actor.pharmacist_id or ""):
                if credential.store_id != batch.owner_store_id:
                    raise ScopeNotCovered("保供责任属于其他门店")
        elif actor.role != d.ROLE_COMPLIANCE:
            raise PermissionDenied("仅责任药师或合规角色可更新保供批次")
        updated = replace(batch, status=status, updated_at=moment)

        def mutate(conn: sqlite3.Connection) -> None:
            self.store._save_batch_conn(conn, updated)

        self.store.eventful(
            actor.actor_id,
            "batch.update",
            {
                "customer_id": batch.customer_id,
                "batch_id": batch_id,
                "status": status,
                "owner_store_id": updated.owner_store_id,
            },
            mutate,
        )
        return {"batch_id": batch_id, "status": status, "owner_store_id": updated.owner_store_id}

    # ------------------------------------------------------------------
    # 门店交接：承接后到期任务在新门店继续，保供责任随之转移
    # ------------------------------------------------------------------
    def open_handover(
        self,
        actor: Actor,
        customer_id: str,
        to_store_id: str,
        at: str | None = None,
    ) -> dict[str, object]:
        if actor.role not in (d.ROLE_PHARMACIST, d.ROLE_COMPLIANCE):
            raise PermissionDenied("仅药师或合规角色可发起交接")
        if not actor.store_id:
            raise BoundaryError("发起交接必须带出门店")
        moment = at or now_iso()
        handover = StoreHandover(
            handover_id=_new_id("handover"),
            customer_id=customer_id,
            from_store_id=actor.store_id,
            to_store_id=to_store_id,
            status=HANDOVER_PENDING,
            created_at=moment,
        )

        def mutate(conn: sqlite3.Connection) -> None:
            self.store._insert_handover_conn(conn, handover)

        self.store.eventful(
            actor.actor_id,
            "handover.open",
            {
                "customer_id": customer_id,
                "handover_id": handover.handover_id,
                "from_store_id": actor.store_id,
                "to_store_id": to_store_id,
            },
            mutate,
        )
        return {"handover_id": handover.handover_id, "status": HANDOVER_PENDING}

    def accept_handover(
        self, actor: Actor, handover_id: str, at: str | None = None
    ) -> dict[str, object]:
        moment = at or now_iso()
        handover = self.store.get_handover(handover_id)
        if handover is None:
            raise BoundaryError("交接不存在")
        if actor.role not in (d.ROLE_PHARMACIST, d.ROLE_COMPLIANCE):
            raise PermissionDenied("仅药师或合规角色可承接交接")
        if actor.role == d.ROLE_PHARMACIST and actor.store_id != handover.to_store_id:
            raise ScopeNotCovered("只有目标门店可承接交接")
        continued: list[str] = []
        moved_batches: list[str] = []

        def mutate(conn: sqlite3.Connection) -> None:
            self.store._save_handover_conn(
                conn, replace(handover, status=HANDOVER_ACCEPTED, accepted_at=moment)
            )
            # 未发送任务（含因交接等待而跳过的到期任务）转到新门店续跑。
            candidates = self.store.list_tasks(customer_id=handover.customer_id)
            for task in candidates:
                if task.store_id != handover.from_store_id:
                    continue
                if task.state in (TASK_SCHEDULED, TASK_SKIPPED) and (
                    task.state == TASK_SCHEDULED
                    or task.skip_reason == SKIP_HANDOVER_PENDING
                ):
                    continued.append(task.task_id)
                    self.store._save_task_conn(
                        conn,
                        replace(
                            task,
                            state=TASK_SCHEDULED,
                            store_id=handover.to_store_id,
                            pharmacist_id="",
                            skip_reason="",
                            revision=task.revision + 1,
                        ),
                    )
            # 在途保供批次的责任归属一并转移。
            for batch in self.store.list_batches(handover.customer_id):
                if (
                    batch.owner_store_id == handover.from_store_id
                    and batch.status in _ACTIVE_BATCH_STATES
                ):
                    moved_batches.append(batch.batch_id)
                    self.store._save_batch_conn(
                        conn, replace(batch, owner_store_id=handover.to_store_id,
                                      updated_at=moment)
                    )

        self.store.eventful(
            actor.actor_id,
            "handover.accept",
            {
                "customer_id": handover.customer_id,
                "handover_id": handover_id,
                "from_store_id": handover.from_store_id,
                "to_store_id": handover.to_store_id,
                "continued_tasks": continued,
                "moved_batches": moved_batches,
            },
            mutate,
        )
        return {
            "handover_id": handover_id,
            "status": HANDOVER_ACCEPTED,
            "continued_tasks": continued,
            "moved_batches": moved_batches,
        }

    # ------------------------------------------------------------------
    # 同标识异内容：先隔离，再由合规隐私复核
    # ------------------------------------------------------------------
    def ingest_health_content(
        self,
        actor: Actor,
        customer_id: str,
        content: str,
        source_store_id: str = "",
        at: str | None = None,
    ) -> dict[str, object]:
        if actor.role not in (d.ROLE_PHARMACIST, d.ROLE_COMPLIANCE):
            raise PermissionDenied("仅药师或合规角色可登记健康内容")
        moment = at or now_iso()
        preview = content.strip()[:80]
        digest = stable_hash({"customer_id": customer_id, "content": content.strip()})
        profile = self.store.get_profile(customer_id)
        if profile is not None and profile.first_content_hash != digest:
            case = QuarantineCase(
                case_id=_new_id("quarantine"),
                customer_id=customer_id,
                incoming_hash=digest,
                incoming_preview=preview,
                status=QUARANTINE_HELD,
                opened_at=moment,
            )

            def mutate_q(conn: sqlite3.Connection) -> None:
                self.store._insert_quarantine_conn(conn, case)
                # 隔离期间暂停该顾客未发送提醒，等待复核结论。
                for task in self.store.list_tasks(
                    customer_id=customer_id, states=(TASK_SCHEDULED,)
                ):
                    self.store._save_task_conn(
                        conn,
                        replace(task, state=TASK_PAUSED, revision=task.revision + 1),
                    )

            self.store.eventful(
                actor.actor_id,
                "privacy.quarantine",
                {
                    "customer_id": customer_id,
                    "case_id": case.case_id,
                    "source_store_id": source_store_id or actor.store_id,
                    "reason": "same_identifier_different_content",
                },
                mutate_q,
            )
            return {
                "quarantined": True,
                "case_id": case.case_id,
                "state": QUARANTINE_HELD,
            }

        if profile is None:
            new_profile = IdentityProfile(
                customer_id=customer_id,
                first_content_hash=digest,
                first_content_preview=preview,
                created_at=moment,
            )

            def mutate_p(conn: sqlite3.Connection) -> None:
                self.store._insert_profile_conn(conn, new_profile)

            self.store.eventful(
                actor.actor_id,
                "privacy.profile_create",
                {"customer_id": customer_id, "source_store_id": source_store_id or actor.store_id},
                mutate_p,
            )
            return {"quarantined": False, "profile": "created"}

        return {"quarantined": False, "profile": "unchanged"}

    def review_quarantine(
        self, actor: Actor, case_id: str, decision: str, at: str | None = None
    ) -> dict[str, object]:
        self._require_role(actor, d.ROLE_COMPLIANCE)
        if decision not in (REVIEW_FIRST_WINS, REVIEW_MERGE, REVIEW_REJECT_DUPLICATE):
            raise BoundaryError(f"未知复核结论：{decision}")
        case = self.store.get_quarantine(case_id)
        if case is None:
            raise BoundaryError("隔离记录不存在")
        if case.status != QUARANTINE_HELD:
            return {"case_id": case_id, "state": case.status, "decision": case.decision}
        moment = at or now_iso()
        new_state = QUARANTINE_RELEASED if decision == REVIEW_MERGE else QUARANTINE_REJECTED
        resumed: list[str] = []

        def mutate(conn: sqlite3.Connection) -> None:
            updated_case = replace(
                case,
                status=new_state,
                reviewer=actor.actor_id,
                reviewed_at=moment,
                decision=decision,
            )
            self.store._save_quarantine_conn(conn, updated_case)
            if decision == REVIEW_MERGE:
                # 确认同一人：以新内容作为后续基线。
                profile = self.store.get_profile(case.customer_id)
                if profile is not None:
                    self.store._replace_profile_conn(
                        conn,
                        replace(
                            profile,
                            first_content_hash=case.incoming_hash,
                            first_content_preview=case.incoming_preview,
                        ),
                    )
            # 隔离解除后恢复因隔离而暂停的未来提醒；若同时存在未处置的
            # 风险异常，则该类暂停仍由风险流程负责，不在此恢复。
            if not self._has_open_risk(case.customer_id):
                for task in self.store.list_tasks(
                    customer_id=case.customer_id, states=(TASK_PAUSED,)
                ):
                    resumed.append(task.task_id)
                    self.store._save_task_conn(
                        conn, replace(task, state=TASK_SCHEDULED, revision=task.revision + 1)
                    )

        self.store.eventful(
            actor.actor_id,
            "privacy.review",
            {
                "customer_id": case.case_id,
                "case_id": case_id,
                "decision": decision,
                "resumed_tasks": resumed,
            },
            mutate,
        )
        return {"case_id": case_id, "state": new_state, "decision": decision, "resumed": resumed}

    # ------------------------------------------------------------------
    # 三类角色视图
    # ------------------------------------------------------------------
    def _task_view(self, task: ReminderTask) -> dict[str, object]:
        view = {
            "task_id": task.task_id,
            "plan_id": task.plan_id,
            "matter": task.matter,
            "due_at": task.due_at,
            "state": task.state,
            "store_id": task.store_id,
        }
        if task.state == TASK_SENT:
            view["sent_at"] = task.sent_at
            view["handled_by"] = task.pharmacist_id
        if task.skip_reason:
            view["skip_reason"] = task.skip_reason
        return view

    def customer_view(self, actor: Actor, customer_id: str) -> dict[str, object]:
        self._require_role(actor, d.ROLE_CUSTOMER)
        if actor.actor_id != customer_id:
            raise PermissionDenied("只能查看本人的陪伴计划")
        consent = self.store.latest_consent(customer_id)
        if consent is not None and consent.state == CONSENT_WITHDRAWN:
            # 撤回停止的是"未来主动联系"，不影响顾客查询既有服务事实：
            # 仍可看到未提醒原因、转诊进度、保供责任与计划来源编号，
            # 但不返回任何健康内容描述。
            return {
                "customer_id": customer_id,
                "consent_state": CONSENT_WITHDRAWN,
                "future_contact": "stopped",
                "withdrawn_at": consent.created_at,
                "plans": [
                    {"plan_id": p.plan_id, "source_ref": p.source_ref}
                    for p in self.store.list_plans(customer_id)
                ],
                "reminders": [
                    self._task_view(t)
                    for t in self.store.list_tasks(customer_id=customer_id)
                ],
                "referrals": [
                    {"referral_id": r.referral_id, "status": r.status,
                     "updated_at": r.updated_at}
                    for r in self.store.list_referrals(customer_id)
                ],
                "supply_batches": [
                    {"batch_id": b.batch_id, "status": b.status,
                     "owner_store_id": b.owner_store_id}
                    for b in self.store.list_batches(customer_id)
                ],
                "notice": BOUNDARY_NOTICE,
            }
        plans = [
            {
                "plan_id": p.plan_id,
                "source_kind": p.source_kind,
                "source_ref": p.source_ref,
            }
            for p in self.store.list_plans(customer_id)
        ]
        tasks = [self._task_view(t) for t in self.store.list_tasks(customer_id=customer_id)]
        referrals = [
            {"referral_id": r.referral_id, "status": r.status, "updated_at": r.updated_at}
            for r in self.store.list_referrals(customer_id)
        ]
        batches = [
            {"batch_id": b.batch_id, "status": b.status, "owner_store_id": b.owner_store_id}
            for b in self.store.list_batches(customer_id)
        ]
        return {
            "customer_id": customer_id,
            "consent_state": consent.state if consent else "unknown",
            "consent_version": consent.version if consent else 0,
            "plans": plans,                       # 计划来源
            "reminders": tasks,                  # 含未提醒原因 skip_reason
            "referrals": referrals,              # 转诊进度
            "supply_batches": batches,           # 保供责任
            "notice": BOUNDARY_NOTICE,
        }

    def pharmacist_view(
        self, actor: Actor, customer_id: str, matter: str, at: str | None = None
    ) -> dict[str, object]:
        moment = at or now_iso()
        self._assert_covered(actor, customer_id, matter, moment)
        consent = self.store.latest_consent(customer_id)
        tasks = [
            self._task_view(t)
            for t in self.store.list_tasks(customer_id=customer_id)
            if t.matter == matter or matter == MATTER_COUNSELING
        ]
        feedback_rows = self.store.connection.execute(
            "SELECT * FROM feedback WHERE customer_id=? ORDER BY received_at",
            (customer_id,),
        ).fetchall()
        feedback = [
            {
                "feedback_id": row["feedback_id"],
                "plan_id": row["plan_id"],
                "content": row["content"],
                "received_at": row["received_at"],
            }
            for row in feedback_rows
        ]
        anomaly_rows = self.store.connection.execute(
            "SELECT anomaly_id, signals, score, state, opened_at FROM anomalies"
            " WHERE customer_id=? ORDER BY opened_at",
            (customer_id,),
        ).fetchall()
        anomalies = [dict(row) for row in anomaly_rows]
        referrals = [
            {"referral_id": r.referral_id, "status": r.status, "target_kind": r.target_kind}
            for r in self.store.list_referrals(customer_id)
        ]
        handovers = [
            {
                "handover_id": h.handover_id,
                "from_store_id": h.from_store_id,
                "to_store_id": h.to_store_id,
                "status": h.status,
            }
            for h in self.store.list_handovers(customer_id)
        ]
        return {
            "customer_id": customer_id,
            "matter": matter,
            "consent_version": consent.version,
            "consent_scopes": list(consent.scopes),
            "pharmacist_id": actor.pharmacist_id,
            "store_id": actor.store_id,
            "reminders": tasks,
            "feedback": feedback,
            "anomalies": anomalies,
            "referrals": referrals,
            "handovers": handovers,
            "notice": BOUNDARY_NOTICE,
        }

    def compliance_view(self, actor: Actor, customer_id: str) -> dict[str, object]:
        """合规视角：完整审计链 + 撤回后的法规最小事实。"""
        self._require_role(actor, d.ROLE_COMPLIANCE)
        consent = self.store.latest_consent(customer_id)
        chain = self.store.verify_chain()
        base: dict[str, object] = {
            "customer_id": customer_id,
            "audit_chain": chain,
            "audit_trail": self.store.list_audit(customer_id),
        }
        if consent is not None and consent.state == CONSENT_WITHDRAWN:
            # 撤回后仅保留法规要求的最小事实，健康内容一律不返回。
            base.update(
                {
                    "access_mode": "minimal_facts",
                    "consent_versions": [
                        self._consent_view(v)
                        for v in self.store.list_consent_versions(customer_id)
                    ],
                    "plan_sources": [
                        {"plan_id": p.plan_id, "source_kind": p.source_kind, "source_ref": p.source_ref}
                        for p in self.store.list_plans(customer_id)
                    ],
                    "contact_facts": [
                        self._task_view(t)
                        for t in self.store.list_tasks(customer_id=customer_id)
                    ],
                    "health_content": "[redacted: consent withdrawn]",
                    "open_quarantine": [
                        {"case_id": c.case_id, "status": c.status}
                        for c in self.store.list_open_quarantine(customer_id)
                    ],
                }
            )
            return base

        base.update(
            {
                "access_mode": "full",
                "consent_versions": [
                    self._consent_view(v)
                    for v in self.store.list_consent_versions(customer_id)
                ],
                "plans": [
                    {
                        "plan_id": p.plan_id,
                        "source_kind": p.source_kind,
                        "source_ref": p.source_ref,
                        "active": p.active,
                    }
                    for p in self.store.list_plans(customer_id)
                ],
                "open_quarantine": [
                    {
                        "case_id": c.case_id,
                        "incoming_preview": c.incoming_preview,
                        "status": c.status,
                        "opened_at": c.opened_at,
                    }
                    for c in self.store.list_open_quarantine(customer_id)
                ],
                "handovers": [
                    {
                        "handover_id": h.handover_id,
                        "from_store_id": h.from_store_id,
                        "to_store_id": h.to_store_id,
                        "status": h.status,
                        "accepted_at": h.accepted_at,
                    }
                    for h in self.store.list_handovers(customer_id)
                ],
                "supply_batches": [
                    {
                        "batch_id": b.batch_id,
                        "status": b.status,
                        "owner_store_id": b.owner_store_id,
                    }
                    for b in self.store.list_batches(customer_id)
                ],
            }
        )
        return base
