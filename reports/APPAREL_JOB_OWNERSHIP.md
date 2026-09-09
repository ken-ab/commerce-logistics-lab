# Interactive job ownership and recovery

2026-09-09. A lifecycle fix for the interactive apparel runner; no new model-quality experiment.

Two isolated regressions reproduced defects in the previous runner: reading a running job from another process changed its persisted status to `interrupted`, and two managers sharing the same job directory could start work concurrently. Both checks failed against the original implementation. The first happened because a different PID was treated as proof of a restart; the second because the concurrency guard belonged to one Python object.

The revised runner holds a nonblocking operating-system lock for the whole execution. All updated managers using that local job directory share this guard, including managers in the same process. A status read cannot recover a new-protocol record while execution holds the lock. Once the lock is free, a lingering `running` record is marked interrupted without retrying the task. A second read after lock acquisition prevents a just-completed record from being overwritten.

Legacy records do not prove lock ownership. They are recovered only when the recorded process has ended or its saved process identity no longer matches. Live or unverifiable legacy owners keep their records and prevent a new overlapping run. The existing Windows/Linux process-identity helper is reused. New records also retain process metadata for diagnosis.

Record writes use unique temporary files followed by replacement. Initial persistence failure prevents submission; executor submission failure produces a failed record when storage remains available. Completion-write failure releases the execution lock and leaves recovery able to identify the unfinished record, even when the Python process remains alive. Recovery preserves existing fields, partial evidence and budget reservations; it does not roll back business effects or retry paid requests.

## Verification

| Check set | Observed result |
| --- | --- |
| Original implementation, two regression checks | 2 failed |
| First fix, original regressions and existing lifecycle/provider checks | 6 passed |
| Expanded lifecycle checks, first attempt | 18 passed; 1 test-fixture failure |
| Corrected lifecycle fixture and regression set | 19 passed |
| Existing HTTP and interactive state-contract checks | 11 passed |

The fixture failure was a Windows venv launcher PID mismatch. The corrected subprocess fixture launches the actual interpreter with the existing environment's package paths; the crash test verifies that the stored worker PID equals the process being terminated. No packages are installed by the tests. The final two sets total **30 passing focused checks**, including **15 new lifecycle cases**. These are deterministic engineering checks, not 30 customers, model responses or business benchmark tasks. One existing dependency deprecation warning was observed.

Run from the repository root in an already configured Python 3.12 environment:

```text
python -m pytest tests/test_apparel_job_ownership.py tests/test_apparel_jobs.py tests/test_apparel_provider_jobs.py tests/test_job_recovery.py tests/test_apparel_api.py tests/test_apparel_interactive_state.py -q
```

Validation ran on Windows with Python 3.12.14. The Unix `flock` branch follows the standard library interface but was not executed on this host. The lock protects cooperating processes on one local filesystem; it is not a distributed scheduler or a guarantee for network filesystems. Stop older runner instances before upgrading: older code does not participate in the new protocol. Do not remove the lock file while workers exist. If legacy liveness cannot be verified, review the owner rather than deleting evidence to bypass the guard.

No production server was restarted as part of these checks. Historical model runs, scores, archived databases, and CV results remain unchanged. New paid calls and GPU inference: **0**.

The implementation uses Python's documented [Windows byte-range locking](https://docs.python.org/3.12/library/msvcrt.html#msvcrt.locking) and [Unix file locking](https://docs.python.org/3.12/library/fcntl.html#fcntl.flock). The distinction between execution ownership and a client's waiting state is also described in the [OpenClaw agent-loop documentation](https://docs.openclaw.ai/concepts/agent-loop); this project uses its own small runner and does not embed OpenClaw.
