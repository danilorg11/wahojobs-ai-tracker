"""Owner-scoped, read-only consumption of prepared professional relations.

Matching only looks up already validated responses. This module has no model,
network, persistence or opportunity-enrichment writer. An explicit selective
preparation integration may build requests and publish responses outside the
matching request. Offline labelled fixtures exercise that same boundary.
"""
from collections import OrderedDict
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import json
import re
import threading

from wahojobs.professional_background_duration import VERSION, confirmed_fact, requirement


SEMANTIC_VERSION = 'professional_occupational_relation_v1'
MODEL_IDENTITY_POLICY_VERSION = 'professional_model_identity_v1'
# Explicit local compatibility fixture, not a claim about today's alias target
# or current prices. Each pair remains in the requested model's authorized
# budget class. No prefix matching or response-derived policy is permitted.
APPROVED_MODEL_IDENTITIES = (('gpt-5-mini', 'gpt-5-mini-2025-08-07', 'gpt-5-mini'),)


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def model_identity_policy_digest():
    return digest(dict(version=MODEL_IDENTITY_POLICY_VERSION, pairs=APPROVED_MODEL_IDENTITIES))


def validate_model_identity(requested, returned):
    if any(type(v) is not str or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,119}', v) is None
           for v in (requested, returned)):
        raise ValueError('invalid_preparation_model_identity')
    if requested != returned and (requested, returned, requested) not in APPROVED_MODEL_IDENTITIES:
        raise ValueError('invalid_preparation_model_identity')
    return dict(requested_model=requested, returned_model=returned,
                policy=model_identity_policy_digest(), budget_class=requested)


def accepted_source_binding(connection, sources):
    """Validate exact accepted capture bytes within the caller's read snapshot."""
    bound = {k: {name: value for name, value in v.items() if name != 'professional_source_binding'}
             for k, v in sources.items()}
    ids = sorted(sources)
    if not ids:
        return bound
    tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {'job_source_content_acceptances', 'job_source_content_captures'} <= tables:
        return bound
    cursor = connection.execute(f"""SELECT a.job_id, a.accepted_capture_id,
        a.promotion_policy_version, c.capture_contract_version, c.provider,
        c.external_id, c.source_url, c.material_content_sha256, c.body, c.body_format, c.metadata_json
        FROM job_source_content_acceptances a JOIN job_source_content_captures c
        ON c.id=a.accepted_capture_id AND c.job_id=a.job_id
        WHERE a.job_id IN ({','.join('?' for _ in ids)})""", ids)
    for row in cursor:
        jid, capture, policy, contract, provider, external, url, material, body, fmt, metadata = row
        source = sources[jid]
        if any(source.get(k) != v for k, v in (
                ('source_slug', provider), ('content_provider', provider), ('external_id', external),
                ('content_external_id', external), ('source_url', url), ('url', url),
                ('material_content_sha256', material), ('body', body), ('body_format', fmt), ('metadata_json', metadata))):
            continue
        bound[jid]['professional_source_binding'] = dict(
            job_id=jid, canonical_opportunity_id=source.get('canonical_opportunity_id'), external_id=external,
            provider=provider, url=url, job_source_hash=source.get('source_hash'),
            accepted_capture_id=capture, material_content_sha256=material,
            promotion_policy_version=policy, capture_contract_version=contract,
            content_digest=digest(dict(body=body, body_format=fmt, metadata_json=metadata)))
    return bound


def current_source_binding(source):
    """Reject stale attached bindings even for an in-memory source packet."""
    binding = source.get('professional_source_binding')
    if not isinstance(binding, dict) or any(binding.get(k) != source.get(s) for k, s in (
            ('job_id', 'job_id'), ('canonical_opportunity_id', 'canonical_opportunity_id'),
            ('external_id', 'external_id'), ('provider', 'source_slug'), ('url', 'url'),
            ('job_source_hash', 'source_hash'), ('material_content_sha256', 'material_content_sha256'))):
        return None
    if binding.get('content_digest') != digest({k: source.get(k) for k in ('body', 'body_format', 'metadata_json')}):
        return None
    return deepcopy(binding)


