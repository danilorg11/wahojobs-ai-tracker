"""Disposable, synthetic post-authentication matcher fixture (no real login)."""

from contextlib import closing, contextmanager
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
import sqlite3
import tempfile
from unittest import mock

from tests.test_authenticated_profile_matches import (
    AuthenticatedProfileMatchesTests, _ConfiguredReadOnlyProvider,
    _seed_configured_inventory, NOW,
)
from tests.test_profile_preference_model import with_preference_model
from wahojobs import authenticated_profile_matches as browser
from wahojobs.matching.metadata_overlay import OpportunityMetadataOverlay
from wahojobs.profiles.preference_model import empty_profile_preferences_v1


class _RowProvider(_ConfiguredReadOnlyProvider):
    @contextmanager
    def __call__(self):
        with super().__call__() as connection:
            connection.row_factory = sqlite3.Row
            yield connection


class SyntheticMatcherFixture:
    def __init__(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="wahojobs-match-slice-")
        self.path = Path(self.temporary.name) / "synthetic.sqlite"
        _seed_configured_inventory(self.path, observed_at=NOW)
        self.provider = _RowProvider(self.path)
        AuthenticatedProfileMatchesTests.setUpClass()
        self.initial_profile = deepcopy(AuthenticatedProfileMatchesTests.profile_v2)
        self.initial_profile["location"]["country"] = "Brazil"
        self.profile = deepcopy(self.initial_profile)
        self.now = NOW
        self.owner = "a"
        self.update_inventory("UPDATE jobs SET title='Python Backend AI Coding Evaluator', "
                              "department='Software Engineering', expertise='Software Engineering', "
                              "commitment='Full-time', location='Remote - Brazil'")
        self.update_inventory("UPDATE canonical_opportunities SET canonical_title="
                              "'Python Backend AI Coding Evaluator', source_category='Software Engineering'")
        self.add_part_time_variant()
        self.set_preferences("full_time")
        self.service = object.__new__(browser.AuthenticatedProfileMatchesService)
        self.integration = browser.AuthenticatedProfileMatchesBrowserIntegration(
            self.service, connection_provider=self.provider,
            write_connection_provider=self.forbidden_write,
            metadata_overlay=OpportunityMetadataOverlay(self.path.with_suffix(".json"), {}),
            confirmed_profile_artifact_sink=lambda _: None,
            completed_profile_confirmation_authenticator=lambda _: None,
            public_origin="https://app.test", now=lambda: self.now,
            ephemeral_identity_factory=lambda: "synthetic-matcher",
        )

    @staticmethod
    def forbidden_write():
        raise AssertionError("The matcher must not write to the fixture database")

    def update_inventory(self, sql, parameters=()):
        # This fixture owns only its newly-created disposable database.
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute(sql, parameters)

    def add_part_time_variant(self):
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.row_factory = sqlite3.Row
            canonical = dict(connection.execute("SELECT * FROM canonical_opportunities").fetchone())
            canonical.update(id=7005, canonical_key="part-time-python-evaluator",
                             canonical_title="Part-time Python Backend AI Coding Evaluator")
            columns = list(canonical)
            connection.execute(f"INSERT INTO canonical_opportunities ({','.join(columns)}) "
                               f"VALUES ({','.join('?' for _ in columns)})", list(canonical.values()))
            row = dict(connection.execute("SELECT * FROM jobs").fetchone())
            row.update(id=7006, canonical_opportunity_id=7005, external_id="synthetic-part-time",
                       title="Part-time Python Backend AI Coding Evaluator", commitment="Part-time",
                       url="https://jobs.example.test/synthetic-part-time", source_hash="synthetic-part-time")
            columns = list(row)
            connection.execute(f"INSERT INTO jobs ({','.join(columns)}) "
                               f"VALUES ({','.join('?' for _ in columns)})", list(row.values()))

    def set_preferences(self, workload):
        model = empty_profile_preferences_v1()
        model["workloads"] = [workload]
        self.profile = with_preference_model(self.profile, model)

    def authority(self):
        return browser.MatchesAuthorityResult("profile", browser._AuthorizedMatchesState(
            "profile", draft_binding=self.owner * 64,
            account_id="synthetic-account-" + self.owner, environment_namespace="synthetic",
            principal_id="synthetic-principal-" + self.owner, session_id="synthetic-session-" + self.owner,
            profile_id="synthetic-owner-" + self.owner, profile_v2=self.profile,
        ))

    def get(self, target="/find-matches"):
        # Only authentication and tracking storage are fixture substitutes. The
        # ownership check, projection, inventory, ranking, typed preferences,
        # relaxations, cache validation and HTML renderer are the actual code.
        with (mock.patch.object(browser.AuthenticatedProfileMatchesService, "resolve",
                                side_effect=lambda *a, **kw: self.authority()),
              mock.patch.object(browser.AuthenticatedProfileMatchesBrowserIntegration,
                                "_load_pipeline_records", return_value=[])):
            return self.integration.handle("GET", target, (
                ("Host", "app.test"), ("Cookie", "wahojobs_session=" + self.owner * 43),
            ))

    def last_run(self):
        return next(reversed(self.integration._registry._runs.values()))

    def advance(self, hours):
        self.now += timedelta(hours=hours)

    def close(self):
        self.integration.close()
        self.temporary.cleanup()
