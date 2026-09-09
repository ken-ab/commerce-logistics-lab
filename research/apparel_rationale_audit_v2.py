"""Explicit model-only configuration, preserving the failed calibration and inputs."""
import argparse
from datetime import datetime, timezone
from research import apparel_rationale_audit as audit

V1 = audit.OUT
audit.OUT = audit.ROOT / "evidence/apparel_rationale_audit_v2"
audit.MODEL = "qwen3.8-max"
# Same prefix intentionally shares the original 20 CNY study cap with v1.


def register():
    if audit.OUT.exists():
        raise FileExistsError("V2 already exists; do not replace its registration")
    failed = audit.read(V1 / "calibration/summary.json")
    assert not failed["gate_passed"] and failed["scheduled"] == 20
    reg = audit.read(V1 / "registration.json")
    assert all(audit.sha(audit.ROOT / f) == h for f, h in reg["source_sha256"].items())
    copies = {"inputs.json": "input_sha256", "labels.json": "label_sha256", "calibration_inputs.json": "calibration_input_sha256"}
    assert all(audit.sha(V1 / name) == reg[key] for name, key in copies.items())
    audit.OUT.mkdir()
    for name in copies:
        (audit.OUT / name).write_bytes((V1 / name).read_bytes())
    sources = [audit.ROOT / "research/apparel_rationale_audit_v2.py",
        audit.ROOT / "research/APPAREL_RATIONALE_AUDIT_V2_PROTOCOL.md",
        V1 / "registration.json", V1 / "calibration/summary.json"]
    reg["source_sha256"].update({p.relative_to(audit.ROOT).as_posix(): audit.sha(p) for p in sources})
    reg.update(registered_at=datetime.now(timezone.utc).isoformat(), model=audit.MODEL,
        ledger_before=audit.ledger(), estimated_cny=[3, 15],
        prior_calibration=str((V1 / "calibration/summary.json").relative_to(audit.ROOT)),
        budget_scope="V1 and V2 combined share the same 20 CNY research prefix; no additional budget.")
    audit.save(audit.OUT / "registration.json", reg)
    print({"registered": True, "model": audit.MODEL, "identical_frozen_inputs": True, "ledger": audit.ledger()})


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("register", "calibration", "campaign"))
    args = parser.parse_args()
    register() if args.mode == "register" else audit.run(args.mode)
