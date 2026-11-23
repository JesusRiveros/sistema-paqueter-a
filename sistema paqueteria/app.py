"""Balula Express: sitio web, cotizador y rastreo de envíos."""
import os
import re
import secrets
from datetime import date, datetime
import time
from functools import wraps

import click
from dotenv import load_dotenv
from flask import (Flask, abort, flash, jsonify, redirect, render_template, request,
                   Response, session, url_for)

import admin
import asistente
import auth
import clientes
import db
import demo
import empresas
import geo
import notificaciones
import reembolsos
import repartidor
import tarifas
from guia_pdf import generar_guia_pdf

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, '..', '.env'))

app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.getenv('SECRET_KEY') or secrets.token_hex(32),
    SQLALCHEMY_DATABASE_URI=os.getenv('DATABASE_URL') or 'sqlite:///' + os.path.join(BASE_DIR, 'instance', 'balula.sqlite3'),
    SQLALCHEMY_ENGINE_OPTIONS={'pool_pre_ping': True},
    UPLOAD_FOLDER=os.getenv('UPLOAD_FOLDER') or os.path.join(BASE_DIR, 'instance', 'archivos'),
    MAX_CONTENT_LENGTH=8 * 1024 * 1024,
    GOOGLE_MAPS_API_KEY=os.getenv('GOOGLE_MAPS_API_KEY', ''),
    PUBLIC_BASE_URL=os.getenv('PUBLIC_BASE_URL', '').rstrip('/'),
    PAGOS_SIMULADOS=os.getenv('PAGOS_SIMULADOS', '1') == '1',
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    PERMANENT_SESSION_LIFETIME=auth.DURACION_SESION,
    SESSION_COOKIE_SECURE=os.getenv('SESSION_COOKIE_SECURE', '0') == '1',
)
os.makedirs(os.path.join(BASE_DIR, 'instance'), exist_ok=True)
db.init_app(app)

CONTACTO = {
    'empresa': 'Balula Express S.A. de C.V.',
    'direccion': 'Av. Industria #742, Bodega 5, Col. Industrial Vallejo, Alcaldía Azcapotzalco, C.P. 02300, Ciudad de México',
    'correo': 'contacto@baluladata.com',
    'telefono': '55 7066 5793',
    'telefono_href': '+525570665793',
    'horario': 'Lunes a viernes de 9:00 a 19:00 · Sábados de 9:00 a 14:00',
}

app.config['CONTACTO'] = CONTACTO
app.register_blueprint(auth.bp)
app.register_blueprint(repartidor.bp)
app.register_blueprint(admin.bp)
app.register_blueprint(clientes.bp)
app.register_blueprint(empresas.bp)
demo.registrar_comando(app)

RE_TELEFONO = re.compile(r'^\d{10}$')
RE_CP = re.compile(r'^\d{5}$')
RE_CORREO = re.compile(r'^[^\s@]+@[^\s@]+\.[^\s@]+$')
RE_GUIA = re.compile(r'^[A-Za-z0-9-]{4,20}$')


# ---------------------------------------------------------------------------
# Utilidades de plantilla
# ---------------------------------------------------------------------------

DIAS = ['lun', 'mar', 'mié', 'jue', 'vie', 'sáb', 'dom']
MESES = ['ene', 'feb', 'mar', 'abr', 'may', 'jun', 'jul', 'ago', 'sep', 'oct', 'nov', 'dic']


@app.template_filter('fecha')
def filtro_fecha(iso, con_dia=True):
    if not iso:
        return ''
    d = date.fromisoformat(str(iso)[:10])
    texto = f'{d.day} {MESES[d.month - 1]}'
    return f'{DIAS[d.weekday()]} {texto}' if con_dia else f'{texto} {d.year}'


@app.template_filter('fecha_hora')
def filtro_fecha_hora(iso):
    return f'{filtro_fecha(iso, con_dia=False)}, {str(iso)[11:16]}'


@app.template_filter('moneda')
def filtro_moneda(valor):
    try:
        return f'${float(valor):,.2f}'
    except (TypeError, ValueError):
        return '—'


