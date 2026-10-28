import csv
import json
import re
from datetime import datetime

REQUIRED_COLUMNS = ["timestamp", "sensor_id", "zone", "type", "value", "unit", "level", "message"]  # RM-16
LEVELS = {"INFO", "WARNING", "CRITICAL", "ERROR"}
SENSOR_ID = re.compile(r"^[A-Z0-9]+-(SS|E\d+)-[A-Z0-9]+$")  # RM-02, ex: TEMP-E2-S204, CO2-E3-O301
MAX_INVALID_RATIO = 0.10  # RM-17
PREVIEW_LINES = 10        # US-02


def check_row(row):
    """Return None if the row is valid, otherwise the reason it is not."""
    try:
        datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
    except (ValueError, AttributeError, KeyError):
        return "timestamp invalide"
    if not SENSOR_ID.match(str(row.get("sensor_id", ""))):
        return "sensor_id invalide"
    try:
        float(row.get("value"))
    except (TypeError, ValueError):
        return "value n'est pas un nombre"
    if row.get("level") not in LEVELS:
        return "level inconnu"
    return None


def read_rows(path, fmt):
    """Yield (line number, row). row is None when the line cannot be read at all."""
    with open(path, encoding="utf-8-sig", newline="") as f:
        if fmt == "csv":
            reader = csv.DictReader(f)
            missing = [c for c in REQUIRED_COLUMNS if c not in (reader.fieldnames or [])]
            if missing:
                raise ValueError("Colonnes manquantes : " + ", ".join(missing))
            for number, row in enumerate(reader, start=2):
                yield number, row
        else:
            for number, line in enumerate(f, start=1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    row = None
                yield number, (row if isinstance(row, dict) else None)


def validate_file(path, fmt):
    report = {"total": 0, "invalid": 0, "errors": [], "preview": []}
    try:
        for number, row in read_rows(path, fmt):
            report["total"] += 1
            reason = "ligne illisible" if row is None else check_row(row)
            if reason:
                report["invalid"] += 1
                if len(report["errors"]) < 5:
                    report["errors"].append(f"Ligne {number} : {reason}")
            if len(report["preview"]) < PREVIEW_LINES:
                report["preview"].append(row or {})
    except UnicodeDecodeError:
        return {**report, "ok": False, "reason": "Le fichier n'est pas encodé en UTF-8."}
    except ValueError as error:
        return {**report, "ok": False, "reason": str(error)}

    if report["total"] == 0:
        return {**report, "ok": False, "reason": "Le fichier est vide."}
    ratio = report["invalid"] / report["total"]
    if ratio > MAX_INVALID_RATIO:
        return {**report, "ok": False, "reason": f"{ratio:.0%} de lignes invalides (maximum 10 %)."}
    return {**report, "ok": True, "reason": None}