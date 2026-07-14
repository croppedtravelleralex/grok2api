#!/usr/bin/env python3
import base64
import hashlib
import hmac
import json
import os
import signal
import sys
import threading
import time
import urllib.parse
from collections import OrderedDict

from bottle import Bottle, HTTPError, request, response
from waitress import serve

sys.path.insert(0, "/app")

import utils
from dtos import V1RequestBase
from flaresolverr_service import _evil_logic


APP = Bottle()
SESSION_LIMIT = max(1, int(os.environ.get("BRIDGE_SESSION_LIMIT", "1")))
SESSION_TTL = max(60, int(os.environ.get("BRIDGE_SESSION_TTL_SECONDS", "1800")))
CLOSE_TIMEOUT = min(5.0, max(0.5, float(os.environ.get("BRIDGE_CLOSE_TIMEOUT_SECONDS", "2"))))
SIGNER_MODULE_ID = int(os.environ.get("BRIDGE_SIGNER_MODULE_ID", "4629918"))
KEY_FILE = os.environ.get("BRIDGE_KEY_FILE", "/run/secrets/browser-bridge-key")
ALLOWED_HOSTS = {"grok.com", "www.grok.com"}
SESSIONS = OrderedDict()
SESSIONS_LOCK = threading.Lock()
SESSION_CREATE_LOCK = threading.Lock()
SESSION_CREATING = threading.Event()


class BrowserSession:
    def __init__(self, driver, proxy_url, origin):
        self.driver = driver
        self.proxy_url = proxy_url
        self.origin = origin
        self.lock = threading.Lock()
        self.last_used = time.monotonic()

    def close(self):
        completed = threading.Event()

        def graceful_close():
            try:
                self.driver.quit()
            except Exception:
                pass
            finally:
                completed.set()

        threading.Thread(target=graceful_close, name="browser-session-close", daemon=True).start()
        if not completed.wait(CLOSE_TIMEOUT):
            _terminate_driver_processes(self.driver)
            completed.wait(0.5)
        _terminate_orphaned_browser_processes()


def _process_children():
    children = {}
    try:
        entries = os.listdir("/proc")
    except OSError:
        return children
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat", encoding="utf-8") as stat_file:
                value = stat_file.read()
            fields = value.rsplit(")", 1)[1].split()
            parent = int(fields[1])
            children.setdefault(parent, []).append(int(entry))
        except (OSError, ValueError, IndexError):
            continue
    return children


def _terminate_process_tree(root_pid):
    if not isinstance(root_pid, int) or root_pid <= 1:
        return
    children = _process_children()
    descendants = []
    pending = [root_pid]
    while pending:
        parent = pending.pop()
        for child in children.get(parent, []):
            descendants.append(child)
            pending.append(child)
    targets = list(reversed(descendants)) + [root_pid]
    for signal_value in (signal.SIGTERM, signal.SIGKILL):
        for pid in targets:
            try:
                os.kill(pid, signal_value)
            except (OSError, ProcessLookupError):
                pass
        if signal_value == signal.SIGTERM:
            time.sleep(0.15)


def _terminate_driver_processes(driver):
    roots = []
    service = getattr(driver, "service", None)
    process = getattr(service, "process", None)
    process_pid = getattr(process, "pid", None)
    browser_pid = getattr(driver, "browser_pid", None)
    for pid in (process_pid, browser_pid):
        if isinstance(pid, int) and pid > 1 and pid not in roots:
            roots.append(pid)
    for pid in roots:
        _terminate_process_tree(pid)


def _terminate_orphaned_browser_processes():
    targets = []
    try:
        entries = os.listdir("/proc")
    except OSError:
        return
    browser_names = {"chrome", "chromedriver", "chromium", "chromium-browser", "chrome_crashpad"}
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/comm", encoding="utf-8") as comm_file:
                name = comm_file.read().strip()
            pid = int(entry)
        except (OSError, ValueError):
            continue
        if pid > 1 and name in browser_names:
            targets.append(pid)
    for signal_value in (signal.SIGTERM, signal.SIGKILL):
        for pid in targets:
            try:
                os.kill(pid, signal_value)
            except (OSError, ProcessLookupError):
                pass
        if signal_value == signal.SIGTERM and targets:
            time.sleep(0.15)


