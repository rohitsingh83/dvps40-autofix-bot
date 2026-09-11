import httpx

def test_app():
    with httpx.Client(base_url="http://localhost:8000") as client:
        # 1. UI Root
        r_ui = client.get("/")
        print("UI Status:", r_ui.status_code, "Length:", len(r_ui.text))
        assert r_ui.status_code == 200
        assert "Autonomous Auto-Fixing DevOps Telegram Bot" in r_ui.text

        # 2. Static CSS & JS
        r_css = client.get("/static/styles.css")
        assert r_css.status_code == 200
        r_js = client.get("/static/app.js")
        assert r_js.status_code == 200
        print("Static assets OK!")

        # 3. API Status
        r_status = client.get("/api/status")
        print("API Status:", r_status.status_code, r_status.json())
        assert r_status.status_code == 200

        # 4. API Simulation
        sim_payload = {
            "platform": "vercel",
            "project": "checkout-service",
            "environment": "production",
            "logs": "SyntaxError: unexpected EOF while parsing\n  File \"app/routes.py\", line 22"
        }
        r_sim = client.post("/api/simulate", json=sim_payload)
        print("Simulation Status:", r_sim.status_code, r_sim.json().get("status"))
        assert r_sim.status_code == 200
        rec = r_sim.json().get("record")
        assert rec.get("status") == "NOTIFIED"
        print("Incident Recorded in Audit Store:", rec.get("id"), "Status:", rec.get("status"))

        # 5. API Incidents List
        r_incidents = client.get("/api/incidents")
        items = r_incidents.json()
        print("Total Incidents in store:", len(items))
        assert len(items) >= 1

    print("ALL APP ENDPOINTS VERIFIED SUCCESSFULLY!")

if __name__ == "__main__":
    test_app()
