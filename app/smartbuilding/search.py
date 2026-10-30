from datetime import datetime, timezone

from flask import Blueprint, current_app, render_template, request, url_for

from .searchlib import (INDEX, MAX_RESULTS, MULTI_FIELDS, PAGE_SIZE, cache_get, cache_key, cache_set,
                        parse_filters, parse_page, run_search)

bp = Blueprint("search", __name__)

# Fallback choices if the aggregation fails
DEFAULT_CHOICES = {
    "zone": ["SS", "E0", "E1", "E2", "E3"],
    "level": ["INFO", "WARNING", "CRITICAL", "ERROR"],
    "type": ["temperature", "humidity", "co2", "light", "presence", "electricity", "water", "heating"],
}


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
        parts.append("depuis le " + params["date_from"][:16].replace("T", " à "))
    if "date_to" in params:
        parts.append("jusqu'au " + params["date_to"][:16].replace("T", " à "))
    return " ; ".join(parts) or "Aucun critère (tous les logs)"


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
    params, errors = parse_filters(request.args)
    params["page"], page_errors = parse_page(request.args, PAGE_SIZE)
    errors += page_errors
    choices = get_choices()
    context = {"params": params, "choices": choices, "errors": errors,
               "result": None, "from_cache": False, "page_size": PAGE_SIZE, "prev_url": None, "next_url": None}

    if not errors:
        try:
            result, from_cache = run_search(params)
        except Exception:
            context["errors"] = ["Elasticsearch est injoignable : la recherche est impossible pour le moment."]
            return render_template("search.html", **context)
        for hit in result["hits"]:
            hit["@timestamp"] = hit.get("@timestamp", "")[:19].replace("T", " ")
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
