"""
Blueprint as a desktop app: the same Streamlit UI, shown in its own window.

Plain words: running `streamlit run streamlit_app.py` opens the app in a browser
tab. This launcher instead starts that same app quietly in the background and
shows it in a native window, like any other program. Closing the window stops
everything.

What it does, in order:
  1. picks a free port and starts the Streamlit server as a separate process,
     listening on THIS computer only (127.0.0.1 -- never on the network), with
     Streamlit's usage statistics off and no outside font download;
  2. opens a window (pywebview) showing "Starting..." and, once the server
     answers, loads the app into it;
  3. when the window closes, stops the server.

If pywebview is not installed it falls back to opening your normal browser, so
`python3 desktop.py` always works.

Where things are kept:
  * Run from a checkout (`python3 desktop.py`): exactly where they always were
    (runs/, memory/, knowledge_base/, .env in the project folder).
  * Packaged app (built by packaging/build_desktop.sh): the app itself is
    read-only, so your data lives in a per-user folder instead -- on a Mac,
    ~/Library/Application Support/Blueprint/ (runs, memory, your knowledge bases
    and your .env). The demo knowledge base is copied there on first launch.

Internal modes (used by the app itself and by tests):
  --serve PORT   run just the Streamlit server (the window starts this)
  --self-test    check the install works (modules, bundled files, bundled Claude
                 CLI, server start-up) without opening a window; add --with-ai
                 to also make one tiny real call to Claude
"""

import argparse
import atexit
import multiprocessing
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from typing import Dict, List, Optional

APP_NAME = "Blueprint"
HOST = "127.0.0.1"                 # this computer only
START_TIMEOUT_SECONDS = 90         # how long to wait for the server to come up
PARENT_PID_ENV = "BLUEPRINT_PARENT_PID"

# Settings given to the Streamlit server (environment variables Streamlit reads).
# They override the project's config file, so the desktop app is always:
#   * reachable only from this computer
#   * not sending anonymous usage statistics
#   * not downloading a font from Google (it uses the system font instead)
#   * not watching source files for changes, and not in Streamlit's dev mode
SERVER_SETTINGS = {
    "STREAMLIT_SERVER_ADDRESS": HOST,
    "STREAMLIT_SERVER_HEADLESS": "true",
    "STREAMLIT_BROWSER_GATHER_USAGE_STATS": "false",
    "STREAMLIT_SERVER_FILE_WATCHER_TYPE": "none",
    "STREAMLIT_GLOBAL_DEVELOPMENT_MODE": "false",
    "STREAMLIT_THEME_FONT": "sans serif",
    "STREAMLIT_THEME_HEADING_FONT": "sans serif",
}


class ServerError(Exception):
    """The Streamlit server did not start, or stopped."""


# ------------------------------------------------------------
# Where things are
# ------------------------------------------------------------

def is_frozen() -> bool:
    # True when running as the packaged app (built with PyInstaller).
    return bool(getattr(sys, "frozen", False))


def bundle_root() -> Path:
    """The folder holding the app's own files (streamlit_app.py, skills/, ...):
    the project folder normally, or the inside of the package when packaged."""
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


def default_data_dir() -> Path:
    """The per-user folder the PACKAGED app keeps its data in."""
    home = Path.home()
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / APP_NAME
    if sys.platform.startswith("win"):
        return Path(os.environ.get("APPDATA", home / "AppData" / "Roaming")) / APP_NAME
    return Path(os.environ.get("XDG_DATA_HOME", home / ".local" / "share")) / APP_NAME.lower()


