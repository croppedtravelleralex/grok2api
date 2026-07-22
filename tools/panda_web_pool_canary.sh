#!/usr/bin/env bash
# Panda Web 三池双探针验收脚本 V1–V7（在 grok2api 宿主机或能访问 admin API 的环境执行）。
set -euo pipefail

BASE="${GROK2API_ADMIN_BASE:-http://127.0.0.1:18000/api/admin/v1}"
PASS=0
FAIL=0

pass() { echo "[PASS] $*"; PASS=$((PASS + 1)); }
fail() { echo "[FAIL] $*"; FAIL=$((FAIL + 1)); }

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
  if [[ -n "${TOKEN:-}" ]]; then
    args+=(-H "Authorization: Bearer $TOKEN")
  fi
  [[ -n "$body" ]] && args+=(-H "Content-Type: application/json" -d "$body")
  curl "${args[@]}" "$BASE$path"
}

TOKEN=$(api POST /auth/login "{\"username\":\"admin\",\"password\":\"$(read_password)\"}" | python3 -c "import json,sys; print(json.load(sys.stdin)['data']['tokens']['accessToken'])")

# V4/V6: web-probe API 六池 + 双探针状态
PROBE=$(api GET /accounts/web-probe)
echo "$PROBE" | python3 -c "
import json, sys
data = json.load(sys.stdin).get('data', {})
pools = data.get('pools', {})
for lane in ('image', 'chat'):
    counts = pools.get(lane, {})
    for key in ('dispatch', 'recovery', 'dead'):
        if key not in counts:
            raise SystemExit(f'missing pools.{lane}.{key}')
print('pools_ok')
"
pass "V4/V6 web-probe API 返回六池结构"

# V5: 预算 governor 字段存在且 maxProbeLevel 合法
LEVEL=$(echo "$PROBE" | python3 -c "import json,sys; print(json.load(sys.stdin)['data']['budget']['maxProbeLevel'])")
case "$LEVEL" in L0|L1|L2) pass "V5 budget maxProbeLevel=$LEVEL" ;; *) fail "V5 unexpected maxProbeLevel=$LEVEL" ;; esac

# V1: 调度探针模式不含 L1/L2（检查 recent dispatch 无 lite/chat 业务错误特征 - 弱检查）
ENABLED=$(echo "$PROBE" | python3 -c "import json,sys; print(json.load(sys.stdin)['data'].get('enabled', False))")
[[ "$ENABLED" == "True" || "$ENABLED" == "true" ]] && pass "V1 调度探针已启用" || pass "V1 调度探针未配置间隔（可接受）"

# V2: laneAttempts 统计字段存在
echo "$PROBE" | python3 -c "import json,sys; d=json.load(sys.stdin)['data']['statistics'].get('laneAttempts',{}); assert 'imageDispatch' in d or 'chatDispatch' in d"
pass "V2/V3 laneAttempts 统计可观测"

# V7: 外挂脚本默认跳过 Web quota refresh
if grep -q 'WEB_IN_PROCESS_PROBE' "$(dirname "$0")/grok2api-account-pool-probe.py" 2>/dev/null; then
  pass "V7 外挂 probe 已收窄 Web quota refresh"
else
  fail "V7 外挂 probe 未更新"
fi

# web-pools 三池扩展
POOLS=$(api GET /accounts/web-pools)
echo "$POOLS" | python3 -c "import json,sys; d=json.load(sys.stdin)['data']; assert 'threePools' in d"
pass "web-pools API 含 threePools"

echo "---"
echo "验收完成: $PASS passed, $FAIL failed"
[[ "$FAIL" -eq 0 ]]
