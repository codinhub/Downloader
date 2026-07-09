import io
import json
import time
import os
import threading
import urllib.request
import urllib.parse
from flask import Flask, request, jsonify, Response
import yt_dlp

app = Flask(__name__)

CACHE = {}
CACHE_TTL = int(os.environ.get("CACHE_TTL", "1800"))
ALLOWED_ORIGIN = os.environ.get("ALLOWED_ORIGIN", "*")
CACHE_LOCK = threading.Lock()


def build_cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = ALLOWED_ORIGIN
    resp.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp


@app.after_request
def after_request(resp):
    return build_cors(resp)


@app.route("/api/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/api/options", methods=["OPTIONS"])
def options():
    return ("", 204)


@app.route("/api/extract", methods=["OPTIONS"])
def extract_options():
    return ("", 204)


def classify_format(f):
    vcodec = f.get("vcodec") or "none"
    acodec = f.get("acodec") or "none"
    if vcodec != "none" and acodec != "none":
        return "video"
    if vcodec != "none":
        return "video_only"
    if acodec != "none":
        return "audio"
    return "other"


def format_resolution(f):
    height = f.get("height")
    width = f.get("width")
    if height:
        return f"{height}p"
    if width:
        return f"{width}x{height or ''}"
    note = f.get("format_note")
    if note:
        return str(note)
    return "audio" if f.get("acodec") not in (None, "none") else "unknown"


def extract_formats(info):
    formats = info.get("formats") or []
    out = []
    seen = set()
    for f in formats:
        url = f.get("url")
        if not url:
            continue
        kind = classify_format(f)
        if kind == "other":
            continue
        height = f.get("height") or 0
        ext = f.get("ext") or "mp4"
        key = (height, ext, kind, f.get("format_id"))
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "format_id": f.get("format_id"),
            "kind": kind,
            "ext": ext,
            "resolution": "audio" if kind == "audio" else format_resolution(f),
            "height": height,
            "filesize": f.get("filesize") or f.get("filesize_approx"),
            "tbr": f.get("tbr"),
            "format_note": f.get("format_note"),
            "url": url,
            "http_headers": f.get("http_headers", {}),
        })
    out.sort(key=lambda x: (x["height"] or 0, x["tbr"] or 0), reverse=True)
    return out


@app.route("/api/extract")
def extract():
    url = request.args.get("url", "").strip()
    if not url or not url.startswith(("http://", "https://")):
        return jsonify({"ok": False, "error": "Please provide a valid http(s) URL."}), 400

    now = time.time()
    with CACHE_LOCK:
        cached = CACHE.get(url)
        if cached and now - cached["ts"] < CACHE_TTL:
            return jsonify(cached["data"])

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "simulate": True,
        "socket_timeout": 20,
        "retries": 2,
        "extractor_args": {
            "youtube": {
                "player_client": ["tv", "tv_embedded", "web_smarttv", "ios", "web"],
            }
        },
    }
    cookies_file = os.environ.get("YT_COOKIES_FILE") or "/app/cookies.txt"
    if cookies_file and os.path.exists(cookies_file):
        ydl_opts["cookiefile"] = cookies_file
    po_token = os.environ.get("YT_PO_TOKEN")
    if po_token:
        ydl_opts["extractor_args"]["youtube"]["po_token"] = po_token
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        msg = str(e).splitlines()[0] if str(e) else "Could not extract this link."
        return jsonify({"ok": False, "error": msg}), 422
    except Exception:
        return jsonify({"ok": False, "error": "Unexpected error while processing the link."}), 500

    if not info:
        return jsonify({"ok": False, "error": "No data found for this link."}), 422

    result = {
        "ok": True,
        "title": info.get("title"),
        "thumbnail": info.get("thumbnail"),
        "duration": info.get("duration"),
        "source": info.get("extractor") or info.get("extractor_key"),
        "webpage_url": info.get("webpage_url") or url,
        "formats": extract_formats(info),
    }

    with CACHE_LOCK:
        CACHE[url] = {"ts": now, "data": result}

    return jsonify(result)


@app.route("/api/download")
def download():
    url = request.args.get("url", "").strip()
    if not url or not url.startswith(("http://", "https://")):
        return "Invalid URL", 400

    try:
        hdr_arg = request.args.get("h", "")
        extra = json.loads(hdr_arg) if hdr_arg else {}
    except Exception:
        extra = {}

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Referer": urllib.parse.urlparse(url).scheme + "://" + urllib.parse.urlparse(url).netloc + "/",
    }
    for k, v in extra.items():
        if v:
            headers[str(k)] = str(v)

    req = urllib.request.Request(url, headers=headers)
    try:
        upstream = urllib.request.urlopen(req, timeout=120)
    except Exception:
        return "Could not fetch the media from the source.", 502

    ctype = upstream.headers.get("Content-Type") or "application/octet-stream"
    disp = upstream.headers.get("Content-Disposition")
    if not disp:
        fname = urllib.parse.unquote(url.rsplit("/", 1)[-1].split("?")[0]) or "video"
        if "." not in fname:
            fname += ".mp4"
        disp = 'attachment; filename="%s"' % fname.replace('"', "")

    clen = upstream.headers.get("Content-Length", "")

    def generate():
        while True:
            chunk = upstream.read(65536)
            if not chunk:
                break
            yield chunk

    return Response(
        generate(),
        headers={
            "Content-Type": ctype,
            "Content-Disposition": disp,
            "Content-Length": clen,
            "Cache-Control": "no-store",
            "Access-Control-Allow-Origin": ALLOWED_ORIGIN,
        },
    )


@app.route("/")
def index():
    return jsonify({
        "service": "social-video-downloader-api",
        "endpoints": ["/api/extract?url=", "/api/health"],
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
