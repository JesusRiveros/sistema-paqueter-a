"""Panel de operaciones: indicadores, envíos, asignación, repartidores y atención a clientes."""
import secrets
import string
from datetime import date, datetime, timedelta

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, send_from_directory, url_for
from sqlalchemy import func, select

import db
import notificaciones
import rutas
from auth import login_requerido
from models import (ChatMensaje, Conversacion, Envio, Escalamiento, Incidencia, MensajeContacto, Notificacion,
                    PruebaEntrega, Queja, UbicacionRepartidor, Usuario, sa)
from repartidor import CEDIS

bp = Blueprint('admin', __name__, url_prefix='/admin')
PENALIZACION_CARGA_KM = 2.0  # cada paquete pendiente "pesa" como 2 km extra al repartir carga


def password_temporal():
    alfabeto = string.ascii_letters + string.digits
    while True:
        pw = ''.join(secrets.choice(alfabeto) for _ in range(12))
        if any(c.isdigit() for c in pw) and any(c.isalpha() for c in pw):
            return pw


def _repartidores(activos=True):
    q = select(Usuario).where(Usuario.rol == 'repartidor')
    if activos:
        q = q.where(Usuario.activo.is_(True))
    return sa.session.scalars(q.order_by(Usuario.nombre)).all()


def _pendientes_por_repartidor():
    filas = sa.session.execute(select(Envio.repartidor_id, func.count()).where(
        Envio.repartidor_id.is_not(None), Envio.estado != 'entregado').group_by(Envio.repartidor_id)).all()
    return dict(filas)


@bp.context_processor
def contadores():
    if not request.endpoint or not request.endpoint.startswith('admin.'):
        return {}
    return {'pendientes_atencion': (
        sa.session.query(MensajeContacto).filter_by(atendido=False).count()
        + sa.session.query(Escalamiento).filter_by(estado='pendiente').count()
        + sa.session.query(Queja).filter_by(estado='abierta').count())}


# ---------------------------------------------------------------- Indicadores

def indicadores():
    hoy = date.today()
    inicio_hoy = datetime.combine(hoy, datetime.min.time())
    hace30 = inicio_hoy - timedelta(days=30)
    envios = sa.session.scalars(select(Envio)).all()
    entregados = [e for e in envios if e.estado == 'entregado']
    pruebas = {p.guia: p for p in sa.session.scalars(select(PruebaEntrega)).all()}
    a_tiempo = sum(1 for e in entregados if e.guia in pruebas
                   and pruebas[e.guia].fecha.date().isoformat() <= e.cotizacion.get('fecha_max', '9999'))
    por_dia = []
    for i in range(13, -1, -1):
        d = hoy - timedelta(days=i)
        por_dia.append({'dia': d, 'n': sum(1 for e in envios if e.creado_en.date() == d)})
    maximo = max([x['n'] for x in por_dia] + [1])
    costos_ia = db.resumen_costos_asistente()
    return {
        'envios_hoy': sum(1 for e in envios if e.creado_en >= inicio_hoy),
        'en_camino': sum(1 for e in envios if e.estado in ('recolectado', 'en_transito', 'en_reparto')),
        'sin_asignar': sum(1 for e in envios if e.repartidor_id is None and e.estado != 'entregado'),
        'entregados': len(entregados),
        'a_tiempo_pct': round(a_tiempo / len(entregados) * 100) if entregados else None,
        'ingresos_30d': sum(e.total for e in envios if e.creado_en >= hace30),
        'incidencias_abiertas': sa.session.query(Incidencia).filter_by(resuelta=False).count(),
        'co2_compensado': round(sum(e.cotizacion.get('co2_kg', 0) for e in envios if e.cotizacion.get('compensar_co2')), 2),
        'por_dia': por_dia, 'max_dia': maximo,
        'ia_conversaciones': costos_ia['conversaciones'],
        'ia_costo_prom': (costos_ia['costo'] / costos_ia['conversaciones']) if costos_ia['conversaciones'] else 0,
    }


@bp.route('/')
@login_requerido('admin')
def panel():
    recientes = sa.session.scalars(select(Envio).order_by(Envio.creado_en.desc()).limit(8)).all()
    return render_template('admin/panel.html', k=indicadores(), recientes=recientes)


# ---------------------------------------------------------------- Envíos y asignación

@bp.route('/envios')
@login_requerido('admin')
def envios():
    filtro = request.args.get('filtro', 'activos')
    q = select(Envio).order_by(Envio.creado_en.desc())
    if filtro == 'activos':
        q = q.where(Envio.estado != 'entregado')
    elif filtro == 'sin_asignar':
        q = q.where(Envio.repartidor_id.is_(None), Envio.estado != 'entregado')
    elif filtro in db.ESTADOS_IDX:
        q = q.where(Envio.estado == filtro)
    return render_template('admin/envios.html', envios=sa.session.scalars(q.limit(300)).all(), filtro=filtro,
                           repartidores=_repartidores(), carga=_pendientes_por_repartidor())


