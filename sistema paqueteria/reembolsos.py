"""Reembolsos y compensaciones. TODAS las reglas de dinero viven aquí, en código (no en el prompt):

1. evaluar(): verifica que el pedido exista y sea del cliente, revisa la política del problema y
   propone opciones. No mueve dinero. Deja el caso en estado 'propuesto'.
2. ejecutar(): solo funciona en un TURNO POSTERIOR al de la propuesta (el cliente tuvo que responder),
   con una opción de las ofrecidas. Aplica tope, límite por cliente y decide: aprobado_auto o pendiente.
3. El admin aprueba o rechaza los pendientes; el cliente recibe aviso automático.

Nota sobre pagos: mientras la pasarela sea simulada, "reembolsar" registra el reembolso aprobado; la
devolución real a la tarjeta se hará cuando exista la integración con la pasarela (TODO).
"""
import secrets
from datetime import date, datetime, timedelta

import hashlib
import hmac

from sqlalchemy import func, or_, select

import db
import politicas
import rutas
from models import Caso, Conversacion, Cupon, Envio, Evento, Evidencia, PruebaEntrega, Queja, ahora, sa

PROBLEMAS = {
    'retraso': 'Retraso en la entrega',
    'danado': 'Paquete dañado',
    'entregado_no_recibido': 'Marcado como entregado pero no llegó',
    'perdido': 'Paquete perdido',
    'cobro_incorrecto': 'Cobro duplicado o incorrecto',
    'trato_repartidor': 'Queja por el trato del repartidor',
}
RESOLUCIONES = {'reembolso': 'Reembolso', 'cupon': 'Cupón de descuento', 'reenvio': 'Reenvío sin costo',
                'revision': 'Revisión por una persona del equipo'}
MAX_FALLOS_VERIFICACION = 3
CONCEPTOS_NO_ENVIO = ('fa-box-open',)  # solo el empaque no es "costo del envío" (la compensación de CO₂ sí se devuelve)
OTP_MINUTOS = 10
OTP_MAX_INTENTOS = 3


class ErrorCaso(ValueError):
    pass


# ---------------------------------------------------------------- utilidades

def _fin_plazo(envio):
    return datetime.combine(date.fromisoformat(envio.cotizacion['fecha_max']), datetime.max.time()).replace(microsecond=0)


def costo_envio(envio):
    """Lo pagado menos el empaque, sin pasar de lo pagado."""
    conceptos = envio.cotizacion.get('conceptos') or []
    importe = sum(c['importe'] for c in conceptos if c.get('icono') not in CONCEPTOS_NO_ENVIO and c['importe'] > 0)
    return round(min(importe or envio.total, envio.total), 2)


def _casos_recientes(telefono, correo=None):
    """Compensaciones automáticas del cliente en el periodo, contadas por teléfono O por correo
    (así no se brinca el límite usando otro número)."""
    desde = datetime.now() - timedelta(days=int(politicas.obtener('ventana_dias_cliente')))
    mismo_cliente = Caso.cliente_telefono == telefono
    if correo:
        mismo_cliente = or_(mismo_cliente, func.lower(Caso.cliente_correo) == correo.lower())
    return sa.session.scalar(select(func.count()).select_from(Caso).where(
        mismo_cliente, Caso.estado == 'aprobado_auto', Caso.creado >= desde))


def correo_registrado(envio, rol):
    """Correo que el cliente dejó al crear el envío (nunca el que dicta en el chat)."""
    if rol == 'remitente':
        correo = envio.remitente.get('correo')
    else:
        correo = envio.correo_destinatario or envio.destinatario.get('correo')
    return (correo or '').strip().lower() or None


def _hash_codigo(caso_folio, codigo):
    from flask import current_app
    clave = current_app.config['SECRET_KEY'].encode()
    return hmac.new(clave, f'{caso_folio}:{codigo}'.encode(), hashlib.sha256).hexdigest()


def enmascarar(correo):
    usuario, _, dominio = correo.partition('@')
    return f'{usuario[:1]}{"*" * max(2, len(usuario) - 1)}@{dominio}'


