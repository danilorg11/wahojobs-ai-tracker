# Preview-only public job release inputs

This directory contains the exact, nonsecret M009 registry and database-local
canonical-key bindings for the isolated job-detail Preview proof. Its scope is
closed to two identities: Karl's approved legacy path and one strict new-format
Handshake canary. The projection builder validates that exact shape and refuses
additional identities, paths, aliases, or bindings.

These inputs do not route traffic by themselves. The builder imports them into
the public-only projection and emits a release manifest whose identity binds the
projection SHA-256, registry SHA-256, exact paths, public IDs, and canonical
keys. Vercel and the origin must use that same generated manifest.