@bp.route('/envios/<guia>')
@login_requerido('admin')
def envio_detalle(guia):
    envio = db.obtener_envio_modelo(guia) or abort(404)
    return render_template('admin/envio.html', envio=envio, d=db.envio_a_dict(envio),
                           prueba=sa.session.scalar(select(PruebaEntrega).where(PruebaEntrega.guia == guia)),
                           incidencias=sa.session.scalars(select(Incidencia).where(Incidencia.guia == guia)).all(),
                           notifs=sa.session.scalars(select(Notificacion).where(Notificacion.guia == guia)).all(),
                           repartidores=_repartidores())


@bp.route('/envios/asignar', methods=['POST'])
@login_requerido('admin')
def asignar():
    envio = db.obtener_envio_modelo(request.form.get('guia', '')) or abort(404)
    rid = request.form.get('repartidor_id', type=int)
    if rid:
        rep = sa.session.get(Usuario, rid)
        if not rep or rep.rol != 'repartidor' or not rep.activo:
            flash('Repartidor inválido.', 'danger')
            return redirect(request.referrer or url_for('admin.envios'))
    envio.repartidor_id = rid or None
    envio.asignado_en = datetime.now() if rid else None
    envio.orden_ruta = None
    sa.session.commit()
    flash(f'{envio.guia}: ' + (f'asignado a {envio.repartidor.nombre}.' if rid else 'sin repartidor.'), 'success')
    return redirect(request.referrer or url_for('admin.envios'))


def auto_asignar():
    """Reparte los envíos sin asignar al repartidor más cercano a su zona, equilibrando la carga."""
    reps = _repartidores()
    if not reps:
        return 0, 'No hay repartidores activos.'
    carga = _pendientes_por_repartidor()
    centros = {}
    for r in reps:
        ub = sa.session.get(UbicacionRepartidor, r.id)
        asignados = [(e.destino_lat, e.destino_lng) for e in r.envios if e.estado != 'entregado' and e.destino_lat is not None]
        if asignados:
            centros[r.id] = (sum(a for a, _ in asignados) / len(asignados), sum(b for _, b in asignados) / len(asignados))
        elif ub:
            centros[r.id] = (ub.lat, ub.lng)
        else:
            centros[r.id] = CEDIS
    sin = sa.session.scalars(select(Envio).where(Envio.repartidor_id.is_(None), Envio.estado != 'entregado')).all()
    n = 0
    for e in sin:
        punto = (e.destino_lat, e.destino_lng) if e.destino_lat is not None else CEDIS
        mejor = min(reps, key=lambda r: rutas.km_entre(centros[r.id], punto) + PENALIZACION_CARGA_KM * carga.get(r.id, 0))
        e.repartidor_id, e.asignado_en, e.orden_ruta = mejor.id, datetime.now(), None
        carga[mejor.id] = carga.get(mejor.id, 0) + 1
        n += 1
    sa.session.commit()
    return n, None


@bp.route('/envios/auto-asignar', methods=['POST'])
@login_requerido('admin')
def auto_asignar_vista():
    n, error = auto_asignar()
    flash(error or (f'{n} envío(s) asignados por cercanía y carga de trabajo.' if n else 'No había envíos sin asignar.'),
          'warning' if error else 'success')
    return redirect(url_for('admin.envios'))


@bp.route('/envios/<guia>/estado', methods=['POST'])
@login_requerido('admin')
def cambiar_estado(guia):
    estado = request.form.get('estado')
    try:
        db.registrar_evento(guia, estado, ubicacion=request.form.get('ubicacion') or None, usuario_id=None)
    except ValueError as e:
        flash(str(e), 'danger')
    else:
        notificaciones.notificar(guia, estado)
        flash('Estado actualizado.', 'success')
    return redirect(url_for('admin.envio_detalle', guia=guia))


# ---------------------------------------------------------------- Mapa en vivo

