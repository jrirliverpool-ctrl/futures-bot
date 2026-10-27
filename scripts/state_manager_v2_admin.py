"""
state_manager_v2_admin.py — Administrative operations for V2 state.

NOT imported by the engine. Only for manual/CLI use by operators.
All operations require explicit confirmation.

Usage:
    python -m scripts.state_manager_v2_admin delete <path> --confirm
    python -m scripts.state_manager_v2_admin backup <path>
    python -m scripts.state_manager_v2_admin inspect <path>
"""

import os
import sys
import json


def _get_state_path_v2(path: str) -> str:
    if path.endswith(".json"):
        return path
    return os.path.join(path, "state.json")


def _confirm_or_abort(prompt: str) -> None:
    print(f"\n{prompt}")
    answer = input("Type 'YES' to confirm: ").strip()
    if answer != "YES":
        print("Aborted.")
        sys.exit(1)


def delete_state_v2_admin(path: str, confirmed: bool = False) -> None:
    """
    Delete V2 state and backups. Requires explicit --confirm flag.

    This is intentionally NOT in the main module to prevent accidental deletion.
    """
    if not confirmed:
        _confirm_or_abort(
            f"This will permanently delete all V2 state files in {path}.\n"
            f"V1 state in paper_state_dir/ is NOT affected."
        )

    target = _get_state_path_v2(path)
    state_dir = os.path.dirname(target) or "."

    if not os.path.exists(state_dir):
        print(f"No state dir at {state_dir}. Nothing to delete.")
        return

    deleted = 0
    for f in os.listdir(state_dir):
        if f == "state.json" or f.startswith("state.json.bak.") or f.startswith(".state_v2_"):
            file_path = os.path.join(state_dir, f)
            try:
                os.remove(file_path)
                print(f"Deleted: {file_path}")
                deleted += 1
            except OSError as e:
                print(f"Failed to delete {file_path}: {e}")

    print(f"\nDeleted {deleted} file(s) from {state_dir}")
    print("V1 state in paper_state_dir/ was NOT touched.")


def inspect_state_v2(path: str) -> None:
    """Print a summary of V2 state."""
    target = _get_state_path_v2(path)
    if not os.path.exists(target):
        print(f"No state at {target}")
        return

    with open(target, "r", encoding="utf-8") as f:
        state = json.load(f)

    print(f"=== V2 State Inspection: {target} ===\n")
    print(f"Version: {state.get('version')}")
    print(f"Strategy hash: {state.get('strategy_hash', '')[:20]}...")
    print(f"Equity: {state.get('equity')} (initial: {state.get('initial_equity')})")
    print(f"Peak equity: {state.get('peak_equity')}")
    print(f"Circuit breaker: {state.get('circuit_breaker_active')}")
    print(f"Max concurrent positions: {state.get('max_concurrent_positions')}")
    print(f"Max portfolio risk: {state.get('max_portfolio_risk')}")
    print(f"\nSymbols ({len(state.get('symbols', {}))}):")
    for sym, s in state.get("symbols", {}).items():
        pos = "OPEN" if s.get("position") else "—"
        pend = "PENDING" if s.get("pending_entry") else "—"
        print(f"  {sym}: {pos} / {pend}")


def backup_state_v2(path: str) -> None:
    """Create a manual backup with a descriptive name."""
    import shutil
    from datetime import datetime, timezone

    target = _get_state_path_v2(path)
    if not os.path.exists(target):
        print(f"No state at {target}")
        return

    backup_path = f"{target}.manual_backup.{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}"
    shutil.copy2(target, backup_path)
    print(f"Backup created: {backup_path}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="V2 state admin operations")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_delete = sub.add_parser("delete", help="Delete V2 state (requires --confirm)")
    p_delete.add_argument("path")
    p_delete.add_argument("--confirm", action="store_true")

    p_inspect = sub.add_parser("inspect", help="Inspect V2 state")
    p_inspect.add_argument("path")

    p_backup = sub.add_parser("backup", help="Create a manual backup")
    p_backup.add_argument("path")

    args = parser.parse_args()

    if args.cmd == "delete":
        delete_state_v2_admin(args.path, confirmed=args.confirm)
    elif args.cmd == "inspect":
        inspect_state_v2(args.path)
    elif args.cmd == "backup":
        backup_state_v2(args.path)