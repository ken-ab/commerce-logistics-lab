"""Opt-in source-material guidance and completion checks over frozen v4/v3."""
from copy import deepcopy

from apparel_fulfillment.agent_candidate_v4 import CandidateSearchAgent
from apparel_fulfillment.source_review import GUIDE, VERSION, full_material, review_sources


class SourceReviewAgent(CandidateSearchAgent):
    def source_status(self):
        return review_sources(self.contract, self.view(), self.observations, self.world())

    def invoke(self, name, args):
        if name == 'read_variant':
            # Unlike the old tool's 1200-character excerpt, this version returns the
            # complete stored record. The current catalogue descriptions are <=458 chars.
            return full_material(self.world(), args['sku']) | {
                'notice': 'Public product record is complete; units where marked, stock, rules and weights are simulation.'}
        return super().invoke(name, args)

    def observed(self, name, args, role):
        value = super().observed(name, args, role)
        status = self.source_status()
        guidance = {'guide': GUIDE, **status}
        value['source_review'] = guidance
        self.observations[value['observation_id']]['source_review'] = deepcopy(guidance)
        self.trace('source_directory', role, observation_id=value['observation_id'], review=deepcopy(status))
        return value

    def check_completion(self, candidate):
        errors = super().check_completion(candidate)
        review = self.source_status()
        source_errors = [{'field': 'source_review', 'reason': 'current_product_sources_not_read',
            'detail': 'Read complete current material using read_variant for these SKUs before finishing: ' +
                      ', '.join(item['sku'] for item in review['missing'])}] if review['missing'] else []
        errors.extend(source_errors)
        candidate['operation_check']['errors'] = deepcopy(errors)
        candidate['operation_check']['passed'] = not errors
        candidate['source_review'] = deepcopy(review)
        candidate['execution_receipt']['source_review'] = deepcopy(review)
        self.trace('source_check', self.arm, enforced=self.enforce_contract, errors=source_errors, review=deepcopy(review))
        return errors

    def run(self, task):
        result = super().run(task)
        result['base_policy_version'] = result['policy_version']
        result['policy_version'] = VERSION
        result['source_review'] = self.source_status()
        return result
