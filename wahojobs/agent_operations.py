"""Pure, dormant domain foundation for bounded AI company operations.

Milestone A1 defines values and deterministic policy decisions only.  This
module deliberately has no database, model, tool, network, filesystem, route,
or scheduling integration.
"""

from __future__ import annotations

from dataclasses import InitVar, dataclass, field, fields, is_dataclass, replace
from datetime import datetime, timezone
from enum import Enum
import hashlib
import hmac
import json
import math
import re
import secrets
from types import MappingProxyType
from typing import Any, Mapping
import unicodedata
import weakref


A1_POLICY_VERSION = "agent-operations-a1-v1"
CANONICAL_SCHEMA = "wahojobs.agent_operations"
CANONICAL_VERSION = 1
ROOT_AUDIT_FINGERPRINT = None

MAX_OBJECTIVE_LENGTH = 500
MAX_SAFE_SUMMARY_LENGTH = 500
MAX_OPERATION_LENGTH = 80
MAX_IDEMPOTENCY_KEY_LENGTH = 256
MAX_EVIDENCE_REFERENCES = 16
MAX_APPROVAL_REFERENCES = 16
MAX_TOOL_INTENTS_PER_RESULT = 32
MAX_TASK_REVISION = 100
MAX_ESCALATION_OPTIONS = 5
MAX_INTEGER = 1_000_000_000
MAX_CANONICAL_STRING_LENGTH = 10_000
MAX_CANONICAL_COLLECTION_ITEMS = 10_000
MAX_CANONICAL_MAPPING_ITEMS = 1_000
MAX_CANONICAL_BYTES = 1_000_000

_IDENTIFIERS = MappingProxyType({
    "agent": re.compile(r"^agt_[0-9a-f]{32}$"),
    "task": re.compile(r"^atk_[0-9a-f]{32}$"),
    "approval": re.compile(r"^aap_[0-9a-f]{32}$"),
    "intent": re.compile(r"^ati_[0-9a-f]{32}$"),
    "event": re.compile(r"^aev_[0-9a-f]{32}$"),
    "escalation": re.compile(r"^aes_[0-9a-f]{32}$"),
})
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_SAFE_LOCATOR = re.compile(
    r"^(metrics|inventory|crawler|tests|source|seo|product|support|b2b|human|policy):[1-9][0-9]{0,5}$"
)
_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{7,255}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_EMAIL = re.compile(r"(?i)\b[^\s@]+@[^\s@]+\.[^\s@]+\b")
_SECRET = re.compile(
    r"(?i)(\b(api[ _-]?key|password|passwd|bearer|session[ _-]?token|"
    r"access[ _-]?token|refresh[ _-]?token|private[ _-]?key|client[ _-]?secret)\b|"
    r"\bsk-[A-Za-z0-9_-]{8,}\b)"
)
_LOCAL_PATH = re.compile(r"(?i)(\b[A-Z]:\\|/Users/|/home/|file://|\.sqlite\b|\.db\b)")
_SQL = re.compile(
    r"(?is)\b(select\s+.+\s+from|insert\s+into|update\s+.+\s+set|"
    r"delete\s+from|drop\s+table|alter\s+table)\b"
)
_HIDDEN_REASONING = re.compile(
    r"(?i)\b(chain[ _-]?of[ _-]?thought|reasoning[ _-]?trace|scratchpad|"
    r"hidden[ _-]?prompt|raw[ _-]?model[ _-]?context|internal[ _-]?monologue|"
    r"private[ _-]?reasoning|raw[ _-]?evidence|evidence[ _-]?payload|"
    r"source[ _-]?payload|raw[ _-]?content|chainOfThought|reasoningTrace|"
    r"hiddenPrompt|rawModelContext|internalMonologue|privateReasoning|"
    r"rawEvidence|evidencePayload|sourcePayload|rawContent)\b"
)
_FORBIDDEN_FIELD_NAMES = frozenset(
    {
        "chainofthought",
        "reasoningtrace",
        "scratchpad",
        "hiddenprompt",
        "rawmodelcontext",
        "internalmonologue",
        "privatereasoning",
        "rawevidence",
        "evidencepayload",
        "sourcepayload",
        "rawcontent",
    }
)
_SIDE_EFFECT_WORDS = re.compile(
    r"(?i)\b(send|publish|deploy|merge|delete|archive|modify|mutate|write|"
    r"transfer|charge|purchase|sign|submit|disable|bypass)\b"
)


class AgentKind(str, Enum):
    CHIEF_OF_STAFF = "chief_of_staff"
    COMPANY_OPERATIONS = "company_operations"
    DATA_OPERATIONS = "data_operations"
    SEO_CONTENT = "seo_content"
    PRODUCT_OPERATIONS = "product_operations"
    CUSTOMER_SUPPORT = "customer_support"
    B2B_SALES = "b2b_sales"
    ENGINEERING_OPERATIONS = "engineering_operations"
    FINANCE_OPERATIONS = "finance_operations"


class CompanyFunction(str, Enum):
    COMPANY_OPERATIONS = "company_operations"
    DATA_OPERATIONS = "data_operations"
    SEO_CONTENT = "seo_content"
    PRODUCT_OPERATIONS = "product_operations"
    CUSTOMER_SUPPORT = "customer_support"
    B2B_SALES = "b2b_sales"
    ENGINEERING_OPERATIONS = "engineering_operations"
    FINANCE_OPERATIONS = "finance_operations"


class AgentEnvironment(str, Enum):
    DEVELOPMENT = "development"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"


