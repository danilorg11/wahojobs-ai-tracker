# Inventory operational email adapter

The existing version-2 alert executable boundary can use
`scripts/operational_email.py --settings /etc/wahojobs-inventory-resend.json`.
This adapter sends one plain-text HTTPS batch to Resend's `/emails` endpoint,
from `Wahojobs Operations <alerts@ops.wahojobs.com>` to `danilo@wahojobs.com`.
It does not reuse WorkOS delivery, use SMTP, install an SDK or enable receiving.

Activation requires a verified sending domain, the free plan with paid overages
disabled, owner-approved settings, and a sending-only API key restricted to
`ops.wahojobs.com`. Enter the key privately into the existing administrative
handoff; keep it out of command arguments, environment values, archives and Git.
Store it root-owned, mode 0600 at
`/etc/wahojobs-beta/operations/resend-api-key`. The reviewed example systemd drop-in
uses LoadCredential; only the health service receives this credential. Install
the settings root-owned, group wahojobs-beta, mode 0640. Both examples are dormant.

The configured alert command is an absolute argument list: the approved release's
Python, `-B`, its `scripts/operational_email.py`, `--settings`, and the settings
path above. The normal health service remains the sole recurring dispatcher.

The adapter claims each event ID durably before sending. Duplicate and overlapping
batches cannot resend claimed events, even beyond Resend's 24-hour idempotency
window. An ambiguous outcome remains uncertain and is never automatically retried.
The existing health outbox records recovery events separately. Only API acceptance
is recorded by this adapter; owner receipt requires confirmation during activation.

Limits: one HTTPS request per invocation, a ten-second socket timeout within the
existing fifteen-second parent deadline, 25 attempted messages per UTC day,
200 events and 256 KB per input packet. Oversized packets fail closed; the parent
records the attempt as uncertain without automatic retry. No redirects or retries
are allowed. No paid overage is enabled by configuration or code.

Validation: eight isolated adapter tests plus 26 existing daily tests passed;
focused independent review found no blocker. Real native credential delivery and
owner receipt remain activation gates. DNS approval/receipts are retained outside
the code repository with the activation evidence; they contain no private key.
