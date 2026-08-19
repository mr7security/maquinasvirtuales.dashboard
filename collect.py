#!/usr/bin/env python3
"""
Colector de estado de maquinas virtuales y copias de Azure -> data.json + index.html

Cruza dos fuentes:
  - Inventario de VMs de la suscripcion (Microsoft.Compute)
  - Elementos protegidos y jobs de los Recovery Services vaults

De ese cruce sale lo que el portal no ensena de un vistazo: que VMs existen,
cuales estan respaldadas, cuando fue su ultima copia y cuales no tienen ninguna.

Uso:
    python3 collect.py                 # lee credenciales de .env / entorno
    python3 collect.py --demo          # datos de ejemplo, sin tocar Azure
    python3 collect.py --days 7        # historico de jobs de los ultimos 7 dias
    python3 collect.py --out /opt/dashboard-copias-azure/public

Requisitos: ver requirements.txt
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import random
import sys
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# --------------------------------------------------------------------------
# Configuracion
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


def env_list(name: str) -> list[str]:
    return [v.strip() for v in (os.environ.get(name, "") or "").split(",") if v.strip()]


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
        dt = datetime.fromisoformat(str(iso_ts).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds() / 3600
    except Exception:
        return None


def model_dict(obj) -> dict:
    """Modelo del SDK -> dict con claves camelCase, o {} si no se puede."""
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    for attr in ("as_dict", "serialize"):
        fn = getattr(obj, attr, None)
        if callable(fn):
            try:
                result = fn()
                if isinstance(result, dict):
                    return result
            except Exception:  # noqa: BLE001
                pass
    return {}


def pick(data: dict, obj, camel: str, snake: str):
    """Lee del dict camelCase y, si no esta, del atributo snake_case."""
    value = data.get(camel)
    if value in (None, ""):
        value = getattr(obj, snake, None)
    return value


def parse_rg(resource_id: str) -> str:
    parts = (resource_id or "").split("/")
    for label in ("resourceGroups", "resourcegroups"):
        if label in parts:
            try:
                return parts[parts.index(label) + 1]
            except IndexError:
                return ""
    return ""


def matches_any(name: str, patterns: list[str]) -> bool:
    lowered = (name or "").lower()
    return any(fnmatch.fnmatch(lowered, str(p).lower()) for p in patterns or [])


OTROS_POR_DEFECTO = {"nombre": "Sin clasificar", "color": "#f59e0b", "exentas": False}


def cargar_grupos() -> dict:
    """Lee grupos.json: agrupacion de tarjetas, color y exencion de copia."""
    ruta = Path(os.environ.get("GROUPS_FILE") or (BASE_DIR / "grupos.json"))
    cfg = {"grupos": [], "otros": dict(OTROS_POR_DEFECTO)}
    if not ruta.exists():
        return cfg
    try:
        datos = json.loads(ruta.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        print(f"[AVISO] No se pudo leer {ruta}: {exc}", file=sys.stderr)
        return cfg
    cfg["grupos"] = [g for g in (datos.get("grupos") or []) if isinstance(g, dict)]
    if isinstance(datos.get("otros"), dict):
        cfg["otros"].update(datos["otros"])
    return cfg


def dias_para_caducar() -> dict:
    """Avisa antes de que caduque el secreto del Service Principal.

    Es una averia silenciosa clasica: el secreto expira y el colector deja de
    recoger datos sin que nadie relacione una cosa con la otra.
    """
    fecha = (os.environ.get("AZURE_SECRET_EXPIRA") or "").strip()
    if not fecha:
        return {}
    try:
        limite = datetime.fromisoformat(fecha).replace(tzinfo=timezone.utc)
    except ValueError:
        return {"error": f"AZURE_SECRET_EXPIRA no tiene formato AAAA-MM-DD: {fecha}"}
    restantes = (limite - datetime.now(timezone.utc)).days
    return {"fecha": limite.date().isoformat(), "dias": restantes}


def costes_por_grupo(credential, subscription_id: str) -> dict:
    """Coste del mes en curso agrupado por grupo de recursos.

    Se agrupa por grupo y no por maquina a proposito: el coste de una VM se
    reparte entre varios recursos (discos, tarjetas de red, IPs publicas) y
    sumarlo por grupo de recursos es lo unico que da una cifra honesta.
    """
    from azure.mgmt.costmanagement import CostManagementClient

    cliente = CostManagementClient(credential)
    ambito = f"/subscriptions/{subscription_id}"
    consulta = {
        "type": "ActualCost",
        "timeframe": "MonthToDate",
        "dataset": {
            "granularity": "None",
            "aggregation": {"total": {"name": "Cost", "function": "Sum"}},
            "grouping": [{"type": "Dimension", "name": "ResourceGroupName"}],
        },
    }
    resultado = cliente.query.usage(ambito, consulta)
    d = model_dict(resultado)

    columnas = [str(c.get("name") or "").lower() for c in (d.get("columns") or [])]
    try:
        i_coste = columnas.index("cost")
    except ValueError:
        i_coste = 0
    try:
        i_grupo = columnas.index("resourcegroupname")
    except ValueError:
        i_grupo = 1
    i_moneda = columnas.index("currency") if "currency" in columnas else None

    importes: dict[str, float] = {}
    moneda = ""
    for fila in d.get("rows") or []:
        try:
            grupo = str(fila[i_grupo]).lower()
            importes[grupo] = importes.get(grupo, 0.0) + float(fila[i_coste])
            if i_moneda is not None and not moneda:
                moneda = str(fila[i_moneda])
        except (IndexError, TypeError, ValueError):
            continue

    return {"por_grupo_recursos": importes, "moneda": moneda or "EUR"}


def asignar_grupo(nombre: str, cfg: dict) -> dict:
    """Primera coincidencia manda; si ninguna encaja, va a 'otros'."""
    for grupo in cfg["grupos"]:
        if matches_any(nombre, grupo.get("patrones")):
            return grupo
    return cfg["otros"]


def classify_backup(
    last_status: str | None,
    last_ts: str | None,
    sla_hours: float,
    protection_state: str | None = None,
) -> str:
    """Estado de la copia: ok | warn | fail | unknown."""
    status = (last_status or "").lower().replace(" ", "")
    state = (protection_state or "").lower().replace(" ", "")
    age = hours_since(last_ts)

    if state in ("protectionstopped", "protectionpaused", "backupsuspended"):
        return "warn"
    if state == "protectionerror":
        return "fail"

    if not status:
        return "unknown"
    if "fail" in status or "error" in status:
        return "fail"
    if "progress" in status or "warning" in status:
        return "warn"
    if "complet" in status or "success" in status or status in ("healthy", "passed"):
        return "warn" if (age is not None and age > sla_hours) else "ok"
    return "warn"


def power_state_from(instance_view) -> str:
    """Extrae 'running' / 'deallocated' / 'stopped' del instance view."""
    data = model_dict(instance_view)
    statuses = data.get("statuses") or getattr(instance_view, "statuses", None) or []
    for status in statuses:
        code = (model_dict(status).get("code") or getattr(status, "code", "") or "")
        if str(code).lower().startswith("powerstate/"):
            return str(code).split("/", 1)[1].lower()
    return ""


def agente_from(instance_view) -> dict:
    """Estado del agente de Azure: la causa habitual de los fallos de backup."""
    data = model_dict(instance_view)
    agente = data.get("vmAgent") or {}
    estado = ""
    for status in agente.get("statuses") or []:
        s = model_dict(status)
        estado = s.get("displayStatus") or s.get("code") or ""
        if estado:
            break
    return {"agente_estado": str(estado), "agente_version": str(agente.get("vmAgentVersion") or "")}


def resumen_politica(propiedades) -> dict:
    """Convierte una politica de backup en algo legible: frecuencia, hora y retencion."""
    d = model_dict(propiedades)
    sched = d.get("schedulePolicy") or {}
    ret = d.get("retentionPolicy") or {}

    frecuencia = str(sched.get("scheduleRunFrequency") or "").capitalize()
    hora = ""
    tiempos = sched.get("scheduleRunTimes") or []
    if tiempos:
        texto = str(tiempos[0])
        if "T" in texto and len(texto) >= 16:
            hora = texto[11:16]

    def duracion(bloque: dict | None) -> str:
        bloque = (bloque or {}).get("retentionDuration") or {}
        cuenta, unidad = bloque.get("count"), str(bloque.get("durationType") or "").lower()
        if not cuenta:
            return ""
        nombres = {"days": "días", "weeks": "semanas", "months": "meses", "years": "años"}
        return f"{cuenta} {nombres.get(unidad, unidad)}"

    extras = []
    for clave, etiqueta in (
        ("weeklySchedule", "semanal"),
        ("monthlySchedule", "mensual"),
        ("yearlySchedule", "anual"),
    ):
        texto = duracion(ret.get(clave))
        if texto:
            extras.append(f"{etiqueta} {texto}")

    return {
        "frecuencia": frecuencia,
        "hora": hora,
        "retencion": duracion(ret.get("dailySchedule")),
        "retencion_extra": ", ".join(extras),
    }


# --------------------------------------------------------------------------
# Recoleccion real desde Azure
# --------------------------------------------------------------------------


def collect_azure(days: int, sla_hours: float) -> dict:
    from azure.identity import ClientSecretCredential, DefaultAzureCredential
    from azure.mgmt.compute import ComputeManagementClient
    from azure.mgmt.recoveryservices import RecoveryServicesClient

    # A partir de azure-mgmt-recoveryservicesbackup 11 el cliente cuelga
    # directamente del paquete; en 3.x-9.x estaba bajo .activestamp
    try:
        from azure.mgmt.recoveryservicesbackup import RecoveryServicesBackupClient
    except ImportError:  # pragma: no cover
        from azure.mgmt.recoveryservicesbackup.activestamp import RecoveryServicesBackupClient

    subscription_id = env("AZURE_SUBSCRIPTION_ID", required=True)
    tenant_id = env("AZURE_TENANT_ID")
    client_id = env("AZURE_CLIENT_ID")
    client_secret = env("AZURE_CLIENT_SECRET")

    if tenant_id and client_id and client_secret:
        credential = ClientSecretCredential(tenant_id, client_id, client_secret)
    else:
        credential = DefaultAzureCredential()

    compute = ComputeManagementClient(credential, subscription_id)
    rsv_client = RecoveryServicesClient(credential, subscription_id)
    backup_client = RecoveryServicesBackupClient(credential, subscription_id)

    # La red es opcional: si falta el paquete, seguimos sin IPs
    network = None
    try:
        from azure.mgmt.network import NetworkManagementClient

        network = NetworkManagementClient(credential, subscription_id)
    except Exception:  # noqa: BLE001
        network = None

    ignore_patterns = env_list("VM_IGNORE_PATTERNS")
    grupos_cfg = cargar_grupos()
    errors: list[str] = []
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=days)

    # ----------------------------------------------------------------------
    # 1. Elementos protegidos de los vaults, indexados por resource id
    # ----------------------------------------------------------------------
    wanted = env_list("AZURE_VAULTS")
    vaults = []

    list_vaults = getattr(rsv_client.vaults, "list_by_subscription_id", None) or getattr(
        rsv_client.vaults, "list_by_subscription"
    )
    for vault in list_vaults():
        if wanted and vault.name not in wanted:
            continue
        vaults.append(
            {"name": vault.name, "resource_group": parse_rg(vault.id), "location": vault.location}
        )

    if not vaults:
        raise RuntimeError(
            "No se ha encontrado ningun Recovery Services vault accesible. "
            "Revisa AZURE_SUBSCRIPTION_ID, AZURE_VAULTS y los permisos del service principal."
        )

    backups_by_resource: dict[str, dict] = {}
    other_protected: list[dict] = []
    jobs: list[dict] = []
    politicas: dict[str, dict] = {}

    for vault in vaults:
        vname, vrg = vault["name"], vault["resource_group"]

        # --- Politicas: frecuencia, hora y retencion reales ------------------
        try:
            for politica in backup_client.backup_policies.list(vname, vrg):
                politicas[str(politica.name)] = resumen_politica(politica.properties)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"Politicas de {vname}: {exc}")

        try:
            for item in backup_client.backup_protected_items.list(vname, vrg):
                p = item.properties
                d = model_dict(p)

                friendly = pick(d, p, "friendlyName", "friendly_name") or item.name
                last_status = pick(d, p, "lastBackupStatus", "last_backup_status")
                last_time = iso(pick(d, p, "lastBackupTime", "last_backup_time"))
                protection_state = pick(d, p, "protectionState", "protection_state")
                mgmt = pick(d, p, "backupManagementType", "backup_management_type")

                if not last_status:
                    last_status = pick(d, p, "protectionStatus", "protection_status") or pick(
                        d, p, "healthStatus", "health_status"
                    )
                if not last_time:
                    last_time = iso(pick(d, p, "lastRecoveryPoint", "last_recovery_point"))

                source_id = str(
                    pick(d, p, "virtualMachineId", "virtual_machine_id")
                    or pick(d, p, "sourceResourceId", "source_resource_id")
                    or ""
                )

                record = {
                    "vault": vname,
                    "name": str(friendly),
                    "type": str(mgmt or "Unknown"),
                    "protection_state": str(protection_state or ""),
                    "health": str(pick(d, p, "healthStatus", "health_status") or ""),
                    "policy": str(pick(d, p, "policyName", "policy_name") or ""),
                    "last_backup_status": str(last_status or ""),
                    "last_backup_time": last_time,
                    "oldest_recovery_point": iso(pick(d, p, "oldestRecoveryPoint", "oldest_recovery_point")),
                    "last_recovery_point": iso(pick(d, p, "lastRecoveryPoint", "last_recovery_point")),
                    "backup_state": classify_backup(last_status, last_time, sla_hours, protection_state),
                }

                if source_id:
                    backups_by_resource[source_id.lower()] = record
                else:
                    # Azure Files y demas cargas no ligadas a una VM
                    grupo = asignar_grupo(str(friendly), grupos_cfg)
                    record.update(
                        {
                            "kind": "share",
                            "resource_group": parse_rg(
                                str(pick(d, p, "sourceResourceId", "source_resource_id") or "")
                            )
                            or vrg,
                            "location": "",
                            "power_state": "",
                            "os": "",
                            "size": "",
                            "state": record["backup_state"],
                            "protected": True,
                            "grupo": grupo.get("nombre", ""),
                            "grupo_color": grupo.get("color", "#94a3b8"),
                            "ip": "",
                            "tags": {},
                            "agente_estado": "",
                            "agente_version": "",
                            "politica_detalle": {},
                            "ultima_duracion_s": None,
                        }
                    )
                    other_protected.append(record)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"Items de {vname}: {exc}")

        try:
            fmt = "%Y-%m-%d %I:%M:%S %p"
            job_filter = f"startTime eq '{start.strftime(fmt)}' and endTime eq '{now.strftime(fmt)}'"
            for job in backup_client.backup_jobs.list(vname, vrg, filter=job_filter):
                p = job.properties
                d = model_dict(p)

                details = []
                for err in d.get("errorDetails") or getattr(p, "error_details", None) or []:
                    if isinstance(err, dict):
                        msg = err.get("errorString") or err.get("errorTitle")
                    else:
                        msg = getattr(err, "error_string", None) or getattr(err, "error_title", None)
                    if msg:
                        details.append(str(msg))

                jobs.append(
                    {
                        "vault": vname,
                        "name": str(pick(d, p, "entityFriendlyName", "entity_friendly_name") or job.name),
                        "operation": str(pick(d, p, "operation", "operation") or ""),
                        "status": str(pick(d, p, "status", "status") or ""),
                        "type": str(pick(d, p, "backupManagementType", "backup_management_type") or ""),
                        "start_time": iso(pick(d, p, "startTime", "start_time")),
                        "end_time": iso(pick(d, p, "endTime", "end_time")),
                        "duration_s": duration_seconds(pick(d, p, "duration", "duration")),
                        "errors": details,
                    }
                )
        except Exception as exc:  # noqa: BLE001
            errors.append(f"Jobs de {vname}: {exc}")

    # ----------------------------------------------------------------------
    # 2. Datos auxiliares: IP privada y ultimo job por maquina
    # ----------------------------------------------------------------------
    ips: dict[str, str] = {}
    if network is not None:
        try:
            for nic in network.network_interfaces.list_all():
                d = model_dict(nic)
                vm_ref = (d.get("virtualMachine") or {}).get("id")
                if not vm_ref:
                    continue
                for cfg in d.get("ipConfigurations") or []:
                    privada = model_dict(cfg).get("privateIPAddress")
                    if privada:
                        ips.setdefault(str(vm_ref).lower(), str(privada))
                        break
        except Exception as exc:  # noqa: BLE001
            errors.append(f"Direcciones IP: {exc}")

    ultimo_job: dict[str, dict] = {}
    for job in jobs:  # ya vienen sin ordenar; nos quedamos con el mas reciente
        clave = job["name"].lower()
        anterior = ultimo_job.get(clave)
        if not anterior or (job.get("start_time") or "") > (anterior.get("start_time") or ""):
            ultimo_job[clave] = job

    # ----------------------------------------------------------------------
    # 3. Inventario de VMs, cruzado con todo lo anterior
    # ----------------------------------------------------------------------
    items: list[dict] = []

    try:
        try:
            vm_list = list(compute.virtual_machines.list_all(status_only="true"))
        except TypeError:
            vm_list = list(compute.virtual_machines.list_all())

        for vm in vm_list:
            d = model_dict(vm)
            vm_id = str(getattr(vm, "id", "") or d.get("id") or "")
            name = str(getattr(vm, "name", "") or d.get("name") or "")

            hardware = d.get("hardwareProfile") or {}
            storage = d.get("storageProfile") or {}
            os_disk = storage.get("osDisk") or {}

            view = getattr(vm, "instance_view", None) or d.get("instanceView")
            power = power_state_from(view)
            agente = agente_from(view)
            if not power or not agente["agente_estado"]:
                try:
                    view = compute.virtual_machines.instance_view(parse_rg(vm_id), name)
                    power = power or power_state_from(view)
                    agente = agente_from(view) if not agente["agente_estado"] else agente
                except Exception:  # noqa: BLE001
                    pass

            backup = backups_by_resource.pop(vm_id.lower(), None)
            grupo = asignar_grupo(name, grupos_cfg)
            exenta = bool(grupo.get("exentas")) or matches_any(name, ignore_patterns)

            if backup:
                state = backup["backup_state"]
            elif exenta:
                state = "exento"
            else:
                state = "sincopia"

            items.append(
                {
                    "kind": "vm",
                    "name": name,
                    "grupo": grupo.get("nombre", ""),
                    "grupo_color": grupo.get("color", "#94a3b8"),
                    "resource_group": parse_rg(vm_id),
                    "location": str(getattr(vm, "location", "") or d.get("location") or ""),
                    "power_state": power,
                    "os": str(os_disk.get("osType") or ""),
                    "size": str(hardware.get("vmSize") or ""),
                    "protected": backup is not None,
                    "state": state,
                    "vault": backup["vault"] if backup else "",
                    "policy": backup["policy"] if backup else "",
                    "protection_state": backup["protection_state"] if backup else "",
                    "health": backup["health"] if backup else "",
                    "last_backup_status": backup["last_backup_status"] if backup else "",
                    "last_backup_time": backup["last_backup_time"] if backup else None,
                    "oldest_recovery_point": backup["oldest_recovery_point"] if backup else None,
                    "last_recovery_point": backup["last_recovery_point"] if backup else None,
                    "type": backup["type"] if backup else "AzureIaasVM",
                    "ip": ips.get(vm_id.lower(), ""),
                    "tags": {str(k): str(v) for k, v in (d.get("tags") or {}).items()},
                    "agente_estado": agente["agente_estado"],
                    "agente_version": agente["agente_version"],
                    "politica_detalle": politicas.get(backup["policy"], {}) if backup else {},
                    "ultima_duracion_s": (ultimo_job.get(name.lower()) or {}).get("duration_s"),
                }
            )
    except Exception as exc:  # noqa: BLE001
        errors.append(f"Inventario de VMs: {exc}")

    # Lo que queda en backups_by_resource son copias de VMs que ya no existen.
    # Por decision de diseno no se muestran, solo se cuentan.
    orphans = len(backups_by_resource)

    # Los elementos que no son VM (Azure Files) tambien llevan politica y duracion
    for extra in other_protected:
        extra["politica_detalle"] = politicas.get(extra.get("policy", ""), {})
        extra["ultima_duracion_s"] = (ultimo_job.get(extra["name"].lower()) or {}).get("duration_s")

    items.extend(other_protected)

    # ----------------------------------------------------------------------
    # 4. Coste del mes en curso (opcional: requiere Cost Management Reader)
    # ----------------------------------------------------------------------
    costes = {}
    if (env("COSTES", "0") or "0").lower() not in ("0", "no", "false"):
        try:
            costes = costes_por_grupo(credential, subscription_id)
        except Exception as exc:  # noqa: BLE001
            errors.append(
                "Costes no disponibles (hace falta el rol 'Cost Management Reader' "
                f"sobre la suscripcion): {exc}"
            )

    payload = build_payload(
        vaults, items, jobs, days, sla_hours, errors, orphans, ignore_patterns, grupos_cfg
    )
    payload["secreto"] = dias_para_caducar()
    payload["tenant_nombre"] = env("TENANT_NOMBRE", "") or ""
    payload["tenant_id"] = tenant_id or ""
    payload["subscription_id"] = subscription_id

    # Reparte el coste entre los grupos del dashboard. Si un grupo de recursos
    # tuviera maquinas de dos grupos distintos su coste contaria en ambos, asi
    # que conviene que cada grupo de recursos pertenezca a un solo area.
    por_rg = costes.get("por_grupo_recursos") or {}
    if por_rg:
        for grupo in payload["grupos"]:
            suyos = {
                str(i.get("resource_group", "")).lower()
                for i in items
                if i.get("grupo") == grupo["nombre"]
            }
            grupo["coste"] = round(sum(por_rg.get(rg, 0.0) for rg in suyos), 2)
        payload["coste_total"] = round(sum(por_rg.values()), 2)
        payload["moneda"] = costes.get("moneda", "EUR")

    return payload


# --------------------------------------------------------------------------
# Modo demo
# --------------------------------------------------------------------------


def collect_demo(days: int, sla_hours: float) -> dict:
    vaults = [{"name": "STNCERAMICA-backup", "resource_group": "rg_backup", "location": "westeurope"}]
    now = datetime.now(timezone.utc)
    ignore_patterns: list[str] = []
    grupos_cfg = cargar_grupos()

    plantilla = [
        ("vmproaddc1", "rg_ad", "running", "Windows", "Standard_B1ms", True, "Completed", 14),
        ("vmproadrds01", "rg_ad", "running", "Windows", "Standard_B2s", True, "Completed", 14),
        ("vmprocitrxapp02", "rg_pro_citrix", "running", "Windows", "Standard_D4s_v3", True, "Failed", 14),
        ("AVDVM-0", "rg_network", "running", "Windows", "Standard_B2s", False, "", 0),
        ("VDI-0", "VDITest-deployment", "deallocated", "Windows", "Standard_D2as_v5", False, "", 0),
        ("sgp-dev18-1", "sgp-dev18", "running", "Windows", "Standard_B8ms", False, "", 0),
        ("sgp-dev19-1", "sgp-dev19", "deallocated", "Windows", "Standard_B8ms", False, "", 0),
        ("sgp-pruebas1-1", "sgp-pruebas1", "deallocated", "Windows", "Standard_B8ms", False, "", 0),
    ]

    items: list[dict] = []
    jobs: list[dict] = []

    for name, rg, power, so, size, protegida, status, horas in plantilla:
        last = iso(now - timedelta(hours=horas)) if protegida else None
        grupo = asignar_grupo(name, grupos_cfg)
        if protegida:
            state = classify_backup(status, last, sla_hours)
        elif grupo.get("exentas"):
            state = "exento"
        else:
            state = "sincopia"

        items.append(
            {
                "kind": "vm",
                "name": name,
                "grupo": grupo.get("nombre", ""),
                "grupo_color": grupo.get("color", "#94a3b8"),
                "resource_group": rg,
                "location": "westeurope",
                "power_state": power,
                "os": so,
                "size": size,
                "protected": protegida,
                "state": state,
                "vault": "STNCERAMICA-backup" if protegida else "",
                "policy": "Dailypolicy" if protegida else "",
                "protection_state": "Protected" if protegida else "",
                "health": "Passed" if protegida else "",
                "last_backup_status": status,
                "last_backup_time": last,
                "oldest_recovery_point": iso(now - timedelta(days=30)) if protegida else None,
                "last_recovery_point": last,
                "type": "AzureIaasVM",
                "ip": f"10.50.70.{20 + len(items)}",
                "tags": {"entorno": "demo"},
                "agente_estado": "Ready" if power == "running" else "",
                "agente_version": "2.7.41491.1144",
                "politica_detalle": {
                    "frecuencia": "Daily",
                    "hora": "02:00",
                    "retencion": "30 días",
                    "retencion_extra": "",
                }
                if protegida
                else {},
                "ultima_duracion_s": random.randint(500, 2700) if protegida else None,
            }
        )

        if protegida:
            for d in range(days):
                inicio = now - timedelta(days=d, hours=random.uniform(0, 3))
                fallo = status == "Failed" and d < 2
                jobs.append(
                    {
                        "vault": "STNCERAMICA-backup",
                        "name": name,
                        "operation": "Backup",
                        "status": "Failed" if fallo else "Completed",
                        "type": "AzureIaasVM",
                        "start_time": iso(inicio),
                        "end_time": iso(inicio + timedelta(minutes=random.randint(8, 45))),
                        "duration_s": random.randint(500, 2700),
                        "errors": ["UserErrorGuestAgentStatusUnavailable: el agente de la VM no responde."]
                        if fallo
                        else [],
                    }
                )

    grupo_share = asignar_grupo("informatica", grupos_cfg)
    items.append(
        {
            "kind": "share",
            "name": "informatica (integracion365fo)",
            "grupo": grupo_share.get("nombre", ""),
            "grupo_color": grupo_share.get("color", "#94a3b8"),
            "resource_group": "rg_backup",
            "location": "westeurope",
            "power_state": "",
            "os": "",
            "size": "",
            "protected": True,
            "state": "ok",
            "vault": "STNCERAMICA-backup",
            "policy": "Dailypolicy",
            "protection_state": "Protected",
            "health": "Passed",
            "last_backup_status": "Completed",
            "last_backup_time": iso(now - timedelta(hours=9)),
            "oldest_recovery_point": iso(now - timedelta(days=30)),
            "last_recovery_point": iso(now - timedelta(hours=9)),
            "type": "AzureStorage",
        }
    )

    payload = build_payload(vaults, items, jobs, days, sla_hours, [], 4, ignore_patterns, grupos_cfg)
    payload["demo"] = True
    return payload


# --------------------------------------------------------------------------
# Payload + render
# --------------------------------------------------------------------------

ORDEN_ESTADO = {"fail": 0, "sincopia": 1, "warn": 2, "unknown": 3, "exento": 4, "ok": 5}


def build_payload(
    vaults, items, jobs, days, sla_hours, errors, orphans, ignore_patterns, grupos_cfg=None
) -> dict:
    jobs.sort(key=lambda j: j.get("start_time") or "", reverse=True)
    items.sort(key=lambda i: (ORDEN_ESTADO.get(i.get("state"), 9), i.get("name", "").lower()))

    failed_jobs = [j for j in jobs if classify_backup(j.get("status"), None, sla_hours) == "fail"]

    counts = {"ok": 0, "warn": 0, "fail": 0, "sincopia": 0, "exento": 0, "unknown": 0}
    for item in items:
        state = item.get("state", "unknown")
        counts[state] = counts.get(state, 0) + 1

    vms = [i for i in items if i.get("kind") == "vm"]

    # Grupos en el orden del fichero de configuracion, con su resumen.
    # Solo se publican los que tienen al menos un elemento.
    cfg = grupos_cfg or {"grupos": [], "otros": dict(OTROS_POR_DEFECTO)}
    grupos = []
    for definicion in list(cfg["grupos"]) + [cfg["otros"]]:
        nombre = definicion.get("nombre", "")
        miembros = [i for i in items if i.get("grupo") == nombre]
        if not miembros:
            continue
        resumen = {"ok": 0, "warn": 0, "fail": 0, "sincopia": 0, "exento": 0, "unknown": 0}
        for m in miembros:
            resumen[m.get("state", "unknown")] = resumen.get(m.get("state", "unknown"), 0) + 1
        grupos.append(
            {
                "nombre": nombre,
                "color": definicion.get("color", "#94a3b8"),
                "exentas": bool(definicion.get("exentas")),
                "total": len(miembros),
                "counts": resumen,
            }
        )

    if counts["fail"] or failed_jobs:
        overall = "fail"
    elif counts["sincopia"] or counts["warn"] or counts["unknown"]:
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
        "grupos": grupos,
        "items": items,
        "jobs": jobs,
        "failed_jobs": len(failed_jobs),
        "total_jobs": len(jobs),
        "total_vms": len(vms),
        "vms_encendidas": len([v for v in vms if v.get("power_state") == "running"]),
        "vms_protegidas": len([v for v in vms if v.get("protected")]),
        "huerfanos": orphans,
        "ignore_patterns": ignore_patterns,
        "collector_errors": errors,
        "demo": False,
    }


def render(payload: dict, out_dir: Path) -> None:
    """Escribe data.json de forma atomica: serve.py lo sirve en /api/data."""
    out_dir.mkdir(parents=True, exist_ok=True)
    destino = out_dir / "data.json"
    temporal = out_dir / "data.json.tmp"
    temporal.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporal.replace(destino)


# --------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="Dashboard de VMs y copias de Azure")
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
            "counts": {"ok": 0, "warn": 0, "fail": 0, "sincopia": 0, "exento": 0, "unknown": 0},
            "vaults": [],
            "grupos": [],
            "items": [],
            "jobs": [],
            "failed_jobs": 0,
            "total_jobs": 0,
            "total_vms": 0,
            "vms_encendidas": 0,
            "vms_protegidas": 0,
            "huerfanos": 0,
            "ignore_patterns": [],
            "collector_errors": [f"El colector no pudo ejecutarse: {exc}"],
            "demo": False,
        }
        render(payload, Path(args.out))
        return 1

    render(payload, Path(args.out))

    try:
        import historico

        historico.registrar_backups(Path(args.out), payload.get("jobs", []), payload.get("counts", {}))
    except Exception as exc:  # noqa: BLE001
        print(f"[AVISO] Historico no disponible: {exc}")

    print(
        f"OK -> {args.out}/data.json | estado={payload['overall']} | "
        f"VMs={payload['total_vms']} protegidas={payload['vms_protegidas']} "
        f"sin copia={payload['counts']['sincopia']} | jobs={payload['total_jobs']} "
        f"fallidos={payload['failed_jobs']} | huerfanos ocultos={payload['huerfanos']}"
    )
    return 2 if payload["overall"] == "fail" else 0


if __name__ == "__main__":
    raise SystemExit(main())
