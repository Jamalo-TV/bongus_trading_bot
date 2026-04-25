from fastapi import FastAPI
from core.state import EMERGENCY_HALT_EVENT, flatten_all_positions
import asyncio

app = FastAPI()

@app.post("/api/v1/panic")
async def panic():
    EMERGENCY_HALT_EVENT.set()
    # Trigger background task to flatten positions
    asyncio.create_task(flatten_all_positions())
    return {"status": "HALTED"}
