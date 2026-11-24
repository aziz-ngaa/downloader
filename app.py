import os, shutil, tempfile, threading, time, uuid
from urllib.parse import urlparse

import requests
import yt_dlp
from flask import Flask, Response, abort, jsonify, request, send_file

app = Flask(__name__)
HERE = os.path.dirname(os.path.abspath(__file__))
TMP = os.path.join(tempfile.gettempdir(), "ytdl_minimal")
os.makedirs(TMP, exist_ok=True)
JOBS, CACHE = {}, {}
HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}


def valid(url):
    try:
        return urlparse(url).hostname in HOSTS
    except Exception:
        return False


def clean_err(e):
    return str(e).replace("ERROR: ", "")[:300]


@app.get("/")
def index():
    return send_file(os.path.join(HERE, "index.html"))


@app.post("/api/info")
def api_info():
    url = (request.json or {}).get("url", "").strip()
    if not valid(url):
        return jsonify(error="Enter a valid YouTube link."), 400
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "noplaylist": True, "skip_download": True}) as y:
            i = y.extract_info(url, download=False)
    except Exception as e:
        return jsonify(error=clean_err(e)), 400

    fm = i.get("formats", [])
    size = lambda f: f.get("filesize") or f.get("filesize_approx") or 0
    audio = [f for f in fm if f.get("vcodec") == "none" and f.get("acodec") not in (None, "none")]
    best_a = max(audio, key=lambda f: f.get("abr") or 0, default=None)
    asz = size(best_a) if best_a else 0

    vids = {}
    for f in fm:
        h = f.get("height")
        if f.get("vcodec") in (None, "none") or not h:
            continue
        if h not in vids or (f.get("tbr") or 0) > vids[h]["tbr"]:
            vids[h] = {"tbr": f.get("tbr") or 0, "fps": f.get("fps"), "size": size(f)}
    video = [{"height": h, "fps": v["fps"], "size": (v["size"] + asz) or None}
             for h, v in sorted(vids.items(), reverse=True)]

    dur = i.get("duration") or 0
    return jsonify(
        id=i["id"], title=i["title"], channel=i.get("uploader"), duration=dur,
        thumb=i.get("thumbnail"), url=url, video=video,
        audio=[{"kbps": k, "size": int(dur * k * 125)} for k in (320, 256, 192, 128)],
    )


@app.get("/api/preview-audio")
def preview_audio():
    url = request.args.get("url", "")
    if not valid(url):
        abort(400)
    hit = CACHE.get(url)
    if not hit or time.time() - hit[0] > 1800:
        try:
            with yt_dlp.YoutubeDL({"quiet": True, "noplaylist": True,
                                   "format": "bestaudio[ext=m4a]/bestaudio"}) as y:
                i = y.extract_info(url, download=False)
        except Exception:
            abort(502)
        hit = CACHE[url] = (time.time(), i["url"], dict(i.get("http_headers", {})))
    headers = dict(hit[2])
    headers["Range"] = request.headers.get("Range", "bytes=0-")
    r = requests.get(hit[1], headers=headers, stream=True, timeout=20)
    keep = ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges")
    return Response(r.iter_content(65536), status=r.status_code,
                    headers={k: r.headers[k] for k in keep if k in r.headers})


def run(job, url, kind, q):
    d = os.path.join(TMP, job)
    os.makedirs(d)

    def hook(p):
        if p["status"] == "downloading":
            t = p.get("total_bytes") or p.get("total_bytes_estimate") or 0
            JOBS[job].update(stage="Downloading", speed=(p.get("_speed_str") or "").strip(),
                             pct=p["downloaded_bytes"] / t * 100 if t else 0)
        elif p["status"] == "finished":
            JOBS[job]["stage"] = "Processing"

    o = {"quiet": True, "noplaylist": True, "windowsfilenames": True,
         "outtmpl": os.path.join(d, "%(title).150B.%(ext)s"), "progress_hooks": [hook]}
    if kind == "mp4":
        h = int(q)
        o.update(format=f"bv*[height<={h}][vcodec^=avc1]+ba[ext=m4a]/bv*[height<={h}]+ba/b[height<={h}]/b",
                 merge_output_format="mp4")
    else:
        o.update(format="bestaudio/best", writethumbnail=True, postprocessors=[
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": str(int(q))},
            {"key": "FFmpegThumbnailsConvertor", "format": "jpg", "when": "before_dl"},
            {"key": "FFmpegMetadata"}, {"key": "EmbedThumbnail"}])
    try:
        with yt_dlp.YoutubeDL(o) as y:
            y.download([url])
        name = next(x for x in os.listdir(d) if x.endswith((".mp4", ".mp3")))
        JOBS[job].update(stage="Done", pct=100, file=os.path.join(d, name), name=name, done=True)
    except Exception as e:
        JOBS[job].update(error=clean_err(e), done=True)


@app.post("/api/download")
def api_download():
    b = request.json or {}
    if not valid(b.get("url", "")) or b.get("kind") not in ("mp4", "mp3"):
        return jsonify(error="Bad request."), 400
    for n in os.listdir(TMP):  # purge anything older than an hour
        p = os.path.join(TMP, n)
        if time.time() - os.path.getmtime(p) > 3600:
            shutil.rmtree(p, ignore_errors=True)
    job = uuid.uuid4().hex
    JOBS[job] = {"stage": "Starting", "pct": 0}
    threading.Thread(target=run, args=(job, b["url"], b["kind"], b["q"]), daemon=True).start()
    return jsonify(job=job)


@app.get("/api/progress/<job>")
def api_progress(job):
    j = JOBS.get(job)
    return (jsonify({k: v for k, v in j.items() if k != "file"}) if j else (jsonify(error="Unknown job."), 404))


@app.get("/api/file/<job>")
def api_file(job):
    j = JOBS.get(job)
    if not j or not j.get("file"):
        abort(404)
    return send_file(j["file"], as_attachment=True, download_name=j["name"])


if __name__ == "__main__":
    if not shutil.which("ffmpeg"):
        print("! ffmpeg not found. Install it, or MP3 conversion and HD merging will fail.")
    port = int(os.environ.get("PORT", 7860))
    print(f"Open http://0.0.0.0:{port}")
    app.run("0.0.0.0", port, threaded=True)
