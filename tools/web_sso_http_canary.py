#!/usr/bin/env python3
"""Sign-then-HTTP canary: no-sig vs bridge-sign vs local curl_cffi chat via Mihomo.

Contrasts:
  A) pure HTTP + SSO, no x-statsig-id  → expect anti-bot 403
  B) panda browser-bridge POST /v1/sign → obtain statsigId
  C) pure HTTP + SSO + statsigId       → expect chat 200 with token/text

Secrets (SSO, bridge key, full statsig) are never printed; results go to .tmp/.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from curl_cffi import requests

PROXY = {"http": "http://127.0.0.1:7897", "https": "http://127.0.0.1:7897"}
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
)
IMPERSONATE = "chrome131"
CHAT_PATH = "/rest/app-chat/conversations/new"
SSH_HOST = os.environ.get("PANDA_SSH_HOST", "panda")
BRIDGE_KEY_FILE = "/root/.secrets/grok2api-browser-bridge-key"
ROOT = Path(__file__).resolve().parents[1]
TMP = ROOT / ".tmp"


def cookie(sso: str) -> str:
    token = sso.strip()
    if token.lower().startswith("sso="):
        token = token[4:].strip()
    if ";" in token:
        token = token.split(";", 1)[0].strip()
    return f"sso={token}; sso-rw={token}"


def chat_payload() -> dict:
    # Align with backend buildWebChatPayload(..., modeId=fast)
    return {
        "collectionIds": [],
        "disabledConnectorIds": [],
        "deviceEnvInfo": {
            "darkModeEnabled": False,
            "devicePixelRatio": 2,
            "screenHeight": 1328,
            "screenWidth": 2056,
            "viewportHeight": 1083,
            "viewportWidth": 2056,
        },
        "disableMemory": True,
        "disableSearch": False,
        "disableSelfHarmShortCircuit": False,
        "disableTextFollowUps": False,
        "enableImageGeneration": True,
        "enableImageStreaming": True,
        "enableSideBySide": True,
        "fileAttachments": [],
        "forceConcise": False,
        "forceSideBySide": False,
        "imageAttachments": [],
        "imageGenerationCount": 2,
        "isAsyncChat": False,
        "message": "Reply with exactly: PONG",
        "modeId": "fast",
        "responseMetadata": {},
        "returnImageBytes": False,
        "returnRawGrokInXaiRequest": False,
        "sendFinalMetadata": True,
        "temporary": True,
    }


def base_headers(sso: str, user_agent: str = UA) -> dict[str, str]:
    return {
        "accept": "*/*",
        "content-type": "application/json",
        "origin": "https://grok.com",
        "referer": "https://grok.com/",
        "user-agent": user_agent,
        "cookie": cookie(sso),
        "cache-control": "no-cache",
        "pragma": "no-cache",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
    }


def classify_body(status: int, body: str, cf: str | None) -> str:
    lower = body.lower()
    if cf or "just a moment" in lower or "cloudflare" in lower and "challenge" in lower:
        return "cf_challenge"
    if status == 403 and "anti-bot" in lower:
        return "anti_bot_403"
    if status == 401:
        return "auth_401"
    if status == 200 and (("PONG" in body) or ("token" in lower) or ('"message"' in lower)):
        return "chat_ok"
    if status == 200:
        return "http_200_unclear"
    return f"http_{status}"


def post_chat(sess: requests.Session, sso: str, statsig: str | None, user_agent: str = UA) -> dict:
    headers = base_headers(sso, user_agent=user_agent)
    if statsig:
        headers["x-statsig-id"] = statsig
    t0 = time.time()
    r = sess.post(
        "https://grok.com" + CHAT_PATH,
        headers=headers,
        json=chat_payload(),
        timeout=90,
        stream=True,
    )
    elapsed = round(time.time() - t0, 2)
    chunks: list[bytes] = []
    total = 0
    for chunk in r.iter_content(chunk_size=4096):
        if not chunk:
            continue
        chunks.append(chunk)
        total += len(chunk)
        if total >= 12000:
            break
    body = b"".join(chunks).decode("utf-8", "replace")
    kind = classify_body(r.status_code, body, r.headers.get("cf-mitigated"))
    return {
        "http": r.status_code,
        "cf": r.headers.get("cf-mitigated"),
        "elapsed_s": elapsed,
        "has_statsig": bool(statsig),
        "statsig_len": len(statsig) if statsig else 0,
        "kind": kind,
        "has_token_hint": ("token" in body.lower()) or ("PONG" in body),
        "body_prefix": body[:280].replace("\n", " "),
    }


def ssh_run(remote: str, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["ssh", SSH_HOST, remote],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def export_sso(limit: int = 2) -> list[dict]:
    """Export Web SSO from panda (script lives in repo; scp then run)."""
    remote_script = "/tmp/panda_export_web_sso.py"
    local = ROOT / "tools" / "panda_export_web_sso.py"
    scp = subprocess.run(
        ["scp", str(local), f"{SSH_HOST}:{remote_script}"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if scp.returncode != 0:
        raise RuntimeError(f"scp export script failed: {scp.stderr[:200]}")
    proc = ssh_run(f"python3 {remote_script} {limit}", timeout=60)
    if proc.returncode != 0:
        raise RuntimeError(f"export failed: {proc.stderr[:300] or proc.stdout[:300]}")
    data = json.loads(proc.stdout)
    accounts = [a for a in data.get("accounts", []) if a.get("sso")]
    if not accounts:
        raise RuntimeError("no SSO accounts exported")
    return accounts


def export_web_egress() -> dict:
    """Prefer a live udeal pass_app proxy; fall back to healthy grok_web egress node."""
    remote = r'''
python3 - <<'PY'
import json, random, re, ssl, sqlite3, urllib.error, urllib.request
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit
import base64
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"

def udeal_proxy(session: str) -> str:
    user = f"userId-2684-custom-8241-region-sg-session-{session}-sessTime-15"
    return f"http://{quote(user, safe='')}:{quote('IM0aSd', safe='')}@as.udealproxy.com:6666"

def sessions_from(path: Path) -> list[str]:
    out = []
    for line in path.read_text(encoding="utf-8-sig", errors="ignore").splitlines():
        if "session=" not in line:
            continue
        s = (parse_qs(urlsplit(line.strip()).query).get("session") or [""])[0].strip()
        if s:
            out.append(s)
    return list(dict.fromkeys(out))

def fetch(url: str, proxy: str, timeout: float = 18):
    handlers = [
        urllib.request.ProxyHandler({"http": proxy, "https": proxy}),
        urllib.request.HTTPSHandler(context=ssl.create_default_context()),
    ]
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html"})
    try:
        with opener.open(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read() or b""
        except Exception:
            body = b""
        return exc.code, body
    except Exception:
        return 0, b""

picked = None
src = Path("/tmp/udeal1000proxy.txt")
if src.exists():
    sessions = sessions_from(src)
    random.seed()
    random.shuffle(sessions)
    for sess in sessions[:36]:
        proxy = udeal_proxy(sess)
        http, body = fetch("https://grok.com/", proxy)
        text = body.decode("utf-8", "replace")
        if http == 200 and "<title>Grok</title>" in text and "Just a moment" not in text:
            picked = {
                "id": f"udeal-{sess}",
                "name": f"udeal-pass-{sess}",
                "health": 1.0,
                "proxyUrl": proxy,
                "userAgent": UA,
                "proxy_host": "as.udealproxy.com:6666",
                "source": "udeal_pass_app",
                "session": sess,
            }
            break

if picked is None:
    def load_key():
        for line in Path("/opt/grok2api/config.yaml").read_text().splitlines():
            if "credentialEncryptionKey" in line:
                return base64.b64decode(line.split(":", 1)[1].strip().strip("\"'"))
        raise SystemExit("no key")
    key = load_key()
    aes = AESGCM(key)
    con = sqlite3.connect("/opt/grok2api/data/backend.db")
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """
        SELECT id, name, health, encrypted_proxy_url, user_agent
        FROM egress_nodes
        WHERE enabled=1 AND scope='grok_web' AND health >= 0.9
          AND (last_error IS NULL OR last_error='')
        ORDER BY health DESC, id ASC
        LIMIT 8
        """
    ).fetchall()
    for r in rows:
        blob = r["encrypted_proxy_url"]
        pad = "=" * ((4 - len(blob) % 4) % 4)
        raw = base64.b64decode(blob + pad)
        url = aes.decrypt(raw[:12], raw[12:], None).decode()
        ua = (r["user_agent"] or "").strip() or UA
        picked = {
            "id": r["id"],
            "name": r["name"],
            "health": r["health"],
            "proxyUrl": url,
            "userAgent": ua,
            "proxy_host": url.split("@")[-1] if "@" in url else url.split("://")[-1],
            "source": "egress_fallback",
        }
        break

print(json.dumps(picked or {}, ensure_ascii=False))
PY
'''
    proc = ssh_run(remote, timeout=420)
    if proc.returncode != 0:
        raise RuntimeError(f"egress export failed: {proc.stderr[:300] or proc.stdout[:300]}")
    data = json.loads(proc.stdout.strip().splitlines()[-1])
    if not data.get("proxyUrl"):
        raise RuntimeError("no healthy web egress / udeal pass_app")
    return data


def panda_preflight() -> dict:
    script = r"""
