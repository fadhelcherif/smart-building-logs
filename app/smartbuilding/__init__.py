import os

import redis
from elasticsearch import Elasticsearch
from flask import Flask, jsonify
from pymongo import MongoClient


def create_app():
    app = Flask(__name__)

    # Connections to the three databases (addresses come from docker-compose.yml)
    app.es = Elasticsearch(os.environ["ELASTICSEARCH_URL"])
    app.mongo = MongoClient(os.environ["MONGO_URL"], serverSelectionTimeoutMS=3000)
    app.redis = redis.Redis.from_url(os.environ["REDIS_URL"], decode_responses=True, socket_timeout=3)

    @app.route("/")
    def index():
        return "<h1>Smart Building</h1><p>L'application fonctionne. Voir <a href='/health'>/health</a>.</p>"

    @app.route("/health")
    def health():
        checks = {
            "elasticsearch": lambda: app.es.ping(),
            "mongodb": lambda: app.mongo.admin.command("ping"),
            "redis": lambda: app.redis.ping(),
        }
        services = {}
        for name, check in checks.items():
            try:
                services[name] = "ok" if check() else "error"
            except Exception:
                services[name] = "error"
        healthy = all(state == "ok" for state in services.values())
        return jsonify(status="ok" if healthy else "degraded", services=services), (200 if healthy else 503)

    return app