#!/usr/bin/env python3
import base64
import hashlib
import hmac
import json
import os
import re
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
MAX_OPERATION_MS = min(900000, max(1000, int(float(os.environ.get("BRIDGE_MAX_OPERATION_SECONDS", "900")) * 1000)))
BOOTSTRAP_TIMEOUT_MS = min(MAX_OPERATION_MS, max(5000, int(float(os.environ.get("BRIDGE_BOOTSTRAP_SECONDS", "30")) * 1000)))
OPERATION_GRACE_SECONDS = min(15.0, max(0.1, float(os.environ.get("BRIDGE_OPERATION_GRACE_SECONDS", "2"))))
SIGNER_MODULE_ID = int(os.environ.get("BRIDGE_SIGNER_MODULE_ID", "4629918"))
KEY_FILE = os.environ.get("BRIDGE_KEY_FILE", "/run/secrets/browser-bridge-key")
ALLOWED_HOSTS = {"grok.com", "www.grok.com"}
SESSIONS = OrderedDict()
SESSIONS_LOCK = threading.Lock()
SESSION_CREATE_LOCK = threading.Lock()
SESSION_CREATING = threading.Event()
SESSION_CREATION_LOCK = threading.Lock()
ACTIVE_SESSION_CREATIONS = set()


class BrowserSession:
    def __init__(self, driver, proxy_url, origin, user_agent=""):
        self.driver = driver
        self.proxy_url = proxy_url
        self.origin = origin
        self.user_agent = user_agent
        self.lock = threading.Lock()
        self.close_lock = threading.Lock()
        self.closed = False
        self.last_used = time.monotonic()

    def close(self):
        with self.close_lock:
            if self.closed:
                return
            self.closed = True
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


class BrowserOperationTimeout(TimeoutError):
    pass


def begin_session_creation():
    token = object()
    with SESSION_CREATION_LOCK:
        ACTIVE_SESSION_CREATIONS.add(token)
        SESSION_CREATING.set()
    return token


def end_session_creation(token):
    with SESSION_CREATION_LOCK:
        ACTIVE_SESSION_CREATIONS.discard(token)
        if not ACTIVE_SESSION_CREATIONS:
            SESSION_CREATING.clear()


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


def bounded_timeout_ms(raw_value, fallback):
    try:
        value = int(raw_value or fallback)
    except (TypeError, ValueError):
        value = fallback
    return min(max(value, 1000), MAX_OPERATION_MS)


def remaining_timeout_ms(deadline):
    return max(1, min(MAX_OPERATION_MS, int((deadline - time.monotonic()) * 1000)))


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


def user_agent_platform(user_agent):
    lowered = str(user_agent or "").lower()
    if "macintosh" in lowered or "mac os x" in lowered:
        return "MacIntel"
    if "windows" in lowered:
        return "Win32"
    return "Linux x86_64"


def close_expired_locked(now):
    expired = [key for key, value in SESSIONS.items() if now - value.last_used > SESSION_TTL]
    for key in expired:
        SESSIONS.pop(key).close()
    return len(expired)


def discard_session(browser):
    with SESSIONS_LOCK:
        stale_keys = [key for key, value in SESSIONS.items() if value is browser]
        for key in stale_keys:
            SESSIONS.pop(key, None)
    browser.close()


def execute_browser_operation(browser, timeout_ms, operation):
    completed = threading.Event()
    state = {}

    def run():
        try:
            with browser.lock:
                if browser.closed:
                    raise RuntimeError("browser session is closed")
                state["result"] = operation()
                browser.last_used = time.monotonic()
        except Exception as error:
            state["error"] = error
        finally:
            completed.set()

    threading.Thread(target=run, name="browser-operation", daemon=True).start()
    if not completed.wait((timeout_ms / 1000) + OPERATION_GRACE_SECONDS):
        discard_session(browser)
        raise BrowserOperationTimeout("browser operation exceeded its deadline")
    error = state.get("error")
    if error is not None:
        discard_session(browser)
        raise error
    return state.get("result")


