import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from tests.persistent_profile_canonical_v2_test_support import (
    append_v2_revision,
    canonical_v2_document,
    create_v2_profile,
    insert_revision_v2,
    insert_source_v2,
    lifecycle_source_content,
    install_canonical_v2_profiles,
)
from tests.persistent_profiles_test_support import (
    add_development_principal,
    digest,
    stable_id,
    timestamp,
)


class PersistentProfileCanonicalV2SchemaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "v2.sqlite"
        self.conn = install_canonical_v2_profiles(self.path)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_v2_envelope_accepted_and_v1_or_identity_mismatch_rejected(self):
        principal = add_development_principal(self.conn, "1")
        profile_id, _ = create_v2_profile(self.conn, principal, suffix="1")
        stored = self.conn.execute(
            "SELECT canonical_schema_version FROM current_product_profiles WHERE profile_id=?",
            (profile_id,),
        ).fetchone()[0]
        self.assertEqual(stored, "canonical_profile_v2")

        for suffix, version, document in (
            ("2", "canonical_profile_v1", canonical_v2_document(stable_id("prf", 2)).replace("canonical_profile_v2", "canonical_profile_v1")),
            ("3", "canonical_profile_v2", canonical_v2_document(stable_id("prf", 999))),
            ("4", "canonical_profile_v2", canonical_v2_document(stable_id("prf", 4), extra={"raw_input": "private"})),
        ):
            test_principal = add_development_principal(self.conn, suffix)
            profile = stable_id("prf", suffix)
            revision = stable_id("pvr", suffix)
            self.conn.execute("BEGIN")
            self.conn.execute(
                "INSERT INTO product_profiles VALUES (?, ?, 'test', ?, ?)",
                (profile, test_principal, revision, timestamp()),
            )
            insert_source_v2(
                self.conn,
                source_id=stable_id("pfs", suffix),
                revision_id=revision,
                profile_id=profile,
                principal_id=test_principal,
                environment="test",
                source_content="confirmed",
                accepted_at=timestamp(),
            )
            with self.subTest(suffix=suffix), self.assertRaises(sqlite3.DatabaseError):
                insert_revision_v2(
                    self.conn,
                    revision_id=revision,
                    profile_id=profile,
                    principal_id=test_principal,
                    environment="test",
                    revision_number=1,
                    previous_revision_id=None,
                    revision_kind="initial",
                    lifecycle_status="active",
                    canonical_schema_version=version,
                    structured_json=document,
                    created_at=timestamp(),
                )
            self.conn.rollback()

    def test_required_v2_envelope_fields_reject_missing_null_and_wrong_types(self):
        cases = (
            ("empty_object", lambda profile_id: {}),
            ("top_level_null", lambda profile_id: None),
            ("top_level_string", lambda profile_id: "canonical_profile_v2"),
            ("top_level_array", lambda profile_id: []),
            ("top_level_number", lambda profile_id: 2),
            ("top_level_boolean", lambda profile_id: True),
            ("missing_schema_version", lambda profile_id: {"identity": {"profile_id": profile_id}}),
            ("null_schema_version", lambda profile_id: {"schema_version": None, "identity": {"profile_id": profile_id}}),
            ("number_schema_version", lambda profile_id: {"schema_version": 2, "identity": {"profile_id": profile_id}}),
            ("boolean_schema_version", lambda profile_id: {"schema_version": True, "identity": {"profile_id": profile_id}}),
            ("array_schema_version", lambda profile_id: {"schema_version": [], "identity": {"profile_id": profile_id}}),
            ("object_schema_version", lambda profile_id: {"schema_version": {}, "identity": {"profile_id": profile_id}}),
            ("empty_schema_version", lambda profile_id: {"schema_version": "", "identity": {"profile_id": profile_id}}),
            ("missing_identity", lambda profile_id: {"schema_version": "canonical_profile_v2"}),
            ("null_identity", lambda profile_id: {"schema_version": "canonical_profile_v2", "identity": None}),
            ("string_identity", lambda profile_id: {"schema_version": "canonical_profile_v2", "identity": profile_id}),
            ("array_identity", lambda profile_id: {"schema_version": "canonical_profile_v2", "identity": []}),
            ("number_identity", lambda profile_id: {"schema_version": "canonical_profile_v2", "identity": 2}),
            ("boolean_identity", lambda profile_id: {"schema_version": "canonical_profile_v2", "identity": False}),
            ("missing_profile_id", lambda profile_id: {"schema_version": "canonical_profile_v2", "identity": {}}),
            ("null_profile_id", lambda profile_id: {"schema_version": "canonical_profile_v2", "identity": {"profile_id": None}}),
            ("number_profile_id", lambda profile_id: {"schema_version": "canonical_profile_v2", "identity": {"profile_id": 2}}),
            ("boolean_profile_id", lambda profile_id: {"schema_version": "canonical_profile_v2", "identity": {"profile_id": True}}),
            ("array_profile_id", lambda profile_id: {"schema_version": "canonical_profile_v2", "identity": {"profile_id": []}}),
            ("object_profile_id", lambda profile_id: {"schema_version": "canonical_profile_v2", "identity": {"profile_id": {}}}),
            ("empty_profile_id", lambda profile_id: {"schema_version": "canonical_profile_v2", "identity": {"profile_id": ""}}),
            ("mismatched_profile_id", lambda profile_id: {"schema_version": "canonical_profile_v2", "identity": {"profile_id": stable_id("prf", 9999)}}),
        )
        for index, (name, build_document) in enumerate(cases, start=100):
            with self.subTest(name=name):
                suffix = str(index)
                principal_id = add_development_principal(self.conn, suffix)
                profile_id = stable_id("prf", suffix)
                revision_id = stable_id("pvr", suffix)
                document = json.dumps(
                    build_document(profile_id),
                    sort_keys=True,
                    separators=(",", ":"),
                )
                self.conn.execute("BEGIN")
                self.conn.execute(
                    "INSERT INTO product_profiles VALUES (?, ?, 'test', ?, ?)",
                    (profile_id, principal_id, revision_id, timestamp()),
                )
                insert_source_v2(
                    self.conn,
                    source_id=stable_id("pfs", suffix),
                    revision_id=revision_id,
                    profile_id=profile_id,
                    principal_id=principal_id,
                    environment="test",
                    source_content="confirmed",
                    accepted_at=timestamp(),
                )
                with self.assertRaises(sqlite3.DatabaseError):
                    insert_revision_v2(
                        self.conn,
                        revision_id=revision_id,
                        profile_id=profile_id,
                        principal_id=principal_id,
                        environment="test",
                        revision_number=1,
                        previous_revision_id=None,
                        revision_kind="initial",
                        lifecycle_status="active",
                        structured_json=document,
                        created_at=timestamp(),
                    )
                self.conn.rollback()

        principal_id = add_development_principal(self.conn, "200")
        profile_id, _ = create_v2_profile(self.conn, principal_id, suffix="200")
        self.assertEqual(
            self.conn.execute(
                "SELECT json_extract(structured_profile_json, '$.identity.profile_id') "
                "FROM current_product_profiles WHERE profile_id=?",
                (profile_id,),
            ).fetchone()[0],
            profile_id,
        )

    def test_exact_lifecycle_actions_and_current_view(self):
        principal = add_development_principal(self.conn, "10")
        profile_id, initial = create_v2_profile(self.conn, principal, suffix="10")
        archived = append_v2_revision(
            self.conn,
            profile_id,
            principal,
            revision_number=2,
            previous_revision_id=initial,
            suffix="11",
            revision_kind="archive",
            lifecycle_status="archived",
        )
        reactivated = append_v2_revision(
            self.conn,
            profile_id,
            principal,
            revision_number=3,
            previous_revision_id=archived,
            suffix="12",
            revision_kind="reactivate",
            lifecycle_status="active",
        )
        terminal = append_v2_revision(
            self.conn,
            profile_id,
            principal,
            revision_number=4,
            previous_revision_id=reactivated,
            suffix="13",
            revision_kind="deletion_request",
            lifecycle_status="deletion_requested",
        )
        current = self.conn.execute(
            "SELECT current_revision_id,current_revision_number,lifecycle_status "
            "FROM current_product_profiles WHERE profile_id=?",
            (profile_id,),
        ).fetchone()
        self.assertEqual(tuple(current), (terminal, 4, "deletion_requested"))
        rows = self.conn.execute(
            "SELECT revision.revision_kind,source.source_content,source.source_type,"
            "source.source_format,source.source_schema_version "
            "FROM product_profile_revisions revision JOIN product_profile_sources source "
            "ON source.revision_id=revision.revision_id WHERE revision.revision_number>1 "
            "ORDER BY revision.revision_number"
        ).fetchall()
        self.assertEqual(
            [tuple(row) for row in rows],
            [
                (action, lifecycle_source_content(action), "confirmed_lifecycle_action", "application/json", "confirmed_lifecycle_action_v1")
                for action in ("archive", "reactivate", "deletion_request")
            ],
        )
        with self.assertRaises(sqlite3.IntegrityError):
            append_v2_revision(
                self.conn,
                profile_id,
                principal,
                revision_number=5,
                previous_revision_id=terminal,
                suffix="14",
            )

    def test_lifecycle_source_exact_bytes_format_schema_and_action(self):
        principal = add_development_principal(self.conn, "20")
        profile_id, initial = create_v2_profile(self.conn, principal, suffix="20")
        invalid = (
            (json.dumps({"schema_version": "confirmed_lifecycle_action_v1", "action": "archive"}), "application/json", "confirmed_lifecycle_action_v1"),
            ('{"action":"archive","extra":true,"schema_version":"confirmed_lifecycle_action_v1"}', "application/json", "confirmed_lifecycle_action_v1"),
            (lifecycle_source_content("unsupported"), "application/json", "confirmed_lifecycle_action_v1"),
            (lifecycle_source_content("archive"), "text/plain", "confirmed_lifecycle_action_v1"),
            (lifecycle_source_content("archive"), "application/json", "wrong_v1"),
            ("free text", "application/json", "confirmed_lifecycle_action_v1"),
        )
        for index, (content, source_format, schema) in enumerate(invalid, start=21):
            with self.subTest(index=index), self.assertRaises(sqlite3.DatabaseError):
                insert_source_v2(
                    self.conn,
                    source_id=stable_id("pfs", index),
                    revision_id=stable_id("pvr", index),
                    profile_id=profile_id,
                    principal_id=principal,
                    environment="test",
                    source_content=content,
                    source_type="confirmed_lifecycle_action",
                    source_format=source_format,
                    source_schema_version=schema,
                    accepted_at=timestamp(2),
                )
            self.conn.rollback()
        self.assertIsNotNone(initial)

    def test_lifecycle_revision_requires_matching_source_and_unchanged_structure(self):
        attempts = (
            {"name": "wrong_action", "revision_kind": "archive", "lifecycle_status": "archived", "source_content": lifecycle_source_content("reactivate")},
            {"name": "ordinary_source", "revision_kind": "archive", "lifecycle_status": "archived", "source_type": "user_confirmed_correction", "source_content": "{}"},
            {"name": "changed_json", "revision_kind": "archive", "lifecycle_status": "archived", "structured_json": "changed"},
            {"name": "changed_hash", "revision_kind": "archive", "lifecycle_status": "archived", "structured_hash": digest("different")},
            {"name": "wrong_count", "revision_kind": "archive", "lifecycle_status": "archived", "source_count": 2},
            {"name": "wrong_ordinal", "revision_kind": "archive", "lifecycle_status": "archived", "ordinal": 2},
            {"name": "correction_target", "revision_kind": "archive", "lifecycle_status": "archived", "correction": True},
            {"name": "wrong_lifecycle", "revision_kind": "archive", "lifecycle_status": "active"},
            {"name": "lifecycle_on_edit", "revision_kind": "edit", "lifecycle_status": "active", "source_type": "confirmed_lifecycle_action", "source_content": lifecycle_source_content("archive")},
        )
        for index, attempt in enumerate(attempts, start=30):
            principal = add_development_principal(self.conn, str(index))
            profile_id, initial = create_v2_profile(self.conn, principal, suffix=str(index))
            values = dict(attempt)
            values.pop("name")
            if values.get("structured_json") == "changed":
                values["structured_json"] = canonical_v2_document(profile_id, extra={"skills": ["new"]})
            if values.pop("correction", False):
                values["correction_of_revision_id"] = initial
            with self.subTest(name=attempt["name"]), self.assertRaises(sqlite3.DatabaseError):
                append_v2_revision(
                    self.conn,
                    profile_id,
                    principal,
                    revision_number=2,
                    previous_revision_id=initial,
                    suffix=str(index + 100),
                    **values,
                )

    def test_ordinary_edit_correction_archive_edit_and_controlled_purge(self):
        principal = add_development_principal(self.conn, "50")
        profile_id, initial = create_v2_profile(self.conn, principal, suffix="50")
        edit = append_v2_revision(
            self.conn, profile_id, principal, revision_number=2,
            previous_revision_id=initial, suffix="51"
        )
        correction = append_v2_revision(
            self.conn, profile_id, principal, revision_number=3,
            previous_revision_id=edit, suffix="52", revision_kind="correction",
            correction_of_revision_id=initial
        )
        archived = append_v2_revision(
            self.conn, profile_id, principal, revision_number=4,
            previous_revision_id=correction, suffix="53", revision_kind="archive",
            lifecycle_status="archived"
        )
        archived_edit = append_v2_revision(
            self.conn, profile_id, principal, revision_number=5,
            previous_revision_id=archived, suffix="54", lifecycle_status="archived"
        )
        terminal = append_v2_revision(
            self.conn, profile_id, principal, revision_number=6,
            previous_revision_id=archived_edit, suffix="55",
            revision_kind="deletion_request", lifecycle_status="deletion_requested"
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("DELETE FROM product_profile_sources WHERE profile_id=?", (profile_id,))
        self.conn.rollback()
        self.conn.execute("DELETE FROM product_profiles WHERE profile_id=?", (profile_id,))
        self.conn.commit()
        self.assertEqual(
            tuple(self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in ("product_profiles", "product_profile_revisions", "product_profile_sources")),
            (0, 0, 0),
        )
        self.assertIsNotNone(terminal)


if __name__ == "__main__":
    unittest.main()
