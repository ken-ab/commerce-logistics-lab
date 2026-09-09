"""Source review with the existing UI's selection and approval protections."""
from apparel_fulfillment.agent_source_v5 import SourceReviewAgent
from apparel_fulfillment.interactive_state import InteractiveOperationAgent

UI_POLICY = 'apparel-interactive-product-sources-v1'


class InteractiveSourceOperationAgent(InteractiveOperationAgent, SourceReviewAgent):
    def __init__(self, *args, **kwargs):
        if kwargs.get('phase') == 'interactive_state_v3':
            kwargs['phase'] = 'interactive_product_sources_v1'
        super().__init__(*args, **kwargs)

    def run(self, task):
        result = super().run(task)
        result['interactive_base_policy'] = result['policy_version']
        result['policy_version'] = UI_POLICY
        return result
