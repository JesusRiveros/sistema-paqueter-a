# Levanta Balula Express (Waitress) y el tunel de Cloudflare juntos, y verifica que respondan.
# Uso (desde la carpeta del proyecto, en PowerShell):
#     powershell -ExecutionPolicy Bypass -File .\iniciar_balula.ps1
# Para detener todo:
#     powershell -ExecutionPolicy Bypass -File .\detener_balula.ps1

$ErrorActionPreference = 'Stop'
$raiz = Split-Path -Parent $MyInvocation.MyCommand.Path
$carpetaApp = Join-Path $raiz 'sistema paqueteria'
$python = Join-Path $raiz '.venv\Scripts\python.exe'
$tunel = 'balula'

if (-not (Test-Path $python)) { throw "No encuentro el entorno virtual en $python (crea .venv e instala requirements.txt)." }
if (-not (Get-Command cloudflared -ErrorAction SilentlyContinue)) {
    throw 'cloudflared no esta instalado. Instalalo con:  winget install --id Cloudflare.cloudflared'
}
if (-not (Test-Path (Join-Path $raiz '.env'))) { throw 'Falta el archivo .env en la carpeta del proyecto (copia .env.example).' }

$enUso = Get-NetTCPConnection -LocalPort 5001 -State Listen -ErrorAction SilentlyContinue
if ($enUso) { throw 'El puerto 5001 ya esta en uso. Ejecuta detener_balula.ps1 o cierra la otra instancia.' }

Write-Host '[1/3] Iniciando la app en http://127.0.0.1:5001 ...' -ForegroundColor Cyan
$procApp = Start-Process -FilePath $python -ArgumentList 'servir.py' -WorkingDirectory $carpetaApp -PassThru
$listo = $false
for ($i = 0; $i -lt 40; $i++) {
    try { Invoke-RestMethod -Uri 'http://127.0.0.1:5001/salud' -TimeoutSec 2 | Out-Null; $listo = $true; break }
    catch { Start-Sleep -Seconds 1 }
}
if (-not $listo) { Stop-Process -Id $procApp.Id -ErrorAction SilentlyContinue; throw 'La app no respondio en 40 s. Revisa la ventana de la app para ver el error.' }

Write-Host "[2/3] Iniciando el tunel de Cloudflare '$tunel' ..." -ForegroundColor Cyan
$procTunel = Start-Process -FilePath 'cloudflared' -ArgumentList 'tunnel', 'run', $tunel -PassThru
Set-Content -Path (Join-Path $raiz '.balula_pids') -Value "$($procApp.Id),$($procTunel.Id)"
Start-Sleep -Seconds 6

Write-Host '[3/3] Verificando ...' -ForegroundColor Cyan
& (Join-Path $raiz 'verificar_balula.ps1')
Write-Host ''
Write-Host 'Balula Express esta en linea. Deja abiertas las dos ventanas (app y tunel).' -ForegroundColor Green
