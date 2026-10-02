"""
compute_hash.py — محاسبه strategy_hash از strategy_config.

طبق Spec Section 15:
    strategy_hash = sha256(canonical_json(STRATEGY_CONFIG))

خواص تضمین‌شده:
  - Single Source of Truth: config از strategy_config.py می‌آید.
  - Deterministic: dict order بی‌اثر.
  - Sensitive: کوچک‌ترین تغییر پارامتر → hash متفاوت.
  - Isolated: تغییر README/کد/state → hash ثابت.
  - Runtime-excluded: symbol، equity، timestamp داخل hash نیستند.

فرمت خروجی: "sha256:<64-char-hex>"
"""

import hashlib
import json

from strategy_config import (
    STRATEGY_CONFIG,
    CANONICAL_KEYS,
    get_strategy_config,
)


# ============================================================
# Canonical JSON
# ============================================================

def config_to_canonical_json(config: dict) -> str:
    """
    JSON canonical:
      - sort_keys=True      → ترتیب الفبایی
      - separators=(',',':') → بدون whitespace
      - ensure_ascii=True    → escape کاراکترهای غیر ASCII
      - allow_nan=False      → NaN/Infinity ممنوع
    """
    return json.dumps(
        config,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


# ============================================================
# Hash
# ============================================================

def compute_strategy_hash(config: dict = None) -> str:
    """
    محاسبه hash.

    Args:
        config: اگر None، از strategy_config.get_strategy_config()
                استفاده می‌شود.
    """
    if config is None:
        config = get_strategy_config()
    canonical = config_to_canonical_json(config)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


# ============================================================
# Metadata
# ============================================================

def get_strategy_metadata(config: dict = None) -> dict:
    """
    Metadata استراتژی برای ذخیره در state.json و trade_log.csv.
    """
    if config is None:
        config = get_strategy_config()
    return {
        "version": config["version"],
        "hash": compute_strategy_hash(config),
    }


# ============================================================
# Verification
# ============================================================

def verify_hash(config: dict, expected_hash: str) -> bool:
    if not isinstance(expected_hash, str):
        return False
    return compute_strategy_hash(config) == expected_hash


# ============================================================
# CLI
# ============================================================

if __name__ == "__main__":
    import sys

    metadata = get_strategy_metadata()
    canonical = config_to_canonical_json(get_strategy_config())

    print("=" * 60)
    print("🧾 ApexQuant Strategy Metadata")
    print("=" * 60)
    print(f"  Version       : {metadata['version']}")
    print(f"  Hash          : {metadata['hash']}")
    print(f"  Canonical JSON:")
    print(f"    {canonical}")
    print("=" * 60)

    if "--json" in sys.argv:
        print(json.dumps(metadata, indent=2))