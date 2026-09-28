# Packaged journal recovery

`private_beta_cold_snapshot_v2` stores the complete retained journal in bounded
ZIP segments. It does not prune live history or change verification clocks.
Existing v1 snapshots remain independently verifiable and restorable.

The daily worker explicitly requests v2 while the application is available.
Preparation binds the current database identity, run, release, configuration,
pin, complete source membership, original file identities and hashes. It checks
available space, packages all journal evidence, verifies expanded file hashes,
and reconstructs each actual plan in bounded disposable storage for the existing
journal chain validator. It repeats source membership/content checks and the
remaining-space check before returning the preparation receipt.

The root supervisor holds the digest of that **returned receipt** in memory.
Cold adoption authenticates it before selecting a format; a mutable disk checksum
is insufficient. Under offline ownership and exclusive SQLite transactions, the
worker snapshots the current database/drafts/companion, verifies current journal
identities and hashes, authenticates the prepared compressed bytes, and adopts
the segments by same-filesystem rename. User writes made during online
preparation therefore remain in the snapshot. The existing 60-second backup,
240-second publication and recovery/aggregate budgets are unchanged.

`verify_snapshot(path)` always verifies expanded data and chains for v2.
The native cold worker may pass `trusted_manifest` **only as the immediate object
returned by its own successful `create_snapshot` call**. This optimized path
still verifies physical hashes and SQLite; it omits only the archive inflation
and journal replay already proved online. Never construct this argument from a
file, command-line JSON, or an untrusted caller.

## Format and limits

The ordinary `files` map contains SQLite, draft, pin, lineage and physical ZIP
records. `journal_segments/000000.zip` and following segments each contain
canonical `journal/<relative>` names and `JOURNAL-INDEX.json`. The index preserves
each original identity (`path`, `device`, `inode`), byte count and SHA-256.
Original absolute paths are metadata, never extraction targets. The outer
segment record binds SHA-256, index SHA-256, member count, expanded bytes and
first/last logical names. `journal_inventory` binds total count, bytes and a
streaming canonical digest of the full sorted logical inventory.

| Limit | Value |
| --- | ---: |
| Physical snapshot files | 10,000 (unchanged) |
| Outer manifest/preparation receipt | 8,000,000 bytes (unchanged) |
| Segments / logical files per segment | 4,096 / 512 |
| All logical files / expanded journal | 1,000,000 / 64 GiB |
| Logical file / expanded segment | 128 MiB / 256 MiB |
| Compressed segment / central directory | 264 MiB / 1 MiB |
| Segment index | 2,000,000 bytes |
| Logical path | 1,024 UTF-8 bytes / 16 components |
| Reconstructed plan | 20,000 files / 512 MiB |

Only regular, unencrypted DEFLATE/STORED ZIP entries are accepted. Checks reject
ZIP64 (including local-only extra fields), multidisk archives, extra padding,
duplicate members/JSON keys, links, directories, unsupported compression,
incorrect CRC/content hashes, size discrepancies, absolute/traversing paths,
Windows device names, case aliases in any path component, and file/directory
collisions. Compressed and central-directory bounds are checked before parsing
or hashing the input. Extraction uses exclusive streaming writes, never ZIP
bulk extraction.

Disk preflight reserves current source bytes with a compression overhead margin,
actual encoded index allowance, the largest bounded plan reconstruction,
current ordinary storage copies, and 64 MiB spare. Format capacity is not a claim
that this much disk or execution time is available on the host. Growth still
requires measured capacity planning; no format can guarantee unlimited history
within fixed disk and outage budgets.

## Restore, relocation, and rollback

Restore reconstructs the original journal file bytes and keeps the original pin.
It remains activation-held until existing lossless reconciliation proves exact
source identity/content and restored membership. V2 lineage references bounded,
content-addressed index chunks in `lineage-history`; it never embeds a growing
million-row inventory or recursively duplicates old receipts. Previous receipts
remain byte-identical, referenced by hash. Relocation persists the destination,
retires the source, then activates the destination; identical interrupted retries
retain this order. Every lineage receipt remains below its existing 8 MB reader
limit, and every new chunk stays below 2 MB.

Creating v2 backups does not change active database/lineage formats, so rolling
code back remains possible while the original storage is active. Old releases
cannot read v2 backups: retain the tested new release for recovery. After an
explicit v2 restore/relocation, the activated lineage requires a v2-aware reader;
older code fails closed. Never rewrite a pin or roll back private database rows
to make an unsupported downgrade work.

The focused tests cover v1 compatibility, more than 10,000 logical files,
actual interrupted/completed journal chains, online user writes, cold verification
without inflation, deep standalone verification, repeated v1/v2 relocation,
interrupted fencing, full hash/path/ZIP attacks with recomputed outer checksums,
and space/size-bound failures. Installation also requires a timed Linux rehearsal
using retained data in an inert private clone, under the actual service resource
limits; it must not retire or activate live storage.
