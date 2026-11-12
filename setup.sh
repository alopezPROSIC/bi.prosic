#!/usr/bin/env bash
# Asistente de configuración de PROSIC BI (Linux/macOS).
# Pregunta los datos, valida Odoo, genera JWT_SECRET y hashes, escribe .env y opcionalmente levanta docker compose.
#   bash setup.sh
# Si ya existe .env, sus valores son los predeterminados (Enter = conservar) y se respalda como .env.bak-<fecha>.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_PATH="$ROOT/.env"
EXAMPLE="$ROOT/.env.example"
[ -f "$EXAMPLE" ] || { echo "No se encontró $EXAMPLE"; exit 1; }

c_ok=$'\e[32m'; c_warn=$'\e[33m'; c_dim=$'\e[90m'; c_step=$'\e[36m'; c_off=$'\e[0m'
step() { printf '\n%s== %s ==%s\n' "$c_step" "$1" "$c_off"; }
warn() { printf '%s%s%s\n' "$c_warn" "$1" "$c_off"; }
dim()  { printf '%s%s%s\n' "$c_dim" "$1" "$c_off"; }

# ---------- lectura de .env / .env.example
env_get() { # env_get ARCHIVO CLAVE
  [ -f "$1" ] || return 0
  local v; v="$(grep -E "^$2=" "$1" | tail -n1 | cut -d= -f2- || true)"
  v="${v%$'\r'}"; v="${v#\'}"; v="${v%\'}"
  printf '%s' "$v"
}
is_placeholder() { [[ -z "$1" || "$1" =~ pega_aqui|^sk-\.\.\.$|^gsk_\.\.\.$|cambia_esto|CAMBIA ]]; }
def() { local v; v="$(env_get "$ENV_PATH" "$1")"; [ -n "$v" ] && printf '%s' "$v" || env_get "$EXAMPLE" "$1"; }
sec() { local v; v="$(env_get "$ENV_PATH" "$1")"; is_placeholder "$v" && printf '' || printf '%s' "$v"; }

# ---------- preguntas
ask() { # ask "Pregunta" "default" [required]
  local v
  while true; do
    read -r -p "$1${2:+ [$2]}: " v || true
    v="${v:-$2}"; v="${v#"${v%%[![:space:]]*}"}"; v="${v%"${v##*[![:space:]]}"}"
    if [ -n "$v" ] || [ -z "${3:-}" ]; then printf '%s' "$v"; return; fi
    warn '  Este dato es obligatorio.' >&2
  done
}
ask_secret() { # ask_secret "Pregunta" "actual" [required]
  local v hint=""
  [ -n "$2" ] && hint=" [Enter = conservar la actual]"
  while true; do
    read -r -s -p "$1$hint: " v || true; echo >&2
    v="${v:-$2}"
    if [ -n "$v" ] || [ -z "${3:-}" ]; then printf '%s' "$v"; return; fi
    warn '  Este dato es obligatorio.' >&2
  done
}
ask_yn() { # ask_yn "Pregunta" s|n  -> exit 0 = sí
  local v hint="(s/N)"; [ "$2" = "s" ] && hint="(S/n)"
  while true; do
    read -r -p "$1 $hint " v || true
    v="$(printf '%s' "${v:-$2}" | tr '[:upper:]' '[:lower:]')"
    case "$v" in s|si|sí|y|yes) return 0 ;; n|no) return 1 ;; esac
  done
}
ask_password() { # ask_password usuario
  local p1 p2
  while true; do
    read -r -s -p "  Contraseña para '$1' (mín. 8 caracteres): " p1; echo >&2
    [ "${#p1}" -ge 8 ] || { warn '  Muy corta.' >&2; continue; }
    read -r -s -p "  Repite la contraseña: " p2; echo >&2
    [ "$p1" = "$p2" ] || { warn '  No coinciden.' >&2; continue; }
    printf '%s' "$p1"; return
  done
}

