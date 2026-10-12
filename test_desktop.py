"""Plain-assert checks for the desktop launcher (desktop.py).
Run: python3 test_desktop.py (or pytest).

Plain words: automatic self-checks for the desktop app's launcher - the part that
starts the app's server privately on your computer and shows it in a window. The
window itself is replaced by a stand-in; one test starts the REAL server.
"""

import http.server
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import types
import urllib.request
from contextlib import contextmanager
from pathlib import Path

import desktop

ROOT = Path(__file__).resolve().parent


@contextmanager
def _patched(obj, **attrs):
    # Temporarily replace attributes, always restoring them.
    old = {k: getattr(obj, k, None) for k in attrs}
    had = {k: hasattr(obj, k) for k in attrs}
    for k, v in attrs.items():
        setattr(obj, k, v)
    try:
        yield
    finally:
        for k in attrs:
            setattr(obj, k, old[k]) if had[k] else delattr(obj, k)


def test_free_port_is_usable_and_local():
    port = desktop.free_port()
    assert 1024 <= port <= 65535
    with socket.socket() as s:
        s.bind((desktop.HOST, port))                         # genuinely free


def test_the_server_is_private_quiet_and_offline_by_default():
    env = desktop.server_env({"CLAUDE_SDLC_ARCHITECT_KB_ROOT": "/data/kb", "CLAUDE_SDLC_ARCHITECT_RUNS_ROOT": "/data/runs"})
    assert env["STREAMLIT_SERVER_ADDRESS"] == "127.0.0.1"                 # this computer only
    assert env["STREAMLIT_BROWSER_GATHER_USAGE_STATS"] == "false"         # no usage statistics
    assert env["STREAMLIT_THEME_FONT"] == "sans serif" == env["STREAMLIT_THEME_HEADING_FONT"]   # no font download
    assert env["STREAMLIT_GLOBAL_DEVELOPMENT_MODE"] == "false" and env["STREAMLIT_SERVER_HEADLESS"] == "true"
    assert env[desktop.PARENT_PID_ENV] == str(os.getpid())
    assert env["CLAUDE_SDLC_ARCHITECT_KB_ROOT"] == "/data/kb"
    # Settings you already made win over the data-folder defaults; the safety settings always apply.
    os.environ["CLAUDE_SDLC_ARCHITECT_KB_ROOT"] = "/mine"
    try:
        env = desktop.server_env({"CLAUDE_SDLC_ARCHITECT_KB_ROOT": "/data/kb"})
        assert env["CLAUDE_SDLC_ARCHITECT_KB_ROOT"] == "/mine"
        os.environ["STREAMLIT_SERVER_ADDRESS"] = "0.0.0.0"
        assert desktop.server_env()["STREAMLIT_SERVER_ADDRESS"] == "127.0.0.1"
    finally:
        os.environ.pop("CLAUDE_SDLC_ARCHITECT_KB_ROOT", None)
        os.environ.pop("STREAMLIT_SERVER_ADDRESS", None)


def test_server_command_for_a_checkout_and_for_the_packaged_app():
    cmd = desktop.server_command(8765)
    assert cmd[0] == sys.executable and cmd[-2:] == ["--serve", "8765"] and cmd[1].endswith("desktop.py")
    with _patched(sys, frozen=True):
        assert desktop.server_command(8765) == [sys.executable, "--serve", "8765"]


def test_data_folder_location_per_platform():
    home = Path.home()
    for platform, expected in (("darwin", home / "Library" / "Application Support" / "Blueprint"),
                               ("linux", home / ".local" / "share" / "blueprint")):
        saved = os.environ.pop("XDG_DATA_HOME", None)
        try:
            with _patched(sys, platform=platform):
                assert desktop.default_data_dir() == expected
        finally:
            if saved is not None:
                os.environ["XDG_DATA_HOME"] = saved


