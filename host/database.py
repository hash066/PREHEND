import sqlite3
import os
import json
from datetime import datetime, timedelta

DB_FILE = os.path.join(os.path.dirname(__file__), "prehend_clinical.db")

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()

    # Patients Table
    c.execute('''
        CREATE TABLE IF NOT EXISTS patients (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            condition TEXT,
            age INTEGER,
            baseline_mvc REAL,
            baseline_noise REAL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # Medications Table
    c.execute('''
        CREATE TABLE IF NOT EXISTS medications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            patient_id INTEGER,
            time_str TEXT,
            name TEXT,
            dose TEXT,
            taken BOOLEAN DEFAULT 0,
            last_taken_date TEXT,
            FOREIGN KEY (patient_id) REFERENCES patients (id)
        )
    ''')

    # Calibrations Table (for Prognostics)
    c.execute('''
        CREATE TABLE IF NOT EXISTS calibrations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            patient_id INTEGER,
            mvc_value REAL,
            snr_value REAL,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (patient_id) REFERENCES patients (id)
        )
    ''')

    # Event Logs Table
    c.execute('''
        CREATE TABLE IF NOT EXISTS event_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT,
            description TEXT,
            severity TEXT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # Sleep Logs Table
    c.execute('''
        CREATE TABLE IF NOT EXISTS sleep_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            patient_id INTEGER,
            date_str TEXT,
            spasm_count INTEGER,
            avg_hrv REAL,
            restlessness_score REAL,
            FOREIGN KEY (patient_id) REFERENCES patients (id)
        )
    ''')

    conn.commit()

    # Seed mock data if empty
    c.execute("SELECT COUNT(*) FROM patients")
    if c.fetchone()[0] == 0:
        c.execute("INSERT INTO patients (name, condition, age, baseline_mvc, baseline_noise) VALUES (?, ?, ?, ?, ?)",
                  ("Patient Zero", "Amyotrophic Lateral Sclerosis (ALS)", 58, 1.0, 0.02))
        patient_id = c.lastrowid
        
        # Seed Meds
        meds = [
            ("08:00", "Levodopa / Carbidopa", "25mg / 100mg"),
            ("12:00", "Baclofen", "10mg (Spasticity)"),
            ("18:00", "Ropinirole", "2mg"),
            ("22:00", "Clonazepam", "0.5mg (Sleep)")
        ]
        for m in meds:
            c.execute("INSERT INTO medications (patient_id, time_str, name, dose, taken, last_taken_date) VALUES (?, ?, ?, ?, 0, '')",
                      (patient_id, m[0], m[1], m[2]))
            
        # Seed 7 days of historical calibrations for Prognostics
        base_date = datetime.now() - timedelta(days=7)
        mvc = 1.0
        snr = 50.0
        for i in range(7):
            date_val = base_date + timedelta(days=i)
            c.execute("INSERT INTO calibrations (patient_id, mvc_value, snr_value, timestamp) VALUES (?, ?, ?, ?)",
                      (patient_id, mvc, snr, date_val))
            mvc *= 0.95  # 5% decay per day
            snr *= 0.90
            
            # Seed Sleep logs
            rest_score = 80 - (i * 2) + (5 if i%2==0 else -5) # Mock score
            c.execute("INSERT INTO sleep_logs (patient_id, date_str, spasm_count, avg_hrv, restlessness_score) VALUES (?, ?, ?, ?, ?)",
                      (patient_id, date_val.strftime("%Y-%m-%d"), 12+i, 45.0, rest_score))
            
    conn.commit()
    conn.close()

def get_db_connection():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

if __name__ == "__main__":
    init_db()
    print(f"Database initialized at {DB_FILE}")
