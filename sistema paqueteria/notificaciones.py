"""Avisos por correo al destinatario en cada cambio de estado.

Siempre se registran en la tabla `notificaciones`. Si SMTP_HOST está configurado en .env, además se
envían. Sin SMTP quedan como 'registrado' (útil en desarrollo para ver qué se habría enviado).
TODO (WhatsApp): agregar un canal con la API de WhatsApp Business o Twilio cuando haya cuenta.
"""
import logging
import os
import smtplib
from email.message import EmailMessage

from flask import current_app, url_for

import db
from models import Notificacion

log = logging.getLogger(__name__)

PLANTILLAS = {
    'creado': ('Tu paquete {guia} ya está registrado',
               'Hola {nombre}:\n\n{remitente} te envió un paquete con Balula Express (guía {guia}).\n'
               'Llegará entre el {fecha_min} y el {fecha_max}.\n\n'
               'Tu código de entrega es: {codigo}\nDíselo al repartidor al recibir el paquete; sin él no se puede entregar.\n\n'
               'Sigue tu envío o cambia el día de entrega aquí: {url}\n'),
    'recolectado': ('Recolectamos tu paquete {guia}', 'Hola {nombre}: ya recolectamos tu paquete. Síguelo aquí: {url}\n'),
    'en_transito': ('Tu paquete {guia} va en camino', 'Hola {nombre}: tu paquete va en camino a tu ciudad. Síguelo aquí: {url}\n'),
    'en_reparto': ('Tu paquete {guia} llega hoy',
                   'Hola {nombre}: tu paquete salió a reparto y llega hoy. Ten a la mano tu código de entrega ({codigo}).\n'
                   'Puedes ver al repartidor en el mapa: {url}\n'),
    'entregado': ('Entregamos tu paquete {guia}', 'Hola {nombre}: tu paquete fue entregado. ¡Gracias por elegir Balula!\n'),
    'incidencia': ('Hubo un detalle con tu paquete {guia}',
                   'Hola {nombre}: {detalle}\nPuedes elegir otro día o dónde dejarlo aquí: {url}\n'),
}


def _enviar_smtp(para, asunto, cuerpo):
    host = os.getenv('SMTP_HOST')
    if not host:
        return 'registrado'
    msg = EmailMessage()
    msg['From'] = os.getenv('SMTP_REMITENTE', 'Balula Express <no-responder@localhost>')
    msg['To'] = para
    msg['Subject'] = asunto
    msg.set_content(cuerpo)
    try:
        with smtplib.SMTP(host, int(os.getenv('SMTP_PUERTO', '587')), timeout=10) as s:
            s.starttls()
            if os.getenv('SMTP_USUARIO'):
                s.login(os.getenv('SMTP_USUARIO'), os.getenv('SMTP_PASSWORD', ''))
            s.send_message(msg)
        return 'enviado'
    except (OSError, smtplib.SMTPException) as e:
        log.error('No se pudo enviar correo a %s: %s', para, e)
        return 'error'


def notificar(guia, tipo, detalle=''):
    envio = db.obtener_envio_modelo(guia)
    if not envio or not envio.correo_destinatario or tipo not in PLANTILLAS:
        return None
    base = current_app.config['PUBLIC_BASE_URL'] or ''
    url = (base + url_for('rastreo', guia=guia)) if base else url_for('rastreo', guia=guia, _external=True)
    cot = envio.cotizacion
    datos = {'guia': guia, 'nombre': envio.destinatario.get('nombre', '').split(' ')[0] or 'Hola',
             'remitente': envio.remitente.get('nombre', 'Alguien'), 'codigo': envio.codigo_entrega,
             'fecha_min': cot.get('fecha_min', ''), 'fecha_max': cot.get('fecha_max', ''), 'url': url,
             'detalle': detalle}
    asunto, cuerpo = (t.format(**datos) for t in PLANTILLAS[tipo])
    estado = _enviar_smtp(envio.correo_destinatario, asunto, cuerpo)
    db.sa.session.add(Notificacion(guia=guia, para=envio.correo_destinatario, asunto=asunto, cuerpo=cuerpo, estado=estado))
    db.sa.session.commit()
    return estado


def notificar_caso(caso):
    """Avisa al cliente la decisión sobre un caso pendiente (aprobado o rechazado)."""
    import politicas
    import reembolsos
    envio = db.obtener_envio_modelo(caso.guia)
    para = caso.correo_contacto or (envio.correo_destinatario if caso.cliente_rol == 'destinatario' else None)
    if not para:
        return None
    nombre = getattr(envio, caso.cliente_rol, {}).get('nombre', '').split(' ')[0] or 'Hola'
    if caso.estado == 'aprobado_admin':
        asunto = f'Tu caso {caso.folio} fue aprobado'
        detalle = {
            'reembolso': f"Aprobamos el reembolso de ${caso.monto:,.2f} MXN. Se reflejará en {politicas.obtener('tiempo_reflejo_reembolso')} en tu mismo método de pago.",
            'cupon': f'Tu cupón es {caso.cupon_codigo}. Úsalo en el paso de pago de tu próximo envío.',
            'reenvio': f'Registramos el reenvío sin costo con la nueva guía {caso.reenvio_guia}.',
            'revision': 'Revisamos tu caso y le daremos seguimiento contigo.',
        }[caso.resolucion]
    else:
        asunto = f'Respuesta sobre tu caso {caso.folio}'
        detalle = 'Revisamos tu caso y en esta ocasión no procede la solicitud.'
    if caso.nota_admin:
        detalle += f'\n\nComentario del equipo: {caso.nota_admin}'
    cuerpo = (f'Hola {nombre}:\n\n{detalle}\n\nGuía: {caso.guia} · {reembolsos.PROBLEMAS[caso.problema]}\n'
              'Si tienes dudas, responde este correo o escríbenos en el chat del sitio.\n')
    estado = _enviar_smtp(para, asunto, cuerpo)
    db.sa.session.add(Notificacion(guia=caso.guia, para=para, asunto=asunto, cuerpo=cuerpo, estado=estado))
    db.sa.session.commit()
    return estado


def enviar_enlace_evidencia(guia, para, ruta):
    """Envía al cliente el enlace para subir la foto del daño (útil en llamadas telefónicas)."""
    base = current_app.config['PUBLIC_BASE_URL'] or ''
    url = base + ruta if base else url_for('rastreo', _external=True).rsplit('/rastreo', 1)[0] + ruta
    asunto = f'Sube la foto de tu paquete {guia}'
    cuerpo = (f'Hola:\n\nPara ayudarte con tu paquete {guia}, sube una foto donde se vea el daño en este enlace:\n{url}\n\n'
              'Después avísanos en el chat o por teléfono. El enlace vence en 7 días.\n')
    estado = _enviar_smtp(para, asunto, cuerpo)
    db.sa.session.add(Notificacion(guia=guia, para=para, asunto=asunto, cuerpo=cuerpo, estado=estado))
    db.sa.session.commit()
    return estado
