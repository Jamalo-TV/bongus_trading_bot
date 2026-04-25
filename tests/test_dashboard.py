import pytest
from fastapi.testclient import TestClient
from dashboard.app import app
from core.state import EMERGENCY_HALT_EVENT

def test_panic_endpoint():
    client = TestClient(app)
    
    # Ensure event is not set
    EMERGENCY_HALT_EVENT.clear()
    assert not EMERGENCY_HALT_EVENT.is_set()
    
    response = client.post("/api/v1/panic")
    assert response.status_code == 200
    assert response.json() == {"status": "HALTED"}
    
    # Ensure event is now set
    assert EMERGENCY_HALT_EVENT.is_set()
