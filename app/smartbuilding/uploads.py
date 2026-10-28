import os
from datetime import datetime, timezone

from bson import ObjectId
from flask import Blueprint, current_app, redirect, render_template, request, url_for
from werkzeug.utils import secure_filename

from .validation import REQUIRED_COLUMNS, validate_file

bp = Blueprint("uploads", __name__)

UPLOAD_DIR = "/app/uploads"                         # watched by Logstash
STAGING_DIR = os.path.join(UPLOAD_DIR, "staging")   # NOT watched by Logstash
ALLOWED = {"csv", "json"}                           # RM-15
STATUS_LABELS = {
    "en_attente": "En attente de confirmation",
    "rejete": "Rejeté",
    "annule": "Annulé",
    "en_cours": "Ingestion en cours",
    "termine": "Terminé",
}


def collection():
    return current_app.mongo["smartbuilding"]["uploads"]


@bp.route("/upload", methods=["GET", "POST"])
def upload():
    if request.method == "GET":
        return render_template("upload.html")

    file = request.files.get("file")
    if not file or not file.filename:
        return render_template("upload.html", error="Aucun fichier sélectionné.")
    name = secure_filename(file.filename)
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext not in ALLOWED:
        return render_template("upload.html", error="Format refusé : seuls les fichiers CSV et JSON sont acceptés.")

    os.makedirs(STAGING_DIR, exist_ok=True)
    stored_name = f"{datetime.now(timezone.utc):%Y%m%d-%H%M%S}_{name}"
    staging_path = os.path.join(STAGING_DIR, stored_name)
    file.save(staging_path)

    report = validate_file(staging_path, ext)
    record = {
        "original_name": file.filename,
        "stored_name": stored_name,
        "format": ext,
        "size": os.path.getsize(staging_path),
        "uploaded_at": datetime.now(timezone.utc),
        "total_lines": report["total"],
        "invalid_lines": report["invalid"],
        "errors": report["errors"],
        "status": "en_attente" if report["ok"] else "rejete",
        "reason": report["reason"],
    }
    if not report["ok"]:
        os.remove(staging_path)
    record_id = collection().insert_one(record).inserted_id
    return render_template("preview.html", record=record, record_id=record_id,
                           report=report, columns=REQUIRED_COLUMNS)


@bp.route("/upload/<record_id>/confirm", methods=["POST"])
def confirm(record_id):
    record = collection().find_one({"_id": ObjectId(record_id), "status": "en_attente"})
    if record:
        # Instant move: Logstash never sees a half-written file
        os.replace(os.path.join(STAGING_DIR, record["stored_name"]),
                   os.path.join(UPLOAD_DIR, record["stored_name"]))
        collection().update_one({"_id": record["_id"]}, {"$set": {"status": "en_cours"}})
    return redirect(url_for("uploads.files"))


@bp.route("/upload/<record_id>/cancel", methods=["POST"])
def cancel(record_id):
    record = collection().find_one({"_id": ObjectId(record_id), "status": "en_attente"})
    if record:
        os.remove(os.path.join(STAGING_DIR, record["stored_name"]))
        collection().update_one({"_id": record["_id"]}, {"$set": {"status": "annule"}})
    return redirect(url_for("uploads.upload"))


@bp.route("/files")
def files():
    records = list(collection().find().sort("uploaded_at", -1))
    for r in records:
        if r["status"] in ("en_cours", "termine"):
            # How many lines of this file are already in Elasticsearch
            r["indexed"] = current_app.es.count(
                index="sensor-logs-*", query={"term": {"source_file": r["stored_name"]}}
            )["count"]
            expected = r["total_lines"] - r["invalid_lines"]
            if r["status"] == "en_cours" and r["indexed"] >= expected:
                collection().update_one({"_id": r["_id"]}, {"$set": {"status": "termine"}})
                r["status"] = "termine"
    return render_template("files.html", records=records, labels=STATUS_LABELS)