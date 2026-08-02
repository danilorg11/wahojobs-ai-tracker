import unittest

from scripts import durable_google_login_app as launcher


class _CompleteDurableIntegration:
    def handle(self, *_args, **_kwargs):
        raise AssertionError("construction_must_not_dispatch")

    def issue_confirmed_profile_artifact(self, **_kwargs):
        raise AssertionError("construction_must_not_issue")

    def authenticate_completed_profile_replay(self, **_kwargs):
        raise AssertionError("construction_must_not_authenticate_replay")

    @staticmethod
    def matches_route(path):
        return path == "/find-matches"


class DurableGoogleLoginLauncherHandlerTests(unittest.TestCase):
    def test_production_construction_uses_durable_only_handler_and_keeps_detachment(self):
        integration = _CompleteDurableIntegration()

        handler = launcher._construct_production_handler(
            integration,
            "https://localhost:8443",
            require_profile_creation=True,
        )

        self.assertEqual(
            handler.__module__,
            "wahojobs.durable_product_browser_handler",
        )
        self.assertIs(
            handler._durable_google_login_browser_integration,
            integration,
        )
        handler._durable_google_login_browser_integration = None
        self.assertIsNone(
            handler._durable_google_login_browser_integration
        )

    def test_required_profile_capabilities_fail_closed(self):
        class HandleOnly:
            @staticmethod
            def handle(*_args, **_kwargs):
                return None

        class ArtifactOnly(HandleOnly):
            @staticmethod
            def issue_confirmed_profile_artifact(**_kwargs):
                return None

        class ArtifactAndReplay(ArtifactOnly):
            @staticmethod
            def authenticate_completed_profile_replay(**_kwargs):
                return False

        cases = (
            (
                "missing-artifact",
                HandleOnly(),
                "profile_creation_capability_unavailable",
            ),
            (
                "invalid-artifact",
                type(
                    "InvalidArtifact",
                    (HandleOnly,),
                    {"issue_confirmed_profile_artifact": None},
                )(),
                "profile_creation_capability_invalid",
            ),
            (
                "missing-replay",
                ArtifactOnly(),
                "profile_creation_capability_invalid",
            ),
            (
                "invalid-replay",
                type(
                    "InvalidReplay",
                    (ArtifactOnly,),
                    {"authenticate_completed_profile_replay": None},
                )(),
                "profile_creation_capability_invalid",
            ),
            (
                "missing-matches",
                ArtifactAndReplay(),
                "profile_matching_capability_unavailable",
            ),
            (
                "invalid-matches",
                type(
                    "InvalidMatches",
                    (ArtifactAndReplay,),
                    {"matches_route": None},
                )(),
                "profile_matching_capability_invalid",
            ),
            (
                "unowned-matches",
                type(
                    "UnownedMatches",
                    (ArtifactAndReplay,),
                    {"matches_route": staticmethod(lambda _path: False)},
                )(),
                "profile_matching_capability_unavailable",
            ),
            (
                "non-boolean-matches",
                type(
                    "NonBooleanMatches",
                    (ArtifactAndReplay,),
                    {"matches_route": staticmethod(lambda _path: 1)},
                )(),
                "profile_matching_capability_unavailable",
            ),
            (
                "failed-matches",
                type(
                    "FailedMatches",
                    (ArtifactAndReplay,),
                    {
                        "matches_route": staticmethod(
                            lambda _path: (_ for _ in ()).throw(
                                RuntimeError("private")
                            )
                        )
                    },
                )(),
                "profile_matching_capability_invalid",
            ),
        )

        for label, integration, reason in cases:
            with self.subTest(label=label):
                with self.assertRaisesRegex(RuntimeError, reason):
                    launcher._construct_production_handler(
                        integration,
                        "https://localhost:8443",
                        require_profile_creation=True,
                    )

    def test_nonproduction_injected_runtime_keeps_optional_capability_contract(self):
        class RoutingOnlyIntegration:
            @staticmethod
            def handle(*_args, **_kwargs):
                return None

        handler = launcher._construct_production_handler(
            RoutingOnlyIntegration(),
            "https://localhost:8443",
            require_profile_creation=False,
        )
        self.assertEqual(
            handler.__module__,
            "wahojobs.durable_product_browser_handler",
        )

        class PartialCreationIntegration(RoutingOnlyIntegration):
            @staticmethod
            def issue_confirmed_profile_artifact(**_kwargs):
                return None

        with self.assertRaisesRegex(
            RuntimeError,
            "profile_creation_capability_invalid",
        ):
            launcher._construct_production_handler(
                PartialCreationIntegration(),
                "https://localhost:8443",
                require_profile_creation=False,
            )


if __name__ == "__main__":
    unittest.main()
