#!/usr/bin/env python3
"""
Histórico local en SQLite.

Azure conserva un histórico corto: los jobs de copia rondan los 30 días y los de
Automation menos. Para ver tendencias de verdad —que un proceso que tardaba 20
minutos lleve tres semanas subiendo— hay que guardarlo en local.

Cada colector llama aquí al terminar. Las inserciones son idempotentes: si la
misma ejecución se recoge dos veces, no se duplica.

El fichero vive junto a los JSON, en OUTPUT_DIR/historico.sqlite.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

ESQUEMA = """
CREATE TABLE IF NOT EXISTS runbook_jobs (
    job        TEXT PRIMARY KEY,
    runbook    TEXT NOT NULL,
    inicio     TEXT,
    fin        TEXT,
    duracion_s REAL,
    estado     TEXT,
    n_errores  INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_runbook_jobs ON runbook_jobs (runbook, inicio);

CREATE TABLE IF NOT EXISTS backup_jobs (
    clave      TEXT PRIMARY KEY,
    nombre     TEXT NOT NULL,
    inicio     TEXT,
    duracion_s REAL,
    estado     TEXT
);
CREATE INDEX IF NOT EXISTS idx_backup_jobs ON backup_jobs (nombre, inicio);

CREATE TABLE IF NOT EXISTS estado_diario (
    fecha   TEXT NOT NULL,
    ambito  TEXT NOT NULL,
    ok      INTEGER DEFAULT 0,
    warn    INTEGER DEFAULT 0,
    fail    INTEGER DEFAULT 0,
    otros   INTEGER DEFAULT 0,
    PRIMARY KEY (fecha, ambito)
);
"""


def abrir(out_dir: Path) -> sqlite3.Connection:
    out_dir.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(out_dir / "historico.sqlite", timeout=15)
    con.executescript(ESQUEMA)
    return con


def _hoy() -> str:
    return datetime.now(timezone.utc).date().isoformat()


# --------------------------------------------------------------------------
# Escritura
# --------------------------------------------------------------------------


def registrar_runbooks(out_dir: Path, jobs: list[dict], counts: dict) -> None:
    try:
        with closing(abrir(out_dir)) as con, con:
            con.executemany(
                "INSERT OR REPLACE INTO runbook_jobs "
                "(job, runbook, inicio, fin, duracion_s, estado, n_errores) "
                "VALUES (?,?,?,?,?,?,?)",
                [
                    (
                        j.get("job"),
                        j.get("runbook"),
                        j.get("inicio"),
                        j.get("fin"),
                        _duracion(j),
                        j.get("estado"),
                        len(j.get("errores") or []),
                    )
                    for j in jobs
                    if j.get("job")
                ],
            )
            _estado(con, "procesos", counts)
    except Exception as exc:  # noqa: BLE001
        print(f"[AVISO] No se pudo escribir el historico de procesos: {exc}")


def registrar_backups(out_dir: Path, jobs: list[dict], counts: dict) -> None:
    try:
        with closing(abrir(out_dir)) as con, con:
            con.executemany(
                "INSERT OR REPLACE INTO backup_jobs "
                "(clave, nombre, inicio, duracion_s, estado) VALUES (?,?,?,?,?)",
                [
                    (
                        f"{j.get('name')}|{j.get('start_time')}",
                        j.get("name"),
                        j.get("start_time"),
                        j.get("duration_s"),
                        j.get("status"),
                    )
                    for j in jobs
                    if j.get("name") and j.get("start_time")
                ],
            )
            _estado(con, "copias", counts)
    except Exception as exc:  # noqa: BLE001
        print(f"[AVISO] No se pudo escribir el historico de copias: {exc}")


def _estado(con: sqlite3.Connection, ambito: str, counts: dict) -> None:
    c = counts or {}
    otros = sum(v for k, v in c.items() if k not in ("ok", "warn", "fail"))
    con.execute(
        "INSERT OR REPLACE INTO estado_diario (fecha, ambito, ok, warn, fail, otros) "
        "VALUES (?,?,?,?,?,?)",
        (_hoy(), ambito, c.get("ok", 0), c.get("warn", 0), c.get("fail", 0), otros),
    )


def _duracion(job: dict) -> float | None:
    if job.get("duracion_s") is not None:
        return job["duracion_s"]
    if not job.get("inicio") or not job.get("fin"):
        return None
    try:
        a = datetime.fromisoformat(str(job["inicio"]).replace("Z", "+00:00"))
        b = datetime.fromisoformat(str(job["fin"]).replace("Z", "+00:00"))
        return max((b - a).total_seconds(), 0)
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------
# Lectura
# --------------------------------------------------------------------------


def tendencias(out_dir: Path, dias: int = 90, max_puntos: int = 60) -> dict:
    """Series de duracion por proceso y por maquina, y estado dia a dia."""
    desde = (datetime.now(timezone.utc) - timedelta(days=dias)).isoformat()
    salida = {"runbooks": {}, "copias": {}, "estado": [], "dias": dias}

    try:
        with closing(abrir(out_dir)) as con:
            con.row_factory = sqlite3.Row

            for fila in con.execute(
                "SELECT runbook, inicio, duracion_s, estado, n_errores FROM runbook_jobs "
                "WHERE inicio >= ? AND duracion_s IS NOT NULL ORDER BY inicio",
                (desde,),
            ):
                salida["runbooks"].setdefault(fila["runbook"], []).append(
                    {
                        "inicio": fila["inicio"],
                        "duracion_s": fila["duracion_s"],
                        "estado": fila["estado"],
                        "errores": fila["n_errores"],
                    }
                )

            for fila in con.execute(
                "SELECT nombre, inicio, duracion_s, estado FROM backup_jobs "
                "WHERE inicio >= ? AND duracion_s IS NOT NULL ORDER BY inicio",
                (desde,),
            ):
                salida["copias"].setdefault(fila["nombre"], []).append(
                    {
                        "inicio": fila["inicio"],
                        "duracion_s": fila["duracion_s"],
                        "estado": fila["estado"],
                    }
                )

            for fila in con.execute(
                "SELECT fecha, ambito, ok, warn, fail, otros FROM estado_diario "
                "WHERE fecha >= ? ORDER BY fecha",
                (desde[:10],),
            ):
                salida["estado"].append(dict(fila))
    except Exception as exc:  # noqa: BLE001
        salida["error"] = str(exc)
        return salida

    # Recortamos a los ultimos N puntos: la grafica no gana nada con mas
    for bloque in ("runbooks", "copias"):
        for clave, serie in salida[bloque].items():
            salida[bloque][clave] = serie[-max_puntos:]

    return salida


def resumen(serie: list[dict]) -> dict:
    """Media y variacion de la ultima ejecucion frente al resto."""
    valores = [p["duracion_s"] for p in serie if p.get("duracion_s") is not None]
    if len(valores) < 2:
        return {}
    ultima = valores[-1]
    previas = valores[:-1]
    media = sum(previas) / len(previas)
    return {
        "media_s": media,
        "ultima_s": ultima,
        "variacion_pct": ((ultima - media) / media * 100) if media else 0,
        "muestras": len(valores),
    }
