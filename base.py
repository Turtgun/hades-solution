"""Base station (Arduino UNO Q): keeps the score and serves the dashboard.

Listens to the same UDP messages helmet_game.py and the weapons already
broadcast on port 4210, so nothing has to be sent to the UNO Q specially:

    shot <shooter> <hit 0/1> <n>     from a weapon   -> score
    hello <player> <device>          heartbeat       -> online lights
    cue <player> U|D|C|N <0/1>       from a helmet   -> helmet online, ON TARGET
    aim <player> <0/1>               from a weapon   -> weapon online

Run it directly on the board's Linux side (not inside App Lab):

    python3 base.py

Then open http://10.42.0.1:8000 from any device on the BowGame Wi-Fi.
Uses only the Python standard library.
"""

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

GAME_PORT = 4210    # must match GAME_PORT in helmet_game.py
WEB_PORT = 8000     # the dashboard
ONLINE_WINDOW = 6   # seconds without a message before a device shows offline
LOCK_WINDOW = 0.5   # seconds an "on target" cue stays lit

lock = threading.Lock()


def new_player():
    return {"hits": 0, "misses": 0, "last_shot": (None, 0.0),
            "weapon_seen": 0.0, "helmet_seen": 0.0, "locked_until": 0.0}


players = {1: new_player(), 2: new_player()}


def handle_packet(text):
    parts = text.split()
    if len(parts) < 2:
        return
    try:
        who = int(parts[1])
    except ValueError:
        return
    if who not in players:
        return
    kind = parts[0]
    now = time.time()
    with lock:
        p = players[who]
        if kind == "shot" and len(parts) >= 4:
            p["weapon_seen"] = now
            hit, n = parts[2] == "1", parts[3]
            last_n, last_t = p["last_shot"]
            if n == last_n and now - last_t < 1.0:
                return  # same shot, sent more than once (same rule as helmet_game.py)
            p["last_shot"] = (n, now)
            p["hits" if hit else "misses"] += 1
            print(f"Player {who}: {'HIT' if hit else 'miss'}  (hits {p['hits']}, misses {p['misses']})")
        elif kind == "hello" and len(parts) >= 3:
            p["helmet_seen" if parts[2] == "helmet" else "weapon_seen"] = now
        elif kind == "cue" and len(parts) >= 4:
            p["helmet_seen"] = now
            p["locked_until"] = now + LOCK_WINDOW if parts[3] == "1" else 0.0
        elif kind == "pos":
            p["helmet_seen"] = now
        elif kind == "aim":
            p["weapon_seen"] = now


def udp_listener():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.bind(("", GAME_PORT))
    while True:
        data, _ = sock.recvfrom(256)
        handle_packet(data.decode("ascii", "ignore"))


def snapshot():
    now = time.time()
    with lock:
        return {
            f"p{who}": {
                "hits": p["hits"],
                "misses": p["misses"],
                "weapon": now - p["weapon_seen"] < ONLINE_WINDOW,
                "helmet": now - p["helmet_seen"] < ONLINE_WINDOW,
                "locked": now < p["locked_until"],
            }
            for who, p in players.items()
        }


