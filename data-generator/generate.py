"""Generate realistic Smart Building sensor logs (CSV and JSON) for testing.

The values follow the business rules of the functional analysis:
- one measurement per sensor every 60 seconds (RM-04)
- INFO / WARNING / CRITICAL thresholds for temperature, humidity and CO2 (RM-05 to RM-08)
- physically impossible values are logged as ERROR (RM-10)
- CSV columns: timestamp, sensor_id, zone, type, value, unit, level, message (RM-16)

A few incidents are injected so that the dashboards and alerts have something to show:
overheating in meeting room E2-S204 in the afternoon, high CO2 in open space E3-O301,
high humidity in office E1-B112, a faulty sensor and an offline presence sensor.

Usage:
    python generate.py                      # 24 h of data, CSV + JSON, in ./output
    python generate.py --hours 6 --format csv --out ../uploads
"""

import argparse
import csv
import json
import math
import os
import random
from datetime import datetime, timedelta, timezone

from faker import Faker

COLUMNS = ["timestamp", "sensor_id", "zone", "type", "value", "unit", "level", "message"]

# Rooms of the building: (zone, room, sensor types)
ROOMS = [
    ("E0", "H01", ["temperature", "humidity", "co2", "light", "presence"]),
    ("E1", "B104", ["temperature", "humidity", "co2", "presence"]),
    ("E1", "B112", ["temperature", "humidity", "co2", "presence"]),
    ("E2", "S204", ["temperature", "humidity", "co2", "presence"]),
    ("E2", "S206", ["temperature", "humidity", "co2", "presence"]),
    ("E2", "S210", ["temperature", "humidity", "co2", "presence"]),
    ("E3", "O301", ["temperature", "humidity", "co2", "light", "presence"]),
    ("E3", "O302", ["temperature", "humidity", "co2", "presence"]),
    ("SS", "T01", ["presence", "electricity", "water", "heating"]),
    ("SS", "T02", ["temperature", "humidity"]),  # server room
]

PREFIX = {"temperature": "TEMP", "humidity": "HUM", "co2": "CO2", "light": "LUM",
          "presence": "PRES", "electricity": "ELEC", "water": "WATER", "heating": "HEAT"}
UNIT = {"temperature": "°C", "humidity": "%", "co2": "ppm", "light": "lux",
        "presence": "", "electricity": "kWh", "water": "m³", "heating": "kWh"}


def office_activity(hour):
    """0 at night, close to 1 in the middle of a working day."""
    if hour < 7 or hour >= 20:
        return 0.0
    return max(0.0, math.sin(math.pi * (hour - 7) / 13))


def bump(hour, center, width, height):
    """A smooth peak around `center` (in hours), used to inject incidents."""
    return height * math.exp(-((hour - center) / width) ** 2)


def measure(sensor_type, zone, room, hour, rng):
    activity = office_activity(hour)
    if sensor_type == "temperature":
        if room == "T02":  # server room
            return round(25.0 + bump(hour, 12.5, 1.2, 4.0) + rng.gauss(0, 0.3), 1)
        value = 20.3 + 3.8 * activity + rng.gauss(0, 0.25)
        if room == "S204":
            value += bump(hour, 15.0, 1.6, 7.0)
        return round(value, 1)
    if sensor_type == "humidity":
        value = 42 + 8 * activity + rng.gauss(0, 1.5)
        if room == "B112":
            value += bump(hour, 13.8, 0.8, 16)
        return round(value)
    if sensor_type == "co2":
        value = 430 + 500 * activity + rng.gauss(0, 30)
        if room == "O301":
            value += bump(hour, 14.6, 0.5, 750)
        return round(value)
    if sensor_type == "light":
        return round(max(0, 450 * activity + rng.gauss(0, 20)))
    if sensor_type == "presence":
        return 1 if rng.random() < 0.15 + 0.8 * activity else 0
    if sensor_type == "electricity":  # kWh consumed during the last minute
        return round((25 + 50 * activity) / 60 + rng.gauss(0, 0.05), 3)
    if sensor_type == "water":
        return round(max(0.0, (0.0005 + 0.004 * activity) + rng.gauss(0, 0.0005)), 4)
    if sensor_type == "heating":
        return round((15 + 80 * activity) / 60 + rng.gauss(0, 0.05), 3)
    raise ValueError(sensor_type)


