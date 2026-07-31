import ast
import base64
import contextlib
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
from pathlib import Path
import pickle
import socket
import types
import unittest
from unittest import mock

import wahojobs.google_oidc_authorization_transactions as domain
import wahojobs.google_oidc_transaction_protection as protection
from wahojobs.google_oidc_authorization_transactions import (
    ClaimedGoogleOidcAuthorizationMaterial,
    GoogleOidcAuthorizationTransactionCleanupResult,
    GoogleOidcAuthorizationTransactionReconciliationResult,
    PreparedDurableGoogleOidcAuthorization,
)
from wahojobs.google_oidc_transaction_protection import (
    GoogleOidcTransactionKeyAuthority,
)


ROOT = Path(__file__).resolve().parents[1]
PROTECTION_PATH = ROOT / "wahojobs" / "google_oidc_transaction_protection.py"
NOW = datetime(2026, 7, 24, 3, 41, 12, tzinfo=timezone.utc)
TRANSACTION_ID = "oidctx_" + ("a" * 32)
STATE = base64.urlsafe_b64encode(bytes(range(32))).rstrip(b"=").decode("ascii")
NONCE = (
    base64.urlsafe_b64encode(bytes(range(32, 64)))
    .rstrip(b"=")
    .decode("ascii")
)
VERIFIER = (
    base64.urlsafe_b64encode(bytes(range(64)))
    .rstrip(b"=")
    .decode("ascii")
)
REQUEST_KEY = (
    "google-oidc-"
    + base64.urlsafe_b64encode(bytes(range(96, 128)))
    .rstrip(b"=")
    .decode("ascii")
)
INVITATION = b"inv_" + (b"a" * 32) + b"." + (b"B" * 43)
LOOKUP_1 = bytes(range(128, 160))
LOOKUP_2 = bytes(range(160, 192))
LOOKUP_3 = bytes(range(192, 224))
PROTECTION_1 = bytes(range(1, 33))
PROTECTION_2 = bytes(range(33, 65))
PROTECTION_3 = bytes(range(65, 97))
FIXED_AES_NONCE = bytes(range(200, 212))


def _reachable_protection_exception_values(exception):
    pending = [("exception", exception)]
    reached = []
    seen = set()
    while pending:
        path, value = pending.pop()
        marker = id(value)
        if marker in seen:
            continue
        seen.add(marker)
        reached.append((path, value))
        if len(reached) > 20_000:
            raise AssertionError("protection exception graph was unexpectedly broad")
        if isinstance(value, BaseException):
            pending.extend(
                (f"{path}.args[{index}]", item)
                for index, item in enumerate(value.args)
            )
            if value.__cause__ is not None:
                pending.append((f"{path}.__cause__", value.__cause__))
            if value.__context__ is not None:
                pending.append((f"{path}.__context__", value.__context__))
            if value.__traceback__ is not None:
                pending.append((f"{path}.__traceback__", value.__traceback__))
        elif isinstance(value, types.TracebackType):
            frame = value.tb_frame
            if Path(frame.f_code.co_filename).resolve() == PROTECTION_PATH.resolve():
                pending.extend(
                    (f"{path}.{name}", item)
                    for name, item in frame.f_locals.items()
                )
            if value.tb_next is not None:
                pending.append((f"{path}.tb_next", value.tb_next))
        elif isinstance(value, dict):
            pending.extend(
                (f"{path}[key]", item) for item in dict.keys(value)
            )
            pending.extend(
                (f"{path}[value]", item) for item in dict.values(value)
            )
        elif isinstance(value, (tuple, list, set, frozenset)):
            pending.extend(
                (f"{path}[{index}]", item)
                for index, item in enumerate(value)
            )
        if isinstance(
            value,
            (
                str,
                bytes,
                bytearray,
                int,
                float,
                bool,
                type(None),
                type,
                types.ModuleType,
                types.FunctionType,
                types.MethodType,
                types.CodeType,
                types.FrameType,
                types.TracebackType,
            ),
        ):
            continue
        try:
            namespace = vars(value)
        except TypeError:
            namespace = None
        if namespace:
            pending.extend(
                (f"{path}.{name}", item)
                for name, item in namespace.items()
            )
        for cls in type(value).__mro__:
            slots = cls.__dict__.get("__slots__", ())
            if isinstance(slots, str):
                slots = (slots,)
            for slot in slots:
                if slot in {"__dict__", "__weakref__"}:
                    continue
                try:
                    item = object.__getattribute__(value, slot)
                except (AttributeError, TypeError):
                    continue
                pending.append((f"{path}.{slot}", item))
    return tuple(reached)


def _authority(
    *,
    lookup=((1, LOOKUP_1),),
    aes=((1, PROTECTION_1),),
    active_lookup=1,
    active_aes=1,
):
    lookup_buffers = tuple((version, bytearray(material)) for version, material in lookup)
    aes_buffers = tuple((version, bytearray(material)) for version, material in aes)
    authority = GoogleOidcTransactionKeyAuthority(
        lookup_keys=lookup_buffers,
        protection_keys=aes_buffers,
        active_lookup_version=active_lookup,
        active_protection_version=active_aes,
    )
    assert all(not buffer for _version, buffer in lookup_buffers + aes_buffers)
    return authority


def _material():
    return {
        "state": bytearray(STATE.encode("ascii")),
        "nonce": bytearray(NONCE.encode("ascii")),
        "pkce_verifier": bytearray(VERIFIER.encode("ascii")),
        "b2d1_request_key": bytearray(REQUEST_KEY.encode("ascii")),
    }


def _aad(
    authority,
    *,
    transaction_id=TRANSACTION_ID,
    environment="test",
    configuration_binding=b"fixed-google-configuration",
    state=STATE,
    lookup_version=1,
    protection_version=1,
    created_at=NOW,
):
    digest = protection._state_lookup_digest(
        authority,
        state,
        lookup_version,
    )
    fingerprint = protection._configuration_fingerprint(
        authority,
        lookup_version,
        configuration_binding,
    )
    return domain._canonical_associated_data(
        transaction_id=transaction_id,
        provider="google",
        environment_namespace=environment,
        configuration_fingerprint=fingerprint,
        state_digest_version=1,
        lookup_key_version=lookup_version,
        state_lookup_digest=digest,
        created_at=created_at,
        expires_at=created_at + timedelta(seconds=600),
        protection_envelope_version=1,
        protection_key_version=protection_version,
    )


def _envelope(authority, associated_data):
    values = _material()
    with mock.patch.object(
        protection.secrets,
        "token_bytes",
        return_value=FIXED_AES_NONCE,
    ):
        envelope = protection._protect_material(
            authority,
            associated_data=associated_data,
            **values,
        )
    assert all(not value for value in values.values())
    return envelope


def _clear_values(values):
    if type(values) is dict:
        for value in values.values():
            if type(value) is bytearray:
                domain._clear_buffer(value)
        values.clear()


