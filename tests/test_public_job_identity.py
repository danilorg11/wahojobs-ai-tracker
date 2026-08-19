from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import unittest

from wahojobs import public_job_identity as identity


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "wahojobs" / "db" / "schema.sql"
MIGRATION = ROOT / "wahojobs" / "db" / "migrations" / "009_public_job_identity.sql"
NOW = datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc)
LATER = datetime(2026, 8, 19, 13, 0, tzinfo=timezone.utc)


class EntropySequence:
    def __init__(self, *values):
        self.values = list(values)

    def __call__(self, size):
        if size != 16:
            raise AssertionError(f"expected a 16-byte request, received {size}")
        if not self.values:
            raise AssertionError("test entropy sequence was exhausted")
        return bytes.fromhex(self.values.pop(0))


def allocator(*hex_values):
    return identity.PublicJobIdAllocator(
        "test-only-authority",
        EntropySequence(*hex_values),
    )


def install_database():
    connection = sqlite3.connect(":memory:")
    connection.executescript(SCHEMA.read_text(encoding="utf-8"))
    connection.executescript(MIGRATION.read_text(encoding="utf-8"))
    seed_canonical_opportunities(connection)
    return connection


def seed_canonical_opportunities(connection, *, offset=0):
    company_id = 1 + offset
    connection.execute(
        "INSERT INTO companies "
        "(id, name, slug, careers_url, source_tier, inventory_model, market_count_policy) "
        "VALUES (?, 'Acme AI', 'acme-ai', 'https://careers.example.test/acme', "
        "'core', 'live_feed', 'count_live')",
        (company_id,),
    )
    for canonical_id, key, title in (
        (101 + offset, "evaluation-engineer", "Evaluation Engineer"),
        (102 + offset, "research-engineer", "Research Engineer"),
        (103 + offset, "safety-engineer", "Safety Engineer"),
    ):
        connection.execute(
            "INSERT INTO canonical_opportunities "
            "(id, company_id, canonical_key, canonical_title, normalized_title, "
            "source_category, first_seen_at, last_seen_at, is_active, variant_count) "
            "VALUES (?, ?, ?, ?, lower(?), 'Engineering', ?, ?, 1, 1)",
            (
                canonical_id,
                company_id,
                key,
                title,
                title,
                NOW.isoformat(),
                NOW.isoformat(),
            ),
        )


