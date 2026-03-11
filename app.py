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


cleanup_downloads_folder()

message_queues = {}
download_results = {}


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

    zip_message = f"Add website snapshot for {source_url}"
    metadata = {
        "source_url": source_url,
        "zip_filename": zip_filename,
        "artifact_root": artifact_root,
        "uploaded_at": datetime.now(timezone.utc).isoformat(),
        "repo": f"{REPO_UPLOAD_OWNER}/{REPO_UPLOAD_NAME}",
        "branch": REPO_UPLOAD_BRANCH,
    }

    zip_response = upsert_github_content(zip_repo_path, zip_bytes, zip_message)
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


def cleanup_abandoned_sessions():
    """Clean up finished sessions after 30 minutes."""
    while True:
        time.sleep(300)
        current_time = time.time()

        sessions_to_remove = []
        for session_id, result in list(download_results.items()):
            created_at = result.get("created_at")
            if result.get("status") in {"complete", "error"} and created_at:
                age = current_time - created_at
                if age > 1800:
                    zip_path = result.get("zip_path")
                    if zip_path and os.path.exists(zip_path):
                        try:
                            os.remove(zip_path)
                            print(f"Removed expired file: {os.path.basename(zip_path)}")
                        except Exception:
                            pass
                    sessions_to_remove.append(session_id)

        for session_id in sessions_to_remove:
            message_queues.pop(session_id, None)
            download_results.pop(session_id, None)


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


def process_download(session_id, url, upload_to_repo=False):
    q = message_queues[session_id]
    download_dir = os.path.join(DOWNLOAD_FOLDER, session_id)
    zip_path = os.path.join(DOWNLOAD_FOLDER, f"{session_id}.zip")

    def log_callback(message):
        q.put(message)

    try:
        downloader = WebsiteDownloader(url, download_dir, log_callback=log_callback)
        success = downloader.process()

        if not success:
            q.put("Falha no download")
            download_results[session_id] = {
                **download_results[session_id],
                "status": "error",
                "error": "Failed to download site",
                "created_at": time.time(),
            }
            return

        site_name = get_site_name(url)
        zip_filename = f"{site_name}.zip"
        artifact_root = build_repo_artifact_root(site_name)

        q.put("Criando arquivo ZIP...")
        zip_directory(download_dir, zip_path)
        shutil.rmtree(download_dir)

        repo_upload_result = {"status": "idle"}
        if upload_to_repo:
            if repo_upload_enabled():
                q.put("Enviando ZIP para o repositório de referencias...")
                try:
                    repo_upload_result = upload_zip_to_reference_repo(
                        zip_path=zip_path,
                        zip_filename=zip_filename,
                        source_url=url,
                        artifact_root=artifact_root,
                    )
                    q.put("ZIP enviado ao repositório com sucesso.")
                except Exception as exc:
                    repo_upload_result = {"status": "error", "error": str(exc)}
                    q.put(f"ZIP gerado, mas o envio ao repositório falhou: {exc}")
            else:
                repo_upload_result = {
                    "status": "unavailable",
                    "error": "GITHUB_UPLOAD_TOKEN is not configured on the server",
                }
                q.put(
                    "ZIP gerado. O envio automatico ao repositório esta indisponivel."
                )

        q.put("Download pronto!")
        download_results[session_id] = {
            **download_results[session_id],
            "status": "complete",
            "zip_path": zip_path,
            "filename": zip_filename,
            "site_name": site_name,
            "artifact_root": artifact_root,
            "repo_upload": repo_upload_result,
            "created_at": time.time(),
        }
    except Exception as exc:
        q.put(f"Erro: {exc}")
        download_results[session_id] = {
            **download_results.get(session_id, {}),
            "status": "error",
            "error": str(exc),
            "created_at": time.time(),
        }

        try:
            if os.path.exists(download_dir):
                shutil.rmtree(download_dir)
            if os.path.exists(zip_path):
                os.remove(zip_path)
        except Exception:
            pass


@app.route("/stream/<session_id>")
def stream(session_id):
    def generate():
        if session_id not in message_queues:
            yield "data: Session not found\n\n"
            return

        q = message_queues[session_id]

        while True:
            try:
                message = q.get(timeout=60)
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

    payload = {
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

    return jsonify(payload)


@app.route("/upload-to-repo/<session_id>", methods=["POST"])
def upload_to_repo(session_id):
    result = download_results.get(session_id)

    if not result:
        return jsonify({"error": "Session not found"}), 404

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

    if not result or result.get("status") != "complete":
        return "File not ready", 404

    zip_path = result.get("zip_path")
    filename = result.get("filename")

    if not zip_path or not os.path.exists(zip_path):
        return "File not found", 404

    result["downloaded_at"] = time.time()
    return send_file(zip_path, as_attachment=True, download_name=filename)


if __name__ == "__main__":
    app.run(debug=True, port=5001, threaded=True)
