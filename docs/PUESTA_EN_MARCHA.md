# Balula Express — Puesta en marcha y presentación

Guía para dejar el sistema funcionando de punta a punta en **https://balulaexpress.com**, servido
desde tu computadora con Cloudflare Tunnel.

---

## 1. Lista única de lo que tienes que hacer tú

Cada llave va en el archivo `.env` de la carpeta raíz del proyecto (`sistema-paqueter-a\.env`).
**Nunca** subas ese archivo a git (ya está en `.gitignore`). Después de cambiar `.env`, reinicia la app.

| # | Servicio | Qué hacer | Variable en `.env` |
|---|---|---|---|
| 1 | **App** | Genera una clave: `.venv\Scripts\python.exe -c "import secrets; print(secrets.token_hex(32))"` | `SECRET_KEY` |
| 2 | **App** | Pon la URL pública y los ajustes de producción | `PUBLIC_BASE_URL=https://balulaexpress.com` · `TRUST_PROXY=1` · `SESSION_COOKIE_SECURE=1` · `FLASK_DEBUG=0` · `PAGOS_SIMULADOS=0` |
| 3 | **Google Maps** | Activa la facturación del proyecto, habilita Maps JavaScript, Places, Directions, Distance Matrix y Geocoding, y crea una **key nueva** restringida a `balulaexpress.com/*` (la anterior está expuesta en el historial de git) | `GOOGLE_MAPS_API_KEY` |
| 4 | **Anthropic (Claude)** | En https://console.anthropic.com → *API Keys* → *Create Key*. Carga saldo en *Billing* | `ANTHROPIC_API_KEY` |
| 5 | **Stripe** | Crea la cuenta y copia las llaves **de prueba** (sección 3) | `STRIPE_SECRET_KEY` (sk_test_…) · `STRIPE_PUBLISHABLE_KEY` (pk_test_…) |
| 6 | **Stripe** | Crea el webhook (sección 3.3) y copia su secreto de firma | `STRIPE_WEBHOOK_SECRET` (whsec_…) |
| 7 | **Correo (SMTP)** | **Obligatorio para los códigos de seguridad de reembolsos.** Opción fácil: Gmail con *contraseña de aplicación* (requiere verificación en 2 pasos): https://myaccount.google.com/apppasswords | `SMTP_HOST=smtp.gmail.com` · `SMTP_PUERTO=587` · `SMTP_USUARIO=tucorreo@gmail.com` · `SMTP_PASSWORD=<contraseña de aplicación>` · `SMTP_REMITENTE=Balula Express <tucorreo@gmail.com>` |
| 8 | **Twilio** (opcional, llamadas) | Crea la cuenta, compra un número y copia el *Auth Token* (sección 5) | `TWILIO_AUTH_TOKEN` · `TWILIO_NUMERO_HUMANO=+52…` |
| 9 | **Equipo humano** | Teléfono y horario reales para escalamientos | `ASISTENTE_TELEFONO_HUMANO` · `ASISTENTE_HORARIO_HUMANO` |
| 10 | **Cloudflare** | Crea la cuenta, mueve los nameservers y crea el túnel (sección 4) | — |
| 11 | **Contenido** | Revisa correo, teléfono y dirección en `CONTACTO` dentro de `sistema paqueteria/app.py` (hoy dice `contacto@baluladata.com`) | — |

> `STRIPE_PUBLISHABLE_KEY` se guarda para cuando se use Stripe.js en el navegador; con Stripe Checkout
> (lo que usa la app) todo ocurre en el servidor y no es indispensable.

---

## 2. Base de datos

