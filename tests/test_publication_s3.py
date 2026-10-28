from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'scripts'))
import publication_s3 as adapter
import routine_release as r


class AssetAdapterTests(unittest.TestCase):
    def setUp(self):
        base=ROOT/'outputs/publication-s3-tests';base.mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=base);self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.objects={'one':'absent','two':'absent'};self.events=[]
        self.boundary=SimpleNamespace(validate=lambda:None,bucket_owner='123456789012',record=lambda:dict(owner='123456789012'),credential_mode='named-profile')
        self.native=SimpleNamespace(Boundary=lambda **kw:self.boundary,load_assets=lambda root:[SimpleNamespace(key=k) for k in self.objects],
            load_reused_assets=lambda root:[],Journal=lambda root:SimpleNamespace(events=self.events),AwsCli=Mock(),
            public_probe=lambda asset,owner:dict(state=self.objects[asset.key]),is_denied=lambda value:value.get('code')=='AccessDenied')
        self.request=dict(operation='a'*32,signature='b'*64,payload=dict(boundary={},workspace=str(self.root),aws_executable='reviewed-aws'))

    def call(self,action):return adapter.operate(action,self.request,self.root,self.native)

    def test_append_only_success_and_exact_reconciliation(self):
        def upload(*args,**kw):
            self.objects.update(one='exact',two='exact');return dict(state='verified')
        self.native.operate=Mock(side_effect=upload)
        self.assertEqual(self.call('observe')['state'],'before');self.call('apply')
        self.assertEqual(self.call('observe')['state'],'applied');self.assertEqual(self.call('check')['state'],'accepted')
        with self.assertRaisesRegex(r.Stop,'never_replayed'):self.call('apply')
        self.assertEqual(self.native.operate.call_count,1)

    def test_lost_response_after_objects_exist_needs_no_repeated_put(self):
        def upload(*args,**kw):
            self.objects.update(one='exact',two='exact');self.events.append(dict(kind='put_intent',intent_id='x',key='one'))
            raise TimeoutError()
        self.native.operate=Mock(side_effect=upload)
        with self.assertRaises(TimeoutError):self.call('apply')
        self.assertEqual(self.call('observe')['state'],'applied');self.assertEqual(self.call('check')['state'],'accepted')
        self.assertEqual(self.native.operate.call_count,1)

    def test_uncertain_cli_result_with_absent_object_keeps_reservation(self):
        def upload(*args,**kw):
            self.events.extend([dict(kind='put_intent',intent_id='x',key='one'),dict(kind='put_result',intent_id='x',outcome=dict(uncertain=True,code='process_timeout'))])
            raise TimeoutError()
        self.native.operate=Mock(side_effect=upload)
        with self.assertRaises(TimeoutError):self.call('apply')
        self.assertEqual(self.call('observe')['state'],'unknown')
        with self.assertRaisesRegex(r.Stop,'original_reconciliation'):self.call('recover')

    def test_known_denial_can_exit_preserving_successful_objects(self):
        def upload(*args,**kw):
            self.objects['one']='exact';self.events.extend([dict(kind='put_intent',intent_id='x',key='two'),
                dict(kind='put_result',intent_id='x',outcome=dict(code='AccessDenied',uncertain=True))]);raise PermissionError()
        self.native.operate=Mock(side_effect=upload)
        with self.assertRaises(PermissionError):self.call('apply')
        self.assertEqual(self.call('observe')['state'],'failed');self.assertEqual(self.call('recover')['state'],'recovered')
        self.assertEqual(self.objects,dict(one='exact',two='absent'))

    def test_foreign_object_is_never_replaced(self):
        self.objects['one']='conflict';self.assertEqual(self.call('observe')['state'],'foreign')


if __name__=='__main__':unittest.main()
