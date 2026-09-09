"""Independent numerical and acceptance-boundary checks; no API calls."""
import unittest
from model_selection_100.validation_readout import paired_interval, replacement_gate


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.reference = {'complete':True, 'valid_responses':150, 'identity_match_rate':1,
                          'avg_cost_cny':.003, 'overall_score':80}
        self.candidate = dict(self.reference, avg_cost_cny=.002, overall_score=81)
        self.contrasts = {'ndcg_at_10':{'lower_95':-.02}, 'hit_exact_at_1':{'lower_95':-.03}}

    def gate(self, **changes):
        return replacement_gate(dict(self.candidate, **changes), self.reference, self.contrasts)

    def test_pairing_retains_exact_constant_difference(self):
        # Variation shared by both models must cancel rather than inflate an unpaired interval.
        result = paired_interval([.2,.3,.8], [.1,.2,.7], resamples=200)
        for key in ('mean_delta','lower_95','upper_95'):
            self.assertAlmostEqual(result[key], .1)

    def test_noninferiority_boundaries_and_strict_score(self):
        self.assertTrue(self.gate()['replace_current_model'])
        self.assertFalse(self.gate(overall_score=80)['replace_current_model'])
        self.contrasts['hit_exact_at_1']['lower_95']=-.030001
        self.assertFalse(self.gate()['replace_current_model'])

    def test_premium_requires_strict_positive_ndcg_lower_bound(self):
        self.contrasts['ndcg_at_10']['lower_95']=0
        self.assertFalse(self.gate(avg_cost_cny=.004)['replace_current_model'])
        self.contrasts['ndcg_at_10']['lower_95']=.00001
        self.assertTrue(self.gate(avg_cost_cny=.004)['replace_current_model'])

    def test_failures_identity_and_missing_cases_block_adoption(self):
        self.assertFalse(self.gate(valid_responses=146)['replace_current_model'])
        self.assertTrue(self.gate(valid_responses=147)['replace_current_model'])
        self.assertFalse(self.gate(identity_match_rate=149/150)['replace_current_model'])
        self.assertFalse(self.gate(complete=False)['replace_current_model'])

    def test_same_luna_never_claims_superiority_to_its_old_protocol(self):
        result=replacement_gate(self.reference,self.reference,{},same_model=True)
        self.assertFalse(result['replace_current_model'])
        self.assertEqual(result['decision'],'retain_current_luna_model')

    def test_reproducible_bootstrap_and_pair_membership_validation(self):
        a=[0.,1.,0.,1.]; b=[0.,0.,0.,1.]
        self.assertEqual(paired_interval(a,b,resamples=200),paired_interval(a,b,resamples=200))
        with self.assertRaises(ValueError): paired_interval(a,b[:-1])


if __name__ == '__main__': unittest.main()
