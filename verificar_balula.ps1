# Verifica que Balula Express responda localmente y en internet, y que cada servicio este configurado.
# Uso:  powershell -ExecutionPolicy Bypass -File .\verificar_balula.ps1

$dominio = 'https://balulaexpress.com'
$fallas = 0

function Revisar($nombre, $bloque) {
    try {
        $mensaje = & $bloque
        Write-Host ("  [OK]    {0}  {1}" -f $nombre, $mensaje) -ForegroundColor Green
    } catch {
        Write-Host ("  [FALLA] {0}  {1}" -f $nombre, $_.Exception.Message) -ForegroundColor Red
        $script:fallas++
    }
}

Write-Host 'Servicios:'
Revisar 'App local (127.0.0.1:5001)' { $r = Invoke-RestMethod 'http://127.0.0.1:5001/salud' -TimeoutSec 5; "base de datos: $($r.base_de_datos)" }
Revisar "Sitio publico ($dominio)" {
    $r = Invoke-RestMethod "$dominio/salud" -TimeoutSec 10
    if (-not $r.https) { throw 'la app no detecta HTTPS: revisa TRUST_PROXY=1 en .env' }
    "pagos: $($r.pagos) | asistente: $($r.asistente) | twilio: $($r.twilio) | correo: $($r.correo_smtp)"
}
Revisar 'www redirige al dominio principal' {
    $r = Invoke-WebRequest 'https://www.balulaexpress.com/' -UseBasicParsing -TimeoutSec 10
    $final = $r.BaseResponse.ResponseUri.AbsoluteUri
    if ($final -notlike "$dominio/*") { throw "termino en $final" }
    $final
}
Revisar 'Pagina de rastreo' { $r = Invoke-WebRequest "$dominio/rastreo" -UseBasicParsing -TimeoutSec 10; "HTTP $($r.StatusCode)" }
Revisar 'Webhook de Stripe responde (debe rechazar sin firma)' {
    try { Invoke-WebRequest "$dominio/webhooks/stripe" -Method Post -Body '{}' -ContentType 'application/json' -UseBasicParsing -TimeoutSec 10 | Out-Null; throw 'acepto una peticion sin firma' }
    catch { if ($_.Exception.Response -and [int]$_.Exception.Response.StatusCode -eq 400) { 'HTTP 400 (correcto)' } else { throw $_ } }
}
Revisar 'Twilio responde (debe rechazar sin firma)' {
    try { Invoke-WebRequest "$dominio/voz/twilio/entrante" -Method Post -Body 'CallSid=CAprueba' -UseBasicParsing -TimeoutSec 10 | Out-Null; throw 'acepto una peticion sin firma' }
    catch { if ($_.Exception.Response -and [int]$_.Exception.Response.StatusCode -eq 403) { 'HTTP 403 (correcto)' } else { throw $_ } }
}

Write-Host ''
if ($fallas -eq 0) { Write-Host 'Todo responde correctamente.' -ForegroundColor Green }
else { Write-Host "$fallas verificacion(es) fallaron. Revisa los mensajes de arriba." -ForegroundColor Red }
