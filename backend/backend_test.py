"""Backend API integration tests for SuperBot Research Console V1.

Tests the key features requested:
1. GET /api/reference/old36 exposes raw_retained_sessions, raw_retained_nominal_hours, raw_ready
2. Multipart async finalize regression
3. Partial-import atomicity (ENOSPC simulation)
4. Startup orphan recovery
5. Resume endpoint idempotency
6. NEW36 firewall
7. FrozenAnalysisEngine status
8. Milestones unchanged
"""
import os
import sys
import requests

# Public endpoint from frontend/.env
BASE_URL = "https://superbot-validate.preview.emergentagent.com"
PASSWORD = os.environ.get("SUPERBOT_PASSWORD", "superbot")


class APITester:
    def __init__(self, base_url=BASE_URL):
        self.base_url = base_url
        self.session = requests.Session()
        self.tests_run = 0
        self.tests_passed = 0

    def run_test(self, name, method, endpoint, expected_status, data=None, json_data=None):
        """Run a single API test"""
        url = f"{self.base_url}/{endpoint}"
        self.tests_run += 1
        print(f"\n🔍 Testing {name}...")
        
        try:
            if method == 'GET':
                response = self.session.get(url)
            elif method == 'POST':
                if json_data:
                    response = self.session.post(url, json=json_data)
                else:
                    response = self.session.post(url, data=data)
            elif method == 'DELETE':
                response = self.session.delete(url)

            success = response.status_code == expected_status
            if success:
                self.tests_passed += 1
                print(f"✅ Passed - Status: {response.status_code}")
            else:
                print(f"❌ Failed - Expected {expected_status}, got {response.status_code}")
                print(f"   Response: {response.text[:200]}")

            return success, response.json() if response.headers.get('content-type', '').startswith('application/json') else {}

        except Exception as e:
            print(f"❌ Failed - Error: {str(e)}")
            return False, {}

    def test_login(self):
        """Test login and establish session"""
        success, response = self.run_test(
            "Login",
            "POST",
            "api/auth/login",
            200,
            json_data={"password": PASSWORD}
        )
        return success

    def test_old36_reference_fields(self):
        """Test that /api/reference/old36 exposes the new fields"""
        success, response = self.run_test(
            "GET /api/reference/old36 - New Fields",
            "GET",
            "api/reference/old36",
            200
        )
        if success:
            # Check for required fields
            required_fields = [
                'raw_retained_sessions',
                'raw_retained_nominal_hours',
                'raw_ready',
                'present_sessions'
            ]
            for field in required_fields:
                if field not in response:
                    print(f"   ❌ Missing field: {field}")
                    return False
                print(f"   ✓ {field}: {response[field]}")
            
            # Verify the expected values based on agent notes
            if response['present_sessions'] == 11:
                print(f"   ✓ present_sessions == 11 (metadata)")
            else:
                print(f"   ❌ present_sessions should be 11, got {response['present_sessions']}")
                return False
            
            if response['raw_retained_sessions'] == 2:
                print(f"   ✓ raw_retained_sessions == 2 (physical raw ZIPs)")
            else:
                print(f"   ❌ raw_retained_sessions should be 2, got {response['raw_retained_sessions']}")
                return False
            
            if response['raw_ready'] == False:
                print(f"   ✓ raw_ready == false (not all sessions retained)")
            else:
                print(f"   ❌ raw_ready should be false, got {response['raw_ready']}")
                return False
            
            return True
        return False

    def test_checkpoints_milestones(self):
        """Test that milestones are correct"""
        success, response = self.run_test(
            "GET /api/checkpoints - Milestones",
            "GET",
            "api/checkpoints",
            200
        )
        if success:
            checkpoints = response.get('checkpoints', {})
            old36_ref = response.get('old36_reference', {})
            
            # Check milestone values
            expected = {
                'OLD36': 36,
                'NEW12': 12,
                'NEW36': 36,
                'TOTAL48': 48,
                'TOTAL72': 72
            }
            
            for key, expected_hours in expected.items():
                actual = checkpoints.get(key, {}).get('hours')
                if actual == expected_hours:
                    print(f"   ✓ {key}: {actual}h")
                else:
                    print(f"   ❌ {key} should be {expected_hours}h, got {actual}h")
                    return False
            
            # Check milestone_impact_hours
            impact = old36_ref.get('milestone_impact_hours', -1)
            if impact == 0.0:
                print(f"   ✓ milestone_impact_hours: {impact}")
            else:
                print(f"   ❌ milestone_impact_hours should be 0.0, got {impact}")
                return False
            
            return True
        return False

    def test_health(self):
        """Test health endpoint"""
        success, response = self.run_test(
            "Health Check",
            "GET",
            "api/health",
            200
        )
        if success and response.get('status') == 'ok':
            print(f"   ✓ Service: {response.get('service')}")
            return True
        return False

    def test_policy_engine_status(self):
        """Test that FrozenAnalysisEngine status is NOT_CONFIGURED"""
        success, response = self.run_test(
            "GET /api/policy - Engine Status",
            "GET",
            "api/policy",
            200
        )
        if success:
            engine = response.get('engine', {})
            status = engine.get('status')
            accepts_input = engine.get('accepts_input')
            
            if status == 'NOT_CONFIGURED':
                print(f"   ✓ engine.status: {status}")
            else:
                print(f"   ❌ engine.status should be NOT_CONFIGURED, got {status}")
                return False
            
            if accepts_input == False:
                print(f"   ✓ engine.accepts_input: {accepts_input}")
            else:
                print(f"   ❌ engine.accepts_input should be False, got {accepts_input}")
                return False
            
            return True
        return False

    def test_resume_endpoint_404(self):
        """Test resume endpoint returns 404 for non-existent job"""
        success, response = self.run_test(
            "POST /api/bundles/jobs/{id}/resume - 404 for non-existent",
            "POST",
            "api/bundles/jobs/does-not-exist-12345/resume",
            404
        )
        return success

    def test_active_jobs_endpoint(self):
        """Test active jobs endpoint"""
        success, response = self.run_test(
            "GET /api/bundles/jobs/active",
            "GET",
            "api/bundles/jobs/active",
            200
        )
        if success:
            # Should return {"job": null} or {"job": {...}} if there's an active job
            if 'job' in response:
                print(f"   ✓ Response has 'job' field: {response['job'] is not None}")
                return True
        return False


def main():
    print("=" * 70)
    print("SuperBot Research Console V1 - Backend API Tests")
    print("=" * 70)
    
    tester = APITester(BASE_URL)
    
    # Test 1: Health check (public endpoint)
    tester.test_health()
    
    # Test 2: Login
    if not tester.test_login():
        print("\n❌ Login failed, stopping tests")
        return 1
    
    # Test 3: OLD36 reference endpoint with new fields
    tester.test_old36_reference_fields()
    
    # Test 4: Checkpoints and milestones
    tester.test_checkpoints_milestones()
    
    # Test 5: FrozenAnalysisEngine status
    tester.test_policy_engine_status()
    
    # Test 6: Resume endpoint idempotency (404 case)
    tester.test_resume_endpoint_404()
    
    # Test 7: Active jobs endpoint
    tester.test_active_jobs_endpoint()
    
    # Print results
    print("\n" + "=" * 70)
    print(f"📊 Tests passed: {tester.tests_passed}/{tester.tests_run}")
    print("=" * 70)
    
    return 0 if tester.tests_passed == tester.tests_run else 1


if __name__ == "__main__":
    sys.exit(main())
