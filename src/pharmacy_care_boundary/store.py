"""药店健康陪伴边界的 SQLite 存储。

设计要点：
- 业务表按实体分表；授权/计划/提醒等都带版本或状态，历史行保留不删除。
- 审计事件只追加，entry_hash 对 (prev_hash, 事件内容) 取 SHA-256，
  所有实体共用同一条链，任何篡改都会断链。
- 已发送提醒在数据库层禁止改写（UPDATE 条件带 status <> 'sent'）。
- 反馈以 idempotency_key 去重；重复提交沿用首条回执。
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from . import domain as d

_SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    record_id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    state TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK(revision > 0),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS consents (
    customer_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    scopes TEXT NOT NULL,
    channels TEXT NOT NULL,
    granted_at TEXT NOT NULL,
    revoked_at TEXT,
    revoked_reason TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (customer_id, version)
);

CREATE TABLE IF NOT EXISTS pharmacists (
    pharmacist_id TEXT PRIMARY KEY,
    store_id TEXT NOT NULL,
    license_no TEXT NOT NULL,
    scopes TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_to TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS plans (
    plan_id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL,
    store_id TEXT NOT NULL,
    pharmacist_id TEXT NOT NULL,
    medications TEXT NOT NULL,
    revision INTEGER NOT NULL,
    consent_version INTEGER NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reminders (
    reminder_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL,
    customer_id TEXT NOT NULL,
    medication TEXT NOT NULL,
    scheduled_at TEXT NOT NULL,
    channel TEXT NOT NULL,
    plan_revision INTEGER NOT NULL,
    consent_version INTEGER NOT NULL,
    status TEXT NOT NULL,
    sent_at TEXT,
    skip_reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reminders_due
    ON reminders(status, scheduled_at);
CREATE INDEX IF NOT EXISTS idx_reminders_customer
    ON reminders(customer_id, scheduled_at);

CREATE TABLE IF NOT EXISTS feedback (
    feedback_id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    content_code TEXT NOT NULL,
    detail TEXT NOT NULL,
    reported_at TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    receipt_no TEXT NOT NULL,
    duplicate_of TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS signals (
    signal_id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    rule_code TEXT NOT NULL,
    severity INTEGER NOT NULL,
    detail TEXT NOT NULL,
    status TEXT NOT NULL,
    opened_by TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    closed_by TEXT NOT NULL DEFAULT '',
    closed_at TEXT,
    closure_note TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS referrals (
    referral_id TEXT PRIMARY KEY,
    signal_id TEXT NOT NULL,
    customer_id TEXT NOT NULL,
    target TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    status TEXT NOT NULL,
    suggested_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT '',
    progress_note TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS supply_batches (
    batch_id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    medication TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK(quantity > 0),
    responsible_store_id TEXT NOT NULL,
    backup_store_id TEXT NOT NULL,
    status TEXT NOT NULL,
    due_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    delivered_at TEXT
);

CREATE TABLE IF NOT EXISTS handoffs (
    handoff_id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    from_store_id TEXT NOT NULL,
    to_store_id TEXT NOT NULL,
    to_pharmacist_id TEXT NOT NULL,
    initiated_by TEXT NOT NULL,
    status TEXT NOT NULL,
    effective_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    note TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS quarantine_cases (
    case_id TEXT PRIMARY KEY,
    identifier TEXT NOT NULL,
    fragment_count INTEGER NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    reviewed_by TEXT NOT NULL DEFAULT '',
    reviewed_at TEXT,
    review_note TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_quarantine_identifier
    ON quarantine_cases(identifier, status);

-- 标识下出现过的健康内容片段（顾客+内容指纹），用于发现同一标识关联不同健康内容
CREATE TABLE IF NOT EXISTS identity_fragments (
    identifier TEXT NOT NULL,
    customer_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    seen_at TEXT NOT NULL,
    PRIMARY KEY (identifier, customer_id, fingerprint)
);

CREATE TABLE IF NOT EXISTS audit_log (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    actor_role TEXT NOT NULL,
    action TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    payload TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    entry_hash TEXT NOT NULL
);
"""


def _enc_set(values: Iterable[str]) -> str:
    return json.dumps(sorted(set(values)), ensure_ascii=False)


def _dec_list(value: str) -> list[str]:
    return json.loads(value) if value else []


