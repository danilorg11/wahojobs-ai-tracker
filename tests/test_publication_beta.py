from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'scripts'))
import publication_beta as beta


class BetaBatchTests(unittest.TestCase):
    def setUp(self):
        base=ROOT/'outputs/publication-beta-tests';base.mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=base);self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.states=dict(code='before',content='before');self.calls=[];self.committed_content=False
        self.request=dict(operation='a'*32,signature='b'*64,payload=dict(environment='local',resources=['/beta'],
            targets=['code','database'],code=dict(targets=['code']),content=dict(database='database')))
        fixture=self
        def action(part,verb):
            fixture.calls.append((part,verb))
            if verb=='apply':
                if part=='content':fixture.committed_content=True
                fixture.states[part]='applied'
            elif verb=='check':fixture.states[part]='accepted'
            elif verb=='recover':fixture.states[part]='recovered'
            return dict(state=fixture.states[part])
        self.addCleanup(patch.stopall)
        patch.object(beta.data,'content',side_effect=lambda verb,req,root:action('content',verb)).start()
        class Code:
            def __init__(self,request):self.request=request
            def call(self):return action('code',self.request['action'])
        patch.object(beta.files,'Worker',Code).start()

    def call(self,verb):return beta.publication(verb,self.request,self.root)

    def test_code_and_data_have_one_outer_service_lifecycle(self):
        self.assertEqual(self.call('apply')['state'],'applied')
        self.assertEqual(self.call('check')['state'],'accepted')
        self.assertEqual(self.calls.count(('code','apply')),1);self.assertEqual(self.calls.count(('content','apply')),1)
        self.assertTrue(self.committed_content)

    def test_recovery_returns_code_and_keeps_committed_content(self):
        self.call('apply');self.assertEqual(self.call('recover')['state'],'recovered')
        self.assertTrue(self.committed_content)
        self.assertEqual(self.call('observe')['state'],'recovered')

    def test_interruption_between_data_and_code_exits_compatibly(self):
        beta.r.once(self.root/'dispatch.json',dict(intent=True));self.states['content']='applied';self.committed_content=True
        self.assertEqual(self.call('observe')['state'],'failed')
        self.assertEqual(self.call('recover')['state'],'recovered');self.assertTrue(self.committed_content)
        self.assertNotIn(('code','recover'),self.calls)

    def test_foreign_successor_prevents_the_whole_reverse(self):
        self.call('apply');self.states['code']='foreign'
        with self.assertRaises(beta.r.Stop):self.call('recover')
        self.assertNotIn(('content','recover'),self.calls)


if __name__=='__main__':unittest.main()
