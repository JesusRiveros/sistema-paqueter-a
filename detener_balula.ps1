# Detiene la app y el tunel que levanto iniciar_balula.ps1
$raiz = Split-Path -Parent $MyInvocation.MyCommand.Path
$archivo = Join-Path $raiz '.balula_pids'
if (Test-Path $archivo) {
    foreach ($id in (Get-Content $archivo).Split(',')) {
        try { Stop-Process -Id ([int]$id) -Force -ErrorAction Stop; Write-Host "Detenido proceso $id" } catch { }
    }
    Remove-Item $archivo
}
$resto = (Get-NetTCPConnection -LocalPort 5001 -State Listen -ErrorAction SilentlyContinue).OwningProcess | Select-Object -Unique
foreach ($id in $resto) { Stop-Process -Id $id -Force; Write-Host "Detenido proceso $id (puerto 5001)" }
Write-Host 'Listo.'
