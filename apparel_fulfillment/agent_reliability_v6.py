"""Separately versioned reliability intervention over frozen source-review v5.

Changes: host-supplied object directory, current required-evidence guide, and
explicit service/event comparison with a deterministic rendered explanation.
No new model tools, permission, hidden retries or increased call/citation caps.
"""
from copy import deepcopy
import json
import time

from apparel_fulfillment.action_contract import StateDecision
from apparel_fulfillment.agent import (BUSINESS_TOOLS, COMMON, Delegation, EXPERTS,
    MAX_COMPLETION_TOKENS, MAX_EXPERT_CALLS, MAX_MODEL_CALLS, MODEL, POLICIES, Routing, compact)
from apparel_fulfillment.agent_source_v5 import SourceReviewAgent
from apparel_fulfillment.agent_state_v3 import CONTRACT_GUIDE
from apparel_fulfillment.data import digest
from apparel_fulfillment.orders import OrderError
from apparel_fulfillment.transport import iso, known_events
from apparel_fulfillment.reliability_support import (GUIDE, VERSION, completion_guide,
    object_context, render_revision, revision_comparison)


class ReliabilityAgent(SourceReviewAgent):
    def guidance(self):
        return completion_guide(self.contract, self.view(), self.observations, self.reference_id)

    def comparison(self, proposal):
        current = self.view()
        if not current['proposals'] or proposal['proposal_id'] != current['proposals'][-1]['proposal_id']:
            return None
        previous = proposal.get('previous_proposal_id')
        if previous:
            old = next(p for p in current['proposals'] if p['proposal_id'] == previous)
        elif self.contract.mode == 'review_proposal':
            old = proposal
        else:
            return None
        now = self.clock()
        events = known_events(self.store.transport_events(), self.store.corridor, now)
        return revision_comparison(old, proposal, events, iso(now))

    def invoke(self, name, args):
        result = super().invoke(name, args)
        if name in ('read_proposal', 'prepare_proposal'):
            proposal = result['proposal'] if name == 'read_proposal' else result
            try:
                comparison = self.comparison(proposal)
                if comparison is not None:
                    result = result | {'revision_comparison': comparison}
            except (ValueError, KeyError, TypeError) as error:
                # Preserve an already performed proposal write in the tool result.
                result = result | {'revision_comparison_error': str(error)[:300]}
        return result

    def observed(self, name, args, role):
        value = super().observed(name, args, role)
        guidance = {'object_context': object_context(self.view(), self.reference_id),
                    'completion_guide': self.guidance()}
        value.update(deepcopy(guidance))
        self.observations[value['observation_id']].update(deepcopy(guidance))
        self.trace('reliability_directory', role, observation_id=value['observation_id'], **guidance)
        return value

    def check_completion(self, candidate):
        errors = super().check_completion(candidate)
        if not errors and self.contract.mode in ('prepare_proposal', 'review_proposal'):
            latest = self.view()['proposals'][-1] if self.view()['proposals'] else None
            if latest and candidate['decision']['proposal_id'] == latest['proposal_id']:
                try:
                    comparison = self.comparison(latest)
                    if comparison is not None:
                        candidate['revision_comparison'] = comparison
                        candidate['revision_explanation'] = render_revision(comparison)
                        candidate['answer'] += '\n\n' + candidate['revision_explanation']
                        self.trace('revision_comparison', self.arm, comparison=comparison,
                                   free_text_rationale_validated=False)
                except (ValueError, KeyError, TypeError) as error:
                    errors.append({'field': 'revision_comparison', 'reason': 'inconsistent_revision_facts', 'detail': str(error)[:300]})
                    candidate['operation_check']['passed'] = False
                    candidate['operation_check']['errors'] = deepcopy(errors)
        candidate['rationale_notice'] = 'Model rationale is retained for analysis and is not validated. The revision_explanation, when present, is generated from program-checked service/event/time facts.'
        return errors

    def run(self, task):
        result = super().run(task)
        result['base_policy_version'] = result['policy_version']
        result['policy_version'] = VERSION
        result['reliability_interventions'] = ['exact_object_context', 'current_required_evidence_guide', 'deterministic_revision_comparison']
        return result

    def loop(self, role, *, delegated_task=None):
        # Frozen v3 loop with explicit v6 guidance hooks; call limits and schemas unchanged.
        root = role == self.arm
        permitted = {} if root and self.arm == 'coordinator' else dict(BUSINESS_TOOLS)
        permitted['finish'] = (StateDecision, 'Return the structured result with exact observation citations; the root executor reports the requested operation.')
        if root and self.arm != 'single':
            permitted['delegate'] = (Delegation, 'Ask one expert for a bounded subtask. The shared model-call cap applies; experts cannot delegate.')
        if root and self.arm == 'on_demand':
            permitted['record_routing'] = (Routing, 'Record why direct execution or delegation fits the request and observed state.')
        tools = [{'type': 'function', 'function': {'name': name, 'description': description,
                  'parameters': schema.model_json_schema()}} for name, (schema, description) in permitted.items()]
        system = COMMON + '\n' + CONTRACT_GUIDE + '\n' + GUIDE + '\n' + (POLICIES[self.arm] if root else
                  'You are the ' + role + ' expert. ' + EXPERTS[role] + ' Return your bounded subtask result using finish; you cannot delegate.')
        inputs = {'operator_request': self.task, 'confirmed_order': self.view()['request'], 'draft_id': self.draft_id,
                  'simulation_time': iso(self.clock()), 'operation_contract': self.contract.model_dump()}
        inputs['object_context'] = object_context(self.view(), self.reference_id)
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
                                     'invalid': candidate['grounding']['invalid'],
                                     'completion_guide': self.guidance(), 'object_context': object_context(self.view(), self.reference_id)}
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
                                 'shared_calls_remaining': MAX_MODEL_CALLS - len(self.calls),
                                 'completion_guide': self.guidance(), 'object_context': object_context(self.view(), self.reference_id)}
                    else:
                        value = self.observed(name, args, role)
                except (OrderError, ValueError, KeyError, TypeError) as error:
                    value = {'error_type': type(error).__name__, 'error': str(error)[:400],
                             'completion_guide': self.guidance(), 'object_context': object_context(self.view(), self.reference_id)}
                    self.trace('tool_rejected', role, tool=name, error_type=type(error).__name__, detail=str(error)[:400])
                messages.append({'role': 'tool', 'tool_call_id': tool.get('id', ''), 'content': compact(value)})
            if completion is not None: return completion
        return None
