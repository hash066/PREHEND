from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import List, Optional
import os
import sqlite3
from database import get_db_connection, init_db

# Initialize database on startup if it doesn't exist
if not os.path.exists(os.path.join(os.path.dirname(__file__), "prehend_clinical.db")):
    init_db()

app = FastAPI(title="PREHEND Clinical API")

class MedCheck(BaseModel):
    med_id: int
    taken: bool

class EventLog(BaseModel):
    event_type: str
    description: str
    severity: str

@app.get("/")
async def serve_dashboard():
    parent_dir = os.path.dirname(os.path.dirname(__file__))
    dist_index = os.path.join(parent_dir, "frontend", "dist", "index.html")
    if os.path.exists(dist_index):
        return FileResponse(dist_index)
    return HTMLResponse("<h1>PREHEND Clinical Dashboard (Vite App Not Built Yet)</h1>")

# Mount static assets if build exists
parent_dir = os.path.dirname(os.path.dirname(__file__))
assets_dir = os.path.join(parent_dir, "frontend", "dist", "assets")
if os.path.exists(assets_dir):
    app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

@app.get("/api/medications")
async def get_medications():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT id, time_str as time, name, dose, taken FROM medications ORDER BY time_str ASC")
    meds = [dict(row) for row in c.fetchall()]
    conn.close()
    return meds

@app.post("/api/medications")
async def update_medication(med: MedCheck):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("UPDATE medications SET taken = ? WHERE id = ?", (med.taken, med.med_id))
    conn.commit()
    conn.close()
    return {"status": "success"}

@app.get("/api/prognostics")
async def get_prognostics():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT timestamp, mvc_value, snr_value FROM calibrations ORDER BY timestamp ASC")
    cals = [dict(row) for row in c.fetchall()]
    conn.close()
    
    if not cals:
        return {"calibrations": [], "estimated_days_left": 0}
        
    # Simple linear decay estimation
    if len(cals) >= 2:
        first = cals[0]['mvc_value']
        last = cals[-1]['mvc_value']
        days = len(cals)
        drop_per_day = (first - last) / days if days > 0 else 0
        
        # Assume threshold is 0.3 for usability
        if drop_per_day > 0 and last > 0.3:
            estimated_days_left = int((last - 0.3) / drop_per_day)
        else:
            estimated_days_left = 0
    else:
        estimated_days_left = 5 # default guess if not enough data
        
    return {
        "calibrations": cals,
        "estimated_days_left": max(0, estimated_days_left)
    }

@app.post("/api/events")
async def log_event(event: EventLog):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("INSERT INTO event_logs (event_type, description, severity) VALUES (?, ?, ?)",
              (event.event_type, event.description, event.severity))
    conn.commit()
    conn.close()
    return {"status": "success"}

@app.get("/api/events")
async def get_events():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT * FROM event_logs ORDER BY timestamp DESC LIMIT 50")
    events = [dict(row) for row in c.fetchall()]
    conn.close()
    return events

@app.get("/api/sleep")
async def get_sleep_logs():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT * FROM sleep_logs ORDER BY date_str ASC")
    logs = [dict(row) for row in c.fetchall()]
    conn.close()
    return logs

@app.get("/api/report")
async def generate_report():
    conn = get_db_connection()
    c = conn.cursor()
    
    # Get Patient info
    c.execute("SELECT * FROM patients LIMIT 1")
    patient = dict(c.fetchone())
    
    # Get meds compliance
    c.execute("SELECT COUNT(*) FROM medications WHERE taken=1")
    taken_count = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM medications")
    total_meds = c.fetchone()[0]
    compliance = (taken_count / total_meds * 100) if total_meds > 0 else 0
    
    # Get critical events
    c.execute("SELECT COUNT(*) FROM event_logs WHERE severity='CRITICAL' OR severity='WARNING'")
    critical_events = c.fetchone()[0]
    
    # Get avg sleep score
    c.execute("SELECT AVG(restlessness_score) FROM sleep_logs")
    avg_sleep = c.fetchone()[0] or 0
    
    conn.close()
    
    report_html = f"""
    <html>
    <head>
        <title>PREHEND Clinical Report</title>
        <style>
            body {{ font-family: 'Segoe UI', Arial, sans-serif; padding: 40px; color: #333; }}
            h1 {{ color: #0284c7; border-bottom: 2px solid #0ea5e9; padding-bottom: 10px; }}
            .card {{ border: 1px solid #cbd5e1; border-radius: 8px; padding: 20px; margin-bottom: 20px; background: #f8fafc; }}
            .metric {{ display: flex; justify-content: space-between; padding: 8px 0; border-bottom: 1px solid #e2e8f0; }}
            .metric strong {{ color: #475569; }}
        </style>
    </head>
    <body>
        <h1>Clinical Summary Report</h1>
        <div class="card">
            <h2>Patient Profile</h2>
            <div class="metric"><strong>Name:</strong> <span>{patient['name']}</span></div>
            <div class="metric"><strong>Condition:</strong> <span>{patient['condition']}</span></div>
            <div class="metric"><strong>Age:</strong> <span>{patient['age']}</span></div>
        </div>
        <div class="card">
            <h2>System & Health Metrics</h2>
            <div class="metric"><strong>Medication Adherence:</strong> <span>{compliance:.1f}%</span></div>
            <div class="metric"><strong>Critical Safety Events (7d):</strong> <span>{critical_events}</span></div>
            <div class="metric"><strong>Avg Sleep Restlessness:</strong> <span>{avg_sleep:.1f} / 100</span></div>
            <div class="metric"><strong>Baseline MVC:</strong> <span>{patient['baseline_mvc']} mV</span></div>
        </div>
        <p style="text-align: center; color: #94a3b8; font-size: 0.9em; margin-top: 40px;">Generated by PREHEND Telemetry System • {patient['created_at']}</p>
        <script>window.print();</script>
    </body>
    </html>
    """
    return HTMLResponse(content=report_html)