def classify(sensor_type, room, value):
    """Return (level, message) following RM-05 to RM-10."""
    if sensor_type == "temperature":
        if value > 80 or value < -20:
            return "ERROR", "Valeur impossible, capteur défectueux"
        warn, crit = (27, 32) if room == "T02" else (26, 30)
        if value > crit:
            return "CRITICAL", f"Seuil critique dépassé (> {crit} °C)"
        if value < 16:
            return "CRITICAL", "Température trop basse (< 16 °C)"
        if value > warn:
            return "WARNING", f"Température au-dessus de {warn} °C"
        if room != "T02" and value < 19:
            return "WARNING", "Température sous 19 °C"
        return "INFO", "Mesure normale"
    if sensor_type == "humidity":
        if value > 100 or value < 0:
            return "ERROR", "Valeur impossible, capteur défectueux"
        if value > 70 or value < 20:
            return "CRITICAL", "Humidité hors limites"
        if value > 60:
            return "WARNING", "Humidité au-dessus de 60 %"
        if value < 30:
            return "WARNING", "Humidité sous 30 %"
        return "INFO", "Mesure normale"
    if sensor_type == "co2":
        if value > 1500:
            return "CRITICAL", "CO2 au-dessus de 1 500 ppm"
        if value > 1000:
            return "WARNING", "CO2 entre 1 000 et 1 500 ppm"
        return "INFO", "Mesure normale"
    if sensor_type == "presence":
        return "INFO", "Présence détectée" if value else "Aucune présence"
    if sensor_type in ("electricity", "water", "heating"):
        return "INFO", "Relevé compteur"
    return "INFO", "Mesure normale"


def generate(start, hours, interval, seed):
    rng = random.Random(seed)
    fake = Faker("fr_FR")
    fake.seed_instance(seed)
    sensors = [(zone, room, t) for zone, room, types in ROOMS for t in types]
    steps = int(hours * 3600 / interval)
    for step in range(steps):
        moment = start + timedelta(seconds=step * interval)
        hour = moment.hour + moment.minute / 60
        for zone, room, sensor_type in sensors:
            sensor_id = f"{PREFIX[sensor_type]}-{zone}-{room}"
            # Offline sensor between 13:13 and 13:40: no data at all (RM-04)
            if sensor_id == "PRES-SS-T01" and 13.2 <= hour < 13.67:
                continue
            value = measure(sensor_type, zone, room, hour, rng)
            # Rare faulty reading on one temperature sensor (RM-10)
            if sensor_id == "TEMP-E3-O302" and rng.random() < 0.002:
                value = round(rng.uniform(85, 99), 1)
            level, message = classify(sensor_type, room, value)
            yield {
                "timestamp": moment.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "sensor_id": sensor_id,
                "zone": zone,
                "type": sensor_type,
                "value": value,
                "unit": UNIT[sensor_type],
                "level": level,
                "message": message,
                "event_id": fake.uuid4(),
            }


def main():
    parser = argparse.ArgumentParser(description="Smart Building sensor log generator")
    parser.add_argument("--start", default=None,
                        help="start date, YYYY-MM-DD (default: yesterday, 00:00 UTC)")
    parser.add_argument("--hours", type=float, default=24, help="duration to generate (default 24)")
    parser.add_argument("--interval", type=int, default=60, help="seconds between measurements (default 60)")
    parser.add_argument("--format", choices=["csv", "json", "both"], default="both")
    parser.add_argument("--out", default=os.path.join(os.path.dirname(__file__), "output"))
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    if args.start:
        start = datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    else:
        today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        start = today - timedelta(days=1)

    os.makedirs(args.out, exist_ok=True)
    name = f"capteurs_{start:%Y-%m-%d}"
    rows = list(generate(start, args.hours, args.interval, args.seed))

    if args.format in ("csv", "both"):
        path = os.path.join(args.out, name + ".csv")
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        print(f"CSV  : {path} ({len(rows)} lignes)")

    if args.format in ("json", "both"):
        # One JSON object per line (NDJSON), the format Logstash reads line by line
        path = os.path.join(args.out, name + ".json")
        with open(path, "w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(f"JSON : {path} ({len(rows)} lignes)")

    levels = {}
    for row in rows:
        levels[row["level"]] = levels.get(row["level"], 0) + 1
    print("Niveaux :", ", ".join(f"{k} {v}" for k, v in sorted(levels.items())))


if __name__ == "__main__":
    main()
