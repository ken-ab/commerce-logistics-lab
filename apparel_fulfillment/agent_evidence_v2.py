"""Isolated citation-directory intervention; the frozen v1 agent is unchanged.

Only the observation envelope gains exact paths to already returned fields.
No changes to tools, facts, prompts, role routing, budgets or decision validation.
Not the UI default and not a claim of improved model performance.
"""
from copy import deepcopy

from apparel_fulfillment.agent import ApparelAgent, compact, pointer


VERSION = 'apparel-citation-directory-v2'
ORDER_FIELDS = ('order_check/status', 'order_check/issues', 'order_check/substitution_proposals',
                'revision', 'selections', 'approved_substitutions')
ROUTE_FIELDS = ('proposal_id', 'version', 'state', 'previous_proposal_id', 'order_check/status',
                'order_check/issues', 'route/status', 'route/total_cost_cents', 'route/arrival_at',
                'route/adjustment_options', 'independent_route_audit/passed')
FIELDS = {
    'read_order': ORDER_FIELDS,
    'read_variant': ('stock/available_catalog_units', 'brand_rule/allowed_sales_regions',
                     'brand_rule/wholesale_minimum_pieces_per_sku', 'variant/sku', 'variant/brand',
                     'variant/style_id', 'variant/color', 'variant/size', 'variant/pieces_per_catalog_unit',
                     'variant/weight_grams_per_catalog_unit', 'variant/source_record/title',
                     'variant/source_record/description'),
    'select_variants': ORDER_FIELDS,
    'prepare_proposal': ROUTE_FIELDS,
    'read_proposal': tuple('proposal/' + name for name in ROUTE_FIELDS) + ('validity/valid', 'validity/violations'),
    'search_variants': ('matches', 'variants', 'truncated', 'dataset_id'),
    'find_alternatives': ('alternatives',),
    'read_transport_events': ('events', 'as_of', 'corridor_id'),
}
MAX_PATHS = 48
MAX_CITABLE_CHARACTERS = 2500  # Same field-size rule as v1 render().


def citation_directory(observation):
    """A selected-path index, never an answer checklist or a new data source.

    A container that exceeds v1's citation bound is replaced by citable children.
    Missing fields are omitted. Oversized scalar values stay in the raw result.
    """
    paths, seen = [], set()

    def include(path):
        if path in seen or len(paths) >= MAX_PATHS:
            return
        seen.add(path)
        try:
            value = pointer(observation, path)
        except (ValueError, KeyError, IndexError, TypeError):
            return
        if len(compact(value)) <= MAX_CITABLE_CHARACTERS:
            paths.append(path)
        elif isinstance(value, (dict, list)):
            items = value.keys() if isinstance(value, dict) else range(len(value))
            for key in items:
                escaped = str(key).replace('~', '~0').replace('/', '~1')
                include(path + '/' + escaped)

    if observation['success']:
        for field in FIELDS.get(observation['tool'], ()):
            include('/result/' + field)
    return {'notice': 'Selected exact citation paths for this observation. Values remain in result. '
                      'Copy the observation_id and an appropriate path when citing; other valid raw-result paths remain allowed. '
                      'Presence in this directory does not establish relevance, readiness, approval or task completion.',
            'paths': paths, 'complete_field_inventory': False}


class EvidenceDirectoryAgent(ApparelAgent):
    def trace(self, kind, role, **payload):
        if kind == 'start':
            payload['policy'] = VERSION
            payload['intervention'] = 'selected_citation_paths_only'
        return super().trace(kind, role, **payload)

    def observed(self, name, args, role):
        value = super().observed(name, args, role)
        catalogue = citation_directory(value)
        value['citation_directory'] = catalogue
        self.observations[value['observation_id']]['citation_directory'] = deepcopy(catalogue)
        # The raw tool trace is retained separately from this derived envelope.
        self.trace('citation_directory', role, observation_id=value['observation_id'], directory=deepcopy(catalogue))
        return value

    def run(self, task):
        result = super().run(task)
        result['policy_version'] = VERSION
        result['intervention'] = 'selected_citation_paths_only'
        return result