def _enviar_codigo(caso, correo):
    import notificaciones
    codigo = f'{secrets.randbelow(10 ** 6):06d}'
    caso.otp_hash = _hash_codigo(caso.folio, codigo)
    caso.otp_expira = datetime.now() + timedelta(minutes=OTP_MINUTOS)
    caso.otp_intentos = 0
    sa.session.commit()
    notificaciones.enviar_codigo_verificacion(caso.guia, correo, codigo, OTP_MINUTOS)


def _evidencia_cliente(guia):
    return sa.session.scalar(select(Evidencia).where(Evidencia.guia == guia, Evidencia.archivo.is_not(None))
                             .order_by(Evidencia.subida.desc()))


def _nueva_evidencia(guia, conversacion_id):
    ev = Evidencia(token=secrets.token_urlsafe(16), guia=guia, conversacion_id=conversacion_id)
    sa.session.add(ev)
    sa.session.commit()
    return ev.token


def evidencia_entrega(envio):
    """¿La entrega tiene evidencia clara? Foto, o firma con ubicación cerca del domicilio."""
    p = sa.session.scalar(select(PruebaEntrega).where(PruebaEntrega.guia == envio.guia))
    if not p:
        return False, {'hay_registro': False}
    distancia_m = None
    if p.lat is not None and envio.destino_lat is not None:
        distancia_m = round(rutas.haversine_km(p.lat, p.lng, envio.destino_lat, envio.destino_lng) * 1000)
    cerca = distancia_m is not None and distancia_m <= int(politicas.obtener('evidencia_distancia_m'))
    clara = bool(p.foto_archivo) or (bool(p.firma_png) and cerca)
    return clara, {'hay_registro': True, 'fecha_hora': p.fecha.strftime('%d/%m/%Y %H:%M'),
                   'recibio': p.receptor_nombre.split(' ')[0], 'relacion': p.relacion, 'tiene_foto': bool(p.foto_archivo),
                   'tiene_firma': bool(p.firma_png), 'distancia_al_domicilio_m': distancia_m}


def _transcripcion(conversacion_id):
    if not conversacion_id:
        return None
    return '\n'.join(f"[{m['fecha'][11:16]}] {m['rol']}: {m['texto']}" for m in db.chat_mensajes(conversacion_id, roles=('cliente', 'asistente', 'herramienta')))


# ---------------------------------------------------------------- 1. evaluar

def verificar(envio, ultimos4):
    ultimos4 = ''.join(ch for ch in str(ultimos4) if ch.isdigit())[-4:]
    if len(ultimos4) != 4:
        return None, None
    for rol in ('remitente', 'destinatario'):
        tel = ''.join(ch for ch in str(getattr(envio, rol).get('telefono', '')) if ch.isdigit())
        if tel and secrets.compare_digest(tel[-4:], ultimos4):
            return rol, tel
    return None, None


def _registrar_fallo(conversacion_id):
    conv = sa.session.get(Conversacion, conversacion_id) if conversacion_id else None
    if not conv:
        return 0
    conv.fallos_verificacion += 1
    sa.session.commit()
    return conv.fallos_verificacion


