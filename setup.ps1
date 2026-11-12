<#
.SYNOPSIS
  Asistente de configuración de PROSIC BI.
  Pregunta los datos paso a paso, valida la conexión con Odoo, genera el secreto JWT y los hashes de
  contraseña, escribe .env (a partir de .env.example) y opcionalmente levanta docker compose.

.EXAMPLE
  .\setup.cmd
  powershell -ExecutionPolicy Bypass -File .\setup.ps1

  Si ya existe .env, sus valores se usan como predeterminados (Enter = conservar) y se respalda como .env.bak-<fecha>.
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$Root = $PSScriptRoot
$EnvPath = Join-Path $Root '.env'
$ExamplePath = Join-Path $Root '.env.example'

# ----------------------------------------------------------------- utilidades
function Read-EnvFile([string]$Path) {
  $map = @{}
  if (Test-Path -LiteralPath $Path) {
    foreach ($line in [IO.File]::ReadAllLines($Path)) {
      if ($line -match '^\s*([A-Z0-9_]+)=(.*)$') {
        $v = $Matches[2].Trim()
        if ($v.Length -ge 2 -and $v.StartsWith("'") -and $v.EndsWith("'")) { $v = $v.Substring(1, $v.Length - 2) }
        $map[$Matches[1]] = $v
      }
    }
  }
  return $map
}

function Test-Placeholder([string]$Value) {
  return (-not $Value) -or ($Value -match 'pega_aqui|^sk-\.\.\.$|^gsk_\.\.\.$|cambia_esto|CAMBIA')
}

function Ask([string]$Prompt, [string]$Default = '', [switch]$Required) {
  while ($true) {
    $suffix = ''
    if ($Default) { $suffix = " [$Default]" }
    $v = Read-Host "$Prompt$suffix"
    if ([string]::IsNullOrWhiteSpace($v)) { $v = $Default }
    $v = "$v".Trim()
    if ($v -or -not $Required) { return $v }
    Write-Host '  Este dato es obligatorio.' -ForegroundColor Yellow
  }
}

function ConvertFrom-SecureText([Security.SecureString]$Secure) {
  $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Secure)
  try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr) }
  finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr) }
}

function Ask-Secret([string]$Prompt, [string]$Current = '', [switch]$Required) {
  $hint = ''
  if ($Current) { $hint = ' [Enter = conservar la actual]' }
  while ($true) {
    $v = (ConvertFrom-SecureText (Read-Host "$Prompt$hint" -AsSecureString)).Trim()
    if (-not $v) { $v = $Current }
    if ($v -or -not $Required) { return $v }
    Write-Host '  Este dato es obligatorio.' -ForegroundColor Yellow
  }
}

function Ask-YesNo([string]$Prompt, [bool]$Default = $true) {
  $hint = '(s/N)'
  if ($Default) { $hint = '(S/n)' }
  while ($true) {
    $v = (Read-Host "$Prompt $hint").Trim().ToLower()
    if (-not $v) { return $Default }
    if ($v -in @('s', 'si', 'sí', 'y', 'yes')) { return $true }
    if ($v -in @('n', 'no')) { return $false }
  }
}

function ConvertTo-Hex([byte[]]$Bytes) {
  return (($Bytes | ForEach-Object { $_.ToString('x2') }) -join '')
}

function New-RandomBytes([int]$Count) {
  $b = New-Object byte[] $Count
  $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
  try { $rng.GetBytes($b) } finally { $rng.Dispose() }
  return , $b
}

# Mismo formato que backend/app/auth.py: pbkdf2-sha256.<iter>.<salt hex>.<hash hex>
function New-PasswordHash([string]$Password, [int]$Iterations = 600000, [byte[]]$Salt = $null) {
  if (-not $Salt) { $Salt = New-RandomBytes 16 }
  $pw = [Text.Encoding]::UTF8.GetBytes($Password)
  $kdf = [Security.Cryptography.Rfc2898DeriveBytes]::new($pw, $Salt, $Iterations, [Security.Cryptography.HashAlgorithmName]::SHA256)
  try { $dk = $kdf.GetBytes(32) } finally { $kdf.Dispose() }
  return "pbkdf2-sha256.$Iterations.$(ConvertTo-Hex $Salt).$(ConvertTo-Hex $dk)"
}

