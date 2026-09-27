import os
import uuid
import threading
from flask import Flask, request, jsonify, send_from_directory, render_template_string

import yt_dlp

app = Flask(__name__)

DOWNLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# In-memory job tracker: job_id -> {"status": ..., "progress": ..., "filename": ...}
JOBS = {}

# ---------------------------------------------------------------------------
# Frontend (single page) — kept as one HTML string so the whole app is one file
# ---------------------------------------------------------------------------
PAGE = """
<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>محمّل الفيديوهات</title>
<style>
  * { box-sizing: border-box; }
  body {
    font-family: "Segoe UI", Tahoma, sans-serif;
    background: #0f172a;
    color: #e2e8f0;
    margin: 0;
    padding: 24px;
    display: flex;
    justify-content: center;
  }
  .card {
    background: #1e293b;
    border-radius: 16px;
    padding: 24px;
    max-width: 480px;
    width: 100%;
    box-shadow: 0 10px 30px rgba(0,0,0,0.3);
  }
  h1 { font-size: 20px; margin-top: 0; text-align: center; }
  input[type=text] {
    width: 100%;
    padding: 12px;
    border-radius: 10px;
    border: 1px solid #334155;
    background: #0f172a;
    color: #e2e8f0;
    font-size: 15px;
  }
  button {
    margin-top: 12px;
    width: 100%;
    padding: 12px;
    border: none;
    border-radius: 10px;
    background: #6366f1;
    color: white;
    font-size: 15px;
    cursor: pointer;
  }
  button:disabled { opacity: 0.6; cursor: not-allowed; }
  .info { margin-top: 18px; }
  .info img { width: 100%; border-radius: 10px; }
  select {
    width: 100%;
    padding: 10px;
    border-radius: 10px;
    margin-top: 10px;
    background: #0f172a;
    color: #e2e8f0;
    border: 1px solid #334155;
  }
  .status { margin-top: 14px; font-size: 14px; text-align: center; }
  .error { color: #f87171; }
  a.download-link {
    display: block;
    margin-top: 14px;
    text-align: center;
    background: #16a34a;
    color: white;
    padding: 12px;
    border-radius: 10px;
    text-decoration: none;
  }
</style>
</head>
<body>
<div class="card">
  <h1>حمّل الفيديو من اللينك</h1>
  <input type="text" id="url" placeholder="الصق اللينك هنا">
  <button id="fetchBtn">جيب معلومات الفيديو</button>

  <div class="info" id="info" style="display:none">
    <img id="thumb" src="">
    <p id="title"></p>
    <select id="formats"></select>
    <button id="downloadBtn">تحميل</button>
  </div>

  <div class="status" id="status"></div>
</div>

<script>
const fetchBtn = document.getElementById('fetchBtn');
const downloadBtn = document.getElementById('downloadBtn');
const statusEl = document.getElementById('status');
const infoEl = document.getElementById('info');
const formatsEl = document.getElementById('formats');

fetchBtn.onclick = async () => {
  const url = document.getElementById('url').value.trim();
  if (!url) return;
  statusEl.textContent = 'بجيب المعلومات...';
  statusEl.className = 'status';
  infoEl.style.display = 'none';

  try {
    const res = await fetch('/api/info', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({url})
    });
    const data = await res.json();
    if (data.error) throw new Error(data.error);

    document.getElementById('thumb').src = data.thumbnail || '';
    document.getElementById('title').textContent = data.title || '';
    formatsEl.innerHTML = '';
    data.formats.forEach(f => {
      const opt = document.createElement('option');
      opt.value = f.format_id;
      const size = f.filesize ? (f.filesize/1024/1024).toFixed(1) + ' MB' : '';
      opt.textContent = `${f.resolution} - ${f.ext} ${size}`;
      formatsEl.appendChild(opt);
    });
    infoEl.style.display = 'block';
    statusEl.textContent = '';
  } catch (e) {
    statusEl.textContent = e.message;
    statusEl.className = 'status error';
  }
};

downloadBtn.onclick = async () => {
  const url = document.getElementById('url').value.trim();
  const format_id = formatsEl.value;
  statusEl.textContent = 'بدء التحميل...';
  statusEl.className = 'status';

  const res = await fetch('/api/download', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({url, format_id})
  });
  const data = await res.json();
  if (data.error) {
    statusEl.textContent = data.error;
    statusEl.className = 'status error';
    return;
  }

  const jobId = data.job_id;
  const poll = setInterval(async () => {
    const r = await fetch(`/api/status/${jobId}`);
    const s = await r.json();
    if (s.status === 'downloading') {
      statusEl.textContent = `بيتحمل... ${s.progress ?? ''}%`;
    } else if (s.status === 'processing') {
      statusEl.textContent = 'بيتجهز...';
    } else if (s.status === 'done') {
      clearInterval(poll);
      statusEl.innerHTML = '';
      const link = document.createElement('a');
      link.href = `/api/file/${jobId}`;
      link.className = 'download-link';
      link.textContent = 'اضغط هنا لحفظ الفيديو';
      statusEl.appendChild(link);
    } else if (s.status === 'error') {
      clearInterval(poll);
      statusEl.textContent = s.error;
      statusEl.className = 'status error';
    }
  }, 1000);
};
</script>
</body>
</html>
"""