import os
mem_kb = 0
for line in open('/proc/meminfo'):
    if line.startswith('MemAvailable:'):
        mem_kb = int(line.split()[1])
        break
load1 = float(open('/proc/loadavg').read().split()[0])
cpus = os.cpu_count() or 1
load_norm = load1 / cpus
st = os.statvfs('/')
disk_pct = round((1 - st.f_bavail / st.f_blocks) * 100, 1)
ok = mem_kb >= 1048576 and load_norm < 0.70 and disk_pct < 85
print(__import__('json').dumps({
  'mem_available_gi': round(mem_kb/1048576, 2),
  'load1': load1,
  'cpus': cpus,
  'load_norm': round(load_norm, 3),
  'disk_pct': disk_pct,
  'ok': ok,
}))
"""
    proc = ssh_run(f"python3 - <<'PY'\n{script}\nPY", timeout=30)
    if proc.returncode != 0:
        raise RuntimeError(f"preflight failed: {proc.stderr[:200]}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


def sync_bridge_app() -> None:
    local = ROOT / "browser-bridge" / "app.py"
    remote = "/opt/grok2api/browser-bridge/app.py"
    proc = subprocess.run(
        ["scp", str(local), f"{SSH_HOST}:{remote}"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"sync app.py failed: {proc.stderr[:200]}")


def start_bridge() -> dict:
    # Canary: longer bootstrap + higher mem (768m OOMs Chrome on grok.com).
    remote = r"""