@app.context_processor
def contexto_global():
    return {
        'google_maps_api_key': app.config['GOOGLE_MAPS_API_KEY'],
        'pagos_simulados': app.config['PAGOS_SIMULADOS'],
        'csrf_token': csrf_token,
        'contacto': CONTACTO,
        'tarifas': tarifas,
        'anio_actual': date.today().year,
        'nombres_estado': db.NOMBRE_ESTADO,
    }


# ---------------------------------------------------------------------------
# Seguridad: token CSRF para formularios y la API
# ---------------------------------------------------------------------------

def csrf_token():
    if '_csrf' not in session:
        session['_csrf'] = secrets.token_urlsafe(32)
    return session['_csrf']


@app.before_request
def verificar_csrf():
    if request.method != 'POST':
        return
    enviado = request.form.get('_csrf') or request.headers.get('X-CSRF-Token')
    if not enviado or not secrets.compare_digest(enviado, session.get('_csrf', '')):
        if request.is_json:
            return jsonify(ok=False, error='La sesión expiró. Recarga la página.'), 400
        flash('Tu sesión expiró. Intenta de nuevo.', 'warning')
        return redirect(request.referrer or url_for('home'))


@app.after_request
def cabeceras_seguridad(resp):
    resp.headers.setdefault('X-Content-Type-Options', 'nosniff')
    resp.headers.setdefault('X-Frame-Options', 'SAMEORIGIN')
    resp.headers.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')
    return resp


# ---------------------------------------------------------------------------
# Mapas: Google con respaldo en OpenStreetMap (ver geo.py)
# ---------------------------------------------------------------------------

def ubicar_direccion(direccion, lat=None, lng=None):
    """Partes de la dirección (para prellenar el paso 3) y coordenadas (para rutas y mapa)."""
    r = geo.geocodificar(direccion, app.config['GOOGLE_MAPS_API_KEY'])
    partes = r['partes'] if r else {'calle': '', 'colonia': '', 'ciudad': '', 'estado': '', 'cp': ''}
    if lat is None and r:
        lat, lng = r['lat'], r['lng']
    return partes, ({'lat': lat, 'lng': lng} if lat is not None and lng is not None else None)


def calcular_distancia_km(origen, destino, coords=None, distancia_cliente=None):
    """Distancia por carretera en km, o None si ningún proveedor pudo calcularla."""
    try:
        km, fuente = geo.distancia_km(origen, destino, app.config['GOOGLE_MAPS_API_KEY'], coords, distancia_cliente)
    except geo.SinProveedores as e:
        app.logger.warning('Sin distancia para "%s" → "%s": %s', origen, destino, e)
        return None
    app.logger.info('Distancia %s km (fuente: %s)', km, fuente)
    return km


# ---------------------------------------------------------------------------
# Flujo del cotizador: protección de pasos
# ---------------------------------------------------------------------------

def requiere(*claves):
    """Redirige al paso 1 si faltan datos de pasos anteriores en la sesión."""
    def decorador(vista):
        @wraps(vista)
        def envoltura(*args, **kwargs):
            if 'cotizacion' not in session:
                flash('Primero cotiza tu envío.', 'info')
                return redirect(url_for('index'))
            if 'contactos' in claves and 'contactos' not in session:
                flash('Completa los datos de remitente y destinatario.', 'info')
                return redirect(url_for('completar'))
            return vista(*args, **kwargs)
        return envoltura
    return decorador


def paso_desde_sesion():
    return 3 if 'contactos' in session else 2 if 'cotizacion' in session else 1


# ---------------------------------------------------------------------------
# Sitio web
# ---------------------------------------------------------------------------

@app.route('/')
def home():
    return render_template('sitio/inicio.html')


@app.route('/inicio')
def inicio():
    return redirect(url_for('home'), 301)


@app.route('/contacto', methods=['POST'])
def contacto():
    nombre = request.form.get('nombre', '').strip()[:120]
    correo = request.form.get('correo', '').strip()[:160]
    mensaje = request.form.get('mensaje', '').strip()[:3000]
    if not nombre or not RE_CORREO.match(correo) or len(mensaje) < 10:
        flash('Revisa tu nombre, un correo válido y un mensaje de al menos 10 caracteres.', 'danger')
        return redirect(url_for('home', _anchor='contacto'))
    db.guardar_mensaje(nombre, correo, mensaje)
    flash('¡Gracias! Recibimos tu mensaje y te responderemos pronto.', 'success')
    return redirect(url_for('home', _anchor='contacto'))