def evaluar(*, conversacion_id, turno, guia, ultimos4_telefono, problema, detalle, correo_contacto=''):
    if problema not in PROBLEMAS:
        raise ErrorCaso('Problema no reconocido.')
    envio = db.obtener_envio_modelo((guia or '').strip().upper().replace(' ', ''))
    conv = sa.session.get(Conversacion, conversacion_id) if conversacion_id else None
    if conv and conv.fallos_verificacion >= MAX_FALLOS_VERIFICACION:
        return {'verificado': False, 'sospecha_fraude': True,
                'nota': 'Demasiados intentos de verificación fallidos en esta conversación. No continúes con el caso; transfiere por sospecha de fraude.'}
    if not envio:
        return {'verificado': False, 'nota': 'No existe esa guía. Pide al cliente que la confirme.'}
    rol, telefono = verificar(envio, ultimos4_telefono)
    if not rol:
        fallos = _registrar_fallo(conversacion_id)
        return {'verificado': False, 'intentos_fallidos': fallos, 'sospecha_fraude': fallos >= MAX_FALLOS_VERIFICACION,
                'nota': 'Los dígitos no coinciden con el teléfono del remitente ni del destinatario.'}

    base = {'verificado': True, 'cliente_es': rol, 'guia': envio.guia, 'estado_envio': db.NOMBRE_ESTADO[envio.estado],
            'total_pagado_mxn': envio.total}
    opciones, contexto = _politica(envio, problema, conversacion_id)
    base.update(contexto)
    if not opciones:
        return base | {'aplica_compensacion': False}

    correo_reg = correo_registrado(envio, rol)
    caso = Caso(folio=db.nuevo_folio('CS', Caso), guia=envio.guia, conversacion_id=conversacion_id,
                cliente_telefono=telefono, cliente_rol=rol, cliente_correo=correo_reg,
                correo_contacto=(correo_contacto or '').strip()[:160] or None,
                problema=problema, detalle=(detalle or '').strip()[:1000], opciones=opciones, turno_propuesta=turno,
                repartidor_id=envio.repartidor_id if problema == 'trato_repartidor' else None)
    sa.session.add(caso)
    sa.session.commit()
    resultado = base | {'aplica_compensacion': True, 'folio': caso.folio, 'opciones': opciones}
    if not any(o['resolucion'] != 'revision' for o in opciones):
        return resultado | {'siguiente_paso': 'Explica la situación. Si el cliente no está de acuerdo y lo confirma, usa '
                                              'ejecutar_compensacion con resolucion "revision" y codigo_verificacion vacío.'}
    if not correo_reg:
        return resultado | {'sin_correo_registrado': True,
                            'siguiente_paso': 'Este envío no tiene un correo registrado para verificar al cliente, así que la '
                                              'compensación quedará pendiente de revisión por una persona. Explica las opciones, '
                                              'pregunta cuál prefiere y, cuando confirme, usa ejecutar_compensacion con '
                                              'codigo_verificacion vacío.'}
    _enviar_codigo(caso, correo_reg)
    return resultado | {'codigo_enviado_a': enmascarar(correo_reg), 'codigo_vigencia_minutos': OTP_MINUTOS,
                        'siguiente_paso': 'Explica las opciones y pregunta cuál prefiere. Por seguridad enviamos un código de 6 '
                                          'dígitos al correo registrado: pídele que te lo dicte. NO ejecutes nada hasta que el '
                                          'cliente elija, confirme y te dé el código en su siguiente mensaje.'}


def _opcion(resolucion, monto=0.0, cupon_pct=None, descripcion=''):
    return {'resolucion': resolucion, 'monto_mxn': round(monto, 2), 'cupon_pct': cupon_pct, 'descripcion': descripcion}


