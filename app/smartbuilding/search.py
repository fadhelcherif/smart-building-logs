import hashlib
import json
from datetime import datetime, timezone

from flask import Blueprint, current_app, render_template, request, url_for

bp = Blueprint("search", __name__)

INDEX = "sensor-logs-*"
PAGE_SIZE = 50
MAX_RESULTS = 10000        # Elasticsearch limit on from + size
CACHE_TTL = 60             # seconds
DATE_FORMAT = "%Y-%m-%dT%H:%M"   # format of <input type="datetime-local">

# Fallback choices if the aggregation fails
DEFAULT_CHOICES = {
    "zone": ["SS", "E0", "E1", "E2", "E3"],
    "level": ["INFO", "WARNING", "CRITICAL", "ERROR"],
    "type": ["temperature", "humidity", "co2", "light", "presence", "electricity", "water", "heating"],
}
MULTI_FIELDS = ["zone", "level", "type"]


# ---------- Redis cache (a Redis failure never breaks the search) ----------

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

def parse_params(args):
    """Read and clean the query string. Returns (params, errors)."""
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
                params[field] = datetime.fromisoformat(text).strftime(DATE_FORMAT)
            except ValueError:
                errors.append(f"La date {label} est invalide.")

    page_text = args.get("page", "1").strip()
    if page_text.isdigit() and int(page_text) >= 1:
        params["page"] = int(page_text)
    else:
        params["page"] = 1
        errors.append("Le numéro de page doit être un nombre positif.")
    if (params["page"] - 1) * PAGE_SIZE + PAGE_SIZE > MAX_RESULTS:
        params["page"] = 1
        errors.append(f"Impossible d'afficher au-delà de {MAX_RESULTS} résultats : affinez votre recherche.")

    if "date_from" in params and "date_to" in params and params["date_from"] > params["date_to"]:
        errors.append("La date de début doit être avant la date de fin.")
    return params, errors


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
        time_range["gte"] = params["date_from"] + ":00Z"
    if "date_to" in params:
        time_range["lte"] = params["date_to"] + ":00Z||/m"   # "/m" = until the end of that minute
    if time_range:
        filters.append({"range": {"@timestamp": time_range}})

    if not must and not filters:
        return {"match_all": {}}
    return {"bool": {"must": must, "filter": filters}}


def describe(params):
    """Criteria as a readable French sentence (for the history page)."""
    parts = []
    if "q" in params:
        parts.append(f"texte « {params['q']} »")
    if "zone" in params:
        parts.append("zone " + ", ".join(params["zone"]))
    if "level" in params:
        parts.append("niveau " + ", ".join(params["level"]))
    if "type" in params:
        parts.append("type " + ", ".join(params["type"]))
    if "sensor_id" in params:
        parts.append(f"capteur {params['sensor_id']}")
    if "date_from" in params:
        parts.append("depuis le " + params["date_from"].replace("T", " à "))
    if "date_to" in params:
        parts.append("jusqu'au " + params["date_to"].replace("T", " à "))
    return " ; ".join(parts) or "Aucun critère (tous les logs)"


# ---------- Elasticsearch ----------

def run_search(params):
    """Returns (result, from_cache). Looks in Redis first, then Elasticsearch."""
    key = cache_key(params)
    result = cache_get(key)
    if result:
        return result, True

    response = current_app.es.search(
        index=INDEX,
        query=build_query(params),
        sort=[{"@timestamp": "desc"}],
        from_=(params["page"] - 1) * PAGE_SIZE,
        size=PAGE_SIZE,
        track_total_hits=True,
    )
    hits = []
    for hit in response["hits"]["hits"]:
        row = hit["_source"]
        row["@timestamp"] = row.get("@timestamp", "")[:19].replace("T", " ")
        hits.append(row)
    result = {"hits": hits, "total": response["hits"]["total"]["value"], "took": response["took"]}
    cache_set(key, result)
    return result, False


def get_choices():
    """Zones, levels and types found in the index (cached like a search)."""
    key = cache_key({"aggregation": "choices"})
    choices = cache_get(key)
    if choices:
        return choices
    try:
        response = current_app.es.search(
            index=INDEX, size=0,
            aggs={field: {"terms": {"field": field, "size": 50}} for field in MULTI_FIELDS},
        )
        choices = {
            field: [b["key"] for b in response["aggregations"][field]["buckets"]]
            for field in MULTI_FIELDS
        }
    except Exception:
        return DEFAULT_CHOICES
    # Empty index: keep the fixed lists so the form is still usable
    choices = {field: choices[field] or DEFAULT_CHOICES[field] for field in MULTI_FIELDS}
    cache_set(key, choices)
    return choices


# ---------- MongoDB history ----------

def history_collection():
    return current_app.mongo["smartbuilding"]["search_history"]


def save_history(params, total, from_cache):
    try:
        history_collection().insert_one({
            "params": params,
            "total_hits": total,
            "from_cache": from_cache,
            "searched_at": datetime.now(timezone.utc),
            "user": None,   # login comes later
        })
    except Exception:
        pass   # history is optional, the search must still work


# ---------- Pages ----------

@bp.route("/search")
def search():
    params, errors = parse_params(request.args)
    choices = get_choices()
    context = {"params": params, "choices": choices, "errors": errors,
               "result": None, "from_cache": False, "page_size": PAGE_SIZE, "prev_url": None, "next_url": None}

    if not errors:
        try:
            result, from_cache = run_search(params)
        except Exception:
            context["errors"] = ["Elasticsearch est injoignable : la recherche est impossible pour le moment."]
            return render_template("search.html", **context)
        context.update(result=result, from_cache=from_cache)

        # Save only a submitted search, not paging
        if request.args and params["page"] == 1:
            save_history(params, result["total"], from_cache)

        # Previous / next links keep the filters
        if params["page"] > 1:
            context["prev_url"] = url_for("search.search", **{**params, "page": params["page"] - 1})
        shown = params["page"] * PAGE_SIZE
        if shown < min(result["total"], MAX_RESULTS):
            context["next_url"] = url_for("search.search", **{**params, "page": params["page"] + 1})
    return render_template("search.html", **context)


@bp.route("/search/history")
def history():
    try:
        docs = list(history_collection().find().sort("searched_at", -1).limit(20))
    except Exception:
        return render_template("search_history.html", entries=[],
                               error="MongoDB est injoignable : l'historique est indisponible.")
    entries = []
    for doc in docs:
        params = {k: v for k, v in doc["params"].items() if k != "page"}
        entries.append({
            "date": doc["searched_at"].strftime("%d/%m/%Y %H:%M"),
            "criteria": describe(doc["params"]),
            "total": doc["total_hits"],
            "url": url_for("search.search", **params),
        })
    return render_template("search_history.html", entries=entries, error=None)