# ---------- hash (mismo formato que backend/app/auth.py); la contraseña viaja por stdin
HASH_PY='import hashlib,secrets,sys
pw=sys.stdin.buffer.read(); salt=secrets.token_hex(16)
dk=hashlib.pbkdf2_hmac("sha256",pw,bytes.fromhex(salt),600000)
print(f"pbkdf2-sha256.600000.{salt}.{dk.hex()}")'
hash_password() {
  local py
  for py in python3 python; do
    if "$py" -c 'import hashlib' >/dev/null 2>&1; then printf '%s' "$1" | "$py" -c "$HASH_PY"; return; fi
  done
  if command -v docker >/dev/null 2>&1; then
    printf '%s' "$1" | docker run --rm -i python:3.12-alpine python -c "$HASH_PY"; return
  fi
  warn 'Se necesita python3 o docker para generar el hash de la contraseña.' >&2; exit 1
}
new_secret() {
  if command -v openssl >/dev/null 2>&1; then openssl rand -base64 48 | tr -d '\r\n=' | tr '+/' '-_'
  else head -c 48 /dev/urandom | base64 | tr -d '\r\n=' | tr '+/' '-_'; fi
}

# ---------- prueba de Odoo (solo common.version / common.authenticate)
json_str() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'; }
odoo_call() { # odoo_call URL METODO ARGS_JSON
  curl -sS --max-time 25 -H 'Content-Type: application/json' -X POST "$1/jsonrpc" \
    -d "{\"jsonrpc\":\"2.0\",\"method\":\"call\",\"id\":1,\"params\":{\"service\":\"common\",\"method\":\"$2\",\"args\":$3}}"
}
test_odoo() { # imprime mensaje; exit 0 si ok
  local ver r uid serie
  if ! command -v curl >/dev/null 2>&1; then echo 'curl no está instalado; se omite la prueba.'; return 0; fi
  if ! ver="$(odoo_call "$1" version '[]' 2>&1)"; then echo "No se pudo conectar a $1/jsonrpc: $ver"; return 1; fi
  serie="$(printf '%s' "$ver" | grep -o '"server_serie": *"[^"]*"' | cut -d'"' -f4 || true)"
  r="$(odoo_call "$1" authenticate "[\"$(json_str "$2")\",\"$(json_str "$3")\",\"$(json_str "$4")\",{}]" 2>&1 || true)"
  if printf '%s' "$r" | grep -q '"error"'; then
    echo "Odoo respondió con error (¿nombre de base de datos correcto?): $(printf '%s' "$r" | grep -o '"message": *"[^"]*"' | tail -n1)"
    return 1
  fi
  uid="$(printf '%s' "$r" | grep -o '"result": *[0-9]\+' | grep -o '[0-9]\+$' || true)"
  if [ -n "$uid" ]; then echo "Conexión correcta: Odoo ${serie:-?}, uid $uid."; return 0; fi
  echo 'Usuario o API key incorrectos (authenticate devolvió false).'; return 1
}

# Valida clave y modelo con GET /models (no consume tokens)
test_ai_key() { # test_ai_key proveedor clave modelo
  local base='https://api.openai.com/v1' body code
  [ "$1" = groq ] && base='https://api.groq.com/openai/v1'
  command -v curl >/dev/null 2>&1 || { echo 'curl no está instalado; se omite la prueba.'; return 0; }
  body="$(curl -sS --max-time 20 -w '\n%{http_code}' -H "Authorization: Bearer $2" "$base/models" 2>&1)" || { echo "No se pudo validar la clave: $body"; return 1; }
  code="${body##*$'\n'}"; body="${body%$'\n'*}"
  [ "$code" = 401 ] && { echo 'Clave inválida (401).'; return 1; }
  [ "$code" = 200 ] || { echo "No se pudo validar la clave (HTTP $code)."; return 1; }
  if ! printf '%s' "$body" | grep -q "\"id\": *\"$3\""; then echo "Clave válida, pero el modelo '$3' no está disponible."; return 1; fi
  echo "Clave válida; modelo '$3' disponible."
}

NEEDS_QUOTE=$'[[:space:]#$\'"]'
fmt_val() { if [[ "$1" =~ $NEEDS_QUOTE ]]; then printf "'%s'" "${1//\'/}"; else printf '%s' "$1"; fi; }

# =====================================================================
printf '\n%sPROSIC BI - asistente de configuración%s\n' "$c_ok" "$c_off"
echo 'Enter acepta el valor entre corchetes. Las claves no se muestran al escribir.'
[ -f "$ENV_PATH" ] && dim 'Se encontró .env: sus valores serán los predeterminados.'

declare -A V

step '1/4  Odoo (usuario de SOLO LECTURA)'
d_url="$(def ODOO_URL)"; d_db="$(def ODOO_DB)"; d_user="$(def ODOO_USER)"; d_key="$(sec ODOO_API_KEY)"
while true; do
  url="$(ask 'URL de Odoo (sin /web)' "$d_url" req)"
  url="${url%%/web*}"; url="${url%/}"; [[ "$url" =~ ^https?:// ]] || url="https://$url"
  db="$(ask 'Base de datos' "$d_db" req)"
  user="$(ask 'Usuario (login/correo) de solo lectura' "$d_user" req)"
  key="$(ask_secret 'API key de ese usuario' "$d_key" req)"
  dim '  Probando conexión...'
  if msg="$(test_odoo "$url" "$db" "$user" "$key")"; then printf '  %s%s%s\n' "$c_ok" "$msg" "$c_off"; break; fi
  warn "  $msg"
  ask_yn '  ¿Volver a capturar los datos de Odoo?' s || { dim '  Se guardarán así; revisa después /api/odoo/health.'; break; }
  d_url="$url"; d_db="$db"; d_user="$user"; d_key="$key"
done
V[ODOO_URL]="$url"; V[ODOO_DB]="$db"; V[ODOO_USER]="$user"; V[ODOO_API_KEY]="$key"
V[ODOO_TZ]="$(ask 'Zona horaria' "$(def ODOO_TZ)")"

step '2/4  Inteligencia artificial (opcional: clave vacía = dashboard sin IA)'
provider=""
while [[ "$provider" != groq && "$provider" != openai ]]; do
  provider="$(ask 'Proveedor: groq (gratis, console.groq.com/keys) u openai (de pago)' "$(def AI_PROVIDER)" | tr '[:upper:]' '[:lower:]')"
done
V[AI_PROVIDER]="$provider"; P="$(printf '%s' "$provider" | tr '[:lower:]' '[:upper:]')"
d_akey="$(sec "${P}_API_KEY")"; d_model="$(def "${P}_MODEL")"
while true; do
  akey="$(ask_secret "${P}_API_KEY" "$d_akey")"
  [ -n "$akey" ] || break
  model="$(ask 'Modelo (debe soportar salida estructurada y tools)' "$d_model" req)"
  dim '  Probando la clave...'
  if msg="$(test_ai_key "$provider" "$akey" "$model")"; then printf '  %s%s%s\n' "$c_ok" "$msg" "$c_off"; break; fi
  warn "  $msg"
  ask_yn '  ¿Volver a capturar la clave?' s || break
  d_akey="$akey"; d_model="$model"
done
V[${P}_API_KEY]="$akey"
if [ -n "$akey" ]; then
  V[${P}_MODEL]="$model"
  anon_def=s; [ "$(def AI_ANONYMIZE)" = "false" ] && anon_def=n
  if ask_yn '¿Anonimizar clientes/proveedores/vendedores antes de enviarlos a la IA?' "$anon_def"; then V[AI_ANONYMIZE]=true; else V[AI_ANONYMIZE]=false; fi
else
  dim '  Sin clave: los endpoints de IA responderán 503 hasta que la agregues.'
fi

step '3/4  Acceso (Google @dominio + usuarios de emergencia)'
dim '  Client ID: Google Cloud Console > APIs y servicios > Credenciales > ID de cliente OAuth (Aplicación web).'
gid="$(ask 'GOOGLE_CLIENT_ID (Enter para omitir: solo usuario/contraseña)' "$(def GOOGLE_CLIENT_ID)")"
V[GOOGLE_CLIENT_ID]="$gid"
if [ -n "$gid" ]; then
  dom="$(ask 'Dominio permitido' "$(def ALLOWED_EMAIL_DOMAIN)" req)"; dom="${dom#@}"
  V[ALLOWED_EMAIL_DOMAIN]="$(printf '%s' "$dom" | tr '[:upper:]' '[:lower:]')"
  V[ADMIN_EMAILS]="$(ask "Correos administradores (separados por coma, @${V[ALLOWED_EMAIL_DOMAIN]})" "$(def ADMIN_EMAILS)" req | tr '[:upper:]' '[:lower:]')"
  pw_def=n; [ "$(def ALLOW_PASSWORD_LOGIN)" = "true" ] && pw_def=s
  if ask_yn '¿Mantener un acceso de emergencia con usuario/contraseña?' "$pw_def"; then V[ALLOW_PASSWORD_LOGIN]=true; else V[ALLOW_PASSWORD_LOGIN]=false; fi
  dim '  Usuario de emergencia (solo se usa si ALLOW_PASSWORD_LOGIN=true):'
fi
users=()
existing="$(sec AUTH_USERS)"
if [ -n "$existing" ]; then
  names="$(printf '%s' "$existing" | tr ',' '\n' | awk -F'|' 'NF>=3{printf "%s%s (%s)", (n++?", ":""), $1, $2}')"
  if ask_yn "Usuarios actuales: $names. ¿Conservarlos?" s; then IFS=',' read -r -a users <<< "$existing"; fi
fi
if [ "${#users[@]}" -eq 0 ]; then
  admin="$(ask 'Usuario administrador' admin req)"
  pw="$(ask_password "$admin")"; dim '  Generando hash...'
  users+=("$admin|admin|$(hash_password "$pw")")
fi
while ask_yn '¿Agregar otro usuario?' n; do
  name="$(ask '  Nombre de usuario' '' req)"
  if [[ "$name" =~ [\|,[:space:]] ]]; then warn '  Sin espacios, comas ni |.'; continue; fi
  role=""; while [[ "$role" != admin && "$role" != viewer ]]; do role="$(ask '  Rol (admin/viewer)' viewer | tr '[:upper:]' '[:lower:]')"; done
  pw="$(ask_password "$name")"; dim '  Generando hash...'
  kept=(); for u in "${users[@]}"; do [ "${u%%|*}" = "$name" ] || kept+=("$u"); done
  users=("${kept[@]}" "$name|$role|$(hash_password "$pw")")
done
V[AUTH_USERS]="$(IFS=','; printf '%s' "${users[*]}")"
jwt="$(sec JWT_SECRET)"
if [ -z "$jwt" ]; then jwt="$(new_secret)"; dim '  JWT_SECRET generado automáticamente.'; fi
V[JWT_SECRET]="$jwt"

step '4/4  Publicación'
port=""; while ! [[ "$port" =~ ^[0-9]{2,5}$ ]]; do port="$(ask 'Puerto local del frontend' "$(def FRONTEND_PORT)")"; done
V[FRONTEND_PORT]="$port"
local_url="http://localhost:$port"
public="$(ask 'URL pública del dashboard (p. ej. https://bi.prosic.com; Enter si solo es local)' "$(def CORS_ORIGINS | cut -d, -f1)")"
public="${public%/}"
if [ -n "$public" ] && [ "$public" != "$local_url" ]; then V[CORS_ORIGINS]="$public,$local_url"; else V[CORS_ORIGINS]="$local_url"; fi

# ---------- escribir .env a partir de .env.example
if [ -f "$ENV_PATH" ]; then
  bak="$ENV_PATH.bak-$(date +%Y%m%d-%H%M%S)"; cp "$ENV_PATH" "$bak"; dim "Respaldo del .env anterior: $bak"
fi
tmp="$(mktemp)"
declare -A done_keys
while IFS= read -r line || [ -n "$line" ]; do
  line="${line%$'\r'}"
  if [[ "$line" =~ ^[[:space:]]*([A-Z0-9_]+)= ]] && [ -n "${V[${BASH_REMATCH[1]}]+x}" ]; then
    k="${BASH_REMATCH[1]}"; printf '%s=%s\n' "$k" "$(fmt_val "${V[$k]}")" >> "$tmp"; done_keys[$k]=1
  else
    printf '%s\n' "$line" >> "$tmp"
  fi
done < "$EXAMPLE"
for k in "${!V[@]}"; do [ -n "${done_keys[$k]+x}" ] || printf '%s=%s\n' "$k" "$(fmt_val "${V[$k]}")" >> "$tmp"; done
mv "$tmp" "$ENV_PATH"; chmod 600 "$ENV_PATH"
printf '\n%s.env creado en %s%s\n' "$c_ok" "$ENV_PATH" "$c_off"

# ---------- docker
if ! command -v docker >/dev/null 2>&1; then warn 'Docker no está instalado. Cuando lo tengas: docker compose up -d --build'; exit 0; fi
if ! ask_yn '¿Construir y levantar ahora (docker compose up -d --build)?' s; then echo 'Cuando quieras: docker compose up -d --build'; exit 0; fi
(cd "$ROOT" && docker compose up -d --build)
dim 'Esperando a que el backend responda...'
for _ in $(seq 1 40); do
  if curl -fsS --max-time 5 "$local_url/api/ping" >/dev/null 2>&1; then
    printf '\n%sListo. Abre %s e inicia sesión con tu usuario admin.%s\n' "$c_ok" "$local_url" "$c_off"; exit 0
  fi
  sleep 3
done
warn 'El backend aún no responde. Revisa: docker compose logs -f backend'
