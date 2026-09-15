#!/usr/bin/env python3
"""Direct test of the real MultiVenue 3H session ZIP.

Tests:
1. run_qa() directly on the retained real ZIP
2. Parser discovers all 3 JSON files
3. All expected fields are resolved correctly
4. API reprocess endpoint works
5. Hours accounting is correct
"""
import sys
import os
import requests
from pathlib import Path

# Add backend to path
sys.path.insert(0, str(Path(__file__).parent))

from qa_engine import run_qa
from zip_security import inspect_zip
from manifest_parser import parse_manifest_from_source

# Configuration
REAL_ZIP_PATH = "/app/backend/data/raw_zips/0dff31ad-df4e-4e8e-a14b-8cb955bf746c.zip"
SESSION_ID = "20260911T040213Z_43a798b7"
BACKEND_URL = "https://superbot-validate.preview.emergentagent.com"
SUPERBOT_PASSWORD = "superbot"

# Expected values from the real ZIP
EXPECTED = {
    "collector_sha256": "e924edc1b8348d9cc8e74a37eaa17e6bbc80116de29d2f7a00cd43444d1265a3",
    "exit_code": 0,
    "watchdog": False,
    "writer_errors": 0,
    "missed_ticks": 2,
    "lag_gt_50ms": 16,
    "reconnect_count": 2,
    "websocket_errors": 2,
    "final_buffer_status": "CLEAN",
    "sync_grid_file_count": 360,
    "books_file_count": 360,
    "trades_file_count": 360,
    "parquet_magic_status": "PASS",
    "duration_hours": 3.0019,  # approximate
}

class TestResults:
    def __init__(self):
        self.tests_run = 0
        self.tests_passed = 0
        self.failures = []

    def test(self, name, condition, detail=""):
        self.tests_run += 1
        if condition:
            self.tests_passed += 1
            print(f"✅ {name}")
            if detail:
                print(f"   {detail}")
        else:
            print(f"❌ {name}")
            if detail:
                print(f"   {detail}")
            self.failures.append(f"{name}: {detail}")

    def summary(self):
        print(f"\n{'='*60}")
        print(f"Tests: {self.tests_passed}/{self.tests_run} passed")
        if self.failures:
            print(f"\nFailures:")
            for f in self.failures:
                print(f"  - {f}")
        print(f"{'='*60}")
        return self.tests_passed == self.tests_run


def test_direct_run_qa():
    """Test 1: Direct run_qa() on the real ZIP."""
    print("\n" + "="*60)
    print("TEST 1: Direct run_qa() on real ZIP")
    print("="*60)
    
    results = TestResults()
    
    # Check file exists
    if not os.path.exists(REAL_ZIP_PATH):
        results.test("Real ZIP exists", False, f"File not found: {REAL_ZIP_PATH}")
        return results
    
    results.test("Real ZIP exists", True, f"Size: {os.path.getsize(REAL_ZIP_PATH) / 1024 / 1024:.1f} MB")
    
    # Run QA
    try:
        report = run_qa(REAL_ZIP_PATH, f"{SESSION_ID}.zip")
        print(f"\nQA Report:")
        print(f"  Session ID: {report.session_id}")
        print(f"  Verdict: {report.verdict}")
        print(f"  Missing fields: {report.missing_fields}")
        print(f"  Failure reasons: {report.failure_reasons}")
        
        # Test verdict
        results.test(
            "Verdict is PASS",
            report.verdict == "PASS",
            f"Got: {report.verdict}, failures: {report.failure_reasons}"
        )
        
        # Test missing fields
        results.test(
            "No missing fields",
            len(report.missing_fields) == 0,
            f"Missing: {report.missing_fields}"
        )
        
        # Test session ID
        results.test(
            "Session ID matches",
            report.session_id == SESSION_ID,
            f"Expected: {SESSION_ID}, Got: {report.session_id}"
        )
        
        # Test each expected field
        for field, expected_value in EXPECTED.items():
            actual_value = report.fields.get(field)
            
            if field == "duration_hours":
                # Allow small tolerance for float comparison
                match = actual_value is not None and abs(actual_value - expected_value) < 0.01
                results.test(
                    f"Field '{field}' matches",
                    match,
                    f"Expected: ~{expected_value}, Got: {actual_value}"
                )
            else:
                match = actual_value == expected_value
                results.test(
                    f"Field '{field}' matches",
                    match,
                    f"Expected: {expected_value}, Got: {actual_value}"
                )
        
        # Test reconnect_summary structure
        reconnect_summary = report.fields.get("reconnect_summary")
        if reconnect_summary:
            print(f"\nReconnect summary: {reconnect_summary}")
            results.test(
                "Reconnect summary is list",
                isinstance(reconnect_summary, list),
                f"Type: {type(reconnect_summary)}"
            )
            
            if isinstance(reconnect_summary, list):
                results.test(
                    "Reconnect summary has 2 entries",
                    len(reconnect_summary) == 2,
                    f"Count: {len(reconnect_summary)}"
                )
                
                # Check that recovered is None (not False, which would fail)
                all_none = all(e.get("recovered") is None for e in reconnect_summary if isinstance(e, dict))
                results.test(
                    "All reconnect entries have recovered=None",
                    all_none,
                    f"Entries: {reconnect_summary}"
                )
        
    except Exception as e:
        results.test("run_qa() executes without error", False, str(e))
        import traceback
        traceback.print_exc()
    
    return results