def _politica(envio, problema, conversacion_id):
    """Devuelve (opciones, contexto) según la tabla de compensaciones."""
    p = politicas.obtener
    total = envio.total
    if problema == 'retraso':
        prueba = sa.session.scalar(select(PruebaEntrega).where(PruebaEntrega.guia == envio.guia))
        referencia = prueba.fecha if prueba else datetime.now()
        horas = (referencia - _fin_plazo(envio)).total_seconds() / 3600
        if horas <= 0:
            return [], {'nota': f"El envío está dentro del plazo: la entrega estimada es a más tardar el {envio.cotizacion['fecha_max']}."}
        if horas <= int(p('retraso_umbral_horas')):
            pct = int(p('cupon_retraso_menor_pct'))
            return [_opcion('cupon', cupon_pct=pct, descripcion=f'Disculpa y cupón de {pct}% para su próximo envío')], \
                {'horas_de_retraso': round(horas)}
        pct = int(p('cupon_retraso_mayor_pct'))
        return [_opcion('cupon', cupon_pct=pct, descripcion=f'Cupón de {pct}% para su próximo envío'),
                _opcion('reembolso', costo_envio(envio), descripcion='Reembolso del costo del envío')], {'horas_de_retraso': round(horas)}

    if problema == 'danado':
        ev = _evidencia_cliente(envio.guia)
        if not ev:
            token = _nueva_evidencia(envio.guia, conversacion_id)
            return [], {'falta_foto': True, 'enlace_para_subir_foto': f'/evidencia/{token}',
                        'nota': 'Antes de ofrecer algo, pide al cliente que suba una foto del daño en ese enlace y te avise.'}
        return [_opcion('reenvio', descripcion='Reenvío sin costo'),
                _opcion('reembolso', total, descripcion='Reembolso total de lo pagado')], {'foto_recibida': True}

    if problema == 'entregado_no_recibido':
        if envio.estado != 'entregado':
            return [], {'nota': 'El sistema no lo marca como entregado; revisa el rastreo con el cliente.'}
        clara, resumen = evidencia_entrega(envio)
        if clara:
            return [_opcion('revision', descripcion='Si no está de acuerdo con la evidencia, una persona revisará el caso')], \
                {'evidencia_clara': True, 'evidencia': resumen,
                 'nota': 'La evidencia indica que sí se entregó. Explícalo con amabilidad (quién recibió, hora). Solo si el cliente no está de acuerdo, ofrece la revisión.'}
        return [_opcion('reenvio', descripcion='Reenvío sin costo'),
                _opcion('reembolso', total, descripcion='Reembolso total de lo pagado')], {'evidencia_clara': False, 'evidencia': resumen}

    if problema == 'perdido':
        if envio.estado == 'entregado':
            return [], {'nota': 'El sistema lo marca como entregado; trátalo como "entregado pero no llegó".'}
        ultimo = max(e.fecha for e in envio.eventos)
        horas_sin = (datetime.now() - ultimo).total_seconds() / 3600
        if horas_sin < int(p('perdido_horas_sin_movimiento')) or datetime.now() <= _fin_plazo(envio):
            return [], {'nota': f"El paquete sigue en proceso (último movimiento hace {round(horas_sin)} h); aún no se considera perdido.",
                        'entrega_estimada_hasta': envio.cotizacion['fecha_max']}
        return [_opcion('reenvio', descripcion='Reenvío sin costo'),
                _opcion('reembolso', total, descripcion='Reembolso total de lo pagado')], {'horas_sin_movimiento': round(horas_sin)}

    if problema == 'cobro_incorrecto':
        conceptos = envio.cotizacion.get('conceptos') or []
        diferencia = round(envio.total - sum(c['importe'] for c in conceptos), 2) if conceptos else 0.0
        duplicado = sa.session.scalar(select(Envio).where(
            Envio.guia != envio.guia, Envio.total == envio.total,
            Envio.creado_en.between(envio.creado_en - timedelta(minutes=30), envio.creado_en + timedelta(minutes=30)),
            Envio.lote_folio.is_(None)).limit(20)) if envio.lote_folio is None else None
        if duplicado and duplicado.remitente.get('telefono') == envio.remitente.get('telefono') \
                and duplicado.destinatario.get('cp') == envio.destinatario.get('cp') and duplicado.estado == 'creado':
            return [_opcion('reembolso', duplicado.total, descripcion=f'Devolución del cobro duplicado (guía {duplicado.guia})')], \
                {'cobro_duplicado': True, 'guia_duplicada': duplicado.guia}
        if diferencia > 0:
            return [_opcion('reembolso', diferencia, descripcion='Devolución de la diferencia cobrada de más')], {'diferencia_mxn': diferencia}
        return [_opcion('revision', descripcion='Si el cliente insiste, una persona revisará su estado de cuenta')], \
            {'nota': 'El cobro coincide con el desglose y no hay cobros duplicados. Explícalo; solo si el cliente insiste ofrece revisión.'}

    if problema == 'trato_repartidor':
        if not envio.repartidor_id:
            return [], {'nota': 'Este envío no tiene repartidor asignado; registra la queja general con registrar_queja.'}
        pct = int(p('cupon_trato_pct'))
        return [_opcion('cupon', cupon_pct=pct, descripcion=f'Disculpa, cupón de {pct}% y queja registrada sobre el repartidor')], {}
    return [], {}


# ---------------------------------------------------------------- 2. ejecutar

def crear_cupon(pct, caso_id=None):
    codigo = 'BAL' + secrets.token_hex(3).upper()
    cupon = Cupon(codigo=codigo, porcentaje=int(pct), caso_id=caso_id,
                  vence=datetime.now() + timedelta(days=int(politicas.obtener('cupon_vigencia_dias'))))
    sa.session.add(cupon)
    return cupon


