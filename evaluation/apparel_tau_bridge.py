"""Strict, offline tau2 replay of frozen apparel operations (custom domain).

The adapter never uses saved business outputs as tool implementations. Only a
new object's opaque UUID is supplied to the actual store's UUID factory.
"""
from contextlib import closing
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from types import SimpleNamespace
from unittest.mock import patch
import uuid

from apparel_fulfillment.action_contract import reference_proposal
from apparel_fulfillment.agent import BUSINESS_TOOLS
from apparel_fulfillment.agent_source_v5 import SourceReviewAgent
from apparel_fulfillment.agent_reliability_v6 import ReliabilityAgent
from apparel_fulfillment.store import ApparelStore
from apparel_fulfillment.transport import instant
from tau2.data_model.message import AssistantMessage, ToolCall, ToolMessage
from tau2.data_model.tasks import Action, EvaluationCriteria, RewardType, Task, UserScenario
from tau2.environment.environment import Environment
from tau2.environment.toolkit import ToolKitBase, ToolType, is_tool
from tau2.evaluator.evaluator_env import EnvironmentEvaluator


VERSION = 'apparel-tau-offline-replay-v1'
ENVELOPE = ('observation_id', 'tool', 'success', 'result')
TABLES = ('metadata', 'inventory', 'drafts', 'approvals', 'proposals', 'transport_events', 'confirmations')
JSON_COLUMNS = {'drafts': ('request', 'selections'), 'approvals': ('payload',),
                'proposals': ('payload',), 'transport_events': ('payload',), 'confirmations': ('payload',)}


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(encoded(value).encode('utf-8')).hexdigest()


def sealed_connection(path):
    """Do not alter a frozen WAL shared-memory file while reading its main DB."""
    path = Path(path).resolve()
    wal = Path(str(path) + '-wal')
    if wal.exists() and wal.stat().st_size:
        raise ValueError('Snapshot has an uncheckpointed WAL; inspect it before replay')
    return sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True)


def snapshot(path):
    """All persistent business fields; no projection onto just expected values."""
    result = {}
    with closing(sealed_connection(path)) as db:
        db.row_factory = sqlite3.Row
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        if tables != set(TABLES) | {'traces'}:
            raise ValueError('Business schema changed; review the snapshot scope before replay')
        for table in TABLES:
            rows = [dict(row) for row in db.execute('SELECT * FROM ' + table)]
            for row in rows:
                for column in JSON_COLUMNS.get(table, ()):
                    row[column] = json.loads(row[column])
            result[table] = sorted(rows, key=encoded)
    return result


def canonical_target(state):
    """Rename proposal identities, never erase identity or version relationships."""
    identities = {}
    for row in state['proposals']:
        if row['id'] in identities:
            raise ValueError('Duplicate proposal identity')
        identities[row['id']] = 'proposal:' + row['draft_id'] + ':v' + str(row['version'])

    def rename(value):
        if isinstance(value, str):
            return identities.get(value, value)
        if isinstance(value, dict):
            return {key: rename(item) for key, item in value.items()}
        if isinstance(value, list):
            return [rename(item) for item in value]
        return value

    return {table: sorted((rename(row) for row in rows), key=encoded) for table, rows in state.items()}


class NoModel:
    def chat(self, *args, **kwargs):
        raise AssertionError('Model calls are forbidden in offline replay')


def no_scorer(*args, **kwargs):
    raise AssertionError('GPU/network scoring is outside this replay protocol')


def clone_initial(initial, destination):
    destination = Path(destination)
    if destination.exists():
        raise FileExistsError('Replay database must be a fresh isolated copy')
    destination.parent.mkdir(parents=True, exist_ok=True)
    with closing(sealed_connection(Path(initial) / 'operations.sqlite')) as source:
        with closing(sqlite3.connect(destination)) as target:
            source.backup(target)
    return ApparelStore(destination, world=read(Path(initial) / 'world.json'))


def executed_steps(execution):
    steps = [deepcopy(t) for t in execution['traces'] if t['kind'] == 'tool']
    if len(steps) != execution['tool_calls'] or sum(t['success'] is True for t in steps) != execution['successful_tool_calls']:
        raise ValueError('Executed tool counts differ from the frozen record')
    for i, step in enumerate(steps, 1):
        if step['tool'] not in BUSINESS_TOOLS:
            raise ValueError('Unknown executed business tool')
        if type(step['success']) is not bool or step['observation_id'] != 'O-' + str(i):
            raise ValueError('Malformed or out-of-sequence observation envelope')
        args = BUSINESS_TOOLS[step['tool']][0].model_validate(step['arguments']).model_dump()
        if encoded(args) != encoded(step['arguments']):
            raise ValueError('Recorded arguments do not match the original tool schema')
        if step['role'] not in {execution['arm'], 'runtime', 'product', 'logistics', 'service'}:
            raise ValueError('Unknown execution role')
    return steps