class AgentLifecycle(str, Enum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    RETIRED = "retired"


class EscalationTarget(str, Enum):
    COMPANY_OPERATIONS_LEAD = "company_operations_lead"
    DATA_OPERATIONS_LEAD = "data_operations_lead"
    CONTENT_LEAD = "content_lead"
    PRODUCT_LEAD = "product_lead"
    SUPPORT_LEAD = "support_lead"
    SALES_LEAD = "sales_lead"
    ENGINEERING_LEAD = "engineering_lead"
    FINANCE_LEAD = "finance_lead"
    SECURITY_LEAD = "security_lead"


class DataClassification(str, Enum):
    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    RESTRICTED = "restricted"


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ActionCategory(str, Enum):
    OBSERVE = "observe"
    ANALYZE = "analyze"
    DRAFT = "draft"
    PROPOSE = "propose"
    TEST = "test"
    MODIFY_INTERNAL = "modify_internal"
    PUBLISH = "publish"
    COMMUNICATE_EXTERNAL = "communicate_external"
    DEPLOY = "deploy"
    FINANCIAL = "financial"
    LEGAL = "legal"
    SECURITY_SENSITIVE = "security_sensitive"
    DELETE_DATA = "delete_data"


READ_ONLY_ACTION_CATEGORIES = frozenset(
    {
        ActionCategory.OBSERVE,
        ActionCategory.ANALYZE,
        ActionCategory.DRAFT,
        ActionCategory.PROPOSE,
        ActionCategory.TEST,
    }
)
SIDE_EFFECT_ACTION_CATEGORIES = frozenset(set(ActionCategory) - set(READ_ONLY_ACTION_CATEGORIES))


class Capability(str, Enum):
    READ_JOB_INVENTORY_SUMMARY = "read_job_inventory_summary"
    READ_SOURCE_HEALTH = "read_source_health"
    READ_MATCHING_QUALITY_METRICS = "read_matching_quality_metrics"
    READ_TEST_AND_BUILD_STATUS = "read_test_and_build_status"
    READ_PRODUCT_METRICS = "read_product_metrics"
    READ_SEO_METRICS = "read_seo_metrics"
    READ_SUPPORT_QUEUE_SUMMARY = "read_support_queue_summary"
    READ_B2B_PIPELINE_SUMMARY = "read_b2b_pipeline_summary"
    CREATE_INTERNAL_BRIEFING = "create_internal_briefing"
    PROPOSE_DATA_QUALITY_TASK = "propose_data_quality_task"
    PROPOSE_CONTENT_UPDATE = "propose_content_update"
    PROPOSE_SUPPORT_REPLY = "propose_support_reply"
    PROPOSE_B2B_OUTREACH = "propose_b2b_outreach"
    PROPOSE_CODE_CHANGE = "propose_code_change"
    PROPOSE_PRODUCT_EXPERIMENT = "propose_product_experiment"
    EXECUTE_READ_ONLY_ANALYSIS = "execute_read_only_analysis"
    EXECUTE_TEST_SUITE = "execute_test_suite"
    CREATE_DRAFT_CONTENT = "create_draft_content"
    CREATE_DRAFT_MESSAGE = "create_draft_message"
    CREATE_DRAFT_PULL_REQUEST = "create_draft_pull_request"
    MODIFY_JOB_METADATA = "modify_job_metadata"
    ARCHIVE_EXPIRED_JOBS = "archive_expired_jobs"
    PUBLISH_CONTENT = "publish_content"
    SEND_SUPPORT_MESSAGE = "send_support_message"
    SEND_B2B_OUTREACH = "send_b2b_outreach"
    MERGE_PULL_REQUEST = "merge_pull_request"
    DEPLOY_PRODUCTION = "deploy_production"
    MODIFY_PRICING = "modify_pricing"
    SPEND_MONEY = "spend_money"
    MODIFY_BILLING = "modify_billing"
    EXECUTE_BANK_TRANSACTION = "execute_bank_transaction"
    SUBMIT_TAX_FILING = "submit_tax_filing"
    SIGN_CONTRACT = "sign_contract"
    ACCESS_SECRETS = "access_secrets"
    DELETE_USER_DATA = "delete_user_data"
    MODIFY_ACCOUNT_OWNERSHIP = "modify_account_ownership"


class TaskKind(str, Enum):
    DAILY_BRIEFING = "daily_briefing"
    DATA_QUALITY_REVIEW = "data_quality_review"
    CONTENT_UPDATE = "content_update"
    SUPPORT_RESPONSE = "support_response"
    B2B_OUTREACH = "b2b_outreach"
    CODE_CHANGE = "code_change"
    PRODUCT_EXPERIMENT = "product_experiment"
    TEST_AND_BUILD_REVIEW = "test_and_build_review"
    OPERATIONAL_ACTION = "operational_action"
    HUMAN_AWARENESS = "human_awareness"


class TaskCreator(str, Enum):
    HUMAN_OPERATOR = "human_operator"
    SYSTEM_POLICY = "system_policy"
    AGENT = "agent"


class ApprovalRequirement(str, Enum):
    NONE = "none"
    HUMAN = "human"


class TaskLifecycle(str, Enum):
    PROPOSED = "proposed"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    RUNNING = "running"
    NEEDS_HUMAN_INPUT = "needs_human_input"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    POLICY_DENIED = "policy_denied"


TERMINAL_TASK_LIFECYCLES = frozenset(
    {
        TaskLifecycle.SUCCEEDED,
        TaskLifecycle.FAILED,
        TaskLifecycle.CANCELLED,
        TaskLifecycle.EXPIRED,
        TaskLifecycle.POLICY_DENIED,
    }
)

TASK_TRANSITIONS = MappingProxyType(
    {
        TaskLifecycle.PROPOSED: frozenset(
            {
                TaskLifecycle.AWAITING_APPROVAL,
                TaskLifecycle.APPROVED,
                TaskLifecycle.CANCELLED,
                TaskLifecycle.EXPIRED,
                TaskLifecycle.POLICY_DENIED,
            }
        ),
        TaskLifecycle.AWAITING_APPROVAL: frozenset(
            {
                TaskLifecycle.APPROVED,
                TaskLifecycle.CANCELLED,
                TaskLifecycle.EXPIRED,
                TaskLifecycle.POLICY_DENIED,
            }
        ),
        TaskLifecycle.APPROVED: frozenset(
            {
                TaskLifecycle.RUNNING,
                TaskLifecycle.CANCELLED,
                TaskLifecycle.EXPIRED,
                TaskLifecycle.POLICY_DENIED,
            }
        ),
        TaskLifecycle.RUNNING: frozenset(
            {
                TaskLifecycle.SUCCEEDED,
                TaskLifecycle.FAILED,
                TaskLifecycle.NEEDS_HUMAN_INPUT,
                TaskLifecycle.CANCELLED,
            }
        ),
        TaskLifecycle.NEEDS_HUMAN_INPUT: frozenset(
            {
                TaskLifecycle.AWAITING_APPROVAL,
                TaskLifecycle.CANCELLED,
                TaskLifecycle.EXPIRED,
                TaskLifecycle.POLICY_DENIED,
            }
        ),
        TaskLifecycle.SUCCEEDED: frozenset(),
        TaskLifecycle.FAILED: frozenset(),
        TaskLifecycle.CANCELLED: frozenset(),
        TaskLifecycle.EXPIRED: frozenset(),
        TaskLifecycle.POLICY_DENIED: frozenset(),
    }
)


class ToolKind(str, Enum):
    INTERNAL_METRICS = "internal_metrics"
    JOB_INVENTORY = "job_inventory"
    CRAWLER_CONTROL = "crawler_control"
    CONTENT_MANAGEMENT = "content_management"
    SUPPORT_DRAFTS = "support_drafts"
    B2B_CRM = "b2b_crm"
    SOURCE_CONTROL = "source_control"
    TEST_RUNNER = "test_runner"
    DEPLOYMENT = "deployment"
    BILLING = "billing"
    FINANCE = "finance"
    COMMUNICATIONS = "communications"


class AgentOperation(str, Enum):
    READ_JOB_INVENTORY_SUMMARY = "read_job_inventory_summary"
    READ_SOURCE_HEALTH = "read_source_health"
    READ_MATCHING_QUALITY_METRICS = "read_matching_quality_metrics"
    READ_TEST_AND_BUILD_STATUS = "read_test_and_build_status"
    READ_PRODUCT_METRICS = "read_product_metrics"
    READ_SEO_METRICS = "read_seo_metrics"
    READ_SUPPORT_QUEUE_SUMMARY = "read_support_queue_summary"
    READ_B2B_PIPELINE_SUMMARY = "read_b2b_pipeline_summary"
    CREATE_INTERNAL_BRIEFING = "create_internal_briefing"
    PROPOSE_DATA_QUALITY_TASK = "propose_data_quality_task"
    PROPOSE_CONTENT_UPDATE = "propose_content_update"
    PROPOSE_SUPPORT_REPLY = "propose_support_reply"
    PROPOSE_B2B_OUTREACH = "propose_b2b_outreach"
    PROPOSE_CODE_CHANGE = "propose_code_change"
    PROPOSE_PRODUCT_EXPERIMENT = "propose_product_experiment"
    EXECUTE_READ_ONLY_ANALYSIS = "execute_read_only_analysis"
    EXECUTE_TEST_SUITE = "execute_test_suite"
    CREATE_DRAFT_CONTENT = "create_draft_content"
    CREATE_DRAFT_MESSAGE = "create_draft_message"
    CREATE_DRAFT_PULL_REQUEST = "create_draft_pull_request"
    MODIFY_JOB_METADATA = "modify_job_metadata"
    ARCHIVE_EXPIRED_JOBS = "archive_expired_jobs"
    START_CRAWLER = "start_crawler"
    STOP_CRAWLER = "stop_crawler"
    PUBLISH_CONTENT = "publish_content"
    SEND_SUPPORT_MESSAGE = "send_support_message"
    SEND_B2B_OUTREACH = "send_b2b_outreach"
    MERGE_PULL_REQUEST = "merge_pull_request"
    DEPLOY_PRODUCTION = "deploy_production"
    MODIFY_PRICING = "modify_pricing"
    SPEND_MONEY = "spend_money"
    MODIFY_BILLING = "modify_billing"
    EXECUTE_BANK_TRANSACTION = "execute_bank_transaction"
    SUBMIT_TAX_FILING = "submit_tax_filing"
    SIGN_CONTRACT = "sign_contract"
    ACCESS_SECRETS = "access_secrets"
    DELETE_USER_DATA = "delete_user_data"
    MODIFY_ACCOUNT_OWNERSHIP = "modify_account_ownership"


class SideEffectClass(str, Enum):
    NONE = "none"
    INTERNAL_DRAFT_ONLY = "internal_draft_only"
    TEST_EXECUTION = "test_execution"
    INTERNAL_STATE_CHANGE = "internal_state_change"
    CONTENT_PUBLICATION = "content_publication"
    EXTERNAL_MESSAGE = "external_message"
    SOURCE_CONTROL_CHANGE = "source_control_change"
    DEPLOYMENT = "deployment"
    BILLING_OR_PRICING = "billing_or_pricing"
    FINANCIAL_TRANSACTION = "financial_transaction"
    LEGAL_ACTION = "legal_action"
    SECURITY_SENSITIVE = "security_sensitive"
    USER_DATA_DELETION = "user_data_deletion"
    OWNERSHIP_MUTATION = "ownership_mutation"

    # Compatibility names for the original A1 vocabulary. These are aliases,
    # not additional accepted values.
    INTERNAL_REVERSIBLE = "internal_state_change"
    PUBLICATION = "content_publication"
    FINANCIAL = "financial_transaction"
    LEGAL = "legal_action"
    SECURITY = "security_sensitive"
    DELETION = "user_data_deletion"


class PolicyDecision(str, Enum):
    ALLOW_READ_ONLY = "allow_read_only"
    ALLOW_APPROVED_EXECUTION = "allow_approved_execution"
    REQUIRE_HUMAN_APPROVAL = "require_human_approval"
    DENY = "deny"
    PROHIBIT = "prohibit"


class ApprovalScope(str, Enum):
    EXACT_TOOL_INTENTS = "exact_tool_intents"


class EvidenceKind(str, Enum):
    METRIC_SNAPSHOT = "metric_snapshot"
    JOB_INVENTORY_SUMMARY = "job_inventory_summary"
    CRAWLER_HEALTH_SUMMARY = "crawler_health_summary"
    TEST_RESULT_SUMMARY = "test_result_summary"
    SOURCE_CONTROL_SUMMARY = "source_control_summary"
    SEO_SUMMARY = "seo_summary"
    PRODUCT_ANALYTICS_SUMMARY = "product_analytics_summary"
    SUPPORT_SUMMARY = "support_summary"
    B2B_PIPELINE_SUMMARY = "b2b_pipeline_summary"
    HUMAN_INSTRUCTION = "human_instruction"
    POLICY_DOCUMENT = "policy_document"


class EvidenceSourceSystem(str, Enum):
    INTERNAL_METRICS = "internal_metrics"
    JOB_INVENTORY = "job_inventory"
    CRAWLER = "crawler"
    TEST_RUNNER = "test_runner"
    SOURCE_CONTROL = "source_control"
    SEO_ANALYTICS = "seo_analytics"
    PRODUCT_ANALYTICS = "product_analytics"
    SUPPORT_SYSTEM = "support_system"
    B2B_CRM = "b2b_crm"
    HUMAN_OPERATOR = "human_operator"
    POLICY_REGISTRY = "policy_registry"


class AuditEventKind(str, Enum):
    TASK_PROPOSED = "task_proposed"
    POLICY_EVALUATED = "policy_evaluated"
    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_GRANTED = "approval_granted"
    APPROVAL_DENIED = "approval_denied"
    EXECUTION_STARTED = "execution_started"
    TOOL_INTENT_AUTHORIZED = "tool_intent_authorized"
    TOOL_INTENT_DENIED = "tool_intent_denied"
    TOOL_ATTEMPT_RECORDED = "tool_attempt_recorded"
    TASK_SUCCEEDED = "task_succeeded"
    TASK_FAILED = "task_failed"
    TASK_CANCELLED = "task_cancelled"
    HUMAN_HANDOFF_REQUESTED = "human_handoff_requested"
    BUDGET_EXHAUSTED = "budget_exhausted"
    POLICY_VIOLATION_DETECTED = "policy_violation_detected"


class EscalationReason(str, Enum):
    APPROVAL_REQUIRED = "approval_required"
    CONFLICTING_EVIDENCE = "conflicting_evidence"
    INSUFFICIENT_DATA = "insufficient_data"
    POLICY_DENIED = "policy_denied"
    BUDGET_EXHAUSTED = "budget_exhausted"
    IRREVERSIBLE_ACTION = "irreversible_action"
    LEGAL_OR_TAX = "legal_or_tax"
    FINANCIAL_ACTION = "financial_action"
    SECURITY_SENSITIVE = "security_sensitive"
    CUSTOMER_HARM_RISK = "customer_harm_risk"
    UNEXPECTED_SYSTEM_STATE = "unexpected_system_state"


class RequiredDecisionType(str, Enum):
    APPROVE_OR_REJECT = "approve_or_reject"
    PROVIDE_INFORMATION = "provide_information"
    CHOOSE_OPTION = "choose_option"
    DEFINE_ROLLBACK = "define_rollback"
    TAKE_OVER = "take_over"


class IdempotencyDomain(str, Enum):
    TASK_PROPOSAL = "task_proposal"
    TOOL_INTENT = "tool_intent"
    APPROVAL = "approval"
    EXECUTION_ATTEMPT = "execution_attempt"
    AUDIT_EVENT = "audit_event"


class ReplayClassification(str, Enum):
    EXACT_REPLAY = "exact_replay"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    DISTINCT_REQUEST = "distinct_request"


class AgentOperationsErrorCode(str, Enum):
    INVALID_AGENT_DEFINITION = "invalid_agent_definition"
    INVALID_TASK = "invalid_task"
    INVALID_TRANSITION = "invalid_transition"
    INVALID_TOOL_INTENT = "invalid_tool_intent"
    INVALID_APPROVAL = "invalid_approval"
    APPROVAL_REQUIRED = "approval_required"
    APPROVAL_MISMATCH = "approval_mismatch"
    APPROVAL_EXPIRED = "approval_expired"
    CAPABILITY_DENIED = "capability_denied"
    DATA_CLASSIFICATION_DENIED = "data_classification_denied"
    RISK_CEILING_EXCEEDED = "risk_ceiling_exceeded"
    ACTION_PROHIBITED = "action_prohibited"
    BUDGET_EXHAUSTED = "budget_exhausted"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    INVALID_AUDIT_CHAIN = "invalid_audit_chain"
    INTERNAL_CONSISTENCY_FAILURE = "internal_consistency_failure"


_ERROR_MESSAGES = MappingProxyType(
    {
        AgentOperationsErrorCode.INVALID_AGENT_DEFINITION: "The agent definition is invalid.",
        AgentOperationsErrorCode.INVALID_TASK: "The agent task is invalid.",
        AgentOperationsErrorCode.INVALID_TRANSITION: "The task transition is not permitted.",
        AgentOperationsErrorCode.INVALID_TOOL_INTENT: "The proposed tool action is invalid.",
        AgentOperationsErrorCode.INVALID_APPROVAL: "Trusted human approval is invalid.",
        AgentOperationsErrorCode.APPROVAL_REQUIRED: "Trusted human approval is required.",
        AgentOperationsErrorCode.APPROVAL_MISMATCH: "The approval does not match the action.",
        AgentOperationsErrorCode.APPROVAL_EXPIRED: "The approval is no longer valid.",
        AgentOperationsErrorCode.CAPABILITY_DENIED: "The capability is not available.",
        AgentOperationsErrorCode.DATA_CLASSIFICATION_DENIED: "The requested data classification is not available.",
        AgentOperationsErrorCode.RISK_CEILING_EXCEEDED: "The action exceeds the configured risk ceiling.",
        AgentOperationsErrorCode.ACTION_PROHIBITED: "The action is prohibited.",
        AgentOperationsErrorCode.BUDGET_EXHAUSTED: "The execution budget is exhausted.",
        AgentOperationsErrorCode.IDEMPOTENCY_CONFLICT: "The idempotency scope conflicts with an earlier request.",
        AgentOperationsErrorCode.INVALID_AUDIT_CHAIN: "The audit event chain is invalid.",
        AgentOperationsErrorCode.INTERNAL_CONSISTENCY_FAILURE: "The agent operation could not be evaluated.",
    }
)


class AgentOperationsError(Exception):
    """Bounded public error that never incorporates rejected input."""

    __slots__ = ("code",)

    def __init__(self, code: AgentOperationsErrorCode):
        if type(code) is not AgentOperationsErrorCode:
            code = AgentOperationsErrorCode.INTERNAL_CONSISTENCY_FAILURE
        self.code = code
        super().__init__(_ERROR_MESSAGES[code])
        self.__cause__ = None
        self.__suppress_context__ = True

    def as_public_dict(self) -> dict[str, str]:
        return {"error": self.code.value, "message": _ERROR_MESSAGES[self.code]}

    def __repr__(self) -> str:
        return f"AgentOperationsError(code={self.code.value!r})"


def _fail(code: AgentOperationsErrorCode) -> None:
    raise AgentOperationsError(code) from None


_CLASSIFICATION_ORDER = MappingProxyType(
    {
        DataClassification.PUBLIC: 0,
        DataClassification.INTERNAL: 1,
        DataClassification.CONFIDENTIAL: 2,
        DataClassification.RESTRICTED: 3,
    }
)
_RISK_ORDER = MappingProxyType(
    {RiskLevel.LOW: 0, RiskLevel.MEDIUM: 1, RiskLevel.HIGH: 2, RiskLevel.CRITICAL: 3}
)


def _exact_enum(value: Any, enum_type: type[Enum], code: AgentOperationsErrorCode) -> None:
    if type(value) is not enum_type:
        _fail(code)


def _bounded_int(value: Any, *, minimum: int, maximum: int, code: AgentOperationsErrorCode) -> int:
    if type(value) is not int or value < minimum or value > maximum:
        _fail(code)
    return value


def _normalized_field_name(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value).casefold()
    if _CONTROL.search(normalized):
        return ""
    return re.sub(r"[_\-\s]+", "", normalized)


def _forbidden_field_name(value: Any) -> bool:
    return type(value) is not str or _normalized_field_name(value) in _FORBIDDEN_FIELD_NAMES


def _canonical_time(value: Any, code: AgentOperationsErrorCode) -> datetime:
    if type(value) is not datetime or value.tzinfo is not timezone.utc or value.microsecond != 0:
        _fail(code)
    return value


def canonical_timestamp(value: datetime) -> str:
    value = _canonical_time(value, AgentOperationsErrorCode.INTERNAL_CONSISTENCY_FAILURE)
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def _safe_text(
    value: Any,
    *,
    maximum: int,
    code: AgentOperationsErrorCode,
    allow_empty: bool = False,
) -> str:
    if type(value) is not str:
        _fail(code)
    normalized = unicodedata.normalize("NFC", value)
    if (
        (not normalized and not allow_empty)
        or len(normalized) > maximum
        or normalized != normalized.strip()
        or _CONTROL.search(normalized)
        or _EMAIL.search(normalized)
        or _SECRET.search(normalized)
        or _LOCAL_PATH.search(normalized)
        or _SQL.search(normalized)
        or _HIDDEN_REASONING.search(normalized)
    ):
        _fail(code)
    return normalized


def _identifier(value: Any, kind: str, code: AgentOperationsErrorCode) -> str:
    if type(value) is not str or not _IDENTIFIERS[kind].fullmatch(value):
        _fail(code)
    return value


def _digest(value: Any, code: AgentOperationsErrorCode) -> str:
    if type(value) is not str or not _DIGEST.fullmatch(value):
        _fail(code)
    return value


def _idempotency_key(value: Any, code: AgentOperationsErrorCode) -> str:
    if type(value) is not str or not _IDEMPOTENCY_KEY.fullmatch(value):
        _fail(code)
    return value


def generate_agent_id() -> str:
    return f"agt_{secrets.token_hex(16)}"


def generate_task_id() -> str:
    return f"atk_{secrets.token_hex(16)}"


def generate_tool_intent_id() -> str:
    return f"ati_{secrets.token_hex(16)}"


def generate_approval_id() -> str:
    return f"aap_{secrets.token_hex(16)}"


def generate_audit_event_id() -> str:
    return f"aev_{secrets.token_hex(16)}"


def generate_escalation_id() -> str:
    return f"aes_{secrets.token_hex(16)}"


def _to_primitive(value: Any) -> Any:
    if isinstance(value, TrustedHumanApproval):
        _fail(AgentOperationsErrorCode.INVALID_APPROVAL)
    if value is None or type(value) in (bool, str, int):
        if type(value) is str:
            normalized = unicodedata.normalize("NFC", value)
            if (
                len(normalized) > MAX_CANONICAL_STRING_LENGTH
                or _CONTROL.search(normalized)
                or _EMAIL.search(normalized)
                or _SECRET.search(normalized)
                or _LOCAL_PATH.search(normalized)
                or _SQL.search(normalized)
                or _HIDDEN_REASONING.search(normalized)
            ):
                _fail(AgentOperationsErrorCode.INTERNAL_CONSISTENCY_FAILURE)
            return normalized
        if type(value) is int and abs(value) > MAX_INTEGER:
            _fail(AgentOperationsErrorCode.INTERNAL_CONSISTENCY_FAILURE)
        return value
    if type(value) is float:
        if not math.isfinite(value):
            _fail(AgentOperationsErrorCode.INTERNAL_CONSISTENCY_FAILURE)
        return 0 if value == 0 else value
    if isinstance(value, Enum):
        return value.value
    if type(value) is datetime:
        return canonical_timestamp(value)
    if is_dataclass(value) and not isinstance(value, type):
        converted = {}
        for data_field in fields(value):
            if data_field.name.startswith("_"):
                continue
            if _forbidden_field_name(data_field.name):
                _fail(AgentOperationsErrorCode.INTERNAL_CONSISTENCY_FAILURE)
            converted[data_field.name] = _to_primitive(getattr(value, data_field.name))
        return converted
    if isinstance(value, Mapping):
        if len(value) > MAX_CANONICAL_MAPPING_ITEMS:
            _fail(AgentOperationsErrorCode.INTERNAL_CONSISTENCY_FAILURE)
        converted: dict[str, Any] = {}
        for key, item in value.items():
            if type(key) is not str or len(key) > 128 or _CONTROL.search(key):
                _fail(AgentOperationsErrorCode.INTERNAL_CONSISTENCY_FAILURE)
            normalized = unicodedata.normalize("NFC", key)
            if (
                normalized in converted
                or _SECRET.search(normalized)
                or _HIDDEN_REASONING.search(normalized)
                or _forbidden_field_name(normalized)
            ):
                _fail(AgentOperationsErrorCode.INTERNAL_CONSISTENCY_FAILURE)
            converted[normalized] = _to_primitive(item)
        return converted
    if type(value) in (tuple, list):
        if len(value) > MAX_CANONICAL_COLLECTION_ITEMS:
            _fail(AgentOperationsErrorCode.INTERNAL_CONSISTENCY_FAILURE)
        return [_to_primitive(item) for item in value]
    if type(value) in (set, frozenset):
        if len(value) > MAX_CANONICAL_COLLECTION_ITEMS:
            _fail(AgentOperationsErrorCode.INTERNAL_CONSISTENCY_FAILURE)
        converted = [_to_primitive(item) for item in value]
        return sorted(converted, key=lambda item: json.dumps(item, sort_keys=True, ensure_ascii=False))
    _fail(AgentOperationsErrorCode.INTERNAL_CONSISTENCY_FAILURE)


def canonical_json_bytes(value: Any) -> bytes:
    envelope = {
        "domain": CANONICAL_SCHEMA,
        "payload": _to_primitive(value),
        "version": CANONICAL_VERSION,
    }
    serialization_failed = False
    try:
        encoded = json.dumps(
            envelope,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        if len(encoded) > MAX_CANONICAL_BYTES:
            _fail(AgentOperationsErrorCode.INTERNAL_CONSISTENCY_FAILURE)
        return encoded
    except (TypeError, ValueError, OverflowError):
        serialization_failed = True
    if serialization_failed:
        _fail(AgentOperationsErrorCode.INTERNAL_CONSISTENCY_FAILURE)


def parse_canonical_json(raw: str | bytes) -> Any:
    if type(raw) is bytes:
        decode_failed = False
        try:
            raw = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            decode_failed = True
        if decode_failed:
            _fail(AgentOperationsErrorCode.INTERNAL_CONSISTENCY_FAILURE)
    if type(raw) is not str:
        _fail(AgentOperationsErrorCode.INTERNAL_CONSISTENCY_FAILURE)
    encoding_failed = False
    try:
        raw_size = len(raw.encode("utf-8"))
    except UnicodeEncodeError:
        raw_size = 0
        encoding_failed = True
    if encoding_failed or raw_size > MAX_CANONICAL_BYTES:
        _fail(AgentOperationsErrorCode.INTERNAL_CONSISTENCY_FAILURE)

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            normalized = unicodedata.normalize("NFC", key)
            if normalized in result:
                _fail(AgentOperationsErrorCode.INTERNAL_CONSISTENCY_FAILURE)
            result[normalized] = value
        return result

    def reject_constant(_value):
        _fail(AgentOperationsErrorCode.INTERNAL_CONSISTENCY_FAILURE)

    parse_failed = False
    try:
        parsed = json.loads(raw, object_pairs_hook=unique_object, parse_constant=reject_constant)
    except AgentOperationsError:
        raise
    except (UnicodeError, json.JSONDecodeError, TypeError, ValueError, OverflowError):
        parsed = None
        parse_failed = True
    if parse_failed:
        _fail(AgentOperationsErrorCode.INTERNAL_CONSISTENCY_FAILURE)
    if (
        type(parsed) is not dict
        or parsed.get("domain") != CANONICAL_SCHEMA
        or parsed.get("version") != CANONICAL_VERSION
        or set(parsed) != {"domain", "payload", "version"}
    ):
        _fail(AgentOperationsErrorCode.INTERNAL_CONSISTENCY_FAILURE)
    if canonical_json_bytes(parsed["payload"]).decode("utf-8") != raw:
        _fail(AgentOperationsErrorCode.INTERNAL_CONSISTENCY_FAILURE)
    return parsed["payload"]


def _fingerprint(domain: str, payload: Any) -> str:
    separated = {
        "fingerprint_domain": domain,
        "fingerprint_version": 1,
        "payload": payload,
        "policy_version": A1_POLICY_VERSION,
    }
    return hashlib.sha256(canonical_json_bytes(separated)).hexdigest()


@dataclass(frozen=True, slots=True)
class CapabilitySpecification:
    capability: Capability
    company_function: CompanyFunction
    action_category: ActionCategory
    minimum_risk: RiskLevel
    maximum_data_classification: DataClassification
    human_approval_always_required: bool
    prohibited_for_autonomous_execution: bool
    documentation_safe_meaning: str


def _spec(
    capability: Capability,
    company_function: CompanyFunction,
    action_category: ActionCategory,
    minimum_risk: RiskLevel,
    maximum_data_classification: DataClassification,
    approval: bool,
    autonomous_prohibition: bool,
    meaning: str,
) -> CapabilitySpecification:
    return CapabilitySpecification(
        capability,
        company_function,
        action_category,
        minimum_risk,
        maximum_data_classification,
        approval,
        autonomous_prohibition,
        meaning,
    )


_CAPABILITY_SPECIFICATIONS = (
    _spec(Capability.READ_JOB_INVENTORY_SUMMARY, CompanyFunction.DATA_OPERATIONS, ActionCategory.OBSERVE, RiskLevel.LOW, DataClassification.INTERNAL, False, False, "Read an aggregate job inventory summary."),
    _spec(Capability.READ_SOURCE_HEALTH, CompanyFunction.DATA_OPERATIONS, ActionCategory.OBSERVE, RiskLevel.LOW, DataClassification.INTERNAL, False, False, "Read aggregate source health facts."),
    _spec(Capability.READ_MATCHING_QUALITY_METRICS, CompanyFunction.DATA_OPERATIONS, ActionCategory.ANALYZE, RiskLevel.LOW, DataClassification.INTERNAL, False, False, "Analyze aggregate matching quality metrics."),
    _spec(Capability.READ_TEST_AND_BUILD_STATUS, CompanyFunction.ENGINEERING_OPERATIONS, ActionCategory.OBSERVE, RiskLevel.LOW, DataClassification.INTERNAL, False, False, "Read test and build status summaries."),
    _spec(Capability.READ_PRODUCT_METRICS, CompanyFunction.PRODUCT_OPERATIONS, ActionCategory.OBSERVE, RiskLevel.LOW, DataClassification.INTERNAL, False, False, "Read aggregate product metrics."),
    _spec(Capability.READ_SEO_METRICS, CompanyFunction.SEO_CONTENT, ActionCategory.OBSERVE, RiskLevel.LOW, DataClassification.INTERNAL, False, False, "Read aggregate search performance metrics."),
    _spec(Capability.READ_SUPPORT_QUEUE_SUMMARY, CompanyFunction.CUSTOMER_SUPPORT, ActionCategory.OBSERVE, RiskLevel.LOW, DataClassification.CONFIDENTIAL, False, False, "Read a privacy-safe support queue summary."),
    _spec(Capability.READ_B2B_PIPELINE_SUMMARY, CompanyFunction.B2B_SALES, ActionCategory.OBSERVE, RiskLevel.LOW, DataClassification.CONFIDENTIAL, False, False, "Read an aggregate business pipeline summary."),
    _spec(Capability.CREATE_INTERNAL_BRIEFING, CompanyFunction.COMPANY_OPERATIONS, ActionCategory.DRAFT, RiskLevel.LOW, DataClassification.INTERNAL, False, False, "Create an internal operational briefing."),
    _spec(Capability.PROPOSE_DATA_QUALITY_TASK, CompanyFunction.DATA_OPERATIONS, ActionCategory.PROPOSE, RiskLevel.LOW, DataClassification.INTERNAL, False, False, "Propose a bounded data quality task."),
    _spec(Capability.PROPOSE_CONTENT_UPDATE, CompanyFunction.SEO_CONTENT, ActionCategory.PROPOSE, RiskLevel.LOW, DataClassification.INTERNAL, False, False, "Propose a content update without publishing it."),
    _spec(Capability.PROPOSE_SUPPORT_REPLY, CompanyFunction.CUSTOMER_SUPPORT, ActionCategory.PROPOSE, RiskLevel.LOW, DataClassification.CONFIDENTIAL, False, False, "Propose a support reply without sending it."),
    _spec(Capability.PROPOSE_B2B_OUTREACH, CompanyFunction.B2B_SALES, ActionCategory.PROPOSE, RiskLevel.LOW, DataClassification.CONFIDENTIAL, False, False, "Propose business outreach without sending it."),
    _spec(Capability.PROPOSE_CODE_CHANGE, CompanyFunction.ENGINEERING_OPERATIONS, ActionCategory.PROPOSE, RiskLevel.LOW, DataClassification.INTERNAL, False, False, "Propose a source change without applying it."),
    _spec(Capability.PROPOSE_PRODUCT_EXPERIMENT, CompanyFunction.PRODUCT_OPERATIONS, ActionCategory.PROPOSE, RiskLevel.LOW, DataClassification.INTERNAL, False, False, "Propose a bounded product experiment."),
    _spec(Capability.EXECUTE_READ_ONLY_ANALYSIS, CompanyFunction.COMPANY_OPERATIONS, ActionCategory.ANALYZE, RiskLevel.LOW, DataClassification.INTERNAL, False, False, "Perform deterministic read-only analysis."),
    _spec(Capability.EXECUTE_TEST_SUITE, CompanyFunction.ENGINEERING_OPERATIONS, ActionCategory.TEST, RiskLevel.LOW, DataClassification.INTERNAL, False, False, "Run a deterministic test suite without product mutation."),
    _spec(Capability.CREATE_DRAFT_CONTENT, CompanyFunction.SEO_CONTENT, ActionCategory.DRAFT, RiskLevel.LOW, DataClassification.INTERNAL, False, False, "Create unpublished draft content."),
    _spec(Capability.CREATE_DRAFT_MESSAGE, CompanyFunction.CUSTOMER_SUPPORT, ActionCategory.DRAFT, RiskLevel.LOW, DataClassification.CONFIDENTIAL, False, False, "Create an unsent draft message."),
    _spec(Capability.CREATE_DRAFT_PULL_REQUEST, CompanyFunction.ENGINEERING_OPERATIONS, ActionCategory.DRAFT, RiskLevel.LOW, DataClassification.INTERNAL, False, False, "Create a draft change proposal without merging it."),
    _spec(Capability.MODIFY_JOB_METADATA, CompanyFunction.DATA_OPERATIONS, ActionCategory.MODIFY_INTERNAL, RiskLevel.MEDIUM, DataClassification.INTERNAL, True, True, "Modify bounded internal job metadata."),
    _spec(Capability.ARCHIVE_EXPIRED_JOBS, CompanyFunction.DATA_OPERATIONS, ActionCategory.MODIFY_INTERNAL, RiskLevel.HIGH, DataClassification.INTERNAL, True, True, "Archive jobs within an approved scope."),
    _spec(Capability.PUBLISH_CONTENT, CompanyFunction.SEO_CONTENT, ActionCategory.PUBLISH, RiskLevel.MEDIUM, DataClassification.INTERNAL, True, True, "Publish approved content within a bounded scope."),
    _spec(Capability.SEND_SUPPORT_MESSAGE, CompanyFunction.CUSTOMER_SUPPORT, ActionCategory.COMMUNICATE_EXTERNAL, RiskLevel.HIGH, DataClassification.CONFIDENTIAL, True, True, "Send one approved support message."),
    _spec(Capability.SEND_B2B_OUTREACH, CompanyFunction.B2B_SALES, ActionCategory.COMMUNICATE_EXTERNAL, RiskLevel.HIGH, DataClassification.CONFIDENTIAL, True, True, "Send approved business outreach."),
    _spec(Capability.MERGE_PULL_REQUEST, CompanyFunction.ENGINEERING_OPERATIONS, ActionCategory.MODIFY_INTERNAL, RiskLevel.HIGH, DataClassification.INTERNAL, True, True, "Merge one explicitly approved source change."),
    _spec(Capability.DEPLOY_PRODUCTION, CompanyFunction.ENGINEERING_OPERATIONS, ActionCategory.DEPLOY, RiskLevel.HIGH, DataClassification.CONFIDENTIAL, True, True, "Deploy an approved build to production."),
    _spec(Capability.MODIFY_PRICING, CompanyFunction.FINANCE_OPERATIONS, ActionCategory.FINANCIAL, RiskLevel.HIGH, DataClassification.CONFIDENTIAL, True, True, "Modify approved pricing within a bounded scope."),
    _spec(Capability.SPEND_MONEY, CompanyFunction.FINANCE_OPERATIONS, ActionCategory.FINANCIAL, RiskLevel.CRITICAL, DataClassification.RESTRICTED, True, True, "Commit company funds."),
    _spec(Capability.MODIFY_BILLING, CompanyFunction.FINANCE_OPERATIONS, ActionCategory.FINANCIAL, RiskLevel.CRITICAL, DataClassification.RESTRICTED, True, True, "Modify a billing account or charge."),
    _spec(Capability.EXECUTE_BANK_TRANSACTION, CompanyFunction.FINANCE_OPERATIONS, ActionCategory.FINANCIAL, RiskLevel.CRITICAL, DataClassification.RESTRICTED, True, True, "Execute a bank transaction."),
    _spec(Capability.SUBMIT_TAX_FILING, CompanyFunction.FINANCE_OPERATIONS, ActionCategory.LEGAL, RiskLevel.CRITICAL, DataClassification.RESTRICTED, True, True, "Submit a tax filing."),
    _spec(Capability.SIGN_CONTRACT, CompanyFunction.COMPANY_OPERATIONS, ActionCategory.LEGAL, RiskLevel.CRITICAL, DataClassification.CONFIDENTIAL, True, True, "Execute a legal agreement."),
    _spec(Capability.ACCESS_SECRETS, CompanyFunction.ENGINEERING_OPERATIONS, ActionCategory.SECURITY_SENSITIVE, RiskLevel.CRITICAL, DataClassification.RESTRICTED, True, True, "Access protected credentials or secrets."),
    _spec(Capability.DELETE_USER_DATA, CompanyFunction.PRODUCT_OPERATIONS, ActionCategory.DELETE_DATA, RiskLevel.CRITICAL, DataClassification.RESTRICTED, True, True, "Delete private user data."),
    _spec(Capability.MODIFY_ACCOUNT_OWNERSHIP, CompanyFunction.COMPANY_OPERATIONS, ActionCategory.SECURITY_SENSITIVE, RiskLevel.CRITICAL, DataClassification.RESTRICTED, True, True, "Modify account ownership authority."),
)

CAPABILITY_TAXONOMY = MappingProxyType(
    {specification.capability: specification for specification in _CAPABILITY_SPECIFICATIONS}
)

A1_PROHIBITED_CAPABILITIES = frozenset(
    {
        Capability.SEND_SUPPORT_MESSAGE,
        Capability.SEND_B2B_OUTREACH,
        Capability.DEPLOY_PRODUCTION,
        Capability.SPEND_MONEY,
        Capability.MODIFY_BILLING,
        Capability.EXECUTE_BANK_TRANSACTION,
        Capability.SUBMIT_TAX_FILING,
        Capability.SIGN_CONTRACT,
        Capability.ACCESS_SECRETS,
        Capability.DELETE_USER_DATA,
        Capability.MODIFY_ACCOUNT_OWNERSHIP,
    }
)


@dataclass(frozen=True, slots=True)
class AgentExecutionBudget:
    maximum_tool_intents_per_task: int
    maximum_execution_attempts: int
    maximum_concurrent_tasks: int
    maximum_cost_units: int
    maximum_external_messages: int
    maximum_modified_records: int
    maximum_content_publications: int
    maximum_runtime_seconds: int

    def __post_init__(self):
        code = AgentOperationsErrorCode.INVALID_AGENT_DEFINITION
        _bounded_int(self.maximum_tool_intents_per_task, minimum=1, maximum=MAX_TOOL_INTENTS_PER_RESULT, code=code)
        _bounded_int(self.maximum_execution_attempts, minimum=1, maximum=32, code=code)
        _bounded_int(self.maximum_concurrent_tasks, minimum=1, maximum=16, code=code)
        _bounded_int(self.maximum_cost_units, minimum=0, maximum=1_000_000, code=code)
        _bounded_int(self.maximum_external_messages, minimum=0, maximum=1_000, code=code)
        _bounded_int(self.maximum_modified_records, minimum=0, maximum=100_000, code=code)
        _bounded_int(self.maximum_content_publications, minimum=0, maximum=1_000, code=code)
        _bounded_int(self.maximum_runtime_seconds, minimum=1, maximum=86_400, code=code)


@dataclass(frozen=True, slots=True)
class AgentBudgetState:
    observed_at: datetime
    tool_intents_used: int = 0
    execution_attempts_used: int = 0
    concurrent_tasks: int = 0
    cost_units_used: int = 0
    external_messages_used: int = 0
    modified_records_used: int = 0
    content_publications_used: int = 0
    runtime_seconds_used: int = 0

    def __post_init__(self):
        code = AgentOperationsErrorCode.BUDGET_EXHAUSTED
        object.__setattr__(self, "observed_at", _canonical_time(self.observed_at, code))
        for field_name in (
            "tool_intents_used",
            "execution_attempts_used",
            "concurrent_tasks",
            "cost_units_used",
            "external_messages_used",
            "modified_records_used",
            "content_publications_used",
            "runtime_seconds_used",
        ):
            _bounded_int(getattr(self, field_name), minimum=0, maximum=MAX_INTEGER, code=code)


DEFAULT_A1_EXECUTION_BUDGET = AgentExecutionBudget(8, 2, 1, 100, 0, 0, 0, 300)


_EVIDENCE_LOCATOR_PREFIX = MappingProxyType(
    {
        EvidenceSourceSystem.INTERNAL_METRICS: "metrics",
        EvidenceSourceSystem.JOB_INVENTORY: "inventory",
        EvidenceSourceSystem.CRAWLER: "crawler",
        EvidenceSourceSystem.TEST_RUNNER: "tests",
        EvidenceSourceSystem.SOURCE_CONTROL: "source",
        EvidenceSourceSystem.SEO_ANALYTICS: "seo",
        EvidenceSourceSystem.PRODUCT_ANALYTICS: "product",
        EvidenceSourceSystem.SUPPORT_SYSTEM: "support",
        EvidenceSourceSystem.B2B_CRM: "b2b",
        EvidenceSourceSystem.HUMAN_OPERATOR: "human",
        EvidenceSourceSystem.POLICY_REGISTRY: "policy",
    }
)


@dataclass(frozen=True, slots=True, repr=False)
class EvidenceReference:
    evidence_kind: EvidenceKind
    source_system: EvidenceSourceSystem
    source_classification: DataClassification
    captured_at: datetime
    content_fingerprint: str
    safe_locator: str
    freshness_boundary: datetime

    def __post_init__(self):
        code = AgentOperationsErrorCode.INVALID_TASK
        _exact_enum(self.evidence_kind, EvidenceKind, code)
        _exact_enum(self.source_system, EvidenceSourceSystem, code)
        _exact_enum(self.source_classification, DataClassification, code)
        if self.source_classification is DataClassification.RESTRICTED:
            _fail(code)
        object.__setattr__(self, "captured_at", _canonical_time(self.captured_at, code))
        object.__setattr__(self, "freshness_boundary", _canonical_time(self.freshness_boundary, code))
        if self.freshness_boundary < self.captured_at:
            _fail(code)
        _digest(self.content_fingerprint, code)
        if (
            type(self.safe_locator) is not str
            or not _SAFE_LOCATOR.fullmatch(self.safe_locator)
            or self.safe_locator.split(":", 1)[0] != _EVIDENCE_LOCATOR_PREFIX[self.source_system]
        ):
            _fail(code)

    def __repr__(self) -> str:
        return (
            "EvidenceReference("
            f"kind={self.evidence_kind.value!r}, source={self.source_system.value!r}, "
            f"classification={self.source_classification.value!r}, locator=<redacted>)"
        )

    __str__ = __repr__


_KIND_FUNCTION = MappingProxyType(
    {
        AgentKind.CHIEF_OF_STAFF: CompanyFunction.COMPANY_OPERATIONS,
        AgentKind.COMPANY_OPERATIONS: CompanyFunction.COMPANY_OPERATIONS,
        AgentKind.DATA_OPERATIONS: CompanyFunction.DATA_OPERATIONS,
        AgentKind.SEO_CONTENT: CompanyFunction.SEO_CONTENT,
        AgentKind.PRODUCT_OPERATIONS: CompanyFunction.PRODUCT_OPERATIONS,
        AgentKind.CUSTOMER_SUPPORT: CompanyFunction.CUSTOMER_SUPPORT,
        AgentKind.B2B_SALES: CompanyFunction.B2B_SALES,
        AgentKind.ENGINEERING_OPERATIONS: CompanyFunction.ENGINEERING_OPERATIONS,
        AgentKind.FINANCE_OPERATIONS: CompanyFunction.FINANCE_OPERATIONS,
    }
)


@dataclass(frozen=True, slots=True, repr=False)
class AgentDefinition:
    agent_id: str
    agent_kind: AgentKind
    company_function: CompanyFunction
    environment: AgentEnvironment
    lifecycle: AgentLifecycle
    policy_version: str
    risk_ceiling: RiskLevel
    granted_capabilities: frozenset[Capability]
    allowed_data_classifications: frozenset[DataClassification]
    execution_budget: AgentExecutionBudget
    escalation_target: EscalationTarget

    def __post_init__(self):
        code = AgentOperationsErrorCode.INVALID_AGENT_DEFINITION
        _identifier(self.agent_id, "agent", code)
        _exact_enum(self.agent_kind, AgentKind, code)
        _exact_enum(self.company_function, CompanyFunction, code)
        _exact_enum(self.environment, AgentEnvironment, code)
        _exact_enum(self.lifecycle, AgentLifecycle, code)
        _exact_enum(self.risk_ceiling, RiskLevel, code)
        _exact_enum(self.escalation_target, EscalationTarget, code)
        if self.policy_version != A1_POLICY_VERSION or _KIND_FUNCTION[self.agent_kind] is not self.company_function:
            _fail(code)
        if type(self.execution_budget) is not AgentExecutionBudget:
            _fail(code)
        if type(self.granted_capabilities) is not frozenset or len(self.granted_capabilities) > len(Capability):
            _fail(code)
        if any(type(item) is not Capability for item in self.granted_capabilities):
            _fail(code)
        if any(CAPABILITY_TAXONOMY[item].company_function is not self.company_function for item in self.granted_capabilities):
            _fail(code)
        if any(
            _RISK_ORDER[CAPABILITY_TAXONOMY[item].minimum_risk] > _RISK_ORDER[self.risk_ceiling]
            for item in self.granted_capabilities
        ):
            _fail(code)
        if type(self.allowed_data_classifications) is not frozenset or not self.allowed_data_classifications:
            _fail(code)
        if any(type(item) is not DataClassification for item in self.allowed_data_classifications):
            _fail(code)
        if DataClassification.RESTRICTED in self.allowed_data_classifications:
            _fail(code)
        maximum = max(_CLASSIFICATION_ORDER[item] for item in self.allowed_data_classifications)
        expected = frozenset(item for item, order in _CLASSIFICATION_ORDER.items() if order <= maximum)
        if self.allowed_data_classifications != expected:
            _fail(code)

    def __repr__(self) -> str:
        return (
            "AgentDefinition("
            f"agent_id={self.agent_id!r}, kind={self.agent_kind.value!r}, "
            f"function={self.company_function.value!r}, environment={self.environment.value!r}, "
            f"lifecycle={self.lifecycle.value!r}, policy_version={self.policy_version!r}, "
            f"risk_ceiling={self.risk_ceiling.value!r}, capabilities={len(self.granted_capabilities)}, "
            f"data_classifications={len(self.allowed_data_classifications)}, "
            "execution_budget=<bounded>, escalation_target=<redacted>)"
        )

    __str__ = __repr__


def approval_requirement_for(capability: Capability, risk: RiskLevel) -> ApprovalRequirement:
    _exact_enum(capability, Capability, AgentOperationsErrorCode.INVALID_TASK)
    _exact_enum(risk, RiskLevel, AgentOperationsErrorCode.INVALID_TASK)
    specification = CAPABILITY_TAXONOMY[capability]
    if (
        specification.human_approval_always_required
        or specification.prohibited_for_autonomous_execution
        or _RISK_ORDER[risk] >= _RISK_ORDER[RiskLevel.MEDIUM]
        or specification.action_category in SIDE_EFFECT_ACTION_CATEGORIES
    ):
        return ApprovalRequirement.HUMAN
    return ApprovalRequirement.NONE


@dataclass(frozen=True, slots=True, repr=False)
class AgentTask:
    task_id: str
    task_kind: TaskKind
    company_function: CompanyFunction
    environment: AgentEnvironment
    requested_capability: Capability
    required_data_classification: DataClassification
    calculated_risk: RiskLevel
    lifecycle: TaskLifecycle
    created_at: datetime
    expires_at: datetime | None
    created_by: TaskCreator
    idempotency_key: str
    objective: str
    evidence_references: tuple[EvidenceReference, ...]
    current_policy_version: str
    approval_requirement: ApprovalRequirement
    execution_budget: AgentExecutionBudget
    revision_number: int
    _transition_lineage: str | None = field(default=None, repr=False, compare=False)
    _transition_authority: InitVar[object] = None

    def __post_init__(self, _transition_authority):
        self._validate_domain_fields(normalize=True)
        code = AgentOperationsErrorCode.INVALID_TASK
        if _transition_authority is None:
            if self.lifecycle is not TaskLifecycle.PROPOSED or self._transition_lineage is not None:
                _fail(code)
        else:
            lineage = _consume_task_transition_authority(_transition_authority, self)
            object.__setattr__(self, "_transition_lineage", lineage)

    def _validate_domain_fields(self, *, normalize: bool) -> None:
        code = AgentOperationsErrorCode.INVALID_TASK
        _identifier(self.task_id, "task", code)
        _exact_enum(self.task_kind, TaskKind, code)
        _exact_enum(self.company_function, CompanyFunction, code)
        _exact_enum(self.environment, AgentEnvironment, code)
        _exact_enum(self.requested_capability, Capability, code)
        _exact_enum(self.required_data_classification, DataClassification, code)
        _exact_enum(self.calculated_risk, RiskLevel, code)
        _exact_enum(self.lifecycle, TaskLifecycle, code)
        _exact_enum(self.created_by, TaskCreator, code)
        _exact_enum(self.approval_requirement, ApprovalRequirement, code)
        created_at = _canonical_time(self.created_at, code)
        if normalize:
            object.__setattr__(self, "created_at", created_at)
        if self.expires_at is not None:
            expires_at = _canonical_time(self.expires_at, code)
            if normalize:
                object.__setattr__(self, "expires_at", expires_at)
            if self.expires_at <= self.created_at:
                _fail(code)
        _idempotency_key(self.idempotency_key, code)
        objective = _safe_text(self.objective, maximum=MAX_OBJECTIVE_LENGTH, code=code)
        if normalize:
            object.__setattr__(self, "objective", objective)
        elif objective != self.objective:
            _fail(code)
        if type(self.evidence_references) is not tuple or len(self.evidence_references) > MAX_EVIDENCE_REFERENCES:
            _fail(code)
        if any(type(item) is not EvidenceReference for item in self.evidence_references):
            _fail(code)
        if any(item.captured_at > self.created_at for item in self.evidence_references):
            _fail(code)
        if self.current_policy_version != A1_POLICY_VERSION or type(self.execution_budget) is not AgentExecutionBudget:
            _fail(code)
        _bounded_int(self.revision_number, minimum=1, maximum=MAX_TASK_REVISION, code=code)
        specification = CAPABILITY_TAXONOMY[self.requested_capability]
        if (
            specification.company_function is not self.company_function
            or _CLASSIFICATION_ORDER[self.required_data_classification]
            > _CLASSIFICATION_ORDER[specification.maximum_data_classification]
            or _RISK_ORDER[self.calculated_risk] < _RISK_ORDER[specification.minimum_risk]
            or self.approval_requirement
            is not approval_requirement_for(self.requested_capability, self.calculated_risk)
        ):
            _fail(code)

    def __repr__(self) -> str:
        return (
            "AgentTask("
            f"task_id={self.task_id!r}, kind={self.task_kind.value!r}, "
            f"function={self.company_function.value!r}, environment={self.environment.value!r}, "
            f"capability={self.requested_capability.value!r}, "
            f"classification={self.required_data_classification.value!r}, risk={self.calculated_risk.value!r}, "
            f"lifecycle={self.lifecycle.value!r}, revision={self.revision_number}, objective=<redacted>, "
            f"evidence_references={len(self.evidence_references)})"
        )

    __str__ = __repr__


def task_proposal_fingerprint(task: AgentTask) -> str:
    if type(task) is not AgentTask:
        _fail(AgentOperationsErrorCode.INVALID_TASK)
    payload = {
        field.name: getattr(task, field.name)
        for field in fields(task)
        if field.name not in {"task_id", "lifecycle"} and not field.name.startswith("_")
    }
    return _fingerprint("task-proposal", payload)


_TRUSTED_APPROVAL_ISSUANCE_STAMP = object()


class TrustedHumanApproval:
    """Sealed authority receipt; A1 intentionally provides no runtime issuer."""

    __slots__ = (
        "_payload",
        "_issuance_stamp",
        "_seal",
        "_issuance_nonce",
        "_self_reference",
        "_instance_attestation",
        "__weakref__",
    )

    def __new__(cls, *_args, **_kwargs):
        raise TypeError("trusted_human_approval_required")

    def __setattr__(self, _name, _value):
        raise AttributeError("trusted_human_approval_is_immutable")

    def __repr__(self) -> str:
        return "TrustedHumanApproval(<redacted>)"

    __str__ = __repr__

    def __reduce_ex__(self, _protocol):
        raise TypeError("trusted_human_approval_not_serializable")

    def __copy__(self):
        raise TypeError("trusted_human_approval_not_copyable")

    def __deepcopy__(self, _memo):
        raise TypeError("trusted_human_approval_not_copyable")

    def __init_subclass__(cls, **_kwargs):
        raise TypeError("trusted_human_approval_not_subclassable")

    @property
    def approval_id(self):
        return self._payload["approval_id"]

    @property
    def task_id(self):
        return self._payload["task_id"]

    @property
    def task_revision(self):
        return self._payload["task_revision"]

    @property
    def approval_scope(self):
        return self._payload["approval_scope"]

    @property
    def task_proposal_fingerprint(self):
        return self._payload["task_proposal_fingerprint"]

    @property
    def environment(self):
        return self._payload["environment"]

    @property
    def approved_capabilities(self):
        return self._payload["approved_capabilities"]

    @property
    def approved_risk_ceiling(self):
        return self._payload["approved_risk_ceiling"]

    @property
    def approved_tool_intent_fingerprints(self):
        return self._payload["approved_tool_intent_fingerprints"]

    @property
    def approved_intent_bundle_fingerprint(self):
        return self._payload["approved_intent_bundle_fingerprint"]

    @property
    def approved_at(self):
        return self._payload["approved_at"]

    @property
    def expires_at(self):
        return self._payload["expires_at"]

    @property
    def policy_version(self):
        return self._payload["policy_version"]


def _validate_approval_payload(payload: Mapping[str, Any]) -> MappingProxyType:
    code = AgentOperationsErrorCode.INVALID_APPROVAL
    required = {
        "approval_id",
        "task_id",
        "task_revision",
        "task_proposal_fingerprint",
        "environment",
        "approval_scope",
        "approved_capabilities",
        "approved_risk_ceiling",
        "approved_tool_intent_fingerprints",
        "approved_intent_bundle_fingerprint",
        "approved_at",
        "expires_at",
        "policy_version",
    }
    if type(payload) is not dict or set(payload) != required:
        _fail(code)
    _identifier(payload["approval_id"], "approval", code)
    _identifier(payload["task_id"], "task", code)
    _bounded_int(payload["task_revision"], minimum=1, maximum=MAX_TASK_REVISION, code=code)
    _digest(payload["task_proposal_fingerprint"], code)
    _exact_enum(payload["environment"], AgentEnvironment, code)
    _exact_enum(payload["approval_scope"], ApprovalScope, code)
    if (
        type(payload["approved_capabilities"]) is not frozenset
        or not payload["approved_capabilities"]
        or any(type(item) is not Capability for item in payload["approved_capabilities"])
    ):
        _fail(code)
    _exact_enum(payload["approved_risk_ceiling"], RiskLevel, code)
    fingerprints = payload["approved_tool_intent_fingerprints"]
    if type(fingerprints) is not tuple or not fingerprints or len(fingerprints) > MAX_APPROVAL_REFERENCES:
        _fail(code)
    for fingerprint in fingerprints:
        _digest(fingerprint, code)
    _digest(payload["approved_intent_bundle_fingerprint"], code)
    approved_at = _canonical_time(payload["approved_at"], code)
    expires_at = _canonical_time(payload["expires_at"], code)
    if expires_at <= approved_at or payload["policy_version"] != A1_POLICY_VERSION:
        _fail(code)
    copied = dict(payload)
    copied["approved_at"] = approved_at
    copied["expires_at"] = expires_at
    return MappingProxyType(copied)


def _approval_payload_seal(payload: Mapping[str, Any]) -> str:
    return _fingerprint("trusted-human-approval-seal", dict(payload))


def approval_fingerprint(approval: TrustedHumanApproval) -> str:
    if not _is_trusted_approval(approval):
        _fail(AgentOperationsErrorCode.INVALID_APPROVAL)
    payload = dict(approval._payload)
    payload.pop("approval_id")
    return _fingerprint("trusted-human-approval", payload)


def _approval_matches_task(approval: TrustedHumanApproval, task: AgentTask, at: datetime) -> tuple[bool, AgentOperationsErrorCode]:
    if not _is_trusted_approval(approval):
        return False, AgentOperationsErrorCode.INVALID_APPROVAL
    if (
        approval.task_id != task.task_id
        or approval.task_revision != task.revision_number
        or not hmac.compare_digest(approval.task_proposal_fingerprint, task_proposal_fingerprint(task))
        or approval.environment is not task.environment
        or approval.policy_version != task.current_policy_version
        or task.requested_capability not in approval.approved_capabilities
        or _RISK_ORDER[task.calculated_risk] > _RISK_ORDER[approval.approved_risk_ceiling]
    ):
        return False, AgentOperationsErrorCode.APPROVAL_MISMATCH
    if approval.expires_at <= at or approval.approved_at > at:
        return False, AgentOperationsErrorCode.APPROVAL_EXPIRED
    return True, AgentOperationsErrorCode.INTERNAL_CONSISTENCY_FAILURE


def _build_task_transition_boundary():
    """Build one nonreusable construction boundary for validated transitions."""

    transition_authority = object()
    lineage_key = secrets.token_bytes(32)

    def task_transition_lineage(task: AgentTask) -> str:
        payload = {
            item.name: getattr(task, item.name)
            for item in fields(task)
            if not item.name.startswith("_")
        }
        message = canonical_json_bytes(
            {
                "domain": "agent-task-transition-lineage",
                "payload": payload,
            }
        )
        return hmac.new(lineage_key, message, hashlib.sha256).hexdigest()

    def is_valid_task_transition_lineage(task: AgentTask) -> bool:
        if task.lifecycle is TaskLifecycle.PROPOSED:
            return task._transition_lineage is None
        if type(task._transition_lineage) is not str or not _DIGEST.fullmatch(task._transition_lineage):
            return False
        return hmac.compare_digest(task._transition_lineage, task_transition_lineage(task))

    def consume_task_transition_authority(authority: Any, task: AgentTask) -> str:
        if authority is not transition_authority:
            _fail(AgentOperationsErrorCode.INVALID_TASK)
        return task_transition_lineage(task)

    def transition_task(
        task: AgentTask,
        target: TaskLifecycle,
        *,
        at: datetime,
        approval: TrustedHumanApproval | None = None,
    ) -> AgentTask:
        code = AgentOperationsErrorCode.INVALID_TRANSITION
        if type(task) is not AgentTask:
            _fail(code)
        try:
            if not is_valid_task_transition_lineage(task):
                _fail(code)
            task._validate_domain_fields(normalize=False)
        except AgentOperationsError:
            _fail(code)
        _exact_enum(target, TaskLifecycle, code)
        at = _canonical_time(at, code)
        if target not in TASK_TRANSITIONS[task.lifecycle]:
            _fail(code)
        if task.expires_at is not None and at >= task.expires_at:
            if target is not TaskLifecycle.EXPIRED:
                _fail(code)
        elif target is TaskLifecycle.EXPIRED:
            _fail(code)
        if task.lifecycle is TaskLifecycle.PROPOSED:
            if target is TaskLifecycle.APPROVED and task.approval_requirement is not ApprovalRequirement.NONE:
                _fail(code)
            if target is TaskLifecycle.AWAITING_APPROVAL and task.approval_requirement is not ApprovalRequirement.HUMAN:
                _fail(code)
        if target is TaskLifecycle.APPROVED and task.lifecycle is TaskLifecycle.AWAITING_APPROVAL:
            if approval is None:
                _fail(AgentOperationsErrorCode.APPROVAL_REQUIRED)
            matches, mismatch_code = _approval_matches_task(approval, task, at)
            if not matches:
                _fail(mismatch_code)
        return replace(task, lifecycle=target, _transition_authority=transition_authority)

    return consume_task_transition_authority, is_valid_task_transition_lineage, transition_task


(
    _consume_task_transition_authority,
    _is_valid_task_transition_lineage,
    transition_task,
) = _build_task_transition_boundary()
del _build_task_transition_boundary


_EXPECTED_SIDE_EFFECT = MappingProxyType(
    {
        ActionCategory.OBSERVE: SideEffectClass.NONE,
        ActionCategory.ANALYZE: SideEffectClass.NONE,
        ActionCategory.DRAFT: SideEffectClass.INTERNAL_DRAFT_ONLY,
        ActionCategory.PROPOSE: SideEffectClass.NONE,
        ActionCategory.TEST: SideEffectClass.TEST_EXECUTION,
        ActionCategory.MODIFY_INTERNAL: SideEffectClass.INTERNAL_STATE_CHANGE,
        ActionCategory.PUBLISH: SideEffectClass.CONTENT_PUBLICATION,
        ActionCategory.COMMUNICATE_EXTERNAL: SideEffectClass.EXTERNAL_MESSAGE,
        ActionCategory.DEPLOY: SideEffectClass.DEPLOYMENT,
        ActionCategory.FINANCIAL: SideEffectClass.FINANCIAL_TRANSACTION,
        ActionCategory.LEGAL: SideEffectClass.LEGAL_ACTION,
        ActionCategory.SECURITY_SENSITIVE: SideEffectClass.SECURITY_SENSITIVE,
        ActionCategory.DELETE_DATA: SideEffectClass.USER_DATA_DELETION,
    }
)

_CAPABILITY_TOOL_KINDS = MappingProxyType(
    {
        Capability.READ_JOB_INVENTORY_SUMMARY: frozenset({ToolKind.JOB_INVENTORY}),
        Capability.READ_SOURCE_HEALTH: frozenset({ToolKind.INTERNAL_METRICS, ToolKind.CRAWLER_CONTROL}),
        Capability.READ_MATCHING_QUALITY_METRICS: frozenset({ToolKind.INTERNAL_METRICS}),
        Capability.READ_TEST_AND_BUILD_STATUS: frozenset({ToolKind.TEST_RUNNER, ToolKind.SOURCE_CONTROL}),
        Capability.READ_PRODUCT_METRICS: frozenset({ToolKind.INTERNAL_METRICS}),
        Capability.READ_SEO_METRICS: frozenset({ToolKind.INTERNAL_METRICS}),
        Capability.READ_SUPPORT_QUEUE_SUMMARY: frozenset({ToolKind.SUPPORT_DRAFTS}),
        Capability.READ_B2B_PIPELINE_SUMMARY: frozenset({ToolKind.B2B_CRM}),
        Capability.CREATE_INTERNAL_BRIEFING: frozenset({ToolKind.INTERNAL_METRICS}),
        Capability.PROPOSE_DATA_QUALITY_TASK: frozenset({ToolKind.INTERNAL_METRICS, ToolKind.JOB_INVENTORY}),
        Capability.PROPOSE_CONTENT_UPDATE: frozenset({ToolKind.CONTENT_MANAGEMENT}),
        Capability.PROPOSE_SUPPORT_REPLY: frozenset({ToolKind.SUPPORT_DRAFTS}),
        Capability.PROPOSE_B2B_OUTREACH: frozenset({ToolKind.B2B_CRM}),
        Capability.PROPOSE_CODE_CHANGE: frozenset({ToolKind.SOURCE_CONTROL}),
        Capability.PROPOSE_PRODUCT_EXPERIMENT: frozenset({ToolKind.INTERNAL_METRICS}),
        Capability.EXECUTE_READ_ONLY_ANALYSIS: frozenset({ToolKind.INTERNAL_METRICS}),
        Capability.EXECUTE_TEST_SUITE: frozenset({ToolKind.TEST_RUNNER}),
        Capability.CREATE_DRAFT_CONTENT: frozenset({ToolKind.CONTENT_MANAGEMENT}),
        Capability.CREATE_DRAFT_MESSAGE: frozenset({ToolKind.SUPPORT_DRAFTS}),
        Capability.CREATE_DRAFT_PULL_REQUEST: frozenset({ToolKind.SOURCE_CONTROL}),
        Capability.MODIFY_JOB_METADATA: frozenset({ToolKind.JOB_INVENTORY}),
        Capability.ARCHIVE_EXPIRED_JOBS: frozenset({ToolKind.JOB_INVENTORY}),
        Capability.PUBLISH_CONTENT: frozenset({ToolKind.CONTENT_MANAGEMENT}),
        Capability.SEND_SUPPORT_MESSAGE: frozenset({ToolKind.COMMUNICATIONS}),
        Capability.SEND_B2B_OUTREACH: frozenset({ToolKind.COMMUNICATIONS}),
        Capability.MERGE_PULL_REQUEST: frozenset({ToolKind.SOURCE_CONTROL}),
        Capability.DEPLOY_PRODUCTION: frozenset({ToolKind.DEPLOYMENT}),
        Capability.MODIFY_PRICING: frozenset({ToolKind.BILLING}),
        Capability.SPEND_MONEY: frozenset({ToolKind.FINANCE}),
        Capability.MODIFY_BILLING: frozenset({ToolKind.BILLING}),
        Capability.EXECUTE_BANK_TRANSACTION: frozenset({ToolKind.FINANCE}),
        Capability.SUBMIT_TAX_FILING: frozenset({ToolKind.FINANCE}),
        Capability.SIGN_CONTRACT: frozenset({ToolKind.COMMUNICATIONS}),
        Capability.ACCESS_SECRETS: frozenset({ToolKind.DEPLOYMENT}),
        Capability.DELETE_USER_DATA: frozenset({ToolKind.INTERNAL_METRICS}),
        Capability.MODIFY_ACCOUNT_OWNERSHIP: frozenset({ToolKind.INTERNAL_METRICS}),
    }
)


_ALL_ENVIRONMENTS = frozenset(AgentEnvironment)
_NON_PRODUCTION_ENVIRONMENTS = frozenset(
    {AgentEnvironment.DEVELOPMENT, AgentEnvironment.TEST, AgentEnvironment.STAGING}
)


@dataclass(frozen=True, slots=True)
class OperationSpecification:
    operation: AgentOperation
    permitted_tool_kinds: frozenset[ToolKind]
    permitted_capabilities: frozenset[Capability]
    required_action_category: ActionCategory
    expected_side_effect_class: SideEffectClass
    minimum_risk: RiskLevel
    maximum_target_data_classification: DataClassification
    reversible: bool
    rollback_plan_required: bool
    human_approval_always_required: bool
    prohibited_in_a1: bool
    allowed_environments: frozenset[AgentEnvironment]
    documentation_safe_meaning: str


_DRAFT_CAPABILITIES = frozenset(
    {
        Capability.CREATE_INTERNAL_BRIEFING,
        Capability.CREATE_DRAFT_CONTENT,
        Capability.CREATE_DRAFT_MESSAGE,
    }
)
_PROPOSAL_CAPABILITIES = frozenset(
    {
        Capability.PROPOSE_DATA_QUALITY_TASK,
        Capability.PROPOSE_CONTENT_UPDATE,
        Capability.PROPOSE_SUPPORT_REPLY,
        Capability.PROPOSE_B2B_OUTREACH,
        Capability.PROPOSE_CODE_CHANGE,
        Capability.PROPOSE_PRODUCT_EXPERIMENT,
    }
)
_OBSERVATION_CAPABILITIES = frozenset(
    {
        Capability.READ_JOB_INVENTORY_SUMMARY,
        Capability.READ_SOURCE_HEALTH,
        Capability.READ_MATCHING_QUALITY_METRICS,
        Capability.READ_TEST_AND_BUILD_STATUS,
        Capability.READ_PRODUCT_METRICS,
        Capability.READ_SEO_METRICS,
        Capability.READ_SUPPORT_QUEUE_SUMMARY,
        Capability.READ_B2B_PIPELINE_SUMMARY,
        Capability.EXECUTE_READ_ONLY_ANALYSIS,
    }
)


def _side_effect_for_capability(capability: Capability) -> SideEffectClass:
    if capability in _DRAFT_CAPABILITIES:
        return SideEffectClass.INTERNAL_DRAFT_ONLY
    if capability in _PROPOSAL_CAPABILITIES or capability in _OBSERVATION_CAPABILITIES:
        return SideEffectClass.NONE
    if capability is Capability.EXECUTE_TEST_SUITE:
        return SideEffectClass.TEST_EXECUTION
    if capability in {Capability.MODIFY_JOB_METADATA, Capability.ARCHIVE_EXPIRED_JOBS}:
        return SideEffectClass.INTERNAL_STATE_CHANGE
    if capability is Capability.CREATE_DRAFT_PULL_REQUEST:
        return SideEffectClass.INTERNAL_DRAFT_ONLY
    if capability is Capability.PUBLISH_CONTENT:
        return SideEffectClass.CONTENT_PUBLICATION
    if capability in {Capability.SEND_SUPPORT_MESSAGE, Capability.SEND_B2B_OUTREACH}:
        return SideEffectClass.EXTERNAL_MESSAGE
    if capability is Capability.MERGE_PULL_REQUEST:
        return SideEffectClass.SOURCE_CONTROL_CHANGE
    if capability is Capability.DEPLOY_PRODUCTION:
        return SideEffectClass.DEPLOYMENT
    if capability in {Capability.MODIFY_PRICING, Capability.MODIFY_BILLING}:
        return SideEffectClass.BILLING_OR_PRICING
    if capability in {Capability.SPEND_MONEY, Capability.EXECUTE_BANK_TRANSACTION}:
        return SideEffectClass.FINANCIAL_TRANSACTION
    if capability in {Capability.SUBMIT_TAX_FILING, Capability.SIGN_CONTRACT}:
        return SideEffectClass.LEGAL_ACTION
    if capability is Capability.ACCESS_SECRETS:
        return SideEffectClass.SECURITY_SENSITIVE
    if capability is Capability.DELETE_USER_DATA:
        return SideEffectClass.USER_DATA_DELETION
    if capability is Capability.MODIFY_ACCOUNT_OWNERSHIP:
        return SideEffectClass.OWNERSHIP_MUTATION
    _fail(AgentOperationsErrorCode.INTERNAL_CONSISTENCY_FAILURE)


def _operation_specification(capability: Capability) -> OperationSpecification:
    capability_specification = CAPABILITY_TAXONOMY[capability]
    effect = _side_effect_for_capability(capability)
    reversible = effect in {
        SideEffectClass.INTERNAL_DRAFT_ONLY,
        SideEffectClass.INTERNAL_STATE_CHANGE,
        SideEffectClass.CONTENT_PUBLICATION,
        SideEffectClass.SOURCE_CONTROL_CHANGE,
        SideEffectClass.BILLING_OR_PRICING,
    }
    rollback_required = capability_specification.minimum_risk is RiskLevel.HIGH and reversible
    prohibited = capability in A1_PROHIBITED_CAPABILITIES or effect in {
        SideEffectClass.EXTERNAL_MESSAGE,
        SideEffectClass.DEPLOYMENT,
        SideEffectClass.BILLING_OR_PRICING,
        SideEffectClass.FINANCIAL_TRANSACTION,
        SideEffectClass.LEGAL_ACTION,
        SideEffectClass.SECURITY_SENSITIVE,
        SideEffectClass.USER_DATA_DELETION,
        SideEffectClass.OWNERSHIP_MUTATION,
    }
    return OperationSpecification(
        operation=AgentOperation(capability.value),
        permitted_tool_kinds=_CAPABILITY_TOOL_KINDS[capability],
        permitted_capabilities=frozenset({capability}),
        required_action_category=capability_specification.action_category,
        expected_side_effect_class=effect,
        minimum_risk=capability_specification.minimum_risk,
        maximum_target_data_classification=capability_specification.maximum_data_classification,
        reversible=reversible,
        rollback_plan_required=rollback_required,
        human_approval_always_required=capability_specification.human_approval_always_required,
        prohibited_in_a1=prohibited,
        allowed_environments=(
            _ALL_ENVIRONMENTS if capability in _OBSERVATION_CAPABILITIES else _NON_PRODUCTION_ENVIRONMENTS
        ),
        documentation_safe_meaning=capability_specification.documentation_safe_meaning,
    )


_OPERATION_SPECIFICATIONS = tuple(_operation_specification(capability) for capability in Capability) + (
    OperationSpecification(
        operation=AgentOperation.START_CRAWLER,
        permitted_tool_kinds=frozenset({ToolKind.CRAWLER_CONTROL}),
        permitted_capabilities=frozenset({Capability.READ_SOURCE_HEALTH}),
        required_action_category=ActionCategory.MODIFY_INTERNAL,
        expected_side_effect_class=SideEffectClass.INTERNAL_STATE_CHANGE,
        minimum_risk=RiskLevel.HIGH,
        maximum_target_data_classification=DataClassification.INTERNAL,
        reversible=True,
        rollback_plan_required=True,
        human_approval_always_required=True,
        prohibited_in_a1=True,
        allowed_environments=_NON_PRODUCTION_ENVIRONMENTS,
        documentation_safe_meaning="Start one crawler only in a future explicitly enabled runtime.",
    ),
    OperationSpecification(
        operation=AgentOperation.STOP_CRAWLER,
        permitted_tool_kinds=frozenset({ToolKind.CRAWLER_CONTROL}),
        permitted_capabilities=frozenset({Capability.READ_SOURCE_HEALTH}),
        required_action_category=ActionCategory.MODIFY_INTERNAL,
        expected_side_effect_class=SideEffectClass.INTERNAL_STATE_CHANGE,
        minimum_risk=RiskLevel.HIGH,
        maximum_target_data_classification=DataClassification.INTERNAL,
        reversible=True,
        rollback_plan_required=True,
        human_approval_always_required=True,
        prohibited_in_a1=True,
        allowed_environments=_NON_PRODUCTION_ENVIRONMENTS,
        documentation_safe_meaning="Stop one crawler only in a future explicitly enabled runtime.",
    ),
)
OPERATION_TAXONOMY = MappingProxyType(
    {specification.operation: specification for specification in _OPERATION_SPECIFICATIONS}
)


@dataclass(frozen=True, slots=True, repr=False)
class AgentToolIntent:
    intent_id: str
    capability: Capability
    tool_kind: ToolKind
    operation: AgentOperation
    environment: AgentEnvironment
    proposed_at: datetime
    action_category: ActionCategory
    target_classification: DataClassification
    risk: RiskLevel
    idempotency_key: str
    sanitized_argument_summary: str
    expected_side_effect_class: SideEffectClass
    reversible: bool
    rollback_plan_summary: str | None
    evidence_references: tuple[EvidenceReference, ...]
    estimated_cost_units: int
    estimated_external_messages: int
    estimated_modified_records: int
    estimated_content_publications: int
    timeout_seconds: int
    argument_fingerprint: str = ""

    def __post_init__(self):
        code = AgentOperationsErrorCode.INVALID_TOOL_INTENT
        _identifier(self.intent_id, "intent", code)
        _exact_enum(self.capability, Capability, code)
        _exact_enum(self.tool_kind, ToolKind, code)
        _exact_enum(self.operation, AgentOperation, code)
        _exact_enum(self.environment, AgentEnvironment, code)
        object.__setattr__(self, "proposed_at", _canonical_time(self.proposed_at, code))
        _exact_enum(self.action_category, ActionCategory, code)
        _exact_enum(self.target_classification, DataClassification, code)
        _exact_enum(self.risk, RiskLevel, code)
        _exact_enum(self.expected_side_effect_class, SideEffectClass, code)
        _idempotency_key(self.idempotency_key, code)
        summary = _safe_text(self.sanitized_argument_summary, maximum=MAX_SAFE_SUMMARY_LENGTH, code=code)
        object.__setattr__(self, "sanitized_argument_summary", summary)
        if type(self.reversible) is not bool:
            _fail(code)
        if self.rollback_plan_summary is not None:
            object.__setattr__(self, "rollback_plan_summary", _safe_text(self.rollback_plan_summary, maximum=MAX_SAFE_SUMMARY_LENGTH, code=code))
        if type(self.evidence_references) is not tuple or len(self.evidence_references) > MAX_EVIDENCE_REFERENCES:
            _fail(code)
        if any(type(item) is not EvidenceReference for item in self.evidence_references):
            _fail(code)
        if any(item.captured_at > self.proposed_at for item in self.evidence_references):
            _fail(code)
        _bounded_int(self.estimated_cost_units, minimum=0, maximum=1_000_000, code=code)
        _bounded_int(self.estimated_external_messages, minimum=0, maximum=1_000, code=code)
        _bounded_int(self.estimated_modified_records, minimum=0, maximum=100_000, code=code)
        _bounded_int(self.estimated_content_publications, minimum=0, maximum=1_000, code=code)
        _bounded_int(self.timeout_seconds, minimum=1, maximum=86_400, code=code)
        specification = CAPABILITY_TAXONOMY[self.capability]
        operation_specification = OPERATION_TAXONOMY[self.operation]
        if (
            specification.action_category is not self.action_category
            or self.capability not in operation_specification.permitted_capabilities
            or self.tool_kind not in operation_specification.permitted_tool_kinds
            or operation_specification.required_action_category is not self.action_category
            or _CLASSIFICATION_ORDER[self.target_classification]
            > _CLASSIFICATION_ORDER[specification.maximum_data_classification]
            or _CLASSIFICATION_ORDER[self.target_classification]
            > _CLASSIFICATION_ORDER[operation_specification.maximum_target_data_classification]
            or _RISK_ORDER[self.risk] < _RISK_ORDER[specification.minimum_risk]
            or _RISK_ORDER[self.risk] < _RISK_ORDER[operation_specification.minimum_risk]
            or self.expected_side_effect_class is not operation_specification.expected_side_effect_class
            or self.reversible is not operation_specification.reversible
            or self.environment not in operation_specification.allowed_environments
        ):
            _fail(code)
        expected_external_messages = 1 if self.expected_side_effect_class is SideEffectClass.EXTERNAL_MESSAGE else 0
        expected_modified_records = 1 if self.expected_side_effect_class in {
            SideEffectClass.INTERNAL_STATE_CHANGE,
            SideEffectClass.SOURCE_CONTROL_CHANGE,
            SideEffectClass.BILLING_OR_PRICING,
            SideEffectClass.USER_DATA_DELETION,
            SideEffectClass.OWNERSHIP_MUTATION,
        } else 0
        expected_publications = 1 if self.expected_side_effect_class is SideEffectClass.CONTENT_PUBLICATION else 0
        if (
            self.estimated_external_messages != expected_external_messages
            or self.estimated_modified_records != expected_modified_records
            or self.estimated_content_publications != expected_publications
            or (self.action_category in READ_ONLY_ACTION_CATEGORIES and _SIDE_EFFECT_WORDS.search(summary))
        ):
            _fail(code)
        if operation_specification.rollback_plan_required and self.rollback_plan_summary is None:
            _fail(code)
        if not operation_specification.rollback_plan_required and self.rollback_plan_summary is not None:
            _fail(code)
        expected_argument_fingerprint = _fingerprint(
            "tool-arguments",
            {"operation": self.operation, "sanitized_argument_summary": summary},
        )
        if self.argument_fingerprint not in ("", expected_argument_fingerprint):
            _fail(code)
        object.__setattr__(self, "argument_fingerprint", expected_argument_fingerprint)

    @property
    def fingerprint(self) -> str:
        return tool_intent_fingerprint(self)

    def __repr__(self) -> str:
        return (
            "AgentToolIntent("
            f"intent_id={self.intent_id!r}, capability={self.capability.value!r}, "
            f"tool_kind={self.tool_kind.value!r}, operation={self.operation!r}, "
            f"category={self.action_category.value!r}, classification={self.target_classification.value!r}, "
            f"risk={self.risk.value!r}, argument_summary=<redacted>, fingerprint={self.fingerprint!r})"
        )

    __str__ = __repr__


def tool_intent_fingerprint(intent: AgentToolIntent) -> str:
    if type(intent) is not AgentToolIntent:
        _fail(AgentOperationsErrorCode.INVALID_TOOL_INTENT)
    payload = {field.name: getattr(intent, field.name) for field in fields(intent) if field.name != "intent_id"}
    return _fingerprint("agent-tool-intent", payload)


def ordered_intent_bundle_fingerprint(intents: tuple[AgentToolIntent, ...]) -> str:
    if (
        type(intents) is not tuple
        or not intents
        or len(intents) > MAX_APPROVAL_REFERENCES
        or any(type(intent) is not AgentToolIntent for intent in intents)
    ):
        _fail(AgentOperationsErrorCode.INVALID_APPROVAL)
    return _fingerprint(
        "ordered-tool-intent-bundle",
        {
            "count": len(intents),
            "intents": tuple(
                {"position": position, "fingerprint": intent.fingerprint}
                for position, intent in enumerate(intents)
            ),
        },
    )


def _build_test_approval_authority():
    """Create one registry-free private authority for A1 validation and tests."""

    signing_key = secrets.token_bytes(32)

    def instance_attestation(
        approval: TrustedHumanApproval,
        payload_seal: str,
        issuance_nonce: bytes,
        self_reference: weakref.ReferenceType,
    ) -> str:
        message = b"\x00".join(
            (
                b"wahojobs-trusted-approval-instance-v1",
                payload_seal.encode("ascii"),
                issuance_nonce.hex().encode("ascii"),
                format(id(approval), "x").encode("ascii"),
                format(id(self_reference), "x").encode("ascii"),
                format(id(_TRUSTED_APPROVAL_ISSUANCE_STAMP), "x").encode("ascii"),
            )
        )
        return hmac.new(signing_key, message, hashlib.sha256).hexdigest()

    def is_trusted_approval(value: Any) -> bool:
        if type(value) is not TrustedHumanApproval:
            return False
        try:
            payload = value._payload
            issuance_stamp = value._issuance_stamp
            payload_seal = value._seal
            issuance_nonce = value._issuance_nonce
            self_reference = value._self_reference
            stored_attestation = value._instance_attestation
            if (
                type(payload) is not type(MappingProxyType({}))
                or issuance_stamp is not _TRUSTED_APPROVAL_ISSUANCE_STAMP
                or type(payload_seal) is not str
                or not _DIGEST.fullmatch(payload_seal)
                or type(issuance_nonce) is not bytes
                or len(issuance_nonce) != 32
                or type(self_reference) is not weakref.ReferenceType
                or type(stored_attestation) is not str
                or not _DIGEST.fullmatch(stored_attestation)
                or hasattr(value, "__dict__")
            ):
                return False
            if self_reference() is not value:
                return False
            expected_attestation = instance_attestation(
                value,
                payload_seal,
                issuance_nonce,
                self_reference,
            )
            if not hmac.compare_digest(stored_attestation, expected_attestation):
                return False
            current_payload_seal = _approval_payload_seal(
                _validate_approval_payload(dict(payload))
            )
        except (AgentOperationsError, AttributeError, TypeError, ValueError):
            return False
        return hmac.compare_digest(payload_seal, current_payload_seal)

    def issue_trusted_approval_for_tests(
        task: AgentTask,
        intents: tuple[AgentToolIntent, ...],
        *,
        approval_id: str,
        approved_at: datetime,
        expires_at: datetime,
        approved_capabilities: frozenset[Capability] | None = None,
        approved_risk_ceiling: RiskLevel | None = None,
    ) -> TrustedHumanApproval:
        """Private deterministic issuer used only by explicit A1 test support."""

        code = AgentOperationsErrorCode.INVALID_APPROVAL
        if type(task) is not AgentTask:
            _fail(code)
        bundle_fingerprint = ordered_intent_bundle_fingerprint(intents)
        approved_at = _canonical_time(approved_at, code)
        if approved_at < task.created_at or any(
            intent.environment is not task.environment or approved_at < intent.proposed_at
            for intent in intents
        ):
            _fail(code)
        payload = _validate_approval_payload(
            {
                "approval_id": approval_id,
                "task_id": task.task_id,
                "task_revision": task.revision_number,
                "task_proposal_fingerprint": task_proposal_fingerprint(task),
                "environment": task.environment,
                "approval_scope": ApprovalScope.EXACT_TOOL_INTENTS,
                "approved_capabilities": approved_capabilities or frozenset({task.requested_capability}),
                "approved_risk_ceiling": approved_risk_ceiling or task.calculated_risk,
                "approved_tool_intent_fingerprints": tuple(intent.fingerprint for intent in intents),
                "approved_intent_bundle_fingerprint": bundle_fingerprint,
                "approved_at": approved_at,
                "expires_at": expires_at,
                "policy_version": A1_POLICY_VERSION,
            }
        )
        approval = object.__new__(TrustedHumanApproval)
        payload_seal = _approval_payload_seal(payload)
        issuance_nonce = secrets.token_bytes(32)
        self_reference = weakref.ref(approval)
        attestation = instance_attestation(
            approval,
            payload_seal,
            issuance_nonce,
            self_reference,
        )
        object.__setattr__(approval, "_payload", payload)
        object.__setattr__(approval, "_issuance_stamp", _TRUSTED_APPROVAL_ISSUANCE_STAMP)
        object.__setattr__(approval, "_seal", payload_seal)
        object.__setattr__(approval, "_issuance_nonce", issuance_nonce)
        object.__setattr__(approval, "_self_reference", self_reference)
        object.__setattr__(approval, "_instance_attestation", attestation)
        return approval

    return is_trusted_approval, issue_trusted_approval_for_tests


_is_trusted_approval, _issue_trusted_approval_for_tests = _build_test_approval_authority()
del _build_test_approval_authority


@dataclass(frozen=True, slots=True)
class AgentPolicyDecision:
    decision: PolicyDecision
    reason_code: AgentOperationsErrorCode | None
    effective_risk: RiskLevel
    policy_version: str = A1_POLICY_VERSION

    def __post_init__(self):
        _exact_enum(self.decision, PolicyDecision, AgentOperationsErrorCode.INTERNAL_CONSISTENCY_FAILURE)
        if self.reason_code is not None:
            _exact_enum(self.reason_code, AgentOperationsErrorCode, AgentOperationsErrorCode.INTERNAL_CONSISTENCY_FAILURE)
        _exact_enum(self.effective_risk, RiskLevel, AgentOperationsErrorCode.INTERNAL_CONSISTENCY_FAILURE)
        if self.policy_version != A1_POLICY_VERSION:
            _fail(AgentOperationsErrorCode.INTERNAL_CONSISTENCY_FAILURE)


def _decision(decision: PolicyDecision, reason: AgentOperationsErrorCode | None, risk: RiskLevel) -> AgentPolicyDecision:
    return AgentPolicyDecision(decision, reason, risk)


def _budget_allows(budget: AgentExecutionBudget, state: AgentBudgetState, intent: AgentToolIntent) -> bool:
    return all(
        (
            state.tool_intents_used < budget.maximum_tool_intents_per_task,
            state.execution_attempts_used < budget.maximum_execution_attempts,
            state.concurrent_tasks < budget.maximum_concurrent_tasks,
            state.cost_units_used + intent.estimated_cost_units <= budget.maximum_cost_units,
            state.external_messages_used + intent.estimated_external_messages <= budget.maximum_external_messages,
            state.modified_records_used + intent.estimated_modified_records <= budget.maximum_modified_records,
            state.content_publications_used + intent.estimated_content_publications <= budget.maximum_content_publications,
            state.runtime_seconds_used + intent.timeout_seconds <= budget.maximum_runtime_seconds,
            intent.timeout_seconds <= budget.maximum_runtime_seconds,
        )
    )


def _task_budget_within_agent_budget(task: AgentExecutionBudget, agent: AgentExecutionBudget) -> bool:
    return all(
        getattr(task, field_name) <= getattr(agent, field_name)
        for field_name in (
            "maximum_tool_intents_per_task",
            "maximum_execution_attempts",
            "maximum_concurrent_tasks",
            "maximum_cost_units",
            "maximum_external_messages",
            "maximum_modified_records",
            "maximum_content_publications",
            "maximum_runtime_seconds",
        )
    )


def _effective_required_classification(task: AgentTask, intent: AgentToolIntent) -> DataClassification:
    classifications = [task.required_data_classification, intent.target_classification]
    classifications.extend(item.source_classification for item in task.evidence_references)
    classifications.extend(item.source_classification for item in intent.evidence_references)
    return max(classifications, key=lambda item: _CLASSIFICATION_ORDER[item])


def _evidence_is_current(evidence: tuple[EvidenceReference, ...], observed_at: datetime) -> bool:
    return all(item.captured_at <= observed_at < item.freshness_boundary for item in evidence)


def evaluate_agent_action(
    agent: AgentDefinition,
    task: AgentTask,
    tool_intent: AgentToolIntent,
    approval: TrustedHumanApproval | None = None,
    budget_state: AgentBudgetState | None = None,
    *,
    observed_at: datetime,
    intent_bundle: tuple[AgentToolIntent, ...] | None = None,
) -> AgentPolicyDecision:
    """Return one deterministic A1 policy result without executing anything."""

    fallback_risk = RiskLevel.CRITICAL
    if type(task) is AgentTask and type(task.calculated_risk) is RiskLevel:
        fallback_risk = task.calculated_risk
    if type(agent) is not AgentDefinition:
        return _decision(PolicyDecision.DENY, AgentOperationsErrorCode.INVALID_AGENT_DEFINITION, fallback_risk)
    if type(task) is not AgentTask:
        return _decision(PolicyDecision.DENY, AgentOperationsErrorCode.INVALID_TASK, fallback_risk)
    if type(tool_intent) is not AgentToolIntent:
        return _decision(PolicyDecision.DENY, AgentOperationsErrorCode.INVALID_TOOL_INTENT, fallback_risk)
    try:
        observed_at = _canonical_time(observed_at, AgentOperationsErrorCode.INVALID_TASK)
    except AgentOperationsError:
        return _decision(PolicyDecision.DENY, AgentOperationsErrorCode.INVALID_TASK, fallback_risk)
    raw_external_effect = tool_intent.estimated_external_messages
    if type(raw_external_effect) is int and raw_external_effect > 0:
        return _decision(PolicyDecision.PROHIBIT, AgentOperationsErrorCode.ACTION_PROHIBITED, fallback_risk)
    try:
        agent = replace(agent)
        if not _is_valid_task_transition_lineage(task):
            _fail(AgentOperationsErrorCode.INVALID_TASK)
        task._validate_domain_fields(normalize=False)
        tool_intent = replace(tool_intent)
    except AgentOperationsError as error:
        return _decision(PolicyDecision.DENY, error.code, fallback_risk)
    effective_risk = max(
        (task.calculated_risk, tool_intent.risk, CAPABILITY_TAXONOMY[tool_intent.capability].minimum_risk),
        key=lambda item: _RISK_ORDER[item],
    )
    if budget_state is not None and type(budget_state) is not AgentBudgetState:
        return _decision(PolicyDecision.DENY, AgentOperationsErrorCode.BUDGET_EXHAUSTED, effective_risk)
    if budget_state is not None and budget_state.observed_at != observed_at:
        return _decision(PolicyDecision.DENY, AgentOperationsErrorCode.BUDGET_EXHAUSTED, effective_risk)
    if agent.lifecycle is not AgentLifecycle.ACTIVE:
        return _decision(PolicyDecision.DENY, AgentOperationsErrorCode.INVALID_AGENT_DEFINITION, effective_risk)
    operation_specification = OPERATION_TAXONOMY[tool_intent.operation]
    if (
        agent.policy_version != task.current_policy_version
        or agent.policy_version != A1_POLICY_VERSION
        or agent.environment is not task.environment
        or task.environment is not tool_intent.environment
        or tool_intent.environment not in operation_specification.allowed_environments
        or task.company_function is not agent.company_function
        or task.requested_capability is not tool_intent.capability
        or CAPABILITY_TAXONOMY[tool_intent.capability].company_function is not agent.company_function
    ):
        return _decision(PolicyDecision.DENY, AgentOperationsErrorCode.CAPABILITY_DENIED, effective_risk)
    if tool_intent.capability not in agent.granted_capabilities:
        return _decision(PolicyDecision.DENY, AgentOperationsErrorCode.CAPABILITY_DENIED, effective_risk)
    specification = CAPABILITY_TAXONOMY[tool_intent.capability]
    effective_classification = _effective_required_classification(task, tool_intent)
    if (
        effective_classification not in agent.allowed_data_classifications
        or _CLASSIFICATION_ORDER[effective_classification]
        > _CLASSIFICATION_ORDER[specification.maximum_data_classification]
        or _CLASSIFICATION_ORDER[effective_classification]
        > _CLASSIFICATION_ORDER[operation_specification.maximum_target_data_classification]
        or effective_classification is DataClassification.RESTRICTED
    ):
        return _decision(PolicyDecision.DENY, AgentOperationsErrorCode.DATA_CLASSIFICATION_DENIED, effective_risk)
    if _RISK_ORDER[effective_risk] > _RISK_ORDER[agent.risk_ceiling]:
        return _decision(PolicyDecision.DENY, AgentOperationsErrorCode.RISK_CEILING_EXCEEDED, effective_risk)
    if (
        task.created_at > observed_at
        or tool_intent.proposed_at > observed_at
        or (task.expires_at is not None and observed_at >= task.expires_at)
        or not _evidence_is_current(task.evidence_references, observed_at)
        or not _evidence_is_current(tool_intent.evidence_references, observed_at)
    ):
        return _decision(PolicyDecision.DENY, AgentOperationsErrorCode.INVALID_TASK, effective_risk)
    if (
        effective_risk is RiskLevel.CRITICAL
        or tool_intent.capability in A1_PROHIBITED_CAPABILITIES
        or operation_specification.prohibited_in_a1
    ):
        return _decision(PolicyDecision.PROHIBIT, AgentOperationsErrorCode.ACTION_PROHIBITED, effective_risk)
    if task.lifecycle in TERMINAL_TASK_LIFECYCLES:
        return _decision(PolicyDecision.DENY, AgentOperationsErrorCode.INVALID_TASK, effective_risk)
    needs_approval = approval_requirement_for(tool_intent.capability, effective_risk) is ApprovalRequirement.HUMAN
    if task.lifecycle in {TaskLifecycle.PROPOSED, TaskLifecycle.AWAITING_APPROVAL, TaskLifecycle.NEEDS_HUMAN_INPUT}:
        if needs_approval:
            return _decision(PolicyDecision.REQUIRE_HUMAN_APPROVAL, AgentOperationsErrorCode.APPROVAL_REQUIRED, effective_risk)
        return _decision(PolicyDecision.DENY, AgentOperationsErrorCode.INVALID_TRANSITION, effective_risk)
    if task.lifecycle not in {TaskLifecycle.APPROVED, TaskLifecycle.RUNNING}:
        return _decision(PolicyDecision.DENY, AgentOperationsErrorCode.INVALID_TRANSITION, effective_risk)
    if not _task_budget_within_agent_budget(task.execution_budget, agent.execution_budget):
        return _decision(PolicyDecision.DENY, AgentOperationsErrorCode.BUDGET_EXHAUSTED, effective_risk)
    effective_budget_state = budget_state or AgentBudgetState(observed_at=observed_at)
    if not _budget_allows(task.execution_budget, effective_budget_state, tool_intent):
        return _decision(PolicyDecision.DENY, AgentOperationsErrorCode.BUDGET_EXHAUSTED, effective_risk)
    if tool_intent.action_category in READ_ONLY_ACTION_CATEGORIES and not needs_approval:
        if _SIDE_EFFECT_WORDS.search(tool_intent.sanitized_argument_summary):
            return _decision(PolicyDecision.PROHIBIT, AgentOperationsErrorCode.ACTION_PROHIBITED, effective_risk)
        return _decision(PolicyDecision.ALLOW_READ_ONLY, None, effective_risk)
    if approval is None:
        return _decision(PolicyDecision.REQUIRE_HUMAN_APPROVAL, AgentOperationsErrorCode.APPROVAL_REQUIRED, effective_risk)
    matches, mismatch_code = _approval_matches_task(approval, task, observed_at)
    if not matches:
        return _decision(PolicyDecision.DENY, mismatch_code, effective_risk)
    if intent_bundle is None:
        intent_bundle = (tool_intent,)
    if (
        type(intent_bundle) is not tuple
        or not intent_bundle
        or len(intent_bundle) > MAX_APPROVAL_REFERENCES
        or any(type(intent) is not AgentToolIntent for intent in intent_bundle)
    ):
        return _decision(PolicyDecision.DENY, AgentOperationsErrorCode.APPROVAL_MISMATCH, effective_risk)
    try:
        intent_bundle = tuple(replace(intent) for intent in intent_bundle)
        if any(
            intent.environment is not task.environment
            or intent.proposed_at > observed_at
            or not _evidence_is_current(intent.evidence_references, observed_at)
            for intent in intent_bundle
        ):
            return _decision(PolicyDecision.DENY, AgentOperationsErrorCode.APPROVAL_MISMATCH, effective_risk)
        current_bundle_fingerprint = ordered_intent_bundle_fingerprint(intent_bundle)
    except AgentOperationsError:
        return _decision(PolicyDecision.DENY, AgentOperationsErrorCode.APPROVAL_MISMATCH, effective_risk)
    current_fingerprints = tuple(intent.fingerprint for intent in intent_bundle)
    if (
        tool_intent.capability not in approval.approved_capabilities
        or _RISK_ORDER[effective_risk] > _RISK_ORDER[approval.approved_risk_ceiling]
        or len(current_fingerprints) != len(approval.approved_tool_intent_fingerprints)
        or any(
            not hmac.compare_digest(current, approved)
            for current, approved in zip(current_fingerprints, approval.approved_tool_intent_fingerprints)
        )
        or not hmac.compare_digest(
            current_bundle_fingerprint, approval.approved_intent_bundle_fingerprint
        )
        or not any(hmac.compare_digest(tool_intent.fingerprint, current) for current in current_fingerprints)
    ):
        return _decision(PolicyDecision.DENY, AgentOperationsErrorCode.APPROVAL_MISMATCH, effective_risk)
    return _decision(PolicyDecision.ALLOW_APPROVED_EXECUTION, None, effective_risk)


@dataclass(frozen=True, slots=True)
class IdempotencyRecord:
    domain: IdempotencyDomain
    principal_scope: str
    task_scope: str | None
    idempotency_key: str
    request_fingerprint: str

    def __post_init__(self):
        code = AgentOperationsErrorCode.IDEMPOTENCY_CONFLICT
        _exact_enum(self.domain, IdempotencyDomain, code)
        _identifier(self.principal_scope, "agent", code)
        if self.task_scope is not None:
            _identifier(self.task_scope, "task", code)
        _idempotency_key(self.idempotency_key, code)
        _digest(self.request_fingerprint, code)


def classify_replay(existing: IdempotencyRecord, candidate: IdempotencyRecord) -> ReplayClassification:
    if type(existing) is not IdempotencyRecord or type(candidate) is not IdempotencyRecord:
        _fail(AgentOperationsErrorCode.IDEMPOTENCY_CONFLICT)
    same_scope = (
        existing.domain is candidate.domain
        and existing.principal_scope == candidate.principal_scope
        and existing.task_scope == candidate.task_scope
        and existing.idempotency_key == candidate.idempotency_key
    )
    if not same_scope:
        return ReplayClassification.DISTINCT_REQUEST
    if hmac.compare_digest(existing.request_fingerprint, candidate.request_fingerprint):
        return ReplayClassification.EXACT_REPLAY
    return ReplayClassification.IDEMPOTENCY_CONFLICT


def execution_attempt_fingerprint(task: AgentTask, intent: AgentToolIntent, attempt_number: int) -> str:
    if type(task) is not AgentTask or type(intent) is not AgentToolIntent:
        _fail(AgentOperationsErrorCode.INTERNAL_CONSISTENCY_FAILURE)
    _bounded_int(attempt_number, minimum=1, maximum=32, code=AgentOperationsErrorCode.INTERNAL_CONSISTENCY_FAILURE)
    return _fingerprint(
        "execution-attempt",
        {
            "task_proposal_fingerprint": task_proposal_fingerprint(task),
            "task_revision": task.revision_number,
            "tool_intent_fingerprint": intent.fingerprint,
            "attempt_number": attempt_number,
        },
    )


@dataclass(frozen=True, slots=True, repr=False)
class AgentAuditEvent:
    event_id: str
    task_id: str
    task_revision: int
    agent_id: str
    event_kind: AuditEventKind
    occurred_at: datetime
    policy_version: str
    previous_event_fingerprint: str | None
    decision_summary: str
    capability: Capability | None
    policy_decision: PolicyDecision | None
    risk: RiskLevel
    evidence_references: tuple[EvidenceReference, ...]
    event_fingerprint: str = ""

    def __post_init__(self):
        code = AgentOperationsErrorCode.INVALID_AUDIT_CHAIN
        _identifier(self.event_id, "event", code)
        _identifier(self.task_id, "task", code)
        _bounded_int(self.task_revision, minimum=1, maximum=MAX_TASK_REVISION, code=code)
        _identifier(self.agent_id, "agent", code)
        _exact_enum(self.event_kind, AuditEventKind, code)
        object.__setattr__(self, "occurred_at", _canonical_time(self.occurred_at, code))
        if self.policy_version != A1_POLICY_VERSION:
            _fail(code)
        if self.previous_event_fingerprint is not None:
            _digest(self.previous_event_fingerprint, code)
        object.__setattr__(self, "decision_summary", _safe_text(self.decision_summary, maximum=MAX_SAFE_SUMMARY_LENGTH, code=code))
        if self.capability is not None:
            _exact_enum(self.capability, Capability, code)
        if self.policy_decision is not None:
            _exact_enum(self.policy_decision, PolicyDecision, code)
        _exact_enum(self.risk, RiskLevel, code)
        if type(self.evidence_references) is not tuple or len(self.evidence_references) > MAX_EVIDENCE_REFERENCES:
            _fail(code)
        if any(type(item) is not EvidenceReference for item in self.evidence_references):
            _fail(code)
        expected = _audit_fingerprint_payload(self)
        if self.event_fingerprint not in ("", expected):
            _fail(code)
        object.__setattr__(self, "event_fingerprint", expected)

    def __repr__(self) -> str:
        return (
            "AgentAuditEvent("
            f"event_id={self.event_id!r}, task_id={self.task_id!r}, revision={self.task_revision}, "
            f"agent_id={self.agent_id!r}, kind={self.event_kind.value!r}, "
            f"fingerprint={self.event_fingerprint!r}, decision_summary=<redacted>)"
        )

    __str__ = __repr__


def _audit_fingerprint_payload(event: AgentAuditEvent) -> str:
    payload = {
        field.name: getattr(event, field.name)
        for field in fields(event)
        if field.name not in {"event_id", "event_fingerprint"}
    }
    return _fingerprint("agent-audit-event", payload)


def audit_event_fingerprint(event: AgentAuditEvent) -> str:
    if type(event) is not AgentAuditEvent:
        _fail(AgentOperationsErrorCode.INVALID_AUDIT_CHAIN)
    return _audit_fingerprint_payload(event)


def validate_audit_chain(events: tuple[AgentAuditEvent, ...]) -> bool:
    code = AgentOperationsErrorCode.INVALID_AUDIT_CHAIN
    if type(events) is not tuple or not events or len(events) > 10_000:
        _fail(code)
    previous = ROOT_AUDIT_FINGERPRINT
    task_binding = None
    prior_time = None
    for event in events:
        if type(event) is not AgentAuditEvent:
            _fail(code)
        reconstructed = replace(event, event_fingerprint="")
        binding = (event.task_id, event.task_revision, event.agent_id, event.policy_version)
        if task_binding is None:
            task_binding = binding
        if binding != task_binding or (prior_time is not None and event.occurred_at < prior_time):
            _fail(code)
        if previous is None:
            if event.previous_event_fingerprint is not None:
                _fail(code)
        elif (
            event.previous_event_fingerprint is None
            or not hmac.compare_digest(event.previous_event_fingerprint, previous)
        ):
            _fail(code)
        recomputed = reconstructed.event_fingerprint
        if not hmac.compare_digest(event.event_fingerprint, recomputed):
            _fail(code)
        previous = event.event_fingerprint
        prior_time = event.occurred_at
    return True


@dataclass(frozen=True, slots=True, repr=False)
class HumanEscalationRequest:
    escalation_id: str
    task_id: str
    agent_id: str
    reason: EscalationReason
    risk: RiskLevel
    required_decision: RequiredDecisionType
    context_summary: str
    evidence_references: tuple[EvidenceReference, ...]
    suggested_options: tuple[str, ...]
    expires_at: datetime

    def __post_init__(self):
        code = AgentOperationsErrorCode.INVALID_TASK
        _identifier(self.escalation_id, "escalation", code)
        _identifier(self.task_id, "task", code)
        _identifier(self.agent_id, "agent", code)
        _exact_enum(self.reason, EscalationReason, code)
        _exact_enum(self.risk, RiskLevel, code)
        _exact_enum(self.required_decision, RequiredDecisionType, code)
        object.__setattr__(self, "context_summary", _safe_text(self.context_summary, maximum=MAX_SAFE_SUMMARY_LENGTH, code=code))
        if type(self.evidence_references) is not tuple or len(self.evidence_references) > MAX_EVIDENCE_REFERENCES:
            _fail(code)
        if any(type(item) is not EvidenceReference for item in self.evidence_references):
            _fail(code)
        if type(self.suggested_options) is not tuple or not self.suggested_options or len(self.suggested_options) > MAX_ESCALATION_OPTIONS:
            _fail(code)
        normalized = tuple(_safe_text(item, maximum=160, code=code) for item in self.suggested_options)
        if len(set(normalized)) != len(normalized):
            _fail(code)
        object.__setattr__(self, "suggested_options", normalized)
        object.__setattr__(self, "expires_at", _canonical_time(self.expires_at, code))

    def __repr__(self) -> str:
        return (
            "HumanEscalationRequest("
            f"escalation_id={self.escalation_id!r}, task_id={self.task_id!r}, "
            f"agent_id={self.agent_id!r}, reason={self.reason.value!r}, "
            f"risk={self.risk.value!r}, context=<redacted>, options={len(self.suggested_options)})"
        )

    __str__ = __repr__


@dataclass(frozen=True, slots=True)
class AgentSafetyPolicy:
    version: str
    read_only_categories: frozenset[ActionCategory]
    side_effect_categories: frozenset[ActionCategory]
    prohibited_capabilities: frozenset[Capability]
    restricted_data_allowed: bool
    stores_hidden_reasoning: bool
    runtime_active: bool


A1_SAFETY_POLICY = AgentSafetyPolicy(
    version=A1_POLICY_VERSION,
    read_only_categories=READ_ONLY_ACTION_CATEGORIES,
    side_effect_categories=SIDE_EFFECT_ACTION_CATEGORIES,
    prohibited_capabilities=A1_PROHIBITED_CAPABILITIES,
    restricted_data_allowed=False,
    stores_hidden_reasoning=False,
    runtime_active=False,
)


__all__ = (
    "A1_POLICY_VERSION",
    "A1_SAFETY_POLICY",
    "A1_PROHIBITED_CAPABILITIES",
    "ActionCategory",
    "AgentAuditEvent",
    "AgentBudgetState",
    "AgentDefinition",
    "AgentEnvironment",
    "AgentExecutionBudget",
    "AgentKind",
    "AgentLifecycle",
    "AgentOperation",
    "AgentOperationsError",
    "AgentOperationsErrorCode",
    "AgentPolicyDecision",
    "AgentSafetyPolicy",
    "AgentTask",
    "AgentToolIntent",
    "ApprovalRequirement",
    "ApprovalScope",
    "AuditEventKind",
    "CAPABILITY_TAXONOMY",
    "Capability",
    "CapabilitySpecification",
    "CompanyFunction",
    "DEFAULT_A1_EXECUTION_BUDGET",
    "DataClassification",
    "EscalationReason",
    "EscalationTarget",
    "EvidenceKind",
    "EvidenceReference",
    "EvidenceSourceSystem",
    "HumanEscalationRequest",
    "IdempotencyDomain",
    "IdempotencyRecord",
    "OPERATION_TAXONOMY",
    "OperationSpecification",
    "PolicyDecision",
    "READ_ONLY_ACTION_CATEGORIES",
    "ROOT_AUDIT_FINGERPRINT",
    "ReplayClassification",
    "RequiredDecisionType",
    "RiskLevel",
    "SIDE_EFFECT_ACTION_CATEGORIES",
    "SideEffectClass",
    "TASK_TRANSITIONS",
    "TERMINAL_TASK_LIFECYCLES",
    "TaskCreator",
    "TaskKind",
    "TaskLifecycle",
    "ToolKind",
    "TrustedHumanApproval",
    "approval_fingerprint",
    "approval_requirement_for",
    "audit_event_fingerprint",
    "canonical_json_bytes",
    "canonical_timestamp",
    "classify_replay",
    "evaluate_agent_action",
    "execution_attempt_fingerprint",
    "generate_agent_id",
    "generate_approval_id",
    "generate_audit_event_id",
    "generate_escalation_id",
    "generate_task_id",
    "generate_tool_intent_id",
    "ordered_intent_bundle_fingerprint",
    "parse_canonical_json",
    "task_proposal_fingerprint",
    "tool_intent_fingerprint",
    "transition_task",
    "validate_audit_chain",
)
