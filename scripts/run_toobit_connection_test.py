"""
run_toobit_connection_test.py — Real Network Read-Only Test.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from toobit_client import ToobitClient

def main():
    api_key = os.environ.get("TOOBIT_API_KEY")
    api_secret = os.environ.get("TOOBIT_API_SECRET")
    
    if not api_key or not api_secret:
        print("❌ ERROR: Secrets (TOOBIT_API_KEY / TOOBIT_API_SECRET) are missing.")
        sys.exit(1)
        
    print("=" * 50)
    print("🌐 REAL NETWORK TEST: Toobit API Authentication")
    print("=" * 50)
    
    try:
        client = ToobitClient(api_key, api_secret)
    except ValueError as e:
        print(f"❌ Initialization Error: {e}")
        sys.exit(1)

    result = client.get_balance()
    status = result.get("status")
    
    if status == "SUCCESS":
        print("✅ SUCCESS! Connection established and signature verified.")
        print("   (Balance data was retrieved and securely masked.)")
        # ما هرگز دیتای موجودی را چاپ نمی‌کنیم تا در لاگ‌های عمومی گیت‌هاب نیاید.
    elif status == "API_ERROR":
        print(f"❌ API REJECTED. Code: {result.get('api_code')}")
        print("   This means the key is invalid, OR it lacks Futures Read permission.")
    elif status == "HTTP_ERROR":
        print(f"❌ HTTP ERROR: Status Code {result.get('status_code')}")
        print("   Check if the endpoint URL or headers are correct.")
    elif status == "NETWORK_ERROR":
        print(f"❌ NETWORK ERROR: {result.get('error')}")
    elif status == "INVALID_RESPONSE":
        print(f"❌ INVALID RESPONSE: {result.get('error')}")
    else:
        print(f"❌ UNKNOWN ERROR: {result}")
        
    if status != "SUCCESS":
        sys.exit(1)

if __name__ == "__main__":
    main()