class GoogleOidcTransactionProtectionTests(unittest.TestCase):
    def setUp(self):
        self._socket_create = mock.patch(
            "socket.create_connection",
            side_effect=AssertionError("external_socket_forbidden"),
        )
        self._socket_connect = mock.patch.object(
            socket.socket,
            "connect",
            side_effect=AssertionError("external_socket_forbidden"),
        )
        self._socket_create.start()
        self._socket_connect.start()

    def tearDown(self):
        self._socket_connect.stop()
        self._socket_create.stop()

    def assert_sanitized_exception_graph(
        self,
        exception,
        *,
        forbidden_objects=(),
        forbidden_markers=(),
    ):
        self.assertIsNone(exception.__cause__)
        self.assertIsNone(exception.__context__)
        reached = _reachable_protection_exception_values(exception)
        forbidden_ids = {id(value) for value in forbidden_objects}
        for path, value in reached:
            with self.subTest(exception_path=path):
                self.assertNotIn(id(value), forbidden_ids)
                if isinstance(value, str):
                    text = value
                elif isinstance(value, (bytes, bytearray)):
                    text = bytes(value).decode("utf-8", "ignore")
                else:
                    continue
                for marker in forbidden_markers:
                    self.assertNotIn(marker, text)

    def test_authority_consumes_buffers_and_exposes_only_bounded_metadata(self):
        lookup = ((3, bytearray(LOOKUP_3)), (1, bytearray(LOOKUP_1)))
        aes = ((2, bytearray(PROTECTION_2)), (1, bytearray(PROTECTION_1)))
        authority = GoogleOidcTransactionKeyAuthority.from_mutable_keys(
            lookup_keys=lookup,
            protection_keys=aes,
            active_lookup_version=3,
            active_protection_version=2,
        )
        self.addCleanup(authority.close)
        self.assertTrue(all(not value for _version, value in lookup + aes))
        self.assertEqual(authority.accepted_lookup_versions, (1, 3))
        self.assertEqual(authority.lookup_key_versions, (1, 3))
        self.assertEqual(authority.accepted_protection_versions, (1, 2))
        self.assertEqual(authority.protection_key_versions, (1, 2))
        self.assertEqual(authority.active_lookup_version, 3)
        self.assertEqual(authority.active_protection_version, 2)
        self.assertFalse(authority.closed)
        self.assertEqual(
            repr(authority),
            "GoogleOidcTransactionKeyAuthority(<configured>)",
        )
        for forbidden in (
            LOOKUP_1.hex(),
            LOOKUP_3.hex(),
            PROTECTION_1.hex(),
            PROTECTION_2.hex(),
            "active_lookup_version=3",
        ):
            self.assertNotIn(forbidden, repr(authority))

    def test_invalid_key_rings_fail_closed_and_clear_every_input_buffer(self):
        cases = (
            (
                ((1, bytearray(LOOKUP_1)), (1, bytearray(LOOKUP_2))),
                ((1, bytearray(PROTECTION_1)),),
                1,
                1,
            ),
            (
                ((1, bytearray(LOOKUP_1)), (2, bytearray(LOOKUP_1))),
                ((1, bytearray(PROTECTION_1)),),
                1,
                1,
            ),
            (
                ((1, bytearray(LOOKUP_1)),),
                ((1, bytearray(LOOKUP_1)),),
                1,
                1,
            ),
            (
                (
                    (1, bytearray(LOOKUP_1)),
                    (2, bytearray(LOOKUP_2)),
                    (3, bytearray(LOOKUP_3)),
                    (4, bytearray(b"Z" * 32)),
                ),
                ((1, bytearray(PROTECTION_1)),),
                1,
                1,
            ),
            (
                ((1, bytearray(LOOKUP_1)),),
                ((1, bytearray(PROTECTION_1)),),
                2,
                1,
            ),
            (
                ((True, bytearray(LOOKUP_1)),),
                ((1, bytearray(PROTECTION_1)),),
                1,
                1,
            ),
            (
                ((1, bytearray(LOOKUP_1[:-1])),),
                ((1, bytearray(PROTECTION_1)),),
                1,
                1,
            ),
        )
        for lookup, aes, active_lookup, active_aes in cases:
            with self.subTest(
                lookup_count=len(lookup),
                active_lookup=active_lookup,
            ):
                all_buffers = tuple(value for _version, value in lookup + aes)
                with self.assertRaisesRegex(
                    TypeError,
                    "^google_oidc_transaction_key_authority_invalid$",
                ):
                    GoogleOidcTransactionKeyAuthority(
                        lookup_keys=lookup,
                        protection_keys=aes,
                        active_lookup_version=active_lookup,
                        active_protection_version=active_aes,
                    )
                self.assertTrue(all(not value for value in all_buffers))

    def test_rejected_container_and_entry_subclasses_clear_without_dispatch(self):
        dispatched = []

        class HostileList(list):
            def __iter__(self):
                dispatched.append("list_iter")
                raise AssertionError("caller_list_iteration_executed")

            def __len__(self):
                dispatched.append("list_len")
                raise AssertionError("caller_list_length_executed")

            def __getitem__(self, key):
                dispatched.append("list_getitem")
                raise AssertionError("caller_list_getitem_executed")

        class HostileTuple(tuple):
            def __iter__(self):
                dispatched.append("tuple_iter")
                raise AssertionError("caller_tuple_iteration_executed")

            def __len__(self):
                dispatched.append("tuple_len")
                raise AssertionError("caller_tuple_length_executed")

            def __getitem__(self, key):
                dispatched.append("tuple_getitem")
                raise AssertionError("caller_tuple_getitem_executed")

        class HostileDict(dict):
            def __iter__(self):
                dispatched.append("dict_iter")
                raise AssertionError("caller_dict_iteration_executed")

            def __len__(self):
                dispatched.append("dict_len")
                raise AssertionError("caller_dict_length_executed")

            def items(self):
                dispatched.append("dict_items")
                raise AssertionError("caller_dict_items_executed")

            def values(self):
                dispatched.append("dict_values")
                raise AssertionError("caller_dict_values_executed")

        cases = (
            lambda buffer: HostileList([(1, buffer)]),
            lambda buffer: HostileTuple(((1, buffer),)),
            lambda buffer: HostileDict({1: buffer}),
            lambda buffer: (HostileList([1, buffer]),),
            lambda buffer: (HostileTuple((1, buffer)),),
        )
        for index, container in enumerate(cases):
            with self.subTest(shape=index):
                lookup_buffer = bytearray(LOOKUP_1)
                protection_buffer = bytearray(PROTECTION_1)
                with self.assertRaisesRegex(
                    TypeError,
                    "^google_oidc_transaction_key_authority_invalid$",
                ):
                    GoogleOidcTransactionKeyAuthority(
                        lookup_keys=container(lookup_buffer),
                        protection_keys=((1, protection_buffer),),
                        active_lookup_version=1,
                        active_protection_version=1,
                    )
                self.assertEqual(lookup_buffer, bytearray())
                self.assertEqual(protection_buffer, bytearray())
        self.assertEqual(dispatched, [])

    def test_rejected_mixed_partial_duplicate_and_cyclic_shapes_clear_all_buffers(self):
        cases = []

        first = bytearray(LOOKUP_1)
        malformed = bytearray(LOOKUP_2)
        protection_buffer = bytearray(PROTECTION_1)
        cases.append(
            (
                [(1, first), [2, malformed, "unexpected"]],
                [(1, protection_buffer)],
                (first, malformed, protection_buffer),
            )
        )

        first = bytearray(LOOKUP_1)
        duplicate_version = bytearray(LOOKUP_2)
        protection_buffer = bytearray(PROTECTION_1)
        cases.append(
            (
                [(1, first), (1, duplicate_version)],
                [(1, protection_buffer)],
                (first, duplicate_version, protection_buffer),
            )
        )

        first = bytearray(LOOKUP_1)
        duplicate_material = bytearray(LOOKUP_1)
        protection_buffer = bytearray(PROTECTION_1)
        cases.append(
            (
                [(1, first), (2, duplicate_material)],
                [(1, protection_buffer)],
                (first, duplicate_material, protection_buffer),
            )
        )

        first = bytearray(LOOKUP_1)
        protection_buffer = bytearray(PROTECTION_1)
        cyclic = [(1, first)]
        cyclic.append(cyclic)
        cases.append(
            (
                cyclic,
                [(1, protection_buffer)],
                (first, protection_buffer),
            )
        )

        for index, (lookup, aes, buffers) in enumerate(cases):
            with self.subTest(case=index):
                with self.assertRaisesRegex(
                    TypeError,
                    "^google_oidc_transaction_key_authority_invalid$",
                ):
                    GoogleOidcTransactionKeyAuthority(
                        lookup_keys=lookup,
                        protection_keys=aes,
                        active_lookup_version=1,
                        active_protection_version=1,
                    )
                self.assertTrue(all(buffer == bytearray() for buffer in buffers))

    def test_rejected_immutable_key_is_not_reachable_from_factory_exception(self):
        immutable_marker = b"immutable-rejected-key-material!"
        self.assertEqual(len(immutable_marker), 32)
        protection_buffer = bytearray(PROTECTION_1)
        with self.assertRaises(TypeError) as caught:
            GoogleOidcTransactionKeyAuthority.from_mutable_keys(
                lookup_keys=((1, immutable_marker),),
                protection_keys=((1, protection_buffer),),
                active_lookup_version=1,
                active_protection_version=1,
            )
        self.assertEqual(protection_buffer, bytearray())
        self.assert_sanitized_exception_graph(
            caught.exception,
            forbidden_objects=(immutable_marker, protection_buffer),
            forbidden_markers=(
                immutable_marker.decode("ascii"),
                PROTECTION_1.hex(),
            ),
        )

    def test_constructor_failure_checkpoints_clear_inputs_and_exception_graph(self):
        original_normalize = protection._normalized_key_ring
        original_validate = protection._validated_key_version
        checkpoints = (
            "first_normalize",
            "second_normalize",
            "active_lookup_version",
            "active_protection_version",
            "reuse",
            "record",
        )
        for checkpoint in checkpoints:
            with self.subTest(checkpoint=checkpoint):
                lookup_buffer = bytearray(LOOKUP_1)
                protection_buffer = bytearray(PROTECTION_1)
                lookup = ((1, lookup_buffer),)
                aes = ((1, protection_buffer),)
                normalize_calls = []
                version_calls = []
                retained_copies = []

                def normalize(value):
                    normalize_calls.append(value)
                    if (
                        checkpoint == "first_normalize"
                        and len(normalize_calls) == 1
                    ) or (
                        checkpoint == "second_normalize"
                        and len(normalize_calls) == 2
                    ):
                        raise RuntimeError(f"checkpoint-{checkpoint}")
                    normalized = original_normalize(value)
                    retained_copies.extend(normalized.values())
                    return normalized

                def validate(value):
                    version_calls.append(value)
                    if (
                        checkpoint == "active_lookup_version"
                        and len(version_calls) == 3
                    ) or (
                        checkpoint == "active_protection_version"
                        and len(version_calls) == 4
                    ):
                        raise RuntimeError(f"checkpoint-{checkpoint}")
                    return original_validate(value)

                patchers = [
                    mock.patch.object(
                        protection,
                        "_normalized_key_ring",
                        side_effect=normalize,
                    ),
                    mock.patch.object(
                        protection,
                        "_validated_key_version",
                        side_effect=validate,
                    ),
                ]
                if checkpoint == "reuse":
                    patchers.append(
                        mock.patch.object(
                            protection,
                            "_rings_reuse_material",
                            side_effect=RuntimeError("checkpoint-reuse"),
                        )
                    )
                if checkpoint == "record":
                    patchers.append(
                        mock.patch.object(
                            protection,
                            "_KeyAuthorityRecord",
                            side_effect=RuntimeError("checkpoint-record"),
                        )
                    )
                candidate = object.__new__(GoogleOidcTransactionKeyAuthority)
                with contextlib.ExitStack() as stack:
                    for patcher in patchers:
                        stack.enter_context(patcher)
                    caught = stack.enter_context(self.assertRaises(TypeError))
                    candidate.__init__(
                        lookup_keys=lookup,
                        protection_keys=aes,
                        active_lookup_version=1,
                        active_protection_version=1,
                    )
                all_buffers = (
                    lookup_buffer,
                    protection_buffer,
                    *retained_copies,
                )
                self.assertTrue(
                    all(buffer == bytearray() for buffer in all_buffers)
                )
                self.assert_sanitized_exception_graph(
                    caught.exception,
                    forbidden_objects=(candidate, *all_buffers),
                    forbidden_markers=(f"checkpoint-{checkpoint}",),
                )

    def test_authority_is_final_immutable_noncopyable_and_nonserializable(self):
        authority = _authority()
        self.addCleanup(authority.close)
        operations = (
            lambda: setattr(authority, "active_lookup_version", 9),
            lambda: copy.copy(authority),
            lambda: copy.deepcopy(authority),
            lambda: pickle.dumps(authority),
            lambda: json.dumps(authority),
            lambda: type(
                "ForgedGoogleOidcTransactionKeyAuthority",
                (GoogleOidcTransactionKeyAuthority,),
                {},
            ),
        )
        for operation in operations:
            with self.subTest(operation=operation):
                with self.assertRaises((TypeError, AttributeError)):
                    operation()
        forged = object.__new__(GoogleOidcTransactionKeyAuthority)
        with self.assertRaises(protection.GoogleOidcTransactionProtectionError):
            protection._state_lookup_digests(forged, STATE)

    def test_close_is_idempotent_clears_keys_and_makes_authority_unusable(self):
        authority = _authority(
            lookup=((1, LOOKUP_1), (2, LOOKUP_2)),
            aes=((1, PROTECTION_1), (2, PROTECTION_2)),
            active_lookup=2,
            active_aes=2,
        )
        record = object.__getattribute__(
            authority,
            "_GoogleOidcTransactionKeyAuthority__record",
        )
        retained = tuple(record.lookup_keys.values()) + tuple(
            record.protection_keys.values()
        )
        authority.close()
        authority.close()
        self.assertTrue(authority.closed)
        self.assertEqual(
            repr(authority),
            "GoogleOidcTransactionKeyAuthority(<closed>)",
        )
        self.assertTrue(all(not value for value in retained))
        self.assertEqual(record.lookup_keys, {})
        self.assertEqual(record.protection_keys, {})
        for operation in (
            lambda: authority.accepted_lookup_versions,
            lambda: authority.active_protection_version,
            lambda: protection._state_lookup_digests(authority, STATE),
            lambda: protection._configuration_fingerprint(
                authority,
                1,
                b"context",
            ),
        ):
            with self.assertRaises(
                (
                    TypeError,
                    protection.GoogleOidcTransactionProtectionError,
                )
            ):
                operation()

    def test_authority_detects_internal_key_or_active_version_tampering(self):
        for field in ("key", "active"):
            with self.subTest(field=field):
                authority = _authority(
                    lookup=((1, LOOKUP_1), (2, LOOKUP_2)),
                    aes=((1, PROTECTION_1),),
                    active_lookup=1,
                )
                self.addCleanup(authority.close)
                record = object.__getattribute__(
                    authority,
                    "_GoogleOidcTransactionKeyAuthority__record",
                )
                if field == "key":
                    record.lookup_keys[1][0] ^= 0xFF
                else:
                    record.active_lookup_version = 2
                with self.assertRaises(
                    protection.GoogleOidcTransactionProtectionError
                ):
                    protection._state_lookup_digests(authority, STATE)

    def test_state_lookup_v1_matches_independent_hmac_vector_and_rotation(self):
        authority = _authority(
            lookup=((2, LOOKUP_2), (1, LOOKUP_1), (3, LOOKUP_3)),
            aes=((1, PROTECTION_1),),
            active_lookup=2,
        )
        self.addCleanup(authority.close)
        digests = protection._state_lookup_digests(authority, STATE)
        self.assertEqual(tuple(version for version, _digest in digests), (1, 2, 3))
        lookup_domain = b"wahojobs-google-oidc-state-lookup-v1"
        payload = (
            len(lookup_domain).to_bytes(2, "big")
            + lookup_domain
            + b"\x02"
            + b"\x00\x00\x00\x04"
            + b"\x00\x00\x00\x01"
            + b"\x00\x00\x00\x2b"
            + STATE.encode("ascii")
        )
        expected = hmac.new(LOOKUP_1, payload, hashlib.sha256).digest()
        self.assertEqual(dict(digests)[1], expected)
        self.assertTrue(
            protection._verify_state_lookup_digest(
                authority,
                STATE,
                1,
                expected,
            )
        )
        changed = bytearray(expected)
        changed[-1] ^= 0x01
        self.assertFalse(
            protection._verify_state_lookup_digest(
                authority,
                STATE,
                1,
                bytes(changed),
            )
        )
        self.assertNotEqual(dict(digests)[1], dict(digests)[2])

    def test_state_lookup_rejects_noncanonical_or_wrong_length_state(self):
        authority = _authority()
        self.addCleanup(authority.close)
        invalid_values = (
            None,
            b"x" * 43,
            "x" * 42,
            "x" * 44,
            STATE[:-1] + "=",
            STATE[:-1] + "!",
            STATE[:-1] + "B",
            type("State", (str,), {})(STATE),
        )
        for value in invalid_values:
            with self.subTest(value=repr(value)):
                with self.assertRaises(
                    protection.GoogleOidcTransactionProtectionError
                ):
                    protection._state_lookup_digests(authority, value)

    def test_configuration_fingerprint_is_versioned_bounded_and_domain_separated(self):
        authority = _authority(
            lookup=((1, LOOKUP_1), (2, LOOKUP_2)),
            aes=((1, PROTECTION_1),),
            active_lookup=2,
        )
        self.addCleanup(authority.close)
        first = protection._configuration_fingerprint(
            authority,
            1,
            b"configuration",
        )
        again = protection._configuration_fingerprint(
            authority,
            1,
            b"configuration",
        )
        rotated = protection._configuration_fingerprint(
            authority,
            2,
            b"configuration",
        )
        changed = protection._configuration_fingerprint(
            authority,
            1,
            b"configuration-2",
        )
        self.assertEqual(first, again)
        self.assertEqual(len(first), 32)
        self.assertNotEqual(first, rotated)
        self.assertNotEqual(first, changed)
        self.assertNotEqual(
            first,
            protection._state_lookup_digest(authority, STATE, 1),
        )
        maximum = b"x" * domain.MAX_DURABLE_CONFIGURATION_CONTEXT_BYTES
        maximum_fingerprint = protection._configuration_fingerprint(
            authority,
            1,
            maximum,
        )
        self.assertEqual(
            maximum_fingerprint,
            protection._configuration_fingerprint(authority, 1, maximum),
        )
        for invalid in (
            b"",
            b"x" * (domain.MAX_DURABLE_CONFIGURATION_CONTEXT_BYTES + 1),
            bytearray(b"configuration"),
        ):
            with self.assertRaises(
                protection.GoogleOidcTransactionProtectionError
            ):
                protection._configuration_fingerprint(authority, 1, invalid)

    def test_canonical_time_plaintext_and_associated_data_are_deterministic(self):
        authority = _authority()
        self.addCleanup(authority.close)
        first_material = _material()
        second_material = _material()
        first = domain._serialize_protected_material_v1(**first_material)
        second = domain._serialize_protected_material_v1(**second_material)
        self.addCleanup(domain._clear_buffer, first)
        self.addCleanup(domain._clear_buffer, second)
        self.assertEqual(first, second)
        self.assertLessEqual(len(first), domain.MAX_PROTECTED_PLAINTEXT_BYTES)
        parsed = domain._parse_protected_material_v1(bytearray(first))
        try:
            self.assertEqual(bytes(parsed["state"]).decode("ascii"), STATE)
            self.assertEqual(bytes(parsed["nonce"]).decode("ascii"), NONCE)
            self.assertEqual(
                bytes(parsed["pkce_verifier"]).decode("ascii"),
                VERIFIER,
            )
            self.assertEqual(
                bytes(parsed["b2d1_request_key"]).decode("ascii"),
                REQUEST_KEY,
            )
        finally:
            _clear_values(parsed)
        aad = _aad(authority)
        self.assertEqual(aad, _aad(authority))
        self.assertEqual(
            domain._canonical_time_text(NOW),
            "2026-07-24T03:41:12+00:00",
        )
        self.assertEqual(
            domain._parse_canonical_time_text(
                "2026-07-24T03:41:12+00:00"
            ),
            NOW,
        )
        for invalid in (
            NOW.replace(microsecond=1),
            NOW.replace(tzinfo=None),
            "2026-07-24T03:41:12Z",
        ):
            with self.assertRaises(TypeError):
                if type(invalid) is str:
                    domain._parse_canonical_time_text(invalid)
                else:
                    domain._canonical_time_text(invalid)

    def test_versioned_invitation_material_preserves_legacy_and_rejects_ambiguity(self):
        legacy_source = _material()
        legacy = domain._serialize_protected_material_v1(**legacy_source)
        self.addCleanup(domain._clear_buffer, legacy)
        legacy_values = domain._parse_protected_material(bytearray(legacy))
        try:
            self.assertIsNone(legacy_values["invitation_credential"])
            self.assertEqual(
                bytes(legacy_values["b2d1_request_key"]),
                REQUEST_KEY.encode("ascii"),
            )
        finally:
            _clear_values(legacy_values)

        invitation = bytearray(INVITATION)
        source = _material()
        encoded = domain._serialize_protected_material(
            **source,
            invitation_credential=invitation,
        )
        self.addCleanup(domain._clear_buffer, encoded)
        self.assertEqual(invitation, INVITATION)
        values = domain._parse_protected_material(bytearray(encoded))
        try:
            self.assertEqual(
                bytes(values["invitation_credential"]),
                INVITATION,
            )
            self.assertEqual(bytes(values["state"]), STATE.encode("ascii"))
        finally:
            _clear_values(values)

        components = (
            domain.PROTECTED_MATERIAL_VERSION.to_bytes(4, "big"),
            STATE.encode("ascii"),
            NONCE.encode("ascii"),
            VERIFIER.encode("ascii"),
            REQUEST_KEY.encode("ascii"),
            INVITATION,
        )
        malformed = (
            domain._length_prefixed_encoding(
                domain._PROTECTED_MATERIAL_DOMAIN_V2,
                ((domain.PROTECTED_MATERIAL_VERSION + 1).to_bytes(4, "big"), *components[1:]),
            ),
            domain._length_prefixed_encoding(
                domain._PROTECTED_MATERIAL_DOMAIN_V2,
                (*components, b"duplicate"),
            ),
            domain._length_prefixed_encoding(
                b"wahojobs-google-oidc-protected-material-v3",
                components,
            ),
        )
        for candidate in malformed:
            with self.subTest(candidate_length=len(candidate)):
                with self.assertRaises(TypeError):
                    domain._parse_protected_material(bytearray(candidate))
        for invalid in (
            b"not-mutable",
            "not-bytes",
            bytearray(),
            bytearray(b"x" * (domain.MAX_INVITATION_CREDENTIAL_BYTES + 1)),
        ):
            with self.subTest(invalid_type=type(invalid).__name__):
                with self.assertRaises(TypeError):
                    domain._serialize_protected_material(
                        **_material(),
                        invitation_credential=invalid,
                    )

    def test_associated_data_binds_every_row_and_configuration_dimension(self):
        authority = _authority(
            lookup=((1, LOOKUP_1), (2, LOOKUP_2)),
            aes=((1, PROTECTION_1), (2, PROTECTION_2)),
        )
        self.addCleanup(authority.close)
        baseline = _aad(authority)
        variants = (
            _aad(
                authority,
                transaction_id="oidctx_" + ("b" * 32),
            ),
            _aad(authority, environment="development"),
            _aad(authority, configuration_binding=b"other-configuration"),
            _aad(authority, state=NONCE),
            _aad(authority, lookup_version=2),
            _aad(authority, protection_version=2),
            _aad(authority, created_at=NOW + timedelta(seconds=1)),
        )
        self.assertEqual(len(set(variants)), len(variants))
        self.assertTrue(all(value != baseline for value in variants))
        with self.assertRaises(TypeError):
            domain._canonical_associated_data(
                transaction_id=TRANSACTION_ID,
                provider="google",
                environment_namespace="test",
                configuration_fingerprint=b"f" * 32,
                state_digest_version=1,
                lookup_key_version=1,
                state_lookup_digest=b"d" * 32,
                created_at=NOW,
                expires_at=NOW + timedelta(seconds=599),
                protection_envelope_version=1,
                protection_key_version=1,
            )

    def test_aes_gcm_round_trip_uses_fixed_nonce_and_clears_sources(self):
        authority = _authority()
        self.addCleanup(authority.close)
        aad = _aad(authority)
        source = _material()
        expected = {name: bytes(value) for name, value in source.items()}
        expected["invitation_credential"] = None
        with mock.patch.object(
            protection.secrets,
            "token_bytes",
            return_value=FIXED_AES_NONCE,
        ) as random_bytes:
            envelope = protection._protect_material(
                authority,
                associated_data=aad,
                **source,
            )
        self.assertEqual(random_bytes.call_args_list, [mock.call(12)])
        self.assertTrue(all(not value for value in source.values()))
        self.assertEqual(envelope.version, 1)
        self.assertEqual(envelope.key_version, 1)
        self.assertEqual(envelope.nonce, FIXED_AES_NONCE)
        self.assertEqual(envelope.protection_nonce, FIXED_AES_NONCE)
        self.assertEqual(envelope.protected_material, envelope.ciphertext)
        self.assertGreaterEqual(len(envelope.ciphertext), 17)
        self.assertLessEqual(len(envelope.ciphertext), 528)
        values = protection._unprotect_material(
            authority,
            protection_key_version=envelope.key_version,
            protection_nonce=envelope.nonce,
            protected_material=envelope.ciphertext,
            associated_data=aad,
        )
        try:
            self.assertEqual(
                {
                    name: None if value is None else bytes(value)
                    for name, value in values.items()
                },
                expected,
            )
        finally:
            _clear_values(values)

    def test_invitation_round_trip_is_exact_bounded_and_ciphertext_only(self):
        authority = _authority()
        self.addCleanup(authority.close)
        aad = _aad(authority)
        source = _material()
        source["invitation_credential"] = bytearray(INVITATION)
        retained = tuple(source.values())
        with mock.patch.object(
            protection.secrets,
            "token_bytes",
            return_value=FIXED_AES_NONCE,
        ):
            envelope = protection._protect_material(
                authority,
                associated_data=aad,
                **source,
            )
        self.assertTrue(all(not value for value in retained))
        self.assertNotIn(INVITATION, envelope.ciphertext)
        values = protection._unprotect_material(
            authority,
            protection_key_version=envelope.key_version,
            protection_nonce=envelope.nonce,
            protected_material=envelope.ciphertext,
            associated_data=aad,
        )
        invitation = values["invitation_credential"]
        try:
            self.assertEqual(bytes(invitation), INVITATION)
        finally:
            _clear_values(values)
        self.assertEqual(invitation, bytearray())

    def test_corruption_wrong_key_nonce_aad_and_cross_row_swap_fail_generically(self):
        authority = _authority()
        other_authority = _authority(
            lookup=((1, LOOKUP_1),),
            aes=((1, PROTECTION_2),),
        )
        self.addCleanup(authority.close)
        self.addCleanup(other_authority.close)
        aad = _aad(authority)
        envelope = _envelope(authority, aad)
        corrupted = bytearray(envelope.ciphertext)
        corrupted[len(corrupted) // 2] ^= 0x01
        changed_nonce = bytearray(envelope.nonce)
        changed_nonce[-1] ^= 0x01
        other_row_aad = _aad(
            authority,
            transaction_id="oidctx_" + ("b" * 32),
        )
        cases = (
            (
                authority,
                envelope.key_version,
                envelope.nonce,
                bytes(corrupted),
                aad,
            ),
            (
                authority,
                envelope.key_version,
                bytes(changed_nonce),
                envelope.ciphertext,
                aad,
            ),
            (
                authority,
                envelope.key_version,
                envelope.nonce,
                envelope.ciphertext,
                aad + b"x",
            ),
            (
                authority,
                envelope.key_version,
                envelope.nonce,
                envelope.ciphertext,
                other_row_aad,
            ),
            (
                other_authority,
                envelope.key_version,
                envelope.nonce,
                envelope.ciphertext,
                aad,
            ),
        )
        for target, version, nonce, ciphertext, associated_data in cases:
            with self.subTest(
                target_is_original=target is authority,
                nonce=nonce,
                ciphertext_length=len(ciphertext),
                aad_length=len(associated_data),
            ):
                with self.assertRaises(
                    protection.GoogleOidcTransactionProtectionError
                ) as caught:
                    protection._unprotect_material(
                        target,
                        protection_key_version=version,
                        protection_nonce=nonce,
                        protected_material=ciphertext,
                        associated_data=associated_data,
                    )
                self.assertEqual(caught.exception.code, "unavailable")
                self.assertEqual(
                    str(caught.exception),
                    "Google OIDC transaction protection is unavailable.",
                )
                for forbidden in (
                    STATE,
                    NONCE,
                    VERIFIER,
                    REQUEST_KEY,
                    TRANSACTION_ID,
                    LOOKUP_1.hex(),
                    PROTECTION_1.hex(),
                ):
                    self.assertNotIn(forbidden, repr(caught.exception))
                    self.assertNotIn(forbidden, str(caught.exception))

    def test_ordinary_failure_exception_graph_is_structurally_sanitized(self):
        authority = _authority()
        self.addCleanup(authority.close)
        record = object.__getattribute__(
            authority,
            "_GoogleOidcTransactionKeyAuthority__record",
        )
        key_buffers = tuple(record.lookup_keys.values()) + tuple(
            record.protection_keys.values()
        )
        aad = _aad(authority)
        envelope = _envelope(authority, aad)
        failures = {
            name: RuntimeError(f"ordinary-sensitive-{name}")
            for name in (
                "lookup-validation",
                "configuration-hmac",
                "protect-encrypt",
                "unprotect-parse",
                "authority-metadata",
            )
        }
        cases = (
            (
                "lookup_validation",
                failures["lookup-validation"],
                mock.patch.object(
                    protection,
                    "_canonical_state_lookup_input",
                    side_effect=failures["lookup-validation"],
                ),
                lambda: protection._state_lookup_digests(authority, STATE),
                (),
            ),
            (
                "configuration_hmac",
                failures["configuration-hmac"],
                mock.patch.object(
                    protection,
                    "_hmac_digest",
                    side_effect=failures["configuration-hmac"],
                ),
                lambda: protection._configuration_fingerprint(
                    authority,
                    1,
                    b"configuration",
                ),
                (),
            ),
            (
                "protect_encrypt",
                failures["protect-encrypt"],
                mock.patch.object(
                    protection,
                    "_encrypt_locked",
                    side_effect=failures["protect-encrypt"],
                ),
                lambda: protection._protect_material(
                    authority,
                    associated_data=aad,
                    **protect_source,
                ),
                (),
            ),
            (
                "unprotect_parse",
                failures["unprotect-parse"],
                mock.patch.object(
                    protection,
                    "_parse_protected_material",
                    side_effect=failures["unprotect-parse"],
                ),
                lambda: protection._unprotect_material(
                    authority,
                    protection_key_version=envelope.key_version,
                    protection_nonce=envelope.nonce,
                    protected_material=envelope.ciphertext,
                    associated_data=aad,
                ),
                (envelope,),
            ),
            (
                "authority_metadata",
                failures["authority-metadata"],
                mock.patch.object(
                    protection,
                    "_key_record_attestation",
                    side_effect=failures["authority-metadata"],
                ),
                lambda: authority.accepted_lookup_versions,
                (),
            ),
        )
        for (
            name,
            dependency_failure,
            patcher,
            operation,
            extra_forbidden,
        ) in cases:
            with self.subTest(stage=name):
                protect_source = _material()
                source_buffers = tuple(protect_source.values())
                with patcher, self.assertRaises(
                    protection.GoogleOidcTransactionProtectionError
                ) as caught:
                    operation()
                if name == "protect_encrypt":
                    self.assertTrue(
                        all(buffer == bytearray() for buffer in source_buffers)
                    )
                self.assert_sanitized_exception_graph(
                    caught.exception,
                    forbidden_objects=(
                        authority,
                        record,
                        envelope,
                        *key_buffers,
                        *source_buffers,
                        *extra_forbidden,
                    ),
                    forbidden_markers=(
                        "ordinary-sensitive-",
                        STATE,
                        NONCE,
                        VERIFIER,
                        REQUEST_KEY,
                        LOOKUP_1.hex(),
                        PROTECTION_1.hex(),
                    ),
                )
                self.assertIsNone(dependency_failure.__traceback__)
                self.assertIsNone(dependency_failure.__cause__)
                self.assertIsNone(dependency_failure.__context__)

    def test_control_flow_failure_is_exact_detached_and_clears_sources(self):
        for exception_type in (
            KeyboardInterrupt,
            SystemExit,
            GeneratorExit,
        ):
            with self.subTest(control=exception_type.__name__):
                authority = _authority()
                self.addCleanup(authority.close)
                record = object.__getattribute__(
                    authority,
                    "_GoogleOidcTransactionKeyAuthority__record",
                )
                key_buffers = tuple(record.lookup_keys.values()) + tuple(
                    record.protection_keys.values()
                )
                source = _material()
                source_buffers = tuple(source.values())
                control = exception_type(
                    f"control-sensitive-{exception_type.__name__}"
                )
                with (
                    mock.patch.object(
                        protection,
                        "_encrypt_locked",
                        side_effect=control,
                    ),
                    self.assertRaises(exception_type) as caught,
                ):
                    protection._protect_material(
                        authority,
                        associated_data=b"associated-data",
                        **source,
                    )
                self.assertIs(caught.exception, control)
                self.assertTrue(
                    all(buffer == bytearray() for buffer in source_buffers)
                )
                self.assert_sanitized_exception_graph(
                    control,
                    forbidden_objects=(
                        authority,
                        record,
                        *key_buffers,
                        *source_buffers,
                    ),
                    forbidden_markers=(
                        STATE,
                        NONCE,
                        VERIFIER,
                        REQUEST_KEY,
                        LOOKUP_1.hex(),
                        PROTECTION_1.hex(),
                    ),
                )
                control.__traceback__ = None

    def test_rotation_decrypts_accepted_old_key_but_rejects_absent_version(self):
        old_authority = _authority(
            lookup=((1, LOOKUP_1), (2, LOOKUP_2)),
            aes=((1, PROTECTION_1), (2, PROTECTION_2)),
            active_lookup=1,
            active_aes=1,
        )
        rotated_authority = _authority(
            lookup=((1, LOOKUP_1), (2, LOOKUP_2)),
            aes=((1, PROTECTION_1), (2, PROTECTION_2)),
            active_lookup=2,
            active_aes=2,
        )
        retired_authority = _authority(
            lookup=((2, LOOKUP_2),),
            aes=((2, PROTECTION_2),),
            active_lookup=2,
            active_aes=2,
        )
        self.addCleanup(old_authority.close)
        self.addCleanup(rotated_authority.close)
        self.addCleanup(retired_authority.close)
        aad = _aad(
            old_authority,
            lookup_version=1,
            protection_version=1,
        )
        envelope = _envelope(old_authority, aad)
        values = protection._unprotect_material(
            rotated_authority,
            protection_key_version=1,
            protection_nonce=envelope.nonce,
            protected_material=envelope.ciphertext,
            associated_data=aad,
        )
        _clear_values(values)
        with self.assertRaises(
            protection.GoogleOidcTransactionProtectionError
        ):
            protection._unprotect_material(
                retired_authority,
                protection_key_version=1,
                protection_nonce=envelope.nonce,
                protected_material=envelope.ciphertext,
                associated_data=aad,
            )

    def test_malformed_envelopes_and_malformed_decrypted_plaintext_fail(self):
        authority = _authority()
        self.addCleanup(authority.close)
        aad = _aad(authority)
        invalid_envelopes = (
            (0, FIXED_AES_NONCE, b"x" * 17),
            (1, FIXED_AES_NONCE[:-1], b"x" * 17),
            (1, FIXED_AES_NONCE, b"x" * 16),
            (1, FIXED_AES_NONCE, b"x" * 529),
            (1, bytearray(FIXED_AES_NONCE), b"x" * 17),
            (1, FIXED_AES_NONCE, bytearray(b"x" * 17)),
        )
        for version, nonce, ciphertext in invalid_envelopes:
            with self.subTest(
                version=version,
                nonce_length=len(nonce),
                ciphertext_length=len(ciphertext),
            ):
                with self.assertRaises(
                    protection.GoogleOidcTransactionProtectionError
                ):
                    protection._unprotect_material(
                        authority,
                        protection_key_version=version,
                        protection_nonce=nonce,
                        protected_material=ciphertext,
                        associated_data=aad,
                    )

        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        malformed = AESGCM(PROTECTION_1).encrypt(
            FIXED_AES_NONCE,
            b"not-canonical-protected-material",
            aad,
        )
        with self.assertRaises(
            protection.GoogleOidcTransactionProtectionError
        ):
            protection._unprotect_material(
                authority,
                protection_key_version=1,
                protection_nonce=FIXED_AES_NONCE,
                protected_material=malformed,
                associated_data=aad,
            )

    def test_protection_failure_still_consumes_every_mutable_source(self):
        authority = _authority()
        self.addCleanup(authority.close)
        cases = (
            (_material(), b""),
            (_material(), b"x" * 2049),
            ({**_material(), "state": bytearray(b"x" * 43)}, b"aad"),
            (
                {**_material(), "b2d1_request_key": bytearray(b"x" * 55)},
                b"aad",
            ),
        )
        for values, aad in cases:
            with self.subTest(aad_length=len(aad)):
                buffers = tuple(values.values())
                with self.assertRaises(
                    protection.GoogleOidcTransactionProtectionError
                ):
                    protection._protect_material(
                        authority,
                        associated_data=aad,
                        **values,
                    )
                self.assertTrue(all(not value for value in buffers))

    def test_envelope_is_redacted_immutable_noncopyable_and_nonserializable(self):
        authority = _authority()
        self.addCleanup(authority.close)
        envelope = _envelope(authority, _aad(authority))
        self.assertEqual(repr(envelope), "_ProtectedEnvelope(<redacted>)")
        self.assertNotIn(envelope.ciphertext.hex(), repr(envelope))
        for operation in (
            lambda: setattr(envelope, "_nonce", b"x" * 12),
            lambda: copy.copy(envelope),
            lambda: copy.deepcopy(envelope),
            lambda: pickle.dumps(envelope),
            lambda: type("ForgedEnvelope", (type(envelope),), {}),
        ):
            with self.assertRaises((TypeError, AttributeError)):
                operation()

    def test_prepared_result_is_sealed_redacted_clearable_and_bounded(self):
        url = (
            "https://accounts.google.com/o/oauth2/v2/auth?"
            f"state={STATE}&nonce={NONCE}"
        )
        prepared = domain._issue_prepared_authorization(
            transaction_id=TRANSACTION_ID,
            authorization_url=url,
            created_at=NOW,
            expires_at=NOW + timedelta(seconds=600),
        )
        self.assertIs(type(prepared), PreparedDurableGoogleOidcAuthorization)
        self.assertEqual(prepared.transaction_id, TRANSACTION_ID)
        self.assertEqual(prepared.authorization_url, url)
        self.assertEqual(prepared.created_at, NOW)
        self.assertEqual(
            prepared.expires_at,
            NOW + timedelta(seconds=600),
        )
        self.assertEqual(
            repr(prepared),
            "PreparedDurableGoogleOidcAuthorization(<redacted>)",
        )
        self.assertNotIn(STATE, repr(prepared))
        self.assertNotIn(NONCE, repr(prepared))
        for operation in (
            lambda: PreparedDurableGoogleOidcAuthorization(),
            lambda: copy.copy(prepared),
            lambda: copy.deepcopy(prepared),
            lambda: pickle.dumps(prepared),
            lambda: type(
                "ForgedPrepared",
                (PreparedDurableGoogleOidcAuthorization,),
                {},
            ),
        ):
            with self.assertRaises((TypeError, AttributeError)):
                operation()
        prepared.close()
        prepared.close()
        self.assertTrue(prepared.closed)
        with self.assertRaises(TypeError):
            _ = prepared.authorization_url

    def test_claimed_material_capsule_is_sealed_one_use_and_clearable(self):
        source = _material()
        source["invitation_credential"] = bytearray(INVITATION)
        fingerprint = b"f" * 32
        capsule = domain._issue_claimed_material(
            transaction_id=TRANSACTION_ID,
            record_version=1,
            provider="google",
            environment_namespace="test",
            configuration_fingerprint=fingerprint,
            state_digest_version=1,
            lookup_key_version=1,
            created_at=NOW,
            expires_at=NOW + timedelta(seconds=600),
            claimed_at=NOW + timedelta(seconds=1),
            protection_envelope_version=1,
            protection_key_version=1,
            **source,
        )
        self.assertIs(type(capsule), ClaimedGoogleOidcAuthorizationMaterial)
        self.assertTrue(capsule.available)
        self.assertEqual(
            repr(capsule),
            "ClaimedGoogleOidcAuthorizationMaterial(<redacted>)",
        )
        for secret in (STATE, NONCE, VERIFIER, REQUEST_KEY, INVITATION.decode("ascii")):
            self.assertNotIn(secret, repr(capsule))
        values = domain._take_claimed_material(capsule)
        self.assertFalse(capsule.available)
        self.assertEqual(values["transaction_id"], TRANSACTION_ID)
        self.assertEqual(values["claimed_at"], NOW + timedelta(seconds=1))
        self.assertEqual(bytes(values["state"]).decode("ascii"), STATE)
        self.assertEqual(bytes(values["invitation_credential"]), INVITATION)
        with self.assertRaises(TypeError):
            domain._take_claimed_material(capsule)
        secret_buffers = tuple(
            values[name]
            for name in (
                "state",
                "nonce",
                "pkce_verifier",
                "b2d1_request_key",
                "invitation_credential",
            )
        )
        domain._clear_claimed_material_values(values)
        self.assertEqual(values, {})
        self.assertTrue(all(not buffer for buffer in secret_buffers))
        capsule.close()

    def test_invalid_claimed_capsule_issuance_consumes_all_secret_buffers(self):
        source = _material()
        source["invitation_credential"] = bytearray(INVITATION)
        retained = tuple(source.values())
        with self.assertRaises(TypeError):
            domain._issue_claimed_material(
                transaction_id=TRANSACTION_ID,
                record_version=1,
                provider="google",
                environment_namespace="test",
                configuration_fingerprint=b"f" * 32,
                state_digest_version=1,
                lookup_key_version=1,
                created_at=NOW,
                expires_at=NOW + timedelta(seconds=600),
                claimed_at=NOW + timedelta(seconds=600),
                protection_envelope_version=1,
                protection_key_version=1,
                **source,
            )
        self.assertTrue(all(not buffer for buffer in retained))

    def test_cleanup_and_reconciliation_results_are_bounded_and_sanitized(self):
        cleanup = domain._issue_cleanup_result(
            expired_count=2,
            deleted_count=3,
            limit=10,
            terminal_retention_seconds=60,
            candidate_inspection_limit=4000,
            terminal_candidates_inspected=7,
            skipped_too_recent=1,
            skipped_structurally_invalid=1,
            skipped_unsupported_version=0,
            skipped_chronology_invalid=0,
            known_remaining=2,
            remaining_exact=True,
            candidate_inspection_truncated=False,
            complete=False,
            commit_outcome="committed",
        )
        self.assertIs(
            type(cleanup),
            GoogleOidcAuthorizationTransactionCleanupResult,
        )
        self.assertEqual(
            cleanup.as_dict(),
            {
                "expired_count": 2,
                "deleted_count": 3,
                "limit": 10,
                "terminal_retention_seconds": 60,
                "candidate_inspection_limit": 4000,
                "terminal_candidates_inspected": 7,
                "skipped_too_recent": 1,
                "skipped_structurally_invalid": 1,
                "skipped_unsupported_version": 0,
                "skipped_chronology_invalid": 0,
                "known_remaining": 2,
                "remaining_exact": True,
                "candidate_inspection_truncated": False,
                "complete": False,
                "commit_outcome": "committed",
            },
        )
        report = domain._issue_reconciliation_result(
            inspected_rows=2,
            issues=(
                {"code": "expired_prepared", "ordinal": 1},
                {"code": "malformed_envelope", "ordinal": 2},
            ),
        )
        self.assertIs(
            type(report),
            GoogleOidcAuthorizationTransactionReconciliationResult,
        )
        self.assertEqual(report.status, "issues_detected")
        self.assertEqual(report.inspected_rows, 2)
        projection = report.as_dict()
        self.assertEqual(len(projection["issues"]), 2)
        serialized = json.dumps(projection, sort_keys=True)
        for forbidden in (
            TRANSACTION_ID,
            STATE,
            NONCE,
            LOOKUP_1.hex(),
            PROTECTION_1.hex(),
        ):
            self.assertNotIn(forbidden, serialized)
        for operation in (
            lambda: domain._issue_cleanup_result(
                expired_count=1,
                deleted_count=1,
                limit=1,
                terminal_retention_seconds=60,
                candidate_inspection_limit=4000,
                terminal_candidates_inspected=0,
                skipped_too_recent=0,
                skipped_structurally_invalid=0,
                skipped_unsupported_version=0,
                skipped_chronology_invalid=0,
                known_remaining=0,
                remaining_exact=True,
                candidate_inspection_truncated=False,
                complete=True,
                commit_outcome="committed",
            ),
            lambda: domain._issue_cleanup_result(
                expired_count=0,
                deleted_count=0,
                limit=1001,
                terminal_retention_seconds=60,
                candidate_inspection_limit=4000,
                terminal_candidates_inspected=0,
                skipped_too_recent=0,
                skipped_structurally_invalid=0,
                skipped_unsupported_version=0,
                skipped_chronology_invalid=0,
                known_remaining=0,
                remaining_exact=True,
                candidate_inspection_truncated=False,
                complete=True,
                commit_outcome="committed",
            ),
            lambda: domain._issue_reconciliation_result(
                inspected_rows=1,
                issues=({"code": "bad-code!"},),
            ),
        ):
            with self.assertRaises(TypeError):
                operation()

    def test_import_surface_is_narrow_and_aesgcm_loader_is_lazy(self):
        self.assertEqual(
            protection.__all__,
            ("GoogleOidcTransactionKeyAuthority",),
        )
        self.assertEqual(
            domain.__all__,
            (
                "PreparedDurableGoogleOidcAuthorization",
                "ClaimedGoogleOidcAuthorizationMaterial",
                "GoogleOidcAuthorizationTransactionCleanupResult",
                "GoogleOidcAuthorizationTransactionReconciliationResult",
                "TRANSACTION_RECORD_VERSION",
                "STATE_DIGEST_VERSION",
                "PROTECTION_ENVELOPE_VERSION",
                "PROTECTED_MATERIAL_VERSION",
                "ASSOCIATED_DATA_VERSION",
                "TRANSACTION_TTL_SECONDS",
            ),
        )
        tree = ast.parse(PROTECTION_PATH.read_text(encoding="utf-8"))
        cryptography_imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module.startswith(
                "cryptography"
            ):
                cryptography_imports.append(node)
        self.assertEqual(len(cryptography_imports), 1)
        parent_map = {
            child: parent
            for parent in ast.walk(tree)
            for child in ast.iter_child_nodes(parent)
        }
        current = cryptography_imports[0]
        enclosing = None
        while current in parent_map:
            current = parent_map[current]
            if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
                enclosing = current
                break
        self.assertIsNotNone(enclosing)
        self.assertEqual(enclosing.name, "_load_aesgcm")
        source = PROTECTION_PATH.read_text(encoding="utf-8")
        for forbidden in (
            "os.environ",
            "getenv(",
            "dotenv",
            "keyring",
            "secretmanager",
            "sqlite3",
            "requests",
            "urllib",
        ):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
