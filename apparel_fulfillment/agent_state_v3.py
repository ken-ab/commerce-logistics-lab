"""State initialization and operation checks over the immutable apparel tools.

Two independently switchable interventions, with a common caller-supplied
operation contract. Research labels and expected outcomes are never imported.
"""
from copy import deepcopy
from datetime import datetime, timezone
import json
import time

from apparel_fulfillment.action_contract import StateDecision, TaskContract, finish_errors, observed_reference, reference_proposal
from apparel_fulfillment.agent import (ApparelAgent, BUSINESS_TOOLS, COMMON, Delegation, EXPERTS,
    MAX_COMPLETION_TOKENS, MAX_EXPERT_CALLS, MAX_MODEL_CALLS, MODEL, POLICIES, Routing, compact)
from apparel_fulfillment.agent_evidence_v2 import EvidenceDirectoryAgent
from apparel_fulfillment.data import digest
from apparel_fulfillment.orders import OrderError
from apparel_fulfillment.transport import iso, known_events


VERSION = 'apparel-operation-state-v3'
CONTRACT_GUIDE = '''The operation_contract describes the operator's requested operation, not a correct answer.
inspect_product and check_order are read-only. stage_candidate means using select_variants to put an appropriate candidate into the order for review; it does NOT approve a change or place an order. Do not say a candidate is staged unless the tool actually staged it.
For every root order operation, selection_snapshot must report all actual current line_id/SKU pairs; mentioning a pair does not perform a selection. For prepare_proposal and review_proposal, also report the order-check status and any current blocking issue. A pending valid proposal is ready for separate operator confirmation. An infeasible route is unfulfillable, with an actual adjustment for the operator to choose.
A draft ID identifies the order workspace, not a transport proposal. Read current order state to discover selected SKUs, approvals and actual proposal IDs. The original requested SKU can differ from a specifically approved replacement. Do not ask the operator to re-supply information that is available in the order tools.
For review_proposal, inspect the specified proposal or, if unspecified, the latest one that existed at operation start. Reuse it if still valid; otherwise revise under the original constraints. Cite its validity and the current proposal's cost, arrival and checked order state.
Every final citation must relate to the current SKU/order/proposal version. A directory is a path aid, not a substitute for executing the requested action. If a tool or completion check rejects an attempt, inspect the actual state and repair within the same shared call limit.
Experts return their delegated subtask result; the root executor returns the complete operator result.'''