def test_parser_discovers_all_files():
    """Test 2: Parser discovers all 3 JSON files."""
    print("\n" + "="*60)
    print("TEST 2: Parser discovers all 3 JSON files")
    print("="*60)
    
    results = TestResults()
    
    try:
        inspection = inspect_zip(REAL_ZIP_PATH)
        manifest_result = parse_manifest_from_source(REAL_ZIP_PATH, inspection)
        
        print(f"\nEntries found: {manifest_result.entries_found}")
        
        expected_files = {
            "manifest.json",
            "runtime_status.json",
            "fast_collection_summary.json"
        }
        
        results.test(
            "All 3 JSON files discovered",
            set(manifest_result.entries_found) == expected_files,
            f"Found: {manifest_result.entries_found}"
        )
        
        # Check derivations
        print(f"\nDerivations: {list(manifest_result.derivations.keys())}")
        results.test(
            "Derivations present",
            len(manifest_result.derivations) > 0,
            f"Count: {len(manifest_result.derivations)}"
        )
        
    except Exception as e:
        results.test("Parser executes without error", False, str(e))
        import traceback
        traceback.print_exc()
    
    return results


def test_api_reprocess():
    """Test 3: API reprocess endpoint."""
    print("\n" + "="*60)
    print("TEST 3: API reprocess endpoint")
    print("="*60)
    
    results = TestResults()
    
    try:
        # Use a session to maintain cookies
        session = requests.Session()
        
        # Login first
        login_url = f"{BACKEND_URL}/api/auth/login"
        login_data = {"password": SUPERBOT_PASSWORD}
        
        print(f"\nLogging in to {login_url}...")
        login_resp = session.post(login_url, json=login_data, timeout=10)
        
        results.test(
            "Login successful",
            login_resp.status_code == 200,
            f"Status: {login_resp.status_code}"
        )
        
        if login_resp.status_code != 200:
            print(f"Login response: {login_resp.text}")
            return results
        
        login_data = login_resp.json()
        results.test(
            "Login authenticated",
            login_data.get("authenticated") is True,
            f"Response: {login_data}"
        )
        
        # Reprocess the session
        reprocess_url = f"{BACKEND_URL}/api/sessions/{SESSION_ID}/reprocess"
        
        print(f"\nReprocessing session at {reprocess_url}...")
        reprocess_resp = session.post(reprocess_url, timeout=30)
        
        results.test(
            "Reprocess request successful",
            reprocess_resp.status_code == 200,
            f"Status: {reprocess_resp.status_code}"
        )
        
        if reprocess_resp.status_code != 200:
            print(f"Reprocess response: {reprocess_resp.text}")
            return results
        
        reprocess_data = reprocess_resp.json()
        print(f"\nReprocess response:")
        print(f"  Verdict: {reprocess_data.get('verdict')}")
        print(f"  Missing fields: {reprocess_data.get('missing_fields')}")
        print(f"  Failure reasons: {reprocess_data.get('failure_reasons')}")
        
        results.test(
            "Reprocess verdict is PASS",
            reprocess_data.get("verdict") == "PASS",
            f"Got: {reprocess_data.get('verdict')}"
        )
        
        results.test(
            "Reprocess has no missing fields",
            reprocess_data.get("missing_fields") == [],
            f"Missing: {reprocess_data.get('missing_fields')}"
        )
        
        results.test(
            "Reprocess has no failure reasons",
            reprocess_data.get("failure_reasons") == [],
            f"Failures: {reprocess_data.get('failure_reasons')}"
        )
        
    except Exception as e:
        results.test("API reprocess executes without error", False, str(e))
        import traceback
        traceback.print_exc()
    
    return results


