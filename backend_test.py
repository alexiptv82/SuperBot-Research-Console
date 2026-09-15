"""SuperBot Research Console V1 - Backend API Tests

Tests all backend endpoints using the public URL and synthetic ZIP fixtures.
"""
import sys
import requests
from pathlib import Path
import time

# Add backend paths for imports
sys.path.insert(0, '/app/backend/tests')
sys.path.insert(0, '/app/backend')

from fixtures import BuildOptions, build_zip
from constants import FROZEN_COLLECTOR_SHA256

BASE_URL = "https://superbot-validate.preview.emergentagent.com/api"
PASSWORD = "superbot"

# Use timestamp to ensure unique session IDs across test runs
TEST_RUN_ID = str(int(time.time()))

class Colors:
    GREEN = '\033[92m'
    RED = '\033[91m'
    YELLOW = '\033[93m'
    BLUE = '\033[94m'
    END = '\033[0m'

class BackendTester:
    def __init__(self):
        self.tests_run = 0
        self.tests_passed = 0
        self.tests_failed = 0
        self.session_cookie = None
        self.failures = []
        
    def log(self, emoji, message, color=Colors.END):
        print(f"{color}{emoji} {message}{Colors.END}")
        
    def test(self, name, func):
        """Run a single test"""
        self.tests_run += 1
        self.log("🔍", f"Testing: {name}", Colors.BLUE)
        try:
            func()
            self.tests_passed += 1
            self.log("✅", f"PASSED: {name}", Colors.GREEN)
            return True
        except AssertionError as e:
            self.tests_failed += 1
            self.log("❌", f"FAILED: {name} - {str(e)}", Colors.RED)
            self.failures.append({"test": name, "error": str(e)})
            return False
        except Exception as e:
            self.tests_failed += 1
            self.log("❌", f"ERROR: {name} - {str(e)}", Colors.RED)
            self.failures.append({"test": name, "error": f"Exception: {str(e)}"})
            return False
    
    def get(self, endpoint, expected_status=200, use_auth=False):
        """Helper for GET requests"""
        url = f"{BASE_URL}/{endpoint}"
        headers = {}
        cookies = {}
        if use_auth and self.session_cookie:
            cookies = self.session_cookie
        
        resp = requests.get(url, headers=headers, cookies=cookies)
        assert resp.status_code == expected_status, \
            f"Expected {expected_status}, got {resp.status_code}. Body: {resp.text[:200]}"
        return resp
    
    def post(self, endpoint, json=None, data=None, files=None, expected_status=200, use_auth=False):
        """Helper for POST requests"""
        url = f"{BASE_URL}/{endpoint}"
        headers = {}
        cookies = {}
        if use_auth and self.session_cookie:
            cookies = self.session_cookie
        
        resp = requests.post(url, json=json, data=data, files=files, headers=headers, cookies=cookies)
        assert resp.status_code == expected_status, \
            f"Expected {expected_status}, got {resp.status_code}. Body: {resp.text[:200]}"
        return resp
    
    # ========== PUBLIC ENDPOINTS ==========
    
    def test_health(self):
        """GET /api/health returns 200 without auth"""
        resp = self.get("health", expected_status=200)
        data = resp.json()
        assert data["status"] == "ok", f"Expected status=ok, got {data}"
        assert "superbot" in data["service"].lower(), f"Expected superbot in service name"
    
    def test_policy(self):
        """GET /api/policy returns frozen SHA256 and engine status"""
        resp = self.get("policy", expected_status=200)
        data = resp.json()
        
        # Check frozen collector SHA256
        assert data["collector_sha256"] == FROZEN_COLLECTOR_SHA256, \
            f"Expected {FROZEN_COLLECTOR_SHA256}, got {data['collector_sha256']}"
        
        # Check engine status
        assert "engine" in data, "Missing engine field"
        assert data["engine"]["status"] == "NOT_CONFIGURED", \
            f"Expected NOT_CONFIGURED, got {data['engine']['status']}"
        
        # Check verdicts
        assert set(data["verdicts"]) == {"PASS", "PASS_WITH_WARNING", "FAIL", "UNRESOLVED"}
        
        # Check duplicate states
        assert set(data["duplicate_states"]) == {"NEW", "EXACT_DUPLICATE", "SAME_SESSION_DIFFERENT_FILE", "CONFLICT"}
    
    # ========== AUTH ENDPOINTS ==========
    
    def test_login_wrong_password(self):
        """POST /api/auth/login with wrong password returns 401"""
        resp = requests.post(f"{BASE_URL}/auth/login", json={"password": "wrongpassword"})
        assert resp.status_code == 401, f"Expected 401, got {resp.status_code}"
    
    def test_login_correct_password(self):
        """POST /api/auth/login with correct password sets session cookie"""
        resp = requests.post(f"{BASE_URL}/auth/login", json={"password": PASSWORD})
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}. Body: {resp.text}"
        data = resp.json()
        assert data["authenticated"] == True, "Expected authenticated=True"
        
        # Store session cookie for subsequent requests
        assert len(resp.cookies) > 0, "No session cookie set"
        self.session_cookie = resp.cookies
        self.log("🔑", f"Session cookie obtained: {list(resp.cookies.keys())}", Colors.YELLOW)
    
    # ========== PROTECTED ENDPOINTS (401 without auth) ==========
    
    def test_sessions_requires_auth(self):
        """GET /api/sessions returns 401 without session"""
        resp = requests.get(f"{BASE_URL}/sessions")
        assert resp.status_code == 401, f"Expected 401, got {resp.status_code}"
    
    def test_checkpoints_requires_auth(self):
        """GET /api/checkpoints returns 401 without session"""
        resp = requests.get(f"{BASE_URL}/checkpoints")
        assert resp.status_code == 401, f"Expected 401, got {resp.status_code}"
    
    def test_audit_requires_auth(self):
        """GET /api/audit returns 401 without session"""
        resp = requests.get(f"{BASE_URL}/audit")
        assert resp.status_code == 401, f"Expected 401, got {resp.status_code}"
    
    def test_overview_requires_auth(self):
        """GET /api/overview returns 401 without session"""
        resp = requests.get(f"{BASE_URL}/overview")
        assert resp.status_code == 401, f"Expected 401, got {resp.status_code}"
    
    # ========== UPLOAD TESTS ==========
    
    def test_upload_pass_zip(self):
        """Upload synthetic PASS ZIP returns duplicate_status=NEW, verdict=PASS, validated_hours=3.0, retained=false"""
        zip_bytes = build_zip(BuildOptions(
            session_id=f'20260910T123759Z_test_pass_{TEST_RUN_ID}',
            collector_sha256=FROZEN_COLLECTOR_SHA256,
            exit_code=0,
            watchdog=False,
            writer_errors=0
        ))
        
        files = {'files': ('test_pass.zip', zip_bytes, 'application/zip')}
        data = {'retain_raw': 'false'}
        
        resp = self.post("sessions/upload", files=files, data=data, expected_status=200, use_auth=True)
        result = resp.json()
        
        assert result["count"] == 1, f"Expected 1 result, got {result['count']}"
        item = result["results"][0]
        
        assert item["duplicate_status"] == "NEW", f"Expected NEW, got {item['duplicate_status']}"
        assert item["verdict"] == "PASS", f"Expected PASS, got {item['verdict']}"
        assert item["validated_hours"] == 3.0, f"Expected 3.0, got {item['validated_hours']}"
        assert item["retained"] == False, f"Expected retained=False, got {item['retained']}"
        
        self.log("📦", f"Uploaded PASS ZIP: {item['session_id']}", Colors.YELLOW)
        return item
    
    def test_upload_wrong_collector_hash(self):
        """Upload with wrong collector hash returns verdict=FAIL, retained=true"""
        zip_bytes = build_zip(BuildOptions(
            session_id=f'20260910T140000Z_test_fail_{TEST_RUN_ID}',
            collector_sha256='b' * 64,  # Wrong hash
            exit_code=0,
            watchdog=False
        ))
        
        files = {'files': ('test_fail.zip', zip_bytes, 'application/zip')}
        data = {'retain_raw': 'false'}
        
        resp = self.post("sessions/upload", files=files, data=data, expected_status=200, use_auth=True)
        result = resp.json()
        
        item = result["results"][0]
        assert item["verdict"] == "FAIL", f"Expected FAIL, got {item['verdict']}"
        assert "Collector SHA256 mismatch" in str(item["failure_reasons"]), \
            f"Expected 'Collector SHA256 mismatch' in failure_reasons, got {item['failure_reasons']}"
        assert item["retained"] == True, f"Expected retained=True for FAIL, got {item['retained']}"
        
        self.log("📦", f"Uploaded FAIL ZIP (wrong hash): {item['session_id']}", Colors.YELLOW)
    
    def test_upload_missing_collector_field(self):
        """Upload with missing collector field returns verdict=UNRESOLVED"""
        zip_bytes = build_zip(BuildOptions(
            session_id=f'20260910T150000Z_test_unresolved_{TEST_RUN_ID}',
            collector_sha256=None,  # Missing field
            exit_code=0
        ))
        
        files = {'files': ('test_unresolved.zip', zip_bytes, 'application/zip')}
        data = {'retain_raw': 'false'}
        
        resp = self.post("sessions/upload", files=files, data=data, expected_status=200, use_auth=True)
        result = resp.json()
        
        item = result["results"][0]
        assert item["verdict"] == "UNRESOLVED", f"Expected UNRESOLVED, got {item['verdict']}"
        assert "collector_sha256" in item["missing_fields"], \
            f"Expected 'collector_sha256' in missing_fields, got {item['missing_fields']}"
        
        self.log("📦", f"Uploaded UNRESOLVED ZIP (missing collector): {item['session_id']}", Colors.YELLOW)
    
    def test_upload_exact_duplicate(self):
        """Uploading the SAME ZIP twice yields duplicate_status=EXACT_DUPLICATE on second upload"""
        zip_bytes = build_zip(BuildOptions(
            session_id=f'20260910T160000Z_test_dup_{TEST_RUN_ID}',
            collector_sha256=FROZEN_COLLECTOR_SHA256,
            exit_code=0
        ))
        
        files = {'files': ('test_dup.zip', zip_bytes, 'application/zip')}
        data = {'retain_raw': 'false'}
        
        # First upload
        resp1 = self.post("sessions/upload", files=files, data=data, expected_status=200, use_auth=True)
        item1 = resp1.json()["results"][0]
        assert item1["duplicate_status"] == "NEW", f"First upload should be NEW"
        assert item1["validated_hours"] == 3.0, f"First upload should have 3.0 hours"
        
        # Second upload (same bytes)
        files = {'files': ('test_dup.zip', zip_bytes, 'application/zip')}
        resp2 = self.post("sessions/upload", files=files, data=data, expected_status=200, use_auth=True)
        item2 = resp2.json()["results"][0]
        assert item2["duplicate_status"] == "EXACT_DUPLICATE", \
            f"Second upload should be EXACT_DUPLICATE, got {item2['duplicate_status']}"
        assert item2["validated_hours"] == 0.0, \
            f"Duplicate should have 0 validated_hours, got {item2['validated_hours']}"
        
        self.log("📦", f"Verified EXACT_DUPLICATE detection", Colors.YELLOW)
    
    def test_upload_same_session_different_file(self):
        """Uploading two ZIPs with SAME session_id but DIFFERENT bytes yields SAME_SESSION_DIFFERENT_FILE"""
        session_id = f'20260910T170000Z_test_diff_{TEST_RUN_ID}'
        
        # First upload
        zip_bytes1 = build_zip(BuildOptions(
            session_id=session_id,
            collector_sha256=FROZEN_COLLECTOR_SHA256,
            exit_code=0,
            writer_errors=0
        ))
        files1 = {'files': ('test_diff1.zip', zip_bytes1, 'application/zip')}
        resp1 = self.post("sessions/upload", files=files1, data={'retain_raw': 'false'}, 
                         expected_status=200, use_auth=True)
        item1 = resp1.json()["results"][0]
        assert item1["duplicate_status"] == "NEW"
        
        # Second upload with same session_id but different content
        zip_bytes2 = build_zip(BuildOptions(
            session_id=session_id,
            collector_sha256=FROZEN_COLLECTOR_SHA256,
            exit_code=0,
            writer_errors=1  # Different content
        ))
        files2 = {'files': ('test_diff2.zip', zip_bytes2, 'application/zip')}
        resp2 = self.post("sessions/upload", files=files2, data={'retain_raw': 'false'}, 
                         expected_status=200, use_auth=True)
        item2 = resp2.json()["results"][0]
        assert item2["duplicate_status"] == "SAME_SESSION_DIFFERENT_FILE", \
            f"Expected SAME_SESSION_DIFFERENT_FILE, got {item2['duplicate_status']}"
        
        self.log("📦", f"Verified SAME_SESSION_DIFFERENT_FILE detection", Colors.YELLOW)
    
    # ========== REGISTRY TESTS ==========
    
    def test_sessions_list(self):
        """GET /api/sessions lists sessions with latest QA run"""
        resp = self.get("sessions", expected_status=200, use_auth=True)
        data = resp.json()
        
        assert "count" in data, "Missing count field"
        assert "sessions" in data, "Missing sessions field"
        assert data["count"] >= 0, "Count should be non-negative"
        
        self.log("📋", f"Sessions list returned {data['count']} sessions", Colors.YELLOW)
    
    def test_sessions_filter_by_status(self):
        """GET /api/sessions?status=PASS filters by status"""
        resp = self.get("sessions?status=PASS", expected_status=200, use_auth=True)
        data = resp.json()
        
        # All returned sessions should have PASS status
        for session in data["sessions"]:
            assert session["operational_status"] == "PASS", \
                f"Expected PASS, got {session['operational_status']}"
        
        self.log("📋", f"Status filter returned {data['count']} PASS sessions", Colors.YELLOW)
    
    def test_sessions_search(self):
        """GET /api/sessions?q=test searches sessions"""
        resp = self.get("sessions?q=test", expected_status=200, use_auth=True)
        data = resp.json()
        
        # All returned sessions should contain 'test' in session_id or filename
        for session in data["sessions"]:
            assert "test" in session["session_id"].lower() or \
                   "test" in (session["original_filename"] or "").lower(), \
                   f"Search term 'test' not found in session"
        
        self.log("📋", f"Search returned {data['count']} matching sessions", Colors.YELLOW)
    
    def test_session_detail(self):
        """GET /api/sessions/{session_id} returns qa_runs list"""
        # First, get a session_id from the list
        resp = self.get("sessions", expected_status=200, use_auth=True)
        sessions = resp.json()["sessions"]
        
        if len(sessions) == 0:
            self.log("⚠️", "No sessions to test detail endpoint", Colors.YELLOW)
            return
        
        session_id = sessions[0]["session_id"]
        resp = self.get(f"sessions/{session_id}", expected_status=200, use_auth=True)
        data = resp.json()
        
        assert data["session_id"] == session_id
        assert "qa_runs" in data, "Missing qa_runs field"
        assert isinstance(data["qa_runs"], list), "qa_runs should be a list"
        assert len(data["qa_runs"]) > 0, "qa_runs should not be empty"
        
        self.log("📋", f"Session detail returned {len(data['qa_runs'])} QA runs", Colors.YELLOW)
    
    # ========== CHECKPOINTS TESTS ==========
    
    def test_checkpoints(self):
        """GET /api/checkpoints returns OLD36/NEW12/TOTAL48/NEW36/TOTAL72 with correct targets"""
        resp = self.get("checkpoints", expected_status=200, use_auth=True)
        data = resp.json()
        
        expected_checkpoints = {
            "OLD36": 36.0,
            "NEW12": 12.0,
            "TOTAL48": 48.0,
            "NEW36": 36.0,
            "TOTAL72": 72.0
        }
        
        assert "checkpoints" in data, "Missing checkpoints field"
        assert "data_qa_ready" in data, "Missing data_qa_ready field"
        
        for cp_name, target in expected_checkpoints.items():
            assert cp_name in data["checkpoints"], f"Missing checkpoint {cp_name}"
            cp = data["checkpoints"][cp_name]
            assert cp["target"] == target, f"Expected target {target} for {cp_name}, got {cp['target']}"
            assert "hours" in cp, f"Missing hours field for {cp_name}"
            assert isinstance(cp["hours"], (int, float)), f"hours should be numeric for {cp_name}"
        
        self.log("🎯", f"Checkpoints: data_qa_ready={data['data_qa_ready']}", Colors.YELLOW)
    
    # ========== REPORTS TESTS ==========
    
    def test_export_json(self):
        """GET /api/reports/export?fmt=json returns downloadable JSON"""
        resp = self.get("reports/export?fmt=json", expected_status=200, use_auth=True)
        assert "application/json" in resp.headers.get("Content-Type", "")
        assert "attachment" in resp.headers.get("Content-Disposition", "")
        
        # Verify it's valid JSON
        data = resp.json()
        assert isinstance(data, (list, dict)), "JSON response should be list or dict"
        
        self.log("📄", "JSON export successful", Colors.YELLOW)
    
    def test_export_csv(self):
        """GET /api/reports/export?fmt=csv returns downloadable CSV"""
        resp = self.get("reports/export?fmt=csv", expected_status=200, use_auth=True)
        assert "text/csv" in resp.headers.get("Content-Type", "")
        assert "attachment" in resp.headers.get("Content-Disposition", "")
        
        # Verify it's CSV-like (has commas or newlines)
        content = resp.text
        assert "," in content or "\n" in content, "CSV should have commas or newlines"
        
        self.log("📄", "CSV export successful", Colors.YELLOW)
    
    def test_export_markdown(self):
        """GET /api/reports/export?fmt=md returns downloadable Markdown"""
        resp = self.get("reports/export?fmt=md", expected_status=200, use_auth=True)
        assert "text/markdown" in resp.headers.get("Content-Type", "")
        assert "attachment" in resp.headers.get("Content-Disposition", "")
        
        content = resp.text
        assert len(content) > 0, "Markdown should not be empty"
        
        self.log("📄", "Markdown export successful", Colors.YELLOW)
    
    # ========== AUDIT LOG TESTS ==========
    
    def test_audit_log(self):
        """GET /api/audit returns append-only events"""
        resp = self.get("audit", expected_status=200, use_auth=True)
        data = resp.json()
        
        assert "count" in data, "Missing count field"
        assert "events" in data, "Missing events field"
        assert isinstance(data["events"], list), "events should be a list"
        
        # Check for expected event types after our actions
        event_types = [e["event_type"] for e in data["events"]]
        
        # We should have login_success from our login
        assert "auth.login_success" in event_types, "Missing auth.login_success event"
        
        # We should have upload.qa events from our uploads
        upload_events = [e for e in data["events"] if e["event_type"] == "upload.qa"]
        assert len(upload_events) > 0, "Missing upload.qa events"
        
        # Check for login_failed from wrong password test
        failed_logins = [e for e in data["events"] if e["event_type"] == "auth.login_failed"]
        assert len(failed_logins) > 0, "Missing auth.login_failed event"
        
        self.log("📜", f"Audit log returned {data['count']} events", Colors.YELLOW)
    
    # ========== REPROCESS TESTS ==========
    
    def test_reprocess_retained_session(self):
        """POST /api/sessions/{id}/reprocess works only when raw was retained"""
        # Upload a FAIL ZIP which should be retained
        zip_bytes = build_zip(BuildOptions(
            session_id=f'20260910T180000Z_test_reprocess_{TEST_RUN_ID}',
            collector_sha256='c' * 64,  # Wrong hash -> FAIL -> retained
            exit_code=0
        ))
        
        files = {'files': ('test_reprocess.zip', zip_bytes, 'application/zip')}
        resp = self.post("sessions/upload", files=files, data={'retain_raw': 'false'}, 
                        expected_status=200, use_auth=True)
        item = resp.json()["results"][0]
        session_id = item["session_id"]
        
        assert item["retained"] == True, "FAIL ZIP should be retained"
        
        # Now try to reprocess
        resp = self.post(f"sessions/{session_id}/reprocess", expected_status=200, use_auth=True)
        data = resp.json()
        
        assert data["session_id"] == session_id, "Reprocessed session_id should match"
        assert data["verdict"] == "FAIL", "Reprocessed verdict should still be FAIL"
        
        self.log("🔄", f"Reprocess successful for {session_id}", Colors.YELLOW)
    
    def test_reprocess_not_retained_fails(self):
        """Reprocess fails when raw was not retained (PASS with retain_raw=false)"""
        # We already uploaded PASS ZIPs earlier which were not retained
        # Try to find one and reprocess it
        resp = self.get("sessions?status=PASS", expected_status=200, use_auth=True)
        sessions = resp.json()["sessions"]
        
        # Find a PASS session that was not retained
        not_retained = [s for s in sessions if not s.get("retained", True)]
        
        if len(not_retained) == 0:
            self.log("⚠️", "No non-retained PASS sessions to test reprocess failure", Colors.YELLOW)
            return
        
        session_id = not_retained[0]["session_id"]
        
        # Try to reprocess - should fail with 409
        resp = requests.post(
            f"{BASE_URL}/sessions/{session_id}/reprocess",
            cookies=self.session_cookie
        )
        assert resp.status_code == 409, \
            f"Expected 409 for non-retained session, got {resp.status_code}"
        
        self.log("🔄", f"Reprocess correctly failed for non-retained session", Colors.YELLOW)
    
    # ========== RUN ALL TESTS ==========
    
    def run_all(self):
        """Run all tests in order"""
        print("\n" + "="*80)
        print("🚀 SuperBot Research Console V1 - Backend API Tests")
        print("="*80 + "\n")
        
        # Public endpoints (no auth required)
        self.test("Health endpoint", self.test_health)
        self.test("Policy endpoint", self.test_policy)
        
        # Auth tests
        self.test("Login with wrong password", self.test_login_wrong_password)
        self.test("Login with correct password", self.test_login_correct_password)
        
        # Protected endpoints require auth
        self.test("Sessions requires auth", self.test_sessions_requires_auth)
        self.test("Checkpoints requires auth", self.test_checkpoints_requires_auth)
        self.test("Audit requires auth", self.test_audit_requires_auth)
        self.test("Overview requires auth", self.test_overview_requires_auth)
        
        # Upload tests (require auth)
        self.test("Upload PASS ZIP", self.test_upload_pass_zip)
        self.test("Upload with wrong collector hash", self.test_upload_wrong_collector_hash)
        self.test("Upload with missing collector field", self.test_upload_missing_collector_field)
        self.test("Upload exact duplicate", self.test_upload_exact_duplicate)
        self.test("Upload same session different file", self.test_upload_same_session_different_file)
        
        # Registry tests
        self.test("Sessions list", self.test_sessions_list)
        self.test("Sessions filter by status", self.test_sessions_filter_by_status)
        self.test("Sessions search", self.test_sessions_search)
        self.test("Session detail", self.test_session_detail)
        
        # Checkpoints
        self.test("Checkpoints endpoint", self.test_checkpoints)
        
        # Reports
        self.test("Export JSON", self.test_export_json)
        self.test("Export CSV", self.test_export_csv)
        self.test("Export Markdown", self.test_export_markdown)
        
        # Audit log
        self.test("Audit log", self.test_audit_log)
        
        # Reprocess
        self.test("Reprocess retained session", self.test_reprocess_retained_session)
        self.test("Reprocess non-retained fails", self.test_reprocess_not_retained_fails)
        
        # Summary
        print("\n" + "="*80)
        print("📊 TEST SUMMARY")
        print("="*80)
        print(f"Total tests: {self.tests_run}")
        print(f"{Colors.GREEN}✅ Passed: {self.tests_passed}{Colors.END}")
        print(f"{Colors.RED}❌ Failed: {self.tests_failed}{Colors.END}")
        print(f"Success rate: {(self.tests_passed/self.tests_run*100):.1f}%")
        
        if self.failures:
            print(f"\n{Colors.RED}Failed tests:{Colors.END}")
            for f in self.failures:
                print(f"  - {f['test']}: {f['error']}")
        
        print("="*80 + "\n")
        
        return self.tests_passed == self.tests_run

if __name__ == "__main__":
    tester = BackendTester()
    success = tester.run_all()
    sys.exit(0 if success else 1)
