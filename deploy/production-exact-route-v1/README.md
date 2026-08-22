# Production Exact-Route Release v1 — offline routing bundle

The activation and rollback JSON files in this directory are generated review
documents. They are not Vercel API payloads and do not publish anything. The
activation bundle names exactly `/jobs` and the Handshake canary; the rollback
bundle contains zero routes. Both bind the same production release ID.

`production-gateway-deployment` is a symbolic target. Resolving it to a reviewed
immutable Vercel deployment and translating this bundle into the existing
`www.wahojobs.com` route owner's project-level configuration are explicitly
outside this offline milestone.

Every preservation path remains with the existing legacy owner. A future route
publication is acceptable only if both exact activation rules can be added in
one action and the zero-rule document can be restored in one action.
