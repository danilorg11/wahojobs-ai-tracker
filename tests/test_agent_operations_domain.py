import copy
from dataclasses import FrozenInstanceError, asdict, fields, replace
from datetime import datetime, timedelta, timezone
import gc
import hashlib
import json
import math
import pickle
import unittest
import weakref

import wahojobs.agent_operations as ao


NOW = datetime(2026, 7, 22, 15, 0, 0, tzinfo=timezone.utc)
LATER = NOW + timedelta(hours=1)
AGENT_ID = "agt_" + "1" * 32
OTHER_AGENT_ID = "agt_" + "2" * 32
TASK_ID = "atk_" + "3" * 32
OTHER_TASK_ID = "atk_" + "4" * 32
INTENT_ID = "ati_" + "5" * 32
APPROVAL_ID = "aap_" + "6" * 32
EVENT_IDS = tuple("aev_" + value * 32 for value in ("7", "8", "9", "a"))
ESCALATION_ID = "aes_" + "b" * 32


KIND_FOR_FUNCTION = {
    ao.CompanyFunction.COMPANY_OPERATIONS: ao.AgentKind.COMPANY_OPERATIONS,
    ao.CompanyFunction.DATA_OPERATIONS: ao.AgentKind.DATA_OPERATIONS,
    ao.CompanyFunction.SEO_CONTENT: ao.AgentKind.SEO_CONTENT,
    ao.CompanyFunction.PRODUCT_OPERATIONS: ao.AgentKind.PRODUCT_OPERATIONS,
    ao.CompanyFunction.CUSTOMER_SUPPORT: ao.AgentKind.CUSTOMER_SUPPORT,
    ao.CompanyFunction.B2B_SALES: ao.AgentKind.B2B_SALES,
    ao.CompanyFunction.ENGINEERING_OPERATIONS: ao.AgentKind.ENGINEERING_OPERATIONS,
    ao.CompanyFunction.FINANCE_OPERATIONS: ao.AgentKind.FINANCE_OPERATIONS,
}


def broad_budget(**changes):
    values = {
        "maximum_tool_intents_per_task": 8,
        "maximum_execution_attempts": 3,
        "maximum_concurrent_tasks": 2,
        "maximum_cost_units": 500,
        "maximum_external_messages": 5,
        "maximum_modified_records": 20,
        "maximum_content_publications": 2,
        "maximum_runtime_seconds": 600,
    }
    values.update(changes)
    return ao.AgentExecutionBudget(**values)


def data_set(maximum=ao.DataClassification.INTERNAL):
    order = (
        ao.DataClassification.PUBLIC,
        ao.DataClassification.INTERNAL,
        ao.DataClassification.CONFIDENTIAL,
    )
    return frozenset(order[: order.index(maximum) + 1])


def classification_for(capability):
    maximum = ao.CAPABILITY_TAXONOMY[capability].maximum_data_classification
    if maximum in (ao.DataClassification.CONFIDENTIAL, ao.DataClassification.RESTRICTED):
        return ao.DataClassification.CONFIDENTIAL
    return maximum


def make_agent(
    capability=ao.Capability.EXECUTE_READ_ONLY_ANALYSIS,
    *,
    capabilities=None,
    lifecycle=ao.AgentLifecycle.ACTIVE,
    risk_ceiling=ao.RiskLevel.HIGH,
    maximum_classification=None,
    budget=None,
    agent_id=AGENT_ID,
    environment=ao.AgentEnvironment.TEST,
):
    specification = ao.CAPABILITY_TAXONOMY[capability]
    maximum_classification = maximum_classification or classification_for(capability)
    return ao.AgentDefinition(
        agent_id=agent_id,
        agent_kind=KIND_FOR_FUNCTION[specification.company_function],
        company_function=specification.company_function,
        environment=environment,
        lifecycle=lifecycle,
        policy_version=ao.A1_POLICY_VERSION,
        risk_ceiling=risk_ceiling,
        granted_capabilities=frozenset({capability}) if capabilities is None else capabilities,
        allowed_data_classifications=data_set(maximum_classification),
        execution_budget=budget or broad_budget(),
        escalation_target=ao.EscalationTarget.COMPANY_OPERATIONS_LEAD,
    )


def make_evidence(
    *,
    fingerprint="d" * 64,
    classification=ao.DataClassification.INTERNAL,
    captured_at=NOW,
    freshness_boundary=NOW + timedelta(hours=2),
    safe_locator="metrics:17",
):
    return ao.EvidenceReference(
        evidence_kind=ao.EvidenceKind.METRIC_SNAPSHOT,
        source_system=ao.EvidenceSourceSystem.INTERNAL_METRICS,
        source_classification=classification,
        captured_at=captured_at,
        content_fingerprint=fingerprint,
        safe_locator=safe_locator,
        freshness_boundary=freshness_boundary,
    )


def make_task(
    capability=ao.Capability.EXECUTE_READ_ONLY_ANALYSIS,
    *,
    lifecycle=ao.TaskLifecycle.APPROVED,
    risk=None,
    classification=None,
    task_id=TASK_ID,
    revision=1,
    expires_at=NOW + timedelta(hours=4),
    budget=None,
    objective="Review aggregate operational quality signals.",
    environment=ao.AgentEnvironment.TEST,
    evidence=None,
    created_at=NOW,
):
    specification = ao.CAPABILITY_TAXONOMY[capability]
    risk = risk or specification.minimum_risk
    classification = classification or classification_for(capability)
    proposed = ao.AgentTask(
        task_id=task_id,
        task_kind=ao.TaskKind.OPERATIONAL_ACTION,
        company_function=specification.company_function,
        environment=environment,
        requested_capability=capability,
        required_data_classification=classification,
        calculated_risk=risk,
        lifecycle=ao.TaskLifecycle.PROPOSED,
        created_at=created_at,
        expires_at=expires_at,
        created_by=ao.TaskCreator.HUMAN_OPERATOR,
        idempotency_key="task-request-0001",
        objective=objective,
        evidence_references=(make_evidence(),) if evidence is None else evidence,
        current_policy_version=ao.A1_POLICY_VERSION,
        approval_requirement=ao.approval_requirement_for(capability, risk),
        execution_budget=budget or broad_budget(),
        revision_number=revision,
    )
    if lifecycle is ao.TaskLifecycle.PROPOSED:
        return proposed
    if lifecycle in {ao.TaskLifecycle.CANCELLED, ao.TaskLifecycle.POLICY_DENIED}:
        return ao.transition_task(proposed, lifecycle, at=created_at)
    current = proposed
    if proposed.approval_requirement is ao.ApprovalRequirement.HUMAN:
        current = ao.transition_task(current, ao.TaskLifecycle.AWAITING_APPROVAL, at=created_at)
        if lifecycle is ao.TaskLifecycle.AWAITING_APPROVAL:
            return current
        provisional_intent = make_intent(
            capability,
            summary="Bounded approved operation facts.",
            environment=environment,
            proposed_at=created_at,
            evidence=current.evidence_references,
        )
        provisional_approval = issue_test_approval(
            current,
            (provisional_intent,),
            approved_at=created_at,
        )
        current = ao.transition_task(
            current,
            ao.TaskLifecycle.APPROVED,
            at=created_at,
            approval=provisional_approval,
        )
    else:
        current = ao.transition_task(current, ao.TaskLifecycle.APPROVED, at=created_at)
    if lifecycle is ao.TaskLifecycle.APPROVED:
        return current
    if lifecycle is ao.TaskLifecycle.EXPIRED:
        return ao.transition_task(current, ao.TaskLifecycle.EXPIRED, at=expires_at)
    current = ao.transition_task(current, ao.TaskLifecycle.RUNNING, at=created_at)
    if lifecycle is ao.TaskLifecycle.RUNNING:
        return current
    if lifecycle in {
        ao.TaskLifecycle.SUCCEEDED,
        ao.TaskLifecycle.FAILED,
        ao.TaskLifecycle.CANCELLED,
        ao.TaskLifecycle.NEEDS_HUMAN_INPUT,
    }:
        return ao.transition_task(current, lifecycle, at=created_at)
    raise AssertionError(f"unsupported test lifecycle: {lifecycle}")


def make_intent(
    capability=ao.Capability.EXECUTE_READ_ONLY_ANALYSIS,
    *,
    intent_id=INTENT_ID,
    summary="Aggregate matching quality signals for the current review.",
    risk=None,
    classification=None,
    reversible=None,
    rollback=None,
    cost=4,
    operation=None,
    environment=ao.AgentEnvironment.TEST,
    proposed_at=NOW,
    evidence=None,
    external_messages=None,
    modified_records=None,
    publications=None,
):
    specification = ao.CAPABILITY_TAXONOMY[capability]
    risk = risk or specification.minimum_risk
    classification = classification or classification_for(capability)
    operation = operation or ao.AgentOperation(capability.value)
    operation_specification = ao.OPERATION_TAXONOMY[operation]
    if reversible is None:
        reversible = operation_specification.reversible
    if rollback is None and operation_specification.rollback_plan_required:
        rollback = "Revert the bounded approved change."
    side_effect = operation_specification.expected_side_effect_class
    if external_messages is None:
        external_messages = 1 if side_effect is ao.SideEffectClass.EXTERNAL_MESSAGE else 0
    if modified_records is None:
        modified_records = 1 if side_effect in {
            ao.SideEffectClass.INTERNAL_STATE_CHANGE,
            ao.SideEffectClass.SOURCE_CONTROL_CHANGE,
            ao.SideEffectClass.BILLING_OR_PRICING,
            ao.SideEffectClass.USER_DATA_DELETION,
            ao.SideEffectClass.OWNERSHIP_MUTATION,
        } else 0
    if publications is None:
        publications = 1 if side_effect is ao.SideEffectClass.CONTENT_PUBLICATION else 0
    return ao.AgentToolIntent(
        intent_id=intent_id,
        capability=capability,
        tool_kind=next(iter(operation_specification.permitted_tool_kinds)),
        operation=operation,
        environment=environment,
        proposed_at=proposed_at,
        action_category=specification.action_category,
        target_classification=classification,
        risk=risk,
        idempotency_key="tool-intent-0001",
        sanitized_argument_summary=summary,
        expected_side_effect_class=side_effect,
        reversible=reversible,
        rollback_plan_summary=rollback,
        evidence_references=(make_evidence(),) if evidence is None else evidence,
        estimated_cost_units=cost,
        estimated_external_messages=external_messages,
        estimated_modified_records=modified_records,
        estimated_content_publications=publications,
        timeout_seconds=30,
    )


