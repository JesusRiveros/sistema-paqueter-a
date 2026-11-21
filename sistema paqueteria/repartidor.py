"""Portal del repartidor: entregas del día, ruta optimizada, prueba de entrega e incidencias."""
import base64
import os
import re
import secrets
from datetime import date, datetime

from flask import Blueprint, abort, current_app, flash, g, jsonify, redirect, render_template, request, session, url_for
from sqlalchemy import select

import db
import modelo_retraso
import notificaciones
import rutas
from auth import login_requerido
from models import Envio, Incidencia, PruebaEntrega, UbicacionRepartidor, ahora, sa

bp = Blueprint('repartidor', __name__, url_prefix='/repartidor')

# Transiciones que puede hacer el repartidor con un botón
SIGUIENTE_ESTADO = {'creado': 'recolectado', 'recolectado': 'en_transito', 'en_transito': 'en_reparto'}
TIPOS_INCIDENCIA = {
    'ausente': 'No había nadie para recibir',
    'direccion_incorrecta': 'La dirección es incorrecta o incompleta',
    'rechazado': 'El destinatario rechazó el paquete',
    'danado': 'El paquete presenta daños',
    'acceso': 'No se pudo acceder al domicilio',
    'otro': 'Otro motivo',
}
RELACIONES = {'destinatario': 'Destinatario', 'familiar': 'Familiar', 'vecino': 'Vecino', 'recepcion': 'Recepción / vigilancia'}
MAX_INTENTOS_CODIGO = 5
RE_FIRMA = re.compile(r'^data:image/png;base64,[A-Za-z0-9+/=]+$')
EXT_FOTO = {'image/jpeg': '.jpg', 'image/png': '.png', 'image/webp': '.webp'}
CEDIS = (19.4895, -99.1795)  # Bodega Vallejo: punto de salida si no hay ubicación del repartidor


def _mis_envios(pendientes=True):
    q = select(Envio).where(Envio.repartidor_id == g.usuario.id)
    if pendientes:
        q = q.where(Envio.estado != 'entregado')
    return sa.session.scalars(q.order_by(Envio.orden_ruta.is_(None), Envio.orden_ruta, Envio.asignado_en)).all()


def _envio_propio(guia):
    envio = db.obtener_envio_modelo(guia)
    if not envio or envio.repartidor_id != g.usuario.id:
        abort(404)
    return envio


def _punto_inicio():
    ub = sa.session.get(UbicacionRepartidor, g.usuario.id)
    if ub and (datetime.now() - ub.fecha).total_seconds() < 3600:
        return (ub.lat, ub.lng), 'tu ubicación actual'
    return CEDIS, 'el CEDIS Vallejo'


def _riesgo(envio, num_parada, km_parada):
    incid = sa.session.query(Incidencia).filter(Incidencia.guia == envio.guia).count()
    hora = datetime.now().hour
    prob = modelo_retraso.predecir({
        'hora_pico': float(hora in (7, 8, 9, 17, 18, 19)), 'km_parada': km_parada, 'num_parada': num_parada,
        'es_rapido': float(envio.cotizacion.get('tipo_envio') == 'fast'), 'incidencias_zona': min(incid, 3),
        'fin_de_semana': float(date.today().weekday() >= 5)},
        os.path.join(current_app.instance_path, 'modelo_retraso.json'))
    return {'prob': round(prob * 100), 'nivel': modelo_retraso.nivel(prob)}


def _paradas_con_riesgo(envios):
    inicio, _ = _punto_inicio()
    paradas, previo = [], inicio
    for n, e in enumerate(envios, start=1):
        punto = (e.destino_lat, e.destino_lng) if e.destino_lat is not None else None
        km = rutas.km_entre(previo, punto) if punto else 0.0
        paradas.append({'envio': e, 'num': n, 'km': round(km, 1), 'riesgo': _riesgo(e, n, km)})
        if punto:
            previo = punto
    return paradas


