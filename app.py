import base64
import json
import os
import queue
import shutil
import threading
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import quote

import requests
from flask import Flask, Response, jsonify, render_template, request, send_file

from downloader import WebsiteDownloader, get_site_name, zip_directory

app = Flask(__name__)

DOWNLOAD_FOLDER = "downloads"
REPO_UPLOAD_TOKEN = os.getenv("GITHUB_UPLOAD_TOKEN", "").strip()
REPO_UPLOAD_OWNER = os.getenv("GITHUB_TARGET_OWNER", "eurodrigobispo").strip()
REPO_UPLOAD_NAME = os.getenv("GITHUB_TARGET_REPO", "referencias-html").strip()
REPO_UPLOAD_BRANCH = os.getenv("GITHUB_TARGET_BRANCH", "main").strip()
REPO_UPLOAD_ROOT = os.getenv("GITHUB_TARGET_ROOT", "sites").strip().strip("/")
REPO_API_BASE = f"https://api.github.com/repos/{REPO_UPLOAD_OWNER}/{REPO_UPLOAD_NAME}"

if not os.path.exists(DOWNLOAD_FOLDER):
    os.makedirs(DOWNLOAD_FOLDER)

message_queues = {}
download_results = {}


def repo_upload_enabled():
    return bool(REPO_UPLOAD_TOKEN)


def cleanup_downloads_folder():
    """Remove all files and folders from downloads directory."""
    try:
        for item in os.listdir(DOWNLOAD_FOLDER):
            item_path = os.path.join(DOWNLOAD_FOLDER, item)
            if os.path.isfile(item_path):
                os.remove(item_path)
            elif os.path.isdir(item_path):
                shutil.rmtree(item_path)
        print("Downloads folder cleaned successfully")
    except Exception as exc:
        print(f"Error cleaning downloads folder: {exc}")


def remove_path(path):
    if not path or not os.path.exists(path):
        return

    if os.path.isdir(path):
        shutil.rmtree(path, ignore_errors=True)
    else:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass


def slugify(value):
    sanitized = []
    previous_dash = False

    for char in value.lower():
        if char.isalnum():
            sanitized.append(char)
            previous_dash = False
        elif not previous_dash:
            sanitized.append("-")
            previous_dash = True

    slug = "".join(sanitized).strip("-")
    return slug or "site"


def sanitize_filename(filename):
    base_name, extension = os.path.splitext(filename)
    return f"{slugify(base_name)}{extension.lower() or '.zip'}"


def build_repo_artifact_root(site_name, created_at=None):
    created_at = created_at or datetime.now(timezone.utc)
    timestamp = created_at.strftime("%Y-%m-%d/%H%M%S")
    site_slug = slugify(site_name)
    parts = [part for part in [REPO_UPLOAD_ROOT, site_slug, timestamp] if part]
    return "/".join(parts)


