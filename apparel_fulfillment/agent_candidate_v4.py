"""Opt-in candidate-search adapter over the unchanged v3 operation contracts."""
from apparel_fulfillment.agent_state_v3 import StateContractAgent
from apparel_fulfillment.candidate_search import VERSION, search_variants
from commerce_lab.retrieval import request_scores


class CandidateSearchAgent(StateContractAgent):
    def __init__(self, *args, candidate_match_mode='any', candidate_scorer=request_scores, **kwargs):
        if candidate_match_mode not in ('all', 'any'):
            raise ValueError('Unknown candidate matching policy')
        super().__init__(*args, **kwargs)
        self.candidate_match_mode = candidate_match_mode
        self.candidate_scorer = candidate_scorer

    def invoke(self, name, args):
        if name == 'search_variants':
            return search_variants(self.world(), args, match_mode=self.candidate_match_mode, scorer=self.candidate_scorer)
        return super().invoke(name, args)

    def trace(self, kind, role, **payload):
        if kind == 'start':
            payload['candidate_search'] = {'version': VERSION, 'match_mode': self.candidate_match_mode}
        return super().trace(kind, role, **payload)

    def run(self, task):
        result = super().run(task)
        result['candidate_search'] = {'version': VERSION, 'match_mode': self.candidate_match_mode}
        return result