def reference_actions(case, before, seed):
    """Use pre-registered task outcomes only, independent of the recorded policy."""
    expected = case['expected']['proposal']
    if expected in ('none', 'keep'):
        return []
    if expected not in ('revise', 'infeasible') or case['contract']['mode'] != 'review_proposal':
        raise ValueError('Unsupported reference task; a new protocol is required')
    calls = [('read_proposal', {'proposal_id': seed['old_id']}),
             ('prepare_proposal', {'expected_revision': before['revision']})]
    return [Action(action_id='reference-' + str(i), name='apparel_operation',
                   arguments={'name': name, 'arguments': args, 'actor': 'runtime'})
            for i, (name, args) in enumerate(calls, 1)]


class ApparelReplayTools(ToolKitBase):
    def __init__(self, case, initial, destination, version, arm, steps=None):
        super().__init__()
        if version not in ('v5', 'v6'):
            raise ValueError('Unknown frozen agent version')
        self.store = clone_initial(initial, destination)
        self.seed = read(Path(initial) / 'seed.json')
        self.before = self.store.view(case['owner'], self.seed['draft_id'])
        cls = SourceReviewAgent if version == 'v5' else ReliabilityAgent
        self.agent = cls(self.store, case['owner'], self.seed['draft_id'], client=NoModel(),
                         arm=arm, now=instant(case['now']), contract=case['contract'],
                         candidate_scorer=no_scorer, phase='offline_tau_replay')
        self.agent.task = case['task']
        self.agent.initial_view = deepcopy(self.before)
        self.agent.reference_id = reference_proposal(self.agent.contract, self.before)
        self.steps = steps
        self.executed = []
        self.allocations = []
        self.existing_ids = {row['id'] for row in snapshot(self.store.path)['proposals']}

    def allocate_uuid(self):
        index = len(self.executed)
        if self.steps is None:
            ident = 'PROP-' + uuid.uuid5(uuid.NAMESPACE_URL, self.seed['draft_id'] + ':tau-reference:' + str(len(self.allocations))).hex
        else:
            step = self.steps[index]
            # No tool result fields other than the opaque creation ID enter execution.
            ident = step['result'].get('proposal_id') if step['success'] else None
            if step['tool'] != 'prepare_proposal' or not isinstance(ident, str) or not re.fullmatch(r'PROP-[0-9a-f]{32}', ident):
                raise ValueError('A real creation lacks a valid recorded opaque identity')
        if ident in self.existing_ids:
            raise ValueError('A generated proposal identity aliases an existing object')
        self.existing_ids.add(ident)
        self.allocations.append({'step': index + 1, 'proposal_id': ident})
        return uuid.UUID(ident.removeprefix('PROP-'))

    @is_tool(ToolType.GENERIC, mutates_state=True)
    def apparel_operation(self, name: str, arguments: dict, actor: str):
        """Execute one real apparel operation and rebuild its observation state.

        Args:
            name: Original business tool name.
            arguments: Original validated tool arguments.
            actor: Recorded root, runtime or expert role.
        """
        if name not in BUSINESS_TOOLS:
            raise ValueError('Unknown business tool')
        args = BUSINESS_TOOLS[name][0].model_validate(arguments).model_dump()
        if actor not in {self.agent.arm, 'runtime', 'product', 'logistics', 'service'}:
            raise ValueError('Unknown role')
        if self.steps is not None:
            if len(self.executed) >= len(self.steps):
                raise ValueError('More replay operations than recorded')
            step = self.steps[len(self.executed)]
            if (name, encoded(args), actor) != (step['tool'], encoded(step['arguments']), step['role']):
                raise ValueError('Replay call differs from the recorded operation')
        # Replace only this module's UUID provider, in this isolated process and
        # for the duration of a synchronous call. No production source edits.
        with patch('apparel_fulfillment.store.uuid', SimpleNamespace(uuid4=self.allocate_uuid)):
            value = self.agent.observed(name, args, actor)
        envelope = {key: deepcopy(value[key]) for key in ENVELOPE}
        self.executed.append({'name': name, 'arguments': args, 'actor': actor, 'output': envelope})
        return envelope

    def snapshot(self):
        return snapshot(self.store.path)

    def get_db_hash(self):
        return digest(canonical_target(self.snapshot()))