class StateContractAgent(EvidenceDirectoryAgent):
    def __init__(self, *args, contract, bootstrap=True, enforce_contract=True, **kwargs):
        super().__init__(*args, **kwargs)
        self.contract = contract if isinstance(contract, TaskContract) else TaskContract.model_validate(contract)
        self.bootstrap, self.enforce_contract = bool(bootstrap), bool(enforce_contract)
        self.initial_view, self.reference_id = None, None
        self.host_completion_checks = 0

    def trace(self, kind, role, **payload):
        if kind == 'start':
            self.initial_view = deepcopy(payload['initial_state'])
            self.reference_id = reference_proposal(self.contract, self.initial_view)
            if self.contract.mode == 'stage_candidate' and self.contract.line_id not in {l['line_id'] for l in self.initial_view['request']['lines']}:
                raise OrderError('The requested staging line does not exist in this order')
            payload.update(policy=VERSION, operation_contract=self.contract.model_dump(),
                           interventions={'bootstrap': self.bootstrap, 'enforce_contract': self.enforce_contract})
        # Bypass the v2 start-label override, while retaining its observed() directory.
        return ApparelAgent.trace(self, kind, role, **payload)

    def invoke(self, name, args):
        if self.enforce_contract:
            if self.contract.mode in ('inspect_product', 'check_order') and name in ('select_variants', 'prepare_proposal'):
                raise OrderError('The requested operation is read-only; do not stage variants or prepare proposals')
            if self.contract.mode == 'stage_candidate' and name == 'prepare_proposal':
                raise OrderError('Only stage a candidate for review in this operation; proposal preparation is a different requested operation')
            if self.contract.mode == 'review_proposal' and name == 'prepare_proposal':
                old = observed_reference(self.observations, self.reference_id)
                if not old:
                    raise OrderError('Read the target proposal validity before attempting a revision; read_order lists actual proposal IDs')
                current = self.store.assess(self.owner, self.draft_id, self.reference_id, now=self.clock())
                if current['valid']:
                    raise OrderError('The target proposal is still valid; keep it instead of creating an unnecessary revision')
                if old[-1][1]['result']['validity']['valid']:
                    raise OrderError('Validity changed since the observed result; re-read the proposal before revising')
        return super().invoke(name, args)

    def check_completion(self, candidate):
        current = self.view()
        latest = current['proposals'][-1] if current['proposals'] else None
        validity = self.store.assess(self.owner, self.draft_id, latest['proposal_id'], now=self.clock()) if latest else None
        self.host_completion_checks += 1
        errors = finish_errors(self.contract, self.initial_view, current, self.observations, candidate,
                               reference_id=self.reference_id, current_validity=validity,
                               current_events_digest=digest(known_events(self.store.transport_events(), self.store.corridor, self.clock())),
                               current_time=iso(self.clock()))
        receipt = {'draft_id': current['id'], 'initial_revision': self.initial_view['revision'], 'current_revision': current['revision'],
                   'selected_lines': current['selections'], 'reference_proposal_id': self.reference_id,
                   'current_proposal_id': latest['proposal_id'] if latest else None,
                   'current_proposal_version': latest['version'] if latest else None,
                   'confirmation': current['confirmation'],
                   'successful_write_observations': [{'observation_id': t['observation_id'], 'tool': t['tool']}
                       for t in self.traces if t['kind'] == 'tool' and t['success'] and t['tool'] in ('select_variants', 'prepare_proposal')],
                   'notice': 'Program-read execution state, separate from model citations. Does not validate every sentence of the rationale.'}
        candidate['operation_check'] = {'passed': not errors, 'enforced': self.enforce_contract, 'errors': errors}
        candidate['execution_receipt'] = receipt
        self.trace('operation_check', self.arm, errors=errors, enforced=self.enforce_contract, state_digest=digest(current), receipt=receipt)
        return errors

    def loop(self, role, *, delegated_task=None):
        # v1 control loop retained explicitly in a separately versioned module.
        root = role == self.arm
        permitted = {} if root and self.arm == 'coordinator' else dict(BUSINESS_TOOLS)
        permitted['finish'] = (StateDecision, 'Return the structured result with exact observation citations; the root executor reports the requested operation.')
        if root and self.arm != 'single':
            permitted['delegate'] = (Delegation, 'Ask one expert for a bounded subtask. The shared model-call cap applies; experts cannot delegate.')
        if root and self.arm == 'on_demand':
            permitted['record_routing'] = (Routing, 'Record why direct execution or delegation fits the request and observed state.')
        tools = [{'type': 'function', 'function': {'name': name, 'description': description,
                  'parameters': schema.model_json_schema()}} for name, (schema, description) in permitted.items()]
        system = COMMON + '\n' + CONTRACT_GUIDE + '\n' + (POLICIES[self.arm] if root else
                  'You are the ' + role + ' expert. ' + EXPERTS[role] + ' Return your bounded subtask result using finish; you cannot delegate.')
        inputs = {'operator_request': self.task, 'confirmed_order': self.view()['request'], 'draft_id': self.draft_id,
                  'simulation_time': iso(self.clock()), 'operation_contract': self.contract.model_dump()}
        if root and self.bootstrap:
            inputs['initial_order_observation'] = self.observed('read_order', {}, 'runtime')
        if delegated_task:
            inputs['delegated_subtask'] = delegated_task
            inputs['shared_observations'] = list(self.observations.values())
        messages = [{'role': 'system', 'content': system}, {'role': 'user', 'content': compact(inputs)}]
        local_calls = 0
        while len(self.calls) < MAX_MODEL_CALLS and (root or local_calls < MAX_EXPERT_CALLS and len(self.calls) < MAX_MODEL_CALLS - 1):
            self.calls.append({'role': role, 'status': 'started'})
            call = self.calls[-1]
            local_calls += 1
            started = time.monotonic()
            try:
                response = self.client.chat(messages, purpose=self.purpose + str(len(self.calls)), model=MODEL,
                                            tools=tools, max_completion_tokens=MAX_COMPLETION_TOKENS, thinking_budget=256)
            except Exception as error:
                call.update(status='failed', error_type=type(error).__name__, error_message=str(error)[:700], latency_seconds=round(time.monotonic() - started, 6))
                self.trace('model_failure', role, **{k: v for k, v in call.items() if k != 'role'})
                raise
            call.update({key: response.get(key) for key in ('usage', 'estimated_cost_cny', 'budget_call_id', 'latency_seconds', 'requested_model', 'returned_model', 'finish_reason')}, status='success')
            message = {key: response['message'][key] for key in ('role', 'content', 'tool_calls') if key in response['message']}
            message.setdefault('role', 'assistant')
            self.trace('model', role, call_number=len(self.calls), response={**call, 'message': message},
                       input_sha256=digest(messages), input_characters=len(compact(messages)), messages=deepcopy(messages))
            messages.append(message)
            tool_calls = message.get('tool_calls', [])
            if not tool_calls:
                messages.append({'role': 'user', 'content': 'Use finish for a structured cited result or continue with a relevant tool. Text alone is not a completed result.'})
                continue
            completion = None
            for tool in tool_calls:
                name = tool.get('function', {}).get('name', '')
                try:
                    if name not in permitted: raise OrderError('Tool is not permitted for this role')
                    args = permitted[name][0].model_validate(json.loads(tool['function']['arguments'])).model_dump()
                    if completion is not None: raise OrderError('No actions may follow finish in the same completion')
                    if name == 'finish':
                        if root and self.arm == 'on_demand' and not self.routing_recorded:
                            raise OrderError('Record the observed-state routing rationale before finishing')
                        candidate = self.render(args)
                        if root and not candidate['grounding']['invalid']:
                            errors = self.check_completion(candidate)
                            if self.enforce_contract: candidate['grounding']['invalid'].extend(errors)
                        if candidate['grounding']['invalid']:
                            value = {'error': 'Completion not accepted. Repair the exact cited fields or the operation/state issues below using tools; no action was silently performed.',
                                     'invalid': candidate['grounding']['invalid']}
                            self.trace('report_rejected', role, decision=args, invalid=candidate['grounding']['invalid'])
                        else:
                            completion = candidate
                            value = {'status': 'reported', 'grounding': completion['grounding']}
                    elif name == 'record_routing':
                        self.routing_recorded = True
                        value = {'recorded': True, **args}
                        self.trace('routing', role, **args, observed_ids=list(self.observations))
                    elif name == 'delegate':
                        if len(self.calls) >= MAX_MODEL_CALLS - 2:
                            raise OrderError('Insufficient shared calls for another expert and final report')
                        if self.arm == 'on_demand': self.routing_recorded = True
                        before = set(self.observations)
                        self.trace('delegation', role, **args, observed_ids=list(self.observations))
                        result = self.loop(args['expert'], delegated_task=args['task'])
                        value = {'expert_report': result, 'new_observations': [o for k, o in self.observations.items() if k not in before],
                                 'shared_calls_remaining': MAX_MODEL_CALLS - len(self.calls)}
                    else:
                        value = self.observed(name, args, role)
                except (OrderError, ValueError, KeyError, TypeError) as error:
                    value = {'error_type': type(error).__name__, 'error': str(error)[:400]}
                    self.trace('tool_rejected', role, tool=name, error_type=type(error).__name__, detail=str(error)[:400])
                messages.append({'role': 'tool', 'tool_call_id': tool.get('id', ''), 'content': compact(value)})
            if completion is not None: return completion
        return None

    def run(self, task):
        result = ApparelAgent.run(self, task)
        result.update(policy_version=VERSION, operation_contract=self.contract.model_dump(),
                      interventions={'bootstrap': self.bootstrap, 'enforce_contract': self.enforce_contract},
                      host_initial_reads=sum(t['kind'] == 'tool' and t['role'] == 'runtime' for t in self.traces),
                      host_completion_checks=self.host_completion_checks)
        return result