def issue_test_approval(
    task,
    intents,
    *,
    approved_at=NOW,
    expires_at=LATER,
    capabilities=None,
    risk_ceiling=None,
    approval_id=APPROVAL_ID,
):
    """Test-only trusted issuer. Production intentionally has no issuer."""

    return ao._issue_trusted_approval_for_tests(
        task,
        intents,
        approval_id=approval_id,
        approved_at=approved_at,
        expires_at=expires_at,
        approved_capabilities=capabilities or frozenset({task.requested_capability}),
        approved_risk_ceiling=risk_ceiling or task.calculated_risk,
    )


def budget_state(**changes):
    values = {"observed_at": NOW + timedelta(minutes=10)}
    values.update(changes)
    return ao.AgentBudgetState(**values)


def evaluate_action(
    agent,
    task,
    intent,
    approval=None,
    budget_state=None,
    *,
    observed_at=None,
    intent_bundle=None,
):
    if observed_at is None:
        observed_at = budget_state.observed_at if type(budget_state) is ao.AgentBudgetState else NOW + timedelta(minutes=10)
    return ao.evaluate_agent_action(
        agent,
        task,
        intent,
        approval,
        budget_state,
        observed_at=observed_at,
        intent_bundle=intent_bundle,
    )


class AgentDefinitionTests(unittest.TestCase):
    def test_valid_agent_is_bounded_immutable_and_redacted(self):
        agent = make_agent()
        self.assertEqual(agent.agent_kind, ao.AgentKind.COMPANY_OPERATIONS)
        self.assertFalse(hasattr(agent, "__dict__"))
        with self.assertRaises(FrozenInstanceError):
            agent.lifecycle = ao.AgentLifecycle.SUSPENDED
        rendered = repr(agent)
        self.assertIn("execution_budget=<bounded>", rendered)
        self.assertNotIn(agent.escalation_target.value, rendered)

    def test_agent_closed_domains_reject_strings_and_mismatches(self):
        base = make_agent()
        for field, value in (
            ("agent_kind", "company_operations"),
            ("company_function", "company_operations"),
            ("environment", "test"),
            ("lifecycle", "active"),
            ("risk_ceiling", "high"),
            ("escalation_target", "company_operations_lead"),
            ("agent_kind", ao.AgentKind.DATA_OPERATIONS),
        ):
            with self.subTest(field=field, value=value):
                with self.assertRaises(ao.AgentOperationsError) as raised:
                    replace(base, **{field: value})
                self.assertEqual(raised.exception.code, ao.AgentOperationsErrorCode.INVALID_AGENT_DEFINITION)

    def test_agent_rejects_unknown_or_cross_function_capabilities(self):
        base = make_agent()
        for capabilities in (
            {ao.Capability.EXECUTE_READ_ONLY_ANALYSIS},
            frozenset({"execute_read_only_analysis"}),
            frozenset({ao.Capability.READ_SEO_METRICS}),
        ):
            with self.subTest(capabilities=capabilities):
                with self.assertRaises(ao.AgentOperationsError):
                    replace(base, granted_capabilities=capabilities)

    def test_agent_rejects_restricted_and_noncontiguous_data_access(self):
        base = make_agent()
        for classifications in (
            frozenset({ao.DataClassification.PUBLIC, ao.DataClassification.RESTRICTED}),
            frozenset({ao.DataClassification.INTERNAL}),
            frozenset({"public"}),
            frozenset(),
        ):
            with self.subTest(classifications=classifications):
                with self.assertRaises(ao.AgentOperationsError):
                    replace(base, allowed_data_classifications=classifications)

    def test_generated_identifiers_are_strong_and_domain_separated(self):
        generated = (
            ao.generate_agent_id(),
            ao.generate_task_id(),
            ao.generate_tool_intent_id(),
            ao.generate_approval_id(),
            ao.generate_audit_event_id(),
            ao.generate_escalation_id(),
        )
        self.assertEqual(len(set(generated)), 6)
        for value, prefix in zip(generated, ("agt_", "atk_", "ati_", "aap_", "aev_", "aes_")):
            self.assertTrue(value.startswith(prefix))
            self.assertEqual(len(value), 36)


class CapabilityAndBudgetTests(unittest.TestCase):
    def test_capability_taxonomy_is_complete_authoritative_and_immutable(self):
        self.assertEqual(set(ao.CAPABILITY_TAXONOMY), set(ao.Capability))
        for capability, specification in ao.CAPABILITY_TAXONOMY.items():
            self.assertIs(capability, specification.capability)
            self.assertIs(type(specification.company_function), ao.CompanyFunction)
            self.assertIs(type(specification.action_category), ao.ActionCategory)
            self.assertIs(type(specification.minimum_risk), ao.RiskLevel)
            self.assertIs(type(specification.maximum_data_classification), ao.DataClassification)
            self.assertTrue(specification.documentation_safe_meaning.endswith("."))
        with self.assertRaises(TypeError):
            ao.CAPABILITY_TAXONOMY[ao.Capability.SPEND_MONEY] = None

    def test_no_wildcard_or_broad_admin_capability_exists(self):
        codes = {item.value for item in ao.Capability}
        self.assertFalse(codes & {"superuser", "all_access", "administrator", "*"})

    def test_operation_taxonomy_is_closed_complete_and_immutable(self):
        self.assertEqual(set(ao.OPERATION_TAXONOMY), set(ao.AgentOperation))
        for operation, specification in ao.OPERATION_TAXONOMY.items():
            with self.subTest(operation=operation):
                self.assertIs(operation, specification.operation)
                self.assertTrue(specification.permitted_tool_kinds)
                self.assertTrue(specification.permitted_capabilities)
                self.assertTrue(specification.allowed_environments)
                self.assertIs(type(specification.required_action_category), ao.ActionCategory)
                self.assertIs(type(specification.expected_side_effect_class), ao.SideEffectClass)
                self.assertTrue(specification.documentation_safe_meaning.endswith("."))
        with self.assertRaises(TypeError):
            ao.OPERATION_TAXONOMY[ao.AgentOperation.START_CRAWLER] = None

    def test_critical_capabilities_are_classified_and_prohibited(self):
        critical = {cap for cap, spec in ao.CAPABILITY_TAXONOMY.items() if spec.minimum_risk is ao.RiskLevel.CRITICAL}
        self.assertTrue(critical)
        self.assertTrue(critical <= ao.A1_PROHIBITED_CAPABILITIES)

    def test_budget_rejects_negative_boolean_float_nonfinite_and_overflow(self):
        base = broad_budget()
        bad_values = (-1, True, 1.0, math.inf, MAXIMUM := 1_000_000_001)
        for value in bad_values:
            with self.subTest(value=value):
                with self.assertRaises(ao.AgentOperationsError):
                    replace(base, maximum_cost_units=value)

    def test_budget_state_is_immutable_and_requires_canonical_time(self):
        state = budget_state()
        with self.assertRaises(FrozenInstanceError):
            state.cost_units_used = 1
        with self.assertRaises(ao.AgentOperationsError):
            replace(state, observed_at=NOW.replace(microsecond=1))
        with self.assertRaises(ao.AgentOperationsError):
            replace(state, concurrent_tasks=True)


