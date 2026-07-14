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

    def quit(self):
        self.closed = True


class _BlockingDriver:
    def __init__(self):
        self.closed = False
        self.release = threading.Event()

    def quit(self):
        self.release.wait(5)
        self.closed = True


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
        self.assertEqual(1, len(result))


if __name__ == "__main__":
    unittest.main()
