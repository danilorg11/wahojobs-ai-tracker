# Explicit fresh inventory repair

The normal daily timer still consumes one 06:00 UTC slot. A failed or missed
slot is preserved and cannot be replayed. Following an authorized deployment,
an operator can request a separate fresh collection through the same native
`wahojobs-inventory.service` supervisor:

```text
scripts/daily_inventory.py run --policy /etc/wahojobs-inventory-v1.json --trigger manual --repair-request inventory-repair-20260927 --all-enabled
```

These are arguments for the existing unit's explicitly reviewed temporary
`ExecStart` override, using its installed Python interpreter and working
directory. They are not a direct worker invocation. Restore the normal unit
command after the attempt; do not replace the timer or change its schedule.
Use repeated `--source` arguments instead of `--all-enabled` to restrict the
request. Both forms preserve current enabled-source restrictions and individual
request/time limits. Disabled or blocked sources cannot be selected manually.

Choose one stable request token before starting. A durable global claim binds
the token to the exact release, source policy, and selection. Repeating that
token—even on another day—performs no collection. Changing its binding is
rejected. An interrupted reservation also consumes the request; issuing a new
token is a separate operator decision, never an automatic retry.

The repair uses the actual UTC collection time, source request budget, common
operation gate, readiness checks, protected-data validation, and bounded native
recovery. It cannot publish an earlier capture as fresh evidence. Its distinct
run directory preserves the original daily receipt and stores selected-source
outcomes. Unselected source states stay unchanged. Health reports show the
historical daily result and subsequent repair separately; pending or missing
records remain visible even when qualifying positive records were published.
This command does not send email.

Before stopping the application, the supervisor prepares and verifies the
journal copy while the application remains available. A digest returned
directly from that worker is held in supervisor memory and passed to the cold
backup worker. No beta-writable receipt can replace this proof. Cold backup
requires the exact prepared copy, current source identities and hashes, release,
run, configuration, and database identity, then snapshots current SQLite data.
Preparation consumes only the existing overall allowance and leaves the
240-second publication window reserved. The existing file-count and preparation
age limits remain enforced; growing evidence history still needs a separately
reviewed archival format before reaching those limits.
