"""
test_kill_switch.py — Unit tests for kill_switch.py.

پوشش (19 تست):
  - state_dir None / non-str / empty → fail-closed (3)
  - KILL file in state_dir → active (1)
  - KILL file in repo_root → active (1)
  - KILL file in both → reason شامل هر دو (1)
  - state["kill_switch"]=True → active (1)
  - state["kill_switch"]=False → inactive (1)
  - state missing kill_switch → inactive (1)
  - state not dict → fail-closed (1)
  - state["kill_switch"] non-bool → fail-closed (1)
  - هیچ منبع → inactive (1)
  - ترکیب KILL + state → reason شامل هر دو (1)
  - repo_root=None → فقط state_dir (1)
  - symlink شکسته → active (1)
  - خطای واقعی فایل‌سیستم (mock) → fail-closed (1)
  - deterministic (1)
  - read-only (1)
  - invalid state_dir overrides valid state (1)
"""

import os
import sys
import shutil
import tempfile
from unittest.mock import patch

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "scripts"),
)

from kill_switch import (
    is_kill_switch_active,
    KILL_FILENAME,
)


# ============================================================
# Helpers
# ============================================================

def _tmp_dir():
    return tempfile.mkdtemp()


def _create_kill(directory):
    path = os.path.join(directory, KILL_FILENAME)
    with open(path, "w") as f:
        f.write("kill")
    return path


# ============================================================
# 1-3. state_dir نامعتبر — fail-closed
# ============================================================

def test_state_dir_none_fail_closed():
    active, reason = is_kill_switch_active(None)
    assert active is True
    assert reason is not None
    assert "error" in reason
    print("✅ test_state_dir_none_fail_closed")


def test_state_dir_non_string_fail_closed():
    active, reason = is_kill_switch_active(12345)
    assert active is True
    assert reason is not None
    assert "error" in reason
    print("✅ test_state_dir_non_string_fail_closed")


def test_state_dir_empty_string_fail_closed():
    active, reason = is_kill_switch_active("")
    assert active is True
    assert reason is not None
    assert "error" in reason
    print("✅ test_state_dir_empty_string_fail_closed")


# ============================================================
# 4. KILL file in state_dir
# ============================================================

def test_kill_file_in_state_dir_activates():
    d = _tmp_dir()
    try:
        _create_kill(d)
        active, reason = is_kill_switch_active(d)
        assert active is True
        assert reason is not None
        assert "KILL" in reason
        assert d in reason
        print("✅ test_kill_file_in_state_dir_activates")
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ============================================================
# 5. KILL file in repo_root
# ============================================================

def test_kill_file_in_repo_root_activates():
    d_state = _tmp_dir()
    d_repo = _tmp_dir()
    try:
        _create_kill(d_repo)
        active, reason = is_kill_switch_active(d_state, repo_root=d_repo)
        assert active is True
        assert reason is not None
        assert d_repo in reason
        print("✅ test_kill_file_in_repo_root_activates")
    finally:
        shutil.rmtree(d_state, ignore_errors=True)
        shutil.rmtree(d_repo, ignore_errors=True)


# ============================================================
# 6. KILL file in both → reason شامل هر دو
# ============================================================

def test_kill_file_in_both_mentions_both():
    d_state = _tmp_dir()
    d_repo = _tmp_dir()
    try:
        _create_kill(d_state)
        _create_kill(d_repo)
        active, reason = is_kill_switch_active(d_state, repo_root=d_repo)
        assert active is True
        assert reason is not None
        assert d_state in reason
        assert d_repo in reason
        assert " | " in reason
        print("✅ test_kill_file_in_both_mentions_both")
    finally:
        shutil.rmtree(d_state, ignore_errors=True)
        shutil.rmtree(d_repo, ignore_errors=True)


# ============================================================
# 7. state["kill_switch"] = True
# ============================================================

