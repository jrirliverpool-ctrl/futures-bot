"""
test_paper_state_manager.py — Unit tests for paper_state_manager.py.

پوشش (۲۲ تست):
  - Paths helpers (2)
  - State I/O: init/load/save/roundtrip (4)
  - Events basics: append/single/multiple (3)
  - Idempotency / dedup (2)
  - Sequence monotonic (3)
  - Event metadata: fingerprint, strategy_hash, timestamp_iso (3)
  - JSONL format & robustness (3)
  - Contract tests: fingerprint coverage, timestamp→candle_ts_ms (2)
"""

import json
import os
import shutil
import sys
import tempfile

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "scripts"),
)

from paper_state_manager import (
    state_file,
    events_file,
    load_state,
    save_state,
    state_exists,
    init_state,
    load_events,
    max_seq,
    append_events,
    events_count,
    _fingerprint,
)

from state_manager import default_state
from compute_hash import compute_strategy_hash


# ============================================================
# Helpers
# ============================================================

def _tmp_dir():
    return tempfile.mkdtemp()


def _sample_entry(idx=0):
    return {
        "type": "ENTRY",
        "side": "LONG",
        "price": 85000.0 + idx,
        "size": 0.001,
        "sl": 84250.0,
        "tp1": 86000.0,
        "timestamp": 1790960400000 + idx * 3_600_000,
    }


def _sample_exit(idx=0):
    return {
        "type": "EXIT",
        "reason": "SL",
        "price": 84250.0,
        "pnl_usd": -10.0,
        "exit_price_adj": 84200.0,
        "ambiguous": False,
        "remaining_size": 0.001,
        "timestamp": 1790964000000 + idx * 3_600_000,
    }


# ============================================================
# 1-2. Paths helpers
# ============================================================

def test_state_file_path():
    assert state_file("/tmp/x") == "/tmp/x/state.json"


def test_events_file_path():
    assert events_file("/tmp/x") == "/tmp/x/events.jsonl"


# ============================================================
# 3-6. State I/O
# ============================================================

def test_init_state_creates():
    d = _tmp_dir()
    try:
        s = init_state(d, 500.0, "BTC-SWAP-USDT")
        assert s["initial_equity"] == 500.0
        assert s["symbol"] == "BTC-SWAP-USDT"
        assert os.path.exists(state_file(d))
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_init_state_preserves_existing():
    d = _tmp_dir()
    try:
        s = default_state(1000.0)
        s["equity"] = 1500.0
        save_state(d, s)

        s2 = init_state(d, 999.0)
        assert s2["equity"] == 1500.0
        assert s2["initial_equity"] == 1000.0
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_state_exists():
    d = _tmp_dir()
    try:
        assert not state_exists(d)
        save_state(d, default_state())
        assert state_exists(d)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_save_load_roundtrip():
    d = _tmp_dir()
    try:
        original = default_state(2000.0)
        original["equity"] = 2100.0
        save_state(d, original)

        loaded = load_state(d)
        assert loaded["equity"] == 2100.0
        assert loaded["initial_equity"] == 2000.0
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ============================================================
# 7-9. Events basics
# ============================================================

def test_load_events_missing_file():
    d = _tmp_dir()
    try:
        assert load_events(d) == []
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_append_single_event():
    d = _tmp_dir()
    try:
        n_app, n_sk = append_events(d, [_sample_entry()])
        assert n_app == 1
        assert n_sk == 0
        assert os.path.exists(events_file(d))
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_append_multiple_events():
    d = _tmp_dir()
    try:
        events = [_sample_entry(0), _sample_exit(0)]
        n_app, _ = append_events(d, events)
        assert n_app == 2
        loaded = load_events(d)
        assert len(loaded) == 2
        assert loaded[0]["event_type"] == "ENTRY"
        assert loaded[1]["event_type"] == "EXIT"
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ============================================================
# 10-11. Idempotency / dedup
# ============================================================

def test_dedup_same_event():
    d = _tmp_dir()
    try:
        ev = _sample_entry()
        append_events(d, [ev])
        n_app, n_sk = append_events(d, [ev])
        assert n_app == 0
        assert n_sk == 1
        assert events_count(d) == 1
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_dedup_partial_batch():
    d = _tmp_dir()
    try:
        append_events(d, [_sample_entry()])
        n_app, n_sk = append_events(d, [
            _sample_entry(),
            _sample_exit(),
        ])
        assert n_app == 1
        assert n_sk == 1
        assert events_count(d) == 2
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ============================================================
# 12-14. Sequence monotonic
# ============================================================

def test_seq_monotonic():
    d = _tmp_dir()
    try:
        append_events(d, [_sample_entry(0)])
        append_events(d, [_sample_exit(0)])
        append_events(d, [_sample_entry(1)])

        events = load_events(d)
        seqs = [int(e["seq"]) for e in events]
        assert seqs == sorted(seqs)
        assert len(set(seqs)) == len(seqs)
        assert seqs == [1, 2, 3]
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_max_seq_empty():
    assert max_seq([]) == 0


def test_max_seq_normal():
    events = [{"seq": 1}, {"seq": 5}, {"seq": 3}]
    assert max_seq(events) == 5


# ============================================================
# 15-17. Event metadata
# ============================================================