class EvidenceTaskAndTransitionTests(unittest.TestCase):
    def test_evidence_is_privacy_safe_bounded_and_redacted(self):
        evidence = make_evidence()
        self.assertNotIn(evidence.safe_locator, repr(evidence))
        for changes in (
            {"source_classification": ao.DataClassification.RESTRICTED},
            {"safe_locator": "usr_" + "f" * 32},
            {"content_fingerprint": "F" * 64},
            {"freshness_boundary": NOW - timedelta(seconds=1)},
        ):
            with self.subTest(changes=changes):
                with self.assertRaises(ao.AgentOperationsError):
                    replace(evidence, **changes)

    def test_task_is_bounded_immutable_and_redacted(self):
        task = make_task()
        with self.assertRaises(FrozenInstanceError):
            task.revision_number = 2
        self.assertNotIn(task.objective, repr(task))
        self.assertNotIn(task.objective, str(task))
        self.assertEqual(task.approval_requirement, ao.ApprovalRequirement.NONE)

    def test_task_rejects_risk_below_floor_and_wrong_approval_requirement(self):
        high = make_task(ao.Capability.ARCHIVE_EXPIRED_JOBS, risk=ao.RiskLevel.HIGH)
        for changes in (
            {"calculated_risk": ao.RiskLevel.LOW},
            {"approval_requirement": ao.ApprovalRequirement.NONE},
        ):
            with self.subTest(changes=changes):
                with self.assertRaises(ao.AgentOperationsError):
                    replace(high, **changes)

    def test_task_bounds_objective_evidence_revision_and_expiry(self):
        task = make_task(lifecycle=ao.TaskLifecycle.PROPOSED)
        for changes in (
            {"objective": "x" * (ao.MAX_OBJECTIVE_LENGTH + 1)},
            {"evidence_references": (make_evidence(),) * (ao.MAX_EVIDENCE_REFERENCES + 1)},
            {"revision_number": ao.MAX_TASK_REVISION + 1},
            {"expires_at": NOW},
        ):
            with self.subTest(changes=changes):
                with self.assertRaises(ao.AgentOperationsError):
                    replace(task, **changes)

    def test_valid_low_risk_transitions(self):
        proposed = make_task(lifecycle=ao.TaskLifecycle.PROPOSED)
        approved = ao.transition_task(proposed, ao.TaskLifecycle.APPROVED, at=NOW)
        running = ao.transition_task(approved, ao.TaskLifecycle.RUNNING, at=NOW)
        for terminal in (
            ao.TaskLifecycle.SUCCEEDED,
            ao.TaskLifecycle.FAILED,
            ao.TaskLifecycle.CANCELLED,
            ao.TaskLifecycle.NEEDS_HUMAN_INPUT,
        ):
            with self.subTest(terminal=terminal):
                result = ao.transition_task(running, terminal, at=NOW)
                self.assertIs(result.lifecycle, terminal)

    def test_valid_approval_transition_requires_exact_trusted_receipt(self):
        proposed = make_task(ao.Capability.MODIFY_JOB_METADATA, lifecycle=ao.TaskLifecycle.PROPOSED)
        awaiting = ao.transition_task(proposed, ao.TaskLifecycle.AWAITING_APPROVAL, at=NOW)
        intent = make_intent(ao.Capability.MODIFY_JOB_METADATA, summary="Bounded normalized metadata fields for approved records.")
        approval = issue_test_approval(awaiting, (intent,))
        approved = ao.transition_task(awaiting, ao.TaskLifecycle.APPROVED, at=NOW, approval=approval)
        self.assertIs(approved.lifecycle, ao.TaskLifecycle.APPROVED)

    def test_expiry_and_terminal_states_are_one_way(self):
        task = make_task(lifecycle=ao.TaskLifecycle.APPROVED, expires_at=NOW + timedelta(minutes=5))
        expired = ao.transition_task(task, ao.TaskLifecycle.EXPIRED, at=NOW + timedelta(minutes=5))
        for target in ao.TaskLifecycle:
            with self.subTest(target=target):
                with self.assertRaises(ao.AgentOperationsError):
                    ao.transition_task(expired, target, at=NOW + timedelta(minutes=6))

    def test_invalid_transition_matrix_is_deterministic(self):
        low = make_task(lifecycle=ao.TaskLifecycle.PROPOSED)
        invalid_targets = set(ao.TaskLifecycle) - set(ao.TASK_TRANSITIONS[low.lifecycle])
        for target in invalid_targets:
            with self.subTest(target=target):
                with self.assertRaises(ao.AgentOperationsError) as first:
                    ao.transition_task(low, target, at=NOW)
                with self.assertRaises(ao.AgentOperationsError) as second:
                    ao.transition_task(low, target, at=NOW)
                self.assertEqual(first.exception.as_public_dict(), second.exception.as_public_dict())

    def test_policy_denied_task_cannot_be_approved(self):
        denied = make_task(lifecycle=ao.TaskLifecycle.POLICY_DENIED)
        with self.assertRaises(ao.AgentOperationsError):
            ao.transition_task(denied, ao.TaskLifecycle.APPROVED, at=NOW)

    def test_approval_is_bound_to_task_revision(self):
        task = make_task(ao.Capability.MODIFY_JOB_METADATA, lifecycle=ao.TaskLifecycle.AWAITING_APPROVAL)
        intent = make_intent(ao.Capability.MODIFY_JOB_METADATA, summary="Bounded normalized metadata fields for approved records.")
        approval = issue_test_approval(task, (intent,))
        revised = make_task(
            ao.Capability.MODIFY_JOB_METADATA,
            lifecycle=ao.TaskLifecycle.AWAITING_APPROVAL,
            revision=2,
        )
        with self.assertRaises(ao.AgentOperationsError) as raised:
            ao.transition_task(revised, ao.TaskLifecycle.APPROVED, at=NOW, approval=approval)
        self.assertEqual(raised.exception.code, ao.AgentOperationsErrorCode.APPROVAL_MISMATCH)


class TrustedApprovalTests(unittest.TestCase):
    def setUp(self):
        self.task = make_task(ao.Capability.MODIFY_JOB_METADATA)
        self.intent = make_intent(ao.Capability.MODIFY_JOB_METADATA, summary="Bounded normalized metadata fields for approved records.")
        self.approval = issue_test_approval(self.task, (self.intent,))

    def test_direct_and_dictionary_construction_are_rejected(self):
        with self.assertRaises(TypeError):
            ao.TrustedHumanApproval()
        self.assertFalse(ao._is_trusted_approval(dict(self.approval._payload)))

    def test_replace_copy_deepcopy_and_pickle_are_rejected(self):
        operations = (
            lambda: replace(self.approval, task_revision=2),
            lambda: copy.copy(self.approval),
            lambda: copy.deepcopy(self.approval),
            lambda: pickle.dumps(self.approval),
        )
        for operation in operations:
            with self.subTest(operation=operation):
                with self.assertRaises(TypeError):
                    operation()

    def test_subclass_and_duck_type_are_rejected(self):
        with self.assertRaises(TypeError):
            class ForgedApproval(ao.TrustedHumanApproval):
                pass

        class Duck:
            task_id = TASK_ID
            task_revision = 1

        self.assertFalse(ao._is_trusted_approval(Duck()))

    def test_unregistered_object_new_forgery_is_rejected(self):
        forged = object.__new__(ao.TrustedHumanApproval)
        object.__setattr__(forged, "_payload", self.approval._payload)
        self.assertFalse(ao._is_trusted_approval(forged))
        decision = evaluate_action(make_agent(ao.Capability.MODIFY_JOB_METADATA), self.task, self.intent, forged, budget_state())
        self.assertIs(decision.decision, ao.PolicyDecision.DENY)
        self.assertIs(decision.reason_code, ao.AgentOperationsErrorCode.INVALID_APPROVAL)

    def assert_unissued_approval_is_denied(self, candidate):
        self.assertFalse(ao._is_trusted_approval(candidate))
        decision = evaluate_action(
            make_agent(ao.Capability.MODIFY_JOB_METADATA),
            self.task,
            self.intent,
            candidate,
            budget_state(),
        )
        self.assertIs(decision.decision, ao.PolicyDecision.DENY)
        self.assertIs(decision.reason_code, ao.AgentOperationsErrorCode.INVALID_APPROVAL)
        self.assertNotIn(
            decision.decision,
            {ao.PolicyDecision.ALLOW_READ_ONLY, ao.PolicyDecision.ALLOW_APPROVED_EXECUTION},
        )

    def test_exact_instance_binding_rejects_copied_approval_state(self):
        stored_fields = tuple(
            name for name in ao.TrustedHumanApproval.__slots__ if name != "__weakref__"
        )

        ordinary = object.__new__(ao.TrustedHumanApproval)
        object.__setattr__(ordinary, "_payload", self.approval._payload)

        stamped = object.__new__(ao.TrustedHumanApproval)
        for name in ("_payload", "_issuance_stamp", "_seal"):
            object.__setattr__(stamped, name, getattr(self.approval, name))

        copied_binding = object.__new__(ao.TrustedHumanApproval)
        for name in stored_fields:
            object.__setattr__(copied_binding, name, getattr(self.approval, name))

        new_binding = object.__new__(ao.TrustedHumanApproval)
        for name in stored_fields:
            if name != "_self_reference":
                object.__setattr__(new_binding, name, getattr(self.approval, name))
        object.__setattr__(new_binding, "_self_reference", weakref.ref(new_binding))

        copied_nonce_and_payload = object.__new__(ao.TrustedHumanApproval)
        for name in stored_fields:
            object.__setattr__(copied_nonce_and_payload, name, getattr(self.approval, name))

        valid_shaped = object.__new__(ao.TrustedHumanApproval)
        for name in stored_fields:
            if name != "_self_reference":
                object.__setattr__(valid_shaped, name, getattr(self.approval, name))
        object.__setattr__(valid_shaped, "_self_reference", weakref.ref(valid_shaped))

        cases = {
            "ordinary_fields": ordinary,
            "issuance_stamp_and_payload_seal": stamped,
            "original_self_binding": copied_binding,
            "new_self_binding_with_original_attestation": new_binding,
            "nonce_task_intents_and_timestamps": copied_nonce_and_payload,
            "manually_allocated_valid_shape": valid_shaped,
        }
        for name, candidate in cases.items():
            with self.subTest(name=name):
                self.assert_unissued_approval_is_denied(candidate)

    def test_issued_instance_validity_is_lifetime_stable_and_deterministic(self):
        first_fingerprint = ao.approval_fingerprint(self.approval)
        for _ in range(4):
            self.assertTrue(ao._is_trusted_approval(self.approval))
            self.assertEqual(ao.approval_fingerprint(self.approval), first_fingerprint)
            decision = evaluate_action(
                make_agent(ao.Capability.MODIFY_JOB_METADATA),
                self.task,
                self.intent,
                self.approval,
                budget_state(),
            )
            self.assertIs(decision.decision, ao.PolicyDecision.ALLOW_APPROVED_EXECUTION)

        other = issue_test_approval(
            self.task,
            (self.intent,),
            approval_id="aap_" + "f" * 32,
        )
        self.assertTrue(ao._is_trusted_approval(other))
        del other
        gc.collect()
        ao._UNRELATED_APPROVAL_TEST_STATE = object()
        del ao._UNRELATED_APPROVAL_TEST_STATE
        self.assertTrue(ao._is_trusted_approval(self.approval))
        self.assertEqual(ao.approval_fingerprint(self.approval), first_fingerprint)

        expired = issue_test_approval(
            self.task,
            (self.intent,),
            expires_at=NOW + timedelta(minutes=1),
        )
        observed = NOW + timedelta(minutes=2)
        decision = evaluate_action(
            make_agent(ao.Capability.MODIFY_JOB_METADATA),
            self.task,
            self.intent,
            expired,
            budget_state(observed_at=observed),
            observed_at=observed,
        )
        self.assertIs(decision.decision, ao.PolicyDecision.DENY)
        self.assertIs(decision.reason_code, ao.AgentOperationsErrorCode.APPROVAL_EXPIRED)

    def test_instance_attestation_is_private_and_test_issuer_is_not_public(self):
        rendered = repr(self.approval) + str(self.approval)
        for private_value in (
            self.approval._seal,
            self.approval._issuance_nonce.hex(),
            self.approval._instance_attestation,
        ):
            self.assertNotIn(private_value, rendered)
        with self.assertRaises(ao.AgentOperationsError) as raised:
            ao.canonical_json_bytes(self.approval)
        public_error = repr(raised.exception) + str(raised.exception) + repr(raised.exception.args)
        for private_value in (
            self.approval._seal,
            self.approval._issuance_nonce.hex(),
            self.approval._instance_attestation,
        ):
            self.assertNotIn(private_value, public_error)
        self.assertFalse(
            set(ao.TrustedHumanApproval.__slots__)
            & {"issuer_capability", "signing_key", "signing_secret"}
        )
        self.assertNotIn("_issue_trusted_approval_for_tests", ao.__all__)

    def test_approval_has_no_public_serialization_or_private_identity(self):
        self.assertFalse(hasattr(self.approval, "__dict__"))
        self.assertEqual(repr(self.approval), "TrustedHumanApproval(<redacted>)")
        with self.assertRaises(TypeError):
            json.dumps(self.approval)
        with self.assertRaises(ao.AgentOperationsError):
            ao.canonical_json_bytes(self.approval)
        rendered = repr(self.approval) + str(self.approval)
        self.assertNotIn(APPROVAL_ID, rendered)
        self.assertFalse(hasattr(self.approval, "email"))
        self.assertFalse(hasattr(self.approval, "session_token"))

    def test_approval_fingerprint_ignores_generated_approval_id(self):
        first = ao.approval_fingerprint(self.approval)
        second = issue_test_approval(
            self.task,
            (self.intent,),
            approval_id="aap_" + "e" * 32,
        )
        self.assertEqual(first, ao.approval_fingerprint(second))

    def test_approval_payload_mutation_invalidates_intrinsic_seal(self):
        payload = dict(self.approval._payload)
        payload["task_revision"] = 2
        object.__setattr__(self.approval, "_payload", ao._validate_approval_payload(payload))
        self.assertFalse(ao._is_trusted_approval(self.approval))