def test_state_kill_switch_true_activates():
    d = _tmp_dir()
    try:
        active, reason = is_kill_switch_active(
            d, state={"kill_switch": True}
        )
        assert active is True
        assert reason is not None
        assert "kill_switch" in reason.lower()
        print("✅ test_state_kill_switch_true_activates")
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ============================================================
# 8. state["kill_switch"] = False
# ============================================================

def test_state_kill_switch_false_inactive():
    d = _tmp_dir()
    try:
        active, reason = is_kill_switch_active(
            d, state={"kill_switch": False}
        )
        assert active is False
        assert reason is None
        print("✅ test_state_kill_switch_false_inactive")
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ============================================================
# 9. state بدون kill_switch → inactive
# ============================================================

def test_state_missing_kill_switch_inactive():
    d = _tmp_dir()
    try:
        active, reason = is_kill_switch_active(
            d, state={"equity": 1000.0}
        )
        assert active is False
        assert reason is None
        print("✅ test_state_missing_kill_switch_inactive")
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ============================================================
# 10. state غیر dict → fail-closed
# ============================================================

def test_state_not_dict_fail_closed():
    d = _tmp_dir()
    try:
        active, reason = is_kill_switch_active(
            d, state="not a dict"
        )
        assert active is True
        assert reason is not None
        assert "error" in reason
        print("✅ test_state_not_dict_fail_closed")
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ============================================================
# 11. state["kill_switch"] غیر bool → fail-closed
# ============================================================

def test_state_kill_switch_non_bool_fail_closed():
    d = _tmp_dir()
    try:
        active, reason = is_kill_switch_active(
            d, state={"kill_switch": "yes"}
        )
        assert active is True
        assert reason is not None
        assert "error" in reason
        print("✅ test_state_kill_switch_non_bool_fail_closed")
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ============================================================
# 12. هیچ منبع فعال → inactive
# ============================================================

def test_no_source_active_returns_inactive():
    d_state = _tmp_dir()
    d_repo = _tmp_dir()
    try:
        active, reason = is_kill_switch_active(
            d_state, state={"kill_switch": False}, repo_root=d_repo
        )
        assert active is False
        assert reason is None
        print("✅ test_no_source_active_returns_inactive")
    finally:
        shutil.rmtree(d_state, ignore_errors=True)
        shutil.rmtree(d_repo, ignore_errors=True)


# ============================================================
# 13. ترکیب KILL + state → reason شامل هر دو
# ============================================================

def test_kill_file_and_state_combined():
    d_state = _tmp_dir()
    try:
        _create_kill(d_state)
        active, reason = is_kill_switch_active(
            d_state, state={"kill_switch": True}
        )
        assert active is True
        assert reason is not None
        assert d_state in reason
        assert "kill_switch" in reason.lower()
        assert " | " in reason
        print("✅ test_kill_file_and_state_combined")
    finally:
        shutil.rmtree(d_state, ignore_errors=True)


# ============================================================
# 14. repo_root=None → فقط state_dir چک می‌شود
# ============================================================

def test_repo_root_none_checks_only_state_dir():
    """
    قرارداد: repo_root=None یعنی بررسی repo_root انجام نشود.
    """
    d = _tmp_dir()
    try:
        active, reason = is_kill_switch_active(d)
        assert active is False
        assert reason is None

        _create_kill(d)
        active, reason = is_kill_switch_active(d)
        assert active is True
        print("✅ test_repo_root_none_checks_only_state_dir")
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ============================================================
# 15. symlink شکسته به KILL → active
# ============================================================

def test_broken_symlink_to_kill_detected():
    d = _tmp_dir()
    try:
        kill_path = os.path.join(d, KILL_FILENAME)
        target = os.path.join(d, "does_not_exist")
        try:
            os.symlink(target, kill_path)
        except (OSError, NotImplementedError):
            print("⚠️  test_broken_symlink_to_kill_detected skipped "
                  "(symlink unsupported)")
            return

        active, reason = is_kill_switch_active(d)
        assert active is True, (
            "broken symlink should be detected via lexists"
        )
        print("✅ test_broken_symlink_to_kill_detected")
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ============================================================
# 16. خطای واقعی فایل‌سیستم (mock) → fail-closed
# ============================================================

