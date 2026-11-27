"""Pagos con Stripe (modo de prueba) y reembolsos reales sobre el pago original.

Flujo de cobro:
  1. El cliente da "Pagar" → creamos un Pedido (con todos los datos del envío) y una sesión de
     Stripe Checkout en MXN → lo mandamos a la página de pago de Stripe.
  2. Stripe confirma el pago por dos vías (las dos son idempotentes):
     - el webhook firmado `checkout.session.completed` (la fuente de verdad), y
     - la redirección de regreso (`/app/pago/exito`), que consulta la sesión a Stripe.
  3. Con el pago confirmado se crean el o los envíos con el ID del pago (payment_intent).
Reembolsos: Stripe Refunds sobre el payment_intent original, nunca a otra cuenta o tarjeta.

Variables (.env): STRIPE_SECRET_KEY (sk_test_...), STRIPE_PUBLISHABLE_KEY (pk_test_...),
STRIPE_WEBHOOK_SECRET (whsec_...). Con llaves sk_live_/pk_live_ el servidor NO arranca.
"""
import logging
import os
import uuid
from datetime import datetime

import stripe
from flask import Blueprint, abort, current_app, flash, jsonify, redirect, render_template, request, session, url_for

import db
from models import Caso, Cupon, Pedido, sa

log = logging.getLogger(__name__)
bp = Blueprint('pagos', __name__)
_cliente = None


class LlaveEnVivo(RuntimeError):
    pass


# ---------------------------------------------------------------- configuración

def validar_llaves():
    """Se ejecuta al arrancar: este proyecto solo acepta llaves de PRUEBA."""
    for var in ('STRIPE_SECRET_KEY', 'STRIPE_PUBLISHABLE_KEY'):
        valor = os.getenv(var, '')
        if valor.startswith(('sk_live_', 'pk_live_', 'rk_live_')):
            raise LlaveEnVivo(f'{var} es una llave EN VIVO de Stripe. Este proyecto solo acepta llaves de prueba '
                              '(sk_test_/pk_test_). El servidor no arrancará hasta que la cambies.')


def modo():
    if os.getenv('STRIPE_SECRET_KEY', '').startswith('sk_test_') or _cliente is not None:
        return 'stripe'
    return 'simulado' if current_app.config['PAGOS_SIMULADOS'] else 'no_disponible'


def cliente():
    global _cliente
    if _cliente is None:
        _cliente = stripe.StripeClient(os.getenv('STRIPE_SECRET_KEY', ''), max_network_retries=2)
    return _cliente


def asignar_cliente(c):
    """Para pruebas: reemplaza el cliente de Stripe por uno simulado."""
    global _cliente
    _cliente = c


def _centavos(monto):
    return int(round(float(monto) * 100))


def _base_url():
    return current_app.config['PUBLIC_BASE_URL'] or request.url_root.rstrip('/')


# ---------------------------------------------------------------- cobro

def iniciar_checkout(*, tipo, datos, total, correo, descripcion, url_cancelar):
    """Crea el Pedido y la sesión de Stripe Checkout; devuelve la URL de pago."""
    pedido = Pedido(id=uuid.uuid4().hex, tipo=tipo, datos=datos, total=round(total, 2), correo=correo)
    sa.session.add(pedido)
    sa.session.commit()
    params = {
        'mode': 'payment',
        'currency': 'mxn',
        'locale': 'es-419',
        'line_items': [{'quantity': 1, 'price_data': {'currency': 'mxn', 'unit_amount': _centavos(total),
                                                      'product_data': {'name': descripcion[:120]}}}],
        'client_reference_id': pedido.id,
        'metadata': {'pedido_id': pedido.id, 'tipo': tipo},
        'payment_intent_data': {'metadata': {'pedido_id': pedido.id}, 'description': descripcion[:120]},
        'success_url': f"{_base_url()}{url_for('pagos.exito')}?session_id={{CHECKOUT_SESSION_ID}}",
        'cancel_url': f'{_base_url()}{url_cancelar}',
    }
    if correo:
        params['customer_email'] = correo
    sesion = cliente().v1.checkout.sessions.create(params=params, options={'idempotency_key': f'pedido-{pedido.id}'})
    pedido.stripe_session_id = sesion.id
    sa.session.commit()
    session['pedido_id'] = pedido.id
    return sesion.url


def completar_pedido(pedido, payment_intent):
    """Crea los envíos de un pedido pagado. Idempotente: si ya se completó, no duplica."""
    if pedido.estado == 'pagado':
        return pedido.guias or []
    import notificaciones
    guias = []
    if pedido.tipo == 'envio':
        d = pedido.datos
        c, contactos = d['cotizacion'], d['contactos']
        guia = db.crear_envio(remitente=contactos['remitente'], destinatario=contactos['destinatario'],
                              cotizacion=c['detalle'], tipo_entrega=c['tipo_entrega'],
                              correo_destinatario=contactos['destinatario'].get('correo'),
                              origen=c.get('origen_geo'), destino=c.get('destino_geo'), commit=False)
        guias.append(guia)
        if d.get('cupon'):
            cupon = sa.session.get(Cupon, d['cupon'])
            if cupon and not cupon.usado:
                cupon.usado, cupon.usado_en_guia = datetime.now(), guia
    else:
        import empresas
        lote = sa.session.get(empresas.Lote, pedido.datos['lote_folio'])
        guias = empresas.generar_envios_lote(lote, commit=False)
    for g in guias:
        e = db.obtener_envio_modelo(g)
        e.metodo_pago, e.stripe_payment_intent = 'stripe', payment_intent
    pedido.estado, pedido.pagado, pedido.guias, pedido.stripe_payment_intent = 'pagado', datetime.now(), guias, payment_intent
    sa.session.commit()
    for g in guias:
        notificaciones.notificar(g, 'creado')
    return guias