def crear_reenvio(envio, caso):
    cot = dict(envio.cotizacion, conceptos=[{'concepto': f'Reenvío sin costo (caso {caso.folio})', 'importe': 0.0, 'icono': 'fa-rotate'}],
               total=0.0)
    guia = db.crear_envio(remitente=envio.remitente, destinatario=envio.destinatario, cotizacion=cot,
                          tipo_entrega=envio.tipo_entrega, correo_destinatario=envio.correo_destinatario,
                          origen={'lat': envio.origen_lat, 'lng': envio.origen_lng} if envio.origen_lat else None,
                          destino={'lat': envio.destino_lat, 'lng': envio.destino_lng} if envio.destino_lat else None,
                          commit=False)
    return guia


def _aplicar(caso, envio):
    """Ejecuta la resolución aprobada (cupón, reenvío o registro de reembolso)."""
    if caso.resolucion == 'cupon':
        opcion = next(o for o in caso.opciones if o['resolucion'] == 'cupon')
        sa.session.flush()
        caso.cupon_codigo = crear_cupon(opcion['cupon_pct'], caso.id).codigo
    elif caso.resolucion == 'reenvio':
        caso.reenvio_guia = crear_reenvio(envio, caso)
    if caso.problema == 'trato_repartidor':
        sa.session.add(Queja(folio=db.nuevo_folio('QJ', Queja), conversacion_id=caso.conversacion_id, guia=envio.guia,
                             tipo='mal_servicio', descripcion=caso.detalle or 'Queja por el trato del repartidor',
                             telefono=caso.cliente_telefono, repartidor_id=caso.repartidor_id))
    envio.eventos.append(Evento(estado=envio.estado, descripcion=f'Caso {caso.folio}: {RESOLUCIONES[caso.resolucion].lower()} aprobado'))


def _validar_codigo(caso, codigo):
    """Lanza ErrorCaso si el código no es válido. Máximo 3 intentos y 10 minutos; un solo uso."""
    if caso.otp_intentos >= OTP_MAX_INTENTOS:
        raise ErrorCaso('Se agotaron los intentos del código de seguridad para este caso.')
    if not caso.otp_hash or not caso.otp_expira or datetime.now() > caso.otp_expira:
        raise ErrorCaso('El código de seguridad venció. Vuelve a evaluar el caso para enviar uno nuevo al correo registrado.')
    codigo = ''.join(ch for ch in str(codigo or '') if ch.isdigit())
    if len(codigo) == 6 and hmac.compare_digest(caso.otp_hash, _hash_codigo(caso.folio, codigo)):
        caso.otp_hash = None
        return
    caso.otp_intentos += 1
    if caso.otp_intentos >= OTP_MAX_INTENTOS:
        caso.estado, caso.motivo_estado = 'bloqueado', 'código de seguridad incorrecto 3 veces'
        if caso.conversacion_id:
            conv = sa.session.get(Conversacion, caso.conversacion_id)
            conv.fallos_verificacion = max(conv.fallos_verificacion, MAX_FALLOS_VERIFICACION)
        sa.session.commit()
        raise ErrorCaso('Código incorrecto por tercera vez: el caso quedó bloqueado. Trátalo como sospecha de fraude y transfiere.')
    sa.session.commit()
    raise ErrorCaso(f'El código no coincide. Le quedan {OTP_MAX_INTENTOS - caso.otp_intentos} intentos.')


