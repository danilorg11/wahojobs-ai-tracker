"""Synthetic database contract fixture; current source evidence is replayed separately."""
import json
import sqlite3
import unittest

from wahojobs.canonical.service import sync_handshake_canonical_opportunities


class HandshakeCanonicalContractTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            CREATE TABLE jobs (id INTEGER PRIMARY KEY, company_id INTEGER,
                external_id TEXT, title TEXT, expertise TEXT, department TEXT,
                first_seen_at TEXT, last_seen_at TEXT, is_active INTEGER,
                semantic_authority_state TEXT, canonical_opportunity_id INTEGER);
            CREATE TABLE job_source_content_captures (id INTEGER PRIMARY KEY,
                provider TEXT, record_promotion_contract_id TEXT,
                promotion_decision TEXT, authority_evidence_json TEXT);
            CREATE TABLE job_source_content_acceptances
                (job_id INTEGER PRIMARY KEY, accepted_capture_id INTEGER);
            CREATE TABLE canonical_opportunities (id INTEGER PRIMARY KEY,
                company_id INTEGER, canonical_key TEXT, canonical_title TEXT,
                normalized_title TEXT, source_category TEXT, language TEXT,
                language_locale TEXT, first_seen_at TEXT, last_seen_at TEXT,
                is_active INTEGER, variant_count INTEGER, updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
        ''')

    def tearDown(self):
        self.db.close()

    def add(self, job_id, cms_id, title, application_id):
        self.db.execute('INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?)',
            (job_id, 1, f'handshake::{cms_id}', title, 'AI', 'AI',
             '2026-09-24T00:00:00Z', '2026-09-24T00:00:00Z', 1,
             'versioned_accepted', None))
        self.db.execute('INSERT INTO job_source_content_captures VALUES (?,?,?,?,?)',
            (job_id, 'handshake', 'handshake_public_cms_record_v1', 'promoted',
             json.dumps({'cms_id':cms_id, 'application_job_id':application_id})))
        self.db.execute('INSERT INTO job_source_content_acceptances VALUES (?,?)',
                        (job_id, job_id))

    def test_shared_application_has_two_distinct_variants_and_reassignment_closes_old_group(self):
        self.add(1, 'cms-one', 'Finance Professional', '123')
        self.add(2, 'cms-two', 'Corporate Finance Analyst', '123')
        self.assertEqual(sync_handshake_canonical_opportunities(self.db, 1), 2)
        rows = self.db.execute('SELECT id, canonical_opportunity_id FROM jobs ORDER BY id').fetchall()
        self.assertEqual(rows[0]['canonical_opportunity_id'], rows[1]['canonical_opportunity_id'])
        self.assertEqual(self.db.execute('SELECT variant_count FROM canonical_opportunities').fetchone()[0], 2)
        self.db.execute('UPDATE job_source_content_captures SET authority_evidence_json=? WHERE id IN (1,2)',
                        (json.dumps({'cms_id':'cms-one', 'application_job_id':'456'}),))
        # Update the second CMS identity independently; each remains its own variant.
        self.db.execute('UPDATE job_source_content_captures SET authority_evidence_json=? WHERE id=2',
                        (json.dumps({'cms_id':'cms-two', 'application_job_id':'456'}),))
        sync_handshake_canonical_opportunities(self.db, 1)
        old = self.db.execute("SELECT is_active,variant_count FROM canonical_opportunities WHERE canonical_key='handshake::hai_job::123'").fetchone()
        new = self.db.execute("SELECT is_active,variant_count FROM canonical_opportunities WHERE canonical_key='handshake::hai_job::456'").fetchone()
        self.assertEqual(tuple(old), (0, 0))
        self.assertEqual(tuple(new), (1, 2))


if __name__ == '__main__':
    unittest.main()
