"""药店健康陪伴边界的领域记录。

本模块只承载数据结构与少量纯函数，不做权限判断与持久化。
所有时间均为 UTC ISO-8601 字符串；授权范围/渠道统一以规范常量表示。

药店（含本系统）只做用药陪伴与合规留痕，不产生诊断或治疗结论，
任何领域记录里出现的健康描述都只能是顾客自述或外部医疗文书的转述。
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Iterable

# ---------------------------------------------------------------------------
# 角色
# ---------------------------------------------------------------------------
ROLE_CUSTOMER = "customer"          # 顾客
ROLE_PHARMACIST = "pharmacist"      # 药师
ROLE_COMPLIANCE = "compliance"      # 合规人员
ROLE_SALES = "sales"                # 销售目标负责人
ROLE_SYSTEM = "system"              # 规则引擎/定时任务

ALL_ROLES = frozenset(
    {ROLE_CUSTOMER, ROLE_PHARMACIST, ROLE_COMPLIANCE, ROLE_SALES, ROLE_SYSTEM}
)

# ---------------------------------------------------------------------------
# 授权与执业范围（同一套事项码，授权与资质都必须覆盖当前事项）
# ---------------------------------------------------------------------------
SCOPE_MED_REMINDER = "med_reminder"   # 用药提醒
SCOPE_ADHERENCE = "adherence"         # 依从反馈处理
SCOPE_RISK_REVIEW = "risk_review"     # 异常信号处置
SCOPE_REFERRAL = "referral"           # 转诊建议与跟进
SCOPE_SUPPLY = "supply"               # 应急保供
SCOPE_HANDOFF = "handoff"             # 门店交接
SCOPE_PLAN = "med_plan"               # 药品计划维护

CANONICAL_SCOPES = frozenset(
    {
        SCOPE_MED_REMINDER,
        SCOPE_ADHERENCE,
        SCOPE_RISK_REVIEW,
        SCOPE_REFERRAL,
        SCOPE_SUPPLY,
        SCOPE_HANDOFF,
        SCOPE_PLAN,
    }
)

# 联系渠道
CHANNEL_SMS = "sms"
CHANNEL_APP = "app"
CHANNEL_CALL = "call"
CANONICAL_CHANNELS = frozenset({CHANNEL_SMS, CHANNEL_APP, CHANNEL_CALL})


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_set(values: Iterable[str] | None) -> frozenset[str]:
    if not values:
        return frozenset()
    return frozenset(str(v).strip() for v in values if str(v).strip())


# ---------------------------------------------------------------------------
# 兼容旧版骨架的最小记录
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Record:
    record_id: str
    owner_id: str
    state: str
    revision: int = 1
    created_at: str = ""

    def stamped(self) -> "Record":
        value = self.created_at or now_iso()
        return replace(self, created_at=value)


# ---------------------------------------------------------------------------
# 1) 顾客授权版本（撤回只冻结未来联系，行不删除）
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ConsentGrant:
    customer_id: str
    version: int
    scopes: frozenset[str]
    channels: frozenset[str]
    granted_at: str
    revoked_at: str | None = None
    revoked_reason: str = ""
    # 法规要求保留的最小事实字段白名单（撤回后合规仍可查看）
    retained_fields: frozenset[str] = frozenset(
        {"customer_id", "version", "scopes", "granted_at", "revoked_at", "revoked_reason"}
    )

    @property
    def active(self) -> bool:
        return self.revoked_at is None

    def covers(self, scope: str, channel: str | None = None) -> bool:
        if not self.active:
            return False
        if scope not in self.scopes:
            return False
        if channel is not None and channel not in self.channels:
            return False
        return True


# ---------------------------------------------------------------------------
# 2) 药师资质（执业范围 + 有效期 + 所属门店）
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PharmacistCredential:
    pharmacist_id: str
    store_id: str
    license_no: str
    scopes: frozenset[str]
    valid_from: str
    valid_to: str            # ISO 字符串，闭区间
    active: bool = True

    def covers(self, scope: str, at: str, store_id: str | None = None) -> bool:
        if not self.active:
            return False
        if scope not in self.scopes:
            return False
        if not (self.valid_from <= at <= self.valid_to):
            return False
        if store_id is not None and self.store_id != store_id:
            return False
        return True


# ---------------------------------------------------------------------------
# 3) 药品计划（修订版本化；计划来源随计划固定）
# ---------------------------------------------------------------------------
PLAN_DRAFT = "draft"
PLAN_ACTIVE = "active"
PLAN_PAUSED = "paused"      # 风险阈值触发，等待人工
PLAN_CLOSED = "closed"


@dataclass(frozen=True)
class MedicationPlan:
    plan_id: str
    customer_id: str
    store_id: str
    pharmacist_id: str
    medications: tuple[str, ...]
    revision: int
    consent_version: int          # 创建/调整时所依据的授权版本
    status: str = PLAN_ACTIVE
    created_at: str = ""
    updated_at: str = ""

    def stamped(self) -> "MedicationPlan":
        ts = self.created_at or now_iso()
        return replace(self, created_at=ts, updated_at=self.updated_at or ts)


# ---------------------------------------------------------------------------
# 4) 提醒（未发送可调整；已发送即冻结，不得改写）
# ---------------------------------------------------------------------------
REMINDER_PENDING = "pending"
REMINDER_SENT = "sent"
REMINDER_CANCELLED = "cancelled"
REMINDER_BLOCKED = "blocked"    # 因授权撤回/资质缺失/隔离而未发送


@dataclass(frozen=True)
class Reminder:
    reminder_id: str
    plan_id: str
    customer_id: str
    medication: str
    scheduled_at: str
    channel: str
    plan_revision: int
    consent_version: int
    status: str = REMINDER_PENDING
    sent_at: str | None = None
    # 未提醒原因码（见 policy.REMINDER_SKIP_*）
    skip_reason: str = ""
    created_at: str = ""

    def stamped(self) -> "Reminder":
        return replace(self, created_at=self.created_at or now_iso())

    @property
    def frozen(self) -> bool:
        """已发送提醒属于过去事实，任何新信息都不得改写。"""
        return self.status == REMINDER_SENT


# ---------------------------------------------------------------------------
# 5) 依从反馈（同一反馈幂等键沿用原回执）
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class AdherenceFeedback:
    feedback_id: str
    customer_id: str
    plan_id: str
    # 顾客自述的标准化码，如 taken / missed / side_effect / concern；
    # 不是诊断，药师不得据此刻画诊断结论。
    content_code: str
    detail: str
    reported_at: str
    idempotency_key: str
    receipt_no: str = ""
    duplicate_of: str = ""    # 重复反馈指向首条回执编号


# ---------------------------------------------------------------------------
# 6) 异常信号（销售角色无权关闭）
# ---------------------------------------------------------------------------
SIGNAL_OPEN = "open"
SIGNAL_PAUSED = "paused"        # 已自动建议暂停并转人工
SIGNAL_REFERRED = "referred"
SIGNAL_CLOSED = "closed"


@dataclass(frozen=True)
class AnomalySignal:
    signal_id: str
    customer_id: str
    plan_id: str
    rule_code: str
    severity: int               # 1..5，阈值由规则配置决定
    detail: str
    status: str
    opened_by: str
    opened_at: str
    closed_by: str = ""
    closed_at: str = ""
    closure_note: str = ""


# ---------------------------------------------------------------------------
# 7) 转诊建议（进度对权限内角色可见）
# ---------------------------------------------------------------------------
REFERRAL_SUGGESTED = "suggested"
REFERRAL_ACCEPTED = "accepted"
REFERRAL_COMPLETED = "completed"
REFERRAL_DECLINED = "declined"


@dataclass(frozen=True)
class Referral:
    referral_id: str
    signal_id: str
    customer_id: str
    target: str                 # 建议就诊科室/机构，非诊断
    reason_code: str
    status: str
    suggested_by: str
    created_at: str
    updated_at: str = ""
    progress_note: str = ""


# ---------------------------------------------------------------------------
# 8) 应急保供批次（责任落到具体门店，交接时转移）
# ---------------------------------------------------------------------------
BATCH_RESERVED = "reserved"
BATCH_IN_TRANSIT = "in_transit"
BATCH_DELIVERED = "delivered"
BATCH_RELEASED = "released"


@dataclass(frozen=True)
class SupplyBatch:
    batch_id: str
    customer_id: str
    plan_id: str
    medication: str
    quantity: int
    responsible_store_id: str
    backup_store_id: str
    status: str
    due_at: str
    created_at: str = ""
    delivered_at: str | None = None

    def stamped(self) -> "SupplyBatch":
        return replace(self, created_at=self.created_at or now_iso())


# ---------------------------------------------------------------------------
# 9) 门店交接（服务进程中断后凭它续跑）
# ---------------------------------------------------------------------------
HANDOFF_SCHEDULED = "scheduled"
HANDOFF_COMPLETED = "completed"
HANDOFF_CANCELLED = "cancelled"


@dataclass(frozen=True)
class StoreHandoff:
    handoff_id: str
    customer_id: str
    plan_id: str
    from_store_id: str
    to_store_id: str
    to_pharmacist_id: str          # 新门店承接药师（须具备相应资质）
    initiated_by: str
    status: str
    effective_at: str
    created_at: str = ""
    completed_at: str | None = None
    note: str = ""

    def stamped(self) -> "StoreHandoff":
        return replace(self, created_at=self.created_at or now_iso())


# ---------------------------------------------------------------------------
# 10) 标识隔离案卷（同一标识出现不同健康内容：先隔离，后隐私复核）
# ---------------------------------------------------------------------------
QUARANTINE_QUARANTINED = "quarantined"
QUARANTINE_RELEASED = "released"
QUARANTINE_BLOCKED = "blocked"


@dataclass(frozen=True)
class QuarantineCase:
    case_id: str
    identifier: str                 # 手机号/会员号等共享标识
    fragment_count: int
    status: str
    created_at: str
    reviewed_by: str = ""
    reviewed_at: str = ""
    review_note: str = ""


# ---------------------------------------------------------------------------
# 11) 审计事件（哈希链：同一条链覆盖全部实体）
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class AuditEvent:
    seq: int
    at: str
    actor_id: str
    actor_role: str
    action: str
    entity_type: str
    entity_id: str
    payload: dict[str, Any]
    prev_hash: str
    entry_hash: str