def test_first_launch_copies_the_demo_knowledge_base_once_and_never_touches_it_again():
    with tempfile.TemporaryDirectory() as tmp:
        bundle, data = Path(tmp) / "bundle", Path(tmp) / "data"
        (bundle / "knowledge_base" / "demo").mkdir(parents=True)
        (bundle / "knowledge_base" / "demo" / "a.md").write_text("# a")
        (bundle / "knowledge_base" / "__pycache__").mkdir()
        (bundle / ".env.example").write_text("JIRA_BASE_URL=")

        env = desktop.ensure_data_dir(data, bundle)
        assert (data / "knowledge_bases" / "demo" / "a.md").read_text() == "# a"
        assert not (data / "knowledge_bases" / "__pycache__").exists()
        assert (data / ".env.example").is_file() and (data / "runs").is_dir() and (data / "memory").is_dir()
        assert env["CLAUDE_SDLC_ARCHITECT_HOME"] == str(data)
        assert env["CLAUDE_SDLC_ARCHITECT_KB_ROOT"] == str(data / "knowledge_bases")

        # The user deletes the demo and adds their own: a later launch leaves it all alone.
        import shutil
        shutil.rmtree(data / "knowledge_bases" / "demo")
        (data / "knowledge_bases" / "mine").mkdir()
        (data / ".env.example").write_text("EDITED")
        desktop.ensure_data_dir(data, bundle)
        assert sorted(p.name for p in (data / "knowledge_bases").iterdir()) == ["mine"]
        assert (data / ".env.example").read_text() == "EDITED"


class _Health(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"ok")

    def log_message(self, *a):
        pass


def test_waiting_for_the_server():
    # Ready: a server that answers "ok" (started a moment late, as a real one is).
    port = desktop.free_port()
    def late():
        time.sleep(0.6)
        srv = http.server.HTTPServer((desktop.HOST, port), _Health)
        srv.timeout = 5
        srv.handle_request(); srv.handle_request(); srv.server_close()
    threading.Thread(target=late, daemon=True).start()
    desktop.wait_until_ready(port, timeout=10, poll=0.1)

    # Never comes up: a clear error after the timeout.
    try:
        desktop.wait_until_ready(desktop.free_port(), timeout=0.6, poll=0.1)
        raise AssertionError("expected ServerError")
    except desktop.ServerError as e:
        assert "did not start" in str(e)

    # The process dies while starting: reported at once, not after the full timeout.
    dead = subprocess.Popen([sys.executable, "-c", "import sys; sys.exit(3)"])
    dead.wait()
    started = time.monotonic()
    try:
        desktop.wait_until_ready(desktop.free_port(), timeout=30, proc=dead, poll=0.1)
        raise AssertionError("expected ServerError")
    except desktop.ServerError as e:
        assert "exit code 3" in str(e) and time.monotonic() - started < 5


def test_stopping_the_server():
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    desktop.stop_server(proc)
    assert proc.poll() is not None
    desktop.stop_server(proc)            # already stopped: fine
    desktop.stop_server(None)            # never started: fine


def test_error_page_shows_the_reason_and_the_log_safely():
    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "desktop.log"
        log.write_text("line1\n<script>alert(1)</script>\nTraceback boom\n")
        page = desktop.error_html("It <broke>", log)
    assert "could not start" in page and "Traceback boom" in page
    assert "<script>" not in page and "It &lt;broke&gt;" in page and "&lt;script&gt;" in page   # escaped
    assert "no log available" in desktop.error_html("x", Path("/nonexistent/desktop.log"))


class _FakeProc:
    returncode = None
    def poll(self): return None
    def wait(self): return 0


def _args(**kw):
    return types.SimpleNamespace(data_dir=kw.get("data_dir"), serve=None, self_test=False, with_ai=False)


def test_without_pywebview_it_opens_the_browser_instead():
    opened = []
    with tempfile.TemporaryDirectory() as tmp, _patched(
        desktop, start_server=lambda *a, **k: _FakeProc(), wait_until_ready=lambda *a, **k: None,
        stop_server=lambda p: None,
    ), _patched(desktop.webbrowser, open=lambda url: opened.append(url)):
        saved = sys.modules.get("webview")
        sys.modules["webview"] = None                       # makes `import webview` fail
        try:
            assert desktop.run(_args(data_dir=tmp)) == 0
        finally:
            sys.modules.pop("webview", None)
            if saved is not None:
                sys.modules["webview"] = saved
    assert len(opened) == 1 and opened[0].startswith("http://127.0.0.1:")


def _fake_webview(start_hook=None):
    shown = {}
    window = types.SimpleNamespace(load_url=lambda u: shown.update(url=u), load_html=lambda h: shown.update(html=h))
    mod = types.ModuleType("webview")
    mod.settings = {}
    mod.create_window = lambda title, **kw: shown.update(title=title, **kw) or window
    mod.start = lambda func=None, *a, **k: func() if func else None
    return mod, shown


