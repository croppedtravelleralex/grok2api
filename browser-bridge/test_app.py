import importlib.util
import sys
import threading
import time
import types
import unittest
from pathlib import Path


class _Bottle:
    def get(self, _path):
        return lambda function: function

    def post(self, _path):
        return lambda function: function


class _Response:
    def set_header(self, _name, _value):
        return None


class _Driver:
    def __init__(self):
        self.closed = False

    def execute_cdp_cmd(self, _name, _payload):
        return None

    def set_script_timeout(self, _timeout):
        return None

    def execute_async_script(self, _script, _cfg):
        return {"status": 200, "headers": {}, "body": ""}

    def quit(self):
        self.closed = True


class _BlockingDriver:
    def __init__(self):
        self.closed = False
        self.release = threading.Event()

    def quit(self):
        self.release.wait(5)
        self.closed = True


class _BlockingScriptDriver:
    def __init__(self):
        self.closed = False
        self.started = threading.Event()
        self.release = threading.Event()

    def set_script_timeout(self, _timeout):
        return None

    def execute_async_script(self, _script, _cfg):
        self.started.set()
        self.release.wait(5)
        return {"status": 200}

    def quit(self):
        self.closed = True
        self.release.set()


def _load_app():
    modules = {
        "bottle": types.SimpleNamespace(
            Bottle=_Bottle,
            HTTPError=RuntimeError,
            request=types.SimpleNamespace(headers={}, json={}),
            response=_Response(),
        ),
        "waitress": types.SimpleNamespace(serve=lambda *_args, **_kwargs: None),
        "utils": types.SimpleNamespace(get_webdriver=lambda _proxy: None, get_user_agent=lambda: None),
        "dtos": types.SimpleNamespace(V1RequestBase=dict),
        "flaresolverr_service": types.SimpleNamespace(_evil_logic=lambda *_args, **_kwargs: None),
    }
    previous = {name: sys.modules.get(name) for name in modules}
    sys.modules.update(modules)
    try:
        path = Path(__file__).with_name("app.py")
        spec = importlib.util.spec_from_file_location("browser_bridge_app_test", path)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        return module
    finally:
        for name, value in previous.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


class HealthCleanupTest(unittest.TestCase):
    def test_health_closes_expired_browser_sessions(self):
        app = _load_app()
        driver = _Driver()
        orphan_cleanup = []
        app._terminate_orphaned_browser_processes = lambda: orphan_cleanup.append(True)
        session = app.BrowserSession(driver, "direct://", "https://grok.com/")
        session.last_used = time.monotonic() - app.SESSION_TTL - 1
        app.SESSIONS["expired"] = session

        payload = app.health()

        self.assertEqual('{"status":"ok","sessions":0}', payload)
        self.assertTrue(driver.closed)
        self.assertEqual(0, len(app.SESSIONS))
        self.assertEqual([True], orphan_cleanup)

    def test_health_force_closes_a_stuck_driver_within_its_budget(self):
        app = _load_app()
        driver = _BlockingDriver()
        session = app.BrowserSession(driver, "direct://", "https://grok.com/")
        session.last_used = time.monotonic() - app.SESSION_TTL - 1
        app.SESSIONS["expired"] = session
        app.CLOSE_TIMEOUT = 0.01
        app._terminate_driver_processes = lambda value: value.release.set()

        started = time.monotonic()
        payload = app.health()

        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual('{"status":"ok","sessions":0}', payload)
        self.assertTrue(driver.closed)

    def test_health_does_not_wait_for_browser_bootstrap(self):
        app = _load_app()
        driver = _Driver()
        bootstrap_started = threading.Event()
        release_bootstrap = threading.Event()
        orphan_cleanup = []
        app._terminate_orphaned_browser_processes = lambda: orphan_cleanup.append(True)

        def get_webdriver(_proxy):
            bootstrap_started.set()
            release_bootstrap.wait(2)
            return driver

        app.utils.get_webdriver = get_webdriver
        result = []
        worker = threading.Thread(
            target=lambda: result.append(app.acquire_session("account-1", "direct://", [], "", "https://grok.com/rest/app-chat/conversations/new")),
            daemon=True,
        )
        worker.start()
        self.assertTrue(bootstrap_started.wait(0.5))
        threading.Timer(0.25, release_bootstrap.set).start()

        started = time.monotonic()
        payload = app.health()
        elapsed = time.monotonic() - started
        worker.join(1)

        self.assertLess(elapsed, 0.1)
        self.assertEqual('{"status":"ok","sessions":0}', payload)
        self.assertEqual([], orphan_cleanup)
        self.assertEqual(1, len(result))

    def test_boolean_environment_flag_can_disable_user_agent_warmup(self):
        app = _load_app()
        app.os.environ["BRIDGE_WARM_USER_AGENT"] = "false"
        try:
            self.assertFalse(app.env_flag("BRIDGE_WARM_USER_AGENT", True))
        finally:
            app.os.environ.pop("BRIDGE_WARM_USER_AGENT", None)

    def test_stuck_browser_operation_is_evicted_and_force_closed(self):
        app = _load_app()
        driver = _BlockingScriptDriver()
        session = app.BrowserSession(driver, "direct://", "https://grok.com/")
        app.SESSIONS["account-1"] = session
        app.OPERATION_GRACE_SECONDS = 0.01
        app.CLOSE_TIMEOUT = 0.01
        app._terminate_orphaned_browser_processes = lambda: None

        started = time.monotonic()
        with self.assertRaises(app.BrowserOperationTimeout):
            app.execute_browser_operation(
                session,
                10,
                lambda: driver.execute_async_script("return", {}),
            )

        self.assertLess(time.monotonic() - started, 0.5)
        self.assertTrue(driver.started.is_set())
        self.assertTrue(driver.closed)
        self.assertEqual(0, len(app.SESSIONS))

    def test_operation_timeout_is_capped_for_low_resource_hosts(self):
        app = _load_app()
        app.MAX_OPERATION_MS = 90000

        self.assertEqual(90000, app.bounded_timeout_ms(180000, 120000))
        self.assertEqual(1000, app.bounded_timeout_ms(1, 120000))

    def test_non_reusable_mode_closes_browser_after_successful_fetch(self):
        app = _load_app()
        driver = _Driver()
        session = app.BrowserSession(driver, "direct://", "https://grok.com/")
        app.SESSIONS["account-1"] = session
        app.authorized = lambda: True
        app.json_body = lambda: {
            "sessionKey": "account-1",
            "url": "https://grok.com/rest/rate-limits",
            "method": "POST",
            "timeoutMs": 1000,
        }
        app.acquire_session = lambda *_args: session
        app._terminate_orphaned_browser_processes = lambda: None
        app.os.environ["BRIDGE_REUSE_SESSIONS"] = "false"
        try:
            payload = app.fetch()
        finally:
            app.os.environ.pop("BRIDGE_REUSE_SESSIONS", None)

        self.assertEqual('{"status":200,"headers":{},"body":""}', payload)
        self.assertTrue(driver.closed)
        self.assertEqual(0, len(app.SESSIONS))


if __name__ == "__main__":
    unittest.main()
