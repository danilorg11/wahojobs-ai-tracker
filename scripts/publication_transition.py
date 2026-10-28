"""Prepare publisher replacements; activate only with actual closure/adoption evidence.

Historical sealed packets are never modified. Until every publisher is adopted,
the common protocol can run in legacy-bridge mode, with the old global exclusion.
Scoped mode needs verified handoff of every publisher, not just a new lock file.
"""
import argparse
from pathlib import Path
import sys
import time
import json

import publication as p


def prepare(output):
    output = Path(output).resolve(); output.mkdir(parents=True, exist_ok=True)
    inventory = p.read(p.ROOT/'deploy/publication/publishers.json')
    rows = []
    for publisher in inventory['publishers']:
        path = p.ROOT/publisher['source']
        row = dict(publisher, present_locally=path.exists(), source=str(path))
        if path.is_file():
            row['source_sha256'] = p.digest(path.read_bytes())
        elif (path/'.git').exists():
            row['source_commit'] = p.legacy.git(path, 'rev-parse', 'HEAD').decode().strip()
        row.update(adopted=False, unknown_prior_outcome_resolved=False, current_remote_state_observed=False)
        rows.append(row)
    replacements = output/'workflow-replacements'; replacements.mkdir(exist_ok=True)
    for old_name in ('nodejs-digital-ocean-deploy-new.yml','nodejs-digital-ocean-deploy-backup.yml',
                     'unified-account-release.yml','unified-account-backup.yml','unified-account-preparation-return.yml'):
        text = '''name: Prepare publication artifact (common protocol)
on:
  workflow_dispatch:
permissions:
  contents: read
concurrency:
  group: wahojobs-publication-preparation-${{ github.ref }}
  cancel-in-progress: false
jobs:
  prepare:
    runs-on: ubuntu-24.04
    timeout-minutes: 15
    steps:
      - uses: actions/checkout@d23441a48e516b6c34aea4fa41551a30e30af803
        with:
          persist-credentials: false
      - uses: actions/setup-node@249970729cb0ef3589644e2896645e5dc5ba9c38
        with:
          node-version: '20.x'
      - name: Build the immutable application; production is a separate operation
        env:
          PUPPETEER_SKIP_DOWNLOAD: 'true'
        run: |
          corepack enable
          corepack prepare yarn@1.22.19 --activate
          yarn install --frozen-lockfile --non-interactive
          yarn build
          git rev-parse HEAD > publication-source.txt
          tar -czf publication-artifact.tar.gz dist publication-source.txt
      - name: Retain artifact in the workflow run
        uses: actions/upload-artifact@v4
        with:
          name: publication-${{ github.sha }}
          path: publication-artifact.tar.gz
'''
        (replacements/old_name).write_text(text, encoding='utf-8')
    proposal = dict(format='wahojobs-publication-transition-v2', state='prepared',
        inventory_sha256=p.digest((p.ROOT/'deploy/publication/publishers.json').read_bytes()),
        publishers=rows, replacements={str(f):p.digest(f.read_bytes()) for f in replacements.iterdir()},
        historical_registry=str(p.legacy.LEGACY_REGISTRY), historical_records_mutated=False,
        production_activated=False, mode='legacy-bridge', owner_handoff_required=True,
        prepared_at=p.now())
    destination = output/'transition-proposal.json'; p.once(destination, proposal)
    return dict(proposal=str(destination), sha256=p.digest(destination.read_bytes()), publishers=len(rows))


def activate(proposal_path, receipts_path, approved_hash, *, state=p.STATE):
    raw = Path(proposal_path).read_bytes()
    p.require(p.digest(raw) == approved_hash, 'exact_transition_approval_required')
    proposal, actual = json.loads(raw), p.read(receipts_path)
    p.require(actual['human_decision_reference'] and actual['approved_proposal_sha256'] == approved_hash, 'actual_owner_handoff_required')
    mode = actual['mode']; p.require(mode in ('legacy-bridge','scoped'), 'invalid_transition_mode')
    received = {row['id']:row for row in actual['publishers']}
    for publisher in proposal['publishers']:
        evidence = received[publisher['id']]
        p.require(evidence['unknown_prior_outcome_resolved'] is True and evidence['evidence_references'], 'publisher_unknown_outcome_not_closed')
        if mode == 'scoped':
            p.require(evidence['adopted'] is True and evidence['common_coordinator'] == str(p.STATE), 'publisher_not_adopted')
        else:
            p.require(evidence.get('adopted') is True or evidence.get('verified_inactive') is True, 'uncoordinated_legacy_publisher_still_possible')
        # Evidence files are genuine original readbacks, never checkbox-only claims.
        for item in evidence['evidence_references']:
            p.require(p.digest(Path(item['path']).read_bytes()) == item['sha256'], 'handoff_evidence_changed')
    state = Path(state)
    with p.legacy.file_lock(p.legacy.LEGACY_REGISTRY.with_name(p.legacy.LEGACY_REGISTRY.name+'.writer.lock')):
        p.legacy.require_legacy_clear(p.legacy.LEGACY_REGISTRY)
        coordinator = p.Coordinator(state, scoped=True)
        with coordinator.connection() as db:
            p.require(not db.execute('SELECT 1 FROM operations WHERE state IN ('+','.join('?'*len(p.ACTIVE))+')',p.ACTIVE).fetchone(), 'unresolved_common_operation_blocks_cutover')
        p.require(not (state/'transition.json').exists(), 'observe_original_transition_no_duplicate_activation')
        p.once(state/'transition.json',dict(state='active',mode=mode,proposal_sha256=approved_hash,
            receipts_sha256=p.digest(Path(receipts_path).read_bytes()),at=p.now(),
            human_decision_reference=actual['human_decision_reference'],publishers=actual['publishers']))
    return p.read(state/'transition.json')


def legacy_entrypoint_guard(lease_reference=None):
    transition = p.STATE/'transition.json'
    if transition.exists() and p.read(transition)['state'] == 'active':
        p.require(lease_reference is not None, 'legacy_publisher_retired_use_scripts_publication_py')
        # A caller cannot mint a lease by passing an environment string.
        row = p.Coordinator().row(lease_reference)
        p.require(row and row['state'] in p.ACTIVE, 'actual_common_operation_claim_required')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action',required=True)
    a=sub.add_parser('prepare');a.add_argument('--out',required=True)
    a=sub.add_parser('activate');a.add_argument('proposal');a.add_argument('--receipts',required=True);a.add_argument('--approved-plan',required=True)
    args=parser.parse_args()
    try:
        print(json.dumps(prepare(args.out) if args.action=='prepare' else activate(args.proposal,args.receipts,args.approved_plan)))
    except Exception as error:
        print(json.dumps(dict(stopped=True,reason=str(error) if isinstance(error,p.Stop) else type(error).__name__)))
        raise SystemExit(1)