def reset_scores():
    with lock:
        for p in players.values():
            p["hits"] = 0
            p["misses"] = 0
    print("Scores reset")


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Blind Archer Duel</title>
<style>
  :root { --bg:#10141a; --panel:#1a212b; --line:#2b3644; --text:#eef2f6; --dim:#93a0b1; --ok:#5bd68a; --off:#5d6979; }
  * { box-sizing:border-box; }
  body { margin:0; min-height:100vh; display:flex; flex-direction:column; background:var(--bg); color:var(--text);
         font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif; }
  header { display:flex; justify-content:space-between; align-items:center; padding:16px 24px; }
  h1 { margin:0; font-size:16px; font-weight:600; letter-spacing:.14em; text-transform:uppercase; color:var(--dim); }
  button { font:inherit; font-size:14px; color:var(--text); background:transparent; border:1px solid var(--line);
           border-radius:8px; padding:8px 16px; cursor:pointer; }
  button:hover { border-color:var(--dim); }
  button.armed { border-color:#ff7a59; color:#ff7a59; }
  #lost { display:none; margin:0 24px 12px; padding:10px 14px; border:1px solid #ff7a59; border-radius:8px; color:#ff7a59; }
  main { flex:1; display:grid; grid-template-columns:1fr 1fr; gap:16px; padding:0 24px 24px; }
  .player { --c:#ff7a59; background:var(--panel); border:1px solid var(--line); border-top:6px solid var(--c);
            border-radius:12px; display:flex; flex-direction:column; align-items:center; justify-content:center; padding:24px; }
  .player.p2 { --c:#4cc2ff; }
  .name { font-size:clamp(20px,3vw,36px); font-weight:700; color:var(--c); }
  .weapon { color:var(--dim); font-size:clamp(13px,1.4vw,18px); letter-spacing:.1em; text-transform:uppercase; }
  .target { margin-top:12px; padding:4px 14px; border:2px solid var(--c); border-radius:999px; color:var(--c);
            font-weight:700; letter-spacing:.12em; font-size:clamp(12px,1.4vw,18px); visibility:hidden; }
  .target.show { visibility:visible; }
  .score { font-size:clamp(96px,22vw,300px); font-weight:800; line-height:1; font-variant-numeric:tabular-nums; }
  .score.flash { animation:flash .6s ease-out; }
  @keyframes flash { from { transform:scale(1.18); color:var(--c); } to { transform:scale(1); } }
  .unit { color:var(--dim); font-size:clamp(14px,1.6vw,20px); letter-spacing:.1em; text-transform:uppercase; }
  .stats { margin-top:20px; font-size:clamp(16px,2vw,26px); color:var(--dim); font-variant-numeric:tabular-nums; }
  .links { margin-top:16px; display:flex; gap:20px; font-size:14px; color:var(--dim); }
  .dot { display:inline-block; width:10px; height:10px; border-radius:50%; background:var(--off); margin-right:6px; }
  .on .dot { background:var(--ok); }
  .on { color:var(--text); }
  @media (max-width:640px) { main { grid-template-columns:1fr; } }
  @media (prefers-reduced-motion:reduce) { .score.flash { animation:none; } }
</style>
</head>
<body>
<header>
  <h1>Blind Archer Duel</h1>
  <button id="reset">Reset scores</button>
</header>
<div id="lost">Lost contact with the base station.</div>
<main>
  <section class="player p1" id="p1">
    <div class="name">Player 1</div>
    <div class="weapon">Bow</div>
    <div class="target">On target</div>
    <div class="score">0</div>
    <div class="unit">hits</div>
    <div class="stats"></div>
    <div class="links"><span class="w"><i class="dot"></i><span></span></span><span class="h"><i class="dot"></i><span></span></span></div>
  </section>
  <section class="player p2" id="p2">
    <div class="name">Player 2</div>
    <div class="weapon">Crossbow</div>
    <div class="target">On target</div>
    <div class="score">0</div>
    <div class="unit">hits</div>
    <div class="stats"></div>
    <div class="links"><span class="w"><i class="dot"></i><span></span></span><span class="h"><i class="dot"></i><span></span></span></div>
  </section>
</main>
<script>
  function link(el, label, on) {
    el.classList.toggle('on', on);
    el.querySelector('span').textContent = label + (on ? ' online' : ' offline');
  }
  function render(state) {
    for (const who of ['p1', 'p2']) {
      const p = state[who], box = document.getElementById(who);
      const score = box.querySelector('.score');
      if (score.textContent !== String(p.hits)) {
        score.textContent = p.hits;
        score.classList.remove('flash'); void score.offsetWidth; score.classList.add('flash');
      }
      const shots = p.hits + p.misses;
      const acc = shots ? Math.round(100 * p.hits / shots) + '% accuracy' : 'no shots yet';
      box.querySelector('.stats').textContent = p.misses + ' missed \\u00b7 ' + acc;
      box.querySelector('.target').classList.toggle('show', p.locked);
      link(box.querySelector('.w'), 'Weapon', p.weapon);
      link(box.querySelector('.h'), 'Helmet', p.helmet);
    }
  }
  async function poll() {
    try {
      render(await (await fetch('/state', { cache: 'no-store' })).json());
      document.getElementById('lost').style.display = 'none';
    } catch (e) {
      document.getElementById('lost').style.display = 'block';
    }
  }
  setInterval(poll, 200); poll();

  const reset = document.getElementById('reset');
  let armed = null;
  reset.onclick = async () => {
    if (!armed) {
      reset.textContent = 'Click again to reset'; reset.classList.add('armed');
      armed = setTimeout(disarm, 3000);
      return;
    }
    disarm();
    await fetch('/reset', { method: 'POST' });
    poll();
  };
  function disarm() { clearTimeout(armed); armed = null; reset.textContent = 'Reset scores'; reset.classList.remove('armed'); }
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def reply(self, code, content_type, body):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/":
            self.reply(200, "text/html; charset=utf-8", PAGE.encode())
        elif self.path == "/state":
            self.reply(200, "application/json", json.dumps(snapshot()).encode())
        else:
            self.reply(404, "text/plain", b"not found")

    def do_POST(self):
        if self.path == "/reset":
            reset_scores()
            self.reply(200, "application/json", b"{}")
        else:
            self.reply(404, "text/plain", b"not found")

    def log_message(self, *args):
        pass  # keep the console for game events only


if __name__ == "__main__":
    threading.Thread(target=udp_listener, daemon=True).start()
    print(f"Listening for game messages on UDP {GAME_PORT}; dashboard on http://10.42.0.1:{WEB_PORT}")
    ThreadingHTTPServer(("", WEB_PORT), Handler).serve_forever()
