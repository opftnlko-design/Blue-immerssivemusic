#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BLU Music Local Server
Uses yt-dlp to stream and download audio from YouTube.
Run this first, then open http://localhost:8765 in your browser.
"""
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

import os
# sys already imported above
import json
import subprocess
import threading
import tempfile
import shutil
from pathlib import Path

# ── Auto-install dependencies ──
def install_pkg(pkg):
    subprocess.check_call([sys.executable, "-m", "pip", "install", pkg, "-q"])

try:
    from flask import Flask, request, Response, send_file, jsonify, send_from_directory
    from flask_cors import CORS
except ImportError:
    print("Installing Flask and flask-cors...")
    install_pkg("flask")
    install_pkg("flask-cors")
    from flask import Flask, request, Response, send_file, jsonify, send_from_directory
    from flask_cors import CORS

try:
    import yt_dlp
except ImportError:
    print("Installing yt-dlp...")
    install_pkg("yt-dlp")
    import yt_dlp

# ────────────────────────────────
app = Flask(__name__, static_folder=None)
CORS(app)

BASE_DIR = Path(__file__).parent
DOWNLOADS_DIR = BASE_DIR / "downloads"
DOWNLOADS_DIR.mkdir(exist_ok=True)

CACHE = {}  # video_id -> stream_url (short-lived)

# ── YDL common options ──
YDL_BASE_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "extract_flat": False,
    "nocheckcertificate": True,
}

# ── Serve the main HTML app ──
@app.route("/")
def index():
    return send_from_directory(str(BASE_DIR), "index.html")

@app.route("/<path:filename>")
def static_files(filename):
    return send_from_directory(str(BASE_DIR), filename)


# ── Get audio stream URL (for browser playback via <audio> or YouTube embed) ──
@app.route("/api/stream")
def stream_audio():
    vid = request.args.get("id", "").strip()
    if not vid or len(vid) != 11:
        return jsonify({"error": "Invalid video ID"}), 400

    # Return cached URL if still fresh
    if vid in CACHE:
        return jsonify({"url": CACHE[vid], "id": vid})

    ydl_opts = {
        **YDL_BASE_OPTS,
        "format": "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best",
        "skip_download": True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={vid}", download=False)
            url = info.get("url") or (info.get("formats", [{}])[-1].get("url"))
            if not url:
                return jsonify({"error": "No stream URL found"}), 404
            CACHE[vid] = url
            # Expire cache after 45 minutes
            def expire():
                CACHE.pop(vid, None)
            t = threading.Timer(45 * 60, expire)
            t.daemon = True
            t.start()
            return jsonify({
                "url": url,
                "id": vid,
                "title": info.get("title", ""),
                "duration": info.get("duration", 0),
                "thumbnail": info.get("thumbnail", "")
            })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Proxy the audio stream through the server (handles CORS for browser) ──
@app.route("/api/proxy")
def proxy_audio():
    vid = request.args.get("id", "").strip()
    if not vid or len(vid) != 11:
        return jsonify({"error": "Invalid video ID"}), 400

    ydl_opts = {
        **YDL_BASE_OPTS,
        "format": "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best",
        "skip_download": True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={vid}", download=False)
            stream_url = info.get("url") or (info.get("formats", [{}])[-1].get("url"))
            if not stream_url:
                return jsonify({"error": "No stream URL found"}), 404

        import urllib.request
        range_header = request.headers.get("Range", None)
        req = urllib.request.Request(stream_url, headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://www.youtube.com/",
        })
        if range_header:
            req.add_header("Range", range_header)

        resp = urllib.request.urlopen(req)
        content_type = resp.headers.get("Content-Type", "audio/mp4")
        status = 206 if range_header else 200

        def generate():
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                yield chunk

        headers = {
            "Content-Type": content_type,
            "Accept-Ranges": "bytes",
            "Access-Control-Allow-Origin": "*",
        }
        if resp.headers.get("Content-Length"):
            headers["Content-Length"] = resp.headers.get("Content-Length")
        if resp.headers.get("Content-Range"):
            headers["Content-Range"] = resp.headers.get("Content-Range")

        return Response(generate(), status=status, headers=headers)

    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Download track (saves to downloads/ folder) ──
@app.route("/api/download")
def download_track():
    vid = request.args.get("id", "").strip()
    title = request.args.get("title", vid).strip()
    if not vid or len(vid) != 11:
        return jsonify({"error": "Invalid video ID"}), 400

    out_path = DOWNLOADS_DIR / f"{vid}.m4a"
    if out_path.exists():
        return jsonify({"status": "already_downloaded", "file": str(out_path), "id": vid})

    ydl_opts = {
        **YDL_BASE_OPTS,
        "format": "bestaudio[ext=m4a]/bestaudio/best",
        "outtmpl": str(DOWNLOADS_DIR / f"{vid}.%(ext)s"),
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "m4a",
            "preferredquality": "192",
        }],
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([f"https://www.youtube.com/watch?v={vid}"])
        # Find the output file
        for f in DOWNLOADS_DIR.glob(f"{vid}.*"):
            return jsonify({"status": "downloaded", "file": str(f), "id": vid})
        return jsonify({"error": "File not found after download"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Serve a downloaded file ──
@app.route("/api/file/<vid>")
def serve_file(vid):
    as_attachment = request.args.get("download", "false").lower() == "true"
    title = request.args.get("title", vid).strip()
    
    # Sanitize title for safe filename
    import re
    safe_title = re.sub(r'[\\/*?:"<>|]', "", title)
    if not safe_title:
        safe_title = vid
        
    for f in DOWNLOADS_DIR.glob(f"{vid}.*"):
        return send_file(
            str(f),
            mimetype="audio/mp4",
            as_attachment=as_attachment,
            download_name=f"{safe_title}.m4a" if as_attachment else None,
            conditional=True
        )
    return jsonify({"error": "Not found"}), 404


# ── Check if a track is downloaded ──
@app.route("/api/is_downloaded")
def is_downloaded():
    vid = request.args.get("id", "").strip()
    for f in DOWNLOADS_DIR.glob(f"{vid}.*"):
        return jsonify({"downloaded": True, "id": vid})
    return jsonify({"downloaded": False, "id": vid})


# ── List downloaded tracks ──
@app.route("/api/downloads")
def list_downloads():
    files = []
    for f in DOWNLOADS_DIR.iterdir():
        if f.suffix in (".m4a", ".mp3", ".webm", ".opus"):
            files.append({"id": f.stem, "file": f.name, "size": f.stat().st_size})
    return jsonify(files)


# ── Delete a downloaded track ──
@app.route("/api/delete")
def delete_track():
    vid = request.args.get("id", "").strip()
    deleted = False
    for f in DOWNLOADS_DIR.glob(f"{vid}.*"):
        f.unlink()
        deleted = True
    return jsonify({"deleted": deleted, "id": vid})


# ── Search via yt-dlp (fallback if Piped/Invidious all fail) ──
@app.route("/api/search")
def search():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"items": []})
    limit = int(request.args.get("limit", 20))

    ydl_opts = {
        **YDL_BASE_OPTS,
        "extract_flat": True,
        "playlist_items": f"1:{limit}",
        "skip_download": True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"ytsearch{limit}:{q}", download=False)
            entries = info.get("entries", [])
            items = []
            for e in entries:
                vid = e.get("id") or e.get("url", "").split("v=")[-1]
                if vid and len(vid) == 11:
                    items.append({
                        "id": vid,
                        "title": e.get("title", "Unknown"),
                        "artist": e.get("uploader") or e.get("channel") or "Unknown",
                        "duration": e.get("duration") or 0,
                        "thumb": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"
                    })
            return jsonify({"items": items})
    except Exception as e:
        return jsonify({"error": str(e), "items": []}), 500


if __name__ == "__main__":
    port = 8765
    print(f"\n{'='*50}")
    print(f"  [BLU] Music Server starting...")
    print(f"  [>>]  Open in browser: http://localhost:{port}")
    print(f"  [DL]  Downloads saved to: {DOWNLOADS_DIR}")
    print(f"  [!!]  Press Ctrl+C to stop")
    print(f"{'='*50}\n")

    # Auto-open browser
    import webbrowser, time
    threading.Timer(1.5, lambda: webbrowser.open(f"http://localhost:{port}")).start()

    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