def test_the_window_is_configured_safely_and_loads_the_app_when_ready():
    mod, shown = _fake_webview()
    stopped = []
    with tempfile.TemporaryDirectory() as tmp, _patched(
        desktop, start_server=lambda *a, **k: _FakeProc(), wait_until_ready=lambda *a, **k: None,
        stop_server=lambda p: stopped.append(p),
    ):
        sys.modules["webview"] = mod
        try:
            assert desktop.run(_args(data_dir=tmp)) == 0
        finally:
            sys.modules.pop("webview", None)
    assert mod.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] is True      # Jira links open in your browser
    assert mod.settings["ALLOW_FILE_URLS"] is False                    # no local file access
    assert shown["title"] == "Blueprint" and "Starting" in shown["html"]
    assert shown["url"].startswith("http://127.0.0.1:")
    assert stopped                                                     # closing the window stops the server


def test_if_the_server_cannot_start_the_window_says_why():
    mod, shown = _fake_webview()
    def fail(*a, **k):
        raise desktop.ServerError("boom")
    with tempfile.TemporaryDirectory() as tmp, _patched(
        desktop, start_server=lambda *a, **k: _FakeProc(), wait_until_ready=fail, stop_server=lambda p: None,
    ):
        sys.modules["webview"] = mod
        try:
            desktop.run(_args(data_dir=tmp))
        finally:
            sys.modules.pop("webview", None)
    assert "url" not in shown and "could not start" in shown["html"] and "boom" in shown["html"]


def test_settings_in_the_data_folders_env_file_are_read():
    # Packaged app: the user's .env lives in the data folder. A real environment variable still wins.
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / ".env").write_text("BP_TEST_FROM_HOME=from-file\nBP_TEST_OVERRIDDEN=from-file\n")
        code = "import config, os; print(os.environ.get('BP_TEST_FROM_HOME'), os.environ.get('BP_TEST_OVERRIDDEN'))"
        env = {**os.environ, "CLAUDE_SDLC_ARCHITECT_HOME": tmp, "BP_TEST_OVERRIDDEN": "from-environment"}
        out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
        assert out.stdout.split() == ["from-file", "from-environment"], out.stderr


def test_the_real_server_runs_the_app_and_is_not_reachable_from_the_network():
    with tempfile.TemporaryDirectory() as tmp:
        data_env = desktop.ensure_data_dir(Path(tmp) / "data", ROOT)
        port = desktop.free_port()
        proc = desktop.start_server(port, data_env, Path(tmp) / "server.log")
        try:
            desktop.wait_until_ready(port, proc=proc)
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=10) as resp:
                assert resp.status == 200

            # Not exposed on the network: connecting through this machine's LAN address is refused.
            lan = next((ip for ip in {a[4][0] for a in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)}
                        if not ip.startswith("127.")), None)
            if lan:
                with socket.socket() as s:
                    s.settimeout(3)
                    assert s.connect_ex((lan, port)) != 0, f"server answered on {lan}:{port}"

            # A real browser-style session: the Blueprint page itself runs without an error.
            try:
                import asyncio
                from websockets.asyncio.client import connect
                from streamlit.proto.BackMsg_pb2 import BackMsg
                from streamlit.proto.ClientState_pb2 import ClientState
                from streamlit.proto.ForwardMsg_pb2 import ForwardMsg
            except ImportError:
                print("   (skipping the page-run part: websockets/streamlit protos not importable)")
                return

            async def session():
                ws = await connect(f"ws://127.0.0.1:{port}/_stcore/stream", subprotocols=["streamlit"], max_size=None)
                msg = BackMsg(); msg.rerun_script.CopyFrom(ClientState(query_string=""))
                await ws.send(msg.SerializeToString())
                errors, title = [], False
                try:
                    while True:
                        fm = ForwardMsg(); fm.ParseFromString(await asyncio.wait_for(ws.recv(), timeout=60))
                        kind = fm.WhichOneof("type")
                        if kind == "delta" and fm.delta.WhichOneof("type") == "new_element":
                            el = fm.delta.new_element
                            if el.WhichOneof("type") == "exception":
                                errors.append(el.exception.message)
                            if el.WhichOneof("type") == "heading" and el.heading.body == "Blueprint":
                                title = True
                        elif kind == "script_finished":
                            return fm.script_finished, errors, title
                finally:
                    await ws.close()

            finished, errors, title = asyncio.run(session())
            assert finished == 0 and not errors and title, (finished, errors, title)
        finally:
            desktop.stop_server(proc)
        assert proc.poll() is not None


# When run directly, execute every test_ function and print "ok" for each.
if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
