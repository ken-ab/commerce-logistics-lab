"""Use unmodified tau-bench ENV/COMMUNICATE evaluators on actual business traces.

This is a custom commerce domain, not an official leaderboard submission.
No LLM is allowed in replay; actual generation remains in the budgeted app client.
"""
import asyncio
import hashlib
import json
from pathlib import Path

from shopping_agent.types import ShoppingSessionContext
from tau2.data_model.message import AssistantMessage, ToolCall, ToolMessage
from tau2.data_model.tasks import Action, EvaluationCriteria, Task, UserScenario, RewardType
from tau2.environment.environment import Environment
from tau2.environment.toolkit import ToolKitBase, ToolType, is_tool
from tau2.evaluator.evaluator_env import EnvironmentEvaluator
from tau2.evaluator.evaluator_communicate import CommunicateEvaluator

from commerce_lab.agent import CommerceAgent, TOOL_INFO
from evaluation.environment import business_snapshot, canonical, create_case_store

BUSINESS_TOOLS = set(TOOL_INFO) - {n for n in TOOL_INFO if n.startswith('ask_')}


class NoModelInReplay:
    config = {'COMMERCE_MODEL': 'replay-no-llm'}

    def chat(self, *args, **kwargs):
        raise AssertionError('A replay must never generate a model response or incur model fees')


