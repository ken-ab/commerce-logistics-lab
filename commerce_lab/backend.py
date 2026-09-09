"""Our persistent implementation of Anthropic's Apache-2.0 StorefrontBackend."""
from __future__ import annotations

from shopping_agent.backend import NotOffered, StorefrontBackend
from shopping_agent.types import (Cart, CartItem, FulfillmentOption, Order, Policy, Product,
    ProductDetails, SearchFilters, ShoppingSessionContext, UserPreferences)

from commerce_lab.state import BusinessError, Store

POLICIES = [
    Policy(policy_id='simulation-v1', title='Research environment scope', category='scope',
        content='Real public ESCI catalog metadata; prices, weights, stock, transport and orders are synthetic. No live merchant, customer payment or carrier booking is connected.'),
    Policy(policy_id='shipping-v1', title='Shipping estimates', category='shipping',
        content='Quotes enforce current stock, route capacity, disruptions, delivery-day limit and shipping budget. One warehouse fulfills each shipment. Simulated quotes exclude duties and taxes; they are not live carrier tariffs.'),
    Policy(policy_id='order-v1', title='Local order confirmation', category='order',
        content='An agent may stage a proposal. Only the host confirmation action creates a simulation order. Confirmation rechecks cart and stock atomically and is idempotent. Orders are visible only to their originating local session.'),
    Policy(policy_id='returns-v1', title='Returns and cancellation availability', category='returns',
        content='The research environment currently provides order lookup but no return, refund or cancellation transaction. Do not claim a refund or cancellation has occurred.'),
]


class ResearchStorefront(StorefrontBackend):
    def __init__(self, store: Store):
        self.store = store

    def check_session(self, session):
        actual = self.store.session(session.session_id)
        if actual['user_id'] != session.user_id:
            raise BusinessError('Session owner mismatch')

    def product(self, row: dict) -> Product:
        stock = self.store.stock(row['id'])
        return Product(product_id=row['id'], title=row['title'], brand=row['brand'] or None,
            price=row['price_usd'], currency='USD', short_description=row['description'][:250],
            labels=['ESCI public metadata', 'Synthetic price and stock'],
            attributes={'color': row['color'], 'locale': row['locale'], 'evidence_id': row['evidence_id'],
                        'weight_kg': str(row['weight_kg']), 'commercial_data': 'synthetic-research-v1'},
            in_stock=any(w['quantity'] > 0 for w in stock['warehouses']))

    async def search_products(self, session: ShoppingSessionContext, query: str,
                              filters: SearchFilters | None = None, limit: int = 8) -> list[Product]:
        self.check_session(session)
        filters = filters or SearchFilters()
        if filters.min_rating is not None or filters.sort != 'relevance':
            raise NotOffered('Source ratings and alternative sort modes are unavailable in this research catalog')
        attrs = filters.attributes
        if filters.category:
            query += ' ' + filters.category
        rows = self.store.catalog.search(query, locale=attrs.get('locale', 'us'), limit=limit,
            max_price=filters.max_price, min_price=filters.min_price, color=attrs.get('color'),
            match_mode=attrs.get('match_mode', 'all'))
        self.store.remember_products(session.session_id, rows)
        return [self.product(row) for row in rows]

    async def get_product_details(self, session: ShoppingSessionContext, product_id: str) -> ProductDetails | None:
        self.check_session(session)
        row = self.store.catalog.get(product_id)
        if not row:
            return None
        self.store.remember_products(session.session_id, [row])
        return ProductDetails(**self.product(row).model_dump(), long_description=row['description'],
            specs={'metadata_source': 'Amazon ESCI', 'commercial_fields': 'synthetic',
                   'text_storage': 'first 1200 characters; raw source preserved'})

    async def get_cart(self, session: ShoppingSessionContext) -> Cart:
        self.check_session(session)
        return Cart(items=[CartItem(**p) for p in self.store.cart(session.session_id)['items']])

    async def add_to_cart(self, session: ShoppingSessionContext, product_id: str, quantity: int) -> Cart:
        self.check_session(session)
        self.store.change_cart(session.session_id, product_id, quantity, add=True)
        return await self.get_cart(session)

    async def update_cart_item(self, session: ShoppingSessionContext, product_id: str, quantity: int) -> Cart:
        self.check_session(session)
        cart = self.store.cart(session.session_id)
        if product_id in {p['product_id'] for p in cart['items']}:
            self.store.change_cart(session.session_id, product_id, quantity)
        return await self.get_cart(session)

    async def remove_from_cart(self, session: ShoppingSessionContext, product_id: str) -> Cart:
        self.check_session(session)
        if product_id in {p['product_id'] for p in self.store.cart(session.session_id)['items']}:
            self.store.change_cart(session.session_id, product_id, 0)
        return await self.get_cart(session)

    async def get_preferences(self, session: ShoppingSessionContext) -> UserPreferences:
        self.check_session(session)
        return UserPreferences(user_id=session.user_id, preferences={})

    async def get_orders(self, session: ShoppingSessionContext, limit: int = 5) -> list[Order]:
        self.check_session(session)
        return [Order(**row) for row in self.store.orders(session.session_id, limit)]

    async def get_order(self, session: ShoppingSessionContext, order_id: str) -> Order | None:
        self.check_session(session)
        row = self.store.get_order(session.session_id, order_id)
        return Order(**row) if row else None

    async def search_policies(self, session: ShoppingSessionContext, query: str) -> list[Policy]:
        self.check_session(session)
        # Only four short, versioned policies; return full coverage without an LLM guess.
        return list(POLICIES)

    async def get_fulfillment_options(self, session: ShoppingSessionContext, product_ids: list[str]) -> list[FulfillmentOption]:
        self.check_session(session)
        # This upstream interface has no destination or quantity. Its default fee
        # is zero, which would falsely imply free shipping before a valid quote.
        raise NotOffered('Use quote_shipping with the current cart, destination, deadline and shipping budget; no shipping fee is known before that quote.')
