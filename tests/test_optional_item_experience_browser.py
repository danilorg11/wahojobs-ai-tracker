"""Opt-in actual Edge interaction using the existing loopback app fixtures."""
import hashlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing
from unittest.mock import patch

from tests.test_optional_item_experience import base
from tests.durable_google_login_browser_test_support import temporary_browser_login_state, _running_https_browser_handler
from tests.test_authenticated_profile_matches import _seed_configured_inventory
from scripts.local_recovery_login import existing_owner_local_login
from wahojobs.durable_product_browser_handler import make_durable_product_browser_handler


@unittest.skipUnless(os.environ.get('WAHOJOBS_BROWSER_NODE'), 'Set WAHOJOBS_BROWSER_NODE and NODE_PATH for the local Edge test')
class OptionalItemExperienceBrowserTests(unittest.TestCase):
    def test_real_authenticated_editor_review_apply_and_details(self):
        with socket.socket() as s:
            s.bind(('127.0.0.1',0));port=s.getsockname()[1]
        with patch('tests.persistent_profiles_repository_test_support.canonical_fixture', side_effect=lambda *a,**k:base()):
            with temporary_browser_login_state(port=port) as state:
                # Reuse the existing small synthetic inventory; no captured or
                # personal source data is needed for this interaction contract.
                _seed_configured_inventory(state.database_path, observed_at=state.clock())
                text='''## Responsibilities

- Evaluate model outputs against a rubric.

## Required

- Experience with R
- Working proficiency in R
'''
                with closing(sqlite3.connect(state.database_path)) as c, c:
                    c.execute("UPDATE jobs SET title='AI Evaluation Reviewer',department='',expertise='',location='Remote - Brazil' WHERE id=7003")
                    c.execute("UPDATE canonical_opportunities SET canonical_title='AI Evaluation Reviewer',source_category='' WHERE id=7002")
                    c.execute("INSERT INTO job_source_contents (job_id,provider,source_type,source_url,external_id,body,body_format,metadata_json,material_content_sha256,first_captured_at,last_captured_at) VALUES (7003,'configured-production','catalog','https://jobs.example.test/distinctive-bilingual-reviewer','configured-distinctive-7003',?,'text/plain','{}',?,?,?)",
                              (text,hashlib.sha256(text.encode()).hexdigest(),state.clock().isoformat(),state.clock().isoformat()))
                output=Path(os.environ.get('WAHOJOBS_BROWSER_OUTPUT') or tempfile.mkdtemp(prefix='waho-experience-screens-'))
                with existing_owner_local_login(state.configuration_path,account_id=state.account_id,clock=state.clock) as (config,app):
                    with _running_https_browser_handler(config,make_durable_product_browser_handler(app)):
                        run=subprocess.run([os.environ['WAHOJOBS_BROWSER_NODE'],str(Path(__file__).with_name('optional_item_experience_browser.cjs')),state.public_origin,str(output)],capture_output=True,text=True,timeout=120)
                        self.assertEqual(run.returncode,0,run.stdout+run.stderr)
                        with closing(sqlite3.connect(state.database_path)) as c:
                            self.assertEqual(c.execute('SELECT max(revision_number) FROM product_profile_revisions').fetchone()[0],2)
                            docs=c.execute('SELECT structured_profile_json FROM product_profile_revisions ORDER BY revision_number').fetchall()
                        old,new=(json.loads(row[0]) for row in docs)
                        self.assertNotIn('item_details',old['experience'])
                        detail=new['experience']['item_details'][0]
                        self.assertEqual((detail['label'],detail['contexts'],detail['months'],detail['autonomy']),('R',['projects','study'],6,'guided'))
                        self.assertEqual(new['experience']['total_years'],20)
                        self.assertEqual(old['languages'],new['languages'])
                        self.assertEqual(old['location'],new['location'])
                print('Anonymous browser evidence:',output)


if __name__=='__main__':unittest.main()
