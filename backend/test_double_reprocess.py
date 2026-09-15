#!/usr/bin/env python3
"""Test that reprocessing the same session twice doesn't double-count hours."""
import sys
import requests
from pathlib import Path

BACKEND_URL = "https://superbot-validate.preview.emergentagent.com"
SUPERBOT_PASSWORD = "superbot"
SESSION_ID = "20260911T040213Z_43a798b7"

def main():
    print("Testing double-reprocess scenario...")
    
    # Use a session to maintain cookies
    session = requests.Session()
    
    # Login
    login_url = f"{BACKEND_URL}/api/auth/login"
    login_data = {"password": SUPERBOT_PASSWORD}
    login_resp = session.post(login_url, json=login_data, timeout=10)
    
    if login_resp.status_code != 200:
        print(f"❌ Login failed: {login_resp.status_code}")
        return 1
    
    print("✅ Logged in")
    
    # Get initial checkpoints
    checkpoints_url = f"{BACKEND_URL}/api/checkpoints"
    initial_resp = session.get(checkpoints_url, timeout=10)
    
    if initial_resp.status_code != 200:
        print(f"❌ Failed to get initial checkpoints: {initial_resp.status_code}")
        return 1
    
    initial_data = initial_resp.json()
    initial_checkpoints = initial_data.get('checkpoints', {})
    
    print("\nInitial checkpoint hours:")
    for name, data in initial_checkpoints.items():
        print(f"  {name}: {data.get('hours')} hours")
    
    # Reprocess the session
    reprocess_url = f"{BACKEND_URL}/api/sessions/{SESSION_ID}/reprocess"
    print(f"\nReprocessing session {SESSION_ID}...")
    reprocess_resp = session.post(reprocess_url, timeout=30)
    
    if reprocess_resp.status_code != 200:
        print(f"❌ Reprocess failed: {reprocess_resp.status_code}")
        print(f"Response: {reprocess_resp.text}")
        return 1
    
    print("✅ First reprocess successful")
    
    # Get checkpoints after first reprocess
    after_first_resp = session.get(checkpoints_url, timeout=10)
    after_first_data = after_first_resp.json()
    after_first_checkpoints = after_first_data.get('checkpoints', {})
    
    print("\nCheckpoint hours after first reprocess:")
    for name, data in after_first_checkpoints.items():
        print(f"  {name}: {data.get('hours')} hours")
    
    # Reprocess again
    print(f"\nReprocessing session {SESSION_ID} again...")
    reprocess_resp2 = session.post(reprocess_url, timeout=30)
    
    if reprocess_resp2.status_code != 200:
        print(f"❌ Second reprocess failed: {reprocess_resp2.status_code}")
        return 1
    
    print("✅ Second reprocess successful")
    
    # Get checkpoints after second reprocess
    after_second_resp = session.get(checkpoints_url, timeout=10)
    after_second_data = after_second_resp.json()
    after_second_checkpoints = after_second_data.get('checkpoints', {})
    
    print("\nCheckpoint hours after second reprocess:")
    for name, data in after_second_checkpoints.items():
        print(f"  {name}: {data.get('hours')} hours")
    
    # Compare
    print("\n" + "="*60)
    print("DOUBLE-REPROCESS TEST RESULTS")
    print("="*60)
    
    all_same = True
    for name in after_first_checkpoints.keys():
        first_hours = after_first_checkpoints[name].get('hours', 0)
        second_hours = after_second_checkpoints[name].get('hours', 0)
        
        if first_hours != second_hours:
            print(f"❌ {name}: Hours changed from {first_hours} to {second_hours}")
            all_same = False
        else:
            print(f"✅ {name}: Hours unchanged at {first_hours}")
    
    if all_same:
        print("\n✅ SUCCESS: No double-counting detected")
        return 0
    else:
        print("\n❌ FAILURE: Hours changed after second reprocess (double-counting)")
        return 1

if __name__ == "__main__":
    sys.exit(main())