class ToolIntentTests(unittest.TestCase):
    def test_fingerprint_is_canonical_and_ignores_generated_intent_id(self):
        first = make_intent()
        second = replace(first, intent_id="ati_" + "f" * 32)
        self.assertEqual(first.fingerprint, second.fingerprint)
        self.assertEqual(first.argument_fingerprint, second.argument_fingerprint)

    def test_changed_argument_summary_changes_both_fingerprints(self):
        first = make_intent()
        second = replace(first, sanitized_argument_summary="Aggregate source quality signals for the current review.", argument_fingerprint="")
        self.assertNotEqual(first.argument_fingerprint, second.argument_fingerprint)
        self.assertNotEqual(first.fingerprint, second.fingerprint)

    def test_execution_relevant_changes_change_intent_fingerprint(self):
        first = make_intent()
        for changes in (
            {"timeout_seconds": 31},
            {"estimated_cost_units": 5},
            {"idempotency_key": "tool-intent-0002"},
        ):
            with self.subTest(changes=changes):
                self.assertNotEqual(first.fingerprint, replace(first, **changes).fingerprint)

    def test_raw_private_confidential_secret_path_sql_and_reasoning_are_rejected(self):
        markers = (
            "Reply to person@example.com",
            "Use API key sk-secret123456",
            "Read C:\\private\\workspace.sqlite",
            "SELECT private_value FROM accounts",
            "Store chain of thought for review",
        )
        for marker in markers:
            with self.subTest(marker=marker):
                with self.assertRaises(ao.AgentOperationsError) as raised:
                    make_intent(summary=marker)
                self.assertNotIn(marker, str(raised.exception))
                self.assertNotIn(marker, repr(raised.exception))

    def test_read_only_intent_cannot_smuggle_side_effects(self):
        for summary in ("Send the aggregate report.", "Delete old records.", "Publish the result."):
            with self.subTest(summary=summary):
                with self.assertRaises(ao.AgentOperationsError):
                    make_intent(summary=summary)

    def test_high_risk_intent_requires_reversibility_and_rollback(self):
        valid = make_intent(
            ao.Capability.MERGE_PULL_REQUEST,
            summary="Approved source change reference and bounded target branch.",
            risk=ao.RiskLevel.HIGH,
        )
        for changes in (
            {"reversible": False},
            {"rollback_plan_summary": None},
        ):
            with self.subTest(changes=changes):
                with self.assertRaises(ao.AgentOperationsError):
                    replace(valid, **changes)

    def test_side_effect_class_and_tool_kind_must_match_capability(self):
        intent = make_intent()
        for changes in (
            {"expected_side_effect_class": ao.SideEffectClass.INTERNAL_REVERSIBLE},
            {"tool_kind": ao.ToolKind.FINANCE},
            {"action_category": ao.ActionCategory.MODIFY_INTERNAL},
        ):
            with self.subTest(changes=changes):
                with self.assertRaises(ao.AgentOperationsError):
                    replace(intent, **changes)


class PolicyDecisionTests(unittest.TestCase):
    def test_low_read_only_action_is_allowed(self):
        decision = evaluate_action(make_agent(), make_task(), make_intent(), budget_state=budget_state())
        self.assertIs(decision.decision, ao.PolicyDecision.ALLOW_READ_ONLY)
        self.assertIs(decision.effective_risk, ao.RiskLevel.LOW)

    def test_medium_action_requires_approval_then_allows_exact_execution(self):
        capability = ao.Capability.MODIFY_JOB_METADATA
        agent = make_agent(capability)
        task = make_task(capability)
        intent = make_intent(capability, summary="Bounded normalized metadata fields for approved records.")
        required = evaluate_action(agent, task, intent, budget_state=budget_state())
        self.assertIs(required.decision, ao.PolicyDecision.REQUIRE_HUMAN_APPROVAL)
        approval = issue_test_approval(task, (intent,))
        allowed = evaluate_action(agent, task, intent, approval, budget_state())
        self.assertIs(allowed.decision, ao.PolicyDecision.ALLOW_APPROVED_EXECUTION)

    def test_high_action_requires_exact_approval_and_rollback(self):
        capability = ao.Capability.MERGE_PULL_REQUEST
        agent = make_agent(capability)
        task = make_task(capability)
        intent = make_intent(capability, summary="Approved source change reference and bounded target branch.")
        approval = issue_test_approval(task, (intent,))
        decision = evaluate_action(agent, task, intent, approval, budget_state())
        self.assertIs(decision.decision, ao.PolicyDecision.ALLOW_APPROVED_EXECUTION)

    def test_mismatched_changed_and_expired_approvals_are_denied(self):
        capability = ao.Capability.MODIFY_JOB_METADATA
        agent = make_agent(capability)
        task = make_task(capability)
        intent = make_intent(capability, summary="Bounded normalized metadata fields for approved records.")
        approval = issue_test_approval(task, (intent,))
        changed = replace(intent, sanitized_argument_summary="Bounded verified metadata fields for approved records.", argument_fingerprint="")
        cases = (
            (make_task(capability, revision=2), intent, approval, budget_state(), ao.AgentOperationsErrorCode.APPROVAL_MISMATCH),
            (task, changed, approval, budget_state(), ao.AgentOperationsErrorCode.APPROVAL_MISMATCH),
            (task, intent, issue_test_approval(task, (intent,), expires_at=NOW + timedelta(minutes=1)), budget_state(observed_at=NOW + timedelta(minutes=2)), ao.AgentOperationsErrorCode.APPROVAL_EXPIRED),
        )
        for candidate_task, candidate_intent, candidate_approval, state, reason in cases:
            with self.subTest(reason=reason):
                decision = evaluate_action(agent, candidate_task, candidate_intent, candidate_approval, state)
                self.assertIs(decision.decision, ao.PolicyDecision.DENY)
                self.assertIs(decision.reason_code, reason)

    def test_missing_capability_and_cross_function_are_denied(self):
        task = make_task()
        intent = make_intent()
        agent = make_agent(capabilities=frozenset())
        decision = evaluate_action(agent, task, intent, budget_state=budget_state())
        self.assertIs(decision.reason_code, ao.AgentOperationsErrorCode.CAPABILITY_DENIED)

    def test_excessive_classification_is_denied(self):
        capability = ao.Capability.READ_SUPPORT_QUEUE_SUMMARY
        agent = make_agent(capability, maximum_classification=ao.DataClassification.INTERNAL)
        task = make_task(capability, classification=ao.DataClassification.CONFIDENTIAL)
        intent = make_intent(capability, classification=ao.DataClassification.CONFIDENTIAL, summary="Aggregate support queue categories for current review.")
        decision = evaluate_action(agent, task, intent, budget_state=budget_state())
        self.assertIs(decision.reason_code, ao.AgentOperationsErrorCode.DATA_CLASSIFICATION_DENIED)

    def test_risk_ceiling_is_enforced(self):
        capability = ao.Capability.MODIFY_JOB_METADATA
        with self.assertRaises(ao.AgentOperationsError) as raised:
            make_agent(capability, risk_ceiling=ao.RiskLevel.LOW)
        self.assertIs(raised.exception.code, ao.AgentOperationsErrorCode.INVALID_AGENT_DEFINITION)

    def test_critical_action_remains_prohibited_even_with_approval(self):
        capability = ao.Capability.SPEND_MONEY
        agent = make_agent(capability, risk_ceiling=ao.RiskLevel.CRITICAL)
        task = make_task(capability, classification=ao.DataClassification.CONFIDENTIAL)
        intent = make_intent(capability, classification=ao.DataClassification.CONFIDENTIAL, summary="Bounded company funds action for human awareness.", reversible=False)
        approval = issue_test_approval(task, (intent,), risk_ceiling=ao.RiskLevel.CRITICAL)
        decision = evaluate_action(agent, task, intent, approval, budget_state())
        self.assertIs(decision.decision, ao.PolicyDecision.PROHIBIT)
        self.assertIs(decision.reason_code, ao.AgentOperationsErrorCode.ACTION_PROHIBITED)

    def test_inactive_agents_never_receive_executable_decision(self):
        for lifecycle in (ao.AgentLifecycle.SUSPENDED, ao.AgentLifecycle.RETIRED):
            with self.subTest(lifecycle=lifecycle):
                decision = evaluate_action(make_agent(lifecycle=lifecycle), make_task(), make_intent(), budget_state=budget_state())
                self.assertIs(decision.decision, ao.PolicyDecision.DENY)

    def test_exhausted_budget_dimensions_are_denied(self):
        agent, task, intent = make_agent(), make_task(), make_intent()
        states = (
            budget_state(tool_intents_used=task.execution_budget.maximum_tool_intents_per_task),
            budget_state(execution_attempts_used=task.execution_budget.maximum_execution_attempts),
            budget_state(concurrent_tasks=task.execution_budget.maximum_concurrent_tasks),
            budget_state(cost_units_used=task.execution_budget.maximum_cost_units),
            budget_state(runtime_seconds_used=task.execution_budget.maximum_runtime_seconds),
        )
        for state in states:
            with self.subTest(state=state):
                decision = evaluate_action(agent, task, intent, budget_state=state)
                self.assertIs(decision.reason_code, ao.AgentOperationsErrorCode.BUDGET_EXHAUSTED)

    def test_expired_and_terminal_tasks_are_denied(self):
        agent, intent = make_agent(), make_intent()
        expired = make_task(expires_at=NOW + timedelta(minutes=1))
        state = budget_state(observed_at=NOW + timedelta(minutes=2))
        decision = evaluate_action(agent, expired, intent, budget_state=state)
        self.assertIs(decision.decision, ao.PolicyDecision.DENY)
        terminal = make_task(lifecycle=ao.TaskLifecycle.SUCCEEDED)
        self.assertIs(
            evaluate_action(agent, terminal, intent, budget_state=budget_state()).decision,
            ao.PolicyDecision.DENY,
        )

    def test_initial_policy_prohibits_deployment_and_external_messages(self):
        self.assertIn(ao.Capability.DEPLOY_PRODUCTION, ao.A1_PROHIBITED_CAPABILITIES)
        self.assertIn(ao.Capability.SEND_SUPPORT_MESSAGE, ao.A1_PROHIBITED_CAPABILITIES)
        self.assertIn(ao.Capability.SEND_B2B_OUTREACH, ao.A1_PROHIBITED_CAPABILITIES)
        self.assertFalse(ao.A1_SAFETY_POLICY.runtime_active)
        self.assertFalse(ao.A1_SAFETY_POLICY.restricted_data_allowed)

    def test_task_budget_cannot_expand_agent_budget(self):
        agent = make_agent(budget=broad_budget(maximum_cost_units=10))
        task = make_task(budget=broad_budget(maximum_cost_units=11))
        decision = evaluate_action(agent, task, make_intent(), budget_state=budget_state())
        self.assertIs(decision.reason_code, ao.AgentOperationsErrorCode.BUDGET_EXHAUSTED)