@bp.route('/')
@login_requerido('repartidor')
def hoy():
    envios = _mis_envios()
    entregados_hoy = sa.session.scalars(select(Envio).join(PruebaEntrega, PruebaEntrega.guia == Envio.guia).where(
        PruebaEntrega.repartidor_id == g.usuario.id, PruebaEntrega.fecha >= datetime.combine(date.today(), datetime.min.time()))).all()
    inicio, inicio_txt = _punto_inicio()
    paradas = _paradas_con_riesgo(envios)
    km_total = round(rutas.longitud_ruta(inicio, [(p['envio'].destino_lat, p['envio'].destino_lng) for p in paradas
                                                   if p['envio'].destino_lat is not None]), 1)
    mapa = [{'guia': p['envio'].guia, 'num': p['num'], 'lat': p['envio'].destino_lat, 'lng': p['envio'].destino_lng,
             'nombre': p['envio'].destinatario.get('nombre'), 'riesgo': p['riesgo']['nivel'],
             'url': url_for('repartidor.detalle', guia=p['envio'].guia)} for p in paradas if p['envio'].destino_lat is not None]
    return render_template('repartidor/hoy.html', paradas=paradas, entregados_hoy=entregados_hoy, km_total=km_total,
                           inicio=inicio, inicio_txt=inicio_txt, mapa=mapa,
                           modelo=modelo_retraso.cargar(os.path.join(current_app.instance_path, 'modelo_retraso.json')))


@bp.route('/optimizar', methods=['POST'])
@login_requerido('repartidor')
def optimizar():
    envios = _mis_envios()
    inicio, inicio_txt = _punto_inicio()
    resultado = rutas.optimizar(inicio, [{'envio': e, 'lat': e.destino_lat, 'lng': e.destino_lng} for e in envios])
    for i, p in enumerate(resultado['paradas'], start=1):
        p['envio'].orden_ruta = i
    sa.session.commit()
    if resultado['km_ahorrados'] > 0:
        flash(f"Ruta optimizada desde {inicio_txt}: {resultado['km']} km en total, "
              f"{resultado['km_ahorrados']} km menos que el orden anterior.", 'success')
    else:
        flash(f"Tu ruta ya estaba en el mejor orden ({resultado['km']} km).", 'info')
    return redirect(url_for('repartidor.hoy'))


@bp.route('/envio/<guia>')
@login_requerido('repartidor')
def detalle(guia):
    envio = _envio_propio(guia)
    return render_template('repartidor/detalle.html', envio=envio, d=db.envio_a_dict(envio),
                           siguiente=SIGUIENTE_ESTADO.get(envio.estado), nombres=db.NOMBRE_ESTADO,
                           tipos_incidencia=TIPOS_INCIDENCIA, relaciones=RELACIONES)


def _coords_formulario():
    lat = request.form.get('lat', type=float)
    lng = request.form.get('lng', type=float)
    if lat is None or lng is None or not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return None, None
    return lat, lng


@bp.route('/envio/<guia>/avanzar', methods=['POST'])
@login_requerido('repartidor')
def avanzar(guia):
    envio = _envio_propio(guia)
    nuevo = SIGUIENTE_ESTADO.get(envio.estado)
    if not nuevo or request.form.get('estado') != nuevo:
        flash('Ese cambio de estado no es válido para este envío.', 'warning')
        return redirect(url_for('repartidor.detalle', guia=guia))
    lat, lng = _coords_formulario()
    db.registrar_evento(guia, nuevo, ubicacion=request.form.get('ubicacion') or None, usuario_id=g.usuario.id, lat=lat, lng=lng)
    notificaciones.notificar(guia, nuevo)
    flash(f'Envío {guia}: {db.NOMBRE_ESTADO[nuevo]}.', 'success')
    return redirect(url_for('repartidor.detalle', guia=guia))