@dataclass(frozen=True, repr=False)
class ComparisonContext:
    """Request-local authority; never added to a profile or public source."""
    owner: tuple
    profile_id: str
    revision_id: str
    profile_digest: str
    evidence: object = field(repr=False)

    def __post_init__(self):
        if (type(self.owner) is not tuple or len(self.owner) != 3
                or any(type(v) is not str or not v for v in self.owner)
                or any(type(v) is not str or not v for v in (self.profile_id, self.revision_id, self.profile_digest))
                or type(self.evidence) is not ProfessionalBackgroundEvidence):
            raise ValueError('invalid_professional_comparison_context')

    def __repr__(self):
        return 'ComparisonContext(content=<redacted>)'


def build_request(packet, comparison, profile, context):
    """Build exact source/clause and confirmed role-fact inputs, not a result.

    The entire profile content digest invalidates reuse even when the selected
    facts happen to be unchanged. Owner/revision identity is supplied only by the
    authenticated boundary. Item experience and AI review labels are not inputs.
    """
    if type(context) is not ComparisonContext or digest(profile) != context.profile_digest:
        return None
    if profile.get('identity', {}).get('profile_id') != context.profile_id:
        return None
    source = packet.get('professional_source_binding')
    ref = comparison.get('source') or {}
    if (comparison.get('kind') != 'professional_background' or not isinstance(source, dict)
            or type(source.get('accepted_capture_id')) is not int
            or source.get('accepted_capture_id', 0) <= 0
            or source.get('job_id') != packet.get('job_id')
            or source.get('material_content_sha256') != packet.get('source_hash')
            or source.get('external_id') != packet.get('external_id') or source.get('url') != packet.get('url')
            or any(ref.get(k) != packet.get(k) for k in ('job_id', 'external_id', 'url', 'source_hash'))):
        return None
    # Narrow first contract: the compound may remain unresolved, but its
    # complete named occupational span must be deterministically identifiable.
    req = requirement(ref.get('quote', ''))
    if req is None:
        return None
    start = ref['quote'].find(req['scope'])
    if start < 0:
        return None
    facts = {}
    for collection in ('recent_roles', 'job_titles'):
        for i, value in enumerate(profile.get('experience', {}).get(collection, [])):
            fact = confirmed_fact(profile, f'experience.{collection}[{i}]', value)
            if fact and isinstance(value, str) and value.strip():
                facts['fact:' + digest(fact)] = fact
    if not facts:
        return None
    binding = dict(owner=list(context.owner), profile_id=context.profile_id, revision_id=context.revision_id,
                   profile_digest=context.profile_digest, source=deepcopy(source), clause=deepcopy(ref),
                   modality=comparison['modality'], source_text_digest=digest(packet['text']),
                   component_version=VERSION, semantic_version=SEMANTIC_VERSION,
                   recipe=context.evidence.recipe, model=context.evidence.model,
                   model_identity_policy=model_identity_policy_digest(),
                   basis=context.evidence.basis)
    request = dict(binding=binding, source_text=packet['text'], occupational_span=dict(start=start, end=start + len(req['scope'])),
                   candidate_facts=facts)
    return dict(request, request_id=digest(request))


def output_schema(request):
    """Responses wire schema; exact evidence and uniqueness remain local.

    Responses rejects uniqueItems. Do not add it here or deduplicate outputs:
    validate_response checks the original array before any publication.
    Each call builds a fresh schema; no shared internal schema is weakened.
    """
    return dict(type='object', additionalProperties=False,
        required=['request_id', 'relation', 'candidate_fact_ids', 'source_span', 'rationale'], properties={
        'request_id': dict(type='string', enum=[request['request_id']]),
        'relation': dict(type='string', enum=['supported_partial', 'not_established', 'ambiguous', 'contradicted']),
        'candidate_fact_ids': dict(type='array', maxItems=len(request['candidate_facts']),
                                   items=dict(type='string', enum=list(request['candidate_facts']))),
        'source_span': dict(type='object', additionalProperties=False, required=['start', 'end'], properties={
            k: dict(type='integer', enum=[v]) for k, v in request['occupational_span'].items()}),
        'rationale': dict(type='string', minLength=1, maxLength=600)})


