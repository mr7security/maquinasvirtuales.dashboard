# Dashboard de estado de Azure Backup — Grupo STN

Automatiza el procedimiento manual documentado en OneNote ("COPIAS AZURE"): en lugar
de entrar al portal, abrir el vault, revisar cada máquina y filtrar los jobs a mano,
un colector en Python consulta la API de Azure cada 30 minutos y publica un dashboard
HTML con el estado actual.

Cruza dos fuentes que en el portal están separadas: el **inventario de máquinas
virtuales** de la suscripción y los **elementos protegidos** de los Recovery
Services vaults.

## Qué muestra

- **Semáforo global** — verde / ámbar / rojo de un vistazo
- **Tarjeta por máquina virtual** — estado de energía, SO, tamaño, grupo de recursos,
  y si tiene copia de seguridad, cuándo fue la última y con qué política
- **Máquinas sin copia** — aviso propio en morado. Es el hueco que el procedimiento
  manual nunca detectaba: si una VM no está protegida, no genera jobs, y por tanto
  no aparece como fallo en ninguna parte
- **Máquinas exentas** — las de desarrollo, pruebas o VDI se muestran en gris y no
  alertan (configurable, ver `VM_IGNORE_PATTERNS`)
- **Copias huérfanas ocultas** — elementos del vault cuya VM ya no existe. No se
  muestran; solo se cuentan en el pie
- **Gráfico de jobs por día** y **tabla filtrable** del histórico, con el mensaje de
  error de cada job fallido

---

## 1. Crear el Service Principal en Azure

Desde Cloud Shell o con Azure CLI, una sola vez:

```bash
SUB=<id-de-suscripcion>

az ad sp create-for-rbac --name "sp-dashboard-copias-azure" --role "Backup Reader" --scopes "/subscriptions/$SUB"

# Necesario ademas para leer el inventario de VMs
az role assignment create --assignee <appId> --role "Reader" --scopes "/subscriptions/$SUB"
```

Hacen falta **los dos roles**: `Backup Reader` para los vaults y `Reader` para el
inventario de máquinas. Ambos son de solo lectura.

> En Cloud Shell con PowerShell, el `\` no vale como continuación de línea: pon
> cada comando en una sola línea.
>
> Si la suscripción cuelga de un tenant distinto al de tu cuenta, primero
> `az login --tenant <id-del-tenant> --use-device-code`.

Devuelve algo así — guarda estos tres valores, el `password` no se vuelve a mostrar:

```json
{
  "appId":    "...",   ->  AZURE_CLIENT_ID
  "password": "...",   ->  AZURE_CLIENT_SECRET
  "tenant":   "..."    ->  AZURE_TENANT_ID
}
```

**Backup Reader** es un rol de solo lectura: puede consultar items, jobs y puntos de
restauración, pero no puede lanzar, detener ni borrar copias. Si prefieres acotar más,
usa `--scopes` apuntando al resource group del vault en lugar de a la suscripción.

> Alternativa sin secreto: si el servidor Ubuntu es una VM de Azure, asígnale una
> Managed Identity con el rol Backup Reader y deja `AZURE_CLIENT_ID`/`SECRET` vacíos.
> El script usa `DefaultAzureCredential` automáticamente.

## 2. Instalar en el servidor Ubuntu

Sigue el mismo patrón que el resto de dashboards de la máquina (`dashboard-sensores`,
`dashboard-radioenlaces`): servicio Python propio bajo `/opt`, usuario dedicado y
systemd. **No usa nginx.**

```bash
sudo apt install -y git
git clone https://github.com/mr7security/maquinasvirtuales.dashboard.git
cd maquinasvirtuales.dashboard
sudo bash deploy/install.sh

# Rellena las credenciales
sudo nano /opt/dashboard-copias-azure/.env

# Primera recogida
sudo systemctl start dashboard-copias-azure-collector.service
sudo journalctl -u dashboard-copias-azure-collector -n 50 --no-pager
```

El instalador crea el usuario `dashboard-copias`, el virtualenv, el servicio web
y el timer de recogida (cada 30 min). Aborta si el puerto 8090 está ocupado.

### Puertos en uso en la máquina

| Puerto | Servicio |
|---|---|
| 8000 | `dashboard-sensores` |
| 8080 | `dashboard-radioenlaces` |
| 8090 | `dashboard-copias-azure` (este) |

Para cambiarlo, edita `PORT` en `deploy/install.sh` y `--port` en
`deploy/dashboard-copias-azure.service`.

## 3. Comprobar

```bash
systemctl status dashboard-copias-azure
systemctl list-timers dashboard-copias-azure-collector.timer
curl -s localhost:8090/data.json | head -40
```

El dashboard queda en `http://<servidor>:8090`.

