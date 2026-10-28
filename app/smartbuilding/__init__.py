import os

import redis
from elasticsearch import Elasticsearch
from flask import Flask, jsonify, render_template
from pymongo import MongoClient


def create_app():
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024  # RM-15: 100 Mo maximum

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
   
   
    from .uploads import bp as uploads_bp
    app.register_blueprint(uploads_bp)

    from .search import bp as search_bp
    app.register_blueprint(search_bp)

    @app.errorhandler(413)
    def too_large(error):
        return render_template("upload.html", error="Fichier trop volumineux : 100 Mo maximum."), 413





    return app