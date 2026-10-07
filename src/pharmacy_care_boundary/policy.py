"""健康陪伴边界的策略层：全部"能不能做、何时该停"的规则集中于此。

核心原则：
1. 药师处理任何记录都必须同时满足 资质覆盖（事项/有效期/门店）与
   顾客授权覆盖（事项/渠道/未撤回），缺一不可。
2. 风险达到阈值只"建议立即暂停并转交人工"，系统不做诊疗决定。
3. 药店不是诊疗方：系统输出只允许结构化事项码与转诊建议，
   不得出现自行生成的诊断结论。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from . import domain as d

# ---------------------------------------------------------------------------
# 未提醒原因码（向顾客/药师解释"为什么没收到提醒"）
# ---------------------------------------------------------------------------
SKIP_CONSENT_REVOKED = "consent_revoked"        # 授权已撤回
SKIP_CONSENT_SCOPE = "consent_scope_missing"    # 授权版本未覆盖该事项
SKIP_CONSENT_CHANNEL = "consent_channel_missing"  # 授权渠道未覆盖
SKIP_CONSENT_VERSION = "consent_version_stale"  # 计划依据的授权版本已被新版本取代
SKIP_CREDENTIAL_SCOPE = "credential_scope_missing"  # 药师执业范围不覆盖
SKIP_CREDENTIAL_EXPIRED = "credential_expired"  # 药师资质过期/停用
SKIP_CREDENTIAL_STORE = "credential_store_mismatch"  # 药师不属于当前承接门店
SKIP_QUARANTINE = "identity_quarantined"        # 标识隔离复核中
SKIP_PLAN_PAUSED = "plan_paused"                # 计划已被风险规则暂停
SKIP_PLAN_CLOSED = "plan_closed"

# ---------------------------------------------------------------------------
# 访问判定
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str = ""

    def __bool__(self) -> bool:
        return self.allowed


def can_handle(
    *,
    scope: str,
    pharmacist: d.PharmacistCredential | None,
    consent: d.ConsentGrant | None,
    at: str,
    channel: str | None = None,
    store_id: str | None = None,
    consent_version: int | None = None,
) -> Decision:
    """资质与授权双重覆盖判定。先查授权，再查资质。"""
    if consent is None:
        return Decision(False, SKIP_CONSENT_REVOKED)
    if consent_version is not None and consent.version != consent_version:
        # 计划必须挂在当前授权版本上；旧版本计划需经人工复核后续跑
        if not consent.active:
            return Decision(False, SKIP_CONSENT_REVOKED)
        return Decision(False, SKIP_CONSENT_VERSION)
    if not consent.active:
        return Decision(False, SKIP_CONSENT_REVOKED)
    if scope not in consent.scopes:
        return Decision(False, SKIP_CONSENT_SCOPE)
    if channel is not None and channel not in consent.channels:
        return Decision(False, SKIP_CONSENT_CHANNEL)

    if pharmacist is None:
        return Decision(False, SKIP_CREDENTIAL_SCOPE)
    if not pharmacist.active:
        return Decision(False, SKIP_CREDENTIAL_EXPIRED)
    if scope not in pharmacist.scopes:
        return Decision(False, SKIP_CREDENTIAL_SCOPE)
    if not (pharmacist.valid_from <= at <= pharmacist.valid_to):
        return Decision(False, SKIP_CREDENTIAL_EXPIRED)
    if store_id is not None and pharmacist.store_id != store_id:
        return Decision(False, SKIP_CREDENTIAL_STORE)
    return Decision(True)


# ---------------------------------------------------------------------------
# 风险规则
# ---------------------------------------------------------------------------
AUTO_PAUSE_THRESHOLD = 4      # severity 达到该值：建议暂停 + 转人工
ESCALATE_ONLY_THRESHOLD = 3   # 达到该值：转人工但不暂停


@dataclass(frozen=True)
class RiskAssessment:
    rule_code: str
    severity: int
    should_pause: bool
    should_escalate: bool
    referral_target: str
    reason_code: str
    detail: str


# 顾客反馈码 → 规则（只识别顾客自述，不推断疾病）
_FEEDBACK_RULES = {
    "side_effect": RiskAssessment(
        rule_code="side_effect_report", severity=4,
        should_pause=True, should_escalate=True,
        referral_target="医师/原处方医疗机构",
        reason_code="adverse_reaction_self_report",
        detail="顾客自述用药后不适，建议暂停发送常规提醒并转人工跟进",
    ),
    "missed": RiskAssessment(
        rule_code="adherence_gap", severity=3,
        should_pause=False, should_escalate=True,
        referral_target="药师人工随访",
        reason_code="repeated_missed_dose",
        detail="依从反馈显示多次漏服，转药师人工随访",
    ),
    "concern": RiskAssessment(
        rule_code="customer_concern", severity=4,
        should_pause=True, should_escalate=True,
        referral_target="医师/原处方医疗机构",
        reason_code="customer_raised_concern",
        detail="顾客主动表达担忧，建议暂停并由人工接管",
    ),
}


def assess_feedback(content_code: str, miss_count: int = 1) -> RiskAssessment | None:
    """根据顾客自述反馈码评估风险。返回 None 表示未命中任何风险规则。"""
    rule = _FEEDBACK_RULES.get(content_code)
    if rule is None:
        return None
    if content_code == "missed" and miss_count < 3:
        # 偶发漏服不升级；连续三次才转人工
        return None
    if content_code == "missed" and miss_count >= 3:
        return rule
    return rule


def threshold_reached(severity: int) -> bool:
    return severity >= AUTO_PAUSE_THRESHOLD


# ---------------------------------------------------------------------------
# 角色限制
# ---------------------------------------------------------------------------
# 只有这些角色可以关闭异常信号；销售目标负责人明确被排除
SIGNAL_CLOSURE_ROLES = frozenset({d.ROLE_PHARMACIST, d.ROLE_COMPLIANCE})
# 标识隔离案卷只允许合规角色复核
QUARANTINE_REVIEW_ROLES = frozenset({d.ROLE_COMPLIANCE})
# 撤回授权后，只有合规角色能查看保留的最小事实
RETAINED_FACT_VIEW_ROLES = frozenset({d.ROLE_COMPLIANCE})
# 门店交接发起/承接
HANDOFF_ROLES = frozenset({d.ROLE_PHARMACIST, d.ROLE_COMPLIANCE})


def can_close_signal(role: str) -> bool:
    return role in SIGNAL_CLOSURE_ROLES


def can_review_quarantine(role: str) -> bool:
    return role in QUARANTINE_REVIEW_ROLES


# ---------------------------------------------------------------------------
# 角色可见字段（三视图过滤）
# ---------------------------------------------------------------------------
# 每个角色在计划/提醒/转诊/保供等视图中允许看到的字段
_VIEW_FIELDS: dict[str, frozenset[str]] = {
    d.ROLE_CUSTOMER: frozenset({
        "plan_id", "status", "medications", "store_id", "pharmacist_id",
        "reminder_id", "medication", "scheduled_at", "channel", "reminder_status",
        "skip_reason", "receipt_no", "referral_id", "referral_status", "target",
        "progress_note", "batch_id", "supply_status", "responsible_store_id",
        "backup_store_id", "due_at", "plan_source", "handoff_status",
        # 反馈回执与风险告知（顾客应知道已被转人工，但看不到内部规则细节）
        "duplicate", "duplicate_of", "feedback_id", "content_code", "detail",
        "reported_at", "signal_id", "signal_status", "auto_action",
        "medical_boundary", "severity", "note", "revoked_at",
        "cancelled_reminders", "customer_id", "version", "scopes", "channels",
        "granted_at", "transferred_batches", "rescheduled", "cancelled_pending",
        "sent_reminders_untouched", "quarantine_case_id", "quarantined",
        "identifier",
    }),
    d.ROLE_PHARMACIST: frozenset({
        "plan_id", "customer_id", "store_id", "pharmacist_id", "medications",
        "revision", "consent_version", "status", "plan_source",
        "reminder_id", "medication", "scheduled_at", "channel", "reminder_status",
        "sent_at", "skip_reason",
        "feedback_id", "content_code", "detail", "reported_at", "receipt_no",
        "signal_id", "rule_code", "severity", "signal_status",
        "referral_id", "referral_status", "target", "reason_code", "progress_note",
        "batch_id", "quantity", "supply_status",
        "responsible_store_id", "backup_store_id", "due_at",
        "handoff_id", "handoff_status", "from_store_id", "to_store_id",
        "to_pharmacist_id", "effective_at",
        "duplicate", "duplicate_of", "auto_action", "medical_boundary",
        "closed_by", "valid_to", "scopes", "transferred_batches",
        "due_tasks", "cancelled_pending", "rescheduled", "sent_reminders_untouched",
        "version", "channels", "granted_at", "revoked_at", "cancelled_reminders",
    }),
    d.ROLE_COMPLIANCE: frozenset({"*"}),  # 合规可见全部（含保留最小事实）
    d.ROLE_SALES: frozenset({
        "plan_id", "status", "supply_status", "batch_id",
        # 销售只能看到运营聚合状态，看不到健康内容与反馈明细
    }),
}


def filter_view(role: str, payload: dict) -> dict:
    allowed = _VIEW_FIELDS.get(role, frozenset())
    if "*" in allowed:
        return dict(payload)
    return {k: v for k, v in payload.items() if k in allowed}


# ---------------------------------------------------------------------------
# 诊断边界守卫：任何系统响应都不得包含自行生成的诊断结论
# ---------------------------------------------------------------------------
# 只拦截"系统自身作出诊断"的断言式表述；顾客自述或外部文书转述不在此列，
# 但系统字段里根本不允许出现自由形式的诊断断言（业务上只有结构化码）。
_DIAGNOSIS_PATTERNS = [
    re.compile(p) for p in (
        r"系统诊断", r"本(系统|平台)(诊断|确诊)", r"自动确诊", r"AI\s*诊断",
        r"(您|你)(被)?(确诊|诊断)为", r"(确诊|诊断)(患|为)有",
    )
]


class DiagnosisProhibited(ValueError):
    """响应中出现了系统自行生成的诊断结论。"""


def assert_no_diagnosis(value: object, path: str = "$") -> None:
    """递归扫描待输出内容；发现诊断断言立即拒绝。"""
    if isinstance(value, str):
        for pattern in _DIAGNOSIS_PATTERNS:
            if pattern.search(value):
                raise DiagnosisProhibited(f"响应字段 {path} 包含自行生成的诊断结论：{value[:60]}")
    elif isinstance(value, dict):
        for key, sub in value.items():
            assert_no_diagnosis(sub, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, sub in enumerate(value):
            assert_no_diagnosis(sub, f"{path}[{index}]")