@bp.route('/envio/<guia>/entregar', methods=['POST'])
@login_requerido('repartidor')
def entregar(guia):
    envio = _envio_propio(guia)
    if envio.estado != 'en_reparto':
        flash('Primero marca el paquete como "En reparto".', 'warning')
        return redirect(url_for('repartidor.detalle', guia=guia))

    clave_intentos = f'intentos_codigo_{guia}'
    intentos = session.get(clave_intentos, 0)
    if intentos >= MAX_INTENTOS_CODIGO:
        flash('Demasiados códigos incorrectos. Registra una incidencia y comunícate con operaciones.', 'danger')
        return redirect(url_for('repartidor.detalle', guia=guia))
    if not secrets.compare_digest(request.form.get('codigo', '').strip(), envio.codigo_entrega):
        session[clave_intentos] = intentos + 1
        flash(f'El código de entrega no coincide. Te quedan {MAX_INTENTOS_CODIGO - intentos - 1} intentos.', 'danger')
        return redirect(url_for('repartidor.detalle', guia=guia))

    receptor = request.form.get('receptor', '').strip()[:120]
    relacion = request.form.get('relacion', '')
    firma = request.form.get('firma', '')
    if len(receptor) < 3 or relacion not in RELACIONES:
        flash('Escribe el nombre de quien recibe y su relación con el destinatario.', 'danger')
        return redirect(url_for('repartidor.detalle', guia=guia))
    if not RE_FIRMA.match(firma) or len(firma) > 400_000:
        flash('Falta la firma de quien recibe.', 'danger')
        return redirect(url_for('repartidor.detalle', guia=guia))

    foto_nombre = None
    foto = request.files.get('foto')
    if foto and foto.filename:
        ext = EXT_FOTO.get(foto.mimetype)
        if not ext:
            flash('La foto debe ser JPG, PNG o WebP.', 'danger')
            return redirect(url_for('repartidor.detalle', guia=guia))
        os.makedirs(current_app.config['UPLOAD_FOLDER'], exist_ok=True)
        foto_nombre = f'{guia}-{secrets.token_hex(6)}{ext}'
        foto.save(os.path.join(current_app.config['UPLOAD_FOLDER'], foto_nombre))

    lat, lng = _coords_formulario()
    sa.session.add(PruebaEntrega(guia=guia, repartidor_id=g.usuario.id, receptor_nombre=receptor, relacion=relacion,
                                 firma_png=firma, foto_archivo=foto_nombre, lat=lat, lng=lng))
    sa.session.commit()
    db.registrar_evento(guia, 'entregado', descripcion=f'Entregado a {receptor} ({RELACIONES[relacion].lower()})',
                        usuario_id=g.usuario.id, lat=lat, lng=lng)
    session.pop(clave_intentos, None)
    notificaciones.notificar(guia, 'entregado')
    flash(f'¡Entregado! {guia} quedó registrado con firma{" y foto" if foto_nombre else ""}.', 'success')
    return redirect(url_for('repartidor.hoy'))


@bp.route('/envio/<guia>/incidencia', methods=['POST'])
@login_requerido('repartidor')
def incidencia(guia):
    envio = _envio_propio(guia)
    tipo = request.form.get('tipo', '')
    if tipo not in TIPOS_INCIDENCIA:
        flash('Elige el tipo de incidencia.', 'danger')
        return redirect(url_for('repartidor.detalle', guia=guia))
    nota = request.form.get('nota', '').strip()[:500] or None
    lat, lng = _coords_formulario()
    sa.session.add(Incidencia(guia=guia, repartidor_id=g.usuario.id, tipo=tipo, nota=nota, lat=lat, lng=lng))
    envio.eventos.append(db.Evento(estado=envio.estado, descripcion=f'Incidencia: {TIPOS_INCIDENCIA[tipo]}',
                                   ubicacion=None, usuario_id=g.usuario.id, lat=lat, lng=lng))
    sa.session.commit()
    notificaciones.notificar(guia, 'incidencia', detalle=f'{TIPOS_INCIDENCIA[tipo]}. Intentaremos de nuevo pronto.')
    flash('Incidencia registrada. Operaciones y el cliente ya fueron avisados.', 'success')
    return redirect(url_for('repartidor.hoy'))


@bp.route('/envio/<guia>/rutas')
@login_requerido('repartidor')
def rutas_alternas(guia):
    envio = _envio_propio(guia)
    if envio.destino_lat is None:
        return jsonify(ok=False, error='Este envío no tiene coordenadas de destino.'), 404
    inicio, _ = _punto_inicio()
    return jsonify(ok=True, **rutas.rutas_alternas(inicio, (envio.destino_lat, envio.destino_lng)))


@bp.route('/ubicacion', methods=['POST'])
@login_requerido('repartidor')
def ubicacion():
    d = request.get_json(silent=True) or {}
    try:
        lat, lng = float(d['lat']), float(d['lng'])
    except (KeyError, TypeError, ValueError):
        return jsonify(ok=False), 400
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return jsonify(ok=False), 400
    ub = sa.session.get(UbicacionRepartidor, g.usuario.id) or UbicacionRepartidor(repartidor_id=g.usuario.id)
    ub.lat, ub.lng, ub.fecha = lat, lng, ahora()
    ub.precision_m = d.get('precision') if isinstance(d.get('precision'), (int, float)) else None
    sa.session.add(ub)
    sa.session.commit()
    return jsonify(ok=True)
