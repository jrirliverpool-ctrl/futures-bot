
"""
Strict paper equity audit.

Uses period_start_equity for period-local ledger reconciliation.
Falls back to initial_equity for older states.

Read-only. Never changes state or event history.
"""

import json
import math
import os
import sys


def fail(message):
    print(f"AUDIT FAILED: {message}")
    return 1


def finite_number(value):
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
    )


def read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as source:
            value = json.load(source)
    except Exception as exc:
        raise RuntimeError(
            f"Cannot read {path}: {exc}"
        ) from exc

    if not isinstance(value, dict):
        raise RuntimeError(
            f"{path} must contain a JSON object."
        )

    return value


def main():
    state_dir = os.environ.get(
        "STATE_DIR",
        "./paper_state_dir",
    )

    state_path = os.path.join(
        state_dir,
        "state.json",
    )

    events_path = os.path.join(
        state_dir,
        "events.jsonl",
    )

    if not os.path.isfile(state_path):
        return fail("state.json not found.")

    if not os.path.isfile(events_path):
        return fail("events.jsonl not found.")

    try:
        state = read_json(state_path)
    except RuntimeError as exc:
        return fail(str(exc))

    for key in (
        "initial_equity",
        "equity",
        "peak_equity",
    ):
        if not finite_number(state.get(key)):
            return fail(f"Invalid financial field: {key}")

    if state.get("position") is not None:
        return fail(
            "Open position exists. "
            "Unrealized PnL cannot be reconciled by this audit."
        )

    period_start_equity = state.get(
        "period_start_equity",
        state["initial_equity"],
    )

    if not finite_number(period_start_equity):
        return fail("Invalid period_start_equity.")

    if period_start_equity <= 0:
        return fail("period_start_equity must be positive.")

    total_gross_pnl = 0.0
    total_commissions = 0.0
    total_funding = 0.0

    event_count = 0
    exit_count = 0

    try:
        with open(events_path, "r", encoding="utf-8") as source:
            for line_number, line in enumerate(source, start=1):
                if not line.strip():
                    continue

                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    return fail(
                        f"Invalid JSON in events.jsonl "
                        f"at line {line_number}."
                    )

                if not isinstance(event, dict):
                    return fail(
                        f"Invalid event object at line {line_number}."
                    )

                event_count += 1

                event_type = event.get("event_type")

                if event_type == "EXIT":
                    exit_count += 1

                    pnl = event.get(
                        "pnl_usd",
                        event.get("pnl_gross", 0.0),
                    )

                    commission = event.get(
                        "total_commission_usd",
                        event.get("commission_usd", 0.0),
                    )

                    if not finite_number(pnl):
                        return fail(
                            f"Invalid EXIT PnL at line {line_number}."
                        )

                    if not finite_number(commission):
                        return fail(
                            f"Invalid EXIT commission at line {line_number}."
                        )

                    total_gross_pnl += pnl
                    total_commissions += commission

                elif event_type == "FUNDING":
                    cashflow = event.get(
                        "cashflow_usd",
                        0.0,
                    )

                    if not finite_number(cashflow):
                        return fail(
                            f"Invalid funding cashflow at line {line_number}."
                        )

                    total_funding += cashflow

    except OSError as exc:
        return fail(
            f"Cannot read events.jsonl: {exc}"
        )

    calculated_equity = (
        period_start_equity
        + total_gross_pnl
        - total_commissions
        + total_funding
    )

    difference = abs(
        calculated_equity - state["equity"]
    )

    print("=" * 55)
    print("APEXQUANT PAPER EQUITY AUDIT")
    print("=" * 55)
    print(f"Period start equity : {period_start_equity:.8f}")
    print(f"Account initial     : {state['initial_equity']:.8f}")
    print(f"Current equity      : {state['equity']:.8f}")
    print(f"Peak equity         : {state['peak_equity']:.8f}")
    print(f"Events              : {event_count}")
    print(f"EXIT events         : {exit_count}")
    print(f"Gross PnL           : {total_gross_pnl:.8f}")
    print(f"Commissions         : {total_commissions:.8f}")
    print(f"Funding             : {total_funding:.8f}")
    print(f"Calculated equity   : {calculated_equity:.8f}")
    print(f"Difference          : {difference:.10f}")
    print("=" * 55)

    if difference > 1e-5:
        return fail(
            "State equity does not match the current-period ledger."
        )

    print("AUDIT PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
