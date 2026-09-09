"""Separate stock-backed identity from catalog-attribute/write provenance."""
from contextlib import closing
from decimal import Decimal
import re

from commerce_lab.agent import CommerceAgent, FinalReport

HOST_VERSION = 'identity-evidence-v2.1'
IDENTITY_ERROR = 'Final product list includes an unobserved catalog ID'


class IdentityAwareCommerceAgent(CommerceAgent):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.stock_identity_ids: set[str] = set()
        self.request_id_candidates: dict[str, set[str]] = {}

    async def execute_tool(self, name, args):
        if name in {'get_product_details', 'get_stock', 'set_cart_item'}:
            incoming = args.get('product_id')
            candidates = self.request_id_candidates.get(incoming, set())
            if len(candidates) == 1:
                canonical = next(iter(candidates))
                if self.store.catalog.get(canonical) is not None:
                    # The original model_response still contains the raw argument.
                    # Record the actual canonical argument used by the tool so the
                    # external strict replay sees exactly the same operation.
                    self.store.trace(self.run_id, 'host', 'tool_argument_normalization', {
                        'name': name, 'incoming_arguments': dict(args), 'canonical_product_id': canonical,
                        'source': 'Unique namespaced reference in the original user request; no locale default or catalog-wide ID guessing.'})
                    args['product_id'] = canonical
        output = await super().execute_tool(name, args)
        if name == 'get_stock' and isinstance(output, dict):
            # Store.stock checks catalog existence before returning this record.
            # Do not put stock-only observations in the cart-write `seen` cache.
            ident = output.get('product_id')
            if (ident == args.get('product_id') and not output.get('error')
                    and isinstance(output.get('warehouses'), list)):
                self.stock_identity_ids.add(ident)
        return output

    def ground_report(self, report: FinalReport) -> dict:
        result = super().ground_report(report)
        with closing(self.store.connect()) as db:
            detail_ids = {r[0] for r in db.execute(
                'SELECT product_id FROM seen WHERE session_id=?', (self.session.session_id,))}
        if all(ident in detail_ids or ident in self.stock_identity_ids for ident in report.product_ids):
            result['errors'] = [error for error in result['errors'] if error != IDENTITY_ERROR]
        result['structured_grounding_passed'] = not result['errors']
        result['host_contract_version'] = HOST_VERSION
        result['identity_evidence'] = {
            ident: ('catalog_record' if ident in detail_ids else 'stock_record_identity_only')
            for ident in report.product_ids if ident in detail_ids or ident in self.stock_identity_ids}
        result['scope'] = ('Checks observed product identity, proposal ownership and structured shipping facts. '
                           'A stock observation does not establish descriptive attributes or permit cart writes. '
                           'Free-text narrative still requires separate claim auditing.')
        return result

    async def run(self, session_id, task, run_id=None):
        # Evidence, quote ownership and accounting belong to this run, even if
        # a caller reuses an agent instance instead of constructing a fresh one.
        self.stock_identity_ids.clear()
        self.request_id_candidates.clear()
        for ident in re.findall(r'(?<![A-Za-z0-9_])(?:us|es|jp):[A-Za-z0-9][A-Za-z0-9_-]{0,36}', task):
            self.request_id_candidates.setdefault(ident.split(':', 1)[1], set()).add(ident)
        self.product_ids.clear()
        self.last_quote = None
        self.call_count = 0
        self.cost = Decimal(0)
        self.store.session(session_id)
        run_id = run_id or self.store.new_run(session_id, task)
        self.store.trace(run_id, 'host', 'evidence_contract_configuration', {
            'version': HOST_VERSION, 'scope': 'Post-v1 isolated product fix; no change to frozen v1 scores.'})
        return await super().run(session_id, task, run_id)