## Arquitectura

Dos unidades de systemd, separadas a propósito:

- `dashboard-copias-azure.service` — servidor web (`serve.py`), siempre activo,
  `Restart=always`. Solo sirve ficheros estáticos, no habla con Azure.
- `dashboard-copias-azure-collector.timer` — dispara `collect.py` cada 30 min,
  que consulta Azure y regenera `public/index.html` y `public/data.json`.

Si Azure no responde, el dashboard sigue en pie mostrando el último dato bueno y
un banner con el error.

---

## Probar el diseño sin credenciales

```bash
python3 collect.py --demo --out ./public
python3 serve.py --port 8090 --dir ./public
```

Y abre `http://<servidor>:8090`.

Genera un dashboard con las 7 VMs reales del procedimiento y datos simulados,
incluyendo una máquina en fallo y otra fuera de SLA para ver los avisos.

## Opciones

| Opción | Por defecto | Qué hace |
|---|---|---|
| `--days N` | 7 | Días de histórico de jobs |
| `--sla-hours H` | 26 | Horas sin backup correcto antes de marcar "Atención" |
| `--out RUTA` | `./public` | Dónde escribir `index.html` y `data.json` |
| `--demo` | — | Datos de ejemplo, sin conectar a Azure |

Equivalen a las variables `BACKUP_DAYS`, `BACKUP_SLA_HOURS` y `OUTPUT_DIR` del `.env`.

### Grupos y exenciones — `grupos.json`

Las tarjetas se agrupan por área, cada una con su color, igual que las sedes del
dashboard de sensores. Se configura en `grupos.json`:

```json
{
  "grupos": [
    {"nombre": "Sistemas / Infraestructuras", "color": "#38bdf8", "exentas": false,
     "patrones": ["vmproaddc1", "vmproadrds01", "vmprocitrxapp02", "informatica*"]},
    {"nombre": "Desarrollo", "color": "#a78bfa", "exentas": true,
     "patrones": ["sgp-dev*"]},
    {"nombre": "Testing", "color": "#f472b6", "exentas": true,
     "patrones": ["AVDVM-*", "AA-PingVm-*", "sgp-pruebas*", "VDI-*", "VDIVM-*"]}
  ],
  "otros": {"nombre": "Sin clasificar", "color": "#f59e0b", "exentas": false}
}
```

Se evalúa en orden y la primera coincidencia manda. Los patrones admiten comodines
y no distinguen mayúsculas.

`exentas: true` significa que a ese grupo no se le exige copia: sus máquinas salen
en gris y no generan aviso. Es lo que evita que Desarrollo y Testing tapen las
incidencias reales.

Toda VM que no encaje en ningún grupo cae en **Sin clasificar**, que sí exige copia.
Así una máquina nueva se hace notar en lugar de pasar desapercibida.

`install.sh` no sobrescribe `grupos.json` si ya existe en el servidor: es
configuración, no código.

## Códigos de salida

| Código | Significado |
|---|---|
| 0 | Todo correcto |
| 1 | El colector no pudo conectar con Azure |
| 2 | Hay elementos o jobs en fallo |

Útiles para engancharlo a Nagios, Zabbix o un check externo.

---

## Estructura

```
maquinasvirtuales.dashboard/
├── collect.py                  Colector: Azure SDK -> data.json
├── serve.py                    Servidor web y API (solo libreria estandar)
├── dashboard.html              Interfaz; consulta /api/data cada 60 s
├── grupos.json                 Agrupacion, colores y exenciones
├── logo.png                    Opcional: se sirve en /logo
├── requirements.txt
├── .env.example
├── .gitignore
└── deploy/
    ├── install.sh                                Instalador para Ubuntu
    ├── dashboard-copias-azure.service            Servidor web, siempre activo
    ├── dashboard-copias-azure-collector.service  Recogida (oneshot)
    └── dashboard-copias-azure-collector.timer    Cada 30 min
```

## Pestaña 2 — Procesos BI

Automatiza el segundo procedimiento de OneNote ("REVISAR TODOS LOS DÍAS AZURE"):
la revisión diaria de los runbooks de Azure Automation de `automationsrvprobi01`.