function New-Secret {
  $b64 = [Convert]::ToBase64String((New-RandomBytes 48))
  return $b64.TrimEnd('=').Replace('+', '-').Replace('/', '_')
}

function Ask-Password([string]$User) {
  while ($true) {
    $p1 = ConvertFrom-SecureText (Read-Host "  Contraseña para '$User' (mín. 8 caracteres)" -AsSecureString)
    if ($p1.Length -lt 8) { Write-Host '  Muy corta.' -ForegroundColor Yellow; continue }
    $p2 = ConvertFrom-SecureText (Read-Host '  Repite la contraseña' -AsSecureString)
    if ($p1 -ne $p2) { Write-Host '  No coinciden.' -ForegroundColor Yellow; continue }
    return $p1
  }
}

# Solo llama common.version / common.authenticate (lectura). Devuelve @{ ok; msg }.
function Test-OdooLogin([string]$Url, [string]$Db, [string]$User, [string]$Key) {
  try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
  } catch { }
  $endpoint = "$($Url.TrimEnd('/'))/jsonrpc"
  $call = {
    param($method, $argList)
    $body = @{ jsonrpc = '2.0'; method = 'call'; id = 1; params = @{ service = 'common'; method = $method; args = $argList } } |
      ConvertTo-Json -Depth 6 -Compress
    Invoke-RestMethod -Uri $endpoint -Method Post -ContentType 'application/json; charset=utf-8' `
      -Body ([Text.Encoding]::UTF8.GetBytes($body)) -TimeoutSec 25
  }
  try {
    $ver = & $call 'version' @()
    $serie = $ver.result.server_serie
    $r = & $call 'authenticate' @($Db, $User, $Key, @{})
  } catch {
    return @{ ok = $false; msg = "No se pudo conectar a $endpoint : $($_.Exception.Message)" }
  }
  if ($r.error) {
    $m = $r.error.data.message
    if (-not $m) { $m = $r.error.message }
    return @{ ok = $false; msg = "Odoo respondió con error: $m (¿nombre de base de datos correcto?)" }
  }
  if ($r.result -and ($r.result -isnot [bool])) {
    return @{ ok = $true; msg = "Conexión correcta: Odoo $serie, uid $($r.result)." }
  }
  return @{ ok = $false; msg = 'Usuario o API key incorrectos (authenticate devolvió false).' }
}

# Lista los modelos del proveedor (GET /models, no consume tokens) para validar clave y modelo.
function Test-AiKey([string]$Provider, [string]$Key, [string]$Model) {
  $base = 'https://api.openai.com/v1'
  if ($Provider -eq 'groq') { $base = 'https://api.groq.com/openai/v1' }
  try {
    $r = Invoke-RestMethod -Uri "$base/models" -Headers @{ Authorization = "Bearer $Key" } -TimeoutSec 20
  } catch {
    $code = $null
    if ($_.Exception.Response) { $code = [int]$_.Exception.Response.StatusCode }
    if ($code -eq 401) { return @{ ok = $false; msg = 'Clave inválida (401).' } }
    return @{ ok = $false; msg = "No se pudo validar la clave: $($_.Exception.Message)" }
  }
  $ids = @($r.data | ForEach-Object { $_.id })
  if ($Model -and ($ids -notcontains $Model)) {
    return @{ ok = $false; msg = "Clave válida, pero el modelo '$Model' no está disponible. Algunos: $(($ids | Select-Object -First 8) -join ', ')" }
  }
  return @{ ok = $true; msg = "Clave válida; modelo '$Model' disponible." }
}

function Format-EnvValue([string]$Value) {
  if ($Value -match "[\s#`$'""]") { return "'" + $Value.Replace("'", '') + "'" }
  return $Value
}