@app.route('/formulario')
def formulario():
    return redirect(url_for('auth.login'), 301)


@app.route('/rastreo')
def rastreo():
    guia = (request.args.get('guia') or request.args.get('tracking') or '').strip().upper()
    envio = error = None
    if guia:
        if not RE_GUIA.match(guia):
            error = 'El número de guía solo puede contener letras, números y guiones.'
        else:
            envio = db.obtener_envio(guia)
            if not envio:
                error = f'No encontramos la guía {guia}. Verifica que esté escrita correctamente.'
    extra = clientes.contexto_rastreo(guia) if envio else {}
    return render_template('sitio/rastreo.html', guia=guia, envio=envio, error=error,
                           estados=db.ESTADOS, estados_idx=db.ESTADOS_IDX, **extra)


# ---------------------------------------------------------------------------
# Cotizador
# ---------------------------------------------------------------------------

@app.route('/app/')
def index():
    datos = dict(session.get('formulario', {}))
    for campo in ('origen', 'destino', 'peso'):
        if request.args.get(campo):
            datos[campo] = request.args[campo][:200]
    return render_template('cotizador/cotizar.html', paso_actual=1, datos=datos, errores={})


@app.route('/app/api/cotizar', methods=['POST'])
def api_cotizar():
    """Cotización en vivo para el formulario. Usa exactamente la misma lógica que el cálculo final."""
    d = request.get_json(silent=True) or {}
    try:
        cot = tarifas.cotizar(
            distancia_km=d.get('distancia_km') or 0,
            peso=d.get('peso'), largo=d.get('largo'), ancho=d.get('ancho'), alto=d.get('alto'),
            caja=d.get('caja') or 'propia', tipo_envio=d.get('tipo_envio') or 'eco',
            compensar_co2=bool(d.get('compensar')),
        )
    except tarifas.ErrorCotizacion as e:
        return jsonify(ok=False, error=str(e)), 422
    return jsonify(ok=True, cotizacion=cot.to_dict(), distancia_estimada=bool(d.get('distancia_km')),
                   cajas_disponibles=tarifas.cajas_disponibles(cot.largo, cot.ancho, cot.alto, cot.peso_real))


@app.route('/app/calculate', methods=['POST'], endpoint='calculate')
def calcular_envio():
    f = request.form
    datos = {k: f.get(k, '').strip() for k in (
        'origen', 'destino', 'peso', 'largo', 'ancho', 'alto', 'caja', 'tipo_entrega', 'tipo_envio',
        'origen_lat', 'origen_lng', 'destino_lat', 'destino_lng', 'distancia_ruta_km')}
    datos['compensar'] = f.get('compensar') == 'si'
    errores = {}

    if len(datos['origen']) < 5:
        errores['origen'] = 'Escribe la dirección de origen y elígela de la lista.'
    if len(datos['destino']) < 5:
        errores['destino'] = 'Escribe la dirección de destino y elígela de la lista.'
    if datos['tipo_entrega'] not in tarifas.TIPOS_ENTREGA:
        errores['tipo_entrega'] = 'Elige cómo nos entregarás el paquete.'

    distancia = None
    if not errores:
        coords = [geo.float_o_none(datos[k]) for k in ('origen_lat', 'origen_lng', 'destino_lat', 'destino_lng')]
        distancia = calcular_distancia_km(datos['origen'], datos['destino'], coords, datos['distancia_ruta_km'])
        if distancia is None:
            errores['destino'] = ('No pudimos calcular la distancia entre esas direcciones. Intenta de nuevo en unos '
                                  'minutos o contáctanos para cotizar por teléfono.')

    cot = None
    if not errores:
        try:
            cot = tarifas.cotizar(distancia_km=distancia, peso=datos['peso'], largo=datos['largo'],
                                  ancho=datos['ancho'], alto=datos['alto'], caja=datos['caja'] or 'propia',
                                  tipo_envio=datos['tipo_envio'], compensar_co2=datos['compensar'])
        except tarifas.ErrorCotizacion as e:
            errores['general'] = str(e)

    if errores:
        return render_template('cotizador/cotizar.html', paso_actual=1, datos=datos, errores=errores), 400

    origen_partes, origen_geo = ubicar_direccion(datos['origen'], coords[0], coords[1])
    destino_partes, destino_geo = ubicar_direccion(datos['destino'], coords[2], coords[3])
    session['formulario'] = datos
    session['cotizacion'] = {
        'origen': datos['origen'],
        'destino': datos['destino'],
        'tipo_entrega': datos['tipo_entrega'],
        'origen_partes': origen_partes,
        'destino_partes': destino_partes,
        'origen_geo': origen_geo,
        'destino_geo': destino_geo,
        'detalle': cot.to_dict(),
    }
    session.pop('contactos', None)
    return redirect(url_for('resultado'))


