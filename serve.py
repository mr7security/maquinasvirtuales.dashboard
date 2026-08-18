#!/usr/bin/env python3
"""
Servidor HTTP del dashboard de copias de Azure.

Sirve el directorio generado por collect.py (index.html + data.json).
Solo librería estándar: sin Flask, sin nginx, mismo enfoque que el resto de
dashboards de la máquina.

Uso:
    python3 serve.py                       # 0.0.0.0:8090, sirve ./public
    python3 serve.py --port 8090 --dir /opt/dashboard-copias-azure/public
"""

from __future__ import annotations

import argparse
import os
import sys
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


class Handler(SimpleHTTPRequestHandler):
    """Sin caché (el HTML se regenera cada 30 min) y sin listado de directorios."""

    server_version = "DashboardCopiasAzure/1.0"

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store, must-revalidate")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        super().end_headers()

    def list_directory(self, path):  # noqa: ARG002
        self.send_error(404, "No encontrado")
        return None

    def log_message(self, fmt: str, *args) -> None:
        # A journal, con el formato habitual de systemd
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))


def main() -> int:
    parser = argparse.ArgumentParser(description="Servidor del dashboard de copias de Azure")
    parser.add_argument("--port", type=int, default=int(os.environ.get("SERVE_PORT", "8090")))
    parser.add_argument("--bind", default=os.environ.get("SERVE_BIND", "0.0.0.0"))
    parser.add_argument("--dir", default=os.environ.get("OUTPUT_DIR", str(BASE_DIR / "public")))
    args = parser.parse_args()

    root = Path(args.dir)
    root.mkdir(parents=True, exist_ok=True)

    if not (root / "index.html").exists():
        print(
            f"[AVISO] Todavía no existe {root}/index.html. "
            "Ejecuta collect.py para generarlo.",
            file=sys.stderr,
        )

    handler = partial(Handler, directory=str(root))
    server = ThreadingHTTPServer((args.bind, args.port), handler)
    print(f"Sirviendo {root} en http://{args.bind}:{args.port}", file=sys.stderr)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Parando.", file=sys.stderr)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