set -e
cd /opt/grok2api
cat > /tmp/bridge-canary.override.yml <<'EOF'
services:
  browser-bridge:
    mem_limit: 1536m
    environment:
      BRIDGE_BOOTSTRAP_SECONDS: "90"
      BRIDGE_MAX_OPERATION_SECONDS: "120"
EOF
docker compose -f docker-compose.yml -f /tmp/bridge-canary.override.yml up -d --force-recreate browser-bridge
for i in $(seq 1 40); do
  ip=$(python3 - <<'PY'
import json,subprocess
info=json.loads(subprocess.check_output(["docker","inspect","grok2api-browser-bridge"],text=True))[0]
print(next(iter(info["NetworkSettings"]["Networks"].values()))["IPAddress"])
PY
)
  if [ -n "$ip" ] && curl -fsS --max-time 3 "http://$ip:8192/healthz" >/dev/null 2>&1; then
    echo "{\"ok\":true,\"ip\":\"$ip\",\"tries\":$i,\"mem_limit\":\"1536m\"}"
    exit 0
  fi
  sleep 3
done
echo '{"ok":false,"error":"bridge health timeout"}'
exit 1
"""
    proc = ssh_run(remote, timeout=180)
    if proc.returncode != 0:
        raise RuntimeError(f"start bridge failed: {proc.stderr[:300] or proc.stdout[:300]}")
    lines = [ln for ln in proc.stdout.strip().splitlines() if ln.startswith("{")]
    return json.loads(lines[-1])


def stop_bridge() -> dict:
    remote = r"""
