#!/usr/bin/env python3
"""
Colector de estado de Azure Backup -> data.json + index.html

Sustituye la revisión manual del portal (Recovery Services vault -> Backup items
-> View jobs) por un proceso automático que se ejecuta en un servidor Ubuntu.

Uso:
    python3 collect.py                 # lee credenciales de .env / entorno
    python3 collect.py --demo          # datos de ejemplo, sin tocar Azure
    python3 collect.py --days 7        # histórico de jobs de los últimos 7 días
    python3 collect.py --out /var/www/backup-dashboard

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

BASE_DIR = Path(__file__).resolve().parent
TEMPLATE = BASE_DIR / "dashboard_template.html"

# --------------------------------------------------------------------------
# Configuración
# --------------------------------------------------------------------------


def load_dotenv(path: Path) -> None:
    """Carga un .env sencillo sin dependencias externas."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def env(name: str, default: str | None = None, required: bool = False) -> str | None:
    value = os.environ.get(name, default)
    if required and not value:
        sys.exit(f"[ERROR] Falta la variable de entorno {name} (revisa tu .env)")
    return value


# --------------------------------------------------------------------------
# Utilidades
# --------------------------------------------------------------------------


def iso(value) -> str | None:
    """Normaliza cualquier fecha a ISO-8601 UTC."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    try:
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    except Exception:
        return str(value)


def duration_seconds(value) -> float | None:
    """timedelta o string ISO-8601 ("PT1H2M3S") -> segundos."""
    if value is None:
        return None
    if isinstance(value, timedelta):
        return value.total_seconds()
    try:
        return float(value)
    except (TypeError, ValueError):
        pass
    text = str(value)
    if text.startswith("PT"):
        total, number = 0.0, ""
        for ch in text[2:]:
            if ch.isdigit() or ch == ".":
                number += ch
            else:
                if number:
                    total += float(number) * {"H": 3600, "M": 60, "S": 1}.get(ch.upper(), 0)
                number = ""
        return total
    return None


def hours_since(iso_ts: str | None) -> float | None:
    if not iso_ts:
        return None
    try:
        dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds() / 3600
    except Exception:
        return None


def classify(last_status: str | None, last_ts: str | None, sla_hours: float) -> str:
    """Devuelve ok | warn | fail | unknown."""
    status = (last_status or "").lower().replace(" ", "")
    age = hours_since(last_ts)

    if not status:
        return "unknown"
    if "fail" in status or "error" in status:
        return "fail"
    if "progress" in status or "warning" in status:
        return "warn"
    if "complet" in status or "success" in status or status in ("healthy", "passed"):
        return "warn" if (age is not None and age > sla_hours) else "ok"
    return "warn"


def parse_rg(resource_id: str) -> str:
    parts = (resource_id or "").split("/")
    try:
        return parts[parts.index("resourceGroups") + 1]
    except (ValueError, IndexError):
        return ""


# --------------------------------------------------------------------------
# Recolección real desde Azure
# --------------------------------------------------------------------------


def collect_azure(days: int, sla_hours: float) -> dict:
    from azure.identity import ClientSecretCredential, DefaultAzureCredential
    from azure.mgmt.recoveryservices import RecoveryServicesClient
    from azure.mgmt.recoveryservicesbackup.activestamp import RecoveryServicesBackupClient

    subscription_id = env("AZURE_SUBSCRIPTION_ID", required=True)
    tenant_id = env("AZURE_TENANT_ID")
    client_id = env("AZURE_CLIENT_ID")
    client_secret = env("AZURE_CLIENT_SECRET")

    if tenant_id and client_id and client_secret:
        credential = ClientSecretCredential(tenant_id, client_id, client_secret)
    else:
        # Managed Identity de la VM, o `az login` en el propio servidor
        credential = DefaultAzureCredential()

    rsv_client = RecoveryServicesClient(credential, subscription_id)
    backup_client = RecoveryServicesBackupClient(credential, subscription_id)

    # --- Vaults a monitorizar ---------------------------------------------
    wanted = [v.strip() for v in (env("AZURE_VAULTS", "") or "").split(",") if v.strip()]
    vaults = []
    for vault in rsv_client.vaults.list_by_subscription():
        if wanted and vault.name not in wanted:
            continue
        vaults.append(
            {"name": vault.name, "resource_group": parse_rg(vault.id), "location": vault.location}
        )

    if not vaults:
        raise RuntimeError(
            "No se ha encontrado ningún Recovery Services vault accesible. "
            "Revisa AZURE_SUBSCRIPTION_ID, AZURE_VAULTS y los permisos del service principal."
        )

    items: list[dict] = []
    jobs: list[dict] = []
    errors: list[str] = []
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=days)

    for vault in vaults:
        vname, vrg = vault["name"], vault["resource_group"]

        # --- Backup items: estado actual por máquina -----------------------
        try:
            for item in backup_client.backup_protected_items.list(vname, vrg):
                p = item.properties
                friendly = getattr(p, "friendly_name", None) or item.name
                last_status = getattr(p, "last_backup_status", None)
                last_time = iso(getattr(p, "last_backup_time", None))

                # Azure Files no expone last_backup_status
                if not last_status:
                    last_status = getattr(p, "protection_status", None) or getattr(p, "health_status", None)
                if not last_time:
                    last_time = iso(getattr(p, "last_recovery_point", None))

                mgmt = getattr(p, "backup_management_type", None)
                items.append(
                    {
                        "vault": vname,
                        "resource_group": parse_rg(getattr(p, "source_resource_id", "") or "") or vrg,
                        "name": friendly,
                        "type": str(mgmt) if mgmt else "Unknown",
                        "protection_state": str(getattr(p, "protection_state", "") or ""),
                        "health": str(getattr(p, "health_status", "") or ""),
                        "policy": getattr(p, "policy_name", None) or "",
                        "last_backup_status": str(last_status or ""),
                        "last_backup_time": last_time,
                        "oldest_recovery_point": iso(getattr(p, "oldest_recovery_point", None)),
                        "last_recovery_point": iso(getattr(p, "last_recovery_point", None)),
                        "state": classify(last_status, last_time, sla_hours),
                    }
                )
        except Exception as exc:  # noqa: BLE001
            errors.append(f"Items de {vname}: {exc}")

        # --- Backup jobs: histórico ----------------------------------------
        try:
            fmt = "%Y-%m-%d %I:%M:%S %p"
            job_filter = f"startTime eq '{start.strftime(fmt)}' and endTime eq '{now.strftime(fmt)}'"
            for job in backup_client.backup_jobs.list(vname, vrg, filter=job_filter):
                p = job.properties
                details = []
                for err in getattr(p, "error_details", None) or []:
                    msg = getattr(err, "error_string", None) or getattr(err, "error_title", None)
                    if msg:
                        details.append(str(msg))
                jobs.append(
                    {
                        "vault": vname,
                        "name": getattr(p, "entity_friendly_name", None) or job.name,
                        "operation": str(getattr(p, "operation", "") or ""),
                        "status": str(getattr(p, "status", "") or ""),
                        "type": str(getattr(p, "backup_management_type", "") or ""),
                        "start_time": iso(getattr(p, "start_time", None)),
                        "end_time": iso(getattr(p, "end_time", None)),
                        "duration_s": duration_seconds(getattr(p, "duration", None)),
                        "errors": details,
                    }
                )
        except Exception as exc:  # noqa: BLE001
            errors.append(f"Jobs de {vname}: {exc}")

    return build_payload(vaults, items, jobs, days, sla_hours, errors)


# --------------------------------------------------------------------------
# Modo demo
# --------------------------------------------------------------------------


def collect_demo(days: int, sla_hours: float) -> dict:
    vaults = [{"name": "STNCERAMICA-backup", "resource_group": "rg_backup", "location": "westeurope"}]
    machines = [
        ("vmproaddc1", "rg_ad"),
        ("vmproadrds01", "rg_ad"),
        ("vmpregscasql01", "rg_pre_gscash"),
        ("vmprocitrxapp02", "rg_pro_citrix"),
        ("vmproctxdlvf01", "rg_pro_citrix"),
        ("vmproctxstrf01", "rg_pro_citrix"),
        ("vmprogscasql01", "rg_pro_gscash"),
    ]
    now = datetime.now(timezone.utc)
    items: list[dict] = []
    jobs: list[dict] = []

    for idx, (name, rg) in enumerate(machines):
        broken = name == "vmproctxdlvf01"
        stale = name == "vmpregscasql01"
        last = now - timedelta(hours=38 if stale else random.uniform(3, 12))
        status = "Failed" if broken else "Completed"
        items.append(
            {
                "vault": "STNCERAMICA-backup",
                "resource_group": rg,
                "name": name,
                "type": "AzureIaasVM",
                "protection_state": "Protected",
                "health": "Unhealthy" if broken else "Passed",
                "policy": "DefaultPolicy",
                "last_backup_status": status,
                "last_backup_time": iso(last),
                "oldest_recovery_point": iso(now - timedelta(days=30 - idx)),
                "last_recovery_point": iso(last),
                "state": classify(status, iso(last), sla_hours),
            }
        )
        for d in range(days):
            start = now - timedelta(days=d, hours=random.uniform(0, 3))
            failed = broken and d < 2
            jobs.append(
                {
                    "vault": "STNCERAMICA-backup",
                    "name": name,
                    "operation": "Backup",
                    "status": "Failed" if failed else "Completed",
                    "type": "AzureIaasVM",
                    "start_time": iso(start),
                    "end_time": iso(start + timedelta(minutes=random.randint(8, 45))),
                    "duration_s": random.randint(500, 2700),
                    "errors": ["UserErrorGuestAgentStatusUnavailable: el agente de la VM no responde."]
                    if failed
                    else [],
                }
            )

    items.append(
        {
            "vault": "STNCERAMICA-backup",
            "resource_group": "rg_backup",
            "name": "informatica (integracion365fo)",
            "type": "AzureStorage",
            "protection_state": "Protected",
            "health": "Passed",
            "policy": "DailyPolicy",
            "last_backup_status": "Completed",
            "last_backup_time": iso(now - timedelta(hours=6)),
            "oldest_recovery_point": iso(now - timedelta(days=30)),
            "last_recovery_point": iso(now - timedelta(hours=6)),
            "state": "ok",
        }
    )

    payload = build_payload(vaults, items, jobs, days, sla_hours, [])
    payload["demo"] = True
    return payload


# --------------------------------------------------------------------------
# Payload + render
# --------------------------------------------------------------------------


def build_payload(vaults, items, jobs, days, sla_hours, errors) -> dict:
    jobs.sort(key=lambda j: j.get("start_time") or "", reverse=True)
    items.sort(key=lambda i: (i.get("state") != "fail", i.get("state") != "warn", i.get("name", "")))

    failed_jobs = [j for j in jobs if classify(j.get("status"), None, sla_hours) == "fail"]

    counts = {"ok": 0, "warn": 0, "fail": 0, "unknown": 0}
    for item in items:
        state = item.get("state", "unknown")
        counts[state] = counts.get(state, 0) + 1

    if counts["fail"] or failed_jobs:
        overall = "fail"
    elif counts["warn"] or counts["unknown"]:
        overall = "warn"
    else:
        overall = "ok"

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window_days": days,
        "sla_hours": sla_hours,
        "overall": overall,
        "counts": counts,
        "vaults": vaults,
        "items": items,
        "jobs": jobs,
        "failed_jobs": len(failed_jobs),
        "total_jobs": len(jobs),
        "collector_errors": errors,
        "demo": False,
    }


def render(payload: dict, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "data.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    if not TEMPLATE.exists():
        sys.exit(f"[ERROR] No se encuentra la plantilla {TEMPLATE}")

    # El JSON se incrusta en el HTML -> fichero autocontenido, sin fetch ni CORS.
    # Se escapa "</" y los separadores U+2028/U+2029 para no romper el <script>.
    inline = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    inline = inline.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")

    html = TEMPLATE.read_text(encoding="utf-8").replace("/*__DATA__*/null", inline)
    (out_dir / "index.html").write_text(html, encoding="utf-8")


# --------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="Dashboard de estado de Azure Backup")
    parser.add_argument("--demo", action="store_true", help="Datos de ejemplo, sin conectar a Azure")
    parser.add_argument("--days", type=int, default=int(os.environ.get("BACKUP_DAYS", "7")))
    parser.add_argument("--sla-hours", type=float, default=float(os.environ.get("BACKUP_SLA_HOURS", "26")))
    parser.add_argument("--out", default=os.environ.get("OUTPUT_DIR", str(BASE_DIR / "public")))
    args = parser.parse_args()

    load_dotenv(BASE_DIR / ".env")

    try:
        payload = (
            collect_demo(args.days, args.sla_hours)
            if args.demo
            else collect_azure(args.days, args.sla_hours)
        )
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "window_days": args.days,
            "sla_hours": args.sla_hours,
            "overall": "fail",
            "counts": {"ok": 0, "warn": 0, "fail": 0, "unknown": 0},
            "vaults": [],
            "items": [],
            "jobs": [],
            "failed_jobs": 0,
            "total_jobs": 0,
            "collector_errors": [f"El colector no pudo ejecutarse: {exc}"],
            "demo": False,
        }
        render(payload, Path(args.out))
        return 1

    render(payload, Path(args.out))
    print(
        f"OK -> {args.out}/index.html | estado={payload['overall']} | "
        f"items={len(payload['items'])} jobs={payload['total_jobs']} fallidos={payload['failed_jobs']}"
    )
    # Código de salida != 0 si hay fallos -> enganchable a monitorización externa
    return 2 if payload["overall"] == "fail" else 0


if __name__ == "__main__":
    raise SystemExit(main())
