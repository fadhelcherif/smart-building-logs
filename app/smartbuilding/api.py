import time
from datetime import datetime

from elasticsearch import TransportError
from flask import Blueprint, current_app, g, jsonify, render_template, request
from pymongo.errors import PyMongoError
from werkzeug.datastructures import MultiDict

from .openapi import SPEC
from .searchlib import (PAGE_SIZE, INDEX, build_query, cache_get, cache_key, cache_set,
                        parse_filters, parse_page, run_search)
from .uploads import STATUS_LABELS

bp = Blueprint("api", __name__, url_prefix="/api/v1")
docs_bp = Blueprint("api_docs", __name__)

RATE_LIMIT = 60        # requests per minute per IP
MAX_SIZE = 100


class ApiError(Exception):
    def __init__(self, message, status=400):
        self.message = message
        self.status = status


@bp.errorhandler(ApiError)
def handle_api_error(error):
    return jsonify(error=error.message), error.status


@bp.errorhandler(TransportError)
def handle_es_down(error):
    return jsonify(error="Elasticsearch est injoignable."), 503


@bp.errorhandler(PyMongoError)
def handle_mongo_down(error):
    return jsonify(error="MongoDB est injoignable."), 503


# Unknown /api/... paths also answer in JSON
@bp.app_errorhandler(404)
def not_found(error):
    if request.path.startswith("/api/"):
        return jsonify(error="Route introuvable."), 404
    return error


@bp.app_errorhandler(405)
def method_not_allowed(error):
    if request.path.startswith("/api/"):
        return jsonify(error="Méthode non autorisée : l'API est en lecture seule (GET)."), 405
    return error


# ---------- Rate limiting: fixed window of one minute, counted in Redis ----------

@bp.before_request
def rate_limit():
    try:
        key = f"ratelimit:{request.remote_addr}:{int(time.time() // 60)}"
        count = current_app.redis.incr(key)
        if count == 1:
            current_app.redis.expire(key, 60)
    except Exception:
        return None   # Redis down: let the request through
    g.remaining = max(0, RATE_LIMIT - count)
    if count > RATE_LIMIT:
        return jsonify(error="Trop de requêtes : 60 par minute maximum."), 429


@bp.after_request
def rate_limit_headers(response):
    if hasattr(g, "remaining"):
        response.headers["X-RateLimit-Limit"] = str(RATE_LIMIT)
        response.headers["X-RateLimit-Remaining"] = str(g.remaining)
    return response


# ---------- Helpers ----------

def check_errors(errors):
    if errors:
        raise ApiError(" ".join(errors))


def iso(value):
    """MongoDB datetimes are naive UTC."""
    return value.isoformat() + "Z" if isinstance(value, datetime) else value


# ---------- Endpoints ----------

@bp.route("/logs")
def logs():
    params, errors = parse_filters(request.args)

    size_text = request.args.get("size", str(PAGE_SIZE)).strip()
    size = int(size_text) if size_text.isdigit() else 0
    if not 1 <= size <= MAX_SIZE:
        errors.append(f"Le paramètre size doit être un nombre entre 1 et {MAX_SIZE}.")
        size = PAGE_SIZE
    params["page"], page_errors = parse_page(request.args, size)
    check_errors(errors + page_errors)
    if size != PAGE_SIZE:
        params["size"] = size   # part of the cache key, since it changes the result

    result, from_cache = run_search(params)
    return jsonify(total=result["total"], page=params["page"], size=size,
                   from_cache=from_cache, results=result["hits"])


@bp.route("/files")
def files():
    query = {}
    status = request.args.get("status", "").strip()
    if status:
        if status not in STATUS_LABELS:
            raise ApiError("Statut invalide : " + ", ".join(STATUS_LABELS) + ".")
        query["status"] = status

    docs = current_app.mongo["smartbuilding"]["uploads"].find(query, {"errors": 0}).sort("uploaded_at", -1)
    results = []
    for doc in docs:
        doc["id"] = str(doc.pop("_id"))
        results.append({key: iso(value) for key, value in doc.items()})
    return jsonify(total=len(results), results=results)


@bp.route("/stats")
def stats():
    # Only these three filters are used here
    wanted = MultiDict((k, v) for k, v in request.args.items(multi=True) if k in ("zone", "date_from", "date_to"))
    params, errors = parse_filters(wanted)
    check_errors(errors)

    key = cache_key({"endpoint": "stats", **params})
    data = cache_get(key)
    from_cache = data is not None
    if not from_cache:
        response = current_app.es.search(
            index=INDEX, query=build_query(params), size=0, track_total_hits=True,
            aggs={
                "by_level": {"terms": {"field": "level"}},
                "by_zone_and_type": {
                    "terms": {"field": "zone", "size": 10},
                    "aggs": {"types": {
                        "terms": {"field": "type", "size": 20},
                        "aggs": {"value_stats": {"stats": {"field": "value"}}},
                    }},
                },
                "top_alert_sensors": {
                    "filter": {"terms": {"level": ["WARNING", "CRITICAL"]}},
                    "aggs": {"sensors": {"terms": {"field": "sensor_id", "size": 10}}},
                },
                "per_hour": {"date_histogram": {"field": "@timestamp", "fixed_interval": "1h", "min_doc_count": 1}},
            },
        )
        data = clean_stats(response)
        cache_set(key, data)
    return jsonify(from_cache=from_cache, **data)


def clean_stats(response):
    """Turn the raw Elasticsearch answer into simple JSON."""
    aggs = response["aggregations"]

    def round2(number):
        return round(number, 2) if number is not None else None

    zones = []
    for zone in aggs["by_zone_and_type"]["buckets"]:
        types = []
        for t in zone["types"]["buckets"]:
            s = t["value_stats"]
            types.append({"type": t["key"], "count": s["count"], "min": round2(s["min"]),
                          "max": round2(s["max"]), "avg": round2(s["avg"])})
        zones.append({"zone": zone["key"], "types": types})

    return {
        "total": response["hits"]["total"]["value"],
        "by_level": [{"level": b["key"], "count": b["doc_count"]} for b in aggs["by_level"]["buckets"]],
        "by_zone_and_type": zones,
        "top_alert_sensors": [{"sensor_id": b["key"], "count": b["doc_count"]}
                              for b in aggs["top_alert_sensors"]["sensors"]["buckets"]],
        "per_hour": [{"hour": b["key_as_string"], "count": b["doc_count"]} for b in aggs["per_hour"]["buckets"]],
    }


# ---------- Documentation ----------

@bp.route("/openapi.json")
def openapi():
    return jsonify(SPEC)


@docs_bp.route("/api/docs")
def docs():
    return render_template("api_docs.html")