@app.route('/app/cotizacion')
@requiere('cotizacion')
def resultado():
    return render_template('cotizador/resultado.html', paso_actual=2, c=session['cotizacion'])


def _leer_persona(prefijo, f, errores, con_referencias=False):
    p = {k: f.get(f'{prefijo}_{k}', '').strip()[:200] for k in
         ('nombre', 'telefono', 'calle', 'colonia', 'ciudad', 'estado', 'cp')}
    p['telefono'] = re.sub(r'\D', '', p['telefono'])
    for campo in ('nombre', 'calle', 'colonia', 'ciudad', 'estado'):
        if len(p[campo]) < 2:
            errores[f'{prefijo}_{campo}'] = 'Este campo es obligatorio.'
    if not RE_TELEFONO.match(p['telefono']):
        errores[f'{prefijo}_telefono'] = 'Escribe un teléfono de 10 dígitos.'
    if not RE_CP.match(p['cp']):
        errores[f'{prefijo}_cp'] = 'El código postal debe tener 5 dígitos.'
    if con_referencias:
        p['referencias'] = f.get(f'{prefijo}_referencias', '').strip()[:300]
        p['correo'] = f.get(f'{prefijo}_correo', '').strip()[:160]
        if p['correo'] and not RE_CORREO.match(p['correo']):
            errores[f'{prefijo}_correo'] = 'Escribe un correo válido o déjalo vacío.'
    return p


@app.route('/app/completar', methods=['GET', 'POST'])
@requiere('cotizacion')
def completar():
    c = session['cotizacion']
    if request.method == 'POST':
        errores = {}
        remitente = _leer_persona('remitente', request.form, errores)
        destinatario = _leer_persona('destinatario', request.form, errores, con_referencias=True)
        if errores:
            return render_template('cotizador/completar.html', paso_actual=3, c=c, errores=errores,
                                   remitente=remitente, destinatario=destinatario), 400
        session['contactos'] = {'remitente': remitente, 'destinatario': destinatario}
        return redirect(url_for('pago'))

    previos = session.get('contactos', {})
    return render_template('cotizador/completar.html', paso_actual=3, c=c, errores={},
                           remitente=previos.get('remitente', c['origen_partes']),
                           destinatario=previos.get('destinatario', c['destino_partes']))


@app.route('/app/pago')
@requiere('cotizacion', 'contactos')
def pago():
    return render_template('cotizador/pago.html', paso_actual=4, c=cotizacion_con_cupon(),
                           contactos=session['contactos'], cupon=session.get('cupon'))


def cotizacion_con_cupon():
    """La cotización de la sesión, con el cupón aplicado si hay uno válido."""
    c = dict(session['cotizacion'])
    codigo = session.get('cupon')
    if codigo:
        cupon, error = reembolsos.validar_cupon(codigo)
        if cupon:
            c['detalle'] = reembolsos.aplicar_cupon_a_cotizacion(c['detalle'], cupon)
        else:
            session.pop('cupon', None)
    return c


@app.route('/app/cupon', methods=['POST'])
@requiere('cotizacion', 'contactos')
def aplicar_cupon():
    if request.form.get('quitar'):
        session.pop('cupon', None)
        return redirect(url_for('pago'))
    cupon, error = reembolsos.validar_cupon(request.form.get('codigo', ''))
    if error:
        flash(error, 'danger')
    else:
        session['cupon'] = cupon.codigo
        flash(f'Cupón aplicado: {cupon.porcentaje}% de descuento.', 'success')
    return redirect(url_for('pago'))


