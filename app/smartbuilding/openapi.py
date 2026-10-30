"""OpenAPI 3.0 specification of the REST API, written by hand (served as JSON)."""


def query_param(name, description, example=None, schema=None):
    param = {"name": name, "in": "query", "required": False, "description": description,
             "schema": schema or {"type": "string"}}
    if example is not None:
        param["example"] = example
    return param


def multi_param(name, description, values):
    return query_param(name, description + " (peut être répété).",
                       schema={"type": "array", "items": {"type": "string", "enum": values}})


DATE_FROM = query_param("date_from", "Date de début (ISO 8601, UTC).", "2026-10-01T00:00:00Z")
DATE_TO = query_param("date_to", "Date de fin (ISO 8601, UTC).", "2026-10-31T23:59:59Z")
ZONES = ["SS", "E0", "E1", "E2", "E3"]


def error_response(description, message):
    return {"description": description,
            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Error"},
                                             "example": {"error": message}}}}


COMMON_ERRORS = {
    "429": error_response("Trop de requêtes (60 par minute et par IP).",
                          "Trop de requêtes : 60 par minute maximum."),
    "503": error_response("Elasticsearch, MongoDB indisponible.", "Elasticsearch est injoignable."),
}

LOG_EXAMPLE = {
    "@timestamp": "2026-10-28T14:30:00.000Z", "sensor_id": "E2-R204-TEMP-01", "zone": "E2",
    "room": "R204", "floor": 2, "type": "temperature", "value": 41.2, "unit": "C",
    "level": "CRITICAL", "message": "Seuil critique dépassé", "source_file": "20261028-143000_logs.csv",
}

SPEC = {
    "openapi": "3.0.3",
    "info": {
        "title": "API Smart Building",
        "version": "1.0.0",
        "description": "API en lecture seule pour consulter les logs des capteurs, "
                       "les fichiers importés et les statistiques. Limite : 60 requêtes par minute par IP "
                       "(en-têtes X-RateLimit-Limit et X-RateLimit-Remaining).",
    },
    "servers": [{"url": "/api/v1"}],
    "paths": {
        "/logs": {"get": {
            "summary": "Rechercher dans les logs",
            "description": "Mêmes filtres que la page de recherche. Résultats triés du plus récent au plus ancien.",
            "parameters": [
                query_param("q", "Texte libre recherché dans le message.", "seuil"),
                multi_param("zone", "Zone", ZONES),
                multi_param("level", "Niveau", ["INFO", "WARNING", "CRITICAL", "ERROR"]),
                multi_param("type", "Type de capteur", ["temperature", "humidity", "co2", "light",
                                                        "presence", "electricity", "water", "heating"]),
                query_param("sensor_id", "Identifiant exact du capteur.", "E2-R204-TEMP-01"),
                DATE_FROM, DATE_TO,
                query_param("page", "Numéro de page (défaut 1).", 1, {"type": "integer", "minimum": 1, "default": 1}),
                query_param("size", "Résultats par page (défaut 50, maximum 100).", 50,
                            {"type": "integer", "minimum": 1, "maximum": 100, "default": 50}),
            ],
            "responses": {
                "200": {"description": "Liste des logs.", "content": {"application/json": {"example": {
                    "total": 192, "page": 1, "size": 1, "from_cache": False, "results": [LOG_EXAMPLE]}}}},
                "400": error_response("Paramètre invalide.", "La date de début est invalide."),
                **COMMON_ERRORS,
            },
        }},
        "/files": {"get": {
            "summary": "Lister les fichiers importés",
            "description": "Documents de la collection MongoDB `uploads`, du plus récent au plus ancien.",
            "parameters": [query_param("status", "Filtrer par statut.", "termine", {
                "type": "string", "enum": ["en_attente", "rejete", "annule", "en_cours", "termine"]})],
            "responses": {
                "200": {"description": "Liste des fichiers.", "content": {"application/json": {"example": {
                    "total": 1, "results": [{
                        "id": "66f1c0a2b3d4e5f601234567", "original_name": "logs.csv",
                        "stored_name": "20261028-143000_logs.csv", "format": "csv", "size": 20480,
                        "uploaded_at": "2026-10-28T14:30:00Z", "total_lines": 100, "invalid_lines": 2,
                        "status": "termine", "reason": None}]}}}},
                "400": error_response("Statut inconnu.", "Statut invalide."),
                **COMMON_ERRORS,
            },
        }},
        "/stats": {"get": {
            "summary": "Statistiques sur les logs",
            "description": "Agrégations Elasticsearch : par niveau, par zone et type, "
                           "capteurs les plus en alerte, volume par heure. Résultat mis en cache 60 s.",
            "parameters": [DATE_FROM, DATE_TO, multi_param("zone", "Zone", ZONES)],
            "responses": {
                "200": {"description": "Statistiques.", "content": {"application/json": {"example": {
                    "total": 172713, "from_cache": False,
                    "by_level": [{"level": "INFO", "count": 150000}],
                    "by_zone_and_type": [{"zone": "E2", "types": [
                        {"type": "temperature", "count": 5000, "min": 15.1, "max": 41.2, "avg": 21.37}]}],
                    "top_alert_sensors": [{"sensor_id": "E2-R204-TEMP-01", "count": 42}],
                    "per_hour": [{"hour": "2026-10-28T14:00:00.000Z", "count": 120}]}}}},
                "400": error_response("Paramètre invalide.", "La date de fin est invalide."),
                **COMMON_ERRORS,
            },
        }},
    },
    "components": {"schemas": {"Error": {"type": "object", "properties": {"error": {"type": "string"}}}}},
}
