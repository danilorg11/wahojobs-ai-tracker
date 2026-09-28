"""Canonical byte parity and complete source-snapshot coverage for streamed SHA."""
from contextlib import closing
from hashlib import sha256
import json
import sqlite3
import unittest
from unittest.mock import patch

from wahojobs import evidence_maintenance as maintenance
from tests import test_evidence_maintenance as existing
from tests.evidence_maintenance_support import source_execute, T0


class SourceFingerprintStreamingTests(unittest.TestCase):
    def test_identical_canonical_bytes_for_nested_unicode_numbers_null_and_escapes(self):
        materials = [None, {}, [], dict(missing='não-configurada'),
            {'z': [None, True, False, 0, -1, 2**70, 1.25, -0.0, 1e-200, 1e200],
             'á': {'quote': '"\\\n\r\t\b\f\u0000', '中文': 'ação café e\u0301 🌎',
                   'array': [{str(index): ['x', index / 7, None]} for index in range(200)]},
             'a': 'full body α' * 10000}]
        for material in materials:
            with self.subTest(kind=type(material).__name__):
                before = maintenance.encoded(material)
                pieces = list(json.JSONEncoder(sort_keys=True, ensure_ascii=False,
                    separators=(',', ':'), allow_nan=False).iterencode(material))
                self.assertEqual(b''.join(piece.encode('utf-8') for piece in pieces), before)
                self.assertEqual(maintenance._source_fingerprint_digest(material), sha256(before).hexdigest())
                self.assertEqual(maintenance._source_fingerprint_digest(material), maintenance.digest(material))
        for number in (float('nan'), float('inf'), -float('inf')):
            with self.subTest(number=number):
                with self.assertRaises(ValueError):
                    maintenance.digest({'bad': [number]})
                with self.assertRaises(ValueError):
                    maintenance._source_fingerprint_digest({'bad': [number]})

    def test_streamed_helper_never_builds_a_complete_encoded_copy(self):
        material = {'history': [{'body': 'observação ' * 1000, 'id': index} for index in range(20)]}
        expected = maintenance.digest(material)
        with patch.object(maintenance, 'encoded', side_effect=AssertionError('whole JSON allocation')):
            self.assertEqual(maintenance._source_fingerprint_digest(material), expected)


class SourceFingerprintDatabaseParityTests(unittest.TestCase):
    setUp = existing.MaintenanceTests.setUp

    def test_real_database_footprint_hash_matches_previous_implementation_and_reads_only(self):
        source_execute(self.path, self.journal, T0)
        before = self.path.read_bytes()
        def previous(connection, slug):
            company = connection.execute('SELECT * FROM companies WHERE slug=?', (slug,)).fetchone()
            if company is None:
                return maintenance.digest(dict(missing=slug))
            company_id = company['id']
            material = dict(company=dict(company))
            for table in ('jobs', 'crawl_runs', 'canonical_opportunities'):
                material[table] = [dict(row) for row in connection.execute(
                    'SELECT * FROM ' + table + ' WHERE company_id=? ORDER BY id', (company_id,))]
            for table in ('job_source_contents', 'job_source_content_acceptances', 'job_source_content_captures'):
                material[table] = [dict(row) for row in connection.execute(
                    'SELECT s.* FROM ' + table + ' s JOIN jobs j ON j.id=s.job_id WHERE j.company_id=? ORDER BY s.rowid', (company_id,))]
            for table in ('opportunity_enrichments', 'opportunity_enrichment_overrides'):
                material[table] = [dict(row) for row in connection.execute(
                    'SELECT e.* FROM ' + table + ' e JOIN canonical_opportunities co ON co.id=e.canonical_opportunity_id WHERE co.company_id=? ORDER BY e.rowid', (company_id,))]
            return maintenance.digest(material)
        with maintenance.read_connection(self.path) as connection:
            for slug in ('alignerr', 'mercor', 'não-configurada'):
                with self.subTest(source=slug):
                    self.assertEqual(maintenance.source_fingerprint(connection, slug), previous(connection, slug))
            original = maintenance.source_fingerprint(connection, 'alignerr')
        self.assertEqual(self.path.read_bytes(), before)
        # A historical payload change still participates in the exact fingerprint;
        # this encoder does not rely on a row's stored hash as a substitute.
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("UPDATE job_source_content_captures SET body=body || ' observação Ω' WHERE id=(SELECT min(id) FROM job_source_content_captures)")
        with maintenance.read_connection(self.path) as connection:
            self.assertNotEqual(maintenance.source_fingerprint(connection, 'alignerr'), original)
            self.assertEqual(maintenance.source_fingerprint(connection, 'alignerr'), previous(connection, 'alignerr'))


if __name__ == '__main__':
    unittest.main()
