"""Funciones para el cliente final: rastreo en vivo, reprogramar entrega y certificado verde."""
from datetime import date, datetime, timedelta

from flask import Blueprint, abort, flash, jsonify, redirect, render_template, request, session, url_for
from sqlalchemy import select

import db
import notificaciones
import rutas
import tarifas
from models import Envio, UbicacionRepartidor, sa

bp = Blueprint('clientes', __name__)

MINUTOS_UBICACION_FRESCA = 15
VELOCIDAD_URBANA_KMH = 22        # promedio en ciudad, incluye paradas
MINUTOS_POR_PARADA = 6
INSTRUCCIONES = {'domicilio': 'En el domicilio', 'vecino': 'Con un vecino', 'sucursal': 'Guardarlo en sucursal para recoger'}
MAX_INTENTOS_VERIFICACION = 5


# ---------------------------------------------------------------- Rastreo en vivo

def estado_en_vivo(envio):
    """Posición del repartidor y tiempo estimado. No revela la dirección del destinatario."""
    if envio.estado != 'en_reparto' or not envio.repartidor_id:
        return None
    ub = sa.session.get(UbicacionRepartidor, envio.repartidor_id)
    if not ub or datetime.now() - ub.fecha > timedelta(minutes=MINUTOS_UBICACION_FRESCA):
        return None
    pendientes = sa.session.scalars(select(Envio).where(
        Envio.repartidor_id == envio.repartidor_id, Envio.estado == 'en_reparto', Envio.guia != envio.guia)).all()
    antes = [e for e in pendientes if e.orden_ruta is not None and envio.orden_ruta is not None and e.orden_ruta < envio.orden_ruta]
    km = None
    if envio.destino_lat is not None:
        puntos = [(e.destino_lat, e.destino_lng) for e in sorted(antes, key=lambda e: e.orden_ruta) if e.destino_lat is not None]
        km = rutas.longitud_ruta((ub.lat, ub.lng), puntos + [(envio.destino_lat, envio.destino_lng)])
    minutos = None
    if km is not None:
        minutos = round(km / VELOCIDAD_URBANA_KMH * 60 * rutas.factor_trafico() + MINUTOS_POR_PARADA * len(antes))
    return {'lat': ub.lat, 'lng': ub.lng, 'hace_min': int((datetime.now() - ub.fecha).total_seconds() // 60),
            'paradas_antes': len(antes), 'km': round(km, 1) if km is not None else None, 'minutos': minutos,
            'repartidor': envio.repartidor.nombre.split(' ')[0] if envio.repartidor else None}


@bp.route('/api/rastreo/<guia>/vivo')
def vivo(guia):
    envio = db.obtener_envio_modelo(guia) or abort(404)
    return jsonify(ok=True, vivo=estado_en_vivo(envio), estado=envio.estado)


# ---------------------------------------------------------------- Reprogramar entrega

def puede_reprogramar(envio):
    return envio.estado in ('creado', 'recolectado', 'en_transito')


def opciones_fecha(envio):
    """Próximos 6 días hábiles a partir de la fecha mínima de entrega (y nunca antes de mañana)."""
    inicio = max(date.fromisoformat(envio.cotizacion['fecha_min']), date.today() + timedelta(days=1))
    opciones, d = [], inicio
    while len(opciones) < 6:
        if d.weekday() < 5:
            opciones.append(d.isoformat())
        d += timedelta(days=1)
    return opciones


@bp.route('/rastreo/<guia>/preferencias', methods=['POST'])
def preferencias(guia):
    envio = db.obtener_envio_modelo(guia) or abort(404)
    destino = url_for('rastreo', guia=envio.guia, _anchor='reprogramar')
    if not puede_reprogramar(envio):
        flash('Este envío ya salió a reparto o fue entregado; ya no se puede cambiar.', 'warning')
        return redirect(destino)

    clave = f'verif_{envio.guia}'
    intentos = session.get(clave, 0)
    if intentos >= MAX_INTENTOS_VERIFICACION:
        flash('Demasiados intentos. Comunícate con atención a clientes.', 'danger')
        return redirect(destino)
    telefono = envio.destinatario.get('telefono', '')
    if not telefono or request.form.get('verificacion', '').strip() != telefono[-4:]:
        session[clave] = intentos + 1
        flash('Los últimos 4 dígitos del teléfono del destinatario no coinciden.', 'danger')
        return redirect(destino)

    fecha = request.form.get('fecha', '')
    instruccion = request.form.get('instruccion', 'domicilio')
    nota = request.form.get('nota', '').strip()[:300]
    if fecha not in opciones_fecha(envio) or instruccion not in INSTRUCCIONES:
        flash('Elige una fecha y una opción válidas.', 'danger')
        return redirect(destino)
    if instruccion == 'vecino' and len(nota) < 5:
        flash('Indica el nombre y el número de casa del vecino.', 'danger')
        return redirect(destino)

    envio.fecha_preferida, envio.instruccion_entrega, envio.nota_entrega = fecha, instruccion, nota or None
    dia = f"{['lunes', 'martes', 'miércoles', 'jueves', 'viernes'][date.fromisoformat(fecha).weekday()]} {date.fromisoformat(fecha).day}"
    envio.eventos.append(db.Evento(estado=envio.estado, descripcion=f'El destinatario pidió entregar el {dia} · {INSTRUCCIONES[instruccion].lower()}'))
    sa.session.commit()
    session.pop(clave, None)
    flash('¡Listo! Guardamos tu preferencia de entrega.', 'success')
    return redirect(destino)


# ---------------------------------------------------------------- Certificado verde

ARBOL_KG_CO2_ANUAL = 21  # absorción aproximada de un árbol adulto por año (referencia común)


@bp.route('/certificado-verde/<guia>')
def certificado(guia):
    envio = db.obtener_envio_modelo(guia) or abort(404)
    if not envio.cotizacion.get('compensar_co2'):
        abort(404)
    return render_template('sitio/certificado.html', envio=envio, kg=envio.cotizacion.get('co2_kg', 0),
                           aportacion=tarifas.COSTO_CO2, arbol_kg=ARBOL_KG_CO2_ANUAL, items=None)


def contexto_rastreo(guia):
    """Datos extra para la página de rastreo."""
    envio = db.obtener_envio_modelo(guia)
    if not envio:
        return {}
    return {'vivo': estado_en_vivo(envio), 'puede_reprogramar': puede_reprogramar(envio),
            'opciones_fecha': opciones_fecha(envio) if puede_reprogramar(envio) else [],
            'instrucciones': INSTRUCCIONES, 'preferencia': {'fecha': envio.fecha_preferida, 'instruccion': envio.instruccion_entrega},
            'compensado': bool(envio.cotizacion.get('compensar_co2'))}


# ---------------------------------------------------------------- Evidencia (foto del daño)

EXT_FOTO = {'image/jpeg': '.jpg', 'image/png': '.png', 'image/webp': '.webp'}


@bp.route('/evidencia/<token>', methods=['GET', 'POST'])
def evidencia(token):
    import os
    import secrets as _secrets
    from flask import current_app
    from models import Evidencia
    ev = sa.session.get(Evidencia, token) or abort(404)
    if datetime.now() - ev.creada > timedelta(days=7):
        abort(404)
    if request.method == 'POST':
        foto = request.files.get('foto')
        ext = EXT_FOTO.get(foto.mimetype) if foto and foto.filename else None
        if not ext:
            flash('Sube una foto en formato JPG, PNG o WebP.', 'danger')
            return redirect(url_for('clientes.evidencia', token=token))
        carpeta = os.path.join(current_app.config['UPLOAD_FOLDER'], 'evidencias')
        os.makedirs(carpeta, exist_ok=True)
        nombre = f'{ev.guia}-{_secrets.token_hex(6)}{ext}'
        foto.save(os.path.join(carpeta, nombre))
        ev.archivo, ev.subida = f'evidencias/{nombre}', datetime.now()
        sa.session.commit()
        flash('¡Gracias! Recibimos tu foto. Regresa al chat y avísale a Lula que ya la subiste.', 'success')
        return redirect(url_for('clientes.evidencia', token=token))
    return render_template('sitio/evidencia.html', ev=ev)
