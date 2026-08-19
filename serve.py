#!/usr/bin/env python3
"""
Servidor del dashboard de copias y maquinas virtuales de Azure.

Rutas:
    /            -> dashboard.html (shell estatico)
    /api/data    -> data.json que genera collect.py
    /csv         -> exportacion del estado actual
    /logo        -> logo.png del directorio de la aplicacion, si existe
    /salud       -> comprobacion simple para monitorizacion

Solo libreria estandar, mismo enfoque que el resto de dashboards de la maquina.

Uso:
    python3 serve.py --port 8090 --dir /opt/dashboard-copias-azure/public
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

BASE_DIR = Path(__file__).resolve().parent

COLUMNAS = [
    ("name", "nombre"),
    ("kind", "tipo_elemento"),
    ("resource_group", "grupo_recursos"),
    ("location", "ubicacion"),
    ("power_state", "estado_energia"),
    ("ip", "ip_privada"),
    ("os", "sistema_operativo"),
    ("size", "tamano"),
    ("grupo", "grupo"),
    ("protected", "protegida"),
    ("state", "estado"),
    ("vault", "vault"),
    ("policy", "politica"),
    ("protection_state", "estado_proteccion"),
    ("health", "salud"),
    ("last_backup_status", "ultimo_backup_estado"),
    ("last_backup_time", "ultimo_backup_fecha"),
    ("ultima_duracion_s", "ultima_duracion_segundos"),
    ("last_recovery_point", "ultimo_punto"),
    ("oldest_recovery_point", "punto_mas_antiguo"),
    ("agente_estado", "agente_vm"),
    ("agente_version", "agente_version"),
]

# Columnas que se aplanan a texto en el CSV
COLUMNAS_EXTRA = [
    ("politica_detalle", "politica_detalle"),
    ("tags", "etiquetas"),
]


class Handler(BaseHTTPRequestHandler):
    server_version = "DashboardCopiasAzure/2.0"

    # Rellenados desde main()
    data_dir: Path = Path(".")
    app_dir: Path = BASE_DIR

    # ------------------------------------------------------------------
    def responder(self, code: int, content_type: str, body: bytes, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, must-revalidate")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def error(self, code: int, msg: str) -> None:
        self.responder(code, "text/plain; charset=utf-8", msg.encode("utf-8"))

    def leer_datos(self) -> dict | None:
        ruta = self.data_dir / "data.json"
        if not ruta.exists():
            return None
        try:
            return json.loads(ruta.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return None

    # ------------------------------------------------------------------
    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_GET(self) -> None:  # noqa: N802
        ruta = urlparse(self.path).path.rstrip("/") or "/"

        if ruta == "/":
            return self.servir_shell()
        if ruta == "/api/data":
            return self.servir_datos()
        if ruta == "/api/procesos":
            return self.servir_procesos()
        if ruta == "/api/tendencia":
            return self.servir_tendencia()
        if ruta == "/csv":
            return self.servir_csv()
        if ruta == "/logo":
            return self.servir_logo()
        if ruta == "/salud":
            datos = self.leer_datos()
            estado = (datos or {}).get("overall", "sin-datos")
            return self.responder(
                200 if datos else 503, "text/plain; charset=utf-8", f"{estado}\n".encode("utf-8")
            )
        return self.error(404, "No encontrado")

    # ------------------------------------------------------------------
    def servir_shell(self) -> None:
        for nombre in ("dashboard.html", "dashboard_template.html", "index.html"):
            fichero = self.app_dir / nombre
            if fichero.exists():
                return self.responder(
                    200, "text/html; charset=utf-8", fichero.read_bytes()
                )
        self.error(500, "No se encuentra dashboard.html en " + str(self.app_dir))

    def servir_datos(self) -> None:
        ruta = self.data_dir / "data.json"
        if not ruta.exists():
            cuerpo = json.dumps(
                {
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                    "overall": "unknown",
                    "items": [],
                    "jobs": [],
                    "counts": {},
                    "vaults": [],
                    "collector_errors": [
                        "Todavia no se ha generado data.json. Ejecuta collect.py "
                        "o espera a la proxima pasada del timer."
                    ],
                }
            ).encode("utf-8")
            return self.responder(200, "application/json; charset=utf-8", cuerpo)
        return self.responder(200, "application/json; charset=utf-8", ruta.read_bytes())

    def servir_procesos(self) -> None:
        ruta = self.data_dir / "procesos.json"
        if not ruta.exists():
            cuerpo = json.dumps(
                {
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                    "overall": "unknown",
                    "procesos": [],
                    "historico": [],
                    "counts": {},
                    "collector_errors": [
                        "Todavia no se ha generado procesos.json. Ejecuta "
                        "collect_runbooks.py o espera a la proxima pasada del timer."
                    ],
                }
            ).encode("utf-8")
            return self.responder(200, "application/json; charset=utf-8", cuerpo)
        return self.responder(200, "application/json; charset=utf-8", ruta.read_bytes())

    def servir_tendencia(self) -> None:
        try:
            sys.path.insert(0, str(self.app_dir))
            import historico

            datos = historico.tendencias(self.data_dir)
        except Exception as exc:  # noqa: BLE001
            datos = {"runbooks": {}, "copias": {}, "estado": [], "error": str(exc)}
        cuerpo = json.dumps(datos, ensure_ascii=False).encode("utf-8")
        self.responder(200, "application/json; charset=utf-8", cuerpo)

    def servir_csv(self) -> None:
        datos = self.leer_datos()
        if datos is None:
            return self.error(503, "Todavia no hay datos que exportar")

        def celda(item: dict, clave: str):
            valor = item.get(clave)
            if isinstance(valor, bool):
                return "si" if valor else "no"
            if isinstance(valor, dict):
                return ", ".join(f"{k}={v}" for k, v in valor.items() if v)
            return "" if valor is None else valor

        todas = COLUMNAS + COLUMNAS_EXTRA
        buffer = io.StringIO()
        # BOM + punto y coma: Excel en espanol lo abre bien de un doble clic
        escritor = csv.writer(buffer, delimiter=";", lineterminator="\r\n")
        escritor.writerow([titulo for _, titulo in todas])
        for item in datos.get("items", []):
            escritor.writerow([celda(item, clave) for clave, _ in todas])

        sello = datetime.now().strftime("%Y%m%d-%H%M")
        cuerpo = ("﻿" + buffer.getvalue()).encode("utf-8")
        self.responder(
            200,
            "text/csv; charset=utf-8",
            cuerpo,
            {"Content-Disposition": f'attachment; filename="copias-azure-{sello}.csv"'},
        )

    def servir_logo(self) -> None:
        for nombre, mime in (
            ("logo.png", "image/png"),
            ("logo.jpg", "image/jpeg"),
            ("logo.svg", "image/svg+xml"),
        ):
            fichero = self.app_dir / nombre
            if fichero.exists():
                return self.responder(200, mime, fichero.read_bytes())
        self.error(404, "Sin logo")

    # ------------------------------------------------------------------
    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))


def main() -> int:
    parser = argparse.ArgumentParser(description="Servidor del dashboard de copias de Azure")
    parser.add_argument("--port", type=int, default=int(os.environ.get("SERVE_PORT", "8090")))
    parser.add_argument("--bind", default=os.environ.get("SERVE_BIND", "0.0.0.0"))
    parser.add_argument("--dir", default=os.environ.get("OUTPUT_DIR", str(BASE_DIR / "public")))
    args = parser.parse_args()

    data_dir = Path(args.dir)
    data_dir.mkdir(parents=True, exist_ok=True)

    Handler.data_dir = data_dir
    Handler.app_dir = BASE_DIR

    if not (data_dir / "data.json").exists():
        print(
            f"[AVISO] Todavia no existe {data_dir}/data.json. Ejecuta collect.py para generarlo.",
            file=sys.stderr,
        )

    server = ThreadingHTTPServer((args.bind, args.port), Handler)
    print(f"Sirviendo {data_dir} en http://{args.bind}:{args.port}", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Parando.", file=sys.stderr)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
