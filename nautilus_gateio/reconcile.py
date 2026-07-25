"""Diagnostic reconciliation: compare local state against the exchange.

After a restart, fetch balances, open orders and recent fills from Gate.io,
compare them with the locally known state, and produce a report of
discrepancies plus recommended actions. This function performs **no mutating
calls** — it only diagnoses; acting on the report is the caller's decision.

Note: this is a standalone helper. Nautilus-native start-up reconciliation
(``generate_order_status_reports`` etc.) is not implemented by the execution
client yet — see the feature matrix in the README.
"""

from __future__ import annotations

from typing import Any


def reconcile(
    execution: Any,
    local_state: dict[str, Any],
    pairs: list[str],
) -> dict[str, Any]:
    """Compare local state with the exchange and report discrepancies.

    Parameters
    ----------
    execution
        An object exposing ``balances()``, ``open_orders(pair)`` and
        ``my_trades(pair)`` — e.g. ``GateioHttpClient`` or a test double.
    local_state
        ``{"positions": {pair: amount}, "known_order_ids": [...]}``.
    pairs
        Currency pairs to inspect.
    """
    report: dict[str, Any] = {
        "exchange_reachable": True,
        "discrepancies": [],
        "actions": [],
        "exchange_balances": {},
        "local_positions": dict(local_state.get("positions", {})),
    }
    try:
        report["exchange_balances"] = execution.balances()
    except Exception as exc:
        report["exchange_reachable"] = False
        report["discrepancies"].append(f"exchange unreachable: {str(exc)[:100]}")
        return report

    known_ids = set(local_state.get("known_order_ids", []))
    for pair in pairs:
        try:
            open_orders = execution.open_orders(pair)
        except Exception as exc:
            report["discrepancies"].append(f"{pair}: failed to fetch orders: {str(exc)[:80]}")
            continue
        for order in open_orders:
            if order["id"] not in known_ids:
                # Order exists on the exchange but is unknown locally.
                report["discrepancies"].append(
                    f"{pair}: unknown open order {order['id']} ({order['side']} {order['left']})"
                )
                report["actions"].append(
                    {
                        "type": "adopt_or_cancel",
                        "pair": pair,
                        "order_id": order["id"],
                        "reason": "exchange order not in local state",
                    }
                )
        # For spot, the position is approximated by the base-currency balance.
        base = pair.split("_")[0]
        base_balance = report["exchange_balances"].get(base, {})
        exchange_base = float(base_balance.get("available", 0)) + float(
            base_balance.get("locked", 0)
        )
        local_position = local_state.get("positions", {}).get(pair, 0)
        if local_position > 0 and abs(exchange_base) < 1e-9:
            report["discrepancies"].append(
                f"{pair}: local position {local_position} but no base asset on exchange"
            )
            report["actions"].append(
                {
                    "type": "resync_position",
                    "pair": pair,
                    "local": local_position,
                    "exchange_base": exchange_base,
                }
            )

    report["in_sync"] = len(report["discrepancies"]) == 0
    return report