def load_key():
    try:
        return open(KEY_FILE, encoding="utf-8").read().strip()
    except OSError:
        return ""


def env_flag(name, fallback):
    value = str(os.environ.get(name, "")).strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return fallback


def authorized():
    expected = load_key()
    if not expected:
        return False
    supplied = request.headers.get("Authorization", "")
    return hmac.compare_digest(supplied, "Bearer " + expected)


def validate_target(raw_url, schemes):
    parsed = urllib.parse.urlsplit(str(raw_url or ""))
    if parsed.scheme not in schemes or parsed.hostname not in ALLOWED_HOSTS or parsed.username or parsed.password:
        raise HTTPError(400, "invalid target")
    return parsed


def parse_proxy(raw_url):
    raw_url = str(raw_url or "").strip()
    if not raw_url or raw_url == "direct://":
        return None
    parsed = urllib.parse.urlsplit(raw_url)
    if parsed.scheme not in {"http", "https", "socks5"} or not parsed.hostname or not parsed.port:
        raise HTTPError(400, "invalid proxy")
    clean_url = f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"
    if parsed.username is None and parsed.password is None:
        return {"url": clean_url}
    return {
        "url": clean_url,
        "username": urllib.parse.unquote(parsed.username or ""),
        "password": urllib.parse.unquote(parsed.password or ""),
    }


def parse_cookies(raw_cookie):
    values = []
    for item in str(raw_cookie or "").split(";"):
        name, separator, value = item.strip().partition("=")
        if not separator or not name or not value:
            continue
        values.append({"name": name, "value": value, "domain": ".grok.com", "path": "/", "secure": True})
    return values


def close_expired_locked(now):
    expired = [key for key, value in SESSIONS.items() if now - value.last_used > SESSION_TTL]
    for key in expired:
        SESSIONS.pop(key).close()
    return len(expired)


def acquire_session(session_key, proxy_url, cookies, referer, target_url):
    if not session_key or len(session_key) > 128:
        raise HTTPError(400, "invalid session key")
    target = validate_target(target_url, {"https", "wss"})
    origin = "https://" + target.hostname + "/"
    bootstrap_url = origin + ("index" if target.hostname in {"grok.com", "www.grok.com"} else "")
    with SESSION_CREATE_LOCK:
        now = time.monotonic()
        with SESSIONS_LOCK:
            close_expired_locked(now)
            current = SESSIONS.get(session_key)
            if current is not None and (current.proxy_url != proxy_url or current.origin != origin):
                SESSIONS.pop(session_key).close()
                current = None
            if current is not None:
                SESSIONS.move_to_end(session_key)
                current.last_used = now
        if current is None:
            stale = []
            with SESSIONS_LOCK:
                while len(SESSIONS) >= SESSION_LIMIT:
                    _, oldest = SESSIONS.popitem(last=False)
                    stale.append(oldest)
            for session in stale:
                session.close()
            driver = None
            SESSION_CREATING.set()
            try:
                driver = utils.get_webdriver(parse_proxy(proxy_url))
                if target.hostname in {"grok.com", "www.grok.com"}:
                    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": r"""
