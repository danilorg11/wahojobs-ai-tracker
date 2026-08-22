# Offline production exact-route gateway

This Vercel package is disabled unless it runs in the production environment
with `WAHOJOBS_PRODUCTION_PUBLIC_ROUTES_ENABLED=1`. Its generated configuration
contains only `/jobs` and the exact Handshake canary. It has no legacy catch-all
and must not own `www.wahojobs.com` directly.

The separately generated activation document is intended for the existing
public route owner. Until that future document is reviewed and published, this
gateway receives no Wahojobs production traffic. The gateway strips browser
credentials, requires the exact release handshake with the authenticated HTTPS
origin, and forces browser and CDN `no-store`.
