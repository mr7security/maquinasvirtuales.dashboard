# Dashboard de estado de Azure Backup — Grupo STN

Automatiza el procedimiento manual documentado en OneNote ("COPIAS AZURE"): en lugar
de entrar al portal, abrir el vault, revisar cada máquina y filtrar los jobs a mano,
un colector en Python consulta la API de Azure cada 30 minutos y publica un dashboard
HTML con el estado actual.

## Qué muestra

- **Semáforo global** — verde / ámbar / rojo de un vistazo
- **Tarjeta por elemento protegido** — las 7 VMs + Azure Files, con estado del último
  backup, antigüedad, política y punto de restauración más antiguo
- **Aviso destacado** de lo que requiere atención (fallo, o más de 26 h sin backup correcto)
- **Gráfico de jobs por día** — correctos vs. fallidos
- **Tabla filtrable y ordenable** del histórico, con el mensaje de error de cada job fallido

---

## 1. Crear el Service Principal en Azure

Desde Cloud Shell o con Azure CLI, una sola vez:

```bash
SUB=7d2eabfa-c2ea-4cf9-aee1-570480d4e134   # STN Cerámica Nulense

az ad sp create-for-rbac \
  --name "sp-backup-dashboard" \
  --role "Backup Reader" \
  --scopes "/subscriptions/$SUB"
```

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

```bash
# En el servidor
sudo apt install -y git
git clone https://github.com/<usuario>/azure-backup-dashboard.git
cd azure-backup-dashboard
sudo bash deploy/install.sh

# Rellena las credenciales
sudo nano /opt/backup-dashboard/.env

# Primera recogida
sudo systemctl start backup-dashboard.service
sudo journalctl -u backup-dashboard.service -n 50
```

El instalador crea el usuario de servicio `backupdash`, el virtualenv, el timer de
systemd (cada 30 min) y el site de nginx.

## 3. Comprobar

```bash
systemctl list-timers backup-dashboard.timer   # próxima ejecución
curl -s localhost/data.json | head -40         # datos en crudo
```

El dashboard se sirve en `http://<servidor>/`. Edita `server_name` y el bloque
`allow/deny` en `/etc/nginx/sites-available/backup-dashboard` para tu red.

---

## Probar el diseño sin credenciales

```bash
python3 collect.py --demo --out ./public
xdg-open public/index.html
```

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
azure-backup-dashboard/
├── collect.py                  Colector: Azure SDK -> data.json + index.html
├── dashboard_template.html     Plantilla (el JSON se inyecta en el HTML)
├── requirements.txt
├── .env.example
└── deploy/
    ├── install.sh              Instalador para Ubuntu
    ├── backup-dashboard.service
    ├── backup-dashboard.timer  Cada 30 min
    └── nginx-backup-dashboard.conf
```

El HTML resultante es autocontenido: el JSON va incrustado, así que no hay llamadas
`fetch` ni problemas de CORS. `data.json` se escribe también por separado por si
quieres consumirlo desde otro sistema.

## Si prefieres cron en vez de systemd

```cron
*/30 * * * * cd /opt/backup-dashboard && ./venv/bin/python collect.py >> /var/log/backup-dashboard.log 2>&1
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
