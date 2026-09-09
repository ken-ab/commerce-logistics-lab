"""UI adapter for proposal-review v6 with existing selection protections."""
from apparel_fulfillment.agent_reliability_v6 import ReliabilityAgent
from apparel_fulfillment.interactive_sources import InteractiveSourceOperationAgent
from apparel_fulfillment.interactive_state import InteractiveOperationAgent

UI_POLICY = 'apparel-interactive-proposal-reliability-v1'


class InteractiveReliabilityOperationAgent(InteractiveOperationAgent, ReliabilityAgent):
    def __init__(self, *args, **kwargs):
        if kwargs.get('phase') == 'interactive_state_v3':
            kwargs['phase'] = 'interactive_proposal_reliability_v1'
        super().__init__(*args, **kwargs)

    def run(self, task):
        result = super().run(task)
        result['interactive_base_policy'] = result['policy_version']
        result['policy_version'] = UI_POLICY
        return result


def scoped_operation_agent(*args, **kwargs):
    """Only the operation evaluated in the 144-run study uses the v6 loop."""
    contract = kwargs.get('contract')
    mode = contract.get('mode') if isinstance(contract, dict) else getattr(contract, 'mode', None)
    factory = InteractiveReliabilityOperationAgent if mode == 'review_proposal' else InteractiveSourceOperationAgent
    return factory(*args, **kwargs)