- **Motor:** SQLite (un solo archivo). Se crea y actualiza sola con migraciones al iniciar (`python servir.py` o `python app.py`).
- **Ubicación:**
  - Base: `sistema paqueteria\instance\balula.sqlite3`
  - Fotos de entregas y de daños: `sistema paqueteria\instance\archivos\`
  - Modelo de riesgo de retraso: `sistema paqueteria\instance\modelo_retraso.json`
- **Respaldo (con la app encendida, es seguro):**
  ```powershell
  cd "sistema paqueteria"
  ..\.venv\Scripts\flask.exe --app app respaldar-base
  ```
  Crea `instance\respaldos\balula-AAAAMMDD-HHMMSS.sqlite3`.
- **Restaurar:** detén la app (`.\detener_balula.ps1`) y copia el respaldo encima de `instance\balula.sqlite3`.
- **PostgreSQL** (opcional, si algún día lo hospedas en la nube): `DATABASE_URL=postgresql+psycopg://usuario:clave@host:5432/balula`.

### Preparar la demo
1. Con la app detenida, respalda tu base actual (o renómbrala).
2. Carga datos de ejemplo con una contraseña conocida:
   ```powershell
   cd "sistema paqueteria"
   ..\.venv\Scripts\flask.exe --app app sembrar-demo --password DemoBalula2026
   ```
   Crea `admin@balula.mx`, `pedro@`, `lucia@` y `raul@balula.mx`, y 22 envíos en CDMX en distintos estados.
3. Haz 1 compra de prueba con Stripe (tarjeta 4242…) **usando tu correo real** como remitente, para mostrar el reembolso con código.
4. Respalda esa base "lista para demo". Si algo se desordena durante la presentación, restáurala.

---

## 3. Stripe (modo de prueba)

### 3.1 Crear la cuenta
1. Entra a https://dashboard.stripe.com/register y regístrate con tu correo. Confirma el correo.
2. **No necesitas activar la cuenta** (datos fiscales o bancarios) para el modo de prueba.
3. En el Dashboard, verifica que esté encendido el **modo de prueba / entorno de prueba** (interruptor "Test mode" o "Sandbox" arriba). Todo debe verse con la etiqueta de prueba.

### 3.2 Copiar las llaves
1. *Desarrolladores* (Developers) → *Claves de API* (API keys).
2. **Clave publicable** `pk_test_…` → `STRIPE_PUBLISHABLE_KEY`.
3. **Clave secreta** → *Mostrar* → `sk_test_…` → `STRIPE_SECRET_KEY`.
4. Si por error pegas una `sk_live_` o `pk_live_`, **la app no arranca** y te dice por qué.

### 3.3 Webhook
1. *Desarrolladores* → *Webhooks* → *Agregar destino / endpoint*.
2. URL: **`https://balulaexpress.com/webhooks/stripe`**
3. Eventos:
   - `checkout.session.completed`
   - `checkout.session.async_payment_succeeded`
   - `checkout.session.expired`
   - `refund.created`
   - `refund.updated`
   - `refund.failed`
4. Guarda. En la página del endpoint, *Secreto de firma* → *Mostrar* → `whsec_…` → `STRIPE_WEBHOOK_SECRET`. Reinicia la app.

### 3.4 Comprobar que llegan los avisos
- Haz una compra en el cotizador con **4242 4242 4242 4242**, cualquier fecha futura, cualquier CVC y cualquier C.P.
- En Stripe → *Webhooks* → tu endpoint → *Entregas de eventos*: deben aparecer con **200**.
- Si ves **400**, el `STRIPE_WEBHOOK_SECRET` no coincide con el de ese endpoint.
- Reembolsos: aprueba uno (por Lula o en Admin → Reembolsos) y revísalo en Stripe → *Pagos* → el pago → *Reembolsos*. En el admin, "Consultar en Stripe" trae el estado real.
- **Prueba local sin túnel:** instala la Stripe CLI y usa `stripe listen --forward-to localhost:5001/webhooks/stripe`. Usa el `whsec_` que imprime ese comando mientras pruebes en local.

---

## 4. Cloudflare: dominio y túnel

### 4.1 Antes de mover los nameservers (no rompas nada)
En Namecheap → *Domain List* → *Manage* (balulaexpress.com):