class IndependentAuditBlockerRegressionTests(unittest.TestCase):
    def test_f1_closed_operation_rejects_crawler_start_as_read_only(self):
        with self.assertRaises(ao.AgentOperationsError) as raised:
            make_intent(
                ao.Capability.READ_SOURCE_HEALTH,
                operation=ao.AgentOperation.START_CRAWLER,
                risk=ao.RiskLevel.HIGH,
                rollback="Restore the prior crawler state.",
            )
        self.assertIs(raised.exception.code, ao.AgentOperationsErrorCode.INVALID_TOOL_INTENT)

    def test_f2_confidential_evidence_denies_internal_agent(self):
        evidence = make_evidence(classification=ao.DataClassification.CONFIDENTIAL)
        task = make_task(evidence=(evidence,))
        intent = make_intent(evidence=(evidence,))
        decision = evaluate_action(make_agent(), task, intent, budget_state=budget_state())
        self.assertIs(decision.decision, ao.PolicyDecision.DENY)
        self.assertIs(decision.reason_code, ao.AgentOperationsErrorCode.DATA_CLASSIFICATION_DENIED)

    def test_f3_side_effect_estimates_are_coherent_and_external_execution_is_prohibited(self):
        with self.assertRaises(ao.AgentOperationsError):
            make_intent(
                ao.Capability.MODIFY_JOB_METADATA,
                summary="Bounded normalized metadata fields for approved records.",
                external_messages=1,
            )
        capability = ao.Capability.SEND_SUPPORT_MESSAGE
        agent = make_agent(capability, maximum_classification=ao.DataClassification.CONFIDENTIAL)
        task = make_task(capability, classification=ao.DataClassification.CONFIDENTIAL)
        intent = make_intent(
            capability,
            classification=ao.DataClassification.CONFIDENTIAL,
            summary="Bounded approved external message operation.",
        )
        approval = issue_test_approval(task, (intent,))
        decision = evaluate_action(agent, task, intent, approval, budget_state())
        self.assertIs(decision.decision, ao.PolicyDecision.PROHIBIT)

    def test_side_effect_smuggling_matrix_fails_closed(self):
        read_only = make_intent()
        draft = make_intent(ao.Capability.CREATE_DRAFT_CONTENT)
        proposal = make_intent(ao.Capability.PROPOSE_DATA_QUALITY_TASK)
        test_intent = make_intent(ao.Capability.EXECUTE_TEST_SUITE)
        high = make_intent(
            ao.Capability.ARCHIVE_EXPIRED_JOBS,
            summary="Bounded approved archival scope.",
        )
        cases = (
            (read_only, {"estimated_modified_records": 1}),
            (draft, {"estimated_content_publications": 1}),
            (proposal, {"estimated_modified_records": 1}),
            (test_intent, {"expected_side_effect_class": ao.SideEffectClass.DEPLOYMENT}),
            (high, {"reversible": False}),
            (high, {"rollback_plan_summary": ""}),
        )
        for intent, changes in cases:
            with self.subTest(changes=changes):
                with self.assertRaises(ao.AgentOperationsError):
                    replace(intent, **changes)

    def test_f4_observation_time_is_mandatory_and_controls_expiry(self):
        with self.assertRaises(TypeError):
            ao.evaluate_agent_action(make_agent(), make_task(), make_intent())
        expired_task = make_task(expires_at=NOW + timedelta(minutes=1))
        observed = NOW + timedelta(minutes=2)
        decision = evaluate_action(
            make_agent(),
            expired_task,
            make_intent(),
            budget_state=budget_state(observed_at=observed),
            observed_at=observed,
        )
        self.assertIs(decision.reason_code, ao.AgentOperationsErrorCode.INVALID_TASK)
        capability = ao.Capability.MODIFY_JOB_METADATA
        task = make_task(capability)
        intent = make_intent(capability, summary="Bounded normalized metadata fields for approved records.")
        approval = issue_test_approval(task, (intent,), expires_at=NOW + timedelta(minutes=1))
        decision = evaluate_action(
            make_agent(capability),
            task,
            intent,
            approval,
            budget_state(observed_at=observed),
            observed_at=observed,
        )
        self.assertIs(decision.reason_code, ao.AgentOperationsErrorCode.APPROVAL_EXPIRED)

    def test_f5_approval_is_registry_free_and_gc_independent(self):
        self.assertFalse(hasattr(ao, "_TRUSTED_APPROVALS"))
        self.assertFalse(hasattr(ao, "_TRUSTED_APPROVAL_SEALS"))
        task = make_task(ao.Capability.MODIFY_JOB_METADATA)
        intent = make_intent(
            ao.Capability.MODIFY_JOB_METADATA,
            summary="Bounded normalized metadata fields for approved records.",
        )
        approval = issue_test_approval(task, (intent,))
        gc.collect()
        self.assertTrue(ao._is_trusted_approval(approval))

    def test_f6_secret_shaped_locator_is_rejected_without_retention(self):
        marker = "sk-privateauditmarker123456"
        with self.assertRaises(ao.AgentOperationsError) as raised:
            make_evidence(safe_locator=marker)
        error = raised.exception
        rendered = repr(error) + str(error) + repr(error.args) + repr(vars(error))
        self.assertNotIn(marker, rendered)
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)

    def test_f7_forbidden_aliases_are_rejected_recursively(self):
        aliases = (
            "chain_of_thought",
            "chainOfThought",
            "chain-of-thought",
            "reasoningTrace",
            "internal_monologue",
            "private-reasoning",
            "raw_evidence",
            "evidencePayload",
            "source payload",
            "rawContent",
        )
        for alias in aliases:
            with self.subTest(alias=alias):
                with self.assertRaises(ao.AgentOperationsError):
                    ao.canonical_json_bytes({"outer": {alias: "bounded marker"}})

    def test_f8_malformed_private_input_has_no_exception_context(self):
        marker = "PRIVATE_CONTEXT_MARKER_7f43b"
        malformed = '{"domain":"wahojobs.agent_operations","payload":"' + marker + '","version":1'
        with self.assertRaises(ao.AgentOperationsError) as raised:
            ao.parse_canonical_json(malformed)
        error = raised.exception
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)
        self.assertNotIn(marker, repr(error) + str(error) + repr(error.args) + repr(vars(error)))
        failures = (
            lambda: ao.parse_canonical_json(
                '{"domain":"wahojobs.agent_operations","payload":{"safe":1,"safe":2},"version":1}'
            ),
            lambda: ao.canonical_json_bytes({"rawEvidence": marker}),
            lambda: replace(make_agent(), environment=marker),
            lambda: replace(make_evidence(), captured_at=marker),
        )
        for failure in failures:
            with self.subTest(failure=failure):
                with self.assertRaises(ao.AgentOperationsError) as matrix_error:
                    failure()
                error = matrix_error.exception
                self.assertIsNone(error.__cause__)
                self.assertIsNone(error.__context__)
                self.assertNotIn(marker, repr(error) + str(error) + repr(error.args) + repr(vars(error)))

    def test_f9_environment_coherence_and_production_matrix(self):
        task = make_task(environment=ao.AgentEnvironment.PRODUCTION)
        decision = evaluate_action(make_agent(), task, make_intent(), budget_state=budget_state())
        self.assertIs(decision.decision, ao.PolicyDecision.DENY)
        with self.assertRaises(ao.AgentOperationsError):
            make_intent(
                ao.Capability.MODIFY_JOB_METADATA,
                environment=ao.AgentEnvironment.PRODUCTION,
                summary="Bounded normalized metadata fields for approved records.",
            )
        production_agent = make_agent(environment=ao.AgentEnvironment.PRODUCTION)
        production_task = make_task(environment=ao.AgentEnvironment.PRODUCTION)
        production_intent = make_intent(environment=ao.AgentEnvironment.PRODUCTION)
        allowed = evaluate_action(
            production_agent,
            production_task,
            production_intent,
            budget_state=budget_state(),
        )
        self.assertIs(allowed.decision, ao.PolicyDecision.ALLOW_READ_ONLY)

    def test_f10_noninitial_task_states_require_transition_authority(self):
        proposed = make_task(lifecycle=ao.TaskLifecycle.PROPOSED)
        for lifecycle in (ao.TaskLifecycle.APPROVED, ao.TaskLifecycle.RUNNING):
            with self.subTest(lifecycle=lifecycle):
                with self.assertRaises(ao.AgentOperationsError):
                    replace(proposed, lifecycle=lifecycle)

    def test_f10_transition_construction_is_ephemeral_and_private(self):
        proposed = make_task(lifecycle=ao.TaskLifecycle.PROPOSED)
        self.assertIs(proposed.lifecycle, ao.TaskLifecycle.PROPOSED)
        public_values = {
            item.name: getattr(proposed, item.name)
            for item in fields(proposed)
            if not item.name.startswith("_")
        }
        for lifecycle in set(ao.TaskLifecycle) - {ao.TaskLifecycle.PROPOSED}:
            with self.subTest(lifecycle=lifecycle):
                with self.assertRaises(ao.AgentOperationsError) as raised:
                    ao.AgentTask(**{**public_values, "lifecycle": lifecycle})
                self.assertIs(raised.exception.code, ao.AgentOperationsErrorCode.INVALID_TASK)
        with self.assertRaises(ao.AgentOperationsError):
            ao.AgentTask(
                **{**public_values, "lifecycle": ao.TaskLifecycle.APPROVED},
                _transition_authority=object(),
            )
        approved = ao.transition_task(proposed, ao.TaskLifecycle.APPROVED, at=NOW)
        self.assertIs(approved.lifecycle, ao.TaskLifecycle.APPROVED)
        self.assertFalse(hasattr(ao, "_TASK_TRANSITION_AUTHORITY"))
        self.assertNotIn("_transition_authority", ao.AgentTask.__slots__)
        self.assertNotIn("_transition_authority", {item.name for item in fields(approved)})
        rendered = repr(approved) + str(approved) + ao.canonical_json_bytes(approved).decode("utf-8")
        self.assertNotIn("transition_authority", rendered)
        self.assertNotIn("transition_lineage", rendered)

    def test_f10_transitioned_values_cannot_propagate_construction_authority(self):
        proposed = make_task(lifecycle=ao.TaskLifecycle.PROPOSED)
        approved = ao.transition_task(proposed, ao.TaskLifecycle.APPROVED, at=NOW)
        original_state = (approved.lifecycle, approved.objective, ao.task_proposal_fingerprint(approved))
        for changes in (
            {},
            {"lifecycle": ao.TaskLifecycle.SUCCEEDED},
            {"lifecycle": ao.TaskLifecycle.PROPOSED},
            {"objective": "Review a different bounded operational scope."},
        ):
            with self.subTest(changes=changes):
                with self.assertRaises(ao.AgentOperationsError):
                    replace(approved, **changes)
                self.assertEqual(
                    (approved.lifecycle, approved.objective, ao.task_proposal_fingerprint(approved)),
                    original_state,
                )

        human_proposed = make_task(
            ao.Capability.MODIFY_JOB_METADATA,
            lifecycle=ao.TaskLifecycle.PROPOSED,
        )
        awaiting = ao.transition_task(
            human_proposed,
            ao.TaskLifecycle.AWAITING_APPROVAL,
            at=NOW,
        )
        with self.assertRaises(ao.AgentOperationsError):
            replace(awaiting, lifecycle=ao.TaskLifecycle.APPROVED)

        reconstructed = {
            item.name: getattr(approved, item.name)
            for item in fields(approved)
            if not item.name.startswith("_")
        }
        with self.assertRaises(ao.AgentOperationsError):
            ao.AgentTask(**reconstructed)

        clones = (copy.copy(approved), copy.deepcopy(approved), pickle.loads(pickle.dumps(approved)))
        for clone in clones:
            with self.subTest(clone_type=type(clone)):
                self.assertIs(clone.lifecycle, ao.TaskLifecycle.APPROVED)
                with self.assertRaises(ao.AgentOperationsError):
                    replace(clone, lifecycle=ao.TaskLifecycle.RUNNING)

        changed_copy = copy.copy(approved)
        object.__setattr__(changed_copy, "lifecycle", ao.TaskLifecycle.RUNNING)
        with self.assertRaises(ao.AgentOperationsError):
            ao.transition_task(changed_copy, ao.TaskLifecycle.SUCCEEDED, at=NOW)
        changed_decision = evaluate_action(
            make_agent(),
            changed_copy,
            make_intent(),
            budget_state=budget_state(),
        )
        self.assertIs(changed_decision.decision, ao.PolicyDecision.DENY)
        self.assertIs(changed_decision.reason_code, ao.AgentOperationsErrorCode.INVALID_TASK)

        running = ao.transition_task(approved, ao.TaskLifecycle.RUNNING, at=NOW)
        self.assertIs(running.lifecycle, ao.TaskLifecycle.RUNNING)

    def test_f10_transition_matrix_is_complete_and_terminal_states_are_one_way(self):
        def source_for(source, target):
            if source is ao.TaskLifecycle.PROPOSED and target is ao.TaskLifecycle.AWAITING_APPROVAL:
                return make_task(
                    ao.Capability.MODIFY_JOB_METADATA,
                    lifecycle=ao.TaskLifecycle.PROPOSED,
                )
            if source is ao.TaskLifecycle.AWAITING_APPROVAL:
                return make_task(
                    ao.Capability.MODIFY_JOB_METADATA,
                    lifecycle=ao.TaskLifecycle.AWAITING_APPROVAL,
                )
            return make_task(lifecycle=source)

        allowed_count = 0
        for source, targets in ao.TASK_TRANSITIONS.items():
            for target in targets:
                with self.subTest(source=source, target=target, allowed=True):
                    task = source_for(source, target)
                    at = task.expires_at if target is ao.TaskLifecycle.EXPIRED else NOW
                    approval = None
                    if source is ao.TaskLifecycle.AWAITING_APPROVAL and target is ao.TaskLifecycle.APPROVED:
                        intent = make_intent(
                            ao.Capability.MODIFY_JOB_METADATA,
                            summary="Bounded normalized metadata fields for approved records.",
                        )
                        approval = issue_test_approval(task, (intent,))
                    result = ao.transition_task(task, target, at=at, approval=approval)
                    self.assertIs(result.lifecycle, target)
                    allowed_count += 1
        self.assertEqual(allowed_count, sum(len(targets) for targets in ao.TASK_TRANSITIONS.values()))

        rejected_count = 0
        for source, targets in ao.TASK_TRANSITIONS.items():
            task = source_for(source, next(iter(targets), source))
            for target in set(ao.TaskLifecycle) - set(targets):
                with self.subTest(source=source, target=target, allowed=False):
                    with self.assertRaises(ao.AgentOperationsError):
                        ao.transition_task(task, target, at=NOW)
                    rejected_count += 1
        self.assertEqual(
            rejected_count,
            len(ao.TaskLifecycle) ** 2 - sum(len(targets) for targets in ao.TASK_TRANSITIONS.values()),
        )

        terminal_tasks = (
            make_task(lifecycle=ao.TaskLifecycle.SUCCEEDED),
            make_task(lifecycle=ao.TaskLifecycle.FAILED),
            make_task(lifecycle=ao.TaskLifecycle.CANCELLED),
            make_task(lifecycle=ao.TaskLifecycle.EXPIRED),
            make_task(lifecycle=ao.TaskLifecycle.POLICY_DENIED),
        )
        for task in terminal_tasks:
            for target in ao.TaskLifecycle:
                with self.subTest(source=task.lifecycle, target=target, replacement=True):
                    with self.assertRaises(ao.AgentOperationsError):
                        replace(task, lifecycle=target)
                    self.assertIn(task.lifecycle, ao.TERMINAL_TASK_LIFECYCLES)

    def test_f10_approval_expiry_and_receipt_boundaries_are_preserved(self):
        capability = ao.Capability.MODIFY_JOB_METADATA
        proposed = make_task(capability, lifecycle=ao.TaskLifecycle.PROPOSED)
        awaiting = ao.transition_task(proposed, ao.TaskLifecycle.AWAITING_APPROVAL, at=NOW)
        intent = make_intent(
            capability,
            summary="Bounded normalized metadata fields for approved records.",
        )
        with self.assertRaises(ao.AgentOperationsError) as missing:
            ao.transition_task(awaiting, ao.TaskLifecycle.APPROVED, at=NOW)
        self.assertIs(missing.exception.code, ao.AgentOperationsErrorCode.APPROVAL_REQUIRED)
        with self.assertRaises(ao.AgentOperationsError) as invalid:
            ao.transition_task(awaiting, ao.TaskLifecycle.APPROVED, at=NOW, approval="approved")
        self.assertIs(invalid.exception.code, ao.AgentOperationsErrorCode.INVALID_APPROVAL)

        expired_approval = issue_test_approval(
            awaiting,
            (intent,),
            expires_at=NOW + timedelta(minutes=1),
        )
        with self.assertRaises(ao.AgentOperationsError) as expired:
            ao.transition_task(
                awaiting,
                ao.TaskLifecycle.APPROVED,
                at=NOW + timedelta(minutes=1),
                approval=expired_approval,
            )
        self.assertIs(expired.exception.code, ao.AgentOperationsErrorCode.APPROVAL_EXPIRED)

        expiring_proposed = make_task(
            capability,
            lifecycle=ao.TaskLifecycle.PROPOSED,
            expires_at=NOW + timedelta(minutes=1),
        )
        expiring_awaiting = ao.transition_task(
            expiring_proposed,
            ao.TaskLifecycle.AWAITING_APPROVAL,
            at=NOW,
        )
        expiring_approval = issue_test_approval(expiring_awaiting, (intent,))
        with self.assertRaises(ao.AgentOperationsError) as task_expired:
            ao.transition_task(
                expiring_awaiting,
                ao.TaskLifecycle.APPROVED,
                at=expiring_awaiting.expires_at,
                approval=expiring_approval,
            )
        self.assertIs(task_expired.exception.code, ao.AgentOperationsErrorCode.INVALID_TRANSITION)

        valid_approval = issue_test_approval(awaiting, (intent,))
        approved = ao.transition_task(
            awaiting,
            ao.TaskLifecycle.APPROVED,
            at=NOW,
            approval=valid_approval,
        )
        self.assertIs(approved.lifecycle, ao.TaskLifecycle.APPROVED)
        self.assertIs(awaiting.lifecycle, ao.TaskLifecycle.AWAITING_APPROVAL)

    def test_f10_terminal_revival_cannot_receive_read_only_allowance(self):
        succeeded = make_task(lifecycle=ao.TaskLifecycle.SUCCEEDED)
        for target in (ao.TaskLifecycle.PROPOSED, ao.TaskLifecycle.APPROVED, ao.TaskLifecycle.RUNNING):
            with self.subTest(target=target):
                with self.assertRaises(ao.AgentOperationsError):
                    replace(succeeded, lifecycle=target)
        decision = evaluate_action(
            make_agent(),
            succeeded,
            make_intent(),
            budget_state=budget_state(),
        )
        self.assertIs(decision.decision, ao.PolicyDecision.DENY)
        self.assertIs(decision.reason_code, ao.AgentOperationsErrorCode.INVALID_TASK)

    def test_f11_evidence_freshness_and_chronology(self):
        with self.assertRaises(ao.AgentOperationsError):
            make_evidence(freshness_boundary=NOW - timedelta(seconds=1))
        future = make_evidence(
            captured_at=NOW + timedelta(minutes=1),
            freshness_boundary=NOW + timedelta(hours=1),
        )
        with self.assertRaises(ao.AgentOperationsError):
            make_task(evidence=(future,))
        stale = make_evidence(freshness_boundary=NOW + timedelta(minutes=1))
        observed = NOW + timedelta(minutes=1)
        decision = evaluate_action(
            make_agent(),
            make_task(evidence=(stale,)),
            make_intent(evidence=(stale,)),
            budget_state=budget_state(observed_at=observed),
            observed_at=observed,
        )
        self.assertIs(decision.reason_code, ao.AgentOperationsErrorCode.INVALID_TASK)
        intent = make_intent(proposed_at=NOW + timedelta(minutes=1), evidence=(future,))
        task = make_task()
        with self.assertRaises(ao.AgentOperationsError):
            issue_test_approval(task, (intent,), approved_at=NOW)
        valid = evaluate_action(make_agent(), make_task(), make_intent(), budget_state=budget_state())
        self.assertIs(valid.decision, ao.PolicyDecision.ALLOW_READ_ONLY)

    def test_f12_complete_task_mutations_invalidate_approval(self):
        capability = ao.Capability.MODIFY_JOB_METADATA
        agent = make_agent(capability)
        task = make_task(capability)
        intent = make_intent(capability, summary="Bounded normalized metadata fields for approved records.")
        approval = issue_test_approval(task, (intent,), risk_ceiling=ao.RiskLevel.HIGH)
        mutations = (
            {"objective": "Review a different bounded metadata scope."},
            {"evidence": (make_evidence(fingerprint="e" * 64),)},
            {"expires_at": task.expires_at + timedelta(minutes=1)},
            {"budget": broad_budget(maximum_cost_units=499)},
            {"risk": ao.RiskLevel.HIGH},
            {"classification": ao.DataClassification.PUBLIC},
            {"revision": 2},
        )
        for changes in mutations:
            with self.subTest(changes=tuple(changes)):
                changed_task = make_task(capability, **changes)
                decision = evaluate_action(agent, changed_task, intent, approval, budget_state())
                self.assertIs(decision.reason_code, ao.AgentOperationsErrorCode.APPROVAL_MISMATCH)
        changed_environment = ao.AgentEnvironment.DEVELOPMENT
        decision = evaluate_action(
            replace(agent, environment=changed_environment),
            make_task(capability, environment=changed_environment),
            replace(intent, environment=changed_environment),
            approval,
            budget_state(),
        )
        self.assertIs(decision.reason_code, ao.AgentOperationsErrorCode.APPROVAL_MISMATCH)
        archive_capability = ao.Capability.ARCHIVE_EXPIRED_JOBS
        archive_task = make_task(archive_capability)
        archive_intent = make_intent(
            archive_capability,
            summary="Bounded approved archival scope.",
        )
        decision = evaluate_action(
            make_agent(archive_capability),
            archive_task,
            archive_intent,
            approval,
            budget_state(),
        )
        self.assertIs(decision.reason_code, ao.AgentOperationsErrorCode.APPROVAL_MISMATCH)
        publish_capability = ao.Capability.PUBLISH_CONTENT
        publish_task = make_task(publish_capability)
        publish_intent = make_intent(
            publish_capability,
            summary="Bounded approved publication scope.",
        )
        decision = evaluate_action(
            make_agent(publish_capability),
            publish_task,
            publish_intent,
            approval,
            budget_state(),
        )
        self.assertIs(decision.reason_code, ao.AgentOperationsErrorCode.APPROVAL_MISMATCH)
        with self.assertRaises(ao.AgentOperationsError):
            replace(
                make_task(capability, lifecycle=ao.TaskLifecycle.PROPOSED),
                current_policy_version="agent-operations-a1-v2",
            )

    def test_f13_agent_rejects_capability_above_risk_ceiling(self):
        with self.assertRaises(ao.AgentOperationsError) as raised:
            make_agent(ao.Capability.ARCHIVE_EXPIRED_JOBS, risk_ceiling=ao.RiskLevel.LOW)
        self.assertIs(raised.exception.code, ao.AgentOperationsErrorCode.INVALID_AGENT_DEFINITION)

    def test_f14_ordered_intent_bundle_rejects_reordering(self):
        capability = ao.Capability.MODIFY_JOB_METADATA
        task = make_task(capability)
        first = make_intent(capability, summary="Bounded normalized metadata fields for approved records.")
        second = make_intent(
            capability,
            intent_id="ati_" + "c" * 32,
            summary="Bounded verified metadata fields for approved records.",
        )
        third = make_intent(
            capability,
            intent_id="ati_" + "d" * 32,
            summary="Bounded reviewed metadata fields for approved records.",
        )
        fourth = make_intent(
            capability,
            intent_id="ati_" + "e" * 32,
            summary="Bounded reconciled metadata fields for approved records.",
        )
        approval = issue_test_approval(task, (first, second, third))
        exact = evaluate_action(
            make_agent(capability),
            task,
            first,
            approval,
            budget_state(),
            intent_bundle=(first, second, third),
        )
        self.assertIs(exact.decision, ao.PolicyDecision.ALLOW_APPROVED_EXECUTION)
        changed_bundles = (
            (third, second, first),
            (second, third, first),
            (first, second, second),
            (first, second),
            (first, second, third, fourth),
        )
        for bundle in changed_bundles:
            with self.subTest(bundle=tuple(intent.intent_id for intent in bundle)):
                changed = evaluate_action(
                    make_agent(capability),
                    task,
                    first,
                    approval,
                    budget_state(),
                    intent_bundle=bundle,
                )
                self.assertIs(changed.reason_code, ao.AgentOperationsErrorCode.APPROVAL_MISMATCH)