@app.route("/")
def index():
    return render_template_string(PAGE)


@app.route("/api/info", methods=["POST"])
def get_info():
    """Fetch basic info + available formats for a given URL, without downloading."""
    data = request.get_json(force=True)
    url = (data or {}).get("url", "").strip()
    if not url:
        return jsonify({"error": "من فضلك ادخل رابط صحيح"}), 400

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        return jsonify({"error": f"مقدرتش أجيب معلومات الفيديو: {e}"}), 400

    formats = []
    for f in info.get("formats", []):
        if f.get("vcodec") != "none" and f.get("acodec") != "none":
            formats.append({
                "format_id": f.get("format_id"),
                "ext": f.get("ext"),
                "resolution": f.get("format_note") or f.get("resolution") or "unknown",
                "filesize": f.get("filesize") or f.get("filesize_approx"),
            })

    return jsonify({
        "title": info.get("title"),
        "thumbnail": info.get("thumbnail"),
        "duration": info.get("duration"),
        "formats": formats,
    })


def _run_download(job_id, url, format_id):
    def progress_hook(d):
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            downloaded = d.get("downloaded_bytes", 0)
            pct = int(downloaded * 100 / total) if total else None
            JOBS[job_id]["progress"] = pct
        elif d.get("status") == "finished":
            JOBS[job_id]["status"] = "processing"

    outtmpl = os.path.join(DOWNLOAD_DIR, f"{job_id}_%(title).80s.%(ext)s")
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "outtmpl": outtmpl,
        "progress_hooks": [progress_hook],
        "format": format_id if format_id else "bestvideo+bestaudio/best",
        "merge_output_format": "mp4",
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
            base, _ = os.path.splitext(filename)
            final_path = base + ".mp4" if os.path.exists(base + ".mp4") else filename
        JOBS[job_id]["status"] = "done"
        JOBS[job_id]["filename"] = os.path.basename(final_path)
    except Exception as e:
        JOBS[job_id]["status"] = "error"
        JOBS[job_id]["error"] = str(e)


@app.route("/api/download", methods=["POST"])
def start_download():
    data = request.get_json(force=True)
    url = (data or {}).get("url", "").strip()
    format_id = (data or {}).get("format_id")
    if not url:
        return jsonify({"error": "من فضلك ادخل رابط صحيح"}), 400

    job_id = uuid.uuid4().hex[:12]
    JOBS[job_id] = {"status": "downloading", "progress": 0}
    thread = threading.Thread(target=_run_download, args=(job_id, url, format_id), daemon=True)
    thread.start()
    return jsonify({"job_id": job_id})


@app.route("/api/status/<job_id>")
def job_status(job_id):
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "job not found"}), 404
    return jsonify(job)


@app.route("/api/file/<job_id>")
def get_file(job_id):
    job = JOBS.get(job_id)
    if not job or job.get("status") != "done":
        return jsonify({"error": "الملف لسه مش جاهز"}), 400
    return send_from_directory(DOWNLOAD_DIR, job["filename"], as_attachment=True)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
