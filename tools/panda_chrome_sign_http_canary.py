#!/usr/bin/env python3
"""Controlled Panda canary: independent Chrome (browser-bridge) sign → HTTP Lite.

Collects a full telemetry dataset under OUT_DIR, then stops the bridge.

Architecture under test:
  - Sign: temporary grok2api-browser-bridge (Chrome146), not grok2api main process
  - Gen: curl_cffi → grok.com REST Lite (same calls future http_reverse would make)
  - grok2api main stays up; health sampled continuously

Hard abort: load1 >= 2.0 OR MemAvailableMiB < 180 → stop bridge immediately.
"""
from __future__ import annotations

import base64
import json
import os
import random
import re
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

try:
    from curl_cffi import requests as crequests
except ImportError:
    print("curl_cffi required", file=sys.stderr)
    raise SystemExit(2)

OUT_DIR = Path(os.environ.get("CANARY_OUT", f"/tmp/panda-chrome-sign-canary-{datetime.now().strftime('%Y%m%d-%H%M%S')}"))
DB = Path("/opt/grok2api/data/backend.db")
CONFIG = Path("/opt/grok2api/config.yaml")
UDEAL_LIST = Path("/tmp/udeal1000proxy.txt")
BRIDGE_NAME = "grok2api-browser-bridge"
BRIDGE_KEY_HOST = "/root/.secrets/grok2api-browser-bridge-key-main"
COMPOSE_DIR = Path("/opt/grok2api")
UDEAL_PASSWORD = "IM0aSd"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
)
CHAT_PATH = "/rest/app-chat/conversations/new"
LOAD_ABORT = 2.0
MEM_ABORT_MIB = 180.0
SAMPLE_HZ = 1.0

_stop_sample = threading.Event()
_abort_reason: str | None = None
_lock = threading.Lock()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def log(event: str, **kw) -> None:
    row = {"ts": utc_now(), "event": event, **kw}
    line = json.dumps(row, ensure_ascii=False)
    print(line, flush=True)
    with (OUT_DIR / "events.jsonl").open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def sh(cmd: list[str] | str, timeout: float = 60, check: bool = False) -> subprocess.CompletedProcess:
    if isinstance(cmd, str):
        return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=check)


def read_meminfo() -> dict:
    out: dict[str, int] = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].endswith(":"):
            out[parts[0][:-1]] = int(parts[1])  # kB
    return {
        "MemTotal_MiB": round(out.get("MemTotal", 0) / 1024, 1),
        "MemAvailable_MiB": round(out.get("MemAvailable", 0) / 1024, 1),
        "MemFree_MiB": round(out.get("MemFree", 0) / 1024, 1),
        "Cached_MiB": round(out.get("Cached", 0) / 1024, 1),
        "SwapTotal_MiB": round(out.get("SwapTotal", 0) / 1024, 1),
        "SwapFree_MiB": round(out.get("SwapFree", 0) / 1024, 1),
        "SwapUsed_MiB": round((out.get("SwapTotal", 0) - out.get("SwapFree", 0)) / 1024, 1),
    }


def read_load() -> dict:
    a, b, c, *_ = Path("/proc/loadavg").read_text().split()
    return {"load1": float(a), "load5": float(b), "load15": float(c), "nproc": os.cpu_count() or 2}


def chrome_rss_mib() -> dict:
    """Sum RSS of chrome/chromium-related processes (host + docker visible)."""
    proc = sh(["ps", "-eo", "pid,rss,comm,args"], timeout=15)
    total = 0
    procs: list[dict] = []
    for line in (proc.stdout or "").splitlines()[1:]:
        low = line.lower()
        if not any(x in low for x in ("chrome", "chromium", "chromedriver")):
            continue
        parts = line.split(None, 3)
        if len(parts) < 3:
            continue
        try:
            pid, rss_kb = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        rss_mib = round(rss_kb / 1024, 1)
        total += rss_mib
        if len(procs) < 40:
            procs.append({"pid": pid, "rss_mib": rss_mib, "comm": parts[2], "args": (parts[3] if len(parts) > 3 else "")[:120]})
    return {"chrome_rss_sum_mib": round(total, 1), "chrome_proc_count": len(procs), "chrome_top": sorted(procs, key=lambda x: -x["rss_mib"])[:12]}