(() => {
  const queue = [];
  const nativePush = Array.prototype.push;
  const wrap = entry => {
    if (!Array.isArray(entry) || typeof entry[entry.length - 1] !== 'function') return entry;
    const original = entry[entry.length - 1];
    entry[entry.length - 1] = function(...args) {
      if (args[0]) globalThis.__grokBridgeRuntime = args[0];
      return original.apply(this, args);
    };
    return entry;
  };
  queue.push = function(...entries) { return nativePush.apply(this, entries.map(wrap)); };
  globalThis.TURBOPACK = queue;
})();
"""})
                bootstrap = V1RequestBase({"url": bootstrap_url, "cookies": cookies, "returnOnlyCookies": True})
                _evil_logic(bootstrap, driver, "GET")
                current = BrowserSession(driver, proxy_url, origin)
                with SESSIONS_LOCK:
                    SESSIONS[session_key] = current
                    current.last_used = time.monotonic()
            except Exception:
                if driver is not None:
                    BrowserSession(driver, proxy_url, origin).close()
                raise
            finally:
                SESSION_CREATING.clear()
    if referer:
        parsed = validate_target(referer, {"https"})
        if urllib.parse.urlsplit(current.driver.current_url).path != parsed.path:
            current.driver.execute_script("history.replaceState(null, '', arguments[0])", referer)
    return current


def json_body():
    value = request.json
    if not isinstance(value, dict):
        raise HTTPError(400, "invalid json")
    return value


def encode_response(value, status=200):
    response.status = status
    response.content_type = "application/json"
    response.set_header("Cache-Control", "no-store")
    return json.dumps(value, separators=(",", ":"))


@APP.get("/healthz")
def health():
    with SESSIONS_LOCK:
        expired_count = close_expired_locked(time.monotonic())
        session_count = len(SESSIONS)
    if session_count == 0 and expired_count == 0 and not SESSION_CREATING.is_set():
        _terminate_orphaned_browser_processes()
    return encode_response({"status": "ok", "sessions": session_count})


@APP.post("/v1/fetch")
def fetch():
    if not authorized():
        raise HTTPError(401, "unauthorized")
    payload = json_body()
    validate_target(payload.get("url"), {"https"})
    method = str(payload.get("method") or "GET").upper()
    if method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
        raise HTTPError(400, "invalid method")
    timeout_ms = min(max(int(payload.get("timeoutMs") or 120000), 1000), 900000)
    cookies = parse_cookies(payload.get("cookie"))
    browser = acquire_session(str(payload.get("sessionKey") or ""), str(payload.get("proxyUrl") or ""), cookies, str(payload.get("referer") or ""), str(payload.get("url") or ""))
    script = r"""
const cfg = arguments[0], done = arguments[arguments.length - 1];
let finished = false;
const finish = value => { if (!finished) { finished = true; done(value); } };
const timer = setTimeout(() => finish({error: 'browser fetch timeout'}), cfg.timeoutMs);
(async () => {
  try {
    const headers = new Headers();
    for (const [name, values] of Object.entries(cfg.headers || {})) {
      for (const value of values) headers.append(name, value);
    }
    const target = new URL(cfg.url);
    if ((target.hostname === 'grok.com' || target.hostname === 'www.grok.com') && target.pathname.startsWith('/rest/')) {
      let signature = '';
      try {
        if (!globalThis.__grokBridgeRuntime) throw new Error('Turbopack runtime unavailable');
        if (!globalThis.__grokBridgeSigner) {
          const signerModule = await globalThis.__grokBridgeRuntime.A(cfg.signerModuleId);
          globalThis.__grokBridgeSigner = signerModule.default();
        }
        signature = await globalThis.__grokBridgeSigner(target.pathname, cfg.method);
      } catch (signerError) {
        signature = btoa(`x0:${signerError}`);
      }
      headers.set('x-statsig-id', signature);
    }
    const init = {method: cfg.method, headers, credentials: 'include', cache: 'no-store'};
    if (cfg.referer) init.referrer = cfg.referer;
    if (cfg.body && !['GET', 'HEAD'].includes(cfg.method)) {
      const binary = atob(cfg.body), bytes = new Uint8Array(binary.length);
      for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
      init.body = bytes;
    }
    const result = await fetch(cfg.url, init);
    const blob = await result.blob();
    const reader = new FileReader();
    reader.onerror = () => finish({error: 'response encoding failed'});
    reader.onload = () => {
      clearTimeout(timer);
      const headersOut = {};
      result.headers.forEach((value, name) => { headersOut[name] = [value]; });
      finish({status: result.status, headers: headersOut, body: String(reader.result).split(',', 2)[1] || ''});
    };
    reader.readAsDataURL(blob);
  } catch (error) {
    clearTimeout(timer);
    finish({error: String(error && (error.stack || error.message) || error).slice(0, 1200)});
  }
})();
"""
    cfg = {
        "url": payload.get("url"), "method": method, "headers": payload.get("headers") or {},
        "body": payload.get("body") or "", "referer": payload.get("referer") or "", "timeoutMs": timeout_ms,
        "signerModuleId": SIGNER_MODULE_ID,
    }
    try:
        with browser.lock:
            browser.driver.set_script_timeout((timeout_ms / 1000) + 15)
            result = browser.driver.execute_async_script(script, cfg)
            browser.last_used = time.monotonic()
        return encode_response(result if isinstance(result, dict) else {"error": "invalid browser result"})
    except Exception as error:
        return encode_response({"error": type(error).__name__ + ": " + str(error)[:300]}, 502)


@APP.post("/v1/websocket")
def websocket():
    if not authorized():
        raise HTTPError(401, "unauthorized")
    payload = json_body()
    validate_target(payload.get("url"), {"wss"})
    timeout_ms = min(max(int(payload.get("timeoutMs") or 180000), 1000), 900000)
    idle_ms = min(max(int(payload.get("idleMs") or 5000), 500), 30000)
    expected = min(max(int(payload.get("expected") or 1), 1), 10)
    cookies = parse_cookies(payload.get("cookie"))
    browser = acquire_session(str(payload.get("sessionKey") or ""), str(payload.get("proxyUrl") or ""), cookies, str(payload.get("referer") or "https://grok.com/imagine"), str(payload.get("url") or ""))
    script = r"""