class PublicJobIdentityTests(unittest.TestCase):
    def setUp(self):
        self.connection = install_database()

    def tearDown(self):
        self.connection.close()

    def allocate(
        self,
        canonical_id=101,
        *,
        entropy="11111111111111111111111111111111",
        primary_path=None,
        title="Evaluation Engineer",
    ):
        return identity.allocate_public_job(
            self.connection,
            allocator=allocator(entropy),
            company_slug="acme-ai",
            canonical_title=title,
            canonical_opportunity_id=canonical_id,
            primary_path=primary_path,
            now=NOW,
        )

    def test_migration_creates_only_empty_dormant_authorities(self):
        counts = {
            table: self.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "public_job_identities",
                "public_job_paths",
                "public_job_bindings",
            )
        }
        self.assertEqual(
            counts,
            {
                "public_job_identities": 0,
                "public_job_paths": 0,
                "public_job_bindings": 0,
            },
        )
        self.assertEqual(self.connection.execute("PRAGMA foreign_key_check").fetchall(), [])
        for table, column in (
            ("public_job_identities", "public_job_id"),
            ("public_job_paths", "path"),
            ("public_job_bindings", "public_job_id"),
        ):
            columns = {
                row[1]: row[3]
                for row in self.connection.execute(f"PRAGMA table_info({table})")
            }
            self.assertEqual(columns[column], 1, f"{table}.{column} must be NOT NULL")
        base_only = sqlite3.connect(":memory:")
        try:
            base_only.executescript(SCHEMA.read_text(encoding="utf-8"))
            self.assertEqual(
                base_only.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'table' AND name LIKE 'public_job_%'"
                ).fetchall(),
                [],
            )
        finally:
            base_only.close()

    def test_allocator_and_frozen_new_path_contract(self):
        public_job_id = identity.generate_public_job_id(
            lambda size: bytes(range(size))
        )
        self.assertEqual(public_job_id, "j000102030405060708090a0b0c0d0e0f")
        self.assertEqual(
            identity.frozen_job_slug("Ácme AI", "Senior Café Engineer"),
            "acme-ai-senior-cafe-engineer",
        )
        path = identity.new_public_job_path(
            "Ácme AI", "Senior Café Engineer", public_job_id
        )
        self.assertEqual(
            path,
            "/job/acme-ai-senior-cafe-engineer-j000102030405060708090a0b0c0d0e0f",
        )
        self.assertLess(len(path.encode("ascii")), 120)
        slug = identity.frozen_job_slug(
            "acme-ai",
            "one two three four five six seven eight nine ten eleven twelve "
            "thirteen fourteen fifteen sixteen seventeen",
        )
        self.assertLessEqual(len(slug), 80)
        self.assertFalse(slug.endswith("-"))
        with self.assertRaises(identity.InvalidPublicJobIdentity):
            identity.generate_public_job_id(lambda _size: b"too short")

    def test_allocation_is_opaque_atomic_and_path_is_frozen(self):
        allocation = self.allocate()
        self.assertEqual(
            allocation.public_job_id,
            "j11111111111111111111111111111111",
        )
        self.assertEqual(allocation.binding_version, 1)
        self.assertEqual(
            identity.resolve_public_job_path(
                self.connection, allocation.primary_path
            ),
            identity.PublicJobRouteDecision(
                kind="serve",
                public_job_id=allocation.public_job_id,
                requested_path=allocation.primary_path,
                primary_path=allocation.primary_path,
                canonical_opportunity_id=101,
            ),
        )
        self.connection.execute(
            "UPDATE canonical_opportunities SET canonical_title = ?, canonical_key = ? "
            "WHERE id = 101",
            ("Renamed Representative Title", "renamed-representative-title"),
        )
        self.assertEqual(
            identity.primary_public_job_path_for_canonical(self.connection, 101),
            allocation.primary_path,
        )
        self.assertNotIn("101", allocation.public_job_id)
        self.assertNotIn("evaluation-engineer", allocation.public_job_id)

    def test_id_collision_retries_without_recycling_or_partial_rows(self):
        first = self.allocate()
        second = identity.allocate_public_job(
            self.connection,
            allocator=allocator(
                "11111111111111111111111111111111",
                "22222222222222222222222222222222",
            ),
            company_slug="acme-ai",
            canonical_title="Research Engineer",
            canonical_opportunity_id=102,
            now=LATER,
        )
        self.assertEqual(first.public_job_id, "j" + "11" * 16)
        self.assertEqual(second.public_job_id, "j" + "22" * 16)
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM public_job_identities"
            ).fetchone()[0],
            2,
        )

    def test_exact_legacy_primary_aliases_and_case_collisions(self):
        primary = "/job/remotasks-online-tasker-1"
        allocation = self.allocate(primary_path=primary)
        alias = "/job/Remotasks-Tasker-Archive-Page/"
        identity.register_public_job_alias(
            self.connection, allocation.public_job_id, alias, now=LATER
        )
        self.assertEqual(
            identity.resolve_public_job_path(self.connection, primary).kind,
            "serve",
        )
        alias_decision = identity.resolve_public_job_path(self.connection, alias)
        self.assertEqual(alias_decision.kind, "redirect")
        self.assertEqual(alias_decision.location, primary)
        case_decision = identity.resolve_public_job_path(
            self.connection, "/job/REMOTASKS-ONLINE-TASKER-1"
        )
        self.assertEqual(case_decision.kind, "redirect")
        self.assertEqual(case_decision.location, primary)
        self.assertIsNone(
            identity.resolve_public_job_path(
                self.connection,
                "/job/invented-readable-slug-" + allocation.public_job_id,
            )
        )
        other = self.allocate(102, entropy="22" * 16)
        with self.assertRaises(sqlite3.IntegrityError):
            identity.register_public_job_alias(
                self.connection,
                other.public_job_id,
                "/job/REMOTASKS-ONLINE-TASKER-1",
                now=LATER,
            )

    def test_issued_identity_and_paths_cannot_be_updated_deleted_or_reused(self):
        allocation = self.allocate(primary_path="/job/legacy-owner")
        for statement, parameters in (
            (
                "UPDATE public_job_identities SET public_job_id = ? "
                "WHERE public_job_id = ?",
                ("j" + "aa" * 16, allocation.public_job_id),
            ),
            (
                "UPDATE public_job_paths SET path_role = 'alias' WHERE path = ?",
                (allocation.primary_path,),
            ),
            (
                "UPDATE public_job_paths SET public_job_id = ? WHERE path = ?",
                ("j" + "aa" * 16, allocation.primary_path),
            ),
            ("DELETE FROM public_job_paths WHERE path = ?", (allocation.primary_path,)),
            (
                "DELETE FROM public_job_identities WHERE public_job_id = ?",
                (allocation.public_job_id,),
            ),
        ):
            with self.subTest(statement=statement):
                with self.assertRaises(sqlite3.IntegrityError):
                    self.connection.execute(statement, parameters)
        with self.assertRaises(identity.PublicJobIdCollisionExhausted):
            identity.allocate_public_job(
                self.connection,
                allocator=allocator("11" * 16),
                company_slug="acme-ai",
                canonical_title="Research Engineer",
                canonical_opportunity_id=102,
                now=LATER,
                collision_retries=1,
            )

    def test_insert_or_replace_cannot_reassign_issued_identity_or_path(self):
        self.assertEqual(
            self.connection.execute("PRAGMA recursive_triggers").fetchone()[0],
            0,
        )
        first = self.allocate(101, entropy="11" * 16, primary_path="/job/owner")
        second = self.allocate(102, entropy="22" * 16)
        identity_before = self.connection.execute(
            "SELECT disposition, redirect_target_public_job_id, created_at, updated_at "
            "FROM public_job_identities WHERE public_job_id = ?",
            (first.public_job_id,),
        ).fetchone()
        path_before = self.connection.execute(
            "SELECT normalized_path, public_job_id, path_role, created_at "
            "FROM public_job_paths WHERE path = ?",
            (first.primary_path,),
        ).fetchone()

        with self.assertRaisesRegex(
            sqlite3.IntegrityError,
            "issued public job identity cannot be replaced",
        ):
            self.connection.execute(
                "INSERT OR REPLACE INTO public_job_identities "
                "(public_job_id, disposition, redirect_target_public_job_id, "
                "created_at, updated_at) VALUES (?, 'gone', NULL, ?, ?)",
                (first.public_job_id, NOW.isoformat(), LATER.isoformat()),
            )
        with self.assertRaisesRegex(
            sqlite3.IntegrityError,
            "issued public job path cannot be replaced",
        ):
            self.connection.execute(
                "INSERT OR REPLACE INTO public_job_paths "
                "(path, normalized_path, public_job_id, path_role, created_at) "
                "VALUES (?, ?, ?, 'alias', ?)",
                (
                    first.primary_path,
                    first.primary_path.lower(),
                    second.public_job_id,
                    LATER.isoformat(),
                ),
            )
        with self.assertRaisesRegex(
            sqlite3.IntegrityError,
            "issued public job path cannot be replaced",
        ):
            self.connection.execute(
                "INSERT OR REPLACE INTO public_job_paths "
                "(path, normalized_path, public_job_id, path_role, created_at) "
                "VALUES (?, ?, ?, 'alias', ?)",
                (
                    "/job/OWNER",
                    first.primary_path.lower(),
                    second.public_job_id,
                    LATER.isoformat(),
                ),
            )

        self.assertEqual(
            self.connection.execute(
                "SELECT disposition, redirect_target_public_job_id, created_at, "
                "updated_at FROM public_job_identities WHERE public_job_id = ?",
                (first.public_job_id,),
            ).fetchone(),
            identity_before,
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT normalized_path, public_job_id, path_role, created_at "
                "FROM public_job_paths WHERE path = ?",
                (first.primary_path,),
            ).fetchone(),
            path_before,
        )

    def test_portable_registry_import_uses_a_different_local_binding(self):
        allocation = self.allocate(primary_path="/job/proven-legacy-owner")
        payload = identity.export_public_job_registry(self.connection)
        self.assertNotIn("bindings", payload)
        imported = sqlite3.connect(":memory:")
        try:
            imported.executescript(SCHEMA.read_text(encoding="utf-8"))
            imported.executescript(MIGRATION.read_text(encoding="utf-8"))
            seed_canonical_opportunities(imported, offset=900)
            identity.import_public_job_registry(imported, payload)
            unbound = identity.resolve_public_job_path(
                imported, allocation.primary_path
            )
            self.assertEqual(unbound.kind, "unbound")
            self.assertIn(
                "serving_identity_unbound",
                {item.code for item in identity.reconcile_public_job_identity(imported)},
            )
            identity.bind_imported_public_job(
                imported,
                allocation.public_job_id,
                1001,
                now=LATER,
            )
            resolved = identity.resolve_public_job_path(
                imported, allocation.primary_path
            )
            self.assertEqual(resolved.kind, "serve")
            self.assertEqual(resolved.canonical_opportunity_id, 1001)
            self.assertEqual(
                identity.export_public_job_registry(imported),
                payload,
            )
            identity.assert_public_job_identity_consistent(imported)
        finally:
            imported.close()

    def test_binding_changes_are_explicit_compare_and_swap(self):
        allocation = self.allocate()
        version = identity.rebind_public_job(
            self.connection,
            allocation.public_job_id,
            expected_version=1,
            canonical_opportunity_id=102,
            now=LATER,
        )
        self.assertEqual(version, 2)
        self.assertEqual(
            identity.resolve_public_job_path(
                self.connection, allocation.primary_path
            ).canonical_opportunity_id,
            102,
        )
        with self.assertRaises(identity.StalePublicJobBinding):
            identity.rebind_public_job(
                self.connection,
                allocation.public_job_id,
                expected_version=1,
                canonical_opportunity_id=103,
                now=LATER,
            )
        self.assertEqual(
            identity.primary_public_job_path_for_canonical(self.connection, 102),
            allocation.primary_path,
        )

    def test_binding_delete_then_insert_cannot_reset_ownership_or_history(self):
        allocation = self.allocate()
        before = self.connection.execute(
            "SELECT canonical_opportunity_id, binding_version, bound_at, updated_at "
            "FROM public_job_bindings WHERE public_job_id = ?",
            (allocation.public_job_id,),
        ).fetchone()
        with self.assertRaisesRegex(
            sqlite3.IntegrityError,
            "binding can retire only with its identity",
        ):
            self.connection.execute(
                "DELETE FROM public_job_bindings WHERE public_job_id = ?",
                (allocation.public_job_id,),
            )
        self.assertEqual(
            self.connection.execute(
                "SELECT canonical_opportunity_id, binding_version, bound_at, "
                "updated_at FROM public_job_bindings WHERE public_job_id = ?",
                (allocation.public_job_id,),
            ).fetchone(),
            before,
        )

    def test_binding_insert_or_replace_cannot_reset_ownership_or_history(self):
        allocation = self.allocate()
        before = self.connection.execute(
            "SELECT canonical_opportunity_id, binding_version, bound_at, updated_at "
            "FROM public_job_bindings WHERE public_job_id = ?",
            (allocation.public_job_id,),
        ).fetchone()
        with self.assertRaisesRegex(
            sqlite3.IntegrityError,
            "issued public job binding cannot be replaced",
        ):
            self.connection.execute(
                "INSERT OR REPLACE INTO public_job_bindings "
                "(public_job_id, canonical_opportunity_id, binding_version, "
                "bound_at, updated_at) VALUES (?, ?, 1, ?, ?)",
                (
                    allocation.public_job_id,
                    103,
                    LATER.isoformat(),
                    LATER.isoformat(),
                ),
            )
        self.assertEqual(
            self.connection.execute(
                "SELECT canonical_opportunity_id, binding_version, bound_at, "
                "updated_at FROM public_job_bindings WHERE public_job_id = ?",
                (allocation.public_job_id,),
            ).fetchone(),
            before,
        )

    def test_binding_update_or_replace_cannot_take_an_owned_canonical_id(self):
        self.assertEqual(
            self.connection.execute("PRAGMA recursive_triggers").fetchone()[0],
            0,
        )
        first = self.allocate(101, entropy="11" * 16)
        second = self.allocate(102, entropy="22" * 16)
        before = self.connection.execute(
            "SELECT public_job_id, canonical_opportunity_id, binding_version, "
            "bound_at, updated_at FROM public_job_bindings ORDER BY public_job_id"
        ).fetchall()

        with self.assertRaisesRegex(
            sqlite3.IntegrityError,
            "canonical opportunity binding is already owned",
        ):
            self.connection.execute(
                "UPDATE OR REPLACE public_job_bindings "
                "SET canonical_opportunity_id = ?, "
                "binding_version = binding_version + 1, updated_at = ? "
                "WHERE public_job_id = ?",
                (102, LATER.isoformat(), first.public_job_id),
            )

        self.assertEqual(
            self.connection.execute(
                "SELECT public_job_id, canonical_opportunity_id, binding_version, "
                "bound_at, updated_at FROM public_job_bindings ORDER BY public_job_id"
            ).fetchall(),
            before,
        )
        self.assertEqual(
            identity.resolve_public_job_path(
                self.connection, second.primary_path
            ).canonical_opportunity_id,
            102,
        )
        identity.assert_public_job_identity_consistent(self.connection)

    def test_binding_update_or_replace_allows_only_valid_owned_transitions(self):
        first = self.allocate(101, entropy="11" * 16)
        second = self.allocate(102, entropy="22" * 16)
        original_bound_at = self.connection.execute(
            "SELECT bound_at FROM public_job_bindings WHERE public_job_id = ?",
            (first.public_job_id,),
        ).fetchone()[0]

        self.connection.execute(
            "UPDATE OR REPLACE public_job_bindings "
            "SET canonical_opportunity_id = canonical_opportunity_id, "
            "binding_version = binding_version + 1, updated_at = ? "
            "WHERE public_job_id = ?",
            (LATER.isoformat(), first.public_job_id),
        )
        self.connection.execute(
            "UPDATE OR REPLACE public_job_bindings "
            "SET canonical_opportunity_id = 103, "
            "binding_version = binding_version + 1, updated_at = ? "
            "WHERE public_job_id = ?",
            (LATER.isoformat(), first.public_job_id),
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT canonical_opportunity_id, binding_version, bound_at "
                "FROM public_job_bindings WHERE public_job_id = ?",
                (first.public_job_id,),
            ).fetchone(),
            (103, 3, original_bound_at),
        )
        with self.assertRaisesRegex(
            sqlite3.IntegrityError,
            "binding version must advance once",
        ):
            self.connection.execute(
                "UPDATE OR REPLACE public_job_bindings "
                "SET canonical_opportunity_id = 101, binding_version = 3, "
                "updated_at = ? WHERE public_job_id = ?",
                (LATER.isoformat(), first.public_job_id),
            )
        with self.assertRaisesRegex(
            sqlite3.IntegrityError,
            "canonical opportunity binding is already owned",
        ):
            self.connection.execute(
                "UPDATE public_job_bindings SET canonical_opportunity_id = 102, "
                "binding_version = binding_version + 1, updated_at = ? "
                "WHERE public_job_id = ?",
                (LATER.isoformat(), first.public_job_id),
            )
        self.assertEqual(
            self.connection.execute(
                "SELECT canonical_opportunity_id, binding_version FROM "
                "public_job_bindings WHERE public_job_id = ?",
                (second.public_job_id,),
            ).fetchone(),
            (102, 1),
        )
        identity.assert_public_job_identity_consistent(self.connection)

    def test_portable_import_preserves_merged_redirect_ownership(self):
        loser = self.allocate(101, entropy="11" * 16, primary_path="/job/loser")
        survivor = self.allocate(
            102,
            entropy="22" * 16,
            primary_path="/job/survivor",
        )
        identity.merge_public_jobs(
            self.connection,
            loser.public_job_id,
            survivor.public_job_id,
            now=LATER,
        )
        payload = identity.export_public_job_registry(self.connection)

        imported = sqlite3.connect(":memory:")
        try:
            imported.executescript(SCHEMA.read_text(encoding="utf-8"))
            imported.executescript(MIGRATION.read_text(encoding="utf-8"))
            seed_canonical_opportunities(imported, offset=900)
            identity.import_public_job_registry(imported, payload)
            identity.bind_imported_public_job(
                imported,
                survivor.public_job_id,
                1001,
                now=LATER,
            )
            decision = identity.resolve_public_job_path(
                imported, loser.primary_path
            )
            self.assertEqual(decision.kind, "redirect")
            self.assertEqual(decision.public_job_id, loser.public_job_id)
            self.assertEqual(
                decision.target_public_job_id,
                survivor.public_job_id,
            )
            self.assertEqual(decision.location, survivor.primary_path)
            identity.assert_public_job_identity_consistent(imported)
        finally:
            imported.close()

    def test_merges_retarget_every_redirect_to_survivor_in_one_hop(self):
        first = self.allocate(101, entropy="11" * 16, primary_path="/job/first")
        second = self.allocate(102, entropy="22" * 16, primary_path="/job/second")
        third = self.allocate(103, entropy="33" * 16, primary_path="/job/third")
        identity.merge_public_jobs(
            self.connection, first.public_job_id, second.public_job_id, now=LATER
        )
        identity.merge_public_jobs(
            self.connection, second.public_job_id, third.public_job_id, now=LATER
        )
        for path in (first.primary_path, second.primary_path):
            decision = identity.resolve_public_job_path(self.connection, path)
            self.assertEqual(decision.kind, "redirect")
            self.assertEqual(decision.location, third.primary_path)
            self.assertEqual(decision.target_public_job_id, third.public_job_id)
        targets = dict(
            self.connection.execute(
                "SELECT public_job_id, redirect_target_public_job_id "
                "FROM public_job_identities WHERE disposition = 'redirect'"
            )
        )
        self.assertEqual(targets[first.public_job_id], third.public_job_id)
        self.assertEqual(targets[second.public_job_id], third.public_job_id)
        with self.assertRaises(identity.PublicJobIdentityInvariantError):
            identity.merge_public_jobs(
                self.connection,
                third.public_job_id,
                first.public_job_id,
                now=LATER,
            )
        identity.assert_public_job_identity_consistent(self.connection)

    def test_redirect_schema_rejects_chains_and_cycles(self):
        first = self.allocate(101, entropy="11" * 16)
        second = self.allocate(102, entropy="22" * 16)
        identity.merge_public_jobs(
            self.connection, first.public_job_id, second.public_job_id, now=LATER
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                "INSERT INTO public_job_identities "
                "(public_job_id, disposition, redirect_target_public_job_id, "
                "created_at, updated_at) VALUES (?, 'redirect', ?, ?, ?)",
                ("j" + "44" * 16, first.public_job_id, NOW.isoformat(), NOW.isoformat()),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                "UPDATE public_job_identities SET redirect_target_public_job_id = ? "
                "WHERE public_job_id = ?",
                (first.public_job_id, first.public_job_id),
            )

    def test_gone_and_restore_preserve_the_same_identity_and_path(self):
        allocation = self.allocate(primary_path="/job/restorable")
        before = identity.export_public_job_registry(self.connection)
        self.assertEqual(
            identity.resolve_public_job_path(
                self.connection, allocation.primary_path
            ).kind,
            "serve",
        )
        identity.mark_public_job_gone(
            self.connection, allocation.public_job_id, now=LATER
        )
        gone = identity.resolve_public_job_path(
            self.connection, allocation.primary_path
        )
        self.assertEqual(gone.kind, "gone")
        self.assertEqual(gone.public_job_id, allocation.public_job_id)
        self.assertEqual(gone.primary_path, allocation.primary_path)
        with self.assertRaises(identity.PublicJobIdCollisionExhausted):
            identity.allocate_public_job(
                self.connection,
                allocator=allocator("11" * 16),
                company_slug="acme-ai",
                canonical_title="Research Engineer",
                canonical_opportunity_id=102,
                now=LATER,
                collision_retries=1,
            )
        identity.restore_public_job(
            self.connection, allocation.public_job_id, now=LATER
        )
        restored = identity.resolve_public_job_path(
            self.connection, allocation.primary_path
        )
        self.assertEqual(restored.kind, "serve")
        self.assertEqual(restored.public_job_id, allocation.public_job_id)
        self.assertEqual(restored.primary_path, allocation.primary_path)
        after = identity.export_public_job_registry(self.connection)
        self.assertEqual(before["paths"], after["paths"])

    def test_temporary_availability_is_external_to_permanent_identity(self):
        allocation = self.allocate(primary_path="/job/stable-while-unavailable")
        before = identity.export_public_job_registry(self.connection)
        self.connection.execute(
            "UPDATE canonical_opportunities SET is_active = 0 WHERE id = 101"
        )
        unavailable_route = identity.resolve_public_job_path(
            self.connection, allocation.primary_path
        )
        self.assertEqual(unavailable_route.kind, "serve")
        self.assertEqual(identity.export_public_job_registry(self.connection), before)
        self.connection.execute(
            "UPDATE canonical_opportunities SET is_active = 1 WHERE id = 101"
        )
        restored_route = identity.resolve_public_job_path(
            self.connection, allocation.primary_path
        )
        self.assertEqual(restored_route, unavailable_route)

    def test_path_validation_rejects_encoding_and_ambiguity(self):
        self.assertEqual(
            identity.validate_public_job_path("/Job/Legacy-Case"),
            "/Job/Legacy-Case",
        )
        for path in (
            "/job/Legacy%20Role/",
            "/job/bad%escape",
            "/job/bad%2Fsegment",
            "/job/../escape",
            "/job/double//segment",
            "/job/query?x=1",
            "/job/unicode-é",
        ):
            with self.subTest(path=path):
                with self.assertRaises(identity.InvalidPublicJobIdentity):
                    identity.validate_public_job_path(path)

    def test_literal_and_percent_encoded_equivalent_paths_cannot_have_owners(self):
        literal = self.allocate(
            101,
            entropy="11" * 16,
            primary_path="/job/a",
        )
        other = self.allocate(102, entropy="22" * 16)
        with self.assertRaises(identity.InvalidPublicJobIdentity):
            identity.register_public_job_alias(
                self.connection,
                other.public_job_id,
                "/job/%61",
                now=LATER,
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                "INSERT INTO public_job_paths "
                "(path, normalized_path, public_job_id, path_role, created_at) "
                "VALUES ('/job/%61', '/job/%61', ?, 'alias', ?)",
                (other.public_job_id, LATER.isoformat()),
            )
        self.assertEqual(
            self.connection.execute(
                "SELECT path, public_job_id FROM public_job_paths "
                "WHERE path IN ('/job/a', '/job/%61') ORDER BY path"
            ).fetchall(),
            [("/job/a", literal.public_job_id)],
        )

    def test_reconciliation_reports_a_missing_protection_object(self):
        self.allocate()
        self.connection.execute("DROP TRIGGER trg_public_job_paths_no_update")
        findings = identity.reconcile_public_job_identity(self.connection)
        missing = [
            item.detail
            for item in findings
            if item.code == "required_schema_object_missing"
        ]
        self.assertEqual(
            missing,
            ["trigger:trg_public_job_paths_no_update"],
        )

    def test_mutations_refuse_a_connection_with_foreign_keys_disabled(self):
        self.connection.commit()
        self.connection.execute("PRAGMA foreign_keys = OFF")
        with self.assertRaisesRegex(
            identity.PublicJobIdentityInvariantError,
            "require SQLite foreign keys",
        ):
            self.allocate()
        self.assertIn(
            "foreign_keys_disabled",
            {item.code for item in identity.reconcile_public_job_identity(self.connection)},
        )


if __name__ == "__main__":
    unittest.main()