El procedimiento manual avisa de que **"que aparezca Completada no quiere decir que
esté OK, hay que revisar que no tenga errores"**. El colector hace justo eso: además
del estado del job, descarga los registros de la última ejecución y cuenta errores y
advertencias reales. Un runbook `Completed` con un error dentro sale en rojo, con el
texto del error en la propia tarjeta.

Y detecta algo que la revisión manual no ve: un runbook que **no se ha ejecutado**.
Para saber cuándo debería haberlo hecho lee las programaciones configuradas en la
cuenta de Automation, así que se adapta solo si cambiáis los horarios.

Configuración en el `.env`:

```
AUTOMATION_ACCOUNT=automationsrvprobi01
AUTOMATION_RESOURCE_GROUP=rg_pro_bi
AUTOMATION_RUNBOOKS=ProcAlm_ProcessAll,ProcAlm_CarteraFecha,Process_AS_Cartera,Process_AS_Cartera_Fecha,Process_AS_Finanzas
RUNBOOK_MARGEN_HORAS=3
```

`RUNBOOK_MARGEN_HORAS` es el tiempo que se espera tras la hora programada antes de
dar un proceso por no ejecutado. Debe cubrir lo que tarda el más lento.

Fuera de alcance por ahora, documentado por si se retoma:

- **Trabajos por lotes de D365 Finance & Operations.** No es Azure Resource Manager;
  va por la API OData de F&O y requiere registrar la aplicación dentro del entorno
- **Correo automático a soportebi@ifr.es.** Conviene esperar a comprobar que la
  detección no da falsos positivos antes de automatizar avisos a un proveedor externo

## Rutas del servidor

| Ruta | Qué devuelve |
|---|---|
| `/` | La interfaz (`dashboard.html`) |
| `/api/data` | Copias y máquinas virtuales. La página lo consulta cada 60 s |
| `/api/procesos` | Estado de los runbooks de Automation |
| `/csv` | Exportación del estado actual, separada por `;` y con BOM para Excel |
| `/logo` | `logo.png` del directorio de la aplicación, si existe |
| `/salud` | Devuelve el estado global en texto plano; 503 si aún no hay datos |

`collect.py` escribe `data.json` de forma atómica, así que la página nunca lee un
fichero a medio escribir.

## Interfaz

Misma estética que `dashboard-sensores` y `dashboard-radioenlaces`. Incluye:

- **Resumen en franja** — contadores centrados en una línea con separadores
- **Tarjetas compactas** agrupadas por área, con el color del grupo
- **Cartel de sin conexión** — si el servidor deja de responder durante tres sondeos
  seguidos aparece a pantalla completa con el tiempo transcurrido
- **Aviso de datos obsoletos** — si el colector lleva más de 90 minutos sin ejecutarse

No lleva modo TV, alertas sonoras ni botón de descarga: este panel se consulta desde
el navegador, no se deja puesto en un monitor. La ruta `/csv` sigue disponible por si
alguien quiere la exportación, simplemente no hay botón.

## Si prefieres cron en vez de systemd

```cron
*/30 * * * * cd /opt/dashboard-copias-azure && ./venv/bin/python collect.py --out ./public >> /var/log/dashboard-copias-azure.log 2>&1
```

## Siguientes pasos posibles

- Aviso por correo o Teams cuando `overall` pase a `fail` (webhook en el colector)
- Ampliar a otras suscripciones: el colector ya descubre todos los vaults visibles,
  basta con instanciarlo por suscripción o dar permiso al mismo SP en varias
- Guardar histórico en SQLite para tendencias a más de 30 días (la API de Azure solo
  conserva los jobs recientes)

## Notas

- La API de Azure Backup limita el histórico de jobs; para ventanas largas conviene
  persistir en local o usar Azure Monitor / LA Workspace.
- `Backup Reader` no da acceso a los datos de las copias, solo a su metadata.
- Protege el `.env` (`chmod 600`) — contiene el secreto del Service Principal.
- **Nunca subas el `.env` a GitHub.** El `.gitignore` ya lo excluye; sube solo
  `.env.example`. Si alguna vez se te cuela un secreto en un commit, rota el
  secreto del Service Principal en Azure — borrar el commit no basta.
- Aunque el repositorio sea privado, en `.env.example` no dejes datos reales de
  la infraestructura más allá de los estrictamente necesarios.
