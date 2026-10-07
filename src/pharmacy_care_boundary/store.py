"""健康陪伴边界台的 SQLite 存储。

所有状态变更都通过 :meth:`Store.eventful` 在同一个事务内完成：业务写入
与审计区块要么同时生效，要么同时回滚。审计区块以 ``prev_hash`` 串联成
哈希链——授权版本、药师资质、药品计划、依从反馈、异常信号、转诊建议、
应急保供批次与门店交接因此处在同一条可校验的审计链上。
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Callable, Iterator

from .domain import (
    AdherenceFeedback,
    Anomaly,
    AuditEntry,
    ConsentVersion,
    Credential,
    IdentityProfile,
    MedicationPlan,
    QuarantineCase,
    Record,
    Referral,
    ReminderTask,
    StoreHandover,
    SupplyBatch,
    now_iso,
    stable_hash,
)


def _loads(value: str) -> tuple[str, ...]:
    return tuple(json.loads(value)) if value else ()


def _dumps(values: tuple[str, ...] | list[str]) -> str:
    return json.dumps(list(values), ensure_ascii=False)


class Store:
    def __init__(self, path: str | Path = ":memory:") -> None:
        self.connection = sqlite3.connect(str(path))
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self._init_schema()

    # ------------------------------------------------------------------
    # schema
    # ------------------------------------------------------------------
    def _init_schema(self) -> None:
        statements = (
            """
            CREATE TABLE IF NOT EXISTS records (
                record_id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                state TEXT NOT NULL,
                revision INTEGER NOT NULL CHECK(revision > 0),
                created_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS audit_log (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                at TEXT NOT NULL,
                actor_id TEXT NOT NULL,
                action TEXT NOT NULL,
                payload TEXT NOT NULL,
                prev_hash TEXT NOT NULL,
                entry_hash TEXT NOT NULL UNIQUE
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS consent_versions (
                customer_id TEXT NOT NULL,
                version INTEGER NOT NULL,
                state TEXT NOT NULL,
                scopes TEXT NOT NULL,
                granted_store_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                note TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (customer_id, version)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS credentials (
                pharmacist_id TEXT PRIMARY KEY,
                store_id TEXT NOT NULL,
                matters TEXT NOT NULL,
                valid_from TEXT NOT NULL,
                valid_to TEXT NOT NULL,
                revoked INTEGER NOT NULL DEFAULT 0
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS plans (
                plan_id TEXT PRIMARY KEY,
                customer_id TEXT NOT NULL,
                source_kind TEXT NOT NULL,
                source_ref TEXT NOT NULL,
                created_at TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY,
                plan_id TEXT NOT NULL,
                customer_id TEXT NOT NULL,
                matter TEXT NOT NULL,
                due_at TEXT NOT NULL,
                state TEXT NOT NULL,
                store_id TEXT NOT NULL DEFAULT '',
                pharmacist_id TEXT NOT NULL DEFAULT '',
                sent_at TEXT NOT NULL DEFAULT '',
                skip_reason TEXT NOT NULL DEFAULT '',
                revision INTEGER NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS feedback (
                feedback_id TEXT PRIMARY KEY,
                customer_id TEXT NOT NULL,
                plan_id TEXT NOT NULL,
                content TEXT NOT NULL,
                received_at TEXT NOT NULL,
                idempotency_key TEXT NOT NULL UNIQUE
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS anomalies (
                anomaly_id TEXT PRIMARY KEY,
                customer_id TEXT NOT NULL,
                signals TEXT NOT NULL,
                score INTEGER NOT NULL,
                state TEXT NOT NULL,
                opened_by TEXT NOT NULL,
                opened_at TEXT NOT NULL,
                closed_by TEXT NOT NULL DEFAULT '',
                closed_at TEXT NOT NULL DEFAULT '',
                closure_note TEXT NOT NULL DEFAULT ''
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS referrals (
                referral_id TEXT PRIMARY KEY,
                customer_id TEXT NOT NULL,
                anomaly_id TEXT NOT NULL,
                target_kind TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT ''
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS batches (
                batch_id TEXT PRIMARY KEY,
                customer_id TEXT NOT NULL,
                plan_id TEXT NOT NULL,
                status TEXT NOT NULL,
                owner_store_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT ''
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS handovers (
                handover_id TEXT PRIMARY KEY,
                customer_id TEXT NOT NULL,
                from_store_id TEXT NOT NULL,
                to_store_id TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                accepted_at TEXT NOT NULL DEFAULT ''
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS profiles (
                customer_id TEXT PRIMARY KEY,
                first_content_hash TEXT NOT NULL,
                first_content_preview TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS quarantine_cases (
                case_id TEXT PRIMARY KEY,
                customer_id TEXT NOT NULL,
                incoming_hash TEXT NOT NULL,
                incoming_preview TEXT NOT NULL,
                status TEXT NOT NULL,
                opened_at TEXT NOT NULL,
                reviewer TEXT NOT NULL DEFAULT '',
                reviewed_at TEXT NOT NULL DEFAULT '',
                decision TEXT NOT NULL DEFAULT ''
            )
            """,
        )
        with self.connection:
            for statement in statements:
                self.connection.execute(statement)

    def eventful(
        self,
        actor_id: str,
        action: str,
        payload: dict[str, object],
        mutate: Callable[[sqlite3.Connection], None] | None = None,
    ) -> AuditEntry:
        """在单个事务内执行业务写入并追加审计区块。

        链头（seq / prev_hash）在同一事务内读取，因此并发写入不会串链；
        ``mutate`` 抛异常时业务写入与审计插入一起回滚。
        """
        with self.connection:
            conn = self.connection
            if mutate is not None:
                mutate(conn)
            row = conn.execute(
                "SELECT seq, entry_hash FROM audit_log ORDER BY seq DESC LIMIT 1"
            ).fetchone()
            seq = (row["seq"] + 1) if row else 1
            prev_hash = row["entry_hash"] if row else ""
            entry = AuditEntry(
                seq=seq,
                at=now_iso(),
                actor_id=actor_id,
                action=action,
                payload=payload,
            ).sealed(seq=seq, prev_hash=prev_hash)
            conn.execute(
                "INSERT INTO audit_log(at, actor_id, action, payload, prev_hash,"
                " entry_hash) VALUES(?,?,?,?,?,?)",
                (
                    entry.at,
                    entry.actor_id,
                    entry.action,
                    json.dumps(entry.payload, ensure_ascii=False, sort_keys=True),
                    entry.prev_hash,
                    entry.entry_hash,
                ),
            )
        return entry

    # ------------------------------------------------------------------
    # 审计链校验与查询
    # ------------------------------------------------------------------
    def verify_chain(self) -> dict[str, object]:
        """自链首重算哈希，返回链长度与首个断链位置（若有）。"""
        rows = self.connection.execute(
            "SELECT seq, at, actor_id, action, payload, prev_hash, entry_hash"
            " FROM audit_log ORDER BY seq"
        ).fetchall()
        prev_hash = ""
        for index, row in enumerate(rows, start=1):
            if row["seq"] != index or row["prev_hash"] != prev_hash:
                return {"ok": False, "length": len(rows), "broken_at": index}
            body = {
                "seq": row["seq"],
                "at": row["at"],
                "actor_id": row["actor_id"],
                "action": row["action"],
                "payload": json.loads(row["payload"]),
                "prev_hash": row["prev_hash"],
            }
            if stable_hash(body) != row["entry_hash"]:
                return {"ok": False, "length": len(rows), "broken_at": index}
            prev_hash = row["entry_hash"]
        return {"ok": True, "length": len(rows), "broken_at": None}

    def list_audit(self, customer_id: str | None = None) -> list[dict[str, object]]:
        rows = self.connection.execute(
            "SELECT seq, at, actor_id, action, payload, prev_hash, entry_hash"
            " FROM audit_log ORDER BY seq"
        ).fetchall()
        entries: list[dict[str, object]] = []
        for row in rows:
            payload = json.loads(row["payload"])
            if customer_id is not None and payload.get("customer_id") != customer_id:
                continue
            entries.append(
                {
                    "seq": row["seq"],
                    "at": row["at"],
                    "actor_id": row["actor_id"],
                    "action": row["action"],
                    "payload": payload,
                    "prev_hash": row["prev_hash"],
                    "entry_hash": row["entry_hash"],
                }
            )
        return entries

    # ------------------------------------------------------------------
    # 基础登记（保留既有契约）
    # ------------------------------------------------------------------
    def add(self, record: Record) -> Record:
        value = record.stamped()

        def mutate(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT INTO records(record_id, owner_id, state, revision, created_at)"
                " VALUES(?,?,?,?,?)",
                (
                    value.record_id,
                    value.owner_id,
                    value.state,
                    value.revision,
                    value.created_at,
                ),
            )

        self.eventful(
            value.owner_id,
            "record.register",
            {"record_id": value.record_id, "state": value.state},
            mutate,
        )
        return value

    def get(self, record_id: str) -> Record | None:
        row = self.connection.execute(
            "SELECT record_id, owner_id, state, revision, created_at"
            " FROM records WHERE record_id=?",
            (record_id,),
        ).fetchone()
        return Record(**dict(row)) if row else None

    # ------------------------------------------------------------------
    # 授权版本
    # ------------------------------------------------------------------
    def add_consent(self, consent: ConsentVersion) -> None:
        def mutate(conn: sqlite3.Connection) -> None:
            self._insert_consent_conn(conn, consent)

        self.eventful(
            consent.granted_store_id,
            "consent.append",
            {
                "customer_id": consent.customer_id,
                "version": consent.version,
                "state": consent.state,
                "scopes": list(consent.scopes),
                "store_id": consent.granted_store_id,
            },
            mutate,
        )

    @staticmethod
    def _insert_consent_conn(conn: sqlite3.Connection, consent: ConsentVersion) -> None:
        conn.execute(
            "INSERT INTO consent_versions(customer_id, version, state, scopes,"
            " granted_store_id, created_at, note) VALUES(?,?,?,?,?,?,?)",
            (
                consent.customer_id,
                consent.version,
                consent.state,
                _dumps(consent.scopes),
                consent.granted_store_id,
                consent.created_at,
                consent.note,
            ),
        )

    def latest_consent(self, customer_id: str) -> ConsentVersion | None:
        row = self.connection.execute(
            "SELECT * FROM consent_versions WHERE customer_id=?"
            " ORDER BY version DESC LIMIT 1",
            (customer_id,),
        ).fetchone()
        return self._consent(row) if row else None

    def list_consent_versions(self, customer_id: str) -> list[ConsentVersion]:
        rows = self.connection.execute(
            "SELECT * FROM consent_versions WHERE customer_id=? ORDER BY version",
            (customer_id,),
        ).fetchall()
        return [self._consent(row) for row in rows]

    @staticmethod
    def _consent(row: sqlite3.Row) -> ConsentVersion:
        return ConsentVersion(
            customer_id=row["customer_id"],
            version=row["version"],
            state=row["state"],
            scopes=_loads(row["scopes"]),
            granted_store_id=row["granted_store_id"],
            created_at=row["created_at"],
            note=row["note"],
        )

    # ------------------------------------------------------------------
    # 药师资质
    # ------------------------------------------------------------------
    def upsert_credential(self, credential: Credential, actor_id: str = "system") -> None:
        def mutate(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT INTO credentials(pharmacist_id, store_id, matters, valid_from,"
                " valid_to, revoked) VALUES(?,?,?,?,?,?)"
                " ON CONFLICT(pharmacist_id) DO UPDATE SET store_id=excluded.store_id,"
                " matters=excluded.matters, valid_from=excluded.valid_from,"
                " valid_to=excluded.valid_to, revoked=excluded.revoked",
                (
                    credential.pharmacist_id,
                    credential.store_id,
                    _dumps(credential.matters),
                    credential.valid_from,
                    credential.valid_to,
                    int(credential.revoked),
                ),
            )

        self.eventful(
            actor_id,
            "credential.upsert",
            {
                "pharmacist_id": credential.pharmacist_id,
                "store_id": credential.store_id,
                "matters": list(credential.matters),
                "revoked": credential.revoked,
            },
            mutate,
        )

    def get_credential(self, pharmacist_id: str) -> Credential | None:
        row = self.connection.execute(
            "SELECT * FROM credentials WHERE pharmacist_id=?", (pharmacist_id,)
        ).fetchone()
        if not row:
            return None
        return Credential(
            pharmacist_id=row["pharmacist_id"],
            store_id=row["store_id"],
            matters=_loads(row["matters"]),
            valid_from=row["valid_from"],
            valid_to=row["valid_to"],
            revoked=bool(row["revoked"]),
        )

    # ------------------------------------------------------------------
    # 药品计划
    # ------------------------------------------------------------------
    def add_plan_conn(self, conn: sqlite3.Connection, plan: MedicationPlan) -> None:
        conn.execute(
            "INSERT INTO plans(plan_id, customer_id, source_kind, source_ref,"
            " created_at, active) VALUES(?,?,?,?,?,?)",
            (
                plan.plan_id,
                plan.customer_id,
                plan.source_kind,
                plan.source_ref,
                plan.created_at,
                int(plan.active),
            ),
        )

    def get_plan(self, plan_id: str) -> MedicationPlan | None:
        row = self.connection.execute(
            "SELECT * FROM plans WHERE plan_id=?", (plan_id,)
        ).fetchone()
        if not row:
            return None
        return MedicationPlan(
            plan_id=row["plan_id"],
            customer_id=row["customer_id"],
            source_kind=row["source_kind"],
            source_ref=row["source_ref"],
            created_at=row["created_at"],
            active=bool(row["active"]),
        )

    def list_plans(self, customer_id: str) -> list[MedicationPlan]:
        rows = self.connection.execute(
            "SELECT * FROM plans WHERE customer_id=? ORDER BY created_at",
            (customer_id,),
        ).fetchall()
        return [
            MedicationPlan(
                plan_id=row["plan_id"],
                customer_id=row["customer_id"],
                source_kind=row["source_kind"],
                source_ref=row["source_ref"],
                created_at=row["created_at"],
                active=bool(row["active"]),
            )
            for row in rows
        ]

    # ------------------------------------------------------------------
    # 提醒任务
    # ------------------------------------------------------------------
    @staticmethod
    def _insert_task_conn(conn: sqlite3.Connection, task: ReminderTask) -> None:
        conn.execute(
            "INSERT INTO tasks(task_id, plan_id, customer_id, matter, due_at, state,"
            " store_id, pharmacist_id, sent_at, skip_reason, revision)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                task.task_id,
                task.plan_id,
                task.customer_id,
                task.matter,
                task.due_at,
                task.state,
                task.store_id,
                task.pharmacist_id,
                task.sent_at,
                task.skip_reason,
                task.revision,
            ),
        )

    def add_task(self, task: ReminderTask) -> None:
        with self.connection:
            self._insert_task_conn(self.connection, task)

    @staticmethod
    def _save_task_conn(conn: sqlite3.Connection, task: ReminderTask) -> None:
        conn.execute(
            "UPDATE tasks SET plan_id=?, customer_id=?, matter=?, due_at=?, state=?,"
            " store_id=?, pharmacist_id=?, sent_at=?, skip_reason=?, revision=?"
            " WHERE task_id=?",
            (
                task.plan_id,
                task.customer_id,
                task.matter,
                task.due_at,
                task.state,
                task.store_id,
                task.pharmacist_id,
                task.sent_at,
                task.skip_reason,
                task.revision,
                task.task_id,
            ),
        )

    def get_task(self, task_id: str) -> ReminderTask | None:
        row = self.connection.execute(
            "SELECT * FROM tasks WHERE task_id=?", (task_id,)
        ).fetchone()
        return self._task(row) if row else None

    def list_tasks(
        self,
        customer_id: str | None = None,
        states: tuple[str, ...] | None = None,
        store_id: str | None = None,
        due_at_or_before: str | None = None,
    ) -> list[ReminderTask]:
        clauses: list[str] = []
        params: list[object] = []
        if customer_id is not None:
            clauses.append("customer_id=?")
            params.append(customer_id)
        if states:
            clauses.append(f"state IN ({','.join('?' for _ in states)})")
            params.extend(states)
        if store_id is not None:
            clauses.append("store_id=?")
            params.append(store_id)
        if due_at_or_before is not None:
            clauses.append("due_at<=?")
            params.append(due_at_or_before)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = self.connection.execute(
            f"SELECT * FROM tasks{where} ORDER BY due_at, task_id", params
        ).fetchall()
        return [self._task(row) for row in rows]

    @staticmethod
    def _task(row: sqlite3.Row) -> ReminderTask:
        return ReminderTask(
            task_id=row["task_id"],
            plan_id=row["plan_id"],
            customer_id=row["customer_id"],
            matter=row["matter"],
            due_at=row["due_at"],
            state=row["state"],
            store_id=row["store_id"],
            pharmacist_id=row["pharmacist_id"],
            sent_at=row["sent_at"],
            skip_reason=row["skip_reason"],
            revision=row["revision"],
        )

    # ------------------------------------------------------------------
    # 依从反馈（幂等键）
    # ------------------------------------------------------------------
    @staticmethod
    def _insert_feedback_conn(
        conn: sqlite3.Connection, feedback: AdherenceFeedback
    ) -> None:
        conn.execute(
            "INSERT INTO feedback(feedback_id, customer_id, plan_id, content,"
            " received_at, idempotency_key) VALUES(?,?,?,?,?,?)",
            (
                feedback.feedback_id,
                feedback.customer_id,
                feedback.plan_id,
                feedback.content,
                feedback.received_at,
                feedback.idempotency_key,
            ),
        )

    def get_feedback_by_key(self, idempotency_key: str) -> AdherenceFeedback | None:
        row = self.connection.execute(
            "SELECT * FROM feedback WHERE idempotency_key=?", (idempotency_key,)
        ).fetchone()
        if not row:
            return None
        return AdherenceFeedback(
            feedback_id=row["feedback_id"],
            customer_id=row["customer_id"],
            plan_id=row["plan_id"],
            content=row["content"],
            received_at=row["received_at"],
            idempotency_key=row["idempotency_key"],
        )

    # ------------------------------------------------------------------
    # 异常
    # ------------------------------------------------------------------
    @staticmethod
    def _insert_anomaly_conn(conn: sqlite3.Connection, anomaly: Anomaly) -> None:
        conn.execute(
            "INSERT INTO anomalies(anomaly_id, customer_id, signals, score, state,"
            " opened_by, opened_at, closed_by, closed_at, closure_note)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                anomaly.anomaly_id,
                anomaly.customer_id,
                _dumps(anomaly.signals),
                anomaly.score,
                anomaly.state,
                anomaly.opened_by,
                anomaly.opened_at,
                anomaly.closed_by,
                anomaly.closed_at,
                anomaly.closure_note,
            ),
        )

    @staticmethod
    def _save_anomaly_conn(conn: sqlite3.Connection, anomaly: Anomaly) -> None:
        conn.execute(
            "UPDATE anomalies SET signals=?, score=?, state=?, opened_by=?,"
            " opened_at=?, closed_by=?, closed_at=?, closure_note=? WHERE anomaly_id=?",
            (
                _dumps(anomaly.signals),
                anomaly.score,
                anomaly.state,
                anomaly.opened_by,
                anomaly.opened_at,
                anomaly.closed_by,
                anomaly.closed_at,
                anomaly.closure_note,
                anomaly.anomaly_id,
            ),
        )

    def get_anomaly(self, anomaly_id: str) -> Anomaly | None:
        row = self.connection.execute(
            "SELECT * FROM anomalies WHERE anomaly_id=?", (anomaly_id,)
        ).fetchone()
        return self._anomaly(row) if row else None

    @staticmethod
    def _anomaly(row: sqlite3.Row) -> Anomaly:
        return Anomaly(
            anomaly_id=row["anomaly_id"],
            customer_id=row["customer_id"],
            signals=_loads(row["signals"]),
            score=row["score"],
            state=row["state"],
            opened_by=row["opened_by"],
            opened_at=row["opened_at"],
            closed_by=row["closed_by"],
            closed_at=row["closed_at"],
            closure_note=row["closure_note"],
        )

    # ------------------------------------------------------------------
    # 转诊
    # ------------------------------------------------------------------
    @staticmethod
    def _insert_referral_conn(conn: sqlite3.Connection, referral: Referral) -> None:
        conn.execute(
            "INSERT INTO referrals(referral_id, customer_id, anomaly_id, target_kind,"
            " status, created_at, updated_at) VALUES(?,?,?,?,?,?,?)",
            (
                referral.referral_id,
                referral.customer_id,
                referral.anomaly_id,
                referral.target_kind,
                referral.status,
                referral.created_at,
                referral.updated_at,
            ),
        )

    @staticmethod
    def _save_referral_conn(conn: sqlite3.Connection, referral: Referral) -> None:
        conn.execute(
            "UPDATE referrals SET status=?, updated_at=? WHERE referral_id=?",
            (referral.status, referral.updated_at, referral.referral_id),
        )

    def get_referral(self, referral_id: str) -> Referral | None:
        row = self.connection.execute(
            "SELECT * FROM referrals WHERE referral_id=?", (referral_id,)
        ).fetchone()
        return Referral(**dict(row)) if row else None

    def list_referrals(self, customer_id: str) -> list[Referral]:
        rows = self.connection.execute(
            "SELECT * FROM referrals WHERE customer_id=? ORDER BY created_at",
            (customer_id,),
        ).fetchall()
        return [Referral(**dict(row)) for row in rows]

    # ------------------------------------------------------------------
    # 应急保供批次
    # ------------------------------------------------------------------
    @staticmethod
    def _insert_batch_conn(conn: sqlite3.Connection, batch: SupplyBatch) -> None:
        conn.execute(
            "INSERT INTO batches(batch_id, customer_id, plan_id, status,"
            " owner_store_id, created_at, updated_at) VALUES(?,?,?,?,?,?,?)",
            (
                batch.batch_id,
                batch.customer_id,
                batch.plan_id,
                batch.status,
                batch.owner_store_id,
                batch.created_at,
                batch.updated_at,
            ),
        )

    @staticmethod
    def _save_batch_conn(conn: sqlite3.Connection, batch: SupplyBatch) -> None:
        conn.execute(
            "UPDATE batches SET status=?, owner_store_id=?, updated_at=?"
            " WHERE batch_id=?",
            (batch.status, batch.owner_store_id, batch.updated_at, batch.batch_id),
        )

    def get_batch(self, batch_id: str) -> SupplyBatch | None:
        row = self.connection.execute(
            "SELECT * FROM batches WHERE batch_id=?", (batch_id,)
        ).fetchone()
        return SupplyBatch(**dict(row)) if row else None

    def list_batches(self, customer_id: str) -> list[SupplyBatch]:
        rows = self.connection.execute(
            "SELECT * FROM batches WHERE customer_id=? ORDER BY created_at",
            (customer_id,),
        ).fetchall()
        return [SupplyBatch(**dict(row)) for row in rows]

    # ------------------------------------------------------------------
    # 门店交接
    # ------------------------------------------------------------------
    @staticmethod
    def _insert_handover_conn(conn: sqlite3.Connection, handover: StoreHandover) -> None:
        conn.execute(
            "INSERT INTO handovers(handover_id, customer_id, from_store_id,"
            " to_store_id, status, created_at, accepted_at) VALUES(?,?,?,?,?,?,?)",
            (
                handover.handover_id,
                handover.customer_id,
                handover.from_store_id,
                handover.to_store_id,
                handover.status,
                handover.created_at,
                handover.accepted_at,
            ),
        )

    @staticmethod
    def _save_handover_conn(conn: sqlite3.Connection, handover: StoreHandover) -> None:
        conn.execute(
            "UPDATE handovers SET status=?, accepted_at=? WHERE handover_id=?",
            (handover.status, handover.accepted_at, handover.handover_id),
        )

    def get_handover(self, handover_id: str) -> StoreHandover | None:
        row = self.connection.execute(
            "SELECT * FROM handovers WHERE handover_id=?", (handover_id,)
        ).fetchone()
        return StoreHandover(**dict(row)) if row else None

    def list_handovers(self, customer_id: str) -> list[StoreHandover]:
        rows = self.connection.execute(
            "SELECT * FROM handovers WHERE customer_id=? ORDER BY created_at",
            (customer_id,),
        ).fetchall()
        return [StoreHandover(**dict(row)) for row in rows]

    def pending_handover_for_store(
        self, customer_id: str, store_id: str
    ) -> StoreHandover | None:
        """该顾客从指定门店转出、且尚未承接的交接。"""
        rows = self.connection.execute(
            "SELECT * FROM handovers WHERE customer_id=? AND from_store_id=?"
            " AND status=? ORDER BY created_at DESC",
            (customer_id, store_id, "pending"),
        ).fetchall()
        return StoreHandover(**dict(rows[0])) if rows else None

    # ------------------------------------------------------------------
    # 标识画像与隐私隔离
    # ------------------------------------------------------------------
    @staticmethod
    def _insert_profile_conn(conn: sqlite3.Connection, profile: IdentityProfile) -> int:
        cur = conn.execute(
            "INSERT OR IGNORE INTO profiles(customer_id, first_content_hash,"
            " first_content_preview, created_at) VALUES(?,?,?,?)",
            (
                profile.customer_id,
                profile.first_content_hash,
                profile.first_content_preview,
                profile.created_at,
            ),
        )
        return cur.rowcount

    def get_profile(self, customer_id: str) -> IdentityProfile | None:
        row = self.connection.execute(
            "SELECT * FROM profiles WHERE customer_id=?", (customer_id,)
        ).fetchone()
        if not row:
            return None
        return IdentityProfile(
            customer_id=row["customer_id"],
            first_content_hash=row["first_content_hash"],
            first_content_preview=row["first_content_preview"],
            created_at=row["created_at"],
        )

    @staticmethod
    def _replace_profile_conn(
        conn: sqlite3.Connection, profile: IdentityProfile
    ) -> None:
        conn.execute(
            "UPDATE profiles SET first_content_hash=?, first_content_preview=?,"
            " created_at=? WHERE customer_id=?",
            (
                profile.first_content_hash,
                profile.first_content_preview,
                profile.created_at,
                profile.customer_id,
            ),
        )

    @staticmethod
    def _insert_quarantine_conn(
        conn: sqlite3.Connection, case: QuarantineCase
    ) -> None:
        conn.execute(
            "INSERT INTO quarantine_cases(case_id, customer_id, incoming_hash,"
            " incoming_preview, status, opened_at, reviewer, reviewed_at, decision)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (
                case.case_id,
                case.customer_id,
                case.incoming_hash,
                case.incoming_preview,
                case.status,
                case.opened_at,
                case.reviewer,
                case.reviewed_at,
                case.decision,
            ),
        )

    @staticmethod
    def _save_quarantine_conn(conn: sqlite3.Connection, case: QuarantineCase) -> None:
        conn.execute(
            "UPDATE quarantine_cases SET status=?, reviewer=?, reviewed_at=?,"
            " decision=? WHERE case_id=?",
            (case.status, case.reviewer, case.reviewed_at, case.decision, case.case_id),
        )

    def get_quarantine(self, case_id: str) -> QuarantineCase | None:
        row = self.connection.execute(
            "SELECT * FROM quarantine_cases WHERE case_id=?", (case_id,)
        ).fetchone()
        return self._quarantine(row) if row else None

    def list_open_quarantine(self, customer_id: str | None = None) -> list[QuarantineCase]:
        if customer_id is None:
            rows = self.connection.execute(
                "SELECT * FROM quarantine_cases WHERE status=? ORDER BY opened_at",
                ("held",),
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT * FROM quarantine_cases WHERE status=? AND customer_id=?"
                " ORDER BY opened_at",
                ("held", customer_id),
            ).fetchall()
        return [self._quarantine(row) for row in rows]

    @staticmethod
    def _quarantine(row: sqlite3.Row) -> QuarantineCase:
        return QuarantineCase(
            case_id=row["case_id"],
            customer_id=row["customer_id"],
            incoming_hash=row["incoming_hash"],
            incoming_preview=row["incoming_preview"],
            status=row["status"],
            opened_at=row["opened_at"],
            reviewer=row["reviewer"],
            reviewed_at=row["reviewed_at"],
            decision=row["decision"],
        )
