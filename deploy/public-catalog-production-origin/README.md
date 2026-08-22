# Offline production public-catalog origin boundary

This directory contains only the generated Caddy boundary and nonsecret example
configuration for Production Exact-Route Release v1. It does not contain an
installer, production credentials, a DNS record, or a deployment command.

The application must bind to `127.0.0.1`. Caddy admits authenticated `GET` and
`HEAD` only for `/jobs`, the exact Handshake canary, and the three authenticated
operator health routes. Every other path is rejected before Python.

The public application origin and authority are always
`https://www.wahojobs.com`; the separate origin hostname is transport-only and
must never appear in canonical or structured-data URLs.
