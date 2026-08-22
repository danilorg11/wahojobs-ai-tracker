# Offline production exact-route release inputs

These nonsecret inputs define Production Exact-Route Release v1. Their scope is
closed to the active Handshake canary: one serving identity, one strict primary
path, and one canonical-key binding. Karl, aliases, redirects, gone identities,
and every other public job path are deliberately absent.

`scripts/build_public_catalog_production_database.py` reads the source database
in read-only mode, emits `catalog.sqlite3` as a public-only projection, and
creates the production release and projection manifests. The generated SQLite
artifact is ignored by Git and must be rebuilt and attested for each reviewed
release. These files do not publish a route or change any external system.
