import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from tests.ownership_test_support import add_principal
from tests.persistent_profiles_test_support import (
    add_account_principal,
    add_development_principal,
    append_revision,
    canonical_document,
    create_profile,
    digest,
    insert_revision,
    insert_source,
    install_persistent_profiles,
    stable_id,
    timestamp,
)
from wahojobs.persistent_profile_schema import (
    MIGRATION_PATH,
    STRUCTURED_PROFILE_DENIED_KEY_FORMS,
    validate_persistent_profile_identifier,
    validate_structured_profile_key,
)


class PersistentProfilesSchemaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "profiles.sqlite"
        self.conn = install_persistent_profiles(self.path)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_development_and_account_principal_eligibility(self):
        dev = add_development_principal(self.conn, "1")
        create_profile(self.conn, dev, suffix="1")

        account = add_account_principal(self.conn, "2")
        create_profile(
            self.conn, account, suffix="2", environment="private_beta"
        )

        production_dev = add_development_principal(
            self.conn, "3", environment="production"
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self._insert_container(production_dev, "3", environment="production")
        self.conn.rollback()

        for offset, principal_type in enumerate(
            ("sample", "system", "legacy_profile"), start=4
        ):
            principal = add_principal(
                self.conn,
                suffix=str(offset),
                environment="test",
                principal_type=principal_type,
                status="active",
                claim_policy="nonclaimable"
                if principal_type != "legacy_profile"
                else "manual_approval",
                exclusive=0,
            )
            self.conn.commit()
            with self.subTest(principal_type=principal_type), self.assertRaises(
                sqlite3.IntegrityError
            ):
                self._insert_container(principal, str(offset))
            self.conn.rollback()

        with self.assertRaises(sqlite3.IntegrityError):
            self._insert_container(stable_id("prn", 99), "9")
        self.conn.rollback()

        unbound = add_principal(
            self.conn,
            suffix="10",
            environment="private_beta",
            principal_type="account_native",
            status="active",
            claim_policy="account_native",
            exclusive=1,
        )
        self.conn.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self._insert_container(unbound, "10", environment="private_beta")
        self.conn.rollback()

    def test_one_profile_per_principal_and_environment_relationship(self):
        principal = add_development_principal(self.conn, "11")
        create_profile(self.conn, principal, suffix="11")
        with self.assertRaises(sqlite3.IntegrityError):
            self._insert_container(principal, "12")
        self.conn.rollback()
        with self.assertRaises(sqlite3.IntegrityError):
            self._insert_container(principal, "13", environment="development")
        self.conn.rollback()

    def test_ids_reject_wrong_prefix_case_nonhex_zero_and_repeated_payloads(self):
        principal = add_development_principal(self.conn, "20")
        invalid_profile_ids = (
            "profile_123",
            "PRF_" + "a" * 32,
            "prf_" + "g" * 32,
            "prf_" + "0" * 32,
            "prf_" + "a" * 32,
            "prf_" + "1" * 31,
        )
        for profile_id in invalid_profile_ids:
            with self.subTest(profile_id=profile_id), self.assertRaises(
                sqlite3.IntegrityError
            ):
                self.conn.execute(
                    "INSERT INTO product_profiles "
                    "(profile_id, principal_id, environment_namespace, initial_revision_id, created_at) "
                    "VALUES (?, ?, 'test', ?, ?)",
                    (profile_id, principal, stable_id("pvr", 20), timestamp()),
                )
            self.conn.rollback()

        profile_id = stable_id("prf", 20)
        invalid_revision_ids = ("pvr_" + "0" * 32, "pvr_" + "b" * 32, "PVR_" + "1" * 32)
        for revision_id in invalid_revision_ids:
            with self.subTest(revision_id=revision_id), self.assertRaises(
                sqlite3.IntegrityError
            ):
                self.conn.execute(
                    "INSERT INTO product_profiles "
                    "(profile_id, principal_id, environment_namespace, initial_revision_id, created_at) "
                    "VALUES (?, ?, 'test', ?, ?)",
                    (profile_id, principal, revision_id, timestamp()),
                )
            self.conn.rollback()

        valid = stable_id("prf", 20)
        self.assertEqual(
            validate_persistent_profile_identifier(valid, "profile"), valid
        )
        for kind, value in (
            ("profile", "prf_" + "0" * 32),
            ("revision", "pvr_" + "a" * 32),
            ("source", "PFS_" + "1" * 32),
            ("unknown", stable_id("prf", 22)),
        ):
            with self.subTest(kind=kind, value=value), self.assertRaises(ValueError):
                validate_persistent_profile_identifier(value, kind)

    def test_initial_bundle_is_atomic_and_requires_matching_revision_and_source(self):
        principal = add_development_principal(self.conn, "21")
        profile_id = stable_id("prf", 21)
        revision_id = stable_id("pvr", 21)
        self.conn.execute("BEGIN")
        self.conn.execute(
            "INSERT INTO product_profiles VALUES (?, ?, 'test', ?, ?)",
            (profile_id, principal, revision_id, timestamp()),
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.commit()
        self.conn.rollback()
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM product_profiles").fetchone()[0], 0
        )

        create_profile(self.conn, principal, suffix="21")
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM product_profiles").fetchone()[0], 1
        )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM product_profile_revisions").fetchone()[0], 1
        )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM product_profile_sources").fetchone()[0], 1
        )

    def test_revision_insertion_seals_exact_contiguous_source_bundle(self):
        principal = add_development_principal(self.conn, "22")
        profile_id = stable_id("prf", 22)
        revision_id = stable_id("pvr", 22)
        when = timestamp()
        self.conn.execute("BEGIN")
        self._insert_container(principal, "22")
        for ordinal, suffix in ((1, 221), (2, 222)):
            insert_source(
                self.conn,
                source_id=stable_id("pfs", suffix),
                revision_id=revision_id,
                profile_id=profile_id,
                principal_id=principal,
                environment="test",
                source_content=f"confirmed source {ordinal}",
                accepted_at=when,
                ordinal=ordinal,
            )
        insert_revision(
            self.conn,
            revision_id=revision_id,
            profile_id=profile_id,
            principal_id=principal,
            environment="test",
            revision_number=1,
            previous_revision_id=None,
            revision_kind="initial",
            lifecycle_status="active",
            structured_json=canonical_document(profile_id),
            created_at=when,
            source_count=2,
        )
        self.conn.commit()
        self.assertEqual(
            self.conn.execute(
                "SELECT source_count FROM product_profile_revisions WHERE revision_id=?",
                (revision_id,),
            ).fetchone()[0],
            2,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM product_profile_sources WHERE revision_id=?",
                (revision_id,),
            ).fetchone()[0],
            2,
        )

        for same_transaction in (True, False):
            with self.subTest(same_transaction=same_transaction):
                if same_transaction:
                    other = add_development_principal(self.conn, "23")
                    other_profile = stable_id("prf", 23)
                    other_revision = stable_id("pvr", 23)
                    self.conn.execute("BEGIN")
                    self._insert_container(other, "23")
                    insert_source(
                        self.conn,
                        source_id=stable_id("pfs", 231),
                        revision_id=other_revision,
                        profile_id=other_profile,
                        principal_id=other,
                        environment="test",
                        source_content="first source",
                        accepted_at=when,
                    )
                    insert_revision(
                        self.conn,
                        revision_id=other_revision,
                        profile_id=other_profile,
                        principal_id=other,
                        environment="test",
                        revision_number=1,
                        previous_revision_id=None,
                        revision_kind="initial",
                        lifecycle_status="active",
                        structured_json=canonical_document(other_profile),
                        created_at=when,
                    )
                    target = (other_revision, other_profile, other)
                else:
                    self.conn.execute("BEGIN")
                    target = (revision_id, profile_id, principal)
                with self.assertRaises(sqlite3.IntegrityError):
                    insert_source(
                        self.conn,
                        source_id=stable_id("pfs", 232 if same_transaction else 223),
                        revision_id=target[0],
                        profile_id=target[1],
                        principal_id=target[2],
                        environment="test",
                        source_content="late source",
                        accepted_at=when,
                        ordinal=2 if same_transaction else 3,
                    )
                self.conn.rollback()

        mismatches = (
            ("declared_lower", (1, 2), 1),
            ("declared_higher", (1,), 2),
            ("missing_ordinal", (1, 3), 2),
        )
        for offset, (name, ordinals, declared) in enumerate(mismatches, start=24):
            with self.subTest(name=name):
                mismatch_principal = add_development_principal(self.conn, str(offset))
                mismatch_profile = stable_id("prf", offset)
                mismatch_revision = stable_id("pvr", offset)
                self.conn.execute("BEGIN")
                self._insert_container(mismatch_principal, str(offset))
                for source_index, ordinal in enumerate(ordinals, start=1):
                    insert_source(
                        self.conn,
                        source_id=stable_id("pfs", offset * 10 + source_index),
                        revision_id=mismatch_revision,
                        profile_id=mismatch_profile,
                        principal_id=mismatch_principal,
                        environment="test",
                        source_content=f"source {ordinal}",
                        accepted_at=when,
                        ordinal=ordinal,
                    )
                with self.assertRaises(sqlite3.IntegrityError):
                    insert_revision(
                        self.conn,
                        revision_id=mismatch_revision,
                        profile_id=mismatch_profile,
                        principal_id=mismatch_principal,
                        environment="test",
                        revision_number=1,
                        previous_revision_id=None,
                        revision_kind="initial",
                        lifecycle_status="active",
                        structured_json=canonical_document(mismatch_profile),
                        created_at=when,
                        source_count=declared,
                    )
                self.conn.rollback()
        self.assertEqual(
            self.conn.execute(
                "SELECT revision.revision_id "
                "FROM product_profile_revisions revision "
                "LEFT JOIN product_profile_sources source "
                "ON source.revision_id = revision.revision_id "
                "GROUP BY revision.revision_id "
                "HAVING revision.source_count <> COUNT(source.source_id)"
            ).fetchall(),
            [],
        )

    def test_revision_contiguity_correction_and_lifecycle(self):
        principal = add_development_principal(self.conn, "30")
        profile_id, first = create_profile(self.conn, principal, suffix="30")
        second = append_revision(
            self.conn,
            profile_id,
            principal,
            revision_number=2,
            previous_revision_id=first,
            suffix="31",
        )
        third = append_revision(
            self.conn,
            profile_id,
            principal,
            revision_number=3,
            previous_revision_id=second,
            suffix="32",
            revision_kind="correction",
            correction_of_revision_id=first,
        )
        fourth = append_revision(
            self.conn,
            profile_id,
            principal,
            revision_number=4,
            previous_revision_id=third,
            suffix="33",
            revision_kind="archive",
            lifecycle_status="archived",
        )
        fifth = append_revision(
            self.conn,
            profile_id,
            principal,
            revision_number=5,
            previous_revision_id=fourth,
            suffix="34",
            revision_kind="reactivate",
            lifecycle_status="active",
        )
        sixth = append_revision(
            self.conn,
            profile_id,
            principal,
            revision_number=6,
            previous_revision_id=fifth,
            suffix="35",
            revision_kind="deletion_request",
            lifecycle_status="deletion_requested",
        )
        current = self.conn.execute(
            "SELECT current_revision_id, current_revision_number, lifecycle_status "
            "FROM current_product_profiles WHERE profile_id=?",
            (profile_id,),
        ).fetchone()
        self.assertEqual(tuple(current), (sixth, 6, "deletion_requested"))
        with self.assertRaises(sqlite3.OperationalError):
            self.conn.execute(
                "UPDATE current_product_profiles SET lifecycle_status='active' WHERE profile_id=?",
                (profile_id,),
            )

        with self.assertRaises(sqlite3.IntegrityError):
            append_revision(
                self.conn,
                profile_id,
                principal,
                revision_number=7,
                previous_revision_id=sixth,
                suffix="36",
            )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM product_profile_revisions WHERE profile_id=?",
                (profile_id,),
            ).fetchone()[0],
            6,
        )

    def test_archived_edits_and_corrections_remain_archived_until_reactivation(self):
        principal = add_development_principal(self.conn, "37")
        profile_id, initial = create_profile(self.conn, principal, suffix="37")
        archived = append_revision(
            self.conn,
            profile_id,
            principal,
            revision_number=2,
            previous_revision_id=initial,
            suffix="371",
            revision_kind="archive",
            lifecycle_status="archived",
        )
        edited = append_revision(
            self.conn,
            profile_id,
            principal,
            revision_number=3,
            previous_revision_id=archived,
            suffix="372",
            revision_kind="edit",
            lifecycle_status="archived",
        )
        corrected = append_revision(
            self.conn,
            profile_id,
            principal,
            revision_number=4,
            previous_revision_id=edited,
            suffix="373",
            revision_kind="correction",
            lifecycle_status="archived",
            correction_of_revision_id=initial,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT lifecycle_status FROM current_product_profiles WHERE profile_id=?",
                (profile_id,),
            ).fetchone()[0],
            "archived",
        )
        reactivated = append_revision(
            self.conn,
            profile_id,
            principal,
            revision_number=5,
            previous_revision_id=corrected,
            suffix="374",
            revision_kind="reactivate",
            lifecycle_status="active",
        )
        terminal = append_revision(
            self.conn,
            profile_id,
            principal,
            revision_number=6,
            previous_revision_id=reactivated,
            suffix="375",
            revision_kind="deletion_request",
            lifecycle_status="deletion_requested",
        )
        attempts = (
            ("edit", "active", None, "376"),
            ("correction", "deletion_requested", initial, "377"),
            ("reactivate", "active", None, "378"),
        )
        for kind, lifecycle, target, suffix in attempts:
            with self.subTest(kind=kind), self.assertRaises(sqlite3.IntegrityError):
                append_revision(
                    self.conn,
                    profile_id,
                    principal,
                    revision_number=7,
                    previous_revision_id=terminal,
                    suffix=suffix,
                    revision_kind=kind,
                    lifecycle_status=lifecycle,
                    correction_of_revision_id=target,
                )

    def test_revision_gaps_wrong_previous_cross_profile_correction_and_time_are_rejected(self):
        principal = add_development_principal(self.conn, "40")
        profile_id, first = create_profile(self.conn, principal, suffix="40")
        other_principal = add_development_principal(self.conn, "41")
        other_profile, other_revision = create_profile(
            self.conn, other_principal, suffix="41"
        )
        attempts = (
            dict(revision_number=3, previous_revision_id=first, suffix="42"),
            dict(revision_number=2, previous_revision_id=other_revision, suffix="43"),
            dict(
                revision_number=2,
                previous_revision_id=first,
                suffix="44",
                revision_kind="correction",
                correction_of_revision_id=other_revision,
            ),
            dict(
                revision_number=2,
                previous_revision_id=first,
                suffix="45",
                created_at=timestamp(-1),
            ),
        )
        for values in attempts:
            with self.subTest(values=values), self.assertRaises(sqlite3.IntegrityError):
                append_revision(self.conn, profile_id, principal, **values)
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM current_product_profiles WHERE profile_id IN (?, ?)",
                (profile_id, other_profile),
            ).fetchone()[0],
            2,
        )

    def test_sources_are_bounded_typed_unicode_safe_and_immutable(self):
        principal = add_development_principal(self.conn, "50")
        content = "Português\n中文\tالعربية\r\n"
        profile_id, revision_id = create_profile(
            self.conn, principal, suffix="50", source_content=content
        )
        stored = self.conn.execute(
            "SELECT source_content FROM product_profile_sources WHERE revision_id=?",
            (revision_id,),
        ).fetchone()[0]
        self.assertEqual(stored, content)
        source_id = stable_id("pfs", 50)
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "UPDATE product_profile_sources SET source_content='changed' WHERE source_id=?",
                (source_id,),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "DELETE FROM product_profile_sources WHERE source_id=?", (source_id,)
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "UPDATE product_profile_revisions SET reason_code='changed' WHERE revision_id=?",
                (revision_id,),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "DELETE FROM product_profile_revisions WHERE revision_id=?",
                (revision_id,),
            )

        invalid_sources = (
            ("bad\x01control", "confirmed_about_you_text", "text/plain", 1),
            ("x" * 32769, "confirmed_about_you_text", "text/plain", 1),
            ("text", "resume", "text/plain", 1),
            ("text", "confirmed_about_you_text", "application/json", 1),
            ("text", "confirmed_about_you_text", "text/plain", 17),
        )
        for index, (value, source_type, source_format, ordinal) in enumerate(
            invalid_sources, start=51
        ):
            with self.subTest(index=index), self.assertRaises(sqlite3.IntegrityError):
                insert_source(
                    self.conn,
                    source_id=stable_id("pfs", index),
                    revision_id=stable_id("pvr", index),
                    profile_id=profile_id,
                    principal_id=principal,
                    environment="test",
                    source_content=value,
                    source_type=source_type,
                    source_format=source_format,
                    ordinal=ordinal,
                    accepted_at=timestamp(),
                )
            self.conn.rollback()

        self.conn.execute("BEGIN")
        insert_source(
            self.conn,
            source_id=stable_id("pfs", 57),
            revision_id=stable_id("pvr", 57),
            profile_id=profile_id,
            principal_id=principal,
            environment="test",
            source_content="deferred orphan",
            accepted_at=timestamp(),
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.commit()
        self.conn.rollback()

        other_principal = add_development_principal(self.conn, "58")
        other_profile, _ = create_profile(self.conn, other_principal, suffix="58")
        cross_revision = stable_id("pvr", 59)
        self.conn.execute("BEGIN")
        insert_source(
            self.conn,
            source_id=stable_id("pfs", 59),
            revision_id=cross_revision,
            profile_id=other_profile,
            principal_id=other_principal,
            environment="test",
            source_content="cross-profile source",
            accepted_at=timestamp(),
        )
        with self.assertRaises(sqlite3.IntegrityError):
            insert_revision(
                self.conn,
                revision_id=cross_revision,
                profile_id=profile_id,
                principal_id=principal,
                environment="test",
                revision_number=2,
                previous_revision_id=revision_id,
                revision_kind="edit",
                lifecycle_status="active",
                structured_json=canonical_document(profile_id),
                created_at=timestamp(2),
            )
        self.conn.rollback()

    def test_source_control_character_policy_and_utf8_size_boundary(self):
        principal = add_development_principal(self.conn, "590")
        profile_id = stable_id("prf", 590)
        revision_id = stable_id("pvr", 590)
        prohibited = tuple(
            code for code in range(0x20) if code not in (0x09, 0x0A, 0x0D)
        ) + tuple(range(0x7F, 0xA0))
        for codepoint in prohibited:
            self.conn.execute("BEGIN")
            self._insert_container(principal, "590")
            with self.subTest(codepoint=f"U+{codepoint:04X}"), self.assertRaises(
                sqlite3.IntegrityError
            ):
                insert_source(
                    self.conn,
                    source_id=stable_id("pfs", 600 + codepoint),
                    revision_id=revision_id,
                    profile_id=profile_id,
                    principal_id=principal,
                    environment="test",
                    source_content=f"before{chr(codepoint)}after",
                    accepted_at=timestamp(),
                )
            self.conn.rollback()

        for index, content in enumerate(
            ("tab\tvalue", "line\nvalue", "carriage\rvalue", "Português 中文 العربية 😀"),
            start=700,
        ):
            with self.subTest(content=repr(content)):
                self.conn.execute("BEGIN")
                self._insert_container(principal, "590")
                insert_source(
                    self.conn,
                    source_id=stable_id("pfs", index),
                    revision_id=revision_id,
                    profile_id=profile_id,
                    principal_id=principal,
                    environment="test",
                    source_content=content,
                    accepted_at=timestamp(),
                )
                self.conn.rollback()

        boundary_principal = add_development_principal(self.conn, "710")
        create_profile(
            self.conn,
            boundary_principal,
            suffix="710",
            source_content="😀" * 8192,
        )
        oversized_principal = add_development_principal(self.conn, "711")
        with self.assertRaises(sqlite3.IntegrityError):
            create_profile(
                self.conn,
                oversized_principal,
                suffix="711",
                source_content="😀" * 8193,
            )

    def test_structured_json_shape_limits_and_raw_identity_fields_are_rejected(self):
        invalid_documents = {
            "invalid_json": "{",
            "top_level_array": "[]",
            "wrong_schema": json.dumps(
                {"schema_version": "v2", "identity": {"profile_id": stable_id("prf", 60)}}
            ),
            "raw_text": canonical_document(
                stable_id("prf", 61), extra={"raw_input": "private text"}
            ),
            "long_scalar": canonical_document(
                stable_id("prf", 62), extra={"skills": "x" * 4097}
            ),
            "too_many_children": canonical_document(
                stable_id("prf", 63), extra={"items": list(range(257))}
            ),
            "too_many_nodes": canonical_document(
                stable_id("prf", 64),
                extra={f"k{i}": list(range(16)) for i in range(256)},
            ),
            "oversized": canonical_document(
                stable_id("prf", 65), extra={"items": ["x" * 4096] * 33}
            ),
        }
        nested = {"leaf": "value"}
        for _ in range(14):
            nested = {"child": nested}
        invalid_documents["too_deep"] = canonical_document(
            stable_id("prf", 66), extra={"nested": nested}
        )
        for index, (name, document) in enumerate(invalid_documents.items(), start=60):
            principal = add_development_principal(self.conn, str(index))
            with self.subTest(name=name), self.assertRaises(sqlite3.DatabaseError):
                create_profile(
                    self.conn,
                    principal,
                    suffix=str(index),
                    structured_json=document,
                )

    def test_structured_json_key_grammar_privacy_and_duplicates(self):
        sensitive_keys = (
            "raw_text",
            "about_you_text",
            "evidence",
            "evidence_snippet",
            "evidence_snippets",
            "cv",
            "token",
            "cookie",
            "authorization",
            "application_content",
            "account_id",
        )
        invalid_grammar = (
            "Original-Text",
            "original.text",
            "original/text",
            "original\\text",
            "original text",
            "original：text",
            "original／text",
            "original_téxt",
            "ｏｒｉｇｉｎａｌ_text",
        )
        suffix = 150
        for key in (*sensitive_keys, *invalid_grammar):
            for placement in ("root", "nested_array"):
                with self.subTest(key=key, placement=placement):
                    principal = add_development_principal(self.conn, str(suffix))
                    profile_id = stable_id("prf", suffix)
                    extra = (
                        {key: "private"}
                        if placement == "root"
                        else {"safe_items": [{key: "private"}]}
                    )
                    with self.assertRaises(sqlite3.DatabaseError):
                        create_profile(
                            self.conn,
                            principal,
                            suffix=str(suffix),
                            structured_json=canonical_document(
                                profile_id, extra=extra
                            ),
                        )
                    suffix += 1

        principal = add_development_principal(self.conn, str(suffix))
        profile_id = stable_id("prf", suffix)
        nested = {"raw_text": "private"}
        for _ in range(8):
            nested = {"safe_child": nested}
        with self.assertRaisesRegex(
            sqlite3.IntegrityError,
            "prohibited raw or identity metadata",
        ):
            create_profile(
                self.conn,
                principal,
                suffix=str(suffix),
                structured_json=canonical_document(
                    profile_id, extra={"safe_nested": nested}
                ),
            )
        suffix += 1

        principal = add_development_principal(self.conn, str(suffix))
        profile_id = stable_id("prf", suffix)
        duplicate = (
            '{"schema_version":"canonical_profile_v1",'
            '"identity":{"profile_id":"'
            + profile_id
            + '","profile_id":"'
            + profile_id
            + '"},"provenance":{}}'
        )
        with self.assertRaisesRegex(
            sqlite3.IntegrityError, "duplicate object keys"
        ):
            create_profile(
                self.conn,
                principal,
                suffix=str(suffix),
                structured_json=duplicate,
            )
        suffix += 1

        principal = add_development_principal(self.conn, str(suffix))
        profile_id = stable_id("prf", suffix)
        safe_document = canonical_document(
            profile_id,
            extra={
                "display_name": "João 👩‍💻",
                "languages": ["中文", "Português", "العربية"],
                "safe_nested": {"preferred_role": "Développeuse"},
            },
        )
        create_profile(
            self.conn,
            principal,
            suffix=str(suffix),
            structured_json=safe_document,
        )
        self.assertEqual(validate_structured_profile_key("preferred_role"), "preferred_role")
        for key in sensitive_keys:
            with self.subTest(helper_key=key), self.assertRaises(ValueError):
                validate_structured_profile_key(key)
            self.assertIn(key.replace("_", ""), STRUCTURED_PROFILE_DENIED_KEY_FORMS)
        for key in invalid_grammar:
            with self.subTest(helper_grammar=key), self.assertRaises(ValueError):
                validate_structured_profile_key(key)
        migration_sql = MIGRATION_PATH.read_text(encoding="utf-8")
        for denied_form in STRUCTURED_PROFILE_DENIED_KEY_FORMS:
            with self.subTest(sql_denied_form=denied_form):
                self.assertIn(f"'{denied_form}'", migration_sql)

    def test_hash_fingerprint_and_timestamp_formats_are_rejected(self):
        principal = add_development_principal(self.conn, "80")
        profile_id = stable_id("prf", 80)
        revision_id = stable_id("pvr", 80)
        self.conn.execute("BEGIN")
        self.conn.execute(
            "INSERT INTO product_profiles VALUES (?, ?, 'test', ?, ?)",
            (profile_id, principal, revision_id, timestamp()),
        )
        insert_source(
            self.conn,
            source_id=stable_id("pfs", 80),
            revision_id=revision_id,
            profile_id=profile_id,
            principal_id=principal,
            environment="test",
            source_content="confirmed",
            accepted_at=timestamp(),
        )
        document = canonical_document(profile_id)
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO product_profile_revisions "
                "(revision_id, profile_id, principal_id, environment_namespace, revision_number, "
                "previous_revision_id, correction_of_revision_id, revision_kind, lifecycle_status, "
                "canonical_schema_version, structured_profile_json, structured_profile_sha256, "
                "source_count, source_bundle_sha256, normalizer_version, reviewer_version, actor_type, "
                "reason_code, idempotency_key, request_fingerprint, created_at) "
                "VALUES (?, ?, ?, 'test', 1, NULL, NULL, 'initial', 'active', "
                "'canonical_profile_v1', ?, ?, 1, ?, NULL, NULL, 'development_service', "
                "'test', 'idempotency-key-80', ?, '2026-07-18')",
                (revision_id, profile_id, principal, document, "A" * 64, digest("bundle"), digest("request")),
            )
        self.conn.rollback()

    def test_profile_purge_requires_terminal_lifecycle_and_cascades_as_one_unit(self):
        principal = add_development_principal(self.conn, "90")
        profile_id, first = create_profile(self.conn, principal, suffix="90")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("DELETE FROM product_profiles WHERE profile_id=?", (profile_id,))
        self.conn.rollback()
        terminal = append_revision(
            self.conn,
            profile_id,
            principal,
            revision_number=2,
            previous_revision_id=first,
            suffix="91",
            revision_kind="deletion_request",
            lifecycle_status="deletion_requested",
        )
        self.conn.execute("DELETE FROM product_profiles WHERE profile_id=?", (profile_id,))
        self.conn.commit()
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM product_profiles").fetchone()[0], 0
        )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM product_profile_revisions").fetchone()[0], 0
        )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM product_profile_sources").fetchone()[0], 0
        )
        self.assertIsNotNone(terminal)

    def _insert_container(self, principal_id, suffix, *, environment="test"):
        self.conn.execute(
            "INSERT INTO product_profiles "
            "(profile_id, principal_id, environment_namespace, initial_revision_id, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                stable_id("prf", suffix),
                principal_id,
                environment,
                stable_id("pvr", suffix),
                timestamp(),
            ),
        )


if __name__ == "__main__":
    unittest.main()