def ejecutar(*, conversacion_id, turno_actual, folio, resolucion, codigo=''):
    caso = sa.session.scalar(select(Caso).where(Caso.folio == (folio or '').upper()))
    if not caso or caso.conversacion_id != conversacion_id:
        raise ErrorCaso('No existe ese caso en esta conversación.')
    if caso.estado != 'propuesto':
        raise ErrorCaso(f'Ese caso ya fue procesado (estado: {caso.estado}).')
    if turno_actual <= caso.turno_propuesta:
        raise ErrorCaso('Todavía no tienes la confirmación del cliente: explícale las opciones y espera su respuesta.')
    opcion = next((o for o in caso.opciones if o['resolucion'] == resolucion), None)
    if not opcion:
        raise ErrorCaso(f'Esa opción no se ofreció. Opciones válidas: {", ".join(o["resolucion"] for o in caso.opciones)}.')

    motivos = []
    if resolucion != 'revision':
        if caso.cliente_correo:
            _validar_codigo(caso, codigo)
        else:
            motivos.append('no hay correo registrado para verificar al cliente')

    envio = db.obtener_envio_modelo(caso.guia)
    caso.resolucion = resolucion
    caso.monto = opcion['monto_mxn'] if resolucion == 'reembolso' else 0.0
    tope = min(float(politicas.obtener('reembolso_tope_mxn')), envio.total)
    previos = _casos_recientes(caso.cliente_telefono, caso.cliente_correo)
    limite = int(politicas.obtener('limite_casos_cliente'))
    if resolucion == 'revision':
        motivos.append('el cliente pidió revisión por una persona')
    if caso.monto > tope:
        motivos.append(f'el monto (${caso.monto:,.2f}) supera el tope automático (${tope:,.2f})')
    if previos >= limite:
        motivos.append(f'el cliente ya tiene {previos} compensaciones automáticas en el periodo (límite {limite})')

    caso.transcripcion = _transcripcion(conversacion_id)
    if motivos:
        caso.estado, caso.motivo_estado = 'pendiente', '; '.join(motivos)
        sa.session.commit()
        return {'estado': 'pendiente_de_aprobacion', 'folio': caso.folio, 'motivo': caso.motivo_estado,
                'tiempo_de_respuesta': politicas.obtener('tiempo_respuesta_pendientes'),
                'nota': 'Dile al cliente que su caso quedó en revisión con ese folio y cuándo tendrá respuesta. No prometas el resultado.'}

    caso.estado, caso.motivo_estado, caso.resuelto, caso.resuelto_por = 'aprobado_auto', 'dentro de política', ahora(), 'asistente'
    _aplicar(caso, envio)
    sa.session.commit()
    resultado = {'estado': 'aprobado', 'folio': caso.folio, 'resolucion': RESOLUCIONES[resolucion]}
    if resolucion == 'reembolso':
        resultado |= {'monto_mxn': caso.monto, 'se_refleja_en': politicas.obtener('tiempo_reflejo_reembolso'),
                      'nota': 'Se devuelve únicamente al método de pago original; no se puede enviar a otra cuenta o tarjeta.'}
    if caso.cupon_codigo:
        resultado |= {'codigo_cupon': caso.cupon_codigo, 'porcentaje': opcion['cupon_pct'],
                      'vigencia_dias': int(politicas.obtener('cupon_vigencia_dias')),
                      'nota': 'Se aplica en el paso de pago del cotizador.'}
    if caso.reenvio_guia:
        resultado |= {'guia_reenvio': caso.reenvio_guia, 'nota': 'El reenvío ya está registrado con esa nueva guía.'}
    return resultado


# ---------------------------------------------------------------- 3. admin

def resolver_pendiente(caso, aprobar, usuario, nota=''):
    if caso.estado != 'pendiente':
        raise ErrorCaso('Ese caso no está pendiente.')
    envio = db.obtener_envio_modelo(caso.guia)
    caso.resuelto, caso.resuelto_por, caso.nota_admin = ahora(), usuario, (nota or '').strip()[:1000] or None
    if aprobar:
        caso.estado = 'aprobado_admin'
        if caso.resolucion != 'revision':
            _aplicar(caso, envio)
    else:
        caso.estado = 'rechazado'
    sa.session.commit()
    return caso


# ---------------------------------------------------------------- cupones en el cotizador

def validar_cupon(codigo):
    cupon = sa.session.get(Cupon, (codigo or '').strip().upper())
    if not cupon:
        return None, 'Ese cupón no existe.'
    if cupon.usado:
        return None, 'Ese cupón ya se usó.'
    if cupon.vence < datetime.now():
        return None, 'Ese cupón ya venció.'
    return cupon, None


def aplicar_cupon_a_cotizacion(cot, cupon):
    """Devuelve una copia de la cotización con el descuento del cupón."""
    cot = dict(cot, conceptos=[c for c in cot['conceptos'] if c.get('icono') != 'fa-ticket'])
    subtotal = round(sum(c['importe'] for c in cot['conceptos']), 2)
    descuento = round(-subtotal * cupon.porcentaje / 100, 2)
    cot['conceptos'].append({'concepto': f'Cupón {cupon.codigo} ({cupon.porcentaje}%)', 'importe': descuento, 'icono': 'fa-ticket'})
    cot['total'] = round(subtotal + descuento, 2)
    return cot
