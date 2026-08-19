#!/usr/bin/env bash
# Instalación del dashboard de copias de Azure en Ubuntu.
# Sigue el mismo patrón que dashboard-sensores y dashboard-radioenlaces:
# servicio Python propio bajo /opt, usuario dedicado, systemd. Sin nginx.
#
# Ejecutar como root desde el directorio del proyecto:  sudo bash deploy/install.sh
set -euo pipefail

APP_NAME=dashboard-copias-azure
APP_DIR=/opt/$APP_NAME
APP_USER=dashboard-copias
PORT=8090
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "==> Parando el servicio si ya estaba instalado (reinstalacion)"
systemctl stop "$APP_NAME.service" 2>/dev/null || true
sleep 1

echo "==> Comprobando que el puerto $PORT está libre"
if ss -ltn "( sport = :$PORT )" | grep -q LISTEN; then
    echo "ERROR: el puerto $PORT lo ocupa otro proceso ajeno a este dashboard."
    echo "       Cambia PORT en este script y --port en deploy/$APP_NAME.service."
    ss -ltnp "( sport = :$PORT )"
    exit 1
fi

echo "==> Instalando dependencias del sistema"
# En Ubuntu con Python nuevo (3.13/3.14) el paquete venv va versionado
PYVER="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
apt-get update -qq
apt-get install -y python3-pip "python${PYVER}-venv" || apt-get install -y python3-pip python3-venv

echo "==> Creando usuario de servicio $APP_USER"
id -u "$APP_USER" &>/dev/null || useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"

echo "==> Copiando la aplicación a $APP_DIR"
mkdir -p "$APP_DIR/public"
install -m 644 "$SRC_DIR/collect.py"       "$APP_DIR/collect.py"
install -m 644 "$SRC_DIR/serve.py"         "$APP_DIR/serve.py"
install -m 644 "$SRC_DIR/dashboard.html"   "$APP_DIR/dashboard.html"
install -m 644 "$SRC_DIR/requirements.txt" "$APP_DIR/requirements.txt"
# grupos.json es configuracion: no se pisa si ya existe en el servidor
[[ -f "$APP_DIR/grupos.json" ]] || install -m 644 "$SRC_DIR/grupos.json" "$APP_DIR/grupos.json"
rm -f "$APP_DIR/dashboard_template.html" "$APP_DIR/public/index.html"
if [[ -f "$SRC_DIR/logo.png" ]]; then install -m 644 "$SRC_DIR/logo.png" "$APP_DIR/logo.png"; fi
[[ -f "$APP_DIR/.env" ]] || install -m 600 "$SRC_DIR/.env.example" "$APP_DIR/.env"

echo "==> Creando el entorno virtual"
python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"

echo "==> Ajustando permisos"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"
chmod 600 "$APP_DIR/.env"
chmod 750 "$APP_DIR"

echo "==> Instalando las unidades de systemd"
install -m 644 "$SRC_DIR/deploy/$APP_NAME.service"             /etc/systemd/system/
install -m 644 "$SRC_DIR/deploy/$APP_NAME-collector.service"   /etc/systemd/system/
install -m 644 "$SRC_DIR/deploy/$APP_NAME-collector.timer"     /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now "$APP_NAME.service"
systemctl enable --now "$APP_NAME-collector.timer"

cat <<EOF

-------------------------------------------------------------------
Instalación terminada.

SIGUIENTE PASO OBLIGATORIO: credenciales del Service Principal
    sudo nano $APP_DIR/.env

Después lanza la primera recogida:
    sudo systemctl start $APP_NAME-collector.service
    sudo journalctl -u $APP_NAME-collector -n 50 --no-pager

Dashboard:      http://\$(hostname -I | awk '{print \$1}'):$PORT
Estado:         systemctl status $APP_NAME
Proxima recogida: systemctl list-timers $APP_NAME-collector.timer

Si usais ufw:   sudo ufw allow $PORT/tcp
-------------------------------------------------------------------
EOF