def ensure_data_dir(data_dir: Path, bundle: Path) -> Dict[str, str]:
    """Create the data folder if needed and return the environment settings that
    point the app at it. On the very first launch the demo knowledge base is
    copied in; after that the folder is never touched, so anything you add,
    change or delete there is left alone. Also leaves a `.env.example` to copy
    to `.env` for the Jira settings."""
    knowledge_bases = data_dir / "knowledge_bases"
    first_launch = not knowledge_bases.exists()
    for folder in (data_dir, data_dir / "runs", data_dir / "memory", knowledge_bases):
        folder.mkdir(parents=True, exist_ok=True)

    if first_launch and (bundle / "knowledge_base").is_dir():
        for demo in (bundle / "knowledge_base").iterdir():
            if demo.is_dir() and not demo.name.startswith((".", "_")):
                shutil.copytree(demo, knowledge_bases / demo.name)

    example, target = bundle / ".env.example", data_dir / ".env.example"
    if example.is_file() and not target.exists():
        shutil.copy2(example, target)

    return {
        "CLAUDE_SDLC_ARCHITECT_HOME": str(data_dir),
        "CLAUDE_SDLC_ARCHITECT_KB_ROOT": str(knowledge_bases),
        "CLAUDE_SDLC_ARCHITECT_RUNS_ROOT": str(data_dir / "runs"),
        "CLAUDE_SDLC_ARCHITECT_MEMORY_DIR": str(data_dir / "memory"),
    }


# ------------------------------------------------------------
# The server
# ------------------------------------------------------------

def free_port() -> int:
    # Ask the operating system for a port nobody is using.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((HOST, 0))
        return s.getsockname()[1]