def github_api_headers():
    return {
        "Authorization": f"Bearer {REPO_UPLOAD_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def get_existing_github_sha(path):
    response = requests.get(
        f"{REPO_API_BASE}/contents/{quote(path, safe='/')}",
        headers=github_api_headers(),
        params={"ref": REPO_UPLOAD_BRANCH},
        timeout=30,
    )

    if response.status_code == 404:
        return None

    if response.status_code != 200:
        raise RuntimeError(
            f"GitHub GET failed ({response.status_code}): {response.text}"
        )

    return response.json().get("sha")


def upsert_github_content(path, content_bytes, message):
    payload = {
        "message": message,
        "content": base64.b64encode(content_bytes).decode("utf-8"),
        "branch": REPO_UPLOAD_BRANCH,
    }

    existing_sha = get_existing_github_sha(path)
    if existing_sha:
        payload["sha"] = existing_sha

    response = requests.put(
        f"{REPO_API_BASE}/contents/{quote(path, safe='/')}",
        headers=github_api_headers(),
        json=payload,
        timeout=60,
    )

    if response.status_code not in (200, 201):
        raise RuntimeError(
            f"GitHub PUT failed ({response.status_code}): {response.text}"
        )

    return response.json()


def upload_zip_to_reference_repo(zip_path, zip_filename, source_url, artifact_root):
    if not repo_upload_enabled():
        raise RuntimeError("GITHUB_UPLOAD_TOKEN is not configured on the server")

    zip_size = os.path.getsize(zip_path)
    if zip_size > 95 * 1024 * 1024:
        raise RuntimeError("ZIP file exceeds the GitHub Contents API size limit")

    zip_repo_path = f"{artifact_root}/{sanitize_filename(zip_filename)}"
    metadata_repo_path = f"{artifact_root}/metadata.json"

    with open(zip_path, "rb") as file_handle:
        zip_bytes = file_handle.read()

    metadata = {
        "source_url": source_url,
        "zip_filename": zip_filename,
        "artifact_root": artifact_root,
        "uploaded_at": datetime.now(timezone.utc).isoformat(),
        "repo": f"{REPO_UPLOAD_OWNER}/{REPO_UPLOAD_NAME}",
        "branch": REPO_UPLOAD_BRANCH,
    }

    zip_response = upsert_github_content(
        zip_repo_path, zip_bytes, f"Add website snapshot for {source_url}"
    )
    metadata_response = upsert_github_content(
        metadata_repo_path,
        json.dumps(metadata, indent=2).encode("utf-8"),
        f"Add metadata for {source_url}",
    )

    return {
        "status": "uploaded",
        "repo": f"{REPO_UPLOAD_OWNER}/{REPO_UPLOAD_NAME}",
        "path": zip_repo_path,
        "url": zip_response["content"]["html_url"],
        "metadata_url": metadata_response["content"]["html_url"],
    }


def enqueue_message(session_id, message):
    queue_ref = message_queues.get(session_id)
    if queue_ref:
        queue_ref.put(message)


def format_log_message(message, log_prefix=""):
    return f"{log_prefix}{message}" if log_prefix else message


def run_download_job(job_key, url, upload_to_repo, session_id, log_prefix=""):
    download_dir = os.path.join(DOWNLOAD_FOLDER, job_key)
    zip_path = os.path.join(DOWNLOAD_FOLDER, f"{job_key}.zip")

    def log_callback(message):
        enqueue_message(session_id, format_log_message(message, log_prefix))

    try:
        downloader = WebsiteDownloader(url, download_dir, log_callback=log_callback)
        success = downloader.process()

        if not success:
            enqueue_message(session_id, format_log_message("Falha no download", log_prefix))
            return {
                "status": "error",
                "source_url": url,
                "error": "Failed to download site",
                "zip_path": None,
                "filename": None,
                "repo_upload": {"status": "idle"},
            }

        site_name = get_site_name(url)
        zip_filename = f"{site_name}.zip"
        artifact_root = build_repo_artifact_root(site_name)

        enqueue_message(session_id, format_log_message("Criando arquivo ZIP...", log_prefix))
        zip_directory(download_dir, zip_path)
        remove_path(download_dir)

        repo_upload_result = {"status": "idle"}
        if upload_to_repo:
            if repo_upload_enabled():
                enqueue_message(
                    session_id,
                    format_log_message(
                        "Enviando ZIP para o repositorio de referencias...",
                        log_prefix,
                    ),
                )
                try:
                    repo_upload_result = upload_zip_to_reference_repo(
                        zip_path=zip_path,
                        zip_filename=zip_filename,
                        source_url=url,
                        artifact_root=artifact_root,
                    )
                    enqueue_message(
                        session_id,
                        format_log_message(
                            "ZIP enviado ao repositorio com sucesso.", log_prefix
                        ),
                    )
                except Exception as exc:
                    repo_upload_result = {"status": "error", "error": str(exc)}
                    enqueue_message(
                        session_id,
                        format_log_message(
                            f"ZIP gerado, mas o envio ao repositorio falhou: {exc}",
                            log_prefix,
                        ),
                    )
            else:
                repo_upload_result = {
                    "status": "unavailable",
                    "error": "GITHUB_UPLOAD_TOKEN is not configured on the server",
                }
                enqueue_message(
                    session_id,
                    format_log_message(
                        "ZIP gerado. O envio automatico ao repositorio esta indisponivel.",
                        log_prefix,
                    ),
                )

        enqueue_message(session_id, format_log_message("Download pronto!", log_prefix))
        return {
            "status": "complete",
            "source_url": url,
            "zip_path": zip_path,
            "filename": zip_filename,
            "site_name": site_name,
            "artifact_root": artifact_root,
            "repo_upload": repo_upload_result,
            "error": None,
        }
    except Exception as exc:
        enqueue_message(session_id, format_log_message(f"Erro: {exc}", log_prefix))
        remove_path(download_dir)
        remove_path(zip_path)
        return {
            "status": "error",
            "source_url": url,
            "zip_path": None,
            "filename": None,
            "repo_upload": {"status": "idle"},
            "error": str(exc),
        }


def build_batch_item(index, url):
    return {
        "index": index,
        "source_url": url,
        "status": "pending",
        "zip_path": None,
        "filename": None,
        "site_name": None,
        "artifact_root": None,
        "repo_upload": {"status": "idle"},
        "error": None,
    }


def batch_counts(items):
    completed = sum(1 for item in items if item.get("status") == "complete")
    failed = sum(1 for item in items if item.get("status") == "error")
    processing = sum(1 for item in items if item.get("status") == "processing")
    pending = sum(1 for item in items if item.get("status") == "pending")
    return {
        "total": len(items),
        "completed": completed,
        "failed": failed,
        "processing": processing,
        "pending": pending,
    }


def serialize_single_result(session_id, result):
    payload = {
        "mode": "single",
        "status": result.get("status"),
        "filename": result.get("filename"),
        "download_url": f"/download-file/{session_id}"
        if result.get("status") == "complete"
        else None,
        "source_url": result.get("source_url"),
        "upload_requested": result.get("upload_requested", False),
        "upload_available": repo_upload_enabled(),
        "repo_upload": result.get("repo_upload", {"status": "idle"}),
        "repo_name": f"{REPO_UPLOAD_OWNER}/{REPO_UPLOAD_NAME}",
    }

    if result.get("error"):
        payload["error"] = result["error"]

    return payload


def serialize_batch_result(session_id, result):
    items = []
    for item in result.get("items", []):
        item_payload = {
            "index": item["index"],
            "source_url": item["source_url"],
            "status": item["status"],
            "filename": item.get("filename"),
            "error": item.get("error"),
            "repo_upload": item.get("repo_upload", {"status": "idle"}),
            "download_url": f"/download-batch-file/{session_id}/{item['index']}"
            if item.get("status") == "complete"
            else None,
        }
        items.append(item_payload)

    return {
        "mode": "batch",
        "status": result.get("status"),
        "upload_requested": result.get("upload_requested", False),
        "upload_available": repo_upload_enabled(),
        "repo_name": f"{REPO_UPLOAD_OWNER}/{REPO_UPLOAD_NAME}",
        "summary": batch_counts(result.get("items", [])),
        "items": items,
    }


def cleanup_abandoned_sessions():
    """Clean up finished sessions after 30 minutes."""
    while True:
        time.sleep(300)
        current_time = time.time()
        sessions_to_remove = []

        for session_id, result in list(download_results.items()):
            created_at = result.get("created_at")
            if result.get("status") not in {"complete", "error"} or not created_at:
                continue

            if current_time - created_at <= 1800:
                continue

            if result.get("mode") == "batch":
                for item in result.get("items", []):
                    remove_path(item.get("zip_path"))
            else:
                remove_path(result.get("zip_path"))

            sessions_to_remove.append(session_id)

        for session_id in sessions_to_remove:
            message_queues.pop(session_id, None)
            download_results.pop(session_id, None)


def process_download(session_id, url, upload_to_repo=False):
    result = run_download_job(session_id, url, upload_to_repo, session_id)
    download_results[session_id] = {
        "mode": "single",
        "status": result["status"],
        "zip_path": result.get("zip_path"),
        "filename": result.get("filename"),
        "source_url": url,
        "site_name": result.get("site_name"),
        "artifact_root": result.get("artifact_root"),
        "upload_requested": upload_to_repo,
        "repo_upload": result.get("repo_upload", {"status": "idle"}),
        "error": result.get("error"),
        "created_at": time.time(),
    }


def process_batch_download(session_id, urls, upload_to_repo=False):
    batch_result = download_results[session_id]
    enqueue_message(session_id, f"Iniciando lote com {len(urls)} links...")

    try:
        for index, url in enumerate(urls):
            item = batch_result["items"][index]
            item["status"] = "processing"
            enqueue_message(session_id, f"[{index + 1}/{len(urls)}] Processando {url}")

            job_result = run_download_job(
                job_key=f"{session_id}-{index}",
                url=url,
                upload_to_repo=upload_to_repo,
                session_id=session_id,
                log_prefix=f"[{index + 1}/{len(urls)}] ",
            )

            item.update(job_result)

        counts = batch_counts(batch_result["items"])
        enqueue_message(
            session_id,
            (
                "Lote concluido. "
                f"{counts['completed']} sucesso(s), {counts['failed']} falha(s)."
            ),
        )
        batch_result["status"] = "complete"
        batch_result["created_at"] = time.time()
    except Exception as exc:
        batch_result["status"] = "error"
        batch_result["error"] = str(exc)
        batch_result["created_at"] = time.time()
        enqueue_message(session_id, f"Erro no lote: {exc}")


cleanup_downloads_folder()
cleanup_thread = threading.Thread(target=cleanup_abandoned_sessions, daemon=True)
cleanup_thread.start()


@app.route("/")
def index():
    return render_template(
        "index.html",
        repo_upload_enabled=repo_upload_enabled(),
        repo_upload_repo=f"{REPO_UPLOAD_OWNER}/{REPO_UPLOAD_NAME}",
    )


@app.route("/start-download", methods=["POST"])
def start_download():
    data = request.get_json() or {}
    url = (data.get("url") or "").strip()
    upload_to_repo = bool(data.get("upload_to_repo"))

    if not url:
        return jsonify({"error": "URL is required"}), 400

    session_id = str(uuid.uuid4())
    message_queues[session_id] = queue.Queue()
    download_results[session_id] = {
        "mode": "single",
        "status": "processing",
        "zip_path": None,
        "filename": None,
        "source_url": url,
        "upload_requested": upload_to_repo,
        "repo_upload": {"status": "idle"},
    }

    thread = threading.Thread(
        target=process_download, args=(session_id, url, upload_to_repo), daemon=True
    )
    thread.start()
    return jsonify({"session_id": session_id})


@app.route("/start-batch-download", methods=["POST"])
def start_batch_download():
    data = request.get_json() or {}
    urls = data.get("urls") or []
    upload_to_repo = bool(data.get("upload_to_repo"))

    cleaned_urls = []
    for raw_url in urls:
        url = (raw_url or "").strip()
        if url:
            cleaned_urls.append(url)

    if not cleaned_urls:
        return jsonify({"error": "At least one URL is required"}), 400

    session_id = str(uuid.uuid4())
    message_queues[session_id] = queue.Queue()
    download_results[session_id] = {
        "mode": "batch",
        "status": "processing",
        "upload_requested": upload_to_repo,
        "items": [build_batch_item(index, url) for index, url in enumerate(cleaned_urls)],
        "error": None,
    }

    thread = threading.Thread(
        target=process_batch_download,
        args=(session_id, cleaned_urls, upload_to_repo),
        daemon=True,
    )
    thread.start()
    return jsonify({"session_id": session_id})


@app.route("/stream/<session_id>")
def stream(session_id):
    def generate():
        if session_id not in message_queues:
            yield "data: Session not found\n\n"
            return

        queue_ref = message_queues[session_id]

        while True:
            try:
                message = queue_ref.get(timeout=60)
                yield f"data: {message}\n\n"

                result = download_results.get(session_id, {})
                if result.get("status") in {"complete", "error"}:
                    yield f"event: done\ndata: {result['status']}\n\n"
                    break
            except queue.Empty:
                yield ": keepalive\n\n"

    return Response(generate(), mimetype="text/event-stream")


@app.route("/session-result/<session_id>")
def session_result(session_id):
    result = download_results.get(session_id)
    if not result:
        return jsonify({"error": "Session not found"}), 404

    if result.get("mode") == "batch":
        return jsonify(serialize_batch_result(session_id, result))

    return jsonify(serialize_single_result(session_id, result))


@app.route("/upload-to-repo/<session_id>", methods=["POST"])
def upload_to_repo(session_id):
    result = download_results.get(session_id)
    if not result:
        return jsonify({"error": "Session not found"}), 404

    if result.get("mode") != "single":
        return jsonify({"error": "Manual upload is only available for single downloads"}), 409

    if result.get("status") != "complete":
        return jsonify({"error": "ZIP file is not ready yet"}), 409

    if not repo_upload_enabled():
        return (
            jsonify({"error": "GITHUB_UPLOAD_TOKEN is not configured on the server"}),
            503,
        )

    zip_path = result.get("zip_path")
    if not zip_path or not os.path.exists(zip_path):
        return jsonify({"error": "ZIP file is no longer available"}), 410

    if result.get("repo_upload", {}).get("status") == "uploaded":
        return jsonify(result["repo_upload"])

    try:
        upload_result = upload_zip_to_reference_repo(
            zip_path=zip_path,
            zip_filename=result["filename"],
            source_url=result["source_url"],
            artifact_root=result["artifact_root"],
        )
        result["repo_upload"] = upload_result
        return jsonify(upload_result)
    except Exception as exc:
        result["repo_upload"] = {"status": "error", "error": str(exc)}
        return jsonify(result["repo_upload"]), 500


@app.route("/download-file/<session_id>")
def download_file(session_id):
    result = download_results.get(session_id)
    if not result or result.get("mode") != "single":
        return "File not ready", 404

    if result.get("status") != "complete":
        return "File not ready", 404

    zip_path = result.get("zip_path")
    filename = result.get("filename")

    if not zip_path or not os.path.exists(zip_path):
        return "File not found", 404

    result["downloaded_at"] = time.time()
    return send_file(zip_path, as_attachment=True, download_name=filename)


@app.route("/download-batch-file/<session_id>/<int:item_index>")
def download_batch_file(session_id, item_index):
    result = download_results.get(session_id)
    if not result or result.get("mode") != "batch":
        return "File not ready", 404

    items = result.get("items", [])
    if item_index < 0 or item_index >= len(items):
        return "File not found", 404

    item = items[item_index]
    if item.get("status") != "complete":
        return "File not ready", 404

    zip_path = item.get("zip_path")
    filename = item.get("filename")
    if not zip_path or not os.path.exists(zip_path):
        return "File not found", 404

    item["downloaded_at"] = time.time()
    return send_file(zip_path, as_attachment=True, download_name=filename)


if __name__ == "__main__":
    app.run(debug=True, port=5001, threaded=True)
