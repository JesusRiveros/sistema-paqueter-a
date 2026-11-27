"""Llamadas telefónicas con Twilio: el cliente llama y Lula contesta por voz.

Flujo: Twilio recibe la llamada → POST /voz/twilio/entrante → respondemos TwiML con un saludo y un
<Gather input="speech"> → Twilio transcribe lo que dice el cliente → POST /voz/twilio/turno con
SpeechResult → Lula responde (misma lógica y herramientas que el chat) → <Say> + nuevo <Gather>.

Configuración (.env):
  TWILIO_AUTH_TOKEN      token de la cuenta (para validar que las peticiones vienen de Twilio)
  TWILIO_NUMERO_HUMANO   número en formato +52... al que se transfiere si hay que pasar con una persona
  TWILIO_VOZ             voz para <Say> (por defecto Polly.Mia-Neural, español de México)
  PUBLIC_BASE_URL        URL pública del sitio (Twilio firma la URL exacta que llama)
En la consola de Twilio, el número debe apuntar ("A call comes in") a  {PUBLIC_BASE_URL}/voz/twilio/entrante
"""
import base64
import hashlib
import hmac
import logging
import os
from xml.sax.saxutils import escape

from flask import Blueprint, Response, current_app, request, url_for

import asistente
import db

log = logging.getLogger(__name__)
bp = Blueprint('voz_twilio', __name__, url_prefix='/voz/twilio')

SALUDO = 'Hola, gracias por llamar a Balula Express. Soy Lula. ¿En qué te puedo ayudar?'
MAX_SILENCIOS = 2


def _config():
    return {'token': os.getenv('TWILIO_AUTH_TOKEN', ''), 'humano': os.getenv('TWILIO_NUMERO_HUMANO', ''),
            'voz': os.getenv('TWILIO_VOZ', 'Polly.Mia-Neural')}


def firma_esperada(url, parametros, token):
    """Algoritmo de Twilio: HMAC-SHA1 de la URL + parámetros POST ordenados, en Base64."""
    datos = url + ''.join(f'{k}{parametros[k]}' for k in sorted(parametros))
    return base64.b64encode(hmac.new(token.encode(), datos.encode('utf-8'), hashlib.sha1).digest()).decode()


def _url_solicitada():
    base = current_app.config['PUBLIC_BASE_URL'] or request.url_root.rstrip('/')
    return base + request.full_path.rstrip('?')


def _peticion_valida():
    token = _config()['token']
    if not token:
        return False
    firma = request.headers.get('X-Twilio-Signature', '')
    esperada = firma_esperada(_url_solicitada(), request.form.to_dict(), token)
    return hmac.compare_digest(firma, esperada)


def _twiml(*partes):
    return Response('<?xml version="1.0" encoding="UTF-8"?><Response>' + ''.join(partes) + '</Response>',
                    mimetype='text/xml')


def _decir(texto):
    return f'<Say voice="{escape(_config()["voz"])}" language="es-MX">{escape(texto)}</Say>'


def _escuchar(silencios=0):
    accion = escape(url_for('voz_twilio.turno', silencios=silencios))
    return (f'<Gather input="speech" language="es-MX" speechTimeout="auto" actionOnEmptyResult="true" '
            f'method="POST" action="{accion}"/>')


def _transferir(mensaje):
    humano = _config()['humano']
    if humano:
        return _twiml(_decir(mensaje + ' Te comunico con una persona del equipo, un momento por favor.'),
                      f'<Dial>{escape(humano)}</Dial>')
    contacto = current_app.config['CONTACTO']
    return _twiml(_decir(f"{mensaje} Una persona del equipo te contactará. También puedes llamar al {contacto['telefono']}, "
                         f"{contacto['horario']}. Gracias por llamar."), '<Hangup/>')


def _conversacion(call_sid):
    """Una conversación por llamada (CallSid = 'CA' + 32 caracteres)."""
    cid = 'tw' + ''.join(ch for ch in call_sid if ch.isalnum())[2:32]
    if not db.obtener_conversacion(cid):
        db.crear_conversacion(cid, 'telefono')
    return cid


@bp.route('/entrante', methods=['POST'])
def entrante():
    if not _peticion_valida():
        return Response('Firma inválida', status=403)
    _conversacion(request.form.get('CallSid', ''))
    return _twiml(_decir(SALUDO), _escuchar())


@bp.route('/turno', methods=['POST'])
def turno():
    if not _peticion_valida():
        return Response('Firma inválida', status=403)
    cid = _conversacion(request.form.get('CallSid', ''))
    dicho = (request.form.get('SpeechResult') or '').strip()
    silencios = request.args.get('silencios', 0, type=int)
    if not dicho:
        if silencios >= MAX_SILENCIOS:
            return _twiml(_decir('No te escucho bien. Puedes volver a llamar cuando gustes. ¡Hasta luego!'), '<Hangup/>')
        return _twiml(_decir('Perdón, no te escuché. ¿Me lo repites?'), _escuchar(silencios + 1))
    try:
        r = asistente.responder(cid, dicho[:1000])
    except asistente.AsistenteNoConfigurado:
        return _transferir('En este momento no puedo atenderte.')
    if r['escalado']:
        return _transferir(r['texto'])
    return _twiml(_decir(r['texto']), _escuchar())
