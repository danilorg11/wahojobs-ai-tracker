"""No AWS/HTTP: prove adoption cannot forget the original uploader journal."""
from contextlib import contextmanager
from pathlib import Path
import sys,unittest,uuid
from types import SimpleNamespace
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import publication_s3 as s
import routine_release as r

class S3JournalTests(unittest.TestCase):
    def fixture(self,events):
        base=ROOT/'outputs/publication-s3-tests'/uuid.uuid4().hex;base.mkdir(parents=True)
        module=base/'outputs/remote-companies-20261021/release-preparation/upload_company_logos.py'
        journal=module.parent/'aws-logo-receipts';journal.mkdir(parents=True)
        worker=base/'new-common-worker';worker.mkdir()
        calls=[];asset=SimpleNamespace(key='immutable.png')
        @contextmanager
        def exclusive(path):
            calls.append(('lock',path));yield
        def Journal(path):
            calls.append(('journal',path));return SimpleNamespace(events=events,directory=path)
        def operate(*args,**kwargs):calls.append(('put-dispatch',None));return dict(state='mocked')
        native=SimpleNamespace(__file__=str(module),exclusive=exclusive,Journal=Journal,Boundary=lambda **kw:SimpleNamespace(**kw,validate=lambda:None,record=lambda:kw),load_assets=lambda _: [asset],load_reused_assets=lambda _:[],public_probe=lambda *_:dict(state='absent'),is_denied=lambda _:False,AwsCli=lambda *a,**kw:None,operate=operate)
        request=dict(operation='a'*32,signature='b'*64,payload=dict(workspace=str(base),boundary=dict(profile='fixture',caller_account='300567346412',caller_arn='arn:aws:iam::300567346412:root',bucket_owner='300567346412',credential_mode='named-profile'),aws_executable='mock'))
        return worker,journal,native,request,calls
    def test_uncertain_legacy_put_is_observed_in_original_journal(self):
        root,journal,native,request,calls=self.fixture([dict(kind='put_intent',intent_id='original',key='immutable.png')])
        self.assertEqual(s.operate('observe',request,root,native)['state'],'unknown')
        self.assertEqual(calls,[('lock',journal),('journal',journal)])
        self.assertFalse((root/'native-journal').exists())
    def test_direct_apply_cannot_duplicate_uncertain_legacy_put(self):
        root,journal,native,request,calls=self.fixture([dict(kind='put_intent',intent_id='original',key='immutable.png')])
        with self.assertRaisesRegex(r.Stop,'original_uncertain_native_put'):s.operate('apply',request,root,native)
        self.assertFalse(any(name=='put-dispatch' for name,_ in calls));self.assertFalse((root/'native-dispatch.json').exists())
    def test_fresh_dispatch_shares_native_exclusive_and_journal(self):
        root,journal,native,request,calls=self.fixture([])
        self.assertEqual(s.operate('apply',request,root,native)['state'],'applied')
        self.assertEqual(calls[:2],[('lock',journal),('journal',journal)])
        self.assertEqual(calls.count(('put-dispatch',None)),1)
    def test_copied_cloudshell_workspace_cannot_create_a_new_history(self):
        root,journal,native,request,calls=self.fixture([])
        request['payload']['boundary']['credential_mode']='cloudshell-managed'
        with self.assertRaisesRegex(r.Stop,'original_cloudshell_workspace'):s.operate('observe',request,root,native)
        self.assertEqual(calls,[])

if __name__=='__main__':unittest.main()