@app.route('/app/procesar_pago', methods=['POST'])
@requiere('cotizacion', 'contactos')
def procesar_pago():
    if not app.config['PAGOS_SIMULADOS']:
        flash('El pago en línea aún no está disponible. Contáctanos para completar tu envío.', 'warning')
        return redirect(url_for('pago'))
    return redirect(url_for('fake_checkout'))


@app.route('/app/checkout', methods=['GET', 'POST'], endpoint='fake_checkout')
@requiere('cotizacion', 'contactos')
def checkout_simulado():
    if not app.config['PAGOS_SIMULADOS']:
        abort(404)
    c = cotizacion_con_cupon()
    if request.method == 'POST':
        resultado_pago = request.form.get('resultado')
        if resultado_pago == 'failure':
            return redirect(url_for('pago_fallido'))
        if resultado_pago != 'success':
            return redirect(url_for('pago_pendiente'))

        contactos = session['contactos']
        guia = db.crear_envio(
            remitente=contactos['remitente'], destinatario=contactos['destinatario'],
            cotizacion=c['detalle'], tipo_entrega=c['tipo_entrega'],
            correo_destinatario=contactos['destinatario'].get('correo'),
            origen=c.get('origen_geo'), destino=c.get('destino_geo'),
        )
        if session.get('cupon'):
            cupon, _ = reembolsos.validar_cupon(session.pop('cupon'))
            if cupon:
                cupon.usado, cupon.usado_en_guia = datetime.now(), guia
                db.sa.session.commit()
        notificaciones.notificar(guia, 'creado')
        session['mis_guias'] = (session.get('mis_guias', []) + [guia])[-20:]
        for clave in ('cotizacion', 'contactos', 'formulario'):
            session.pop(clave, None)
        return redirect(url_for('confirmado', guia=guia))

    return render_template('cotizador/checkout_simulado.html', c=c, contactos=session['contactos'])


@app.route('/app/pago_fallido')
def pago_fallido():
    return render_template('cotizador/estado_pago.html', estado='fallido')


@app.route('/app/pago_pendiente')
def pago_pendiente():
    return render_template('cotizador/estado_pago.html', estado='pendiente')


def _guia_propia(guia):
    guia = guia.upper()
    if guia not in session.get('mis_guias', []):
        abort(404)
    envio = db.obtener_envio(guia)
    if not envio:
        abort(404)
    return envio


def url_rastreo_publica(guia):
    base = app.config['PUBLIC_BASE_URL'] or request.url_root.rstrip('/')
    return f"{base}{url_for('rastreo', guia=guia)}"


@app.route('/app/envio/<guia>')
def confirmado(guia):
    envio = _guia_propia(guia)
    return render_template('cotizador/confirmado.html', envio=envio, url_rastreo=url_rastreo_publica(envio['guia']))


@app.route('/app/guia/<guia>.pdf')
def descargar_guia(guia):
    envio = _guia_propia(guia)
    pdf = generar_guia_pdf(envio, url_rastreo_publica(envio['guia']))
    return Response(pdf, mimetype='application/pdf', headers={
        'Content-Disposition': f'attachment; filename=guia_{envio["guia"]}.pdf'})


@app.route('/app/download/guia')
def download_guia():
    guias = session.get('mis_guias', [])
    if not guias:
        abort(404)
    return redirect(url_for('descargar_guia', guia=guias[-1]))


@app.route('/app/nueva')
def nueva_cotizacion():
    for clave in ('cotizacion', 'contactos', 'formulario'):
        session.pop(clave, None)
    return redirect(url_for('index'))


# ---------------------------------------------------------------------------
# Asistente de atención al cliente (chat)
# ---------------------------------------------------------------------------

ASISTENTE_LIMITE_MENSAJES = 20      # por sesión...
ASISTENTE_VENTANA_SEG = 5 * 60      # ...cada 5 minutos


def _conversacion_de_sesion(canal='web', crear=True):
    cid = session.get('asistente_conv')
    if cid and db.obtener_conversacion(cid):
        return cid
    if not crear:
        return None
    cid = asistente.nueva_conversacion(canal)
    session['asistente_conv'] = cid
    return cid


@app.route('/api/asistente/estado')
def asistente_estado():
    return jsonify(configurado=asistente.esta_configurado())