def test_hours_accounting():
    """Test 4: Hours accounting via API."""
    print("\n" + "="*60)
    print("TEST 4: Hours accounting")
    print("="*60)
    
    results = TestResults()
    
    try:
        # Use a session to maintain cookies
        session = requests.Session()
        
        # Login
        login_url = f"{BACKEND_URL}/api/auth/login"
        login_data = {"password": SUPERBOT_PASSWORD}
        login_resp = session.post(login_url, json=login_data, timeout=10)
        
        if login_resp.status_code != 200:
            results.test("Login for hours test", False, "Login failed")
            return results
        
        # Get overview
        overview_url = f"{BACKEND_URL}/api/overview"
        print(f"\nFetching overview from {overview_url}...")
        overview_resp = session.get(overview_url, timeout=10)
        
        results.test(
            "Overview request successful",
            overview_resp.status_code == 200,
            f"Status: {overview_resp.status_code}"
        )
        
        if overview_resp.status_code == 200:
            overview_data = overview_resp.json()
            print(f"\nOverview data:")
            print(f"  Sessions: {overview_data.get('sessions')}")
            print(f"  Verdict counts: {overview_data.get('verdict_counts')}")
            
            # Check that we have at least one session
            session_count = overview_data.get("sessions", 0)
            results.test(
                "At least one session exists",
                session_count > 0,
                f"Sessions: {session_count}"
            )
            
            # Check checkpoints in overview
            checkpoints = overview_data.get("checkpoints", {})
            print(f"  Checkpoints in overview: {checkpoints}")
        
        # Get checkpoints
        checkpoints_url = f"{BACKEND_URL}/api/checkpoints"
        print(f"\nFetching checkpoints from {checkpoints_url}...")
        checkpoints_resp = session.get(checkpoints_url, timeout=10)
        
        results.test(
            "Checkpoints request successful",
            checkpoints_resp.status_code == 200,
            f"Status: {checkpoints_resp.status_code}"
        )
        
        if checkpoints_resp.status_code == 200:
            checkpoints_data = checkpoints_resp.json()
            checkpoints_dict = checkpoints_data.get('checkpoints', {})
            print(f"\nCheckpoints: {len(checkpoints_dict)} found")
            
            # Check if any checkpoint has validated hours
            total_hours = 0
            for name, data in checkpoints_dict.items():
                hours = data.get('hours', 0)
                total_hours += hours
                if hours > 0:
                    print(f"  Checkpoint {name}: {hours} hours (target: {data.get('target')})")
            
            results.test(
                "Total validated hours > 0 across checkpoints",
                total_hours > 0,
                f"Total hours: {total_hours}"
            )
        
    except Exception as e:
        results.test("Hours accounting test executes without error", False, str(e))
        import traceback
        traceback.print_exc()
    
    return results


def test_policy_unchanged():
    """Test 5: Policy endpoint unchanged."""
    print("\n" + "="*60)
    print("TEST 5: Policy endpoint unchanged")
    print("="*60)
    
    results = TestResults()
    
    try:
        policy_url = f"{BACKEND_URL}/api/policy"
        print(f"\nFetching policy from {policy_url}...")
        policy_resp = requests.get(policy_url, timeout=10)
        
        results.test(
            "Policy request successful",
            policy_resp.status_code == 200,
            f"Status: {policy_resp.status_code}"
        )
        
        if policy_resp.status_code == 200:
            policy_data = policy_resp.json()
            print(f"\nPolicy data:")
            print(f"  Collector SHA256: {policy_data.get('collector_sha256')}")
            print(f"  Engine status: {policy_data.get('engine', {}).get('status')}")
            
            results.test(
                "Collector SHA256 matches",
                policy_data.get("collector_sha256") == EXPECTED["collector_sha256"],
                f"Expected: {EXPECTED['collector_sha256']}, Got: {policy_data.get('collector_sha256')}"
            )
            
            results.test(
                "Engine status is NOT_CONFIGURED",
                policy_data.get("engine", {}).get("status") == "NOT_CONFIGURED",
                f"Got: {policy_data.get('engine', {}).get('status')}"
            )
        
    except Exception as e:
        results.test("Policy test executes without error", False, str(e))
        import traceback
        traceback.print_exc()
    
    return results


def main():
    print("\n" + "="*60)
    print("REAL ZIP COMPREHENSIVE TEST SUITE")
    print("="*60)
    print(f"Real ZIP: {REAL_ZIP_PATH}")
    print(f"Session ID: {SESSION_ID}")
    print(f"Backend URL: {BACKEND_URL}")
    
    all_results = []
    
    # Run all tests
    all_results.append(test_direct_run_qa())
    all_results.append(test_parser_discovers_all_files())
    all_results.append(test_api_reprocess())
    all_results.append(test_hours_accounting())
    all_results.append(test_policy_unchanged())
    
    # Overall summary
    print("\n" + "="*60)
    print("OVERALL SUMMARY")
    print("="*60)
    
    total_tests = sum(r.tests_run for r in all_results)
    total_passed = sum(r.tests_passed for r in all_results)
    
    print(f"Total tests: {total_passed}/{total_tests} passed")
    
    all_failures = []
    for r in all_results:
        all_failures.extend(r.failures)
    
    if all_failures:
        print(f"\nAll failures:")
        for f in all_failures:
            print(f"  - {f}")
    
    print("="*60)
    
    return 0 if total_passed == total_tests else 1


if __name__ == "__main__":
    sys.exit(main())