class CanonicalAndIdempotencyTests(unittest.TestCase):
    def test_canonical_json_is_utf8_compact_sorted_nfc_and_round_trips(self):
        left = {"z": [2, 1], "name": "Cafe\u0301", "a": {"b": True}}
        right = {"a": {"b": True}, "name": "Caf\u00e9", "z": [2, 1]}
        encoded = ao.canonical_json_bytes(left)
        self.assertEqual(encoded, ao.canonical_json_bytes(right))
        self.assertNotIn(b" ", encoded)
        self.assertEqual(ao.parse_canonical_json(encoded), {"a": {"b": True}, "name": "Caf\u00e9", "z": [2, 1]})

    def test_parser_rejects_duplicate_keys_nonfinite_and_noncanonical_input(self):
        samples = (
            '{"domain":"wahojobs.agent_operations","payload":{"x":1,"x":2},"version":1}',
            '{"domain":"wahojobs.agent_operations","payload":NaN,"version":1}',
            '{ "domain":"wahojobs.agent_operations","payload":1,"version":1}',
        )
        for sample in samples:
            with self.subTest(sample=sample):
                with self.assertRaises(ao.AgentOperationsError):
                    ao.parse_canonical_json(sample)

    def test_serializer_rejects_secret_private_and_hidden_reasoning_material(self):
        for value in (
            {"api_key": "bounded"},
            {"value": "person@example.com"},
            {"value": "C:\\private\\workspace.sqlite"},
            {"chain_of_thought": "not retained"},
        ):
            with self.subTest(value=value):
                with self.assertRaises(ao.AgentOperationsError):
                    ao.canonical_json_bytes(value)

    def test_task_fingerprint_ignores_generated_id_and_lifecycle(self):
        first = make_task()
        second = make_task(task_id=OTHER_TASK_ID)
        running = ao.transition_task(first, ao.TaskLifecycle.RUNNING, at=NOW)
        self.assertEqual(ao.task_proposal_fingerprint(first), ao.task_proposal_fingerprint(second))
        self.assertEqual(ao.task_proposal_fingerprint(first), ao.task_proposal_fingerprint(running))

    def test_exact_changed_and_distinct_replays_are_classified(self):
        fingerprint = ao.task_proposal_fingerprint(make_task())
        existing = ao.IdempotencyRecord(ao.IdempotencyDomain.TASK_PROPOSAL, AGENT_ID, None, "task-request-0001", fingerprint)
        exact = replace(existing)
        changed = replace(existing, request_fingerprint="f" * 64)
        distinct = replace(existing, idempotency_key="task-request-0002")
        self.assertIs(ao.classify_replay(existing, exact), ao.ReplayClassification.EXACT_REPLAY)
        self.assertIs(ao.classify_replay(existing, changed), ao.ReplayClassification.IDEMPOTENCY_CONFLICT)
        self.assertIs(ao.classify_replay(existing, distinct), ao.ReplayClassification.DISTINCT_REQUEST)

    def test_principal_and_task_scopes_are_not_interchangeable(self):
        fingerprint = ao.tool_intent_fingerprint(make_intent())
        first = ao.IdempotencyRecord(ao.IdempotencyDomain.TOOL_INTENT, AGENT_ID, TASK_ID, "tool-intent-0001", fingerprint)
        for candidate in (
            replace(first, principal_scope=OTHER_AGENT_ID),
            replace(first, task_scope=OTHER_TASK_ID),
            replace(first, domain=ao.IdempotencyDomain.EXECUTION_ATTEMPT),
        ):
            with self.subTest(candidate=candidate):
                self.assertIs(ao.classify_replay(first, candidate), ao.ReplayClassification.DISTINCT_REQUEST)

    def test_execution_attempt_fingerprint_binds_attempt_task_revision_and_intent(self):
        task, intent = make_task(), make_intent()
        first = ao.execution_attempt_fingerprint(task, intent, 1)
        self.assertNotEqual(first, ao.execution_attempt_fingerprint(task, intent, 2))
        self.assertNotEqual(first, ao.execution_attempt_fingerprint(make_task(revision=2), intent, 1))


