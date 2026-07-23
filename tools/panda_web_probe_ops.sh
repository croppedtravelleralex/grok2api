#!/usr/bin/env bash
# Panda 部署后运维：动态调高 Web 探针预算 + 删除聊轨死池不可恢复账号
set -euo pipefail

BASE="${GROK2API_ADMIN_BASE:-http://127.0.0.1:18000/api/admin/v1}"
DRY_RUN="${DRY_RUN:-0}"

read_password() {
  for f in "${GROK2API_ADMIN_PASSWORD_FILE:-}" /root/.secrets/grok2api-admin-password /opt/grok2api/staging/admin-password; do
    [[ -n "$f" && -f "$f" ]] || continue
    tr -d '\r\n' <"$f"
    return 0
  done
  echo "admin password file missing" >&2
  exit 1
}

api() {
  local method="$1" path="$2" body="${3:-}"
  local args=(-sS -X "$method" -H "Accept: application/json")
  [[ -n "${TOKEN:-}" ]] && args+=(-H "Authorization: Bearer $TOKEN")
  [[ -n "$body" ]] && args+=(-H "Content-Type: application/json" -d "$body")
  curl "${args[@]}" "$BASE$path"
}

TOKEN=$(api POST /auth/login "{\"username\":\"admin\",\"password\":\"$(read_password)\"}" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["data"]["tokens"]["accessToken"])')

export TOKEN BASE DRY_RUN
python3 <<'PY'
import json, math, os, urllib.request

base = os.environ["BASE"].rstrip("/")
token = os.environ["TOKEN"]
dry = os.environ.get("DRY_RUN", "0") == "1"

def call(method, path, body=None):
    req = urllib.request.Request(
        f"{base}{path}",
        data=None if body is None else json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json", **({} if body is None else {"Content-Type": "application/json"}))},
        method=method,
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.load(resp)["data"]

settings = call("GET", "/settings")
probe = call("GET", "/accounts/web-probe")
accounts_page = call("GET", "/accounts?provider=grok_web&page=1&pageSize=1")
web_total = int(accounts_page.get("total", 0))
chat_dead = int(probe.get("pools", {}).get("chat", {}).get("dead", 0))

lite_global = max(24, min(120, math.ceil(web_total / 15)))
lite_per_account = 3 if web_total >= 120 else 2
chat_per_account = 4 if web_total >= 120 else 3

cfg = settings["config"]
wp = dict(cfg.get("webProbe") or {})
wp.update({
    "probeUnknownQuota": True,
    "liteGlobalPerHour": lite_global,
    "litePerAccountPerDay": lite_per_account,
    "chatPerAccountPerDay": chat_per_account,
    "dispatchInterval": wp.get("dispatchInterval") or "30s",
    "idleInterval": wp.get("idleInterval") or "5m",
    "initialDelay": wp.get("initialDelay") or "30s",
    "deadL2MinInterval": wp.get("deadL2MinInterval") or "24h",
    "pipelineL1Threshold": wp.get("pipelineL1Threshold", 0.7),
    "pipelineL0OnlyThreshold": wp.get("pipelineL0OnlyThreshold", 0.9),
})
cfg["webProbe"] = wp

print("=== probe budget tune ===")
print(json.dumps({
    "webTotal": web_total,
    "chatDead": chat_dead,
    "liteGlobalPerHour": lite_global,
    "litePerAccountPerDay": lite_per_account,
    "chatPerAccountPerDay": chat_per_account,
    "probeUnknownQuota": True,
    "dryRun": dry,
}, ensure_ascii=False, indent=2))

if not dry:
    updated = call("PUT", "/settings", {"revision": settings["revision"], "config": cfg})
    print("settings revision ->", updated["revision"])

# collect deletable unauthorized chat-dead accounts
delete_ids = []
page = 1
page_size = 200
while True:
    data = call("GET", f"/accounts?provider=grok_web&page={page}&pageSize={page_size}")
    items = data.get("items", [])
    if not items:
        break
    for item in items:
        err = (item.get("lastError") or "").lower()
        auth = item.get("authStatus") or ""
        enabled = bool(item.get("enabled"))
        failures = int(item.get("failureCount") or 0)
        unauthorized = "unauthorized" in err or "credential unauthorized" in err
        if auth == "reauthRequired":
            delete_ids.append(str(item["id"]))
            continue
        if unauthorized and (err.startswith("web_dead:") or not enabled or failures >= 2):
            delete_ids.append(str(item["id"]))
    if len(items) < page_size:
        break
    page += 1

delete_ids = sorted(set(delete_ids), key=int)
print("=== delete candidates ===")
print(json.dumps({"count": len(delete_ids), "sample": delete_ids[:20]}, ensure_ascii=False, indent=2))

if delete_ids and not dry:
    batch = 50
    deleted = 0
    for i in range(0, len(delete_ids), batch):
        chunk = delete_ids[i:i+batch]
        result = call("DELETE", "/accounts", {"provider": "grok_web", "ids": chunk})
        deleted += int(result.get("deleted", len(chunk)))
    print(f"deleted {deleted} accounts")
PY