const cfg = arguments[0], done = arguments[arguments.length - 1];
let frames = [], completed = new Set(), finished = false, idleTimer = null;
const finish = error => {
  if (finished) return;
  finished = true;
  clearTimeout(timeoutTimer); clearTimeout(idleTimer);
  try { socket.close(); } catch (_) {}
  done({frames, error: error || ''});
};
const scheduleFinish = () => {
  clearTimeout(idleTimer);
  if (completed.size >= cfg.expected) idleTimer = setTimeout(() => finish(''), cfg.idleMs);
};
const timeoutTimer = setTimeout(() => finish('browser websocket timeout'), cfg.timeoutMs);
const socket = new WebSocket(cfg.url);
socket.onopen = () => { for (const message of cfg.messages || []) socket.send(JSON.stringify(message)); };
socket.onerror = () => finish('browser websocket error');
socket.onclose = event => { if (!finished && completed.size < cfg.expected) finish('browser websocket closed: ' + event.code); };
socket.onmessage = event => {
  const value = String(event.data); frames.push(value);
  try {
    const parsed = JSON.parse(value);
    if (parsed.type === 'error') return finish('upstream websocket error');
    if (parsed.current_status === 'completed' || parsed.currentStatus === 'completed') {
      completed.add(String(parsed.image_id || parsed.imageId || parsed.id || completed.size));
    }
  } catch (_) {}
  scheduleFinish();
};
"""
    cfg = {
        "url": payload.get("url"), "messages": payload.get("messages") or [], "timeoutMs": timeout_ms,
        "idleMs": idle_ms, "expected": expected,
    }
    try:
        with browser.lock:
            browser.driver.set_script_timeout((timeout_ms / 1000) + 15)
            result = browser.driver.execute_async_script(script, cfg)
            browser.last_used = time.monotonic()
        frames = [base64.b64encode(str(value).encode()).decode() for value in (result or {}).get("frames", [])]
        return encode_response({"frames": frames, "error": (result or {}).get("error", "")})
    except Exception as error:
        return encode_response({"error": type(error).__name__ + ": " + str(error)[:300]}, 502)


if __name__ == "__main__":
    if env_flag("BRIDGE_WARM_USER_AGENT", True):
        utils.get_user_agent()
    serve(APP, host="0.0.0.0", port=int(os.environ.get("PORT", "8192")), threads=max(2, int(os.environ.get("BRIDGE_THREADS", "2"))))