def _enc_tuple(values: Iterable[str]) -> str:
    return json.dumps(list(values), ensure_ascii=False)


class UpdateBlocked(Exception):
    """试图改写不可变记录（如已发送提醒）。"""


class Store:
    def __init__(self, path: str | Path = ":memory:", clock=d.now_iso) -> None:
        self.connection = sqlite3.connect(str(path))
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.executescript(_SCHEMA)
        self.connection.commit()
        self._clock = clock

    # -- 旧版兼容 ----------------------------------------------------------
    def add(self, record: d.Record) -> d.Record:
        value = record.stamped()
        with self.connection:
            self.connection.execute(
                "INSERT INTO records(record_id, owner_id, state, revision, created_at) "
                "VALUES(?,?,?,?,?)",
                (value.record_id, value.owner_id, value.state, value.revision, value.created_at),
            )
        return value

    def get(self, record_id: str) -> d.Record | None:
        row = self.connection.execute(
            "SELECT record_id, owner_id, state, revision, created_at FROM records WHERE record_id=?",
            (record_id,),
        ).fetchone()
        return d.Record(**dict(row)) if row else None

    # -- 审计哈希链 --------------------------------------------------------
    def append_audit(
        self,
        *,
        actor_id: str,
        actor_role: str,
        action: str,
        entity_type: str,
        entity_id: str,
        payload: dict[str, Any] | None = None,
    ) -> d.AuditEvent:
        import hashlib

        at = self._clock()
        body = json.dumps(payload or {}, ensure_ascii=False, sort_keys=True)
        with self.connection:
            last = self.connection.execute(
                "SELECT entry_hash FROM audit_log ORDER BY seq DESC LIMIT 1"
            ).fetchone()
            prev_hash = last["entry_hash"] if last else ""
            material = json.dumps(
                [prev_hash, at, actor_id, actor_role, action, entity_type, entity_id, body],
                ensure_ascii=False,
                sort_keys=True,
            )
            entry_hash = hashlib.sha256(material.encode("utf-8")).hexdigest()
            cur = self.connection.execute(
                "INSERT INTO audit_log(at, actor_id, actor_role, action, entity_type, "
                "entity_id, payload, prev_hash, entry_hash) VALUES(?,?,?,?,?,?,?,?,?)",
                (at, actor_id, actor_role, action, entity_type, entity_id, body,
                 prev_hash, entry_hash),
            )
            seq = cur.lastrowid
        return d.AuditEvent(
            seq=seq, at=at, actor_id=actor_id, actor_role=actor_role, action=action,
            entity_type=entity_type, entity_id=entity_id, payload=payload or {},
            prev_hash=prev_hash, entry_hash=entry_hash,
        )

    def verify_audit_chain(self) -> bool:
        """重算整条链；断链或内容被改返回 False。"""
        import hashlib

        prev = ""
        for row in self.connection.execute(
            "SELECT * FROM audit_log ORDER BY seq ASC"
        ).fetchall():
            material = json.dumps(
                [prev, row["at"], row["actor_id"], row["actor_role"], row["action"],
                 row["entity_type"], row["entity_id"], row["payload"]],
                ensure_ascii=False, sort_keys=True,
            )
            digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
            if digest != row["entry_hash"] or row["prev_hash"] != prev:
                return False
            prev = digest
        return True

    def list_audit(self, entity_type: str = "", entity_id: str = "") -> list[d.AuditEvent]:
        sql = "SELECT * FROM audit_log"
        clauses: list[str] = []
        params: list[Any] = []
        if entity_type:
            clauses.append("entity_type=?")
            params.append(entity_type)
        if entity_id:
            clauses.append("entity_id=?")
            params.append(entity_id)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY seq ASC"
        rows = self.connection.execute(sql, params).fetchall()
        return [
            d.AuditEvent(
                seq=r["seq"], at=r["at"], actor_id=r["actor_id"], actor_role=r["actor_role"],
                action=r["action"], entity_type=r["entity_type"], entity_id=r["entity_id"],
                payload=json.loads(r["payload"]), prev_hash=r["prev_hash"],
                entry_hash=r["entry_hash"],
            )
            for r in rows
        ]

    # -- 授权 --------------------------------------------------------------
    def add_consent(self, grant: d.ConsentGrant) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT INTO consents(customer_id, version, scopes, channels, granted_at, "
                "revoked_at, revoked_reason) VALUES(?,?,?,?,?,?,?)",
                (grant.customer_id, grant.version, _enc_set(grant.scopes),
                 _enc_set(grant.channels), grant.granted_at, grant.revoked_at,
                 grant.revoked_reason),
            )

    def latest_consent(self, customer_id: str) -> d.ConsentGrant | None:
        row = self.connection.execute(
            "SELECT * FROM consents WHERE customer_id=? ORDER BY version DESC LIMIT 1",
            (customer_id,),
        ).fetchone()
        return self._consent_row(row) if row else None

    def consent_version(self, customer_id: str, version: int) -> d.ConsentGrant | None:
        row = self.connection.execute(
            "SELECT * FROM consents WHERE customer_id=? AND version=?",
            (customer_id, version),
        ).fetchone()
        return self._consent_row(row) if row else None

    def revoke_consent(self, customer_id: str, at: str, reason: str) -> bool:
        with self.connection:
            cur = self.connection.execute(
                "UPDATE consents SET revoked_at=?, revoked_reason=? "
                "WHERE customer_id=? AND revoked_at IS NULL",
                (at, reason, customer_id),
            )
            return cur.rowcount > 0

    @staticmethod
    def _consent_row(row: sqlite3.Row) -> d.ConsentGrant:
        return d.ConsentGrant(
            customer_id=row["customer_id"], version=row["version"],
            scopes=frozenset(_dec_list(row["scopes"])),
            channels=frozenset(_dec_list(row["channels"])),
            granted_at=row["granted_at"], revoked_at=row["revoked_at"],
            revoked_reason=row["revoked_reason"],
        )

    # -- 药师资质 ----------------------------------------------------------
    def upsert_pharmacist(self, cred: d.PharmacistCredential) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT INTO pharmacists(pharmacist_id, store_id, license_no, scopes, "
                "valid_from, valid_to, active) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(pharmacist_id) DO UPDATE SET store_id=excluded.store_id, "
                "license_no=excluded.license_no, scopes=excluded.scopes, "
                "valid_from=excluded.valid_from, valid_to=excluded.valid_to, "
                "active=excluded.active",
                (cred.pharmacist_id, cred.store_id, cred.license_no, _enc_set(cred.scopes),
                 cred.valid_from, cred.valid_to, 1 if cred.active else 0),
            )

    def get_pharmacist(self, pharmacist_id: str) -> d.PharmacistCredential | None:
        row = self.connection.execute(
            "SELECT * FROM pharmacists WHERE pharmacist_id=?", (pharmacist_id,)
        ).fetchone()
        return self._pharmacist_row(row) if row else None

    @staticmethod
    def _pharmacist_row(row: sqlite3.Row) -> d.PharmacistCredential:
        return d.PharmacistCredential(
            pharmacist_id=row["pharmacist_id"], store_id=row["store_id"],
            license_no=row["license_no"], scopes=frozenset(_dec_list(row["scopes"])),
            valid_from=row["valid_from"], valid_to=row["valid_to"],
            active=bool(row["active"]),
        )

    # -- 药品计划 ----------------------------------------------------------
    def add_plan(self, plan: d.MedicationPlan) -> d.MedicationPlan:
        value = plan.stamped()
        with self.connection:
            self.connection.execute(
                "INSERT INTO plans(plan_id, customer_id, store_id, pharmacist_id, medications, "
                "revision, consent_version, status, created_at, updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (value.plan_id, value.customer_id, value.store_id, value.pharmacist_id,
                 _enc_tuple(value.medications), value.revision, value.consent_version,
                 value.status, value.created_at, value.updated_at),
            )
        return value

    def get_plan(self, plan_id: str) -> d.MedicationPlan | None:
        row = self.connection.execute("SELECT * FROM plans WHERE plan_id=?", (plan_id,)).fetchone()
        return self._plan_row(row) if row else None

    def list_plans(self, customer_id: str = "") -> list[d.MedicationPlan]:
        if customer_id:
            rows = self.connection.execute(
                "SELECT * FROM plans WHERE customer_id=? ORDER BY created_at ASC",
                (customer_id,),
            ).fetchall()
        else:
            rows = self.connection.execute("SELECT * FROM plans ORDER BY created_at ASC").fetchall()
        return [self._plan_row(r) for r in rows]

    def update_plan_status(self, plan_id: str, status: str, at: str) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE plans SET status=?, updated_at=? WHERE plan_id=?", (status, at, plan_id)
            )

    @staticmethod
    def _plan_row(row: sqlite3.Row) -> d.MedicationPlan:
        return d.MedicationPlan(
            plan_id=row["plan_id"], customer_id=row["customer_id"], store_id=row["store_id"],
            pharmacist_id=row["pharmacist_id"], medications=tuple(_dec_list(row["medications"])),
            revision=row["revision"], consent_version=row["consent_version"],
            status=row["status"], created_at=row["created_at"], updated_at=row["updated_at"],
        )

    # -- 提醒 --------------------------------------------------------------
    def add_reminder(self, reminder: d.Reminder) -> d.Reminder:
        value = reminder.stamped()
        with self.connection:
            self.connection.execute(
                "INSERT INTO reminders(reminder_id, plan_id, customer_id, medication, "
                "scheduled_at, channel, plan_revision, consent_version, status, sent_at, "
                "skip_reason, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (value.reminder_id, value.plan_id, value.customer_id, value.medication,
                 value.scheduled_at, value.channel, value.plan_revision, value.consent_version,
                 value.status, value.sent_at, value.skip_reason, value.created_at),
            )
        return value

    def get_reminder(self, reminder_id: str) -> d.Reminder | None:
        row = self.connection.execute(
            "SELECT * FROM reminders WHERE reminder_id=?", (reminder_id,)
        ).fetchone()
        return self._reminder_row(row) if row else None

    def list_reminders(self, customer_id: str = "", plan_id: str = "") -> list[d.Reminder]:
        clauses, params = [], []
        if customer_id:
            clauses.append("customer_id=?")
            params.append(customer_id)
        if plan_id:
            clauses.append("plan_id=?")
            params.append(plan_id)
        sql = "SELECT * FROM reminders"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY scheduled_at ASC"
        return [self._reminder_row(r) for r in self.connection.execute(sql, params).fetchall()]

    def due_reminders(self, now: str) -> list[d.Reminder]:
        """到期且仍待发的提醒——门店交接或进程中断后凭它续跑。"""
        rows = self.connection.execute(
            "SELECT * FROM reminders WHERE status=? AND scheduled_at<=? ORDER BY scheduled_at ASC",
            (d.REMINDER_PENDING, now),
        ).fetchall()
        return [self._reminder_row(r) for r in rows]

    def _update_reminder(self, reminder_id: str, **fields: Any) -> None:
        # status='sent' 的行在 WHERE 中被排除：已发送即冻结
        sets = ", ".join(f"{k}=?" for k in fields)
        params = list(fields.values()) + [reminder_id]
        with self.connection:
            cur = self.connection.execute(
                f"UPDATE reminders SET {sets} WHERE reminder_id=? AND status<>'sent'", params
            )
            if cur.rowcount == 0:
                raise UpdateBlocked(f"提醒已发送或不存在，禁止改写：{reminder_id}")

    def mark_reminder_sent(self, reminder_id: str, at: str) -> None:
        self._update_reminder(reminder_id, status=d.REMINDER_SENT, sent_at=at)

    def cancel_reminder(self, reminder_id: str, reason: str) -> None:
        self._update_reminder(reminder_id, status=d.REMINDER_CANCELLED, skip_reason=reason)

    def block_reminder(self, reminder_id: str, reason: str) -> None:
        self._update_reminder(reminder_id, status=d.REMINDER_BLOCKED, skip_reason=reason)

    @staticmethod
    def _reminder_row(row: sqlite3.Row) -> d.Reminder:
        return d.Reminder(
            reminder_id=row["reminder_id"], plan_id=row["plan_id"],
            customer_id=row["customer_id"], medication=row["medication"],
            scheduled_at=row["scheduled_at"], channel=row["channel"],
            plan_revision=row["plan_revision"], consent_version=row["consent_version"],
            status=row["status"], sent_at=row["sent_at"], skip_reason=row["skip_reason"],
            created_at=row["created_at"],
        )

    # -- 依从反馈（幂等）---------------------------------------------------
    def find_feedback_by_key(self, idempotency_key: str) -> d.AdherenceFeedback | None:
        row = self.connection.execute(
            "SELECT * FROM feedback WHERE idempotency_key=?", (idempotency_key,)
        ).fetchone()
        return self._feedback_row(row) if row else None

    def add_feedback(self, feedback: d.AdherenceFeedback) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT INTO feedback(feedback_id, customer_id, plan_id, content_code, detail, "
                "reported_at, idempotency_key, receipt_no, duplicate_of) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (feedback.feedback_id, feedback.customer_id, feedback.plan_id,
                 feedback.content_code, feedback.detail, feedback.reported_at,
                 feedback.idempotency_key, feedback.receipt_no, feedback.duplicate_of),
            )

    def list_feedback(self, plan_id: str = "", customer_id: str = "") -> list[d.AdherenceFeedback]:
        clauses, params = [], []
        if plan_id:
            clauses.append("plan_id=?")
            params.append(plan_id)
        if customer_id:
            clauses.append("customer_id=?")
            params.append(customer_id)
        sql = "SELECT * FROM feedback"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY reported_at ASC"
        return [self._feedback_row(r) for r in self.connection.execute(sql, params).fetchall()]

    @staticmethod
    def _feedback_row(row: sqlite3.Row) -> d.AdherenceFeedback:
        return d.AdherenceFeedback(
            feedback_id=row["feedback_id"], customer_id=row["customer_id"],
            plan_id=row["plan_id"], content_code=row["content_code"], detail=row["detail"],
            reported_at=row["reported_at"], idempotency_key=row["idempotency_key"],
            receipt_no=row["receipt_no"], duplicate_of=row["duplicate_of"],
        )

    # -- 异常信号 ----------------------------------------------------------
    def add_signal(self, signal: d.AnomalySignal) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT INTO signals(signal_id, customer_id, plan_id, rule_code, severity, "
                "detail, status, opened_by, opened_at, closed_by, closed_at, closure_note) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (signal.signal_id, signal.customer_id, signal.plan_id, signal.rule_code,
                 signal.severity, signal.detail, signal.status, signal.opened_by,
                 signal.opened_at, signal.closed_by, signal.closed_at, signal.closure_note),
            )

    def get_signal(self, signal_id: str) -> d.AnomalySignal | None:
        row = self.connection.execute("SELECT * FROM signals WHERE signal_id=?", (signal_id,)).fetchone()
        return self._signal_row(row) if row else None

    def list_signals(self, customer_id: str = "", status: str = "") -> list[d.AnomalySignal]:
        clauses, params = [], []
        if customer_id:
            clauses.append("customer_id=?")
            params.append(customer_id)
        if status:
            clauses.append("status=?")
            params.append(status)
        sql = "SELECT * FROM signals"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY opened_at ASC"
        return [self._signal_row(r) for r in self.connection.execute(sql, params).fetchall()]

    def update_signal(self, signal_id: str, status: str, closed_by: str, closed_at: str,
                      note: str) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE signals SET status=?, closed_by=?, closed_at=?, closure_note=? "
                "WHERE signal_id=?",
                (status, closed_by, closed_at, note, signal_id),
            )

    @staticmethod
    def _signal_row(row: sqlite3.Row) -> d.AnomalySignal:
        return d.AnomalySignal(
            signal_id=row["signal_id"], customer_id=row["customer_id"], plan_id=row["plan_id"],
            rule_code=row["rule_code"], severity=row["severity"], detail=row["detail"],
            status=row["status"], opened_by=row["opened_by"], opened_at=row["opened_at"],
            closed_by=row["closed_by"], closed_at=row["closed_at"],
            closure_note=row["closure_note"],
        )

    # -- 转诊 --------------------------------------------------------------
    def add_referral(self, referral: d.Referral) -> d.Referral:
        with self.connection:
            self.connection.execute(
                "INSERT INTO referrals(referral_id, signal_id, customer_id, target, reason_code, "
                "status, suggested_by, created_at, updated_at, progress_note) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (referral.referral_id, referral.signal_id, referral.customer_id, referral.target,
                 referral.reason_code, referral.status, referral.suggested_by, referral.created_at,
                 referral.updated_at or referral.created_at, referral.progress_note),
            )
        return referral

    def get_referral(self, referral_id: str) -> d.Referral | None:
        row = self.connection.execute(
            "SELECT * FROM referrals WHERE referral_id=?", (referral_id,)
        ).fetchone()
        return self._referral_row(row) if row else None

    def list_referrals(self, customer_id: str = "", signal_id: str = "") -> list[d.Referral]:
        clauses, params = [], []
        if customer_id:
            clauses.append("customer_id=?")
            params.append(customer_id)
        if signal_id:
            clauses.append("signal_id=?")
            params.append(signal_id)
        sql = "SELECT * FROM referrals"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at ASC"
        return [self._referral_row(r) for r in self.connection.execute(sql, params).fetchall()]

    def update_referral(self, referral_id: str, status: str, at: str, note: str) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE referrals SET status=?, updated_at=?, progress_note=? WHERE referral_id=?",
                (status, at, note, referral_id),
            )

    @staticmethod
    def _referral_row(row: sqlite3.Row) -> d.Referral:
        return d.Referral(
            referral_id=row["referral_id"], signal_id=row["signal_id"],
            customer_id=row["customer_id"], target=row["target"],
            reason_code=row["reason_code"], status=row["status"],
            suggested_by=row["suggested_by"], created_at=row["created_at"],
            updated_at=row["updated_at"], progress_note=row["progress_note"],
        )

    # -- 应急保供批次 ------------------------------------------------------
    def add_supply_batch(self, batch: d.SupplyBatch) -> d.SupplyBatch:
        value = batch.stamped()
        with self.connection:
            self.connection.execute(
                "INSERT INTO supply_batches(batch_id, customer_id, plan_id, medication, quantity, "
                "responsible_store_id, backup_store_id, status, due_at, created_at, delivered_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (value.batch_id, value.customer_id, value.plan_id, value.medication,
                 value.quantity, value.responsible_store_id, value.backup_store_id, value.status,
                 value.due_at, value.created_at, value.delivered_at),
            )
        return value

    def get_supply_batch(self, batch_id: str) -> d.SupplyBatch | None:
        row = self.connection.execute(
            "SELECT * FROM supply_batches WHERE batch_id=?", (batch_id,)
        ).fetchone()
        return self._batch_row(row) if row else None

    def list_supply_batches(self, customer_id: str = "", status: str = "") -> list[d.SupplyBatch]:
        clauses, params = [], []
        if customer_id:
            clauses.append("customer_id=?")
            params.append(customer_id)
        if status:
            clauses.append("status=?")
            params.append(status)
        sql = "SELECT * FROM supply_batches"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY due_at ASC"
        return [self._batch_row(r) for r in self.connection.execute(sql, params).fetchall()]

    def update_batch_status(self, batch_id: str, status: str, delivered_at: str | None) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE supply_batches SET status=?, delivered_at=? WHERE batch_id=?",
                (status, delivered_at, batch_id),
            )

    def transfer_batch_responsibility(self, batch_id: str, new_store_id: str) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE supply_batches SET responsible_store_id=? WHERE batch_id=? AND status<>?",
                (new_store_id, batch_id, d.BATCH_DELIVERED),
            )

    @staticmethod
    def _batch_row(row: sqlite3.Row) -> d.SupplyBatch:
        return d.SupplyBatch(
            batch_id=row["batch_id"], customer_id=row["customer_id"], plan_id=row["plan_id"],
            medication=row["medication"], quantity=row["quantity"],
            responsible_store_id=row["responsible_store_id"],
            backup_store_id=row["backup_store_id"], status=row["status"], due_at=row["due_at"],
            created_at=row["created_at"], delivered_at=row["delivered_at"],
        )

    # -- 门店交接 ----------------------------------------------------------
    def add_handoff(self, handoff: d.StoreHandoff) -> d.StoreHandoff:
        value = handoff.stamped()
        with self.connection:
            self.connection.execute(
                "INSERT INTO handoffs(handoff_id, customer_id, plan_id, from_store_id, "
                "to_store_id, to_pharmacist_id, initiated_by, status, effective_at, created_at, "
                "completed_at, note) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (value.handoff_id, value.customer_id, value.plan_id, value.from_store_id,
                 value.to_store_id, value.to_pharmacist_id, value.initiated_by, value.status,
                 value.effective_at, value.created_at, value.completed_at, value.note),
            )
        return value

    def get_handoff(self, handoff_id: str) -> d.StoreHandoff | None:
        row = self.connection.execute(
            "SELECT * FROM handoffs WHERE handoff_id=?", (handoff_id,)
        ).fetchone()
        return self._handoff_row(row) if row else None

    def list_handoffs(self, customer_id: str = "", plan_id: str = "") -> list[d.StoreHandoff]:
        clauses, params = [], []
        if customer_id:
            clauses.append("customer_id=?")
            params.append(customer_id)
        if plan_id:
            clauses.append("plan_id=?")
            params.append(plan_id)
        sql = "SELECT * FROM handoffs"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY effective_at ASC"
        return [self._handoff_row(r) for r in self.connection.execute(sql, params).fetchall()]

    def complete_handoff(self, handoff_id: str, at: str) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE handoffs SET status=?, completed_at=? WHERE handoff_id=?",
                (d.HANDOFF_COMPLETED, at, handoff_id),
            )

    @staticmethod
    def _handoff_row(row: sqlite3.Row) -> d.StoreHandoff:
        return d.StoreHandoff(
            handoff_id=row["handoff_id"], customer_id=row["customer_id"], plan_id=row["plan_id"],
            from_store_id=row["from_store_id"], to_store_id=row["to_store_id"],
            to_pharmacist_id=row["to_pharmacist_id"], initiated_by=row["initiated_by"],
            status=row["status"], effective_at=row["effective_at"], created_at=row["created_at"],
            completed_at=row["completed_at"], note=row["note"],
        )

    # -- 标识隔离 ----------------------------------------------------------
    def add_quarantine_case(self, case: d.QuarantineCase) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT INTO quarantine_cases(case_id, identifier, fragment_count, status, "
                "created_at, reviewed_by, reviewed_at, review_note) VALUES(?,?,?,?,?,?,?,?)",
                (case.case_id, case.identifier, case.fragment_count, case.status, case.created_at,
                 case.reviewed_by, case.reviewed_at, case.review_note),
            )

    def open_quarantine_for(self, identifier: str) -> d.QuarantineCase | None:
        """取该标识当前仍在隔离中的案卷。"""
        row = self.connection.execute(
            "SELECT * FROM quarantine_cases WHERE identifier=? AND status=? "
            "ORDER BY created_at DESC LIMIT 1",
            (identifier, d.QUARANTINE_QUARANTINED),
        ).fetchone()
        return self._quarantine_row(row) if row else None

    def list_quarantine(self, status: str = "") -> list[d.QuarantineCase]:
        sql = "SELECT * FROM quarantine_cases"
        params: list[Any] = []
        if status:
            sql += " WHERE status=?"
            params.append(status)
        sql += " ORDER BY created_at ASC"
        return [self._quarantine_row(r) for r in self.connection.execute(sql, params).fetchall()]

    def resolve_quarantine(self, case_id: str, status: str, reviewer: str, at: str,
                           note: str) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE quarantine_cases SET status=?, reviewed_by=?, reviewed_at=?, "
                "review_note=? WHERE case_id=?",
                (status, reviewer, at, note, case_id),
            )

    @staticmethod
    def _quarantine_row(row: sqlite3.Row) -> d.QuarantineCase:
        return d.QuarantineCase(
            case_id=row["case_id"], identifier=row["identifier"],
            fragment_count=row["fragment_count"], status=row["status"],
            created_at=row["created_at"], reviewed_by=row["reviewed_by"],
            reviewed_at=row["reviewed_at"], review_note=row["review_note"],
        )

    # -- 标识健康内容片段 --------------------------------------------------
    def add_identity_fragment(self, identifier: str, customer_id: str, fingerprint: str,
                              at: str) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT OR IGNORE INTO identity_fragments(identifier, customer_id, fingerprint, "
                "seen_at) VALUES(?,?,?,?)",
                (identifier, customer_id, fingerprint, at),
            )

    def identity_fragments(self, identifier: str) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT identifier, customer_id, fingerprint, seen_at FROM identity_fragments "
            "WHERE identifier=? ORDER BY seen_at ASC",
            (identifier,),
        ).fetchall()

    def identifiers_for_customer(self, customer_id: str) -> list[str]:
        rows = self.connection.execute(
            "SELECT DISTINCT identifier FROM identity_fragments WHERE customer_id=?",
            (customer_id,),
        ).fetchall()
        return [r["identifier"] for r in rows]

    def customers_for_identifier(self, identifier: str) -> list[str]:
        rows = self.connection.execute(
            "SELECT DISTINCT customer_id FROM identity_fragments WHERE identifier=?",
            (identifier,),
        ).fetchall()
        return [r["customer_id"] for r in rows]
