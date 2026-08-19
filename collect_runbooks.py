#!/usr/bin/env python3
"""
Colector de procesos BI (runbooks de Azure Automation) -> procesos.json

Automatiza la revision diaria documentada en OneNote ("REVISAR TODOS LOS DIAS
AZURE"). El punto clave del procedimiento manual es este aviso:

    "que aparezca Completada no quiere decir que este ok,
     hay que revisar que no tenga errores"

Por eso el colector no se queda con el estado del job: baja los registros de
cada ejecucion y cuenta errores y advertencias de verdad. Un runbook que
termina "Completed" con un error dentro se marca en rojo.

Ademas detecta algo que la revision manual no ve: un runbook que directamente
no se ha ejecutado. Para saber cuando deberia haberlo hecho lee las
programaciones configuradas en la propia cuenta de Automation.

Uso:
    python3 collect_runbooks.py                 # lee credenciales de .env
    python3 collect_runbooks.py --demo          # datos de ejemplo
    python3 collect_runbooks.py --out ./public

Requisitos: ver requirements.txt
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

from collect import env, env_list, hours_since, iso, load_dotenv, model_dict, pick

BASE_DIR = Path(__file__).resolve().parent

# Margen tras la hora prevista antes de dar por no ejecutado un runbook
MARGEN_HORAS = float(os.environ.get("RUNBOOK_MARGEN_HORAS", "3"))

# Cuantos registros de error guardamos por ejecucion
MAX_MENSAJES = 6

ORDEN_ESTADO = {"fail": 0, "sinejecutar": 1, "warn": 2, "unknown": 3, "ok": 4}


# --------------------------------------------------------------------------
# Utilidades
# --------------------------------------------------------------------------


def intervalo_a_timedelta(frecuencia: str, intervalo) -> timedelta | None:
    """Frecuencia de una programacion de Azure -> duracion entre ejecuciones."""
    try:
        n = int(intervalo or 1)
    except (TypeError, ValueError):
        n = 1
    f = str(frecuencia or "").lower()
    if f == "hour":
        return timedelta(hours=n)
    if f == "day":
        return timedelta(days=n)
    if f == "week":
        return timedelta(weeks=n)
    if f == "month":
        return timedelta(days=30 * n)
    return None


def props(obj) -> dict:
    """Propiedades del recurso, ya bajadas al nivel correcto.

    as_dict() sobre un recurso de Azure devuelve {id, name, properties: {...}}.
    Casi todo lo que interesa (isEnabled, frequency, nextRun, runbook, schedule)
    esta dentro de 'properties', no en la raiz.
    """
    interior = model_dict(getattr(obj, "properties", None))
    if interior:
        return interior
    entero = model_dict(obj)
    return entero.get("properties") or entero


def texto_estado(valor) -> str:
    """Normaliza el estado de un job.

    El SDK devuelve un enum, y su representacion en texto es 'JobStatus.COMPLETED'.
    Nos quedamos solo con la parte util: 'Completed'.
    """
    texto = str(getattr(valor, "value", valor) or "").strip()
    if "." in texto and " " not in texto:
        texto = texto.rsplit(".", 1)[-1]
    return texto[:1].upper() + texto[1:].lower() if texto else ""


def clasificar(job: dict | None, esperada: str | None) -> str:
    """ok | warn | fail | sinejecutar | unknown."""
    if job is None:
        return "sinejecutar" if esperada else "unknown"

    estado = (job.get("estado") or "").lower()

    if estado in ("failed", "suspended", "stopped"):
        return "fail"
    if job.get("errores"):
        # Aqui esta el valor: "Completada" con errores dentro NO es correcto
        return "fail"
    if estado in ("running", "new", "activating", "queued"):
        return "warn"
    if job.get("advertencias"):
        return "warn"

    # Termino bien, pero puede haberse quedado sin ejecutar en la ultima ventana
    if esperada and (job.get("inicio") or "") < esperada:
        return "sinejecutar"

    if estado in ("completed", "succeeded"):
        return "ok"
    return "warn"


# --------------------------------------------------------------------------
# Recoleccion real
# --------------------------------------------------------------------------


def collect_azure(dias: int) -> dict:
    from azure.identity import ClientSecretCredential, DefaultAzureCredential
    from azure.mgmt.automation import AutomationClient

    subscription_id = env("AZURE_SUBSCRIPTION_ID", required=True)
    tenant_id = env("AZURE_TENANT_ID")
    client_id = env("AZURE_CLIENT_ID")
    client_secret = env("AZURE_CLIENT_SECRET")

    cuenta = env("AUTOMATION_ACCOUNT", required=True)
    grupo = env("AUTOMATION_RESOURCE_GROUP", required=True)
    vigilados = env_list("AUTOMATION_RUNBOOKS")

    if tenant_id and client_id and client_secret:
        credential = ClientSecretCredential(tenant_id, client_id, client_secret)
    else:
        credential = DefaultAzureCredential()

    client = AutomationClient(credential, subscription_id)
    errores: list[str] = []
    ahora = datetime.now(timezone.utc)
    desde = ahora - timedelta(days=dias)

    # ----------------------------------------------------------------------
    # 1. Programaciones: cuando deberia haber corrido cada runbook
    # ----------------------------------------------------------------------
    programas: dict[str, dict] = {}
    try:
        for prog in client.schedule.list_by_automation_account(grupo, cuenta):
            d = props(prog)
            programas[str(prog.name)] = {
                "nombre": str(prog.name),
                "activa": bool(d.get("isEnabled", True)),
                "frecuencia": texto_estado(d.get("frequency")),
                "intervalo": d.get("interval"),
                "proxima": iso(d.get("nextRun")),
            }
    except Exception as exc:  # noqa: BLE001
        errores.append(f"Programaciones: {exc}")

    enlaces: dict[str, list[str]] = {}
    try:
        for enlace in client.job_schedule.list_by_automation_account(grupo, cuenta):
            d = props(enlace)
            runbook = str((d.get("runbook") or {}).get("name") or "").strip()
            horario = str((d.get("schedule") or {}).get("name") or "").strip()
            if runbook and horario:
                enlaces.setdefault(runbook, []).append(horario)
    except Exception as exc:  # noqa: BLE001
        errores.append(f"Vinculos runbook-programacion: {exc}")

    if programas and not enlaces:
        errores.append(
            "Hay programaciones en la cuenta pero ninguna aparece vinculada a un runbook. "
            "Sin ese vinculo no se puede avisar de un proceso que no se haya ejecutado."
        )

    # ----------------------------------------------------------------------
    # 2. Runbooks a vigilar
    # ----------------------------------------------------------------------
    runbooks: list[str] = []
    try:
        todos = [str(r.name) for r in client.runbook.list_by_automation_account(grupo, cuenta)]
    except Exception as exc:  # noqa: BLE001
        errores.append(f"Listado de runbooks: {exc}")
        todos = []

    if vigilados:
        runbooks = list(vigilados)
        faltan = [r for r in vigilados if todos and r not in todos]
        if faltan:
            errores.append(
                "Estos runbooks estan en la configuracion pero no existen en la cuenta: "
                + ", ".join(faltan)
            )
    else:
        runbooks = todos

    # ----------------------------------------------------------------------
    # 3. Jobs de cada runbook
    # ----------------------------------------------------------------------
    def jobs_de(nombre: str) -> list:
        """Jobs del runbook, con respaldo si el filtro del servicio no cuela."""
        try:
            return list(
                client.job.list_by_automation_account(
                    grupo, cuenta, filter=f"properties/runbook/name eq '{nombre}'"
                )
            )
        except Exception:  # noqa: BLE001
            sueltos = []
            for job in client.job.list_by_automation_account(grupo, cuenta):
                d = props(job)
                if ((d.get("runbook") or {}).get("name") or "") == nombre:
                    sueltos.append(job)
                if len(sueltos) > 60:
                    break
            return sueltos

    def registros(job_name: str, tipo: str) -> list[str]:
        """Resumen de los streams de un tipo (Error / Warning) de una ejecucion."""
        mensajes: list[str] = []
        try:
            flujo = client.job_stream.list_by_job(
                grupo, cuenta, job_name, filter=f"properties/streamType eq '{tipo}'"
            )
            for stream in flujo:
                d = props(stream)
                texto = d.get("summary") or (d.get("streamText") or "")
                if texto:
                    mensajes.append(str(texto).strip()[:600])
                if len(mensajes) >= MAX_MENSAJES:
                    break
        except Exception as exc:  # noqa: BLE001
            errores.append(f"Registros de {job_name[:8]} ({tipo}): {exc}")
        return mensajes

    procesos: list[dict] = []
    historico: list[dict] = []

    for nombre in runbooks:
        ejecuciones = []
        try:
            for job in jobs_de(nombre):
                d = props(job)
                inicio = iso(pick(d, job.properties, "startTime", "start_time")) or iso(
                    pick(d, job.properties, "creationTime", "creation_time")
                )
                if inicio and inicio < iso(desde):
                    continue
                ejecuciones.append(
                    {
                        "job": str(job.name),
                        "runbook": nombre,
                        "estado": texto_estado(pick(d, job.properties, "status", "status")),
                        "detalle_estado": str(
                            pick(d, job.properties, "statusDetails", "status_details") or ""
                        ),
                        "inicio": inicio,
                        "fin": iso(pick(d, job.properties, "endTime", "end_time")),
                        "excepcion": str(pick(d, job.properties, "exception", "exception") or ""),
                    }
                )
        except Exception as exc:  # noqa: BLE001
            errores.append(f"Jobs de {nombre}: {exc}")

        ejecuciones.sort(key=lambda j: j.get("inicio") or "", reverse=True)

        # Solo bajamos los registros de la ultima ejecucion: es lo que se revisa
        # a diario y evita cientos de llamadas innecesarias.
        ultima = ejecuciones[0] if ejecuciones else None
        if ultima:
            ultima["errores"] = registros(ultima["job"], "Error")
            ultima["advertencias"] = registros(ultima["job"], "Warning")
            if ultima["excepcion"] and ultima["excepcion"] not in ultima["errores"]:
                ultima["errores"].insert(0, ultima["excepcion"])

        # Cuando deberia haber corrido por ultima vez, segun su programacion
        esperada, horario_txt = None, ""
        for nombre_prog in enlaces.get(nombre, []):
            prog = programas.get(nombre_prog)
            if not prog or not prog.get("activa") or not prog.get("proxima"):
                continue
            paso = intervalo_a_timedelta(prog["frecuencia"], prog["intervalo"])
            if not paso:
                continue
            try:
                proxima = datetime.fromisoformat(prog["proxima"].replace("Z", "+00:00"))
            except Exception:  # noqa: BLE001
                continue
            candidata = proxima - paso
            if esperada is None or candidata > esperada:
                esperada = candidata
            partes = [prog["frecuencia"].lower()]
            if prog.get("intervalo") and int(prog["intervalo"]) != 1:
                partes.insert(0, f"cada {prog['intervalo']}")
            horario_txt = " ".join(partes) + " · próx. " + proxima.astimezone().strftime("%d/%m %H:%M")

        # Damos un margen para no avisar mientras el proceso aun esta corriendo
        limite = iso(esperada + timedelta(hours=MARGEN_HORAS)) if esperada else None
        esperada_iso = iso(esperada) if esperada and iso(ahora) > (limite or "") else None

        estado = clasificar(ultima, esperada_iso)

        procesos.append(
            {
                "nombre": nombre,
                "cuenta": cuenta,
                "resource_group": grupo,
                "estado": estado,
                "ultimo_estado": (ultima or {}).get("estado", ""),
                "inicio": (ultima or {}).get("inicio"),
                "fin": (ultima or {}).get("fin"),
                "duracion_s": _duracion(ultima),
                "errores": (ultima or {}).get("errores", []),
                "advertencias": (ultima or {}).get("advertencias", []),
                "n_errores": len((ultima or {}).get("errores", [])),
                "n_advertencias": len((ultima or {}).get("advertencias", [])),
                "horario": horario_txt,
                "esperada": iso(esperada) if esperada else None,
                "ejecuciones": len(ejecuciones),
            }
        )
        historico.extend(ejecuciones[:20])

    historico.sort(key=lambda j: j.get("inicio") or "", reverse=True)
    return build_payload(procesos, historico, dias, cuenta, grupo, errores)


def _duracion(job: dict | None) -> float | None:
    if not job or not job.get("inicio") or not job.get("fin"):
        return None
    try:
        a = datetime.fromisoformat(job["inicio"].replace("Z", "+00:00"))
        b = datetime.fromisoformat(job["fin"].replace("Z", "+00:00"))
        return max((b - a).total_seconds(), 0)
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------
# Modo demo
# --------------------------------------------------------------------------


def collect_demo(dias: int) -> dict:
    ahora = datetime.now(timezone.utc)
    plantilla = [
        ("ProcAlm_ProcessAll", "03:30", 0, 0, "Completed"),
        ("ProcAlm_CarteraFecha", "03:55", 0, 2, "Completed"),
        ("Process_AS_Cartera", "05:00", 0, 0, "Completed"),
        ("Process_AS_Cartera_Fecha", "05:30", 0, 0, "Completed"),
        ("Process_AS_Finanzas", "06:50", 1, 0, "Completed"),
    ]
    procesos, historico = [], []

    for idx, (nombre, hora, n_err, n_adv, estado) in enumerate(plantilla):
        inicio = ahora - timedelta(hours=5 + idx)
        fin = inicio + timedelta(minutes=random.randint(3, 40))
        errores = (
            [
                "Invoke-ProcessASDatabase : Failed to save modifications to the server. "
                "Error returned: 'Column RecId in Table Cuenta contable contains a duplicate value'."
            ]
            if n_err
            else []
        )
        advertencias = ["La particion tardo mas de lo habitual en procesarse."] * n_adv
        job = {
            "job": f"demo-{idx}",
            "runbook": nombre,
            "estado": estado,
            "inicio": iso(inicio),
            "fin": iso(fin),
            "errores": errores,
            "advertencias": advertencias,
            "excepcion": "",
        }
        procesos.append(
            {
                "nombre": nombre,
                "cuenta": "automationsrvprobi01",
                "resource_group": "rg_pro_bi",
                "estado": clasificar(job, None),
                "ultimo_estado": estado,
                "inicio": iso(inicio),
                "fin": iso(fin),
                "duracion_s": (fin - inicio).total_seconds(),
                "errores": errores,
                "advertencias": advertencias,
                "n_errores": len(errores),
                "n_advertencias": len(advertencias),
                "horario": f"day · próx. mañana {hora}",
                "esperada": iso(inicio),
                "ejecuciones": dias,
            }
        )
        for d in range(dias):
            arranque = ahora - timedelta(days=d, hours=5 + idx)
            historico.append(
                {
                    "job": f"demo-{idx}-{d}",
                    "runbook": nombre,
                    "estado": "Failed" if (n_err and d == 0) else "Completed",
                    "inicio": iso(arranque),
                    "fin": iso(arranque + timedelta(minutes=random.randint(3, 40))),
                    "errores": errores if (n_err and d == 0) else [],
                    "advertencias": [],
                    "excepcion": "",
                }
            )

    historico.sort(key=lambda j: j.get("inicio") or "", reverse=True)
    payload = build_payload(procesos, historico, dias, "automationsrvprobi01", "rg_pro_bi", [])
    payload["demo"] = True
    return payload


# --------------------------------------------------------------------------
# Payload + salida
# --------------------------------------------------------------------------


def build_payload(procesos, historico, dias, cuenta, grupo, errores) -> dict:
    procesos.sort(key=lambda p: (ORDEN_ESTADO.get(p["estado"], 9), p["nombre"].lower()))

    counts = {"ok": 0, "warn": 0, "fail": 0, "sinejecutar": 0, "unknown": 0}
    for p in procesos:
        counts[p["estado"]] = counts.get(p["estado"], 0) + 1

    if counts["fail"]:
        overall = "fail"
    elif counts["sinejecutar"] or counts["warn"] or counts["unknown"]:
        overall = "warn"
    else:
        overall = "ok"

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window_days": dias,
        "cuenta": cuenta,
        "resource_group": grupo,
        "overall": overall,
        "counts": counts,
        "procesos": procesos,
        "historico": historico,
        "total_errores": sum(p["n_errores"] for p in procesos),
        "total_advertencias": sum(p["n_advertencias"] for p in procesos),
        "collector_errors": errores,
        "demo": False,
    }


def render(payload: dict, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    temporal = out_dir / "procesos.json.tmp"
    temporal.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporal.replace(out_dir / "procesos.json")


def main() -> int:
    parser = argparse.ArgumentParser(description="Estado de los procesos BI de Azure Automation")
    parser.add_argument("--demo", action="store_true", help="Datos de ejemplo, sin conectar a Azure")
    parser.add_argument("--days", type=int, default=int(os.environ.get("RUNBOOK_DAYS", "7")))
    parser.add_argument("--out", default=os.environ.get("OUTPUT_DIR", str(BASE_DIR / "public")))
    args = parser.parse_args()

    load_dotenv(BASE_DIR / ".env")

    try:
        payload = collect_demo(args.days) if args.demo else collect_azure(args.days)
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "window_days": args.days,
            "cuenta": os.environ.get("AUTOMATION_ACCOUNT", ""),
            "resource_group": os.environ.get("AUTOMATION_RESOURCE_GROUP", ""),
            "overall": "fail",
            "counts": {"ok": 0, "warn": 0, "fail": 0, "sinejecutar": 0, "unknown": 0},
            "procesos": [],
            "historico": [],
            "total_errores": 0,
            "total_advertencias": 0,
            "collector_errors": [f"El colector no pudo ejecutarse: {exc}"],
            "demo": False,
        }
        render(payload, Path(args.out))
        return 1

    render(payload, Path(args.out))

    try:
        import historico

        historico.registrar_runbooks(
            Path(args.out), payload.get("historico", []), payload.get("counts", {})
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[AVISO] Historico no disponible: {exc}")

    print(
        f"OK -> {args.out}/procesos.json | estado={payload['overall']} | "
        f"procesos={len(payload['procesos'])} errores={payload['total_errores']} "
        f"advertencias={payload['total_advertencias']} "
        f"sin ejecutar={payload['counts']['sinejecutar']}"
    )
    return 2 if payload["overall"] == "fail" else 0


if __name__ == "__main__":
    raise SystemExit(main())