cd /opt/grok2api
docker compose stop browser-bridge >/dev/null
api=$(docker inspect -f '{{.State.Health.Status}}' grok2api 2>/dev/null || echo unknown)
bridge=$(docker inspect -f '{{.State.Status}}' grok2api-browser-bridge 2>/dev/null || echo missing)
echo "{\"api_health\":\"$api\",\"bridge_status\":\"$bridge\"}"
"""
    proc = ssh_run(remote, timeout=60)
    if proc.returncode != 0:
        raise RuntimeError(f"stop bridge failed: {proc.stderr[:200]}")
    lines = [ln for ln in proc.stdout.strip().splitlines() if ln.startswith("{")]
    return json.loads(lines[-1])


def bridge_sign(
    sso: str,
    account_id: int,
    *,
    proxy_url: str = "",
    user_agent: str = UA,
    timeout_ms: int = 120000,
) -> dict:
    """Call /v1/sign via docker exec → 127.0.0.1:8192 (avoids host↔container DNS/IP issues)."""
    import base64
    import tempfile

    payload = {
        "path": CHAT_PATH,
        "method": "POST",
        "cookie": cookie(sso),
        "userAgent": user_agent,
        "referer": "https://grok.com/",
        "sessionKey": f"canary-{account_id}",
        "timeoutMs": timeout_ms,
        "proxyUrl": proxy_url,
    }
    payload_b64 = base64.b64encode(json.dumps(payload).encode()).decode()
    helper = f"""#!/usr/bin/env python3
