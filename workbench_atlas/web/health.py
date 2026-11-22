"""Distinguish empty research stores from unavailable or malformed stores."""

import json
import sqlite3
from pathlib import Path


def data_health(settings) -> dict:
    stores = []
    for key, path, table in (
        ("scenarios", settings.scenario_db_path, "scenarios"),
        ("evidence", settings.evidence_db_path, "evidence_events"),
        ("risks", settings.emerging_risks_path, None),
    ):
        path = Path(path)
        status, message = "ok", "Available"
        try:
            if not path.exists():
                status, message = "uninitialized", "No store has been created yet"
            elif table:
                with sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True,
                                     timeout=2.0) as conn:
                    conn.execute(f'SELECT * FROM "{table}" LIMIT 1').fetchall()
            else:
                data = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(data, list) or not all(isinstance(x, dict) for x in data):
                    raise ValueError("Risk register must contain a list of records")
        except (OSError, sqlite3.Error, ValueError):
            status, message = "unavailable", "Store could not be read; displayed counts are incomplete"
        stores.append({"store": key, "status": status, "message": message})
    return {"status": "unavailable" if any(x["status"] == "unavailable" for x in stores)
            else "uninitialized" if all(x["status"] == "uninitialized" for x in stores)
            else "ok", "stores": stores}
