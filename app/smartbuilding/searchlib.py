"""Code shared by the search page (search.py) and the REST API (api.py)."""
import hashlib
import json
from datetime import datetime, timezone

from flask import current_app

INDEX = "sensor-logs-*"
PAGE_SIZE = 50
MAX_RESULTS = 10000        # Elasticsearch limit on from + size
CACHE_TTL = 60             # seconds
DATE_FORMAT = "%Y-%m-%dT%H:%M:%S"
MULTI_FIELDS = ["zone", "level", "type"]


# ---------- Redis cache (a Redis failure never breaks the request) ----------

def cache_key(data):
    # Same parameters -> same key (sorted JSON)
    raw = json.dumps(data, sort_keys=True)
    return "search:" + hashlib.sha256(raw.encode()).hexdigest()


def cache_get(key):
    try:
        value = current_app.redis.get(key)
        return json.loads(value) if value else None
    except Exception:
        return None


def cache_set(key, value):
    try:
        current_app.redis.setex(key, CACHE_TTL, json.dumps(value))
    except Exception:
        pass


# ---------- Parameters ----------

def parse_date(text):
    """ISO 8601 text (UTC, or with an offset) -> 'YYYY-MM-DDTHH:MM:SS' in UTC."""
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo:
        parsed = parsed.astimezone(timezone.utc)
    return parsed.strftime(DATE_FORMAT)


def parse_filters(args):
    """Read the filters (everything except paging). Returns (params, errors)."""
    params, errors = {}, []

    q = args.get("q", "").strip()
    if q:
        params["q"] = q
    sensor_id = args.get("sensor_id", "").strip()
    if sensor_id:
        params["sensor_id"] = sensor_id
    for field in MULTI_FIELDS:
        values = sorted(set(v for v in args.getlist(field) if v))
        if values:
            params[field] = values

    for field, label in (("date_from", "de début"), ("date_to", "de fin")):
        text = args.get(field, "").strip()
        if text:
            try:
                params[field] = parse_date(text)
            except ValueError:
                errors.append(f"La date {label} est invalide.")

    if "date_from" in params and "date_to" in params and params["date_from"] > params["date_to"]:
        errors.append("La date de début doit être avant la date de fin.")
    return params, errors


def parse_page(args, size):
    """Read 'page'. Returns (page, errors)."""
    text = args.get("page", "1").strip()
    if not (text.isdigit() and int(text) >= 1):
        return 1, ["Le numéro de page doit être un nombre positif."]
    page = int(text)
    if page * size > MAX_RESULTS:
        return 1, [f"Impossible d'afficher au-delà de {MAX_RESULTS} résultats : affinez votre recherche."]
    return page, []


# ---------- Elasticsearch ----------

def build_query(params):
    must, filters = [], []
    if "q" in params:
        must.append({"match": {"message": params["q"]}})
    for field in MULTI_FIELDS:
        if field in params:
            filters.append({"terms": {field: params[field]}})
    if "sensor_id" in params:
        filters.append({"term": {"sensor_id": params["sensor_id"]}})

    time_range = {}
    if "date_from" in params:
        time_range["gte"] = params["date_from"] + "Z"
    if "date_to" in params:
        # Round up to the end of the minute (or second) the user typed
        rounding = "/m" if params["date_to"].endswith(":00") else "/s"
        time_range["lte"] = params["date_to"] + "Z||" + rounding
    if time_range:
        filters.append({"range": {"@timestamp": time_range}})

    if not must and not filters:
        return {"match_all": {}}
    return {"bool": {"must": must, "filter": filters}}


def run_search(params):
    """Returns (result, from_cache). Looks in Redis first, then Elasticsearch.
    params contains the filters plus 'page' and (optional) 'size'."""
    key = cache_key(params)
    result = cache_get(key)
    if result:
        return result, True

    size = params.get("size", PAGE_SIZE)
    response = current_app.es.search(
        index=INDEX,
        query=build_query(params),
        sort=[{"@timestamp": "desc"}],
        from_=(params["page"] - 1) * size,
        size=size,
        track_total_hits=True,
    )
    result = {
        "hits": [hit["_source"] for hit in response["hits"]["hits"]],
        "total": response["hits"]["total"]["value"],
        "took": response["took"],
    }
    cache_set(key, result)
    return result, False
