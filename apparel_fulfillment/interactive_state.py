"""Validate an operator's explicit intent before any paid interactive request."""
from contextlib import closing

from pydantic import ValidationError
from apparel_fulfillment.agent_state_v3 import StateContractAgent

from apparel_fulfillment.action_contract import TaskContract, reference_proposal
from apparel_fulfillment.orders import OrderError


UI_POLICY = 'apparel-interactive-operation-v1'


class InteractiveOperationAgent(StateContractAgent):
    """UI operation permissions, separate from the frozen research agent."""

    def invoke(self, name, args):
        if name == 'select_variants' and self.contract.mode in ('prepare_proposal', 'review_proposal'):
            raise OrderError('This operation preserves current selections and their specific approvals. '
                             'Read the current order and prepare/review its proposal; use a separate '
                             'stage_candidate operation if the operator wants another product.')
        return super().invoke(name, args)

    def trace(self, kind, role, **payload):
        if kind == 'start':
            payload['interactive_operation_policy'] = UI_POLICY
            payload['preserve_current_selections'] = self.contract.mode in ('prepare_proposal', 'review_proposal')
        return super().trace(kind, role, **payload)

    def run(self, task):
        result = super().run(task)
        result['base_policy_version'] = result['policy_version']
        result['policy_version'] = UI_POLICY
        return result


def checked_operation(store, owner, draft_id, operation):
    if operation is None:
        return None
    try:
        contract = TaskContract.model_validate(operation)
    except ValidationError as error:
        raise OrderError('请选择完整且一致的本次操作参数。') from error
    view = store.view(owner, draft_id)
    if view['confirmation'] and contract.mode not in ('inspect_product', 'check_order'):
        raise OrderError('这笔模拟订单已经确认；请创建新订单再进行修改。')
    if contract.mode == 'inspect_product':
        with closing(store.connect()) as db:
            known = contract.product_sku in store._world(db)['variants']
        if not known:
            raise OrderError('请选择有来源记录的服装商品。')
    if contract.mode == 'stage_candidate' and contract.line_id not in {l['line_id'] for l in view['request']['lines']}:
        raise OrderError('请选择当前订单中存在的明细行。')
    if contract.mode == 'review_proposal':
        try:
            reference_proposal(contract, view)
        except OrderError:
            raise OrderError('当前订单还没有该提案，请先准备提案。') from None
    return contract.model_dump()