def docker_stats_map() -> dict:
    proc = sh(
        ["docker", "stats", "--no-stream", "--format", "{{.Name}}\t{{.MemUsage}}\t{{.MemPerc}}\t{{.CPUPerc}}\t{{.PIDs}}"],
        timeout=20,
    )
    rows = {}
    for line in (proc.stdout or "").splitlines():
        parts = line.split("\t")
        if len(parts) >= 5:
            rows[parts[0]] = {
                "mem_usage": parts[1],
                "mem_perc": parts[2],
                "cpu_perc": parts[3],
                "pids": parts[4],
            }
    return rows


def healthz() -> dict:
    t0 = time.time()
    proc = sh(["curl", "-sS", "-m", "3", "http://127.0.0.1:18000/healthz"], timeout=8)
    return {
        "ok": (proc.stdout or "").strip() == '{"ok":true}' or '"ok":true' in (proc.stdout or ""),
        "body": (proc.stdout or "")[:200],
        "rc": proc.returncode,
        "latency_ms": round((time.time() - t0) * 1000, 1),
    }


def snapshot(phase: str) -> dict:
    snap = {
        "ts": utc_now(),
        "phase": phase,
        "mem": read_meminfo(),
        "load": read_load(),
        "chrome": chrome_rss_mib(),
        "docker": docker_stats_map(),
        "healthz": healthz(),
    }
    with (OUT_DIR / "snapshots.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(snap, ensure_ascii=False) + "\n")
    return snap


def sampler_loop() -> None:
    global _abort_reason
    while not _stop_sample.is_set():
        snap = snapshot("sample")
        load1 = snap["load"]["load1"]
        avail = snap["mem"]["MemAvailable_MiB"]
        if load1 >= LOAD_ABORT or avail < MEM_ABORT_MIB:
            with _lock:
                if _abort_reason is None:
                    _abort_reason = f"hard_abort load1={load1} MemAvailable_MiB={avail}"
            log("hard_abort_triggered", load1=load1, MemAvailable_MiB=avail)
            stop_bridge("hard_abort")
            break
        _stop_sample.wait(SAMPLE_HZ)


def load_encryption_key() -> bytes:
    for line in CONFIG.read_text(encoding="utf-8").splitlines():
        if "credentialEncryptionKey" in line:
            return base64.b64decode(line.split(":", 1)[1].strip().strip("\"'"))
    raise SystemExit("encryption key missing")


def decrypt_sso(account_id: int) -> str:
    key = load_encryption_key()
    aes = AESGCM(key)
    con = sqlite3.connect(str(DB))
    row = con.execute(
        "SELECT encrypted_primary FROM account_credentials WHERE account_id=?",
        (account_id,),
    ).fetchone()
    if not row:
        raise SystemExit(f"no credentials for {account_id}")
    raw = base64.b64decode(row[0])
    token = aes.decrypt(raw[:12], raw[12:], None).decode().strip()
    if token.lower().startswith("sso="):
        token = token[4:].strip()
    return token


def pick_free_account() -> int:
    con = sqlite3.connect(str(DB))
    ids = [
        int(r[0])
        for r in con.execute(
            """
            SELECT DISTINCT a.id
            FROM provider_accounts a
            JOIN account_quota_windows w ON w.account_id = a.id
            WHERE a.provider='grok_web' AND a.enabled=1 AND a.auth_status='active'
              AND ((w.mode='fast' AND w.total=30) OR (w.mode='auto' AND w.total IN (7,20)))
            ORDER BY a.id DESC
            LIMIT 12
            """
        )
    ]
    if not ids:
        raise SystemExit("no free-like web accounts")
    return ids[0]


def udeal_proxy(session: str) -> str:
    user = f"userId-2684-custom-8241-region-sg-session-{session}-sessTime-15"
    return f"http://{quote(user, safe='')}:{quote(UDEAL_PASSWORD, safe='')}@as.udealproxy.com:6666"


def sessions_from_udeal() -> list[str]:
    out: list[str] = []
    if not UDEAL_LIST.exists():
        return out
    for line in UDEAL_LIST.read_text(encoding="utf-8-sig", errors="ignore").splitlines():
        if "session=" not in line:
            continue
        s = (parse_qs(urlsplit(line.strip()).query).get("session") or [""])[0].strip()
        if s:
            out.append(s)
    return list(dict.fromkeys(out))


def probe_udeal(session: str) -> dict:
    proxy = udeal_proxy(session)
    t0 = time.time()
    try:
        r = crequests.get(
            "https://grok.com/",
            proxies={"http": proxy, "https": proxy},
            impersonate="chrome146",
            timeout=20,
            headers={"User-Agent": UA},
        )
        text = r.text or ""
        ok = r.status_code == 200 and "<title>Grok</title>" in text and "Just a moment" not in text
        return {
            "session": session,
            "http": r.status_code,
            "bytes": len(r.content or b""),
            "ok": ok,
            "elapsed_s": round(time.time() - t0, 2),
            "proxy": proxy,
        }
    except Exception as exc:
        return {"session": session, "http": 0, "ok": False, "error": f"{type(exc).__name__}:{exc}"[:200], "elapsed_s": round(time.time() - t0, 2), "proxy": proxy}


def pick_egress() -> dict:
    """Prefer curl pass_app; else any udeal that reaches CF challenge (Chrome can clear)."""
    known = Path("/tmp/udeal-pass-canary.json")
    tried: list[dict] = []
    candidates: list[dict] = []

    def consider(row: dict) -> dict | None:
        tried.append({k: v for k, v in row.items() if k != "proxy"})
        log("egress_try", **{k: v for k, v in row.items() if k != "proxy"})
        if row.get("ok"):
            return row
        # CF challenge HTML via working proxy — usable for browser sign
        if row.get("http") in (403, 503) and int(row.get("bytes") or 0) > 1000:
            row = dict(row)
            row["ok_mode"] = "cf_challenge_reachable"
            return row
        return None

    if known.exists():
        try:
            prev = json.loads(known.read_text(encoding="utf-8"))
            sess = prev.get("session")
            if sess:
                hit = consider(probe_udeal(sess))
                if hit:
                    (OUT_DIR / "egress_tries.json").write_text(json.dumps(tried, ensure_ascii=False, indent=2), encoding="utf-8")
                    return hit
        except Exception as exc:
            log("egress_known_fail", error=str(exc)[:200])

    sessions = sessions_from_udeal()
    random.shuffle(sessions)
    for sess in sessions[:30]:
        hit = consider(probe_udeal(sess))
        if hit:
            (OUT_DIR / "egress_tries.json").write_text(json.dumps(tried, ensure_ascii=False, indent=2), encoding="utf-8")
            return hit

    (OUT_DIR / "egress_tries.json").write_text(json.dumps(tried, ensure_ascii=False, indent=2), encoding="utf-8")
    if candidates:
        pick = candidates[0]
        pick["ok_mode"] = "cf_challenge_reachable"
        log("egress_fallback_cf_challenge", session=pick.get("session"), http=pick.get("http"))
        return pick
    raise SystemExit("no udeal egress (even CF challenge)")


def start_bridge() -> dict:
    override = Path("/tmp/bridge-sign-canary.override.yml")
    override.write_text(
        """
services:
  browser-bridge:
    image: grok2api-browser-bridge:chrome146
    mem_limit: 1536m
    cpus: "1.0"
    environment:
      BRIDGE_BOOTSTRAP_SECONDS: "90"
      BRIDGE_MAX_OPERATION_SECONDS: "120"
      BRIDGE_SESSION_LIMIT: "1"
      BRIDGE_REUSE_SESSIONS: "true"
      BRIDGE_SESSION_TTL_SECONDS: "600"
    volumes:
      - type: bind
        source: /root/.secrets/grok2api-browser-bridge-key-main
        target: /run/secrets/browser-bridge-key
        read_only: true
""".lstrip(),
        encoding="utf-8",
    )
    # Ensure compose uses the secret path from .env; override remounts key file.
    env = os.environ.copy()
    env["GROK2API_BROWSER_BRIDGE_KEY_FILE"] = BRIDGE_KEY_HOST
    t0 = time.time()
    proc = subprocess.run(
        ["docker", "compose", "-f", "docker-compose.yml", "-f", str(override), "up", "-d", "browser-bridge"],
        cwd=str(COMPOSE_DIR),
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )
    ready = False
    last = ""
    for _ in range(40):
        time.sleep(1.5)
        h = sh(
            ["docker", "exec", BRIDGE_NAME, "python3", "-c", "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8192/healthz', timeout=3).read().decode())"],
            timeout=10,
        )
        last = (h.stdout or h.stderr or "")[:300]
        if "ok" in last.lower() or '"status"' in last:
            ready = True
            break
    return {
        "elapsed_s": round(time.time() - t0, 2),
        "up_rc": proc.returncode,
        "up_stdout": (proc.stdout or "")[-500:],
        "up_stderr": (proc.stderr or "")[-500:],
        "ready": ready,
        "health_tail": last,
    }


def stop_bridge(reason: str) -> dict:
    log("bridge_stop_begin", reason=reason)
    t0 = time.time()
    proc = subprocess.run(
        ["docker", "compose", "stop", "browser-bridge"],
        cwd=str(COMPOSE_DIR),
        capture_output=True,
        text=True,
        timeout=90,
    )
    # Extra kill chrome leftovers if any on host (bridge is containerized; usually none)
    out = {
        "reason": reason,
        "elapsed_s": round(time.time() - t0, 2),
        "rc": proc.returncode,
        "stdout": (proc.stdout or "")[-400:],
        "stderr": (proc.stderr or "")[-400:],
    }
    log("bridge_stop_done", **out)
    return out


def bridge_key() -> str:
    return Path(BRIDGE_KEY_HOST).read_text(encoding="utf-8").strip()


def dump_session_cookies(session_key: str) -> str | None:
    """Pull Cookie jar from reused bridge Selenium session (needed for cf_clearance)."""
    inner = r"""
import json,sys
import app as bridge
key=sys.argv[1]
sess=bridge.SESSIONS.get(key)
if not sess:
  print(json.dumps({"error":"session_missing","keys":list(bridge.SESSIONS.keys())[:20]}))
  raise SystemExit(0)
try:
  cookies=sess.driver.get_cookies() or []
except Exception as e:
  print(json.dumps({"error":type(e).__name__+":"+str(e)[:200]}))
  raise SystemExit(0)
parts=[]
names=[]
for c in cookies:
  n=c.get("name") or ""
  v=c.get("value") or ""
  if n and v:
    parts.append(f"{n}={v}")
    names.append(n)
print(json.dumps({"cookie":"; ".join(parts),"names":names,"count":len(names),"has_cf":"cf_clearance" in names}))
"""
    proc = subprocess.run(
        ["docker", "exec", BRIDGE_NAME, "python3", "-c", inner, session_key],
        capture_output=True,
        timeout=30,
    )
    raw = (proc.stdout or b"").decode("utf-8", "replace").strip()
    try:
        data = json.loads(raw.splitlines()[-1] if raw else "{}")
    except Exception:
        log("cookie_dump_fail", stdout=raw[:300], stderr=(proc.stderr or b"").decode()[:300])
        return None
    log("cookie_dump", has_cf=data.get("has_cf"), count=data.get("count"), names=data.get("names"))
    (OUT_DIR / "cookie_dump_meta.json").write_text(
        json.dumps({k: v for k, v in data.items() if k != "cookie"}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return data.get("cookie") if data.get("has_cf") else data.get("cookie")


def call_sign(sso: str, proxy: str) -> dict:
    session_key = f"canary-sign-{uuid.uuid4().hex[:8]}"
    payload = {
        "path": "/rest/modes",
        "method": "GET",
        "cookie": f"sso={sso}; sso-rw={sso}",
        "userAgent": UA,
        "referer": "https://grok.com/",
        "sessionKey": session_key,
        "timeoutMs": 120000,
        "proxyUrl": proxy,
    }
    inner = r"""
import json,sys,urllib.error,urllib.request
payload=json.load(sys.stdin)
key=open('/run/secrets/browser-bridge-key','rb').read().strip().decode()
body=json.dumps(payload).encode()
req=urllib.request.Request(
  'http://127.0.0.1:8192/v1/sign', data=body,
  headers={'Authorization':'Bearer '+key,'Content-Type':'application/json'}, method='POST')
try:
  with urllib.request.urlopen(req, timeout=150) as resp:
    print(json.dumps({'http':resp.status,'body':json.loads(resp.read().decode())}))
except urllib.error.HTTPError as e:
  err=e.read().decode('utf-8','replace')
  try: parsed=json.loads(err)
  except Exception: parsed=None
  print(json.dumps({'http':e.code,'error':str(e),'body':parsed,'body_text':err[:800]}))
except Exception as e:
  print(json.dumps({'http':0,'error':type(e).__name__+': '+str(e)[:400]}))
"""
    t0 = time.time()
    proc = subprocess.run(
        ["docker", "exec", "-i", BRIDGE_NAME, "python3", "-c", inner],
        input=json.dumps(payload).encode(),
        capture_output=True,
        timeout=180,
    )
    elapsed = round(time.time() - t0, 2)
    stdout = (proc.stdout or b"").decode("utf-8", "replace").strip()
    stderr = (proc.stderr or b"").decode("utf-8", "replace").strip()
    data: dict = {"http": 0, "error": "empty_stdout", "elapsed_s": elapsed, "rc": proc.returncode}
    lines = [ln for ln in stdout.splitlines() if ln.startswith("{")]
    if lines:
        data = json.loads(lines[-1])
        data["elapsed_s"] = elapsed
        data["rc"] = proc.returncode
    if stderr:
        data["stderr_tail"] = stderr[-600:]
    # redact secrets in saved body
    body = data.get("body") if isinstance(data.get("body"), dict) else {}
    safe_body = {
        "has_statsig": bool(body.get("statsigId")),
        "statsig_len": len(body.get("statsigId") or ""),
        "statsig_prefix": (body.get("statsigId") or "")[:12],
        "error": (body.get("error") or "")[:300],
        "attempts": body.get("attempts"),
        "keys": sorted(body.keys()) if body else [],
    }
    data["body_safe"] = safe_body
    data["statsigId"] = body.get("statsigId")
    data["sessionKey"] = session_key
    data["cookie"] = body.get("cookie") or body.get("cookies") or body.get("cookieHeader")
    if not data.get("cookie"):
        data["cookie"] = dump_session_cookies(session_key)
    data["has_cf"] = bool(data.get("cookie") and "cf_clearance=" in str(data.get("cookie")))
    return data


def looks_bad_sig(sig: str) -> bool:
    if not sig or len(sig) < 20:
        return True
    if sig.startswith("eDA6") or sig.startswith("x0:"):
        return True
    return False


def http_phase(name: str, proxy: str, sso: str, statsig: str | None, cookie_header: str | None, payload: dict | None = None, method: str = "GET", path: str = "/rest/modes") -> dict:
    url = "https://grok.com" + path
    headers = {
        "User-Agent": UA,
        "Accept": "application/json",
        "Origin": "https://grok.com",
        "Referer": "https://grok.com/",
    }
    if statsig:
        headers["x-statsig-id"] = statsig
    # cookie: prefer bridge jar; always include sso
    jar = cookie_header or ""
    if "sso=" not in jar:
        jar = (jar + "; " if jar else "") + f"sso={sso}; sso-rw={sso}"
    headers["Cookie"] = jar
    t0 = time.time()
    try:
        if method == "GET":
            r = crequests.get(url, headers=headers, proxies={"http": proxy, "https": proxy}, impersonate="chrome146", timeout=60)
        else:
            headers["Content-Type"] = "application/json"
            r = crequests.post(url, headers=headers, json=payload or {}, proxies={"http": proxy, "https": proxy}, impersonate="chrome146", timeout=120)
        text = r.text or ""
        anti = "anti_bot" in text or "Just a moment" in text
        out = {
            "name": name,
            "path": path,
            "method": method,
            "http": r.status_code,
            "elapsed_s": round(time.time() - t0, 2),
            "bytes": len(r.content or b""),
            "anti_bot": anti,
            "content_type": r.headers.get("content-type", ""),
            "body_head": text[:400],
        }
        # extract image urls for lite
        urls = re.findall(r"https://assets\.grok\.com/[^\s\"'\\]+", text)
        out["asset_urls"] = list(dict.fromkeys(urls))[:8]
        return out
    except Exception as exc:
        return {"name": name, "path": path, "method": method, "http": 0, "elapsed_s": round(time.time() - t0, 2), "error": f"{type(exc).__name__}:{exc}"[:400]}


def download_assets(urls: list[str], proxy: str, cookie: str, sso: str) -> list[dict]:
    results = []
    img_dir = OUT_DIR / "images"
    img_dir.mkdir(parents=True, exist_ok=True)
    jar = cookie if cookie and "sso=" in cookie else f"sso={sso}; sso-rw={sso}"
    for i, u in enumerate(urls[:4]):
        t0 = time.time()
        try:
            r = crequests.get(
                u,
                headers={"User-Agent": UA, "Cookie": jar, "Referer": "https://grok.com/"},
                proxies={"http": proxy, "https": proxy},
                impersonate="chrome146",
                timeout=60,
            )
            path = img_dir / f"asset_{i}.bin"
            path.write_bytes(r.content or b"")
            results.append({"url_tail": u[-60:], "http": r.status_code, "bytes": len(r.content or b""), "elapsed_s": round(time.time() - t0, 2), "file": str(path)})
        except Exception as exc:
            results.append({"url_tail": u[-60:], "error": f"{type(exc).__name__}:{exc}"[:200], "elapsed_s": round(time.time() - t0, 2)})
    return results


def summarize_samples() -> dict:
    path = OUT_DIR / "snapshots.jsonl"
    if not path.exists():
        return {}
    loads, avails, chrome, bridge_mem, api_mem, health_ok = [], [], [], [], [], []
    for line in path.read_text(encoding="utf-8").splitlines():
        s = json.loads(line)
        loads.append(s["load"]["load1"])
        avails.append(s["mem"]["MemAvailable_MiB"])
        chrome.append(s["chrome"]["chrome_rss_sum_mib"])
        d = s.get("docker") or {}
        if BRIDGE_NAME in d:
            # parse "xxxMiB / yyy"
            m = re.search(r"([\d.]+)(MiB|GiB)", d[BRIDGE_NAME]["mem_usage"])
            if m:
                val = float(m.group(1)) * (1024 if m.group(2) == "GiB" else 1)
                bridge_mem.append(val)
        if "grok2api" in d:
            m = re.search(r"([\d.]+)(MiB|GiB)", d["grok2api"]["mem_usage"])
            if m:
                val = float(m.group(1)) * (1024 if m.group(2) == "GiB" else 1)
                api_mem.append(val)
        health_ok.append(1 if s.get("healthz", {}).get("ok") else 0)

    def agg(xs: list[float]) -> dict:
        if not xs:
            return {}
        return {"min": round(min(xs), 2), "max": round(max(xs), 2), "avg": round(sum(xs) / len(xs), 2), "n": len(xs)}

    return {
        "load1": agg(loads),
        "MemAvailable_MiB": agg(avails),
        "chrome_rss_sum_mib": agg(chrome),
        "bridge_mem_mib": agg(bridge_mem),
        "grok2api_mem_mib": agg(api_mem),
        "healthz_ok_ratio": round(sum(health_ok) / len(health_ok), 3) if health_ok else None,
    }


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    t_all = time.time()
    result: dict = {
        "started_at": utc_now(),
        "host": sh(["hostname"]).stdout.strip(),
        "out_dir": str(OUT_DIR),
        "architecture": {
            "sign": "independent browser-bridge chrome146 /v1/sign",
            "gen": "host curl_cffi chrome146 → grok REST Lite (stand-in for grok2api http_reverse)",
            "note": "grok2api http_reverse switch not shipped; Lite uses same upstream API",
        },
        "gates": {"load1_abort": LOAD_ABORT, "mem_available_abort_mib": MEM_ABORT_MIB},
    }

    log("canary_start", out=str(OUT_DIR))
    result["baseline"] = snapshot("baseline")

    sampler = threading.Thread(target=sampler_loop, name="sampler", daemon=True)
    sampler.start()

    account_id = pick_free_account()
    sso = decrypt_sso(account_id)
    result["account_id"] = account_id
    result["sso_len"] = len(sso)
    log("account", id=account_id, sso_len=len(sso))

    try:
        egress = pick_egress()
    except SystemExit as exc:
        result["error"] = str(exc)
        log("egress_failed", error=str(exc))
        _stop_sample.set()
        result["summary_samples"] = summarize_samples()
        result["finished_at"] = utc_now()
        result["total_s"] = round(time.time() - t_all, 2)
        (OUT_DIR / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return 1

    result["egress"] = {k: v for k, v in egress.items() if k != "proxy"}
    proxy = egress["proxy"]
    log("egress_ok", session=egress.get("session"), http=egress.get("http"), bytes=egress.get("bytes"))

    up = start_bridge()
    result["bridge_start"] = up
    log("bridge_start", **{k: v for k, v in up.items() if k != "up_stdout"})
    result["after_bridge_start"] = snapshot("after_bridge_start")
    if not up.get("ready"):
        result["error"] = "bridge_not_ready"
        stop_bridge("not_ready")
        _stop_sample.set()
        result["summary_samples"] = summarize_samples()
        result["finished_at"] = utc_now()
        result["total_s"] = round(time.time() - t_all, 2)
        (OUT_DIR / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return 2

    if _abort_reason:
        result["error"] = _abort_reason
        _stop_sample.set()
        result["summary_samples"] = summarize_samples()
        (OUT_DIR / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return 3

    sign = call_sign(sso, proxy)
    result["sign"] = {
        "http": sign.get("http"),
        "elapsed_s": sign.get("elapsed_s"),
        "body_safe": sign.get("body_safe"),
        "has_cookie": bool(sign.get("cookie")),
        "error": sign.get("error"),
    }
    log("sign_done", **result["sign"])
    result["after_sign"] = snapshot("after_sign")

    statsig = sign.get("statsigId")
    cookie = sign.get("cookie") if isinstance(sign.get("cookie"), str) else None
    # some bridge versions return cookie in body fields
    body = sign.get("body") if isinstance(sign.get("body"), dict) else {}
    if not cookie:
        for k in ("cookie", "cookieHeader", "cookies"):
            if isinstance(body.get(k), str) and body.get(k):
                cookie = body[k]
                break

    phases = []
    if statsig and not looks_bad_sig(statsig):
        phases.append(http_phase("modes_probe", proxy, sso, statsig, cookie, method="GET", path="/rest/modes"))
        phases.append(
            http_phase(
                "rate_limits",
                proxy,
                sso,
                statsig,
                cookie,
                method="POST",
                path="/rest/rate-limits",
                payload={"modelName": "fast"},
            )
        )
        phases.append(
            http_phase(
                "chat_pong",
                proxy,
                sso,
                statsig,
                cookie,
                method="POST",
                path=CHAT_PATH,
                payload={
                    "temporary": True,
                    "modelName": "grok-3",
                    "message": "PONG",
                    "fileAttachments": [],
                    "imageAttachments": [],
                    "disableSearch": True,
                    "enableImageGeneration": False,
                    "returnImageBytes": False,
                    "returnRawGrokInXaiRequest": False,
                    "enableImageStreaming": False,
                    "enableSideBySide": False,
                    "sendFinalMetadata": True,
                    "toolOverrides": {},
                    "isPreset": False,
                    "disableTextFollowUps": True,
                },
            )
        )
        lite_payload = {
            "temporary": True,
            "modelName": "grok-3",
            "message": "Drawing: a simple orange cat, realistic, clear details",
            "fileAttachments": [],
            "imageAttachments": [],
            "disableSearch": True,
            "enableImageGeneration": True,
            "returnImageBytes": False,
            "returnRawGrokInXaiRequest": False,
            "enableImageStreaming": True,
            "enableSideBySide": False,
            "sendFinalMetadata": True,
            "toolOverrides": {},
            "isPreset": False,
            "disableTextFollowUps": True,
            "modeId": "fast",
        }
        lite = http_phase("lite", proxy, sso, statsig, cookie, method="POST", path=CHAT_PATH, payload=lite_payload)
        phases.append(lite)
        if lite.get("asset_urls"):
            result["downloads"] = download_assets(lite["asset_urls"], proxy, cookie or "", sso)
        result["http_phases"] = [{k: v for k, v in p.items() if k != "body_head"} | {"body_head": (p.get("body_head") or "")[:200]} for p in phases]
        for p in phases:
            log("http_phase", name=p.get("name"), http=p.get("http"), elapsed_s=p.get("elapsed_s"), anti_bot=p.get("anti_bot"), assets=len(p.get("asset_urls") or []))
    else:
        result["error"] = result.get("error") or "bad_or_missing_statsig"
        log("sign_unusable", body_safe=sign.get("body_safe"), error=sign.get("error"))

    result["after_http"] = snapshot("after_http")

    # Bridge logs tail
    logs = sh(["docker", "logs", BRIDGE_NAME, "--tail", "40"], timeout=20)
    (OUT_DIR / "bridge_logs_tail.txt").write_text((logs.stdout or "") + "\n" + (logs.stderr or ""), encoding="utf-8")

    stop_bridge("canary_complete")
    _stop_sample.set()
    sampler.join(timeout=5)
    time.sleep(2)
    result["after_stop"] = snapshot("after_stop")
    result["abort_reason"] = _abort_reason
    result["summary_samples"] = summarize_samples()
    result["finished_at"] = utc_now()
    result["total_s"] = round(time.time() - t_all, 2)

    # success criteria
    lite_ok = any(p.get("name") == "lite" and p.get("http") == 200 and not p.get("anti_bot") for p in result.get("http_phases") or [])
    result["verdict"] = {
        "bridge_ready": bool(up.get("ready")),
        "sign_ok": bool(statsig) and not looks_bad_sig(statsig or ""),
        "lite_ok": lite_ok,
        "healthz_held": (result["summary_samples"] or {}).get("healthz_ok_ratio") == 1.0,
        "hard_aborted": bool(_abort_reason),
        "pass": bool(up.get("ready")) and bool(statsig) and not looks_bad_sig(statsig or "") and lite_ok and not _abort_reason,
    }

    (OUT_DIR / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    log("canary_done", verdict=result["verdict"], total_s=result["total_s"], summary=result["summary_samples"])
    return 0 if result["verdict"]["pass"] else 4


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        stop_bridge("keyboard")
        raise
    except Exception as exc:
        log("fatal", error=f"{type(exc).__name__}:{exc}"[:500])
        try:
            stop_bridge("fatal")
        except Exception:
            pass
        raise