def test_real_filesystem_error_fail_closed():
    """
    شبیه‌سازی خطای واقعی دسترسی با unittest.mock.

    سناریو: PermissionError در os.path.lexists → fail-closed.
    """
    d = _tmp_dir()
    try:
        def raising_lexists(path):
            raise PermissionError(f"mock: permission denied on {path}")

        with patch("kill_switch.os.path.lexists",
                   side_effect=raising_lexists):
            active, reason = is_kill_switch_active(d)

        assert active is True, (
            "filesystem error must fail-closed (active=True)"
        )
        assert reason is not None
        assert "error" in reason
        assert "permission" in reason.lower()
        print("✅ test_real_filesystem_error_fail_closed")
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ============================================================
# 17. Deterministic
# ============================================================

def test_deterministic():
    d = _tmp_dir()
    try:
        _create_kill(d)
        r1 = is_kill_switch_active(d, state={"kill_switch": True})
        r2 = is_kill_switch_active(d, state={"kill_switch": True})
        assert r1 == r2

        d2 = _tmp_dir()
        try:
            r3 = is_kill_switch_active(d2, state={"kill_switch": False})
            r4 = is_kill_switch_active(d2, state={"kill_switch": False})
            assert r3 == r4
            assert r3 == (False, None)
        finally:
            shutil.rmtree(d2, ignore_errors=True)

        print("✅ test_deterministic")
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ============================================================
# 18. read-only (هیچ فایل ساخته/تغییر نمی‌شود)
# ============================================================

def test_read_only_no_side_effects():
    d = _tmp_dir()
    try:
        before = sorted(os.listdir(d))
        _ = is_kill_switch_active(d, state={"kill_switch": True})
        after = sorted(os.listdir(d))
        assert before == after, (
            f"directory contents changed: before={before} after={after}"
        )
        print("✅ test_read_only_no_side_effects")
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ============================================================
# 19. state_dir نامعتبر + state سالم → باز هم fail-closed
# ============================================================

def test_invalid_state_dir_overrides_valid_state():
    active, reason = is_kill_switch_active(
        None, state={"kill_switch": False}
    )
    assert active is True
    assert reason is not None
    assert "error" in reason
    print("✅ test_invalid_state_dir_overrides_valid_state")


# ============================================================
# Main
# ============================================================

if __name__ == "__main__":
    tests = [
        test_state_dir_none_fail_closed,
        test_state_dir_non_string_fail_closed,
        test_state_dir_empty_string_fail_closed,
        test_kill_file_in_state_dir_activates,
        test_kill_file_in_repo_root_activates,
        test_kill_file_in_both_mentions_both,
        test_state_kill_switch_true_activates,
        test_state_kill_switch_false_inactive,
        test_state_missing_kill_switch_inactive,
        test_state_not_dict_fail_closed,
        test_state_kill_switch_non_bool_fail_closed,
        test_no_source_active_returns_inactive,
        test_kill_file_and_state_combined,
        test_repo_root_none_checks_only_state_dir,
        test_broken_symlink_to_kill_detected,
        test_real_filesystem_error_fail_closed,
        test_deterministic,
        test_read_only_no_side_effects,
        test_invalid_state_dir_overrides_valid_state,
    ]
    passed, failed = 0, 0
    print("=" * 60)
    print("🧪 Kill Switch Unit Tests")
    print("=" * 60)
    for t in tests:
        try:
            t()
            passed += 1
        except Exception as exc:
            import traceback
            print(f"❌ {t.__name__}: {exc}")
            traceback.print_exc()
            failed += 1
    print("=" * 60)
    print(f"🏁 FINAL RESULT: {passed}/{len(tests)} PASSED")
    print("=" * 60)
    if failed:
        sys.exit(1)