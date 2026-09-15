"""Release selection covers real authenticated consumers and intact fixture cohort."""
import unittest
from tests.private_beta_matching_support import PERSONAS, sources, cohort_case, emit_receipt


class PrivateBetaMatchingTests(unittest.TestCase):
    def test_preselected_inventory_preserves_authorities_and_provider_breadth(self):
        rows=sources()
        self.assertEqual(len(rows),16)
        self.assertEqual(len({r['job_id'] for r in rows}),16)
        self.assertEqual({r['provider'] for r in rows},{'mercor','alignerr','micro1','meridial','synthetic-beta'})
        self.assertEqual(sum(r['observed_at'] is None for r in rows),3)
        self.assertTrue(all(r['body'] and r['body_sha256'] for r in rows))
        network=next(r for r in rows if r.get('original_job_id')==1039)
        self.assertEqual(network['opportunity_kind'],'evergreen_application')
        self.assertEqual(network['include_live'],0)

    def test_targeted_strict_preference_is_actually_evaluated(self):
        result=cohort_case('software_engineer',strict_pay=True)
        emit_receipt('software_engineer_strict_pay',result)
        self.assertEqual(result['status'],200)
        self.assertEqual(result['inventory_count'],16)
        self.assertTrue(result['preferences']['evaluations'])
        self.assertTrue(any(row['admission']['status']!='keep' for row in result['preferences']['evaluations']))
        self.assertNotIn(960014,result['main'])
        self.assertTrue(all(row['detail_status']==(404 if row['job_id']==960000 else 200) for row in result['outcomes']))


def case(name):
    def test(self):
        result=cohort_case(name)
        emit_receipt(name,result)
        self.assertEqual(result['status'],200)
        self.assertEqual(result['inventory_count'],16)
        self.assertTrue(result['projected'],'normal canonical projection executed')
        self.assertIsNotNone(result['preferences'],'normal typed preferences executed')
        self.assertEqual(len(result['outcomes']),16)
        self.assertTrue(all(r['detail_status']==(404 if r['job_id']==960000 else 200) for r in result['outcomes']))
        network=next(r for r in result['outcomes'] if r['job_id']==960000)
        self.assertIsNone(network['main_rank'],'Unavailable network must never have a displayed dead link')
        self.assertIsNone(network['conditional_rank'])
        self.assertIsNone(network['rendered_comparison'])
        self.assertEqual(network['scored'][0]['opportunity_kind'],'evergreen_application')
        self.assertEqual(network['scored'][0]['inventory_model'],'live_feed')
        self.assertEqual(network['scored'][0]['include_in_live_market_estimate'],0)
        self.assertEqual({r['job_id'] for r in result['outcomes'] if r['scored']},
                         {r['job_id'] for r in sources()})
        self.assertEqual(result['other_owner_status'],200)
        self.assertIn(result['anonymous_status'],(303,401))
        # No count/rank target: omitted opportunities are valid outcomes.
        for row in result['outcomes']:
            if row['main_rank'] or row['conditional_rank']:
                self.assertIsNotNone(row['selected_match'])
                self.assertIsNotNone(row['rendered_comparison'])
                self.assertTrue(row['rendered_quotes_present'])
                self.assertEqual(row['rendered_comparison'],row['exact_comparison'])
    return test


for _name in PERSONAS:
    setattr(PrivateBetaMatchingTests,'test_authenticated_'+_name,case(_name))
