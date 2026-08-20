from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest

from scripts.public_job_identity_migration import (
    apply_public_job_identity_migration,
)
from tests.test_authenticated_profile_matches import (
    _seed_configured_inventory,
)
from tests.workos_authkit_test_support import build_m008
from wahojobs.authenticated_profile_matches import (
    AuthenticatedProfileMatchesBrowserIntegration,
    AuthenticatedProfileMatchesService,
)
from wahojobs.matching.metadata_overlay import OpportunityMetadataOverlay
from wahojobs.public_job_canary import PublicJobCanaryRoutingGate
from wahojobs.public_job_identity import (
    PublicJobIdAllocator,
    allocate_public_job,
    mark_public_job_gone,
)


NOW = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
ORIGIN = "https://app.test"
TEMPORARY_PATH = "/job/opportunity-7002"
PERMANENT_PATH = "/job/disposable-proven-canary"
PUBLIC_JOB_ID = "j" + ("11" * 16)
OTHER_PUBLIC_JOB_ID = "j" + ("22" * 16)


class _ReadOnlyProvider:
    def __init__(self, path):
        self.path = Path(path)

    @contextmanager
    def __call__(self):
        connection = sqlite3.connect(
            f"file:{self.path.as_posix()}?mode=ro",
            uri=True,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA query_only = ON")
        try:
            yield connection
        finally:
            connection.close()


class PublicJobCanaryRoutingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(
            prefix="wahojobs-public-job-canary-",
            ignore_cleanup_errors=True,
        )
        self.database_path = Path(self.directory.name) / "canary.sqlite3"
        connection = build_m008(self.database_path)
        apply_public_job_identity_migration(connection)
        connection.close()
        _seed_configured_inventory(self.database_path, observed_at=NOW)
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            allocation = allocate_public_job(
                connection,
                allocator=PublicJobIdAllocator(
                    "disposable-routing-test",
                    random_source=lambda size: bytes.fromhex("11" * size),
                ),
                company_slug="configured-production",
                canonical_title="Distinctive Bilingual Data Annotation Reviewer",
                canonical_opportunity_id=7002,
                primary_path=PERMANENT_PATH,
                now=NOW,
            )
            self.assertEqual(allocation.public_job_id, PUBLIC_JOB_ID)
        finally:
            connection.close()
        self.provider = _ReadOnlyProvider(self.database_path)

    def tearDown(self):
        self.directory.cleanup()

    def _integration(self, gate=None):
        service = object.__new__(AuthenticatedProfileMatchesService)
        options = {}
        if gate is not None:
            options["public_job_canary_gate"] = gate
        integration = AuthenticatedProfileMatchesBrowserIntegration(
            service,
            connection_provider=self.provider,
            metadata_overlay=OpportunityMetadataOverlay(
                path=self.database_path.with_suffix(".overlay.json"),
                records_by_key={},
            ),
            confirmed_profile_artifact_sink=lambda _artifact: None,
            completed_profile_confirmation_authenticator=lambda _artifact: None,
            public_origin=ORIGIN,
            now=lambda: NOW,
            **options,
        )
        self.addCleanup(integration.close)
        return integration

    @staticmethod
    def _headers():
        return (("Host", "app.test"),)

    def test_default_gate_is_disabled_and_temporary_route_is_unchanged(self):
        integration = self._integration()

        self.assertFalse(integration.matches_route(PERMANENT_PATH))
        self.assertTrue(integration.matches_route(TEMPORARY_PATH))
        temporary = integration.handle("GET", TEMPORARY_PATH, self._headers())
        self.assertEqual(temporary.status, 200)
        self.assertIn(TEMPORARY_PATH.encode("ascii"), temporary.body)

    def test_exact_id_gate_serves_primary_and_redirects_only_its_temp_route(self):
        integration = self._integration(PublicJobCanaryRoutingGate((PUBLIC_JOB_ID,)))

        self.assertTrue(integration.matches_route(PERMANENT_PATH))
        primary = integration.handle("GET", PERMANENT_PATH, self._headers())
        temporary = integration.handle("GET", TEMPORARY_PATH, self._headers())
        unmapped = integration.handle(
            "GET",
            "/job/unapproved-legacy-path",
            self._headers(),
        )

        self.assertEqual(primary.status, 200)
        self.assertIn(
            (ORIGIN + PERMANENT_PATH).encode("ascii"),
            primary.body,
        )
        self.assertEqual(temporary.status, 301)
        self.assertEqual(dict(temporary.headers)["Location"], PERMANENT_PATH)
        self.assertEqual(unmapped.status, 404)

    def test_wrong_exact_id_cannot_expose_a_registered_path(self):
        integration = self._integration(
            PublicJobCanaryRoutingGate((OTHER_PUBLIC_JOB_ID,))
        )

        permanent = integration.handle("GET", PERMANENT_PATH, self._headers())
        temporary = integration.handle("GET", TEMPORARY_PATH, self._headers())
        self.assertEqual(permanent.status, 404)
        self.assertEqual(temporary.status, 200)

    def test_gone_lifecycle_is_visible_only_through_the_exact_gate(self):
        connection = sqlite3.connect(self.database_path)
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            mark_public_job_gone(
                connection,
                PUBLIC_JOB_ID,
                now=datetime(2026, 8, 20, 13, 0, tzinfo=timezone.utc),
            )
        finally:
            connection.close()

        enabled = self._integration(PublicJobCanaryRoutingGate((PUBLIC_JOB_ID,)))
        disabled = self._integration()
        self.assertEqual(
            enabled.handle("GET", PERMANENT_PATH, self._headers()).status,
            410,
        )
        self.assertEqual(
            enabled.handle("GET", TEMPORARY_PATH, self._headers()).status,
            301,
        )
        self.assertFalse(disabled.matches_route(PERMANENT_PATH))

    def test_gate_rejects_invalid_duplicate_and_oversized_identity_sets(self):
        for values in (
            ("not-a-public-id",),
            (PUBLIC_JOB_ID, PUBLIC_JOB_ID),
            tuple("j" + f"{index:032x}" for index in range(65)),
        ):
            with self.subTest(count=len(values)):
                with self.assertRaises(ValueError):
                    PublicJobCanaryRoutingGate(values)


if __name__ == "__main__":
    unittest.main()