def trajectory(steps):
    messages = []
    for i, step in enumerate(steps, 1):
        ident = 'apparel-step-' + str(i)
        messages.append(AssistantMessage(role='assistant', tool_calls=[ToolCall(
            id=ident, name='apparel_operation', arguments={'name': step['tool'],
            'arguments': step['arguments'], 'actor': step['role']}, requestor='assistant')]))
        messages.append(ToolMessage(role='tool', id=ident, name='apparel_operation',
            content=Environment.to_json_str({key: step[key] for key in ENVELOPE})))
    return messages


def evaluate_record(case, execution, initial, live_path, output, version):
    """Keep replay fidelity, task DB reward, and original report acceptance separate."""
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    case = {**case, 'owner': execution['before']['owner']}
    steps = executed_steps(execution)
    seed = read(Path(initial) / 'seed.json')
    constructed = []

    def factory(solo_mode=False):
        if len(constructed) >= 2:
            raise ValueError('Unexpected extra environment construction')
        kit = ApparelReplayTools(case, initial, output / ('predicted.sqlite' if not constructed else 'reference.sqlite'),
                                 version, execution['arm'], steps if not constructed else None)
        if encoded(kit.before) != encoded(execution['before']):
            raise ValueError('Initial database differs from recorded start state')
        constructed.append(kit)
        return Environment(domain_name=VERSION, tools=kit,
            policy='Custom offline apparel replay; no model, payment or shipment.', solo_mode=solo_mode)

    task = Task(id=case['id'], user_scenario=UserScenario(instructions=case['task']),
        evaluation_criteria=EvaluationCriteria(actions=reference_actions(case, execution['before'], seed),
                                               reward_basis=[RewardType.DB]))
    messages = trajectory(steps)
    reward, replay_error = None, None
    try:
        reward = EnvironmentEvaluator.calculate_reward(factory, task, messages, strict_replay=True)
    except (ValueError, TypeError, AssertionError) as error:
        # Preserve failures without echoing whole potentially large tool materials.
        replay_error = {'type': type(error).__name__, 'detail': str(error)[:600]}
    predicted = constructed[0].snapshot() if constructed else None
    live = snapshot(live_path)
    reference = constructed[1].snapshot() if len(constructed) == 2 else None
    expected_outputs = [{key: t[key] for key in ENVELOPE} for t in steps]
    actual_outputs = [t['output'] for t in constructed[0].executed] if constructed else []
    exact_outputs = encoded(expected_outputs) == encoded(actual_outputs)
    reference_ok = reference is not None and len(constructed[1].executed) == len(task.evaluation_criteria.actions)
    reference_ok = reference_ok and all(t['output']['success'] for t in constructed[1].executed)
    result = {'case_id': case['id'], 'version': version, 'arm': execution['arm'],
        'strict_replay_passed': replay_error is None and reward is not None,
        'exact_output_types_passed': exact_outputs,
        'replayed_tools': len(actual_outputs), 'recorded_tools': len(steps),
        'recorded_business_errors': sum(not t['success'] for t in steps),
        'live_state_match': predicted is not None and encoded(predicted) == encoded(live),
        'reference_actions_succeeded': reference_ok,
        'target_db_match': bool(reward and reward.db_check.db_match and reference_ok),
        'tau_reward': reward.model_dump(mode='json') if reward else None,
        'replay_error': replay_error,
        'snapshot_sha256': {k: digest(v) if v is not None else None for k, v in
                            (('predicted', predicted), ('live', live), ('reference', reference))},
        'new_model_calls': 0, 'new_gpu_calls': 0, 'new_model_cost_cny': 0}
    result['replay_fidelity_passed'] = all(result[k] for k in
        ('strict_replay_passed', 'exact_output_types_passed', 'live_state_match'))
    artifacts = {'score.json': result, 'task.json': task.model_dump(mode='json'),
        'trajectory.json': [m.model_dump(mode='json') for m in messages],
        'predicted_state.json': predicted, 'live_state.json': live, 'reference_state.json': reference,
        'step_checks.json': [{'step': i, 'tool': step['tool'], 'recorded_success': step['success'],
            'expected_output_sha256': digest(expected_outputs[i-1]),
            'actual_output_sha256': digest(actual_outputs[i-1]) if i <= len(actual_outputs) else None}
            for i, step in enumerate(steps, 1)],
        'identity_allocations.json': {str(i): kit.allocations for i, kit in enumerate(constructed)}}
    for name, value in artifacts.items():
        with (output / name).open('x', encoding='utf-8') as file:
            file.write(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
    return result
