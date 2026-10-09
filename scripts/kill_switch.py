"""
kill_switch.py — Emergency stop برای ApexQuant.

قرارداد:
  kill switch فعال است اگر هر یک از این منابع True باشد:

    1. فایل KILL در state_dir
    2. فایل KILL در repo_root (اگر داده شود)
    3. state["kill_switch"] == True

  Fail-closed:
    هر خطا در بررسی هر منبع → active=True با reason="error: ..."

  - خروجی: (active: bool, reason: str | None)
  - هیچ نوشتنی انجام نمی‌دهد
  - هیچ سفارشی ارسال نمی‌کند
  - هیچ API صدا زده نمی‌شود

نکته:
  - این ماژول فقط وضعیت را گزارش می‌دهد.
  - تصمیم استفاده به لایه‌ی بالاتر (run_paper / paper_engine) واگذار می‌شود.
  - بررسی قفل، جایگزین مدیریت پوزیشن باز نیست.
    این ماژول ورودهای جدید را مسدود می‌کند، ولی پوزیشن باز را نمی‌بندد.
"""

import os


# ============================================================
# Constants
# ============================================================

KILL_FILENAME = "KILL"


# ============================================================
# Helpers
# ============================================================

def _path_present(path):
    """
    بررسی وجود یک مسیر، شامل symlink شکسته.

    Returns:
        (present: bool, error: str | None)

    Fail-safe: در صورت خطا، error ست می‌شود.
    """
    try:
        return os.path.lexists(path), None
    except Exception as e:
        return False, str(e)


def _check_kill_file(directory, label):
    """
    بررسی فایل KILL در یک دایرکتوری.

    Returns:
        (found: bool, reason: str | None, error: str | None)
    """
    try:
        path = os.path.join(directory, KILL_FILENAME)
    except Exception as e:
        return False, None, f"cannot construct path for {label}: {e}"

    present, err = _path_present(path)
    if err is not None:
        return False, None, f"cannot check {label} at {path}: {err}"
    if present:
        return True, f"KILL file present: {path}", None
    return False, None, None


# ============================================================
# Public API
# ============================================================

def is_kill_switch_active(state_dir, state=None, repo_root=None):
    """
    بررسی فعال بودن kill switch.

    Args:
        state_dir : مسیر دایرکتوری paper-state (اجباری)
        state     : dict state (اختیاری) — بررسی state["kill_switch"]
        repo_root : مسیر ریشه مخزن (اختیاری) — بررسی KILL ثانویه

    Returns:
        (active: bool, reason: str | None)

    قواعد:
      - اگر state_dir نامعتبر → (True, "error: ...")
      - اگر هر KILL یافت شود → (True, "KILL file present: <path>")
      - اگر state["kill_switch"] == True → (True, "state['kill_switch'] is True")
      - اگر چند منبع فعال → همه در reason با " | " جدا می‌شوند
      - اگر هیچ‌کدام → (False, None)
      - هر خطا در بررسی state → (True, "error: ...")
    """
    # ---- Validate state_dir ----
    if state_dir is None:
        return True, "error: state_dir is None"
    if not isinstance(state_dir, str):
        return True, (
            f"error: state_dir is not a string "
            f"({type(state_dir).__name__})"
        )
    if not state_dir.strip():
        return True, "error: state_dir is empty"

    reasons = []

    # ---- Check KILL in state_dir ----
    found, reason, err = _check_kill_file(state_dir, "state_dir")
    if err is not None:
        return True, f"error: {err}"
    if found:
        reasons.append(reason)

    # ---- Check KILL in repo_root (اختیاری) ----
    if repo_root is not None:
        if not isinstance(repo_root, str):
            return True, (
                f"error: repo_root is not a string "
                f"({type(repo_root).__name__})"
            )
        if repo_root.strip():
            found, reason, err = _check_kill_file(repo_root, "repo_root")
            if err is not None:
                return True, f"error: {err}"
            if found:
                reasons.append(reason)

    # ---- Check state["kill_switch"] ----
    if state is not None:
        if not isinstance(state, dict):
            return True, (
                f"error: state is not a dict "
                f"({type(state).__name__})"
            )
        try:
            ks = state.get("kill_switch", False)
        except Exception as e:
            return True, f"error reading state['kill_switch']: {e}"

        if not isinstance(ks, bool):
            return True, (
                f"error: state['kill_switch'] is not bool "
                f"({type(ks).__name__})"
            )
        if ks is True:
            reasons.append("state['kill_switch'] is True")

    # ---- Aggregate ----
    if reasons:
        return True, " | ".join(reasons)
    return False, None