1. **Advanced DNS:** toma captura de **todos** los registros:
   - **A / AAAA / CNAME** de `@` y `www`: ¿hay un sitio hoy? Por ejemplo GitHub Pages (`185.199.108-111.153` o un CNAME a `usuario.github.io`) o la página de estacionamiento de Namecheap. El túnel los va a reemplazar.
   - **MX** y la sección *Mail Settings*:
     - Si usas **Email Forwarding de Namecheap** o **Private Email**, ese correo depende de sus servidores. Tendrás que copiar sus MX y TXT a Cloudflare, o cambiar a *Cloudflare Email Routing*, que es gratis.
     - Si no hay correo con ese dominio, no hay nada que cuidar.
   - **TXT:** SPF (`v=spf1…`), DKIM, DMARC (`_dmarc`) y verificaciones (Google Search Console, etc.).
   - **URL Redirect Records:** son una función de Namecheap y **dejan de funcionar** al cambiar nameservers. Se recrean como *Redirect Rules* en Cloudflare.
2. Pestaña *Domain* → **DNSSEC: desactívalo** antes del cambio. Si queda activo, el dominio deja de resolver.
3. Haz el cambio **al menos 1 o 2 días antes de la presentación**.

### 4.2 Mover los nameservers
1. Crea una cuenta en https://dash.cloudflare.com → *Agregar un dominio* → `balulaexpress.com` → plan **Free**.
2. Cloudflare importa los registros DNS. **Compáralos con tus capturas** y agrega los que falten (sobre todo MX y TXT). Los registros de correo deben quedar con la **nube gris (Solo DNS)**.
3. Borra los A/CNAME de `@` y `www` que apuntaban al sitio anterior; el túnel creará los nuevos.
4. Cloudflare te muestra **2 nameservers** (algo como `ana.ns.cloudflare.com` y `bob.ns.cloudflare.com`).
5. Namecheap → *Domain* → *Nameservers*: cambia *Namecheap BasicDNS* por **Custom DNS**, pega los dos y guarda (✓).
6. Espera el correo de Cloudflare "*balulaexpress.com is now active*". Suele tardar minutos y como máximo 24 h. Para comprobarlo: `nslookup -type=ns balulaexpress.com 1.1.1.1`.
7. En Cloudflare → *SSL/TLS* → *Edge Certificates* → **Always Use HTTPS: On**.

### 4.3 Crear el túnel con nombre (una sola vez)
En PowerShell:
```powershell
winget install --id Cloudflare.cloudflared        # luego cierra y abre la terminal
cloudflared --version
cloudflared tunnel login                           # abre el navegador: elige balulaexpress.com y autoriza
cloudflared tunnel create balula                   # imprime el ID (UUID) y la ruta del archivo .json
cloudflared tunnel route dns balula balulaexpress.com
cloudflared tunnel route dns balula www.balulaexpress.com
```
- Si `route dns` dice que el registro ya existe, bórralo en Cloudflare → DNS, o repite el comando con `--overwrite-dns`.
- Copia `deploy\cloudflared\config.yml.example` a `C:\Users\<tu_usuario>\.cloudflared\config.yml` y reemplaza `<TU_USUARIO>` y `<UUID_DEL_TUNEL>`.
- Valida la configuración con `cloudflared tunnel ingress validate`. Debe decir *OK*.

### 4.4 Levantar app y túnel juntos
Desde la carpeta raíz del proyecto:
```powershell
powershell -ExecutionPolicy Bypass -File .\iniciar_balula.ps1     # inicia la app (Waitress) + túnel y verifica
powershell -ExecutionPolicy Bypass -File .\verificar_balula.ps1   # solo verificar
powershell -ExecutionPolicy Bypass -File .\detener_balula.ps1     # detener todo
```
La app escucha solo en `127.0.0.1:5001`. Nadie de tu red puede entrar directo; el único acceso es el túnel.
Mientras quieras el sitio en línea, la computadora debe seguir **encendida, sin suspenderse y con internet**.

---

## 5. Twilio (llamadas)

