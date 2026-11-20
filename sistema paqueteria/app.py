"""Balula Express: sitio web, cotizador y rastreo de envíos."""
import math
import os
import re
import secrets
from datetime import date
from functools import wraps

import click
import requests
from dotenv import load_dotenv
from flask import (Flask, abort, flash, jsonify, redirect, render_template, request,
                   Response, session, url_for)

import db
import tarifas
from guia_pdf import generar_guia_pdf

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, '..', '.env'))

app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.getenv('SECRET_KEY') or secrets.token_hex(32),
    DATABASE=os.getenv('DATABASE') or os.path.join(BASE_DIR, 'instance', 'balula.sqlite3'),
    GOOGLE_MAPS_API_KEY=os.getenv('GOOGLE_MAPS_API_KEY', ''),
    PUBLIC_BASE_URL=os.getenv('PUBLIC_BASE_URL', '').rstrip('/'),
    PAGOS_SIMULADOS=os.getenv('PAGOS_SIMULADOS', '1') == '1',
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
)
db.init_app(app)

CONTACTO = {
    'empresa': 'Balula Express S.A. de C.V.',
    'direccion': 'Av. Industria #742, Bodega 5, Col. Industrial Vallejo, Alcaldía Azcapotzalco, C.P. 02300, Ciudad de México',
    'correo': 'contacto@baluladata.com',
    'telefono': '55 7066 5793',
    'telefono_href': '+525570665793',
    'horario': 'Lunes a viernes de 9:00 a 19:00 · Sábados de 9:00 a 14:00',
}

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
# Google Maps (servidor)
# ---------------------------------------------------------------------------

def descomponer_direccion(direccion):
    vacio = {'calle': '', 'colonia': '', 'ciudad': '', 'estado': '', 'cp': ''}
    if not app.config['GOOGLE_MAPS_API_KEY'] or not direccion:
        return vacio
    try:
        resp = requests.get('https://maps.googleapis.com/maps/api/geocode/json',
                            params={'address': direccion, 'key': app.config['GOOGLE_MAPS_API_KEY'],
                                    'language': 'es', 'region': 'mx'},
                            timeout=8).json()
        componentes = resp['results'][0]['address_components']
    except (requests.RequestException, KeyError, IndexError, ValueError) as e:
        app.logger.warning('No se pudo geocodificar "%s": %s', direccion, e)
        return vacio

    partes = dict(vacio)
    numero = ruta = ''
    for comp in componentes:
        tipos = comp['types']
        if 'street_number' in tipos:
            numero = comp['long_name']
        elif 'route' in tipos:
            ruta = comp['long_name']
        elif 'sublocality_level_1' in tipos or 'neighborhood' in tipos:
            partes['colonia'] = comp['long_name']
        elif 'locality' in tipos:
            partes['ciudad'] = comp['long_name']
        elif 'administrative_area_level_1' in tipos:
            partes['estado'] = comp['long_name']
        elif 'postal_code' in tipos:
            partes['cp'] = comp['long_name']
    partes['calle'] = f'{ruta} {numero}'.strip()
    return partes


def _haversine_km(lat1, lon1, lat2, lon2):
    r = 6371
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _float_o_none(valor):
    try:
        n = float(valor)
        return n if math.isfinite(n) else None
    except (TypeError, ValueError):
        return None


def calcular_distancia_km(origen, destino, coords=None, distancia_cliente=None):
    """Distancia por carretera. Orden: Distance Matrix → ruta del navegador → línea recta × 1.25."""
    if app.config['GOOGLE_MAPS_API_KEY']:
        try:
            data = requests.get('https://maps.googleapis.com/maps/api/distancematrix/json',
                                params={'origins': origen, 'destinations': destino,
                                        'key': app.config['GOOGLE_MAPS_API_KEY']},
                                timeout=8).json()
            if data.get('status') != 'OK':
                app.logger.error('Google Distance Matrix rechazó la petición: %s %s',
                                 data.get('status'), data.get('error_message', ''))
            elemento = data['rows'][0]['elements'][0]
            if elemento.get('status') == 'OK':
                return round(elemento['distance']['value'] / 1000, 1)
        except (requests.RequestException, KeyError, IndexError, ValueError) as e:
            app.logger.warning('Distance Matrix falló: %s', e)

    distancia_cliente = _float_o_none(distancia_cliente)
    if distancia_cliente and 0 < distancia_cliente < 10000:
        return round(distancia_cliente, 1)

    if coords and all(c is not None for c in coords):
        return round(_haversine_km(*coords) * 1.25, 1)
    return None


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


@app.route('/iniciar-sesion')
def login():
    return render_template('sitio/login.html')


@app.route('/formulario')
def formulario():
    return redirect(url_for('login'), 301)


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
    return render_template('sitio/rastreo.html', guia=guia, envio=envio, error=error,
                           estados=db.ESTADOS, estados_idx=db.ESTADOS_IDX)


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
        coords = [_float_o_none(datos[k]) for k in ('origen_lat', 'origen_lng', 'destino_lat', 'destino_lng')]
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

    session['formulario'] = datos
    session['cotizacion'] = {
        'origen': datos['origen'],
        'destino': datos['destino'],
        'tipo_entrega': datos['tipo_entrega'],
        'origen_partes': descomponer_direccion(datos['origen']),
        'destino_partes': descomponer_direccion(datos['destino']),
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
    return render_template('cotizador/pago.html', paso_actual=4, c=session['cotizacion'],
                           contactos=session['contactos'])


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
    c = session['cotizacion']
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
        )
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
# Errores
# ---------------------------------------------------------------------------

@app.errorhandler(404)
def no_encontrado(_e):
    return render_template('sitio/error.html', codigo=404, titulo='Página no encontrada',
                           mensaje='La página que buscas no existe o fue movida.'), 404


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
    app.run(debug=os.getenv('FLASK_DEBUG') == '1', port=5001)
