import sqlite3
import json
import random
import threading
import os
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

DB_PATH = "/home/david/loupe/aesthetic/preferences.db"
THUMBS = "/home/david/loupe-pipeline/culling/contactsheets/thumbs"
BIND_HOST = "0.0.0.0"
BIND_PORT = 8770

_lock = threading.Lock()
_orientation = {}  # pair_id -> (left_asset_id, right_asset_id)

SOURCE_PRIORITY = {
    "apple_zshot_disagree": 0,
    "apple_v1_disagree": 1,
    "apple_quantile_spread": 2,
    "random": 3,
}


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def pick_pair(conn):
    rows = conn.execute(
        """
        SELECT pair_id, asset_a, asset_b, source FROM pairs
        WHERE pair_id NOT IN (SELECT pair_id FROM judgments)
        """
    ).fetchall()
    if not rows:
        return None
    rows = sorted(rows, key=lambda r: (SOURCE_PRIORITY.get(r["source"], 9), random.random()))
    return rows[0]


PAGE_HTML = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>preference tool</title>
<style>
  html, body { height: 100%; margin: 0; background: #111; color: #eee; font-family: -apple-system, sans-serif; }
  #wrap { display: flex; flex-direction: column; align-items: center; justify-content: center; height: 100%; gap: 16px; }
  #pair { display: flex; gap: 24px; align-items: center; justify-content: center; }
  .side { display: flex; flex-direction: column; align-items: center; gap: 10px; }
  .thumb-box { width: 44vw; max-width: 640px; height: 60vh; display: flex; align-items: center; justify-content: center; background: #1c1c1c; border-radius: 8px; overflow: hidden; }
  img { max-width: 100%; max-height: 100%; object-fit: contain; }
  button { font-size: 16px; padding: 10px 18px; border-radius: 6px; border: 1px solid #444; background: #222; color: #eee; cursor: pointer; }
  button:hover { background: #333; }
  #skipRow { margin-top: 4px; }
  #counter { color: #888; font-size: 14px; }
  #hint { color: #666; font-size: 12px; }
  .placeholder { color: #555; font-size: 14px; }
</style>
</head>
<body>
<div id="wrap">
  <div id="pair">
    <div class="side">
      <div class="thumb-box" id="leftBox"><span class="placeholder">loading...</span></div>
      <button id="leftBtn">&#9664; Left better</button>
    </div>
    <div class="side">
      <div class="thumb-box" id="rightBox"><span class="placeholder">loading...</span></div>
      <button id="rightBtn">Right better &#9654;</button>
    </div>
  </div>
  <div id="skipRow"><button id="skipBtn">Skip &mdash; can't judge</button></div>
  <div id="counter">N judged: -</div>
  <div id="hint">&larr;/&rarr; to pick, space to skip</div>
</div>
<script>
let current = null;
let busy = false;

async function loadNext() {
  busy = true;
  document.getElementById('leftBox').innerHTML = '<span class="placeholder">loading...</span>';
  document.getElementById('rightBox').innerHTML = '<span class="placeholder">loading...</span>';
  const res = await fetch('/next_pair');
  const data = await res.json();
  current = data;
  if (!data.pair_id) {
    document.getElementById('leftBox').innerHTML = '<span class="placeholder">no pairs left</span>';
    document.getElementById('rightBox').innerHTML = '<span class="placeholder">no pairs left</span>';
    busy = false;
    return;
  }
  const l = new Image();
  l.src = data.left_thumb_url;
  l.onload = () => { document.getElementById('leftBox').innerHTML = ''; document.getElementById('leftBox').appendChild(l); };
  const r = new Image();
  r.src = data.right_thumb_url;
  r.onload = () => { document.getElementById('rightBox').innerHTML = ''; document.getElementById('rightBox').appendChild(r); };
  busy = false;
  refreshStats();
}

async function refreshStats() {
  const res = await fetch('/stats');
  const s = await res.json();
  document.getElementById('counter').textContent = `N judged: ${s.judged}  (skipped: ${s.skipped}, remaining: ${s.remaining})`;
}

async function choose(winner) {
  if (busy || !current || !current.pair_id) return;
  busy = true;
  await fetch('/choice', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({pair_id: current.pair_id, winner: winner})
  });
  loadNext();
}

document.getElementById('leftBtn').onclick = () => choose('left');
document.getElementById('rightBtn').onclick = () => choose('right');
document.getElementById('skipBtn').onclick = () => choose('skip');

window.addEventListener('keydown', (e) => {
  if (e.code === 'ArrowLeft') choose('left');
  else if (e.code === 'ArrowRight') choose('right');
  else if (e.code === 'Space') { e.preventDefault(); choose('skip'); }
});

loadNext();
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "PrefTool/1.0"

    def log_message(self, fmt, *args):
        pass

    def _send_json(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/":
            body = PAGE_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/next_pair":
            with _lock:
                conn = db()
                try:
                    row = pick_pair(conn)
                    if row is None:
                        self._send_json({"pair_id": None})
                        return
                    pair_id, a, b, source = row["pair_id"], row["asset_a"], row["asset_b"], row["source"]
                    if random.random() < 0.5:
                        left, right = a, b
                    else:
                        left, right = b, a
                    _orientation[pair_id] = (left, right)
                    self._send_json({
                        "pair_id": pair_id,
                        "left_thumb_url": f"/thumb/{left}",
                        "right_thumb_url": f"/thumb/{right}",
                    })
                finally:
                    conn.close()
            return

        if path == "/stats":
            conn = db()
            try:
                total = conn.execute("SELECT COUNT(*) c FROM pairs").fetchone()["c"]
                judged = conn.execute("SELECT COUNT(*) c FROM judgments WHERE skipped=0").fetchone()["c"]
                skipped = conn.execute("SELECT COUNT(*) c FROM judgments WHERE skipped=1").fetchone()["c"]
                done_pairs = conn.execute("SELECT COUNT(DISTINCT pair_id) c FROM judgments").fetchone()["c"]
                self._send_json({
                    "judged": judged,
                    "skipped": skipped,
                    "remaining": total - done_pairs,
                    "total": total,
                })
            finally:
                conn.close()
            return

        if path.startswith("/thumb/"):
            asset_id_str = path[len("/thumb/"):]
            if not asset_id_str.isdigit():
                self.send_response(400)
                self.end_headers()
                return
            fpath = os.path.join(THUMBS, f"{int(asset_id_str)}.jpg")
            if not os.path.isfile(fpath):
                self.send_response(404)
                self.end_headers()
                return
            with open(fpath, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
            return

        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path != "/choice":
            self.send_response(404)
            self.end_headers()
            return

        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            self._send_json({"error": "bad json"}, status=400)
            return

        pair_id = payload.get("pair_id")
        winner = payload.get("winner")
        if pair_id is None or winner not in ("left", "right", "skip"):
            self._send_json({"error": "bad payload"}, status=400)
            return

        with _lock:
            orient = _orientation.get(pair_id)
            conn = db()
            try:
                row = conn.execute(
                    "SELECT asset_a, asset_b FROM pairs WHERE pair_id=?", (pair_id,)
                ).fetchone()
                if row is None:
                    self._send_json({"error": "unknown pair_id"}, status=400)
                    return
                if orient is None:
                    # server restarted mid-serve; fall back to stored pair order
                    left, right = row["asset_a"], row["asset_b"]
                else:
                    left, right = orient

                ts = datetime.now(timezone.utc).isoformat()
                if winner == "skip":
                    conn.execute(
                        "INSERT INTO judgments(pair_id, winner_asset, loser_asset, skipped, shown_left_asset, ts_utc) "
                        "VALUES (?, NULL, NULL, 1, ?, ?)",
                        (pair_id, left, ts),
                    )
                else:
                    winner_asset = left if winner == "left" else right
                    loser_asset = right if winner == "left" else left
                    conn.execute(
                        "INSERT INTO judgments(pair_id, winner_asset, loser_asset, skipped, shown_left_asset, ts_utc) "
                        "VALUES (?, ?, ?, 0, ?, ?)",
                        (pair_id, winner_asset, loser_asset, left, ts),
                    )
                conn.commit()
                _orientation.pop(pair_id, None)
                self._send_json({"ok": True})
            finally:
                conn.close()


def main():
    server = ThreadingHTTPServer((BIND_HOST, BIND_PORT), Handler)
    print(f"preftool serving on {BIND_HOST}:{BIND_PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
