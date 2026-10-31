"""Start the server:  python -m pantry  [--host 0.0.0.0] [--port 8790] [--browser]

On the Proxmox LXC this runs under systemd (deploy/pantry.service) with waitress.
On Windows, start.bat runs it locally for trying things out.
"""
from __future__ import annotations

import argparse
import logging
import os
import socket
import threading
import webbrowser
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import __version__
from . import db as dbm
from .app import create_app


def _setup_logging(data_dir: Path) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        h = RotatingFileHandler(data_dir / "ai.log", maxBytes=1_000_000, backupCount=2, encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logging.getLogger("pantry.ai").addHandler(h)
    except OSError as e:
        print("Warning: can't write the AI log:", e)


def _in_use(host: str, port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1" if host in ("0.0.0.0", "::") else host, port)) == 0


def main() -> None:
    ap = argparse.ArgumentParser(prog="pantry")
    ap.add_argument("--host", default=os.environ.get("PANTRY_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("PANTRY_PORT", "8790")))
    ap.add_argument("--browser", action="store_true", help="open the app in a browser")
    args = ap.parse_args()

    url = f"http://{'127.0.0.1' if args.host in ('0.0.0.0', '::') else args.host}:{args.port}"
    if _in_use(args.host, args.port):
        # Already running (e.g. started at login): just open it.
        print(f"Pantry is already running at {url}.")
        if args.browser:
            webbrowser.open(url)
        return

    app = create_app()
    data_dir = Path(app.config["DATA_DIR"])
    _setup_logging(data_dir)
    try:
        dbm.auto_backup(app.config["DB_PATH"], app.config["BACKUP_DIR"])
    except Exception as e:  # a failed backup should never stop the app
        print("Warning: automatic backup failed:", e)

    print(f"Pantry v{__version__} running at {url}")
    print(f"Database: {app.config['DB_PATH']}")
    if args.browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        from waitress import serve
    except ImportError:
        print("(waitress isn't installed; using Flask's built-in server)")
        app.run(host=args.host, port=args.port, debug=False, threaded=True, use_reloader=False)
    else:
        serve(app, host=args.host, port=args.port, threads=8, ident="pantry")


if __name__ == "__main__":
    main()