@bp.route('/mapa')
@login_requerido('admin')
def mapa():
    limite = datetime.now() - timedelta(hours=2)
    ubic = [{'nombre': r.nombre, 'lat': u.lat, 'lng': u.lng, 'hace_min': int((datetime.now() - u.fecha).total_seconds() // 60)}
            for r in _repartidores() for u in [sa.session.get(UbicacionRepartidor, r.id)] if u and u.fecha >= limite]
    paquetes = [{'guia': e.guia, 'lat': e.destino_lat, 'lng': e.destino_lng, 'estado': db.NOMBRE_ESTADO[e.estado],
                 'repartidor': e.repartidor.nombre if e.repartidor else None}
                for e in sa.session.scalars(select(Envio).where(Envio.estado != 'entregado', Envio.destino_lat.is_not(None))).all()]
    return render_template('admin/mapa.html', ubic=ubic, paquetes=paquetes, cedis=CEDIS)


# ---------------------------------------------------------------- Repartidores

@bp.route('/repartidores')
@login_requerido('admin')
def repartidores():
    return render_template('admin/repartidores.html', repartidores=_repartidores(activos=False), carga=_pendientes_por_repartidor())


@bp.route('/repartidores/nuevo', methods=['POST'])
@login_requerido('admin')
def nuevo_repartidor():
    nombre = request.form.get('nombre', '').strip()[:120]
    correo = request.form.get('correo', '').strip()[:160]
    if len(nombre) < 3 or '@' not in correo:
        flash('Escribe nombre y correo válidos.', 'danger')
        return redirect(url_for('admin.repartidores'))
    pw = password_temporal()
    try:
        db.crear_usuario(nombre=nombre, correo=correo, password=pw, rol=request.form.get('rol', 'repartidor'),
                         telefono=request.form.get('telefono', '').strip()[:20] or None,
                         zona=request.form.get('zona', '').strip()[:80] or None, debe_cambiar_password=True)
    except ValueError as e:
        flash(str(e), 'danger')
        return redirect(url_for('admin.repartidores'))
    flash(f'Usuario creado. Contraseña temporal para {correo}: {pw} — compártela en persona; se pedirá cambiarla al entrar.', 'success')
    return redirect(url_for('admin.repartidores'))


@bp.route('/repartidores/<int:uid>/activo', methods=['POST'])
@login_requerido('admin')
def alternar_activo(uid):
    u = sa.session.get(Usuario, uid) or abort(404)
    u.activo = not u.activo
    if not u.activo:
        for e in u.envios:
            if e.estado != 'entregado':
                e.repartidor_id, e.orden_ruta = None, None
    sa.session.commit()
    flash(f"{u.nombre} {'activado' if u.activo else 'desactivado (sus envíos pendientes quedaron sin asignar)'}.", 'success')
    return redirect(url_for('admin.repartidores'))


@bp.route('/repartidores/<int:uid>/restablecer', methods=['POST'])
@login_requerido('admin')
def restablecer(uid):
    u = sa.session.get(Usuario, uid) or abort(404)
    pw = password_temporal()
    u.fijar_password(pw)
    u.debe_cambiar_password, u.intentos_fallidos, u.bloqueado_hasta = True, 0, None
    sa.session.commit()
    flash(f'Nueva contraseña temporal para {u.correo}: {pw}', 'success')
    return redirect(url_for('admin.repartidores'))


# ---------------------------------------------------------------- Atención a clientes

@bp.route('/atencion')
@login_requerido('admin')
def atencion():
    return render_template(
        'admin/atencion.html',
        mensajes=sa.session.scalars(select(MensajeContacto).order_by(MensajeContacto.atendido, MensajeContacto.creado_en.desc()).limit(100)).all(),
        escalamientos=sa.session.scalars(select(Escalamiento).order_by(Escalamiento.creado.desc()).limit(100)).all(),
        quejas=sa.session.scalars(select(Queja).order_by(Queja.creada.desc()).limit(100)).all(),
        conversaciones=sa.session.scalars(select(Conversacion).where(Conversacion.llamadas_api > 0).order_by(Conversacion.actualizada.desc()).limit(50)).all(),
        costos=db.resumen_costos_asistente())


@bp.route('/atencion/<tipo>/<int:item_id>/atendido', methods=['POST'])
@login_requerido('admin')
def marcar_atendido(tipo, item_id):
    modelos = {'mensaje': (MensajeContacto, 'atendido', True), 'escalamiento': (Escalamiento, 'estado', 'atendido'),
               'queja': (Queja, 'estado', 'cerrada')}
    if tipo not in modelos:
        abort(404)
    modelo, campo, valor = modelos[tipo]
    obj = sa.session.get(modelo, item_id) or abort(404)
    setattr(obj, campo, valor)
    sa.session.commit()
    flash('Marcado como atendido.', 'success')
    return redirect(url_for('admin.atencion'))


@bp.route('/conversaciones/<cid>')
@login_requerido('admin')
def conversacion(cid):
    conv = sa.session.get(Conversacion, cid) or abort(404)
    mensajes = sa.session.scalars(select(ChatMensaje).where(ChatMensaje.conversacion_id == cid).order_by(ChatMensaje.id)).all()
    return render_template('admin/conversacion.html', conv=conv, mensajes=mensajes)


@bp.route('/fotos/<path:nombre>')
@login_requerido('admin')
def foto_entrega(nombre):
    """Las fotos de entrega no son públicas: solo el personal de operaciones puede verlas."""
    return send_from_directory(current_app.config['UPLOAD_FOLDER'], nombre)