class CommerceReplayTools(ToolKitBase):
    def __init__(self, case, path, policy, *, catalog=None):
        super().__init__()
        self.store, self.session_id = create_case_store(case, path, catalog=catalog)
        self.agent = CommerceAgent(self.store, NoModelInReplay(), policy=policy)
        self.agent.session = ShoppingSessionContext(session_id=self.session_id,
            user_id=self.store.session(self.session_id)['user_id'], timezone='Asia/Shanghai')
        self.agent.run_id = self.store.new_run(self.session_id, 'External evaluator replay: no LLM')

    def perform(self, name, args):
        validated = TOOL_INFO[name][0].model_validate(args).model_dump()
        return canonical(asyncio.run(self.agent.execute_tool(name, validated)))

    def get_db_hash(self):
        return hashlib.sha256(json.dumps(self.snapshot(), sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def snapshot(self):
        return business_snapshot(self.store, self.session_id, self.agent.last_quote)

    @is_tool(ToolType.READ, mutates_state=True)
    def search_products(self, query: str, locale: str = 'us', max_price_usd: float | None = None,
                        color: str | None = None, limit: int = 5):
        """Replay a catalog search, including its seen-product cache effect."""
        return self.perform('search_products', locals_without_self(locals()))

    @is_tool(ToolType.READ, mutates_state=True)
    def get_product_details(self, product_id: str):
        """Replay a catalog read that permits subsequent cart changes."""
        return self.perform('get_product_details', {'product_id': product_id})

    @is_tool(ToolType.READ)
    def get_stock(self, product_id: str):
        """Read synthetic stock; this does not change business state."""
        return self.perform('get_stock', {'product_id': product_id})

    @is_tool(ToolType.READ)
    def get_cart(self):
        """Read the current cart."""
        return self.perform('get_cart', {})

    @is_tool(ToolType.WRITE)
    def set_cart_item(self, product_id: str, quantity: int):
        """Set an absolute quantity through the actual transactional store."""
        return self.perform('set_cart_item', locals_without_self(locals()))

    @is_tool(ToolType.READ, mutates_state=True)
    def quote_shipping(self, destination: str, deadline_days: float, shipping_budget_usd: float,
                       blocked_legs: list[str] | None = None):
        """Replay a quote and retain the last observed quote in the evaluated state."""
        return self.perform('quote_shipping', {**locals_without_self(locals()), 'blocked_legs': blocked_legs or []})

    @is_tool(ToolType.WRITE)
    def stage_order(self, destination: str, deadline_days: float, shipping_budget_usd: float,
                    blocked_legs: list[str] | None = None):
        """Stage a simulation proposal without host confirmation or stock deduction."""
        return self.perform('stage_order', {**locals_without_self(locals()), 'blocked_legs': blocked_legs or []})

    @is_tool(ToolType.READ)
    def search_policies(self):
        """Read current research policies."""
        return self.perform('search_policies', {})

    @is_tool(ToolType.READ)
    def get_orders(self):
        """Read only this case's orders."""
        return self.perform('get_orders', {})

    @is_tool(ToolType.READ)
    def get_order(self, order_id: str):
        """Read a single order belonging to this case."""
        return self.perform('get_order', {'order_id': order_id})


def locals_without_self(values):
    return {k: v for k, v in values.items() if k != 'self'}


def trajectory(record):
    messages, omitted_rejected = [], []
    for t in record['traces']:
        if t['kind'] != 'tool_result':
            continue
        p = t['payload']
        if p['name'] not in BUSINESS_TOOLS:
            # Specialist delegation is represented by its actual child tool events;
            # replaying the parent would execute those effects twice.
            continue
        if isinstance(p['output'], dict) and p['output'].get('error'):
            # Schema rejection / rolled-back business errors have no state effects.
            # Keep their original evidence and count. Live-vs-replayed state is an
            # additional gate, so any hidden effect makes evaluation fail closed.
            omitted_rejected.append(p)
            continue
        ident = 'trace-' + str(t['id'])
        messages.append(AssistantMessage(role='assistant', tool_calls=[ToolCall(
            id=ident, name=p['name'], arguments=p['arguments'])]))
        messages.append(ToolMessage(id=ident, role='tool',
            content=Environment.to_json_str(canonical(p['output']))))
    messages.append(AssistantMessage(role='assistant', content=json.dumps(
        record.get('result', {}).get('report', {}), ensure_ascii=False)))
    return messages, omitted_rejected


def evaluate_record(case, record, live_snapshot, output_dir: Path, *, policy, catalog=None):
    output_dir.mkdir(parents=True, exist_ok=False)
    constructed = []

    def factory(solo_mode=False):
        if solo_mode:
            raise ValueError('This commerce adapter uses explicit host confirmation')
        kit = CommerceReplayTools(case, output_dir / f'replay_{len(constructed)}.sqlite', policy, catalog=catalog)
        env = Environment('commerce-logistics-custom-v1', 'Local research simulation; preserve user constraints.', tools=kit)
        constructed.append(kit)
        return env

    task = Task(id=case['id'], user_scenario=UserScenario(instructions=case['task']),
        evaluation_criteria=EvaluationCriteria(
            actions=[Action(action_id=f'gold-{i}', **a) for i, a in enumerate(case['gold_actions'])],
            communicate_info=case.get('communicate_info', []),
            reward_basis=[RewardType.DB, RewardType.COMMUNICATE]))
    messages, rejected = trajectory(record)
    env_result = EnvironmentEvaluator.calculate_reward(factory, task, messages, strict_replay=True)
    comm_result = CommunicateEvaluator.calculate_reward(task, messages)
    replay_matches = constructed[0].snapshot() == live_snapshot
    report = (record.get('result') or {}).get('report', {})
    completed = record['status'] == 'completed'
    status_ok = report.get('status') in case.get('allowed_statuses', ['completed'])
    result = {'framework': 'tau-bench', 'domain': 'commerce-logistics-custom-v1',
        'scope': 'Strict replay plus projected business end-state; not an official benchmark or complete narrative evaluator.',
        'db': env_result.model_dump(mode='json'), 'communication': comm_result.model_dump(mode='json'),
        'live_replay_state_matches': replay_matches, 'run_completed': completed, 'report_status_allowed': status_ok,
        'rejected_tool_calls_without_effects': len(rejected),
        'passed': bool(env_result.reward == 1 and comm_result.reward == 1 and replay_matches and completed and status_ok)}
    (output_dir / 'score.json').write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    (output_dir / 'task.json').write_text(task.model_dump_json(indent=2)+'\n', encoding='utf-8')
    (output_dir / 'trajectory.json').write_text(json.dumps([m.model_dump(mode='json') for m in messages],ensure_ascii=False,indent=2)+'\n', encoding='utf-8')
    return result