class AuditAndEscalationTests(unittest.TestCase):
    def make_chain(self):
        first = ao.AgentAuditEvent(
            event_id=EVENT_IDS[0], task_id=TASK_ID, task_revision=1, agent_id=AGENT_ID,
            event_kind=ao.AuditEventKind.TASK_PROPOSED, occurred_at=NOW,
            policy_version=ao.A1_POLICY_VERSION, previous_event_fingerprint=None,
            decision_summary="Bounded task proposal recorded.", capability=ao.Capability.EXECUTE_READ_ONLY_ANALYSIS,
            policy_decision=None, risk=ao.RiskLevel.LOW, evidence_references=(make_evidence(),),
        )
        second = ao.AgentAuditEvent(
            event_id=EVENT_IDS[1], task_id=TASK_ID, task_revision=1, agent_id=AGENT_ID,
            event_kind=ao.AuditEventKind.POLICY_EVALUATED, occurred_at=NOW + timedelta(seconds=1),
            policy_version=ao.A1_POLICY_VERSION, previous_event_fingerprint=first.event_fingerprint,
            decision_summary="Read-only policy conditions satisfied.", capability=ao.Capability.EXECUTE_READ_ONLY_ANALYSIS,
            policy_decision=ao.PolicyDecision.ALLOW_READ_ONLY, risk=ao.RiskLevel.LOW,
            evidence_references=(make_evidence(),),
        )
        third = ao.AgentAuditEvent(
            event_id=EVENT_IDS[2], task_id=TASK_ID, task_revision=1, agent_id=AGENT_ID,
            event_kind=ao.AuditEventKind.TASK_SUCCEEDED, occurred_at=NOW + timedelta(seconds=2),
            policy_version=ao.A1_POLICY_VERSION, previous_event_fingerprint=second.event_fingerprint,
            decision_summary="Bounded read-only task completed.", capability=ao.Capability.EXECUTE_READ_ONLY_ANALYSIS,
            policy_decision=ao.PolicyDecision.ALLOW_READ_ONLY, risk=ao.RiskLevel.LOW,
            evidence_references=(make_evidence(),),
        )
        return first, second, third

    def test_valid_audit_chain_and_redacted_representation(self):
        chain = self.make_chain()
        self.assertTrue(ao.validate_audit_chain(chain))
        self.assertNotIn(chain[0].decision_summary, repr(chain[0]))

    def test_audit_fingerprint_ignores_generated_event_id(self):
        event = self.make_chain()[0]
        changed_id = replace(event, event_id=EVENT_IDS[3], event_fingerprint="")
        self.assertEqual(event.event_fingerprint, changed_id.event_fingerprint)

    def test_changed_missing_reordered_and_mutated_chains_are_rejected(self):
        first, second, third = self.make_chain()
        mutated = replace(second, decision_summary="A different safe policy result.", event_fingerprint="")
        cases = (
            (first, third),
            (second, first, third),
            (first, mutated, third),
        )
        for chain in cases:
            with self.subTest(chain=chain):
                with self.assertRaises(ao.AgentOperationsError) as raised:
                    ao.validate_audit_chain(chain)
                self.assertIs(raised.exception.code, ao.AgentOperationsErrorCode.INVALID_AUDIT_CHAIN)

    def test_object_mutation_and_malformed_hash_are_detected(self):
        first, second, third = self.make_chain()
        object.__setattr__(second, "decision_summary", "Mutated safe summary.")
        with self.assertRaises(ao.AgentOperationsError):
            ao.validate_audit_chain((first, second, third))
        with self.assertRaises(ao.AgentOperationsError):
            replace(first, previous_event_fingerprint="not-a-digest", event_fingerprint="")

    def test_unsupported_event_and_hidden_reasoning_fields_are_rejected(self):
        event = self.make_chain()[0]
        with self.assertRaises(ao.AgentOperationsError):
            replace(event, event_kind="custom_event", event_fingerprint="")
        with self.assertRaises(ao.AgentOperationsError):
            replace(event, decision_summary="Saved scratchpad for later.", event_fingerprint="")
        self.assertFalse(hasattr(event, "chain_of_thought"))
        self.assertFalse(hasattr(event, "reasoning_trace"))

    def test_human_escalation_is_closed_bounded_immutable_and_redacted(self):
        escalation = ao.HumanEscalationRequest(
            escalation_id=ESCALATION_ID,
            task_id=TASK_ID,
            agent_id=AGENT_ID,
            reason=ao.EscalationReason.CONFLICTING_EVIDENCE,
            risk=ao.RiskLevel.MEDIUM,
            required_decision=ao.RequiredDecisionType.CHOOSE_OPTION,
            context_summary="Two privacy-safe aggregate signals conflict.",
            evidence_references=(make_evidence(),),
            suggested_options=("Request a refreshed summary.", "Stop the task."),
            expires_at=LATER,
        )
        self.assertNotIn(escalation.context_summary, repr(escalation))
        with self.assertRaises(FrozenInstanceError):
            escalation.reason = ao.EscalationReason.POLICY_DENIED
        with self.assertRaises(ao.AgentOperationsError):
            replace(escalation, reason="conflicting_evidence")


