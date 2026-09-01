import copy
import unittest

from scripts.semantic_packet_bundle_identity import (
    BundleIdentityError,
    build_explicit_packet_identity,
    classify_legacy_packet_digest,
    independent_authenticated_packet_sha256,
    independent_canonical_sha256,
    validate_explicit_packet_identity,
)


class SemanticPacketBundleIdentityTests(unittest.TestCase):
    def packet(self):
        packet = {
            "packet_version": "test_packet_v1",
            "payload": {"language": "Portuguese", "required": False},
        }
        packet["packet_sha256"] = independent_authenticated_packet_sha256(packet)
        return packet

    def test_legacy_digest_is_classified_as_envelope_not_authenticator(self):
        packet = self.packet()
        artifact_bytes = b'{"artifact":"immutable"}\n'
        authenticated = packet["packet_sha256"]
        envelope = independent_canonical_sha256(packet)

        self.assertNotEqual(authenticated, envelope)
        self.assertEqual(
            classify_legacy_packet_digest(packet, artifact_bytes, envelope),
            "canonical_packet_envelope_including_authenticator",
        )

    def test_corrected_identity_uses_explicit_non_ambiguous_digest_fields(self):
        packet = self.packet()
        artifact_bytes = b'{"artifact":"immutable"}\n'
        source_packet_sha256 = "a" * 64
        legacy_digest = independent_canonical_sha256(packet)
        identity = build_explicit_packet_identity(
            packet=packet,
            artifact_bytes=artifact_bytes,
            source_packet_sha256=source_packet_sha256,
            legacy_manifest_packet_sha256=legacy_digest,
        )

        self.assertNotIn("packet_sha256", identity)
        self.assertEqual(
            identity["authenticated_packet_sha256"], packet["packet_sha256"]
        )
        self.assertEqual(
            identity["canonical_packet_envelope_sha256"], legacy_digest
        )
        validate_explicit_packet_identity(
            identity,
            packet=packet,
            artifact_bytes=artifact_bytes,
            source_packet_sha256=source_packet_sha256,
        )

    def test_explicit_identity_rejects_artifact_digest_staleness(self):
        packet = self.packet()
        artifact_bytes = b'{"artifact":"immutable"}\n'
        source_packet_sha256 = "b" * 64
        identity = build_explicit_packet_identity(
            packet=packet,
            artifact_bytes=artifact_bytes,
            source_packet_sha256=source_packet_sha256,
            legacy_manifest_packet_sha256=independent_canonical_sha256(packet),
        )
        tampered = copy.deepcopy(identity)
        tampered["artifact_file_sha256"] = "0" * 64

        with self.assertRaisesRegex(BundleIdentityError, "artifact_file_digest_mismatch"):
            validate_explicit_packet_identity(
                tampered,
                packet=packet,
                artifact_bytes=artifact_bytes,
                source_packet_sha256=source_packet_sha256,
            )


if __name__ == "__main__":
    unittest.main()
