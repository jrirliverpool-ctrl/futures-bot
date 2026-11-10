
#!/usr/bin/env python3
"""
ApexQuant - Toobit Public ExchangeInfo Snapshot

Read-only diagnostic utility.

Features:
- Downloads the public Toobit exchangeInfo response.
- Preserves the exact response bytes.
- Calculates SHA-256.
- Saves a metadata manifest.
- Does not use API credentials.
- Does not place, amend, or cancel orders.

Run from repository root:

python scripts/capture_toobit_exchange_info.py
"""

import argparse
import hashlib
import json
import os
import sys
import tempfile

from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen


SOURCE_URL = (
    "https://api.toobit.com/api/v1/exchangeInfo"
)

DEFAULT_OUTPUT_DIR = (
    Path("artifacts") / "toobit_exchange_info"
)

DEFAULT_TIMEOUT = 30


def utc_now():
    return datetime.now(timezone.utc)


def atomic_write(path: Path, content: bytes):
    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    fd, temporary_path = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )

    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())

        os.replace(temporary_path, path)

    except Exception:
        try:
            os.unlink(temporary_path)
        except OSError:
            pass

        raise


def fetch_public_exchange_info(timeout):
    request = Request(
        SOURCE_URL,
        headers={
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "User-Agent": "ApexQuant-ReadOnly-Diagnostic/1.0",
        },
        method="GET",
    )

    with urlopen(
        request,
        timeout=timeout,
    ) as response:

        status = response.status

        if status != 200:
            raise RuntimeError(
                f"Unexpected HTTP status: {status}"
            )

        raw = response.read()

        headers = {
            "content_type": response.headers.get(
                "Content-Type"
            ),
            "date": response.headers.get("Date"),
            "etag": response.headers.get("ETag"),
            "last_modified": response.headers.get(
                "Last-Modified"
            ),
            "content_encoding": response.headers.get(
                "Content-Encoding"
            ),
        }

    if not raw:
        raise RuntimeError(
            "ExchangeInfo returned an empty response"
        )

    try:
        parsed = json.loads(
            raw.decode("utf-8-sig")
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"ExchangeInfo response is not valid UTF-8 JSON: {exc}"
        ) from exc

    return raw, parsed, status, headers


def build_manifest(
    raw,
    parsed,
    status,
    headers,
    filename,
):
    contracts = None

    if isinstance(parsed, dict):
        candidate = parsed.get("contracts")

        if isinstance(candidate, list):
            contracts = candidate

    return {
        "schema": "apexquant_toobit_raw_snapshot_v1",
        "source_url": SOURCE_URL,
        "retrieved_at_utc": utc_now().isoformat(),
        "http_status": status,
        "response_headers": headers,
        "raw_filename": filename,
        "raw_bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "json_valid": True,
        "json_root_type": type(parsed).__name__,
        "contracts_field_present": (
            contracts is not None
        ),
        "contracts_count": (
            len(contracts)
            if contracts is not None
            else None
        ),
        "read_only": True,
        "api_credentials_used": False,
        "orders_submitted": 0,
        "orders_amended": 0,
        "orders_canceled": 0,
        "trading_eligibility": False,
    }


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Capture a read-only raw Toobit exchangeInfo snapshot."
        )
    )

    parser.add_argument(
        "--out-dir",
        default=str(DEFAULT_OUTPUT_DIR),
    )

    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT,
    )

    args = parser.parse_args()

    if args.timeout <= 0:
        parser.error("--timeout must be positive")

    output_dir = Path(args.out_dir)

    try:
        print("APEXQUANT TOOBIT SNAPSHOT")
        print("=" * 55)

        print(f"Source: {SOURCE_URL}")
        print("Mode: READ ONLY")
        print("API credentials: NONE")

        raw, parsed, status, headers = (
            fetch_public_exchange_info(args.timeout)
        )

        timestamp = utc_now().strftime(
            "%Y%m%dT%H%M%SZ"
        )

        filename = (
            f"exchangeInfo_{timestamp}.json"
        )

        raw_path = output_dir / filename

        digest = hashlib.sha256(raw).hexdigest()

        manifest = build_manifest(
            raw,
            parsed,
            status,
            headers,
            filename,
        )

        manifest_path = output_dir / (
            f"exchangeInfo_{timestamp}.manifest.json"
        )

        checksum_path = output_dir / (
            f"exchangeInfo_{timestamp}.sha256"
        )

        atomic_write(
            raw_path,
            raw,
        )

        atomic_write(
            checksum_path,
            f"{digest}  {filename}\n".encode("utf-8"),
        )

        manifest_bytes = (
            json.dumps(
                manifest,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ) + "\n"
        ).encode("utf-8")

        atomic_write(
            manifest_path,
            manifest_bytes,
        )

        print("-" * 55)
        print(f"HTTP status: {status}")
        print(f"Raw bytes: {len(raw)}")
        print(f"SHA-256: {digest}")
        print(f"JSON root: {manifest['json_root_type']}")
        print(
            "Contracts count:",
            manifest["contracts_count"],
        )

        print("-" * 55)
        print(f"RAW: {raw_path}")
        print(f"SHA: {checksum_path}")
        print(f"MANIFEST: {manifest_path}")

        print("=" * 55)
        print("SNAPSHOT CAPTURE: SUCCESS")
        print("TRADING ELIGIBILITY: FALSE")
        print("ORDERS SUBMITTED: 0")

        return 0

    except Exception as exc:
        print(
            f"SNAPSHOT CAPTURE FAILED: "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )

        return 1


if __name__ == "__main__":
    raise SystemExit(main())
