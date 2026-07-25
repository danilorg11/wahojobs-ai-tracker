"""Key authority and authenticated protection for durable Google OIDC state.

Key material is supplied explicitly in mutable caller-owned buffers.  This
module has no production loader or implicit configuration source.  AESGCM is
loaded only when a protection operation is requested, keeping module import
free of third-party backend initialization.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading

from wahojobs.google_oidc_authorization_transactions import (
    MAX_KEY_VERSION,
    MAX_PROTECTED_CIPHERTEXT_BYTES,
    MAX_PROTECTED_PLAINTEXT_BYTES,
    PROTECTION_ENVELOPE_VERSION,
    _canonical_configuration_binding_input,
    _canonical_state_lookup_input,
    _clear_buffer,
    _parse_protected_material_v1,
    _serialize_protected_material_v1,
)


__all__ = ("GoogleOidcTransactionKeyAuthority",)


_KEY_BYTES = 32
_NONCE_BYTES = 12
_MAX_RING_KEYS = 3
_MAX_ASSOCIATED_DATA_BYTES = 2048
_MAX_KEY_INPUT_CLEANUP_NODES = 64
_KEY_RECORD_CAPABILITY = object()
_ENVELOPE_ISSUANCE_CAPABILITY = object()
_SANITIZED_FAILURE = object()
_LOOKUP_ATTESTATION_DOMAIN = (
    b"wahojobs-google-oidc-lookup-key-attestation-v1\x00"
)
_PROTECTION_ATTESTATION_DOMAIN = (
    b"wahojobs-google-oidc-protection-key-attestation-v1\x00"
)
_CONFIGURATION_FINGERPRINT_DOMAIN = (
    b"wahojobs-google-oidc-configuration-fingerprint-v1\x00"
)
_PUBLIC_FAILURE_MESSAGE = "Google OIDC transaction protection is unavailable."


class _ControlFlowSignal:
    __slots__ = ("control",)

    def __init__(self, control):
        self.control = control


class GoogleOidcTransactionProtectionError(Exception):
    """One bounded protection failure with no key, row, or material detail."""

    __slots__ = ("code",)

    def __init__(self):
        self.code = "unavailable"
        super().__init__(_PUBLIC_FAILURE_MESSAGE)

    def as_public_dict(self):
        return {"error": self.code, "message": _PUBLIC_FAILURE_MESSAGE}

    def __repr__(self):
        return "GoogleOidcTransactionProtectionError(code='unavailable')"

    def __reduce_ex__(self, _protocol):
        raise TypeError("google_oidc_transaction_protection_not_serializable")

    def __copy__(self):
        raise TypeError("google_oidc_transaction_protection_not_copyable")

    def __deepcopy__(self, _memo):
        raise TypeError("google_oidc_transaction_protection_not_copyable")

    def __init_subclass__(cls, **_kwargs):
        raise TypeError("google_oidc_transaction_protection_not_subclassable")


class GoogleOidcTransactionKeyAuthority:
    """Exact-type, explicitly configured, clearable lookup/AES key authority."""

    __slots__ = ("__record", "__weakref__")

    def __init__(
        self,
        *,
        lookup_keys,
        protection_keys,
        active_lookup_version,
        active_protection_version,
    ):
        result = _initialize_authority_sensitive(
            self,
            lookup_keys,
            protection_keys,
            active_lookup_version,
            active_protection_version,
        )
        if result is _SANITIZED_FAILURE:
            self = None
            lookup_keys = None
            protection_keys = None
            active_lookup_version = None
            active_protection_version = None
            result = None
            raise TypeError(
                "google_oidc_transaction_key_authority_invalid"
            ) from None
        if type(result) is _ControlFlowSignal:
            control = result.control
            self = None
            lookup_keys = None
            protection_keys = None
            active_lookup_version = None
            active_protection_version = None
            result = None
            _detach_exception(control)
            raise control from None

    @classmethod
    def from_mutable_keys(
        cls,
        *,
        lookup_keys,
        protection_keys,
        active_lookup_version,
        active_protection_version,
    ):
        authority = None
        result = None
        if cls is not GoogleOidcTransactionKeyAuthority:
            _clear_collected_key_buffers(lookup_keys, protection_keys)
            cls = None
            lookup_keys = None
            protection_keys = None
            active_lookup_version = None
            active_protection_version = None
            raise TypeError("google_oidc_transaction_key_authority_invalid")
        try:
            authority = cls(
                lookup_keys=lookup_keys,
                protection_keys=protection_keys,
                active_lookup_version=active_lookup_version,
                active_protection_version=active_protection_version,
            )
        except (KeyboardInterrupt, SystemExit, GeneratorExit) as control:
            _detach_exception(control)
            result = _ControlFlowSignal(control)
        except Exception as exc:
            _detach_exception(exc)
            result = _SANITIZED_FAILURE
        cls = None
        lookup_keys = None
        protection_keys = None
        active_lookup_version = None
        active_protection_version = None
        if result is _SANITIZED_FAILURE:
            result = None
            raise TypeError(
                "google_oidc_transaction_key_authority_invalid"
            ) from None
        if type(result) is _ControlFlowSignal:
            control = result.control
            result = None
            _detach_exception(control)
            raise control from None
        return authority

    @property
    def accepted_lookup_versions(self):
        result = _authority_metadata_sensitive(self, "lookup_versions")
        self = None
        return _publish_sensitive_result(result)

    @property
    def lookup_key_versions(self):
        result = _authority_metadata_sensitive(self, "lookup_versions")
        self = None
        return _publish_sensitive_result(result)

    @property
    def accepted_protection_versions(self):
        result = _authority_metadata_sensitive(self, "protection_versions")
        self = None
        return _publish_sensitive_result(result)

    @property
    def protection_key_versions(self):
        result = _authority_metadata_sensitive(self, "protection_versions")
        self = None
        return _publish_sensitive_result(result)

    @property
    def active_lookup_version(self):
        result = _authority_metadata_sensitive(self, "active_lookup")
        self = None
        return _publish_sensitive_result(result)

    @property
    def active_protection_version(self):
        result = _authority_metadata_sensitive(self, "active_protection")
        self = None
        return _publish_sensitive_result(result)

    @property
    def closed(self):
        try:
            record = object.__getattribute__(
                self,
                "_GoogleOidcTransactionKeyAuthority__record",
            )
        except AttributeError:
            return True
        if type(record) is not _KeyAuthorityRecord:
            return True
        with record.lock:
            return record.closed

    def close(self):
        try:
            record = object.__getattribute__(
                self,
                "_GoogleOidcTransactionKeyAuthority__record",
            )
        except AttributeError:
            return
        if type(record) is not _KeyAuthorityRecord:
            return
        with record.lock:
            if record.closed:
                return
            _clear_key_ring(record.lookup_keys)
            _clear_key_ring(record.protection_keys)
            record.lookup_keys.clear()
            record.protection_keys.clear()
            record.attestation = None
            record.closed = True

    def __setattr__(self, _name, _value):
        self = None
        _name = None
        _value = None
        raise AttributeError("google_oidc_transaction_key_authority_is_immutable")

    def __repr__(self):
        state = "closed" if self.closed else "configured"
        return f"GoogleOidcTransactionKeyAuthority(<{state}>)"

    __str__ = __repr__

    def __reduce_ex__(self, _protocol):
        self = None
        _protocol = None
        raise TypeError("google_oidc_transaction_key_authority_not_serializable")

    def __copy__(self):
        self = None
        raise TypeError("google_oidc_transaction_key_authority_not_copyable")

    def __deepcopy__(self, _memo):
        self = None
        _memo = None
        raise TypeError("google_oidc_transaction_key_authority_not_copyable")

    def __init_subclass__(cls, **_kwargs):
        raise TypeError("google_oidc_transaction_key_authority_not_subclassable")

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


class _ProtectedEnvelope:
    """Internal immutable projection of an AES-GCM envelope."""

    __slots__ = (
        "_version",
        "_key_version",
        "_nonce",
        "_ciphertext",
    )

    def __new__(cls, *_args, **_kwargs):
        raise TypeError("google_oidc_protection_envelope_required")

    @classmethod
    def _issue(
        cls,
        capability,
        *,
        key_version,
        nonce,
        ciphertext,
    ):
        if (
            cls is not _ProtectedEnvelope
            or capability is not _ENVELOPE_ISSUANCE_CAPABILITY
            or type(key_version) is not int
            or not (1 <= key_version <= MAX_KEY_VERSION)
            or type(nonce) is not bytes
            or len(nonce) != _NONCE_BYTES
            or type(ciphertext) is not bytes
            or not (17 <= len(ciphertext) <= MAX_PROTECTED_CIPHERTEXT_BYTES)
        ):
            raise TypeError("google_oidc_protection_envelope_invalid")
        instance = object.__new__(cls)
        object.__setattr__(
            instance,
            "_version",
            PROTECTION_ENVELOPE_VERSION,
        )
        object.__setattr__(instance, "_key_version", key_version)
        object.__setattr__(instance, "_nonce", bytes(nonce))
        object.__setattr__(instance, "_ciphertext", bytes(ciphertext))
        return instance

    @property
    def version(self):
        return self._version

    @property
    def key_version(self):
        return self._key_version

    @property
    def nonce(self):
        return self._nonce

    @property
    def protection_nonce(self):
        return self._nonce

    @property
    def ciphertext(self):
        return self._ciphertext

    @property
    def protected_material(self):
        return self._ciphertext

    def __setattr__(self, _name, _value):
        self = None
        _name = None
        _value = None
        raise AttributeError("google_oidc_protection_envelope_is_immutable")

    def __repr__(self):
        return "_ProtectedEnvelope(<redacted>)"

    __str__ = __repr__

    def __reduce_ex__(self, _protocol):
        self = None
        _protocol = None
        raise TypeError("google_oidc_protection_envelope_not_serializable")

    def __copy__(self):
        self = None
        raise TypeError("google_oidc_protection_envelope_not_copyable")

    def __deepcopy__(self, _memo):
        self = None
        _memo = None
        raise TypeError("google_oidc_protection_envelope_not_copyable")

    def __init_subclass__(cls, **_kwargs):
        raise TypeError("google_oidc_protection_envelope_not_subclassable")


class _KeyAuthorityRecord:
    __slots__ = (
        "capability",
        "lookup_keys",
        "protection_keys",
        "active_lookup_version",
        "active_protection_version",
        "lock",
        "closed",
        "attestation",
    )

    def __init__(
        self,
        *,
        capability,
        lookup_keys,
        protection_keys,
        active_lookup_version,
        active_protection_version,
    ):
        self.capability = capability
        self.lookup_keys = lookup_keys
        self.protection_keys = protection_keys
        self.active_lookup_version = active_lookup_version
        self.active_protection_version = active_protection_version
        self.lock = threading.Lock()
        self.closed = False
        self.attestation = _key_record_attestation(self)


def _initialize_authority_sensitive(
    authority,
    lookup_keys,
    protection_keys,
    active_lookup_version,
    active_protection_version,
):
    input_buffers = _collect_input_key_buffers(
        lookup_keys,
        protection_keys,
    )
    lookup_copies = {}
    protection_copies = {}
    record = None
    installed = False
    try:
        if type(authority) is not GoogleOidcTransactionKeyAuthority:
            raise TypeError("google_oidc_transaction_key_authority_invalid")
        try:
            object.__getattribute__(
                authority,
                "_GoogleOidcTransactionKeyAuthority__record",
            )
        except AttributeError:
            pass
        else:
            raise TypeError("google_oidc_transaction_key_authority_invalid")
        lookup_copies = _normalized_key_ring(lookup_keys)
        protection_copies = _normalized_key_ring(protection_keys)
        active_lookup_version = _validated_key_version(
            active_lookup_version
        )
        active_protection_version = _validated_key_version(
            active_protection_version
        )
        if (
            active_lookup_version not in lookup_copies
            or active_protection_version not in protection_copies
            or _rings_reuse_material(lookup_copies, protection_copies)
        ):
            raise TypeError("google_oidc_transaction_key_authority_invalid")
        record = _KeyAuthorityRecord(
            capability=_KEY_RECORD_CAPABILITY,
            lookup_keys=lookup_copies,
            protection_keys=protection_copies,
            active_lookup_version=active_lookup_version,
            active_protection_version=active_protection_version,
        )
        object.__setattr__(
            authority,
            "_GoogleOidcTransactionKeyAuthority__record",
            record,
        )
        installed = True
        return None
    except (KeyboardInterrupt, SystemExit, GeneratorExit) as control:
        _detach_exception(control)
        return _ControlFlowSignal(control)
    except Exception as exc:
        _detach_exception(exc)
        return _SANITIZED_FAILURE
    finally:
        for buffer in input_buffers:
            _clear_buffer(buffer)
        if not installed:
            _clear_key_ring(lookup_copies)
            _clear_key_ring(protection_copies)
        authority = None
        lookup_keys = None
        protection_keys = None
        active_lookup_version = None
        active_protection_version = None
        input_buffers = None
        lookup_copies = None
        protection_copies = None
        record = None


def _authority_metadata_sensitive(authority, field):
    record = None
    try:
        record = _usable_authority_record(authority)
        with record.lock:
            _require_usable_record_locked(record)
            if field == "lookup_versions":
                return tuple(sorted(record.lookup_keys))
            if field == "protection_versions":
                return tuple(sorted(record.protection_keys))
            if field == "active_lookup":
                return record.active_lookup_version
            if field == "active_protection":
                return record.active_protection_version
            raise TypeError("google_oidc_transaction_key_authority_unavailable")
    except (KeyboardInterrupt, SystemExit, GeneratorExit) as control:
        _detach_exception(control)
        return _ControlFlowSignal(control)
    except Exception as exc:
        _detach_exception(exc)
        return _SANITIZED_FAILURE
    finally:
        authority = None
        field = None
        record = None


def _publish_sensitive_result(result):
    if result is _SANITIZED_FAILURE:
        result = None
        raise _protection_failure() from None
    if type(result) is _ControlFlowSignal:
        control = result.control
        result = None
        _detach_exception(control)
        raise control from None
    return result


def _state_lookup_digests(authority, state):
    result = _state_lookup_digests_sensitive(authority, state)
    authority = None
    state = None
    return _publish_sensitive_result(result)


def _state_lookup_digests_sensitive(authority, state):
    canonical = None
    record = None
    try:
        canonical = _canonical_state_lookup_input(state)
        record = _usable_authority_record(authority)
        with record.lock:
            _require_usable_record_locked(record)
            return tuple(
                (
                    version,
                    _hmac_digest(record.lookup_keys[version], canonical),
                )
                for version in sorted(record.lookup_keys)
            )
    except (KeyboardInterrupt, SystemExit, GeneratorExit) as control:
        _detach_exception(control)
        return _ControlFlowSignal(control)
    except Exception as exc:
        _detach_exception(exc)
        return _SANITIZED_FAILURE
    finally:
        canonical = None
        state = None
        authority = None
        record = None


def _state_lookup_digest(authority, state, lookup_key_version):
    result = _state_lookup_digest_sensitive(
        authority,
        state,
        lookup_key_version,
    )
    authority = None
    state = None
    lookup_key_version = None
    return _publish_sensitive_result(result)


def _state_lookup_digest_sensitive(authority, state, lookup_key_version):
    canonical = None
    try:
        lookup_key_version = _validated_key_version(lookup_key_version)
        canonical = _canonical_state_lookup_input(state)
        return _lookup_hmac_for_version(
            authority,
            lookup_key_version,
            canonical,
        )
    except (KeyboardInterrupt, SystemExit, GeneratorExit) as control:
        _detach_exception(control)
        return _ControlFlowSignal(control)
    except Exception as exc:
        _detach_exception(exc)
        return _SANITIZED_FAILURE
    finally:
        canonical = None
        state = None
        authority = None
        lookup_key_version = None


def _verify_state_lookup_digest(
    authority,
    state,
    lookup_key_version,
    expected_digest,
):
    result = _verify_state_lookup_digest_sensitive(
        authority,
        state,
        lookup_key_version,
        expected_digest,
    )
    authority = None
    state = None
    lookup_key_version = None
    expected_digest = None
    return _publish_sensitive_result(result)


def _verify_state_lookup_digest_sensitive(
    authority,
    state,
    lookup_key_version,
    expected_digest,
):
    canonical = None
    actual = None
    try:
        if type(expected_digest) is not bytes or len(expected_digest) != 32:
            raise TypeError("google_oidc_state_lookup_digest_invalid")
        lookup_key_version = _validated_key_version(lookup_key_version)
        canonical = _canonical_state_lookup_input(state)
        actual = _lookup_hmac_for_version(
            authority,
            lookup_key_version,
            canonical,
        )
        return hmac.compare_digest(actual, expected_digest)
    except (KeyboardInterrupt, SystemExit, GeneratorExit) as control:
        _detach_exception(control)
        return _ControlFlowSignal(control)
    except Exception as exc:
        _detach_exception(exc)
        return _SANITIZED_FAILURE
    finally:
        canonical = None
        actual = None
        expected_digest = None
        state = None
        authority = None
        lookup_key_version = None


def _configuration_fingerprint(
    authority,
    lookup_key_version,
    configuration_binding,
):
    result = _configuration_fingerprint_sensitive(
        authority,
        lookup_key_version,
        configuration_binding,
    )
    authority = None
    lookup_key_version = None
    configuration_binding = None
    return _publish_sensitive_result(result)


def _configuration_fingerprint_sensitive(
    authority,
    lookup_key_version,
    configuration_binding,
):
    canonical = None
    payload = None
    try:
        lookup_key_version = _validated_key_version(lookup_key_version)
        canonical = _canonical_configuration_binding_input(
            configuration_binding
        )
        payload = (
            _CONFIGURATION_FINGERPRINT_DOMAIN
            + len(canonical).to_bytes(4, "big")
            + canonical
        )
        return _lookup_hmac_for_version(
            authority,
            lookup_key_version,
            payload,
        )
    except (KeyboardInterrupt, SystemExit, GeneratorExit) as control:
        _detach_exception(control)
        return _ControlFlowSignal(control)
    except Exception as exc:
        _detach_exception(exc)
        return _SANITIZED_FAILURE
    finally:
        canonical = None
        payload = None
        configuration_binding = None
        authority = None
        lookup_key_version = None


def _protect_material(
    authority,
    *,
    state,
    nonce,
    pkce_verifier,
    b2d1_request_key,
    associated_data,
):
    result = _protect_material_sensitive(
        authority,
        state=state,
        nonce=nonce,
        pkce_verifier=pkce_verifier,
        b2d1_request_key=b2d1_request_key,
        associated_data=associated_data,
    )
    authority = None
    state = None
    nonce = None
    pkce_verifier = None
    b2d1_request_key = None
    associated_data = None
    return _publish_sensitive_result(result)


def _protect_material_sensitive(
    authority,
    *,
    state,
    nonce,
    pkce_verifier,
    b2d1_request_key,
    associated_data,
):
    source_buffers = (state, nonce, pkce_verifier, b2d1_request_key)
    plaintext = None
    generated_nonce = None
    ciphertext = None
    record = None
    key_version = None
    try:
        associated_data = _validated_associated_data(associated_data)
        plaintext = _serialize_protected_material_v1(
            state=state,
            nonce=nonce,
            pkce_verifier=pkce_verifier,
            b2d1_request_key=b2d1_request_key,
        )
        if not (1 <= len(plaintext) <= MAX_PROTECTED_PLAINTEXT_BYTES):
            raise TypeError("google_oidc_protected_material_invalid")
        record = _usable_authority_record(authority)
        with record.lock:
            _require_usable_record_locked(record)
            key_version = record.active_protection_version
            generated_nonce = secrets.token_bytes(_NONCE_BYTES)
            if (
                type(generated_nonce) is not bytes
                or len(generated_nonce) != _NONCE_BYTES
            ):
                raise TypeError("google_oidc_protection_nonce_invalid")
            ciphertext = _encrypt_locked(
                record.protection_keys[key_version],
                generated_nonce,
                plaintext,
                associated_data,
            )
        if (
            type(ciphertext) is not bytes
            or len(ciphertext) != len(plaintext) + 16
            or len(ciphertext) > MAX_PROTECTED_CIPHERTEXT_BYTES
        ):
            raise TypeError("google_oidc_protected_material_invalid")
        return _ProtectedEnvelope._issue(
            _ENVELOPE_ISSUANCE_CAPABILITY,
            key_version=key_version,
            nonce=generated_nonce,
            ciphertext=ciphertext,
        )
    except (KeyboardInterrupt, SystemExit, GeneratorExit) as control:
        _detach_exception(control)
        return _ControlFlowSignal(control)
    except Exception as exc:
        _detach_exception(exc)
        return _SANITIZED_FAILURE
    finally:
        for buffer in source_buffers:
            _clear_buffer(buffer)
        _clear_buffer(plaintext)
        plaintext = None
        generated_nonce = None
        ciphertext = None
        associated_data = None
        authority = None
        state = None
        nonce = None
        pkce_verifier = None
        b2d1_request_key = None
        source_buffers = None
        record = None
        key_version = None


def _unprotect_material(
    authority,
    *,
    protection_key_version,
    protection_nonce,
    protected_material,
    associated_data,
):
    result = _unprotect_material_sensitive(
        authority,
        protection_key_version=protection_key_version,
        protection_nonce=protection_nonce,
        protected_material=protected_material,
        associated_data=associated_data,
    )
    authority = None
    protection_key_version = None
    protection_nonce = None
    protected_material = None
    associated_data = None
    return _publish_sensitive_result(result)


def _unprotect_material_sensitive(
    authority,
    *,
    protection_key_version,
    protection_nonce,
    protected_material,
    associated_data,
):
    plaintext_bytes = None
    plaintext_buffer = None
    values = None
    record = None
    key = None
    retained = False
    try:
        protection_key_version = _validated_key_version(
            protection_key_version
        )
        if (
            type(protection_nonce) is not bytes
            or len(protection_nonce) != _NONCE_BYTES
            or type(protected_material) is not bytes
            or not (
                17
                <= len(protected_material)
                <= MAX_PROTECTED_CIPHERTEXT_BYTES
            )
        ):
            raise TypeError("google_oidc_protection_envelope_invalid")
        associated_data = _validated_associated_data(associated_data)
        record = _usable_authority_record(authority)
        with record.lock:
            _require_usable_record_locked(record)
            try:
                key = record.protection_keys[protection_key_version]
            except KeyError:
                raise TypeError(
                    "google_oidc_protection_key_unavailable"
                ) from None
            plaintext_bytes = _decrypt_locked(
                key,
                protection_nonce,
                protected_material,
                associated_data,
            )
        if (
            type(plaintext_bytes) is not bytes
            or not (1 <= len(plaintext_bytes) <= MAX_PROTECTED_PLAINTEXT_BYTES)
            or len(protected_material) != len(plaintext_bytes) + 16
        ):
            raise TypeError("google_oidc_protected_material_invalid")
        plaintext_buffer = bytearray(plaintext_bytes)
        plaintext_bytes = None
        values = _parse_protected_material_v1(plaintext_buffer)
        retained = True
        return values
    except (KeyboardInterrupt, SystemExit, GeneratorExit) as control:
        _detach_exception(control)
        return _ControlFlowSignal(control)
    except Exception as exc:
        _detach_exception(exc)
        return _SANITIZED_FAILURE
    finally:
        _clear_buffer(plaintext_buffer)
        if not retained and type(values) is dict:
            for value in values.values():
                _clear_buffer(value)
            values.clear()
        plaintext_bytes = None
        plaintext_buffer = None
        protection_nonce = None
        protected_material = None
        associated_data = None
        authority = None
        protection_key_version = None
        values = None
        record = None
        key = None


def _lookup_hmac_for_version(authority, lookup_key_version, payload):
    record = _usable_authority_record(authority)
    with record.lock:
        _require_usable_record_locked(record)
        try:
            key = record.lookup_keys[lookup_key_version]
        except KeyError:
            raise TypeError("google_oidc_lookup_key_unavailable") from None
        return _hmac_digest(key, payload)


def _encrypt_locked(key, nonce, plaintext, associated_data):
    aesgcm = None
    key_bytes = None
    plaintext_bytes = None
    try:
        AESGCM = _load_aesgcm()
        key_bytes = bytes(key)
        plaintext_bytes = bytes(plaintext)
        aesgcm = AESGCM(key_bytes)
        return aesgcm.encrypt(nonce, plaintext_bytes, associated_data)
    finally:
        aesgcm = None
        key_bytes = None
        plaintext_bytes = None


def _decrypt_locked(key, nonce, ciphertext, associated_data):
    aesgcm = None
    key_bytes = None
    try:
        AESGCM = _load_aesgcm()
        key_bytes = bytes(key)
        aesgcm = AESGCM(key_bytes)
        return aesgcm.decrypt(nonce, ciphertext, associated_data)
    finally:
        aesgcm = None
        key_bytes = None


def _load_aesgcm():
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    return AESGCM


def _validated_associated_data(value):
    if (
        type(value) is not bytes
        or not (1 <= len(value) <= _MAX_ASSOCIATED_DATA_BYTES)
    ):
        raise TypeError("google_oidc_associated_data_invalid")
    return bytes(value)


def _usable_authority_record(authority):
    if type(authority) is not GoogleOidcTransactionKeyAuthority:
        raise TypeError("google_oidc_transaction_key_authority_required")
    try:
        record = object.__getattribute__(
            authority,
            "_GoogleOidcTransactionKeyAuthority__record",
        )
    except AttributeError:
        raise TypeError(
            "google_oidc_transaction_key_authority_required"
        ) from None
    if type(record) is not _KeyAuthorityRecord:
        raise TypeError("google_oidc_transaction_key_authority_required")
    return record


def _require_usable_record_locked(record):
    if (
        record.capability is not _KEY_RECORD_CAPABILITY
        or record.closed
        or type(record.lookup_keys) is not dict
        or type(record.protection_keys) is not dict
        or not (1 <= len(record.lookup_keys) <= _MAX_RING_KEYS)
        or not (1 <= len(record.protection_keys) <= _MAX_RING_KEYS)
        or record.active_lookup_version not in record.lookup_keys
        or record.active_protection_version not in record.protection_keys
        or any(
            type(version) is not int
            or not (1 <= version <= MAX_KEY_VERSION)
            or type(key) is not bytearray
            or len(key) != _KEY_BYTES
            for ring in (record.lookup_keys, record.protection_keys)
            for version, key in ring.items()
        )
        or _rings_reuse_material(record.lookup_keys, record.protection_keys)
    ):
        raise TypeError("google_oidc_transaction_key_authority_unavailable")
    expected = _key_record_attestation(record)
    actual = record.attestation
    if (
        type(actual) is not tuple
        or len(actual) != len(expected)
        or any(
            type(left) is not bytes
            or type(right) is not bytes
            or not hmac.compare_digest(left, right)
            for left, right in zip(actual, expected)
        )
    ):
        raise TypeError("google_oidc_transaction_key_authority_unavailable")


def _key_record_attestation(record):
    active_binding = (
        record.active_lookup_version.to_bytes(4, "big")
        + record.active_protection_version.to_bytes(4, "big")
    )
    parts = []
    for domain, ring in (
        (_LOOKUP_ATTESTATION_DOMAIN, record.lookup_keys),
        (_PROTECTION_ATTESTATION_DOMAIN, record.protection_keys),
    ):
        for version in sorted(ring):
            parts.append(
                _hmac_digest(
                    ring[version],
                    domain + active_binding + version.to_bytes(4, "big"),
                )
            )
    return tuple(parts)


def _normalized_key_ring(value):
    entries = _key_entries(value)
    if not (1 <= len(entries) <= _MAX_RING_KEYS):
        raise TypeError("google_oidc_transaction_key_authority_invalid")
    normalized = {}
    retained = False
    try:
        for entry in entries:
            if type(entry) not in {tuple, list} or len(entry) != 2:
                raise TypeError(
                    "google_oidc_transaction_key_authority_invalid"
                )
            version, buffer = entry
            version = _validated_key_version(version)
            if (
                version in normalized
                or type(buffer) is not bytearray
                or len(buffer) != _KEY_BYTES
            ):
                raise TypeError(
                    "google_oidc_transaction_key_authority_invalid"
                )
            copied = bytearray(buffer)
            if any(
                hmac.compare_digest(copied, existing)
                for existing in normalized.values()
            ):
                _clear_buffer(copied)
                raise TypeError(
                    "google_oidc_transaction_key_authority_invalid"
                )
            normalized[version] = copied
        retained = True
        return normalized
    finally:
        if not retained:
            _clear_key_ring(normalized)


def _key_entries(value):
    if type(value) is dict:
        return tuple(value.items())
    if type(value) in {tuple, list}:
        return tuple(value)
    raise TypeError("google_oidc_transaction_key_authority_invalid")


def _collect_input_key_buffers(*rings):
    collected = []
    seen_buffers = set()
    seen_containers = set()
    examined = 0
    for ring in rings:
        if examined >= _MAX_KEY_INPUT_CLEANUP_NODES:
            break
        ring_marker = id(ring)
        if ring_marker in seen_containers:
            continue
        seen_containers.add(ring_marker)
        examined += 1
        if isinstance(ring, dict):
            try:
                candidates = iter(dict.values(ring))
                candidate_count = dict.__len__(ring)
            except Exception:
                continue
            for _index in range(candidate_count):
                if examined >= _MAX_KEY_INPUT_CLEANUP_NODES:
                    break
                try:
                    candidate = next(candidates)
                except Exception:
                    break
                examined += 1
                if (
                    type(candidate) is bytearray
                    and id(candidate) not in seen_buffers
                ):
                    seen_buffers.add(id(candidate))
                    collected.append(candidate)
            continue
        if isinstance(ring, list):
            sequence_type = list
        elif isinstance(ring, tuple):
            sequence_type = tuple
        else:
            continue
        try:
            entry_count = sequence_type.__len__(ring)
        except Exception:
            continue
        for index in range(entry_count):
            if examined >= _MAX_KEY_INPUT_CLEANUP_NODES:
                break
            try:
                entry = sequence_type.__getitem__(ring, index)
            except Exception:
                break
            examined += 1
            entry_marker = id(entry)
            if entry_marker in seen_containers:
                continue
            if isinstance(entry, list):
                entry_type = list
            elif isinstance(entry, tuple):
                entry_type = tuple
            else:
                continue
            seen_containers.add(entry_marker)
            try:
                if entry_type.__len__(entry) < 2:
                    continue
                candidate = entry_type.__getitem__(entry, 1)
            except Exception:
                continue
            if examined >= _MAX_KEY_INPUT_CLEANUP_NODES:
                break
            examined += 1
            if (
                type(candidate) is bytearray
                and id(candidate) not in seen_buffers
            ):
                seen_buffers.add(id(candidate))
                collected.append(candidate)
    return tuple(collected)


def _clear_collected_key_buffers(*rings):
    for buffer in _collect_input_key_buffers(*rings):
        _clear_buffer(buffer)


def _rings_reuse_material(left, right):
    all_entries = tuple(left.values()) + tuple(right.values())
    return any(
        hmac.compare_digest(all_entries[first], all_entries[second])
        for first in range(len(all_entries))
        for second in range(first + 1, len(all_entries))
    )


def _clear_key_ring(ring):
    if type(ring) is not dict:
        return
    for key in ring.values():
        _clear_buffer(key)


def _validated_key_version(value):
    if type(value) is not int or not (1 <= value <= MAX_KEY_VERSION):
        raise TypeError("google_oidc_transaction_key_version_invalid")
    return value


def _hmac_digest(key, payload):
    if (
        type(key) is not bytearray
        or len(key) != _KEY_BYTES
        or type(payload) is not bytes
    ):
        raise TypeError("google_oidc_transaction_protection_invalid")
    return hmac.new(bytes(key), payload, hashlib.sha256).digest()


def _protection_failure():
    return GoogleOidcTransactionProtectionError()


def _detach_exception(exc):
    try:
        exc.__traceback__ = None
        exc.__cause__ = None
        exc.__context__ = None
    except Exception:
        pass