def test_events_have_fingerprint():
    d = _tmp_dir()
    try:
        append_events(d, [_sample_entry()])
        ev = load_events(d)[0]
        assert "event_id" in ev
        assert len(ev["event_id"]) == 16
        assert all(c in "0123456789abcdef" for c in ev["event_id"])
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_events_have_strategy_metadata():
    d = _tmp_dir()
    try:
        append_events(d, [_sample_entry()])
        ev = load_events(d)[0]
        assert ev["strategy_hash"] == compute_strategy_hash()
        assert "strategy_version" in ev
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_events_have_timestamp_iso():
    d = _tmp_dir()
    try:
        append_events(d, [_sample_entry()])
        ev = load_events(d)[0]
        assert ev["timestamp_iso"].endswith("+00:00")
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ============================================================
# 18-20. JSONL format & robustness
# ============================================================

def test_jsonl_format_one_per_line():
    d = _tmp_dir()
    try:
        append_events(d, [_sample_entry(0), _sample_exit(0)])
        with open(events_file(d), "r") as f:
            lines = [l for l in f.readlines() if l.strip()]
        assert len(lines) == 2
        for line in lines:
            obj = json.loads(line)
            assert isinstance(obj, dict)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_corrupted_line_skipped():
    d = _tmp_dir()
    try:
        with open(events_file(d), "w") as f:
            f.write('{"event_id":"abc","seq":1}\n')
            f.write('not valid json\n')
            f.write('{"event_id":"def","seq":2}\n')
        loaded = load_events(d)
        assert len(loaded) == 2
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_append_does_not_overwrite():
    d = _tmp_dir()
    try:
        append_events(d, [_sample_entry(0)])
        with open(events_file(d), "r") as f:
            content1 = f.read()

        append_events(d, [_sample_exit(0)])
        with open(events_file(d), "r") as f:
            content2 = f.read()

        assert content1 in content2
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ============================================================
# 21-22. Contract tests
# ============================================================

def test_fingerprint_covers_all_identity_fields():
    """
    قرارداد fingerprint: تغییر هر یک از فیلدهای هویت event
    باید fingerprint را عوض کند.

    این تست تضمین می‌کند که هیچ فیلد مؤثری از fingerprint
    جا نیفتاده باشد.
    """
    base_event = {
        "type": "ENTRY",
        "timestamp": 1790960400000,
        "side": "LONG",
        "price": 85000.0,
        "size": 0.001,
        "sl": 84250.0,
        "tp1": 86000.0,
    }
    strategy_hash = compute_strategy_hash()
    base_fp = _fingerprint(base_event, strategy_hash)

    # تغییر هر فیلد باید fingerprint را عوض کند
    variations = [
        {"type": "EXIT"},
        {"timestamp": 1790960400001},
        {"side": "SHORT"},
        {"price": 85001.0},
        {"size": 0.002},
        {"sl": 84251.0},
        {"tp1": 86001.0},
        {"entry_price": 85000.0},
        {"exit_price_adj": 84200.0},
        {"pnl_usd": -10.0},
        {"remaining_size": 0.0005},
        {"reason": "SL"},
        {"ambiguous": True},
    ]

    for v in variations:
        modified = dict(base_event)
        modified.update(v)
        fp = _fingerprint(modified, strategy_hash)
        assert fp != base_fp, (
            f"fingerprint did NOT change for variation {v}"
        )

    # strategy_hash هم باید مؤثر باشد
    fp_diff_hash = _fingerprint(base_event, "sha256:different")
    assert fp_diff_hash != base_fp


def test_timestamp_contract_input_output():
    """
    قرارداد timestamp:
      - ورودی خام: فیلد `timestamp`
      - خروجی JSONL: فیلد `candle_ts_ms`
      - مقدار یکسان است
      - فیلد `timestamp` در خروجی وجود ندارد
    """
    d = _tmp_dir()
    try:
        input_ts = 1790960400000
        ev = {
            "type": "ENTRY",
            "side": "LONG",
            "price": 85000.0,
            "size": 0.001,
            "sl": 84250.0,
            "tp1": 86000.0,
            "timestamp": input_ts,
        }
        append_events(d, [ev])
        loaded = load_events(d)[0]

        # خروجی: candle_ts_ms دارد با همان مقدار
        assert "candle_ts_ms" in loaded
        assert loaded["candle_ts_ms"] == input_ts

        # خروجی: فیلد timestamp ندارد
        assert "timestamp" not in loaded

        # ورودی: timestamp داشت
        assert "timestamp" in ev
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ============================================================
# Main
# ============================================================

if __name__ == "__main__":
    tests = [
        # Paths
        test_state_file_path,
        test_events_file_path,
        # State I/O
        test_init_state_creates,
        test_init_state_preserves_existing,
        test_state_exists,
        test_save_load_roundtrip,
        # Events basics
        test_load_events_missing_file,
        test_append_single_event,
        test_append_multiple_events,
        # Dedup
        test_dedup_same_event,
        test_dedup_partial_batch,
        # Seq
        test_seq_monotonic,
        test_max_seq_empty,
        test_max_seq_normal,
        # Metadata
        test_events_have_fingerprint,
        test_events_have_strategy_metadata,
        test_events_have_timestamp_iso,
        # JSONL format
        test_jsonl_format_one_per_line,
        test_corrupted_line_skipped,
        test_append_does_not_overwrite,
        # Contract tests
        test_fingerprint_covers_all_identity_fields,
        test_timestamp_contract_input_output,
    ]

    passed, failed = 0, 0
    print("=" * 60)
    print("🧪 Paper State Manager Unit Tests")
    print("=" * 60)
    for t in tests:
        try:
            t()
            print(f"✅ {t.__name__}")
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