def validate_response(request, output):
    if (type(request) is not dict or request.get('request_id') != digest({k:v for k,v in request.items() if k != 'request_id'})
            or type(output) is not dict
            or set(output) != {'request_id', 'relation', 'candidate_fact_ids', 'source_span', 'rationale'}
            or output['request_id'] != request['request_id']
            or output['relation'] not in ('supported_partial', 'not_established', 'ambiguous', 'contradicted')
            or type(output['candidate_fact_ids']) is not list
            or any(type(v) is not str or v not in request['candidate_facts'] for v in output['candidate_fact_ids'])
            or any(v in output['candidate_fact_ids'][:i] for i, v in enumerate(output['candidate_fact_ids']))
            or (output['relation'] == 'supported_partial' and not output['candidate_fact_ids'])
            or type(output['source_span']) is not dict or output['source_span'] != request['occupational_span']
            or any(type(v) is not int for v in output['source_span'].values())
            or type(output['rationale']) is not str or not output['rationale'].strip() or len(output['rationale']) > 600):
        raise ValueError('invalid_professional_relation_output')
    return deepcopy(output)


class ProfessionalBackgroundEvidence:
    """Bounded process-local prepared evidence, with explicit publish/lookup.

No callable provider is accepted, so a matching lookup cannot invoke a model.
Publishing is a separate, explicitly authorized operation. Generation changes
invalidate recommendation reuse. Entries are never global opportunity facts.
"""
    def __init__(self, *, recipe, model, basis, capacity=256):
        if (any(type(v) is not str or not v or len(v) > 120 for v in (recipe, model))
                or basis not in ('offline_labelled_stub', 'semantic_model_output')
                or type(capacity) is not int or not 1 <= capacity <= 1024):
            raise ValueError('invalid_professional_evidence_configuration')
        self.recipe, self.model, self.basis = recipe, model, basis
        self._capacity, self._generation = capacity, 0
        self._entries, self._lock = OrderedDict(), threading.RLock()

    @property
    def generation(self):
        with self._lock:
            return self._generation

    @property
    def generation_token(self):
        with self._lock:
            return (self._generation, self.recipe, self.model, self.basis,
                    VERSION, SEMANTIC_VERSION, model_identity_policy_digest())

    @contextmanager
    def consume_generation(self, expected):
        """Linearize result acceptance against publish, with one bounded check.

        Consumers do database/comparison work before this guard, then accept
        their response/register its context under this same publication lock.
        A raced computation is unusable, even if individual lookups succeeded.
        No model/network work belongs inside this guard. Publications after
        acceptance belong to subsequent requests, not the in-flight response.
        """
        with self._lock:
            if self.generation_token != expected:
                raise ValueError('professional_evidence_changed_during_consumption')
            yield

    def publish(self, request, output, *, model_identity=None):
        result = validate_response(request, output)
        binding = request['binding']
        if any(binding.get(k) != v for k, v in (
                ('recipe', self.recipe), ('model', self.model), ('basis', self.basis),
                ('component_version', VERSION), ('semantic_version', SEMANTIC_VERSION),
                ('model_identity_policy', model_identity_policy_digest()))):
            raise ValueError('professional_evidence_version_mismatch')
        if model_identity is not None:
            if (type(model_identity) is not dict or model_identity != validate_model_identity(
                    self.model, model_identity.get('returned_model'))):
                raise ValueError('invalid_preparation_model_identity')
        elif self.basis == 'semantic_model_output':
            raise ValueError('invalid_preparation_model_identity')
        with self._lock:
            self._entries[request['request_id']] = (result, deepcopy(model_identity))
            self._entries.move_to_end(request['request_id'])
            while len(self._entries) > self._capacity:
                self._entries.popitem(last=False)
            self._generation += 1

    def lookup(self, request):
        if request is None:
            return None
        with self._lock:
            result = self._entries.get(request['request_id'])
            if result is None:
                return None
            try:
                if request['binding'].get('model_identity_policy') != model_identity_policy_digest():
                    return None
                output, identity = result
                output = validate_response(request, output)
                if identity is not None:
                    if identity != validate_model_identity(self.model, identity['returned_model']):
                        return None
                    output['model_identity'] = deepcopy(identity)  # local metadata, never model authority
                return output
            except (ValueError, TypeError, KeyError):
                return None

    def __repr__(self):
        return 'ProfessionalBackgroundEvidence(content=<redacted>)'