@app.route('/api/asistente/historial')
def asistente_historial():
    cid = _conversacion_de_sesion(crear=False)
    mensajes = db.chat_mensajes(cid) if cid else []
    return jsonify(mensajes=[{'rol': m['rol'], 'texto': m['texto']} for m in mensajes])


@app.route('/api/asistente/reiniciar', methods=['POST'])
def asistente_reiniciar():
    session.pop('asistente_conv', None)
    return jsonify(ok=True)


@app.route('/api/asistente/mensaje', methods=['POST'])
def asistente_mensaje():
    datos = request.get_json(silent=True) or {}
    texto = str(datos.get('texto', '')).strip()
    if not texto:
        return jsonify(ok=False, error='Escribe un mensaje.'), 400
    if len(texto) > 1000:
        return jsonify(ok=False, error='Tu mensaje es muy largo; resúmelo en menos de 1000 caracteres.'), 400

    ahora = time.time()
    recientes = [t for t in session.get('asistente_ts', []) if ahora - t < ASISTENTE_VENTANA_SEG]
    if len(recientes) >= ASISTENTE_LIMITE_MENSAJES:
        return jsonify(ok=False, error='Enviaste muchos mensajes seguidos. Espera un par de minutos.'), 429
    session['asistente_ts'] = recientes + [ahora]

    canal = datos.get('canal') if datos.get('canal') in ('web', 'voz') else 'web'
    cid = _conversacion_de_sesion(canal)
    try:
        r = asistente.responder(cid, texto)
    except asistente.AsistenteNoConfigurado:
        return jsonify(ok=False, error='El asistente no está disponible en este momento. '
                                       f"Llámanos al {CONTACTO['telefono']} o escríbenos a {CONTACTO['correo']}."), 503
    return jsonify(ok=True, texto=r['texto'], escalado=r['escalado'])


@app.cli.command('asistente-costos')
def asistente_costos():
    """Muestra tokens y costo estimado del asistente (total y promedio por conversación)."""
    r = db.resumen_costos_asistente()
    n = r['conversaciones'] or 1
    click.echo(f"Conversaciones: {r['conversaciones']}  ·  turnos: {r['turnos']}  ·  llamadas a la API: {r['llamadas']}")
    click.echo(f"Tokens entrada: {r['entrada']:,}  ·  salida: {r['salida']:,}  ·  caché leída: {r['cache_lectura']:,}  ·  caché escrita: {r['cache_escritura']:,}")
    click.echo(f"Costo estimado total: US${r['costo']:.4f}  ·  promedio por conversación: US${r['costo'] / n:.4f}")


# ---------------------------------------------------------------------------
# Errores
# ---------------------------------------------------------------------------

@app.errorhandler(404)
def no_encontrado(_e):
    return render_template('sitio/error.html', codigo=404, titulo='Página no encontrada',
                           mensaje='La página que buscas no existe o fue movida.'), 404


@app.errorhandler(403)
def prohibido(_e):
    return render_template('sitio/error.html', codigo=403, titulo='Acceso restringido',
                           mensaje='Tu usuario no tiene permiso para ver esta sección.'), 403


@app.errorhandler(500)
def error_interno(_e):
    return render_template('sitio/error.html', codigo=500, titulo='Algo salió mal',
                           mensaje='Tuvimos un problema al procesar tu solicitud. Intenta de nuevo en unos minutos.'), 500


# ---------------------------------------------------------------------------
# Comandos de operación
# ---------------------------------------------------------------------------

@app.cli.command('avanzar-envio')
@click.argument('guia')
@click.argument('estado', type=click.Choice([e[0] for e in db.ESTADOS]))
@click.option('--ubicacion', default=None, help='Ej. "CEDIS Vallejo, CDMX"')
@click.option('--descripcion', default=None, help='Texto que verá el cliente.')
def avanzar_envio(guia, estado, ubicacion, descripcion):
    """Registra un nuevo estado de rastreo para una guía."""
    try:
        db.registrar_evento(guia.upper(), estado, descripcion, ubicacion)
    except ValueError as e:
        raise click.ClickException(str(e))
    click.echo(f'{guia.upper()} → {estado}')


if __name__ == '__main__':
    db.preparar_base(app)
    app.run(debug=os.getenv('FLASK_DEBUG') == '1', port=5001)
