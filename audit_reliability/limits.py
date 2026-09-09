"""An additional experiment ceiling; the original global ledger stays authoritative."""
from decimal import Decimal
import threading

from research.budget import BudgetExceeded


class IncrementalBudget:
    def __init__(self, ledger, maximum_increment):
        self.ledger = ledger
        self.start = Decimal(ledger.summary()['accounted_and_reserved_cny'])
        self.ceiling = self.start + Decimal(str(maximum_increment))
        self.lock = threading.Lock()

    def reserve(self, **kwargs):
        with self.lock:
            used = Decimal(self.ledger.summary()['accounted_and_reserved_cny'])
            if used + Decimal(kwargs['maximum_cny']) > self.ceiling:
                raise BudgetExceeded('This experiment additional accounting ceiling would be exceeded')
            return self.ledger.reserve(**kwargs)

    def __getattr__(self, name):
        return getattr(self.ledger, name)