def _pedido_de_sesion(sesion):
    # Los objetos de Stripe no son dict (no tienen .get); se accede por clave.
    metadata = getattr(sesion, 'metadata', None)
    pid = (metadata['pedido_id'] if metadata and 'pedido_id' in metadata else None) \
        or getattr(sesion, 'client_reference_id', None)
    return sa.session.get(Pedido, pid) if pid else None


@bp.route('/app/pago/exito')
def exito():
    """Regreso desde Stripe. Confirma con la API (no confiamos en el parámetro de la URL)."""
    session_id = request.args.get('session_id', '')
    pedido = sa.session.get(Pedido, session.get('pedido_id', '')) if session.get('pedido_id') else None
    if not pedido or pedido.stripe_session_id != session_id:
        abort(404)
    if pedido.estado != 'pagado':
        sesion = cliente().v1.checkout.sessions.retrieve(session_id)
        if getattr(sesion, 'payment_status', '') != 'paid':
            return render_template('cotizador/procesando.html'), 202
        completar_pedido(pedido, getattr(sesion, 'payment_intent', None))
    session['mis_guias'] = (session.get('mis_guias', []) + (pedido.guias or []))[-200:]
    session.pop('pedido_id', None)
    if pedido.tipo == 'lote':
        flash(f'¡Pago recibido! Se generaron {len(pedido.guias or [])} guías.', 'success')
        return redirect(url_for('empresas.lote', folio=pedido.datos['lote_folio']))
    for clave in ('cotizacion', 'contactos', 'formulario', 'cupon'):
        session.pop(clave, None)
    return redirect(url_for('confirmado', guia=pedido.guias[0]))


# ---------------------------------------------------------------- webhook

def verificar_evento(payload, firma):
    secreto = os.getenv('STRIPE_WEBHOOK_SECRET', '')
    if not secreto:
        raise stripe.SignatureVerificationError('Falta STRIPE_WEBHOOK_SECRET', firma)
    return stripe.StripeClient('sk_test_verificacion').construct_event(payload, firma, secreto)


@bp.route('/webhooks/stripe', methods=['POST'])
def webhook():
    try:
        evento = verificar_evento(request.get_data(), request.headers.get('Stripe-Signature', ''))
    except (stripe.SignatureVerificationError, ValueError) as e:
        log.warning('Webhook de Stripe rechazado: %s', e)
        return jsonify(ok=False, error='firma inválida'), 400
    obj = evento.data.object
    if evento.type in ('checkout.session.completed', 'checkout.session.async_payment_succeeded'):
        pedido = _pedido_de_sesion(obj)
        if pedido and getattr(obj, 'payment_status', '') == 'paid':
            completar_pedido(pedido, getattr(obj, 'payment_intent', None))
    elif evento.type == 'checkout.session.expired':
        pedido = _pedido_de_sesion(obj)
        if pedido and pedido.estado == 'pendiente':
            pedido.estado = 'expirado'
            sa.session.commit()
    elif evento.type in ('refund.created', 'refund.updated', 'refund.failed'):
        caso = sa.session.query(Caso).filter_by(stripe_refund_id=obj.id).first()
        if caso:
            caso.reembolso_estado = obj.status
            caso.reembolso_error = getattr(obj, 'failure_reason', None)
            sa.session.commit()
    return jsonify(ok=True)


# ---------------------------------------------------------------- reembolsos

def ejecutar_reembolso(caso, reintento=False):
    """Reembolsa con Stripe sobre el pago ORIGINAL del envío. No hace commit.

    La clave de idempotencia evita reembolsos dobles si la petición se repite; un reintento manual
    (tras un error) usa una clave nueva porque Stripe recuerda la respuesta de cada clave."""
    envio = db.obtener_envio_modelo(caso.reembolso_guia or caso.guia)
    if not envio or not envio.stripe_payment_intent or modo() != 'stripe':
        caso.reembolso_estado = 'manual'
        caso.reembolso_error = 'El envío no se pagó con Stripe (pago simulado): la devolución se hace manualmente.'
        return caso
    try:
        refund = cliente().v1.refunds.create(
            params={'payment_intent': envio.stripe_payment_intent, 'amount': _centavos(caso.monto),
                    'reason': 'requested_by_customer', 'metadata': {'caso': caso.folio, 'guia': envio.guia}},
            options={'idempotency_key': f'reembolso-{caso.folio}' + (f'-{uuid.uuid4().hex[:8]}' if reintento else '')})
        caso.stripe_refund_id, caso.reembolso_estado, caso.reembolso_error = refund.id, refund.status, None
    except stripe.StripeError as e:
        log.error('Stripe rechazó el reembolso del caso %s: %s', caso.folio, e)
        caso.reembolso_estado, caso.reembolso_error = 'error', getattr(e, 'user_message', None) or str(e)
    return caso


def actualizar_estado_reembolso(caso):
    if not caso.stripe_refund_id:
        return caso
    refund = cliente().v1.refunds.retrieve(caso.stripe_refund_id)
    caso.reembolso_estado = refund.status
    caso.reembolso_error = getattr(refund, 'failure_reason', None)
    return caso
