"""Bounded scheduling for new studies, with no implicit retry of any job."""
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

from research.provider_gate import ProviderHeld


def dispatch(jobs, worker, preflight, *, max_workers=3):
    """Yield results; leave unstarted jobs untouched when a durable hold appears."""
    pending = iter(jobs)
    exhausted = False
    stopped = False
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        active = {}
        while active or not (exhausted or stopped):
            while not (exhausted or stopped) and len(active) < max_workers:
                try:
                    preflight()
                except ProviderHeld:
                    stopped = True
                    break
                try:
                    job = next(pending)
                except StopIteration:
                    exhausted = True
                    break
                active[pool.submit(worker, job)] = job
            if not active:
                break
            done, _ = wait(active, return_when=FIRST_COMPLETED)
            for future in done:
                job = active.pop(future)
                # Workers persist their attempt/result and do not retry internally.
                yield job, future.result()
    if stopped:
        raise ProviderHeld('Provider hold stopped scheduling; unstarted jobs remain unattempted.')