def acquire_session(session_key, proxy_url, cookies, referer, target_url, timeout_ms=None, user_agent="", light_bootstrap=False):
    if not session_key or len(session_key) > 128:
        raise HTTPError(400, "invalid session key")
    timeout_ms = bounded_timeout_ms(None, 120000) if timeout_ms is None else max(1, min(MAX_OPERATION_MS, int(timeout_ms)))
    deadline = time.monotonic() + (timeout_ms / 1000)
    target = validate_target(target_url, {"https", "wss"})
    origin = "https://" + target.hostname + "/"
    bootstrap_url = origin
    with SESSION_CREATE_LOCK:
        now = time.monotonic()
        with SESSIONS_LOCK:
            close_expired_locked(now)
            current = SESSIONS.get(session_key)
            if current is not None and (current.proxy_url != proxy_url or current.origin != origin or current.user_agent != user_agent):
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
            creation_token = begin_session_creation()
            completed = threading.Event()
            cancelled = threading.Event()
            state = {}

            def bootstrap():
                driver = None
                browser = None
                try:
                    state["stage"] = "launching browser"
                    driver = utils.get_webdriver(parse_proxy(proxy_url))
                    browser = BrowserSession(driver, proxy_url, origin, user_agent)
                    state["browser"] = browser
                    if cancelled.is_set():
                        browser.close()
                        return
                    if target.hostname in {"grok.com", "www.grok.com"}:
                        if user_agent:
                            driver.execute_cdp_cmd("Network.setUserAgentOverride", {"userAgent": user_agent, "platform": user_agent_platform(user_agent)})
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
                    if light_bootstrap and target.hostname in {"grok.com", "www.grok.com"}:
                        # Skip FlareSolverr: used when egress already clears CF (e.g. udeal pass_app).
                        state["stage"] = "loading Grok via direct navigation"
                        for cookie in cookies or []:
                            try:
                                driver.execute_cdp_cmd("Network.setCookie", {
                                    "name": cookie["name"],
                                    "value": cookie["value"],
                                    "domain": cookie.get("domain") or ".grok.com",
                                    "path": cookie.get("path") or "/",
                                    "secure": bool(cookie.get("secure", True)),
                                })
                            except Exception:
                                pass
                        driver.set_page_load_timeout(max(30, min(90, timeout_ms // 1000)))
                        driver.get(bootstrap_url)
                    else:
                        state["stage"] = "loading Grok through the selected proxy"
                        request_value = V1RequestBase({"url": bootstrap_url, "cookies": cookies, "returnOnlyCookies": True})
                        _evil_logic(request_value, driver, "GET")
                    if cancelled.is_set():
                        browser.close()
                        return
                    state["session"] = browser
                    state["stage"] = "ready"
                except Exception as error:
                    state["error"] = error
                    if browser is not None:
                        browser.close()
                finally:
                    end_session_creation(creation_token)
                    completed.set()

            threading.Thread(target=bootstrap, name="browser-session-bootstrap", daemon=True).start()
            bootstrap_timeout_ms = min(remaining_timeout_ms(deadline), BOOTSTRAP_TIMEOUT_MS)
            if not completed.wait(bootstrap_timeout_ms / 1000):
                cancelled.set()
                stage = state.get("stage", "unknown stage")
                browser = state.get("browser")
                if browser is not None:
                    browser.close()
                else:
                    _terminate_orphaned_browser_processes()
                completed.wait(0.5)
                end_session_creation(creation_token)
                _terminate_orphaned_browser_processes()
                raise BrowserOperationTimeout("browser session bootstrap timed out while " + stage)
            error = state.get("error")
            if error is not None:
                stage = state.get("stage", "unknown stage")
                raise RuntimeError("browser session bootstrap failed while " + stage + ": " + type(error).__name__ + ": " + str(error)[:240]) from error
            current = state.get("session")
            if current is None:
                raise RuntimeError("browser session bootstrap returned no session")
            with SESSIONS_LOCK:
                SESSIONS[session_key] = current
                current.last_used = time.monotonic()
    if referer:
        parsed = validate_target(referer, {"https"})

        def update_referer():
            if urllib.parse.urlsplit(current.driver.current_url).path != parsed.path:
                current.driver.execute_script("history.replaceState(null, '', arguments[0])", referer)

        execute_browser_operation(current, remaining_timeout_ms(deadline), update_referer)
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


def safe_error_message(error):
    message = type(error).__name__ + ": " + str(error)[:300]
    # Selenium/代理库的异常偶尔会回显带认证信息的 URL；日志只保留协议和地址。
    return re.sub(r"(?i)(https?://)([^/@\s]+)@", r"\1***@", message)


def log_bridge_error(phase, error):
    print(json.dumps({"event": "browser_bridge_error", "phase": phase, "error": safe_error_message(error)}, separators=(",", ":")), flush=True)


def error_response(error):
    message = safe_error_message(error)
    log_bridge_error("bootstrap", error)
    return encode_response({"error": message}, 502)


@APP.get("/healthz")
def health():
    with SESSIONS_LOCK:
        expired_count = close_expired_locked(time.monotonic())
        session_count = len(SESSIONS)
    if session_count == 0 and expired_count == 0 and not SESSION_CREATING.is_set():
        _terminate_orphaned_browser_processes()
    return encode_response({"status": "ok", "sessions": session_count})


SIGN_SCRIPT = r"""
const cfg = arguments[0], done = arguments[arguments.length - 1];
let finished = false;
const finish = value => { if (!finished) { finished = true; done(value); } };
const timer = setTimeout(() => finish({error: 'browser sign timeout'}), cfg.timeoutMs);
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const looksGood = sig => {
  const value = String(sig || '').trim();
  return value.length > 20 && !value.startsWith('x0:') && !value.startsWith('eDA6');
};
const trySign = async (moduleId) => {
  const signerModule = await globalThis.__grokBridgeRuntime.A(moduleId);
  if (!signerModule || typeof signerModule.default !== 'function') {
    throw new Error('module has no default factory');
  }
  const signer = signerModule.default();
  if (typeof signer !== 'function') throw new Error('default() did not return signer');
  const statsigId = await signer(cfg.path, cfg.method);
  if (!looksGood(statsigId)) throw new Error('empty or fallback statsig id');
  globalThis.__grokBridgeSigner = signer;
  globalThis.__grokBridgeSignerModuleId = moduleId;
  return String(statsigId).trim();
};
const cachedModuleIds = () => {
  const runtime = globalThis.__grokBridgeRuntime;
  const ids = [];
  const cache = runtime && (runtime.c || runtime.m || runtime.modules);
  if (cache && typeof cache === 'object') {
    for (const key of Object.keys(cache)) {
      const n = Number(key);
      if (Number.isFinite(n) && n > 0) ids.push(n);
    }
  }
  ids.sort((a, b) => a - b);
  return ids;
};
(async () => {
  try {
    const deadline = Date.now() + Math.max(8000, (cfg.timeoutMs || 30000) - 2000);
    while (Date.now() < deadline) {
      if (globalThis.__grokBridgeRuntime && document.body && document.body.childNodes.length >= 3) break;
      await sleep(250);
    }
    if (!globalThis.__grokBridgeRuntime) throw new Error('Turbopack runtime unavailable');
    if (!document.body || document.body.childNodes.length < 3) {
      throw new Error('Grok DOM not ready for signer (body.childNodes=' + ((document.body && document.body.childNodes.length) || 0) + ')');
    }
    await sleep(2500);

    const captured = [];
    const note = (sig) => { if (looksGood(sig)) captured.push(String(sig).trim()); };
    try {
      const origSet = Headers.prototype.set;
      Headers.prototype.set = function(name, value) {
        if (String(name).toLowerCase() === 'x-statsig-id') note(value);
        return origSet.apply(this, arguments);
      };
      const origAppend = Headers.prototype.append;
      Headers.prototype.append = function(name, value) {
        if (String(name).toLowerCase() === 'x-statsig-id') note(value);
        return origAppend.apply(this, arguments);
      };
    } catch (_) {}
    const origFetch = window.fetch.bind(window);
    window.fetch = async (input, init = {}) => {
      try {
        const headers = init && init.headers;
        if (headers instanceof Headers) note(headers.get('x-statsig-id'));
        else if (headers && typeof headers === 'object') note(headers['x-statsig-id'] || headers['X-Statsig-Id']);
      } catch (_) {}
      return origFetch(input, init);
    };
    // Nudge SPA into issuing a signed REST call if it can.
    for (const path of ['/rest/rate-limits', cfg.path]) {
      try {
        await origFetch(path, {
          method: 'POST',
          credentials: 'include',
          headers: {'content-type': 'application/json'},
          body: JSON.stringify(path === cfg.path
            ? {temporary: true, message: 'ping', modeId: 'fast'}
            : {requestKind: 'CLIENT_STATE_UPDATE'}),
        });
      } catch (_) {}
      if (captured.length) break;
      await sleep(500);
    }
    if (captured.length) {
      clearTimeout(timer);
      finish({statsigId: captured[0], path: cfg.path, method: cfg.method, source: 'fetch-capture'});
      return;
    }

    const preferred = Number(cfg.signerModuleId) || 4629918;
    let preferredError = null;
    try {
      const statsigId = await trySign(preferred);
      clearTimeout(timer);
      finish({
        statsigId,
        path: cfg.path,
        method: cfg.method,
        source: 'module',
        signerModuleId: preferred,
        attempts: 1,
      });
      return;
    } catch (error) {
      preferredError = error;
    }

    // DOM diagnostics when the known signer fails (usually missing meta node).
    const metas = [...document.querySelectorAll('meta')].map(m => ({
      name: m.getAttribute('name'),
      property: m.getAttribute('property'),
      content: String(m.getAttribute('content') || '').slice(0, 80),
    }));
    const ver = metas.filter(m => /verif|statsig|grok|site/i.test(String(m.name || '') + String(m.property || '')));
    finish({
      error: String(preferredError && (preferredError.stack || preferredError.message) || preferredError).slice(0, 800),
      signerModuleId: preferred,
      source: 'module-failed',
      dom: {
        title: document.title,
        url: location.href,
        bodyKids: document.body ? document.body.childNodes.length : 0,
        metaCount: metas.length,
        ver,
        metas: metas.slice(0, 25),
        text: String((document.body && document.body.innerText) || '').slice(0, 160),
      },
      capturedCount: captured.length,
    });
  } catch (error) {
    clearTimeout(timer);
    finish({error: String(error && (error.stack || error.message) || error).slice(0, 1200)});
  }
})();
"""


@APP.post("/v1/sign")
def sign():
    """只生成 grok.com /rest/* 的 x-statsig-id，不发起业务请求。"""
    if not authorized():
        raise HTTPError(401, "unauthorized")
    payload = json_body()
    method = str(payload.get("method") or "POST").upper()
    if method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
        raise HTTPError(400, "invalid method")
    path = str(payload.get("path") or "").strip()
    if not path.startswith("/rest/"):
        raise HTTPError(400, "invalid path")
    timeout_ms = bounded_timeout_ms(payload.get("timeoutMs"), 120000)
    deadline = time.monotonic() + (timeout_ms / 1000)
    cookies = parse_cookies(payload.get("cookie"))
    session_key = str(payload.get("sessionKey") or "sign-only")[:128]
    bootstrap_url = "https://grok.com/"
    # /v1/sign is for already-clear egress; FlareSolverr bootstrap often times out on panda.
    light_bootstrap = payload.get("lightBootstrap")
    if light_bootstrap is None:
        light_bootstrap = True
    else:
        light_bootstrap = bool(light_bootstrap)
    try:
        browser = acquire_session(
            session_key,
            str(payload.get("proxyUrl") or ""),
            cookies,
            str(payload.get("referer") or bootstrap_url),
            bootstrap_url,
            remaining_timeout_ms(deadline),
            str(payload.get("userAgent") or ""),
            light_bootstrap=light_bootstrap,
        )
    except HTTPError:
        raise
    except Exception as error:
        return error_response(error)
    timeout_ms = remaining_timeout_ms(deadline)
    cfg = {
        "path": path,
        "method": method,
        "timeoutMs": timeout_ms,
        "signerModuleId": SIGNER_MODULE_ID,
    }
    try:
        def operation():
            # Bootstrap may leave a sparse challenge/cookie page; ensure SPA shell is present.
            try:
                current = browser.driver.current_url or ""
                ready = browser.driver.execute_script(
                    "return !!(globalThis.__grokBridgeRuntime && document.body && document.body.childNodes && document.body.childNodes.length >= 3);"
                )
                if ("grok.com" not in current) or (not ready):
                    browser.driver.get(bootstrap_url)
                    deadline_local = time.monotonic() + min(45.0, timeout_ms / 1000)
                    while time.monotonic() < deadline_local:
                        ready = browser.driver.execute_script(
                            "return !!(globalThis.__grokBridgeRuntime && document.body && document.body.childNodes && document.body.childNodes.length >= 3);"
                        )
                        if ready:
                            break
                        time.sleep(0.4)
            except Exception:
                pass
            browser.driver.set_script_timeout((timeout_ms / 1000) + 15)
            return browser.driver.execute_async_script(SIGN_SCRIPT, cfg)

        result = execute_browser_operation(browser, timeout_ms, operation)
        if not isinstance(result, dict):
            return encode_response({"error": "invalid browser result"}, 502)
        if result.get("error"):
            log_bridge_error("sign", RuntimeError(str(result.get("error"))))
            err_out = {"error": str(result.get("error"))[:300]}
            if result.get("cachedCount") is not None:
                err_out["cachedCount"] = result.get("cachedCount")
            if result.get("tried") is not None:
                err_out["tried"] = result.get("tried")
            if result.get("errors"):
                err_out["errors"] = result.get("errors")
            return encode_response(err_out, 502)
        statsig_id = str(result.get("statsigId") or "").strip()
        if not statsig_id or statsig_id.startswith("eDA6") or statsig_id.startswith("x0:"):
            # eDA6... is base64 of "x0:..." error fallback from older fetch path
            return encode_response({"error": "statsig signature unavailable", "detail": statsig_id[:120]}, 502)
        payload_out = {"statsigId": statsig_id, "path": path, "method": method}
        if result.get("source"):
            payload_out["source"] = result.get("source")
        if result.get("signerModuleId") is not None:
            payload_out["signerModuleId"] = result.get("signerModuleId")
        # Export jar for HTTP replay (cf_clearance + site cookies). Never log values.
        try:
            jar = browser.driver.get_cookies() or []
            parts = []
            names = []
            for item in jar:
                name = str(item.get("name") or "")
                value = str(item.get("value") or "")
                if not name or not value:
                    continue
                parts.append(f"{name}={value}")
                names.append(name)
            if parts:
                payload_out["cookie"] = "; ".join(parts)
                payload_out["cookieNames"] = names
                payload_out["hasCfClearance"] = "cf_clearance" in names
        except Exception as cookie_error:
            payload_out["cookieError"] = type(cookie_error).__name__
        return encode_response(payload_out)
    except Exception as error:
        log_bridge_error("sign", error)
        return encode_response({"error": type(error).__name__ + ": " + str(error)[:300]}, 502)
    finally:
        if not env_flag("BRIDGE_REUSE_SESSIONS", True):
            discard_session(browser)


@APP.post("/v1/fetch")
def fetch():
    if not authorized():
        raise HTTPError(401, "unauthorized")
    payload = json_body()
    validate_target(payload.get("url"), {"https"})
    method = str(payload.get("method") or "GET").upper()
    if method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
        raise HTTPError(400, "invalid method")
    timeout_ms = bounded_timeout_ms(payload.get("timeoutMs"), 120000)
    deadline = time.monotonic() + (timeout_ms / 1000)
    cookies = parse_cookies(payload.get("cookie"))
    try:
        browser = acquire_session(str(payload.get("sessionKey") or ""), str(payload.get("proxyUrl") or ""), cookies, str(payload.get("referer") or ""), str(payload.get("url") or ""), remaining_timeout_ms(deadline), str(payload.get("userAgent") or ""))
    except HTTPError:
        raise
    except Exception as error:
        return error_response(error)
    timeout_ms = remaining_timeout_ms(deadline)
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
        def operation():
            browser.driver.set_script_timeout((timeout_ms / 1000) + 15)
            return browser.driver.execute_async_script(script, cfg)

        result = execute_browser_operation(browser, timeout_ms, operation)
        if isinstance(result, dict) and result.get("error"):
            log_bridge_error("fetch", RuntimeError(str(result.get("error"))))
        return encode_response(result if isinstance(result, dict) else {"error": "invalid browser result"})
    except Exception as error:
        log_bridge_error("fetch", error)
        return encode_response({"error": type(error).__name__ + ": " + str(error)[:300]}, 502)
    finally:
        if not env_flag("BRIDGE_REUSE_SESSIONS", True):
            discard_session(browser)


@APP.post("/v1/websocket")
def websocket():
    if not authorized():
        raise HTTPError(401, "unauthorized")
    payload = json_body()
    validate_target(payload.get("url"), {"wss"})
    timeout_ms = bounded_timeout_ms(payload.get("timeoutMs"), 180000)
    deadline = time.monotonic() + (timeout_ms / 1000)
    idle_ms = min(max(int(payload.get("idleMs") or 5000), 500), 30000)
    expected = min(max(int(payload.get("expected") or 1), 1), 10)
    cookies = parse_cookies(payload.get("cookie"))
    try:
        browser = acquire_session(str(payload.get("sessionKey") or ""), str(payload.get("proxyUrl") or ""), cookies, str(payload.get("referer") or "https://grok.com/imagine"), str(payload.get("url") or ""), remaining_timeout_ms(deadline), str(payload.get("userAgent") or ""))
    except HTTPError:
        raise
    except Exception as error:
        return error_response(error)
    timeout_ms = remaining_timeout_ms(deadline)
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
        def operation():
            browser.driver.set_script_timeout((timeout_ms / 1000) + 15)
            return browser.driver.execute_async_script(script, cfg)

        result = execute_browser_operation(browser, timeout_ms, operation)
        if isinstance(result, dict) and result.get("error"):
            log_bridge_error("websocket", RuntimeError(str(result.get("error"))))
        frames = [base64.b64encode(str(value).encode()).decode() for value in (result or {}).get("frames", [])]
        return encode_response({"frames": frames, "error": (result or {}).get("error", "")})
    except Exception as error:
        log_bridge_error("websocket", error)
        return encode_response({"error": type(error).__name__ + ": " + str(error)[:300]}, 502)
    finally:
        if not env_flag("BRIDGE_REUSE_SESSIONS", True):
            discard_session(browser)


if __name__ == "__main__":
    if env_flag("BRIDGE_WARM_USER_AGENT", True):
        utils.get_user_agent()
    serve(APP, host="0.0.0.0", port=int(os.environ.get("PORT", "8192")), threads=max(2, int(os.environ.get("BRIDGE_THREADS", "2"))))