1. Crea la cuenta en https://www.twilio.com/try-twilio. En *Console* → *Account Info* copia el **Auth Token** → `TWILIO_AUTH_TOKEN`.
2. Compra un número en *Phone Numbers* → *Buy a number*. Ten en cuenta:
   - Los números de **México** piden un *Regulatory Bundle* (identificación y domicilio) y pueden tardar días.
   - Un número de **EE. UU.** se obtiene al momento; quien llame desde México paga larga distancia.
   - En cuenta de prueba (*trial*) solo puedes llamar desde números verificados, y Twilio reproduce un aviso antes de contestar.
3. *Phone Numbers* → tu número → *Configure* → **Voice Configuration** → *A call comes in*: **Webhook**
   - URL: **`https://balulaexpress.com/voz/twilio/entrante`**
   - Método: **HTTP POST**
4. **Comprobar:** llama al número. Lula saluda y te escucha. En Twilio → *Monitor* → *Logs* → *Calls* → la llamada → *Request Inspector* verás las peticiones con **200**.
   - **403:** la URL configurada en Twilio no es exactamente `PUBLIC_BASE_URL` + `/voz/twilio/entrante`, o el `TWILIO_AUTH_TOKEN` es otro.
   - **11200** en *Monitor* → *Errors*: Twilio no pudo llegar al sitio; revisa que el túnel esté corriendo.

---

## 6. Lista de verificación para el día de la presentación

**Un día antes**
- [ ] `iniciar_balula.ps1` → `verificar_balula.ps1` sin fallas.
- [ ] Compra de prueba con 4242: la guía se genera y el webhook aparece con 200 en Stripe.
- [ ] El chat de Lula responde (rastreo, cotización, queja con código por correo).
- [ ] Llamada de prueba (si usas Twilio).
- [ ] Respaldo "lista para demo" (`respaldar-base`).

**1 hora antes**
1. Laptop **conectada a la corriente** e internet por **cable** si es posible.
2. Evita que se duerma: `powercfg /change standby-timeout-ac 0` y `powercfg /change monitor-timeout-ac 0`.
3. Cierra programas pesados; esta computadora se ha quedado sin memoria antes.
4. Abre PowerShell en la carpeta del proyecto y corre `powershell -ExecutionPolicy Bypass -File .\iniciar_balula.ps1`.
5. Espera el mensaje **"Todo responde correctamente"**.
6. Abre en el navegador (Edge o Chrome; **Edge tiene la mejor voz**):
   - `https://balulaexpress.com` (inicio y chat)
   - `https://balulaexpress.com/iniciar-sesion` → entra como admin en una pestaña y como repartidor en otra ventana privada
   - `https://balulaexpress.com/salud` → `ok: true`, `pagos: "stripe"`, `asistente: true`, `https: true`
7. Ten a la mano: la tarjeta **4242 4242 4242 4242**, las contraseñas demo y tu correo abierto (para los códigos de 6 dígitos).

**Orden sugerido de la demo**
1. Sitio → cotizar → pagar con Stripe (aviso de modo de prueba) → guía PDF → rastreo.
2. Admin → asignación automática → mapa en vivo.
3. Repartidor (celular) → optimizar ruta → entregar con código, firma y foto → el rastreo se actualiza.
4. Lula por chat y voz: queja por retraso → código por correo → cupón o reembolso → Admin → Reembolsos (estado real de Stripe).
5. Empresas → carga de Excel con descuento por volumen → certificado verde.

**Plan B (si falla internet o el túnel)**
- Presenta en local: `http://127.0.0.1:5001`. **Antes**, en `.env` cambia `SESSION_COOKIE_SECURE=0` y deja `PUBLIC_BASE_URL=` vacío, y reinicia la app. Con la cookie segura no podrás iniciar sesión por http, y con la URL pública Stripe regresaría al dominio caído.
- En local, Stripe funciona para cobrar (redirige a Stripe y regresa), pero el webhook no llega. La guía igual se genera al regresar del pago, porque la app consulta a Stripe directamente.
