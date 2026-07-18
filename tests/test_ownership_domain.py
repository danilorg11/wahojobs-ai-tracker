import dataclasses
import importlib
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from wahojobs import ownership


class OwnershipDomainTests(unittest.TestCase):
    def test_random_identifiers_have_128_random_bits_and_stable_prefixes(self):
        generated = {
            ownership.new_principal_id(): r"^prn_[0-9a-f]{32}$",
            ownership.new_alias_id(): r"^loa_[0-9a-f]{32}$",
            ownership.new_binding_id(): r"^pab_[0-9a-f]{32}$",
            ownership.new_binding_event_id(): r"^obe_[0-9a-f]{32}$",
        }
        self.assertEqual(len(generated), 4)
        for value, pattern in generated.items():
            self.assertRegex(value, pattern)

    def test_metadata_is_deterministic_bounded_and_privacy_safe(self):
        validated, encoded = ownership.canonical_metadata(
            {"review": {"approved": True}, "tags": ["manual", "dormant"]}
        )
        self.assertEqual(validated["review"]["approved"], True)
        self.assertEqual(
            encoded,
            '{"review":{"approved":true},"tags":["manual","dormant"]}',
        )
        self.assertEqual(ownership.validate_metadata_document(encoded), validated)
        for metadata in (
            {"email": "person@example.test"},
            {"session_token": "secret"},
            {"resume": "raw application content"},
            {"nested": {"provider_subject": "private"}},
            {"raw_application_content": "private"},
            {"SQL Query": "select private"},
        ):
            with self.assertRaises(ownership.OwnershipValidationError):
                ownership.canonical_metadata(metadata)

    def test_alias_validation_preserves_exact_historical_value(self):
        self.assertEqual(ownership.validate_legacy_alias("Mixed_Case-Owner"), "Mixed_Case-Owner")
        for value in ("", " owner", "owner ", "owner\nvalue", "x" * 513):
            with self.assertRaises(ownership.OwnershipValidationError):
                ownership.validate_legacy_alias(value)

    def test_public_models_do_not_expose_raw_alias_or_account_identity(self):
        alias_fields = {field.name for field in dataclasses.fields(ownership.PublicLegacyOwnerAlias)}
        binding_fields = {
            field.name for field in dataclasses.fields(ownership.PublicPrincipalAccountBinding)
        }
        self.assertNotIn("alias_value", alias_fields)
        self.assertNotIn("user_id", binding_fields)
        self.assertNotIn("value_fingerprint", alias_fields)
        self.assertIn("account_reference", binding_fields)

    def test_degenerate_identifiers_are_rejected(self):
        for validator, prefix in (
            (ownership.validate_principal_id, "prn"),
            (ownership.validate_alias_id, "loa"),
            (ownership.validate_binding_id, "pab"),
            (ownership.validate_binding_event_id, "obe"),
        ):
            for character in "0123456789abcdef":
                with self.subTest(prefix=prefix, character=character):
                    with self.assertRaises(ownership.OwnershipValidationError):
                        validator(f"{prefix}_{character * 32}")

    def test_import_has_no_file_database_network_or_environment_side_effect(self):
        with tempfile.TemporaryDirectory() as tmp:
            before_environment = dict(os.environ)
            sys.modules.pop("wahojobs.ownership", None)
            imported = importlib.import_module("wahojobs.ownership")
            self.assertEqual(dict(os.environ), before_environment)
            self.assertEqual(imported.MIGRATION_VERSION, "003_product_principals")
            env = dict(os.environ)
            root = str(Path(__file__).resolve().parents[1])
            env["PYTHONPATH"] = root + os.pathsep + env.get("PYTHONPATH", "")
            result = subprocess.run(
                [sys.executable, "-B", "-c", "import wahojobs.ownership; print('imported')"],
                cwd=tmp,
                env=env,
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "imported")
            self.assertEqual(list(Path(tmp).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
