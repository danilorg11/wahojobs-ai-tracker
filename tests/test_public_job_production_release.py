from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import tempfile
import unittest

from scripts.render_public_job_production_routes import (
    PRESERVATION_PATHS,
    render_caddy_configuration,
    render_route_publication,
    render_vercel_configuration,
)
from wahojobs import public_job_identity
from wahojobs.public_job_release import (
    HANDSHAKE_CANARY_CANONICAL_KEY,
    HANDSHAKE_CANARY_PUBLIC_JOB_ID,
    HANDSHAKE_CANARY_PUBLIC_JOB_PATH,
    KARL_PUBLIC_JOB_PATH,
    PUBLIC_JOB_PRODUCTION_RELEASE_FORMAT,
    PublicJobReleaseError,
    build_production_release,
    load_production_binding_publications,
    load_production_registry_artifact,
    load_production_release_manifest,
    validate_production_release_manifest,
)


ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "deploy/public-job-production/registry.json"
BINDINGS = ROOT / "deploy/public-job-production/bindings.json"
MANIFEST = ROOT / "deploy/vercel-production-gateway/release-manifest.json"
VERCEL = ROOT / "deploy/vercel-production-gateway/vercel.json"
CADDY = ROOT / "deploy/public-catalog-production-origin/Caddyfile"
ACTIVATION = (
    ROOT / "deploy/production-exact-route-v1/route-publication-activation.json"
)
ROLLBACK = ROOT / "deploy/production-exact-route-v1/route-publication-rollback.json"


def _recompute_release(document):
    payload = {
        "database_sha256": document["database_sha256"],
        "format": document["format"],
        "published_details": document["published_details"],
        "registry_sha256": document["registry_sha256"],
    }
    result = deepcopy(document)
    result["release_id"] = sha256(
        (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode(
            "ascii"
        )
    ).hexdigest()
    return result


class PublicJobProductionReleaseTests(unittest.TestCase):
    def test_checked_in_inputs_and_release_are_exact_canary_only(self):
        registry = load_production_registry_artifact(REGISTRY)
        bindings = load_production_binding_publications(BINDINGS)
        release = load_production_release_manifest(MANIFEST)
        self.assertEqual(
            build_production_release(
                database_sha256=release.database_sha256,
                registry_artifact=registry,
                bindings=bindings,
            ),
            release,
        )
        self.assertEqual(
            release.as_dict()["format"],
            PUBLIC_JOB_PRODUCTION_RELEASE_FORMAT,
        )
        self.assertEqual(release.paths, (HANDSHAKE_CANARY_PUBLIC_JOB_PATH,))
        self.assertEqual(release.public_job_ids, (HANDSHAKE_CANARY_PUBLIC_JOB_ID,))
        self.assertEqual(
            release.published_details[0].canonical_key,
            HANDSHAKE_CANARY_CANONICAL_KEY,
        )
        self.assertNotIn(KARL_PUBLIC_JOB_PATH, release.paths)

    def test_all_generated_boundaries_derive_from_the_same_release(self):
        release = load_production_release_manifest(MANIFEST)
        self.assertEqual(VERCEL.read_text(encoding="utf-8"), render_vercel_configuration(release))
        self.assertEqual(CADDY.read_text(encoding="utf-8"), render_caddy_configuration(release))
        self.assertEqual(
            ACTIVATION.read_text(encoding="utf-8"),
            render_route_publication(release, enabled=True),
        )
        self.assertEqual(
            ROLLBACK.read_text(encoding="utf-8"),
            render_route_publication(release, enabled=False),
        )

        vercel = json.loads(VERCEL.read_text(encoding="utf-8"))
        self.assertEqual(
            vercel["rewrites"],
            [
                {"source": "/jobs", "destination": "/api/jobs"},
                {
                    "source": HANDSHAKE_CANARY_PUBLIC_JOB_PATH,
                    "destination": "/api/job",
                },
            ],
        )
        self.assertFalse(
            any("*" in item["source"] or ":" in item["source"] for item in vercel["rewrites"])
        )

        activation = json.loads(ACTIVATION.read_text(encoding="utf-8"))
        rollback = json.loads(ROLLBACK.read_text(encoding="utf-8"))
        self.assertEqual(
            [item["source"] for item in activation["routes"]],
            ["/jobs", HANDSHAKE_CANARY_PUBLIC_JOB_PATH],
        )
        self.assertEqual(rollback["routes"], [])
        self.assertEqual(activation["release_id"], rollback["release_id"])
        self.assertEqual(activation["cache_policy"], "no-store")
        self.assertIn(KARL_PUBLIC_JOB_PATH, PRESERVATION_PATHS)
        self.assertTrue(set(PRESERVATION_PATHS).isdisjoint(item["source"] for item in activation["routes"]))

    def test_release_rejects_karl_extra_routes_and_rebinding(self):
        release = load_production_release_manifest(MANIFEST).as_dict()
        variants = []
        with_karl = deepcopy(release)
        with_karl["published_details"].append(
            {
                "path": KARL_PUBLIC_JOB_PATH,
                "public_job_id": "j7b8550e11700c9b26ac68deb753e1f82",
                "canonical_key": "oneforma::177080",
            }
        )
        variants.append(_recompute_release(with_karl))
        rebound = deepcopy(release)
        rebound["published_details"][0]["canonical_key"] = "other::canary"
        variants.append(_recompute_release(rebound))
        preview = deepcopy(release)
        preview["format"] = "wahojobs-public-job-preview-release-v1"
        variants.append(_recompute_release(preview))
        for document in variants:
            with self.subTest(document=document):
                with self.assertRaises(PublicJobReleaseError):
                    validate_production_release_manifest(document)

    def test_registry_rejects_aliases_and_any_second_identity(self):
        original = json.loads(REGISTRY.read_text(encoding="ascii"))
        alias = deepcopy(original)
        alias_path = deepcopy(alias["paths"][0])
        alias_path["path"] += "-alias"
        alias_path["normalized_path"] += "-alias"
        alias_path["path_role"] = "alias"
        alias["paths"].append(alias_path)
        extra = deepcopy(original)
        extra["identities"].append(
            {
                "public_job_id": "j7b8550e11700c9b26ac68deb753e1f82",
                "disposition": "serving",
                "redirect_target_public_job_id": None,
                "created_at": "2026-08-21T15:35:26+00:00",
                "updated_at": "2026-08-21T15:35:26+00:00",
            }
        )
        extra["paths"].append(
            {
                "path": KARL_PUBLIC_JOB_PATH,
                "normalized_path": KARL_PUBLIC_JOB_PATH,
                "public_job_id": "j7b8550e11700c9b26ac68deb753e1f82",
                "path_role": "primary",
                "created_at": "2026-08-21T15:35:26+00:00",
            }
        )
        with tempfile.TemporaryDirectory(prefix="wahojobs-production-release-") as name:
            root = Path(name)
            for index, document in enumerate((alias, extra)):
                artifact = public_job_identity.public_job_registry_artifact(document)
                path = root / f"registry-{index}.json"
                path.write_bytes(artifact.canonical_json)
                with self.subTest(index=index):
                    with self.assertRaises(PublicJobReleaseError):
                        load_production_registry_artifact(path)


if __name__ == "__main__":
    unittest.main()