class PrivacyAndErrorTests(unittest.TestCase):
    def test_all_public_errors_are_bounded_and_detached(self):
        marker = "sk-private-marker-123456"
        try:
            make_intent(summary=marker)
        except ao.AgentOperationsError as error:
            rendered = str(error) + repr(error) + json.dumps(error.as_public_dict())
            self.assertNotIn(marker, rendered)
            self.assertIsNone(error.__cause__)
            self.assertTrue(error.__suppress_context__)
            self.assertEqual(set(error.as_public_dict()), {"error", "message"})
        else:
            self.fail("unsafe marker accepted")

    def test_domain_models_have_no_credential_client_callback_or_model_fields(self):
        forbidden = {"credential", "api_key", "token", "database_path", "callback", "model", "tool_client"}
        model_types = (
            ao.AgentDefinition,
            ao.AgentTask,
            ao.AgentToolIntent,
            ao.EvidenceReference,
            ao.AgentAuditEvent,
            ao.HumanEscalationRequest,
        )
        for model_type in model_types:
            names = {field.name for field in model_type.__dataclass_fields__.values()}
            with self.subTest(model_type=model_type):
                self.assertFalse(names & forbidden)

    def test_initial_policy_never_stores_hidden_reasoning(self):
        self.assertFalse(ao.A1_SAFETY_POLICY.stores_hidden_reasoning)
        source_fields = set(ao.AgentAuditEvent.__dataclass_fields__)
        self.assertFalse(source_fields & {"chain_of_thought", "reasoning_trace", "scratchpad", "hidden_prompt", "raw_model_context"})


if __name__ == "__main__":
    unittest.main()
