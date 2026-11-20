#!/usr/bin/env bash
# Despliegue de BI PROSIC en este servidor (podman-compose + cPanel/Apache).
#
# Uso:
#   ./deploy.sh                    git pull + reconstruye backend y frontend
#   ./deploy.sh backend            solo backend (y lo que dependa de él)
#   ./deploy.sh --no-pull          sin git pull (despliega lo que hay en disco)
#   ./deploy.sh --env              solo recrea backend para releer .env
#   ./deploy.sh --no-pull frontend
#
# No toca volúmenes (bi_data conserva el historial de flota) ni el .env.
set -uo pipefail

cd "$(dirname "$(readlink -f "$0")")"

PORT="${FRONTEND_PORT_DEPLOY:-8101}"
PULL=1
SERVICES=()

for arg in "$@"; do
  case "$arg" in
    --no-pull) PULL=0 ;;
    --env)     PULL=0; SERVICES=(backend) ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    backend|frontend|redis) SERVICES+=("$arg") ;;
    *) echo "Argumento desconocido: $arg (usa --help)"; exit 2 ;;
  esac
done
[ ${#SERVICES[@]} -eq 0 ] && SERVICES=(backend frontend)

log()  { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m!! %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31mXX %s\033[0m\n' "$*"; exit 1; }

command -v podman-compose >/dev/null || die "podman-compose no está instalado"
[ -f .env ] || die "Falta .env"

# --- 0. Avisos sobre .env (claves repetidas: gana la última) ----------------
dups=$(grep -E '^[A-Z_]+=' .env | cut -d= -f1 | sort | uniq -d | tr '\n' ' ')
[ -n "$dups" ] && warn "Claves repetidas en .env (gana la última): ${dups}"

# --- 1. git pull ------------------------------------------------------------
if [ "$PULL" -eq 1 ]; then
  log "git pull"
  if [ -n "$(git status --porcelain)" ]; then
    warn "Hay cambios locales sin commit; se omite el pull."
    git status -s
  else
    git pull --ff-only || die "git pull falló (¿historia divergente?)"
  fi
fi
git log -1 --format='Versión: %h %s'

# --- 2. Liberar montajes que cPanel filtra a /home/virtfs -------------------
# Sin esto, 'podman rm' falla con "device or resource busy".
liberar_montajes() {
  findmnt -rn -o TARGET 2>/dev/null \
    | grep '^/home/virtfs/.*/var/lib/containers/storage/' \
    | sort -r | while read -r m; do umount "$m" 2>/dev/null; done
}

# Vigilante: mientras corre el script, libera continuamente los montajes filtrados
# (el build y 'up' también los generan y fallan al limpiar).
( while :; do liberar_montajes; sleep 1; done ) &
VIGILANTE=$!
trap 'kill "$VIGILANTE" 2>/dev/null' EXIT

# --- 3. Quitar contenedores viejos de los servicios a desplegar -------------
log "Deteniendo: ${SERVICES[*]}"
liberar_montajes
for svc in "${SERVICES[@]}"; do
  names=$(podman ps -a --filter "label=io.podman.compose.service=${svc}" --format '{{.Names}}')
  [ -z "$names" ] && names="biprosic_${svc}_1"
  for name in $names; do
    podman rm -f --depend "$name" >/dev/null 2>&1
    liberar_montajes
    # Entradas huérfanas que solo existen en el almacenamiento
    podman rm -f --storage "$name" >/dev/null 2>&1
  done
done
liberar_montajes

# --- 4. Construir y levantar ------------------------------------------------
log "Construyendo imágenes"
if ! timeout 900 podman-compose build "${SERVICES[@]}"; then
  warn "build devolvió error; reintento tras liberar montajes"
  liberar_montajes
  timeout 900 podman-compose build "${SERVICES[@]}" || die "Falló el build"
fi
# Limpia contenedores de build que quedaron huérfanos por el bloqueo de montajes
liberar_montajes
command -v buildah >/dev/null && buildah rm --all >/dev/null 2>&1

log "Levantando contenedores"
liberar_montajes
if ! timeout 300 podman-compose up -d; then
  warn "up -d falló; reintento tras liberar montajes"
  liberar_montajes
  timeout 300 podman-compose up -d || die "up -d falló. Revisa: podman ps -a --storage"
fi

# --- 5. Verificación --------------------------------------------------------
log "Verificando"
ok=0
for i in $(seq 1 30); do
  if curl -fsS -m 5 "http://127.0.0.1:${PORT}/api/ping" >/dev/null 2>&1; then ok=1; break; fi
  sleep 2
done
podman ps --format 'table {{.Names}}\t{{.Status}}'
if [ "$ok" -eq 1 ]; then
  printf '\n\033[1;32mOK\033[0m  /api/ping responde en 127.0.0.1:%s\n' "$PORT"
else
  warn "No responde /api/ping en 127.0.0.1:${PORT}. Últimos logs del backend:"
  podman logs --tail 30 biprosic_backend_1 2>&1 | tail -30
  exit 1
fi

# Conexión con Odoo (usa las variables que realmente cargó el contenedor)
if podman exec biprosic_backend_1 python -c "
import urllib.request, json, os
body = {'jsonrpc':'2.0','method':'call','params':{'service':'common','method':'authenticate',
        'args':[os.environ['ODOO_DB'], os.environ['ODOO_USER'], os.environ['ODOO_API_KEY'], {}]}}
req = urllib.request.Request(os.environ['ODOO_URL'] + '/jsonrpc', json.dumps(body).encode(),
                             {'Content-Type': 'application/json'})
uid = json.load(urllib.request.urlopen(req, timeout=15)).get('result')
raise SystemExit(0 if uid else 1)
" 2>/dev/null; then
  echo "OK  Odoo autentica correctamente"
else
  warn "El backend NO logra autenticarse en Odoo: revisa ODOO_URL / ODOO_DB / ODOO_USER / ODOO_API_KEY en .env"
  exit 1
fi