import base64, json, subprocess, sys
payload_b64 = {payload_b64!r}
timeout_ms = {timeout_ms}
inner = r'''
import json, sys, urllib.error, urllib.request
payload = json.load(sys.stdin)
key = open("/run/secrets/browser-bridge-key", "rb").read().strip().decode()
body = json.dumps(payload).encode()
req = urllib.request.Request(
    "http://127.0.0.1:8192/v1/sign",
    data=body,
    headers={{"Authorization": "Bearer " + key, "Content-Type": "application/json"}},
    method="POST",
)
try:
    with urllib.request.urlopen(req, timeout=max(30, int(payload.get("timeoutMs", 120000)) // 1000 + 20)) as resp:
        raw = resp.read().decode()
        print(json.dumps({{"http": resp.status, "body": json.loads(raw)}}))
except urllib.error.HTTPError as e:
    err_body = e.read().decode("utf-8", "replace")
    try:
        parsed = json.loads(err_body)
    except Exception:
        parsed = None
    print(json.dumps({{"http": e.code, "error": "HTTPError: %s" % e, "body": parsed, "body_text": err_body[:400]}}))
except Exception as e:
    print(json.dumps({{"http": 0, "error": type(e).__name__ + ": " + str(e)[:200]}}))
'''
payload = base64.b64decode(payload_b64)
proc = subprocess.run(
    ["docker", "exec", "-i", "grok2api-browser-bridge", "python3", "-c", inner],
    input=payload,
    capture_output=True,
    timeout=max(120, timeout_ms // 1000 + 40),
)
out = (proc.stdout or b"").decode("utf-8", "replace").strip()
err = (proc.stderr or b"").decode("utf-8", "replace").strip()
if not out:
    print(json.dumps({{"http": 0, "error": "empty docker exec output: " + err[:300], "rc": proc.returncode}}))
else:
    # last JSON line
    lines = [ln for ln in out.splitlines() if ln.startswith("{{")]
    print(lines[-1] if lines else out)
"""
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as fh:
        fh.write(helper)
        local_helper = fh.name
    remote_helper = f"/tmp/bridge_sign_{account_id}.py"
    try:
        scp = subprocess.run(
            ["scp", local_helper, f"{SSH_HOST}:{remote_helper}"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if scp.returncode != 0:
            return {"http": 0, "error": f"scp sign helper failed: {scp.stderr[:200]}"}
        proc = ssh_run(f"python3 {remote_helper}; rm -f {remote_helper}", timeout=max(180, timeout_ms // 1000 + 60))
        if not proc.stdout.strip():
            return {"http": 0, "error": f"empty sign response: {proc.stderr[:300]}"}
        lines = [ln for ln in proc.stdout.strip().splitlines() if ln.startswith("{")]
        return json.loads(lines[-1])
    finally:
        try:
            Path(local_helper).unlink(missing_ok=True)
        except OSError:
            pass


def load_accounts_from_file(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return [a for a in data.get("accounts", []) if a.get("sso")]


def main() -> None:
    parser = argparse.ArgumentParser(description="Bridge-sign + pure HTTP chat canary")
    parser.add_argument("--sso-file", type=Path, help="existing SSO JSON (skip export)")
    parser.add_argument("--limit", type=int, default=2)
    parser.add_argument("--skip-preflight", action="store_true")
    parser.add_argument("--skip-sync", action="store_true")
    parser.add_argument("--skip-start", action="store_true", help="assume bridge already up")
    parser.add_argument("--keep-bridge", action="store_true", help="do not stop bridge after")
    parser.add_argument("--account-id", type=int, help="only this account id")
    args = parser.parse_args()

    TMP.mkdir(parents=True, exist_ok=True)
    summary: dict = {"stages": {}}

    if not args.skip_preflight:
        pf = panda_preflight()
        summary["stages"]["preflight"] = pf
        print(json.dumps({"event": "preflight", **{k: pf[k] for k in pf if k != "ok"}}, ensure_ascii=False))
        if not pf.get("ok"):
            raise SystemExit("preflight failed: resources below thresholds")

    if not args.skip_sync:
        sync_bridge_app()
        summary["stages"]["sync"] = {"ok": True}
        print(json.dumps({"event": "sync_app", "ok": True}))

    if not args.skip_start:
        started = start_bridge()
        summary["stages"]["start_bridge"] = started
        print(json.dumps({"event": "start_bridge", **started}, ensure_ascii=False))
        if not started.get("ok"):
            raise SystemExit("bridge failed to become healthy")

    try:
        if args.sso_file:
            accounts = load_accounts_from_file(args.sso_file)
        else:
            accounts = export_sso(args.limit)
            # persist without full SSO for audit? keep full in .tmp for rerun, gitignored
            TMP.joinpath("web-sso-canary.json").write_text(
                json.dumps({"count": len(accounts), "accounts": accounts}, ensure_ascii=False),
                encoding="utf-8",
            )
        if args.account_id:
            accounts = [a for a in accounts if a["id"] == args.account_id]
        accounts = accounts[: args.limit]
        print(json.dumps({"event": "accounts", "count": len(accounts), "ids": [a["id"] for a in accounts]}))

        egress = export_web_egress()
        chat_ua = egress.get("userAgent") or UA
        summary["stages"]["egress"] = {
            "id": egress.get("id"),
            "name": egress.get("name"),
            "health": egress.get("health"),
            "proxy_host": egress.get("proxy_host"),
            "source": egress.get("source"),
            "ua_prefix": (chat_ua or "")[:48],
        }
        print(
            json.dumps(
                {
                    "event": "egress",
                    "id": egress.get("id"),
                    "name": egress.get("name"),
                    "proxy_host": egress.get("proxy_host"),
                    "source": egress.get("source"),
                    "ua_prefix": (chat_ua or "")[:48],
                },
                ensure_ascii=False,
            )
        )

        sess = requests.Session(impersonate=IMPERSONATE, proxies=PROXY)
        results = []
        for acc in accounts:
            aid = acc["id"]
            sso = acc["sso"]
            row: dict = {"account_id": aid}

            # A: no signature
            try:
                row["A_no_sig"] = post_chat(sess, sso, None, user_agent=chat_ua)
            except Exception as exc:  # noqa: BLE001
                row["A_no_sig"] = {"error": f"{type(exc).__name__}: {str(exc)[:160]}"}
            print(
                json.dumps(
                    {
                        "event": "A_no_sig",
                        "account_id": aid,
                        "http": row["A_no_sig"].get("http"),
                        "kind": row["A_no_sig"].get("kind") or row["A_no_sig"].get("error"),
                        "elapsed_s": row["A_no_sig"].get("elapsed_s"),
                    },
                    ensure_ascii=False,
                )
            )

            # B: bridge sign (Chrome via web egress — panda direct hits CF)
            sign_res = bridge_sign(
                sso,
                aid,
                proxy_url=egress["proxyUrl"],
                user_agent=chat_ua,
                timeout_ms=120000,
            )
            statsig = None
            if isinstance(sign_res.get("body"), dict):
                statsig = sign_res["body"].get("statsigId")
            row["B_sign"] = {
                "http": sign_res.get("http"),
                "error": sign_res.get("error"),
                "has_statsig": bool(statsig),
                "statsig_len": len(statsig) if statsig else 0,
                "sign_error": (sign_res.get("body") or {}).get("error")
                if isinstance(sign_res.get("body"), dict)
                else sign_res.get("body_text", "")[:200],
            }
            print(
                json.dumps(
                    {
                        "event": "B_sign",
                        "account_id": aid,
                        "http": row["B_sign"]["http"],
                        "has_statsig": row["B_sign"]["has_statsig"],
                        "statsig_len": row["B_sign"]["statsig_len"],
                        "sign_error": (row["B_sign"].get("sign_error") or row["B_sign"].get("error") or None),
                    },
                    ensure_ascii=False,
                )
            )

            # C: signed pure HTTP (local Mihomo; same UA as bridge session)
            if statsig:
                try:
                    row["C_signed"] = post_chat(sess, sso, statsig, user_agent=chat_ua)
                except Exception as exc:  # noqa: BLE001
                    row["C_signed"] = {"error": f"{type(exc).__name__}: {str(exc)[:160]}"}
            else:
                row["C_signed"] = {"skipped": True, "reason": "no statsig"}
            print(
                json.dumps(
                    {
                        "event": "C_signed",
                        "account_id": aid,
                        "http": row["C_signed"].get("http"),
                        "kind": row["C_signed"].get("kind") or row["C_signed"].get("error") or row["C_signed"].get("reason"),
                        "has_token_hint": row["C_signed"].get("has_token_hint"),
                        "elapsed_s": row["C_signed"].get("elapsed_s"),
                    },
                    ensure_ascii=False,
                )
            )
            results.append(row)

        summary["results"] = results
        out = TMP / "web-sso-http-canary-result.json"
        # strip body prefixes that might leak content but keep classification
        safe = json.loads(json.dumps(results))
        for r in safe:
            for key in ("A_no_sig", "C_signed"):
                if isinstance(r.get(key), dict) and "body_prefix" in r[key]:
                    # keep short prefix for debugging anti-bot vs CF
                    pass
        out.write_text(json.dumps({"summary_meta": {k: summary["stages"] for k in ("stages",)}, "results": safe}, ensure_ascii=False, indent=2), encoding="utf-8")
        # rewrite properly
        out.write_text(
            json.dumps({"stages": summary["stages"], "results": results}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(json.dumps({"event": "wrote", "path": str(out)}))

        # acceptance quick check
        a_ok = any((r.get("A_no_sig") or {}).get("kind") == "anti_bot_403" for r in results)
        c_ok = any((r.get("C_signed") or {}).get("kind") == "chat_ok" for r in results)
        print(json.dumps({"event": "acceptance", "A_anti_bot": a_ok, "C_chat_ok": c_ok}))
    finally:
        if not args.keep_bridge:
            stopped = stop_bridge()
            print(json.dumps({"event": "stop_bridge", **stopped}, ensure_ascii=False))


if __name__ == "__main__":
    main()
