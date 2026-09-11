import hashlib
import hmac
import json
import httpx

def test_webhooks():
    secret = "your-random-hex-secret-here"
    
    # 1. Test Health
    r = httpx.get("http://localhost:8000/health")
    print("Health Status:", r.status_code, r.json())
    assert r.status_code == 200

    # 2. Test Vercel Webhook with valid HMAC
    vercel_payload = {
        "type": "deployment.error",
        "deployment": {
            "id": "dpl_test_demo_01",
            "name": "payment-gateway",
            "target": "production",
            "error": "SyntaxError: Unexpected token",
            "buildLogs": "Traceback (most recent call last):\n  File \"app/routes.py\", line 15\nSyntaxError: invalid syntax"
        }
    }
    body = json.dumps(vercel_payload).encode()
    sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

    r = httpx.post(
        "http://localhost:8000/webhooks/vercel",
        content=body,
        headers={"Content-Type": "application/json", "X-Vercel-Signature": sig}
    )
    print("Vercel Webhook Status:", r.status_code, r.json())
    assert r.status_code == 202

    # 3. Test Railway Webhook with valid HMAC
    railway_payload = {
        "status": "DEPLOYMENT_FAILED",
        "deploymentId": "dep_railway_999",
        "projectName": "core-backend",
        "environmentName": "staging",
        "logs": "Traceback (most recent call last):\n  File \"src/server.py\", line 44, in <module>\n    import missing_pkg\nModuleNotFoundError: No module named 'missing_pkg'"
    }
    body2 = json.dumps(railway_payload).encode()
    sig2 = "sha256=" + hmac.new(secret.encode(), body2, hashlib.sha256).hexdigest()

    r = httpx.post(
        "http://localhost:8000/webhooks/railway",
        content=body2,
        headers={"Content-Type": "application/json", "X-Railway-Signature": sig2}
    )
    print("Railway Webhook Status:", r.status_code, r.json())
    assert r.status_code == 202

    # 4. Test Invalid HMAC rejection (Security test)
    bad_sig = "sha256=invalid_hex_signature"
    r = httpx.post(
        "http://localhost:8000/webhooks/railway",
        content=body2,
        headers={"Content-Type": "application/json", "X-Railway-Signature": bad_sig}
    )
    print("Invalid Signature Status:", r.status_code, r.json())
    assert r.status_code == 401
    print("ALL INTEGRATION TESTS PASSED!")

if __name__ == "__main__":
    test_webhooks()
