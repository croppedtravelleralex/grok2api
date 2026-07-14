import importlib.util
import sys
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

    def quit(self):
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
        session = app.BrowserSession(driver, "direct://", "https://grok.com/")
        session.last_used = time.monotonic() - app.SESSION_TTL - 1
        app.SESSIONS["expired"] = session

        payload = app.health()

        self.assertEqual('{"status":"ok","sessions":0}', payload)
        self.assertTrue(driver.closed)
        self.assertEqual(0, len(app.SESSIONS))


if __name__ == "__main__":
    unittest.main()