def server_env(data_env: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """The environment for the server process: the current one, plus the data
    folder settings (only where you have not set your own), plus the fixed
    safety settings above."""
    env = dict(os.environ)
    for key, value in (data_env or {}).items():
        env.setdefault(key, value)
    env.update(SERVER_SETTINGS)
    env[PARENT_PID_ENV] = str(os.getpid())
    return env


def server_command(port: int) -> List[str]:
    # The packaged app starts itself again in --serve mode; from a checkout we
    # run this file again.
    if is_frozen():
        return [sys.executable, "--serve", str(port)]
    return [sys.executable, str(Path(__file__).resolve()), "--serve", str(port)]


def start_server(port: int, data_env: Optional[Dict[str, str]], log_path: Path) -> subprocess.Popen:
    """Start the Streamlit server in the background. Its output goes to a log
    file, which is shown if the app fails to start."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = open(log_path, "ab")
    return subprocess.Popen(
        server_command(port),
        env=server_env(data_env),
        cwd=str(bundle_root()),            # so Streamlit finds .streamlit/config.toml
        stdout=log,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
    )


def wait_until_ready(port: int, timeout: float = START_TIMEOUT_SECONDS,
                     proc: Optional[subprocess.Popen] = None, poll: float = 0.3) -> None:
    """Wait for the server to answer. Raises ServerError if it exits early or
    does not come up in time."""
    url = f"http://{HOST}:{port}/_stcore/health"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc is not None and proc.poll() is not None:
            raise ServerError(f"The app's server stopped while starting (exit code {proc.returncode}).")
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if resp.read().decode("utf-8", "replace").strip().lower() == "ok":
                    return
        except (urllib.error.URLError, OSError, ValueError):
            pass
        time.sleep(poll)
    raise ServerError(f"The app's server did not start within {timeout:.0f} seconds.")


def stop_server(proc: Optional[subprocess.Popen]) -> None:
    # Ask nicely, then insist.
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def _exit_when_parent_dies() -> None:
    """In --serve mode: if the window process disappears without cleaning up
    (it crashed or was force-quit), shut this server down too instead of
    leaving it running unseen."""
    parent = int(os.environ.get(PARENT_PID_ENV, "0") or 0)
    if not parent:
        return

    def watch() -> None:
        while True:
            time.sleep(2)
            if os.getppid() != parent:
                os._exit(0)

    threading.Thread(target=watch, daemon=True).start()


def serve(port: int) -> None:
    """Run the Streamlit server in THIS process (the --serve mode)."""
    _exit_when_parent_dies()
    sys.argv = [
        "streamlit", "run", str(bundle_root() / "streamlit_app.py"),
        "--server.port", str(port),
        "--server.address", HOST,
        "--server.headless", "true",
        "--browser.gatherUsageStats", "false",
        "--global.developmentMode", "false",
    ]
    from streamlit.web import cli as streamlit_cli
    sys.exit(streamlit_cli.main())


# ------------------------------------------------------------
# The window
# ------------------------------------------------------------

SPLASH_HTML = """<!doctype html><meta charset="utf-8"><title>Blueprint</title>
<body style="margin:0;height:100vh;display:flex;align-items:center;justify-content:center;
font-family:-apple-system,system-ui,sans-serif;color:#1B1F2A;background:#fff">
<div style="text-align:center"><div style="font-size:28px;font-weight:700;letter-spacing:-0.02em">Blueprint</div>
<div style="margin-top:10px;color:#6B7280">Starting…</div></div></body>"""


def error_html(message: str, log_path: Path) -> str:
    # What the window shows if the server cannot start: the reason, plus the
    # tail of the server's log so the cause is visible without hunting for files.
    try:
        tail = "".join(log_path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)[-25:])
    except OSError:
        tail = "(no log available)"
    esc = lambda t: t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return (
        '<!doctype html><meta charset="utf-8"><body style="font-family:-apple-system,system-ui,sans-serif;'
        'margin:32px;color:#1B1F2A"><h2>Blueprint could not start</h2>'
        f"<p>{esc(message)}</p><p style='color:#6B7280'>Log: {esc(str(log_path))}</p>"
        f"<pre style='background:#F5F6FB;padding:12px;border-radius:8px;overflow:auto'>{esc(tail)}</pre></body>"
    )


def run(args: argparse.Namespace) -> int:
    """Start the server and show the app in a window (or the browser)."""
    data_dir = Path(args.data_dir) if args.data_dir else (default_data_dir() if is_frozen() else None)
    data_env = ensure_data_dir(data_dir, bundle_root()) if data_dir else None
    log_path = (data_dir or bundle_root()) / "desktop.log"

    port = free_port()
    proc = start_server(port, data_env, log_path)
    atexit.register(stop_server, proc)
    url = f"http://{HOST}:{port}"

    try:
        import webview
    except ImportError:
        print("pywebview is not installed, so opening your browser instead "
              "(install it with: pip install -r requirements-desktop.txt).")
        wait_until_ready(port, proc=proc)
        webbrowser.open(url)
        print(f"Blueprint is running at {url}  (press Ctrl-C to stop)")
        try:
            proc.wait()
        except KeyboardInterrupt:
            pass
        return 0

    # Links that open a new tab (for example a Jira ticket) go to your normal
    # browser, never into this window; local file access is off.
    webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
    webview.settings["ALLOW_FILE_URLS"] = False
    # Download buttons (the requirement / design Word and PDF files) must SAVE the file via a
    # save dialog. Without this the window navigates to the file itself and cannot get back.
    webview.settings["ALLOW_DOWNLOADS"] = True

    window = webview.create_window(APP_NAME, html=SPLASH_HTML, width=1280, height=860, min_size=(900, 600))

    def show_app_when_ready() -> None:
        try:
            wait_until_ready(port, proc=proc)
            window.load_url(url)
        except ServerError as exc:
            window.load_html(error_html(str(exc), log_path))

    webview.start(show_app_when_ready)       # returns when the window is closed
    stop_server(proc)
    return 0


# ------------------------------------------------------------
# Self-test (also used to verify a freshly built app)
# ------------------------------------------------------------

def self_test(with_ai: bool = False) -> int:
    """Check the install works, without opening a window. Prints one line per
    check and returns 0 only if all passed."""
    results: List[tuple] = []

    def check(name: str, fn) -> None:
        try:
            detail = fn() or ""
            results.append((True, name, detail))
        except Exception as exc:  # noqa: BLE001 -- reported, not hidden
            results.append((False, name, f"{type(exc).__name__}: {exc}"))

    root = bundle_root()

    def pipeline_imports():
        # Importing these also tells the packager which modules to include.
        import agents.design, agents.quality, agents.release, agents.requirement, agents.discuss  # noqa: F401
        import config, llm, run_log                                                   # noqa: F401
        import hive.blackboard, hive.events, hive.memory                              # noqa: F401
        import knowledge_base.loader, orchestrator.graph                              # noqa: F401
        import tools.issue_tracker, tools.jira_client, tools.requirement_doc          # noqa: F401
        import tools.design_doc, tools.diagrams, tools.confluence_client, tools.fonts  # noqa: F401
        import tools.test_plan_doc                                                    # noqa: F401
        return "all pipeline modules import"

    def bundled_files():
        needed = ["streamlit_app.py", ".streamlit/config.toml"] + [
            f"skills/{n}.md" for n in ("requirement", "design", "quality", "release", "discuss")]
        missing = [n for n in needed if not (root / n).is_file()]
        if missing:
            raise FileNotFoundError("missing: " + ", ".join(missing))
        return f"{len(needed)} files present"

    def requirement_documents():
        # Make a real Word file and a real PDF, so a missing library or font shows up here.
        import zipfile
        from io import BytesIO
        from tools import requirement_doc
        sample = {"title": "Self-test", "business_goal": "Check export", "scope_summary": "Scope",
                  "acceptance_criteria": ["It works."], "out_of_scope": [], "clarifying_questions": []}
        docx = requirement_doc.build(sample, {}, "docx")[0]
        if "word/document.xml" not in zipfile.ZipFile(BytesIO(docx)).namelist():
            raise RuntimeError("the .docx is not a valid Word file")
        pdf = requirement_doc.build(sample, {}, "pdf")[0]
        if not pdf.startswith(b"%PDF") or b"%%EOF" not in pdf[-1024:]:
            raise RuntimeError("the PDF is not valid")
        return f"Word ({len(docx)} bytes) and PDF ({len(pdf)} bytes) were made"

    def test_plan_workbook():
        # Make a real Excel workbook, so a missing library shows up here.
        import zipfile
        from io import BytesIO
        from tools import test_plan_doc
        plan = {"summary": "Self-test", "test_cases": [{
            "id": "TC-001", "title": "It works", "requirement_refs": ["AC-1"], "component": "App", "type": "Functional",
            "priority": "High", "preconditions": "", "steps": ["Open it"], "test_data": "", "expected_result": "It opens"}]}
        data = test_plan_doc.build({"title": "Self-test", "acceptance_criteria": ["It works."]}, plan, {})[0]
        if "xl/workbook.xml" not in zipfile.ZipFile(BytesIO(data)).namelist():
            raise RuntimeError("the .xlsx is not a valid Excel file")
        return f"Excel workbook ({len(data)} bytes) was made"

    def design_documents():
        # Draw the diagrams and make the design Word file and PDF, so a missing library shows up here.
        import zipfile
        from io import BytesIO
        from tools import design_doc, diagrams
        comps = [{"name": "Web App", "kind": "client", "status": "new", "responsibility": "UI"},
                 {"name": "Orders API", "kind": "service", "status": "new", "responsibility": "Orders"},
                 {"name": "Orders DB", "kind": "database", "status": "new", "responsibility": "Storage"}]
        conns = [{"from": "Web App", "to": "Orders API", "label": "places order", "kind": "http", "status": "new"},
                 {"from": "Orders API", "to": "Orders DB", "label": "reads/writes", "kind": "db", "status": "new"}]
        png = diagrams.architecture_diagram(comps, conns, "Self-test")
        if not png.startswith(b"\x89PNG"):
            raise RuntimeError("the diagram is not a PNG")
        design = {"change_required": True, "overview": "Self-test", "components": comps, "connections": conns,
                  "flows": [], "affected_applications": [], "new_applications_needed": [], "dependency_order": []}
        docx = design_doc.build({"title": "Self-test"}, design, {}, None, "docx")[0]
        if "word/document.xml" not in zipfile.ZipFile(BytesIO(docx)).namelist():
            raise RuntimeError("the design .docx is not a valid Word file")
        pdf = design_doc.build({"title": "Self-test"}, design, {}, None, "pdf")[0]
        if not pdf.startswith(b"%PDF"):
            raise RuntimeError("the design PDF is not valid")
        return f"diagram, Word ({len(docx)} bytes) and PDF ({len(pdf)} bytes) were made"

    def claude_cli():
        import claude_agent_sdk
        cli = Path(claude_agent_sdk.__file__).parent / "_bundled" / ("claude.exe" if sys.platform.startswith("win") else "claude")
        if not cli.is_file():
            raise FileNotFoundError(f"bundled Claude CLI not found at {cli}")
        out = subprocess.run([str(cli), "--version"], capture_output=True, text=True, timeout=60)
        if out.returncode != 0:
            raise RuntimeError(f"`claude --version` exited {out.returncode}: {out.stderr.strip()[:200]}")
        return f"runs ({out.stdout.strip()})"

    def data_folder():
        with tempfile.TemporaryDirectory() as tmp:
            env = ensure_data_dir(Path(tmp) / "data", root)
            kbs = sorted(p.name for p in Path(env["CLAUDE_SDLC_ARCHITECT_KB_ROOT"]).iterdir())
            return f"created; demo knowledge bases copied: {kbs or 'none bundled'}"

    def server_starts():
        with tempfile.TemporaryDirectory() as tmp:
            data_env = ensure_data_dir(Path(tmp) / "data", root)
            port = free_port()
            proc = start_server(port, data_env, Path(tmp) / "server.log")
            try:
                wait_until_ready(port, proc=proc)
                with urllib.request.urlopen(f"http://{HOST}:{port}/", timeout=10) as resp:
                    if resp.status != 200:
                        raise RuntimeError(f"home page returned {resp.status}")
            except ServerError:
                print((Path(tmp) / "server.log").read_text(errors="replace")[-1500:])
                raise
            finally:
                stop_server(proc)
            return f"started on {HOST}:{port} and served the home page"

    check("pipeline modules", pipeline_imports)
    check("bundled app files", bundled_files)
    check("requirement documents", requirement_documents)
    check("test plan workbook", test_plan_workbook)
    check("design documents", design_documents)
    check("bundled Claude CLI", claude_cli)
    check("data folder", data_folder)
    check("server start-up", server_starts)
    if with_ai:
        def ai_call():
            import llm
            answer = llm.call_llm_json('Reply with the JSON {"ok": true} and nothing else.', "Self-test",
                                       required_keys=["ok"])
            return f"Claude answered: {answer}"
        check("real call to Claude", ai_call)

    for ok, name, detail in results:
        print(f"{'PASS' if ok else 'FAIL'}  {name}: {detail}")
    return 0 if all(ok for ok, _, _ in results) else 1


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Blueprint desktop app")
    parser.add_argument("--serve", type=int, metavar="PORT", help=argparse.SUPPRESS)
    parser.add_argument("--self-test", action="store_true", help="check the install works, then exit")
    parser.add_argument("--with-ai", action="store_true", help="with --self-test: also make one real call to Claude")
    parser.add_argument("--data-dir", metavar="DIR", help="keep runs, memory, knowledge bases and .env in this folder")
    args = parser.parse_args(argv)

    if args.serve:
        serve(args.serve)
        return 0
    if args.self_test:
        return self_test(with_ai=args.with_ai)
    return run(args)


if __name__ == "__main__":
    multiprocessing.freeze_support()      # needed by packaged apps
    sys.exit(main())
