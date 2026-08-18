#!/usr/bin/env bash
# Instalación del dashboard de Azure Backup en Ubuntu 22.04 / 24.04
# Ejecutar como root desde el directorio del proyecto:  sudo bash deploy/install.sh
set -euo pipefail

APP_DIR=/opt/backup-dashboard
WEB_DIR=/var/www/backup-dashboard
APP_USER=backupdash
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "==> Instalando dependencias del sistema"
apt-get update -qq
apt-get install -y python3-venv python3-pip nginx

echo "==> Creando usuario de servicio"
id -u "$APP_USER" &>/dev/null || useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"

echo "==> Copiando la aplicación a $APP_DIR"
mkdir -p "$APP_DIR" "$WEB_DIR"
cp "$SRC_DIR/collect.py" "$SRC_DIR/dashboard_template.html" "$SRC_DIR/requirements.txt" "$APP_DIR/"
[[ -f "$APP_DIR/.env" ]] || cp "$SRC_DIR/.env.example" "$APP_DIR/.env"

echo "==> Creando el entorno virtual"
python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"

echo "==> Ajustando permisos"
chown -R "$APP_USER:www-data" "$APP_DIR" "$WEB_DIR"
chmod 600 "$APP_DIR/.env"
chmod 755 "$WEB_DIR"

echo "==> Instalando el servicio systemd"
cp "$SRC_DIR/deploy/backup-dashboard.service" /etc/systemd/system/
cp "$SRC_DIR/deploy/backup-dashboard.timer"   /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now backup-dashboard.timer

echo "==> Instalando la configuración de nginx"
cp "$SRC_DIR/deploy/nginx-backup-dashboard.conf" /etc/nginx/sites-available/backup-dashboard
ln -sf /etc/nginx/sites-available/backup-dashboard /etc/nginx/sites-enabled/backup-dashboard
nginx -t && systemctl reload nginx

cat <<EOF

-------------------------------------------------------------------
Instalación terminada.

SIGUIENTE PASO OBLIGATORIO: edita las credenciales
    sudo nano $APP_DIR/.env

Después lanza la primera recogida:
    sudo systemctl start backup-dashboard.service
    sudo journalctl -u backup-dashboard.service -n 50

El dashboard queda en $WEB_DIR/index.html y se sirve por nginx.
Ajusta 'server_name' en /etc/nginx/sites-available/backup-dashboard.
-------------------------------------------------------------------
EOF