# Usa .env.example como plantilla (conserva comentarios) y reemplaza los valores indicados.
function Write-EnvFile([string]$Template, [string]$Path, [hashtable]$Values) {
  $done = @{}
  $out = New-Object System.Collections.Generic.List[string]
  foreach ($line in [IO.File]::ReadAllLines($Template)) {
    if ($line -match '^\s*([A-Z0-9_]+)=' -and $Values.ContainsKey($Matches[1])) {
      $k = $Matches[1]
      $out.Add("$k=$(Format-EnvValue $Values[$k])")
      $done[$k] = $true
    } else {
      $out.Add($line)
    }
  }
  foreach ($k in $Values.Keys) {
    if (-not $done.ContainsKey($k)) { $out.Add("$k=$(Format-EnvValue $Values[$k])") }
  }
  # UTF-8 sin BOM y fin de línea LF (docker compose y python-dotenv lo leen sin problemas).
  [IO.File]::WriteAllText($Path, (($out -join "`n") + "`n"), (New-Object Text.UTF8Encoding($false)))
}

function Write-Step([string]$Text) {
  Write-Host ''
  Write-Host "== $Text ==" -ForegroundColor Cyan
}

# ----------------------------------------------------------------- asistente
function Invoke-Setup {
  if (-not (Test-Path -LiteralPath $ExamplePath)) { throw "No se encontró $ExamplePath" }

  Write-Host ''
  Write-Host 'PROSIC BI - asistente de configuración' -ForegroundColor Green
  Write-Host 'Enter acepta el valor entre corchetes. Las claves no se muestran al escribir.'

  $example = Read-EnvFile $ExamplePath
  $current = Read-EnvFile $EnvPath
  $hasEnv = Test-Path -LiteralPath $EnvPath
  if ($hasEnv) { Write-Host "Se encontró .env: sus valores serán los predeterminados." -ForegroundColor DarkGray }

  # Valor actual (si .env existe) o el del ejemplo
  $def = { param($k) if ($current.ContainsKey($k) -and $current[$k]) { $current[$k] } else { $example[$k] } }
  # Secretos: solo el valor actual real (nunca el placeholder del ejemplo)
  $sec = { param($k) if ($current.ContainsKey($k) -and -not (Test-Placeholder $current[$k])) { $current[$k] } else { '' } }

  $v = @{}

  # ---------- Odoo
  Write-Step '1/4  Odoo (usuario de SOLO LECTURA)'
  while ($true) {
    $url = Ask 'URL de Odoo (sin /web)' (& $def 'ODOO_URL') -Required
    $url = ($url -replace '/web.*$', '').TrimEnd('/')
    if ($url -notmatch '^https?://') { $url = "https://$url" }
    $db = Ask 'Base de datos' (& $def 'ODOO_DB') -Required
    $user = Ask 'Usuario (login/correo) de solo lectura' (& $def 'ODOO_USER') -Required
    $key = Ask-Secret 'API key de ese usuario' (& $sec 'ODOO_API_KEY') -Required

    Write-Host '  Probando conexión...' -ForegroundColor DarkGray
    $t = Test-OdooLogin $url $db $user $key
    if ($t.ok) { Write-Host "  $($t.msg)" -ForegroundColor Green; break }
    Write-Host "  $($t.msg)" -ForegroundColor Yellow
    if (-not (Ask-YesNo '  ¿Volver a capturar los datos de Odoo?' $true)) {
      Write-Host '  Se guardarán así; puedes revisar después con /api/odoo/health.' -ForegroundColor DarkGray
      break
    }
    $current['ODOO_URL'] = $url; $current['ODOO_DB'] = $db; $current['ODOO_USER'] = $user; $current['ODOO_API_KEY'] = $key
  }
  $v['ODOO_URL'] = $url; $v['ODOO_DB'] = $db; $v['ODOO_USER'] = $user; $v['ODOO_API_KEY'] = $key
  $v['ODOO_TZ'] = Ask 'Zona horaria' (& $def 'ODOO_TZ')

  # ---------- IA
  Write-Step '2/4  Inteligencia artificial (opcional: clave vacía = dashboard sin IA)'
  $provider = ''
  while ($provider -notin @('groq', 'openai')) {
    $provider = (Ask 'Proveedor: groq (gratis, console.groq.com/keys) u openai (de pago)' (& $def 'AI_PROVIDER')).ToLower()
  }
  $v['AI_PROVIDER'] = $provider
  $P = $provider.ToUpper()   # GROQ / OPENAI
  while ($true) {
    $akey = Ask-Secret "${P}_API_KEY" (& $sec "${P}_API_KEY")
    if (-not $akey) { break }
    $model = Ask 'Modelo (debe soportar salida estructurada y tools)' (& $def "${P}_MODEL") -Required
    Write-Host '  Probando la clave...' -ForegroundColor DarkGray
    $t = Test-AiKey $provider $akey $model
    if ($t.ok) { Write-Host "  $($t.msg)" -ForegroundColor Green; break }
    Write-Host "  $($t.msg)" -ForegroundColor Yellow
    if (-not (Ask-YesNo '  ¿Volver a capturar la clave?' $true)) { break }
  }
  $v["${P}_API_KEY"] = $akey
  if ($akey) {
    $v["${P}_MODEL"] = $model
    $anonDefault = (& $def 'AI_ANONYMIZE') -ne 'false'
    $v['AI_ANONYMIZE'] = if (Ask-YesNo '¿Anonimizar clientes/proveedores/vendedores antes de enviarlos a la IA?' $anonDefault) { 'true' } else { 'false' }
  } else {
    Write-Host '  Sin clave: los endpoints de IA responderán 503 hasta que la agregues.' -ForegroundColor DarkGray
  }

  # ---------- Acceso
  Write-Step '3/4  Acceso (Google @dominio + usuarios de emergencia)'
  Write-Host '  Client ID: Google Cloud Console > APIs y servicios > Credenciales > ID de cliente OAuth (Aplicación web).' -ForegroundColor DarkGray
  $gid = Ask 'GOOGLE_CLIENT_ID (Enter para omitir: solo usuario/contraseña)' (& $def 'GOOGLE_CLIENT_ID')
  $v['GOOGLE_CLIENT_ID'] = $gid
  if ($gid) {
    $v['ALLOWED_EMAIL_DOMAIN'] = (Ask 'Dominio permitido' (& $def 'ALLOWED_EMAIL_DOMAIN') -Required).TrimStart('@').ToLower()
    $v['ADMIN_EMAILS'] = (Ask "Correos administradores (separados por coma, @$($v['ALLOWED_EMAIL_DOMAIN']))" (& $def 'ADMIN_EMAILS') -Required).ToLower()
    $pwDefault = (& $def 'ALLOW_PASSWORD_LOGIN') -eq 'true'
    $v['ALLOW_PASSWORD_LOGIN'] = if (Ask-YesNo '¿Mantener un acceso de emergencia con usuario/contraseña?' $pwDefault) { 'true' } else { 'false' }
  }
  $users = New-Object System.Collections.Generic.List[string]
  $existing = & $sec 'AUTH_USERS'
  if ($existing) {
    $names = ($existing.Split(',') | ForEach-Object { $p = $_.Split('|'); if ($p.Count -ge 3) { "$($p[0].Trim()) ($($p[1].Trim()))" } }) -join ', '
    if (Ask-YesNo "Usuarios actuales: $names. ¿Conservarlos?" $true) {
      $existing.Split(',') | ForEach-Object { if ($_.Trim()) { $users.Add($_.Trim()) } }
    }
  }
  if ($users.Count -eq 0) {
    if ($gid) { Write-Host '  Usuario de emergencia (solo se usa si ALLOW_PASSWORD_LOGIN=true):' -ForegroundColor DarkGray }
    $admin = Ask 'Usuario administrador' 'admin' -Required
    $pw = Ask-Password $admin
    Write-Host '  Generando hash...' -ForegroundColor DarkGray
    $users.Add("$admin|admin|$(New-PasswordHash $pw)")
  }
  while (Ask-YesNo '¿Agregar otro usuario?' $false) {
    $name = Ask '  Nombre de usuario' '' -Required
    if ($name -match '[|,\s]') { Write-Host '  Sin espacios, comas ni |.' -ForegroundColor Yellow; continue }
    $role = ''
    while ($role -notin @('admin', 'viewer')) { $role = (Ask '  Rol (admin/viewer)' 'viewer').ToLower() }
    $pw = Ask-Password $name
    Write-Host '  Generando hash...' -ForegroundColor DarkGray
    [void]$users.RemoveAll([Predicate[string]] { param($u) $u.Split('|')[0] -eq $name })
    $users.Add("$name|$role|$(New-PasswordHash $pw)")
  }
  $v['AUTH_USERS'] = $users -join ','
  $jwt = & $sec 'JWT_SECRET'
  if (-not $jwt) { $jwt = New-Secret; Write-Host '  JWT_SECRET generado automáticamente.' -ForegroundColor DarkGray }
  $v['JWT_SECRET'] = $jwt

  # ---------- Web
  Write-Step '4/4  Publicación'
  $port = ''
  while ($port -notmatch '^\d{2,5}$') { $port = Ask 'Puerto local del frontend' (& $def 'FRONTEND_PORT') }
  $v['FRONTEND_PORT'] = $port
  $local = "http://localhost:$port"
  $pubDefault = (& $def 'CORS_ORIGINS').Split(',')[0]
  $public = (Ask 'URL pública del dashboard (p. ej. https://bi.prosic.com; Enter si solo es local)' $pubDefault).TrimEnd('/')
  $origins = @($local)
  if ($public -and $public -ne $local) { $origins = @($public, $local) }
  $v['CORS_ORIGINS'] = $origins -join ','

  # ---------- Escribir
  if ($hasEnv) {
    $bak = "$EnvPath.bak-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
    Copy-Item -LiteralPath $EnvPath -Destination $bak
    Write-Host ''
    Write-Host "Respaldo del .env anterior: $bak" -ForegroundColor DarkGray
  }
  Write-EnvFile $ExamplePath $EnvPath $v
  Write-Host ''
  Write-Host ".env creado en $EnvPath" -ForegroundColor Green

  # ---------- Docker
  if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Write-Host 'Docker no está instalado o no está en el PATH. Cuando lo tengas: docker compose up -d --build' -ForegroundColor Yellow
    return
  }
  if (-not (Ask-YesNo '¿Construir y levantar ahora (docker compose up -d --build)?' $true)) {
    Write-Host 'Cuando quieras: docker compose up -d --build'
    return
  }
  Push-Location $Root
  try { docker compose up -d --build } finally { Pop-Location }
  if ($LASTEXITCODE -ne 0) { Write-Host 'docker compose falló; revisa el mensaje de arriba.' -ForegroundColor Red; return }

  Write-Host 'Esperando a que el backend responda...' -ForegroundColor DarkGray
  $deadline = (Get-Date).AddSeconds(120)
  $up = $false
  while ((Get-Date) -lt $deadline) {
    try {
      $r = Invoke-WebRequest -Uri "$local/api/ping" -UseBasicParsing -TimeoutSec 5
      if ($r.StatusCode -eq 200) { $up = $true; break }
    } catch { }
    Start-Sleep -Seconds 3
  }
  if ($up) {
    Write-Host ''
    Write-Host "Listo. Abre $local e inicia sesión con tu usuario admin." -ForegroundColor Green
    Write-Host 'Diagnóstico de Odoo (como admin): menú del navegador -> /api/odoo/health, o ver README sección 4.'
  } else {
    Write-Host 'El backend aún no responde. Revisa: docker compose logs -f backend' -ForegroundColor Yellow
  }
}

# Permite cargar las funciones sin ejecutar el asistente: . .\setup.ps1
if ($MyInvocation.InvocationName -ne '.') { Invoke-Setup }
