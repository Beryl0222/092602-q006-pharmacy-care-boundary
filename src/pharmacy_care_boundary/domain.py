"""药店健康陪伴边界台的领域词汇与规则内核。

本模块只定义"边界"：谁在什么授权版本下、凭什么资质、对哪一类事项
可以做什么，以及风险出现时系统如何处置。任何输出都不允许出现系统
自行生成的诊断或治疗结论，药店只提供用药提醒、依从陪伴与转诊协助。
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# 角色
# ---------------------------------------------------------------------------
ROLE_CUSTOMER = "customer"          # 顾客：查看自己的计划与提醒原因
ROLE_PHARMACIST = "pharmacist"      # 药师：在资质与授权双覆盖下处理记录
ROLE_COMPLIANCE = "compliance"      # 合规：审计链、隐私复核、最小事实
ROLE_SALES = "sales"                # 销售目标负责人：无权关闭异常

ROLES = (ROLE_CUSTOMER, ROLE_PHARMACIST, ROLE_COMPLIANCE, ROLE_SALES)

# ---------------------------------------------------------------------------
# 事项类型：药师执业范围与顾客授权范围都按同一词汇表达，便于做交集
# ---------------------------------------------------------------------------
MATTER_MEDICATION_REMINDER = "medication_reminder"   # 用药提醒
MATTER_ADHERENCE_REVIEW = "adherence_review"         # 依从反馈回访
MATTER_REFERRAL = "referral"                         # 转诊协助
MATTER_EMERGENCY_SUPPLY = "emergency_supply"         # 应急保供
MATTER_COUNSELING = "counseling"                     # 用药咨询

MATTERS = (
    MATTER_MEDICATION_REMINDER,
    MATTER_ADHERENCE_REVIEW,
    MATTER_REFERRAL,
    MATTER_EMERGENCY_SUPPLY,
    MATTER_COUNSELING,
)

# ---------------------------------------------------------------------------
# 授权状态
# ---------------------------------------------------------------------------
CONSENT_GRANTED = "granted"        # 当前有效
CONSENT_WITHDRAWN = "withdrawn"    # 已撤回：停止一切未来联系

# ---------------------------------------------------------------------------
# 提醒任务状态机：新信息只调整"未发送"任务，已发送记录永不改写
# ---------------------------------------------------------------------------
TASK_SCHEDULED = "scheduled"        # 未发送、可调整
TASK_SENT = "sent"                  # 已发送：历史事实，冻结
TASK_SKIPPED = "skipped"            # 未发送而被取消（撤回/无覆盖/隔离）
TASK_PAUSED = "paused"              # 风险规则自动暂停，等待人工

SKIP_CONSENT_WITHDRAWN = "consent_withdrawn"       # 授权已撤回
SKIP_SCOPE_NOT_COVERED = "scope_not_covered"       # 资质或授权不覆盖
SKIP_QUARANTINED = "quarantined"                   # 同标识异内容隔离中
SKIP_PLAN_ADJUSTED = "plan_adjusted"               # 新信息调整，原计划任务取消
SKIP_HANDOVER_PENDING = "handover_pending"         # 门店交接尚未承接

# 可调的状态只有这一个
ADJUSTABLE_TASK_STATES = frozenset({TASK_SCHEDULED})

# ---------------------------------------------------------------------------
# 异常信号
# ---------------------------------------------------------------------------
ANOMALY_OPEN = "open"
ANOMALY_AUTO_PAUSED = "auto_paused"  # 达阈值：系统自动建议暂停并转人工
ANOMALY_ESCALATED = "escalated"     # 已转交人工
ANOMALY_CLOSED = "closed"           # 仅合规角色可关闭，销售无权

# 关闭异常的角色
ANOMALY_CLOSER_ROLES = frozenset({ROLE_COMPLIANCE})

# ---------------------------------------------------------------------------
# 转诊
# ---------------------------------------------------------------------------
REFERRAL_SUGGESTED = "suggested"
REFERRAL_ACCEPTED = "accepted"
REFERRAL_IN_PROGRESS = "in_progress"
REFERRAL_DONE = "done"
REFERRAL_DECLINED = "declined"

# ---------------------------------------------------------------------------
# 应急保供批次
# ---------------------------------------------------------------------------
BATCH_PLANNED = "planned"
BATCH_ALLOCATED = "allocated"
BATCH_DISPATCHED = "dispatched"
BATCH_DELIVERED = "delivered"

# ---------------------------------------------------------------------------
# 门店交接
# ---------------------------------------------------------------------------
HANDOVER_PENDING = "pending"
HANDOVER_ACCEPTED = "accepted"

# ---------------------------------------------------------------------------
# 同标识异内容隔离（隐私复核）
# ---------------------------------------------------------------------------
QUARANTINE_HELD = "held"
QUARANTINE_RELEASED = "released"
QUARANTINE_REJECTED = "rejected"

# 隐私复核结论
REVIEW_FIRST_WINS = "first_wins"        # 以首次登记的健康内容为准
REVIEW_MERGE = "merge"                  # 确认属同一人，合并
REVIEW_REJECT_DUPLICATE = "reject_dup"  # 判定为错误标识，拒绝后者

# ---------------------------------------------------------------------------
# 风险阈值：信号分值达到阈值即自动建议暂停并转人工。阈值属规则，不属诊断。
# ---------------------------------------------------------------------------
RISK_THRESHOLD = 10
RISK_SIGNAL_SCORES = {
    "adverse_reaction": 10,   # 疑似不良反应
    "overdose": 10,           # 过量/误服
    "supply_gap": 6,          # 断药风险
    "repeated_missed_dose": 4,  # 连续漏服
    "conflicting_content": 8,  # 同一标识出现互相矛盾的健康内容
}

# ---------------------------------------------------------------------------
# 医疗诊断边界：系统输出中禁止出现自行生成的诊断/治疗结论。
# 仅拦截"系统生成"的断言；转述顾客自述或既有医嘱原文不在此列，
# 但服务层会把这类原文显式标注为引述，绝不由系统补全结论。
# ---------------------------------------------------------------------------
_DIAGNOSIS_PATTERNS = (
    re.compile(r"(诊断|确诊)(为|是|为是|：|:|患有|结果)"),
    re.compile(r"你(患有?|得的?是)"),
    re.compile(r"(建议|应当|应该|需要)(您)?(服用|加量|减量|停用|停药|换药|住院|手术)"),
    re.compile(r"(本病|该病|病情)(是|为|属)"),
    re.compile(r"diagnos(is|ed with)"),
    re.compile(r"\byou (have|had) (a |an )?[a-z]+ (disease|syndrome|disorder)\b"),
)

# 标准免责口径，附在所有面向顾客的响应末尾
BOUNDARY_NOTICE = "本提醒为用药依从性服务，不构成诊断或治疗建议；如有不适请及时就医。"


class BoundaryError(Exception):
    """边界规则拒绝本次操作。"""


class PermissionDenied(BoundaryError):
    """角色无权执行该操作。"""


class ScopeNotCovered(BoundaryError):
    """药师资质或顾客授权未覆盖当前事项。"""


@dataclass(frozen=True)
class Actor:
    """请求的发起方：角色 + 可选药师身份/所属门店。"""

    actor_id: str
    role: str
    pharmacist_id: str = ""
    store_id: str = ""

    def __post_init__(self) -> None:
        if self.role not in ROLES:
            raise BoundaryError(f"未知角色：{self.role}")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def risk_score(signals: list[str]) -> int:
    return sum(RISK_SIGNAL_SCORES.get(signal, 1) for signal in signals)


def risk_reached(signals: list[str]) -> bool:
    return risk_score(signals) >= RISK_THRESHOLD


def assert_no_generated_diagnosis(text: str) -> None:
    """任何系统待发送文本都必须通过该检查；命中即拒绝生成。"""
    if not text:
        return
    # 标准免责口径本身提到"诊断/治疗"字样，属边界声明而非结论，先剔除。
    checked = text.replace(BOUNDARY_NOTICE, "")
    for pattern in _DIAGNOSIS_PATTERNS:
        if pattern.search(checked):
            raise BoundaryError(
                "响应内容疑似包含自行生成的诊断/治疗结论，已拦截："
                + pattern.pattern
            )


def stable_hash(payload: dict[str, object]) -> str:
    """对结构化载荷做稳定哈希，供审计链串联。"""
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 基础登记记录（保留既有契约）
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Record:
    record_id: str
    owner_id: str
    state: str
    revision: int = 1
    created_at: str = ""

    def stamped(self) -> "Record":
        value = self.created_at or datetime.now(timezone.utc).isoformat()
        return replace(self, created_at=value)


# ---------------------------------------------------------------------------
# 领域记录（均为不可变快照；状态推进通过追加新版本/审计事件实现）
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ConsentVersion:
    """顾客授权版本。撤回即追加 withdrawn 版本，历史版本保留不删。"""

    customer_id: str
    version: int
    state: str
    scopes: tuple[str, ...]
    granted_store_id: str
    created_at: str
    note: str = ""


@dataclass(frozen=True)
class Credential:
    """药师执业资质：可处理的事项范围与有效期、所属门店。"""

    pharmacist_id: str
    store_id: str
    matters: tuple[str, ...]
    valid_from: str
    valid_to: str
    revoked: bool = False


@dataclass(frozen=True)
class MedicationPlan:
    """药品计划（来源必须是医嘱/处方，不接受系统自创）。"""

    plan_id: str
    customer_id: str
    source_kind: str          # prescription / doctor_order
    source_ref: str           # 处方或医嘱编号
    created_at: str
    active: bool = True


@dataclass(frozen=True)
class ReminderTask:
    """单条到期提醒任务。sent 后冻结，永不更新、不删除。"""

    task_id: str
    plan_id: str
    customer_id: str
    matter: str
    due_at: str
    state: str = TASK_SCHEDULED
    store_id: str = ""
    pharmacist_id: str = ""
    sent_at: str = ""
    skip_reason: str = ""
    revision: int = 1


@dataclass(frozen=True)
class AdherenceFeedback:
    """依从反馈。重复反馈沿用原回执（幂等）。"""

    feedback_id: str
    customer_id: str
    plan_id: str
    content: str
    received_at: str
    idempotency_key: str


@dataclass(frozen=True)
class Anomaly:
    """异常信号记录及处置状态。"""

    anomaly_id: str
    customer_id: str
    signals: tuple[str, ...]
    score: int
    state: str
    opened_by: str
    opened_at: str
    closed_by: str = ""
    closed_at: str = ""
    closure_note: str = ""


@dataclass(frozen=True)
class Referral:
    """转诊建议与进度。系统只建议与记录，不做诊断。"""

    referral_id: str
    customer_id: str
    anomaly_id: str
    target_kind: str
    status: str
    created_at: str
    updated_at: str = ""


@dataclass(frozen=True)
class SupplyBatch:
    """应急保供批次及其对顾客的责任归属。"""

    batch_id: str
    customer_id: str
    plan_id: str
    status: str
    owner_store_id: str
    created_at: str
    updated_at: str = ""


@dataclass(frozen=True)
class StoreHandover:
    """门店交接：承接后到期任务在新门店继续运行。"""

    handover_id: str
    customer_id: str
    from_store_id: str
    to_store_id: str
    status: str
    created_at: str
    accepted_at: str = ""


@dataclass(frozen=True)
class IdentityProfile:
    """顾客标识与其首次登记的健康内容指纹，用于同标识异内容检测。"""

    customer_id: str
    first_content_hash: str
    first_content_preview: str
    created_at: str


@dataclass(frozen=True)
class QuarantineCase:
    """同一标识出现不同健康内容时的隔离记录，等待隐私复核。"""

    case_id: str
    customer_id: str
    incoming_hash: str
    incoming_preview: str
    status: str
    opened_at: str
    reviewer: str = ""
    reviewed_at: str = ""
    decision: str = ""


@dataclass(frozen=True)
class AuditEntry:
    """审计链上的一个区块：prev_hash + 本块载荷哈希。"""

    seq: int
    at: str
    actor_id: str
    action: str
    payload: dict[str, object] = field(default_factory=dict)
    prev_hash: str = ""
    entry_hash: str = ""

    def sealed(self, seq: int, prev_hash: str) -> "AuditEntry":
        body = {
            "seq": seq,
            "at": self.at,
            "actor_id": self.actor_id,
            "action": self.action,
            "payload": self.payload,
            "prev_hash": prev_hash,
        }
        return replace(
            self, seq=seq, prev_hash=prev_hash, entry_hash=stable_hash(body)
        )
