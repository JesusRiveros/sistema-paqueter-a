"""Pagos con Stripe: checkout, webhook firmado y reembolsos. Stripe se simula: no hay llamadas reales."""
import hashlib
import hmac
import io
import json
import os
import subprocess
import sys
import time
from datetime import date, timedelta
from types import SimpleNamespace as NS

import pytest
import stripe
from sqlalchemy import select

import db
import pagos
import reembolsos as rb
from models import Caso, Cupon, Envio, Lote, Pedido, sa
from test_flujo import hasta_pago

SECRETO_WH = 'whsec_prueba_local'


class StripeFalso:
    """Imita stripe.StripeClient().v1 con lo que usa la app."""

    def __init__(self):
        self.sesiones, self.reembolsos, self.llamadas = {}, {}, []
        self.pagado = True
        self.error_reembolso = None
        ns = NS
        self.v1 = ns(checkout=ns(sessions=ns(create=self._crear_sesion, retrieve=self._obtener_sesion)),
                     refunds=ns(create=self._crear_reembolso, retrieve=self._obtener_reembolso))

    def _crear_sesion(self, params=None, options=None):
        self.llamadas.append(('session.create', params, options))
        sid = f'cs_test_{len(self.sesiones) + 1}'
        self.sesiones[sid] = params
        return NS(id=sid, url=f'https://checkout.stripe.com/c/pay/{sid}')

    def _obtener_sesion(self, sid, params=None, options=None):
        p = self.sesiones[sid]
        return NS(id=sid, payment_status='paid' if self.pagado else 'unpaid', payment_intent=f'pi_test_{sid[-1]}',
                  client_reference_id=p['client_reference_id'], metadata=p['metadata'])

    def _crear_reembolso(self, params=None, options=None):
        self.llamadas.append(('refund.create', params, options))
        if self.error_reembolso:
            raise self.error_reembolso
        rid = f're_test_{len(self.reembolsos) + 1}'
        self.reembolsos[rid] = 'succeeded'
        return NS(id=rid, status='succeeded')

    def _obtener_reembolso(self, rid, params=None, options=None):
        return NS(id=rid, status=self.reembolsos[rid], failure_reason=None)


@pytest.fixture
def stripe_falso(app, monkeypatch):
    falso = StripeFalso()
    pagos.asignar_cliente(falso)
    monkeypatch.setenv('STRIPE_WEBHOOK_SECRET', SECRETO_WH)
    yield falso
    pagos.asignar_cliente(None)


def evento_firmado(tipo, objeto, secreto=SECRETO_WH):
    payload = json.dumps({'id': 'evt_test', 'object': 'event', 'type': tipo, 'data': {'object': objeto}})
    t = int(time.time())
    firma = hmac.new(secreto.encode(), f'{t}.{payload}'.encode(), hashlib.sha256).hexdigest()
    return payload, f't={t},v1={firma}'


def pagar_con_stripe(client, csrf):
    hasta_pago(client, csrf)
    return client.post('/app/procesar_pago', data={'_csrf': csrf})


# ---------------------------------------------------------------- llaves

@pytest.mark.parametrize('variable, valor', [('STRIPE_SECRET_KEY', 'sk_live_123'), ('STRIPE_PUBLISHABLE_KEY', 'pk_live_123')])
def test_llave_en_vivo_impide_arrancar(variable, valor, tmp_path):
    env = dict(os.environ, **{variable: valor}, DATABASE_URL='sqlite:///' + str(tmp_path / 'x.db').replace(os.sep, '/'))
    carpeta = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    r = subprocess.run([sys.executable, '-c', 'import app'], cwd=carpeta, env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode != 0 and 'EN VIVO' in r.stderr


def test_sin_llaves_usa_pago_simulado(client, csrf, app):
    with app.app_context(), app.test_request_context():
        assert pagos.modo() == 'simulado'


# ---------------------------------------------------------------- checkout

def test_checkout_en_mxn_con_aviso_de_prueba(client, csrf, app, stripe_falso):
    hasta_pago(client, csrf)
    html = client.get('/app/pago').get_data(as_text=True)
    assert 'Modo de prueba – usa la tarjeta 4242 4242 4242 4242' in html
    r = client.post('/app/procesar_pago', data={'_csrf': csrf})
    assert r.status_code == 303 and r.location.startswith('https://checkout.stripe.com/')
    _, params, opciones = stripe_falso.llamadas[0]
    item = params['line_items'][0]['price_data']
    assert params['currency'] == 'mxn' and item['currency'] == 'mxn' and item['unit_amount'] == 31000
    assert params['customer_email'] == 'remitente@ejemplo.com'
    assert '{CHECKOUT_SESSION_ID}' in params['success_url'] and opciones['idempotency_key'].startswith('pedido-')
    with app.app_context():
        pedido = sa.session.get(Pedido, params['metadata']['pedido_id'])
        assert pedido.estado == 'pendiente' and pedido.total == 310.0


def test_cupon_se_aplica_antes_de_cobrar(client, csrf, app, stripe_falso):
    with app.app_context():
        codigo = rb.crear_cupon(20).codigo
        sa.session.commit()
    hasta_pago(client, csrf)
    client.post('/app/cupon', data={'_csrf': csrf, 'codigo': codigo})
    client.post('/app/procesar_pago', data={'_csrf': csrf})
    assert stripe_falso.llamadas[0][1]['line_items'][0]['price_data']['unit_amount'] == 24800  # 310 - 20%
    sid = list(stripe_falso.sesiones)[0]
    client.get(f'/app/pago/exito?session_id={sid}')
    with app.app_context():
        assert sa.session.get(Cupon, codigo).usado is not None


def test_regreso_de_stripe_crea_envio_con_id_de_pago(client, csrf, app, stripe_falso):
    pagar_con_stripe(client, csrf)
    sid = list(stripe_falso.sesiones)[0]
    r = client.get(f'/app/pago/exito?session_id={sid}')
    guia = r.location.rsplit('/', 1)[1]
    with app.app_context():
        e = db.obtener_envio_modelo(guia)
        assert e.metodo_pago == 'stripe' and e.stripe_payment_intent == 'pi_test_1' and e.total == 310.0
    assert client.get(f'/app/guia/{guia}.pdf').mimetype == 'application/pdf'


def test_regreso_sin_pago_muestra_procesando(client, csrf, stripe_falso):
    stripe_falso.pagado = False
    pagar_con_stripe(client, csrf)
    sid = list(stripe_falso.sesiones)[0]
    r = client.get(f'/app/pago/exito?session_id={sid}')
    assert r.status_code == 202 and 'confirmando tu pago' in r.get_data(as_text=True)


def test_regreso_con_sesion_ajena_es_rechazado(client, csrf, app, stripe_falso):
    pagar_con_stripe(client, csrf)
    assert client.get('/app/pago/exito?session_id=cs_test_otra').status_code == 404
    assert app.test_client().get(f'/app/pago/exito?session_id={list(stripe_falso.sesiones)[0]}').status_code == 404


# ---------------------------------------------------------------- webhook

def test_webhook_firmado_confirma_el_pago_una_sola_vez(client, csrf, app, stripe_falso):
    pagar_con_stripe(client, csrf)
    sid, params = list(stripe_falso.sesiones.items())[0]
    objeto = {'id': sid, 'object': 'checkout.session', 'payment_status': 'paid', 'payment_intent': 'pi_test_wh',
              'client_reference_id': params['client_reference_id'], 'metadata': params['metadata']}
    for _ in range(2):  # Stripe puede reenviar el mismo evento
        payload, firma = evento_firmado('checkout.session.completed', objeto)
        assert app.test_client().post('/webhooks/stripe', data=payload, headers={'Stripe-Signature': firma},
                                      content_type='application/json').status_code == 200
    client.get(f'/app/pago/exito?session_id={sid}')  # y además regresa el navegador
    with app.app_context():
        envios = sa.session.query(Envio).filter_by(stripe_payment_intent='pi_test_wh').all()
        assert len(envios) == 1
        assert sa.session.get(Pedido, params['metadata']['pedido_id']).estado == 'pagado'


def test_webhook_con_firma_invalida_se_rechaza(app, stripe_falso):
    payload, firma = evento_firmado('checkout.session.completed', {'id': 'cs_x', 'object': 'checkout.session'}, secreto='whsec_otro')
    r = app.test_client().post('/webhooks/stripe', data=payload, headers={'Stripe-Signature': firma}, content_type='application/json')
    assert r.status_code == 400
    assert app.test_client().post('/webhooks/stripe', data=payload, content_type='application/json').status_code == 400


def test_webhook_sesion_expirada(client, csrf, app, stripe_falso):
    pagar_con_stripe(client, csrf)
    sid, params = list(stripe_falso.sesiones.items())[0]
    payload, firma = evento_firmado('checkout.session.expired', {'id': sid, 'object': 'checkout.session', 'metadata': params['metadata']})
    app.test_client().post('/webhooks/stripe', data=payload, headers={'Stripe-Signature': firma}, content_type='application/json')
    with app.app_context():
        assert sa.session.get(Pedido, params['metadata']['pedido_id']).estado == 'expirado'


# ---------------------------------------------------------------- reembolsos

def _envio_pagado(app, total=200.0, pi='pi_original_1'):
    hace3 = (date.today() - timedelta(days=3)).isoformat()
    conceptos = [{'concepto': 'Tarifa base', 'importe': total - 25, 'icono': 'fa-route'},
                 {'concepto': 'Empaque', 'importe': 25.0, 'icono': 'fa-box-open'}]
    with app.app_context():
        guia = db.crear_envio(remitente={'nombre': 'Tienda', 'telefono': '5500001111', 'correo': 'tienda@ejemplo.com'},
                              destinatario={'nombre': 'Luis', 'telefono': '5511223344'},
                              cotizacion={'total': total, 'fecha_min': hace3, 'fecha_max': hace3, 'tipo_envio': 'eco',
                                          'conceptos': conceptos}, tipo_entrega='recolector', correo_destinatario=None)
        e = db.obtener_envio_modelo(guia)
        e.metodo_pago, e.stripe_payment_intent = 'stripe', pi
        sa.session.commit()
    return guia


def _reembolso_aprobado(app, guia):
    from test_reembolsos import codigo_recibido, conv
    cid = conv(app)
    with app.app_context():
        r = rb.evaluar(conversacion_id=cid, turno=1, guia=guia, ultimos4_telefono='1111', problema='retraso', detalle='tarde')
    with app.app_context():
        return rb.ejecutar(conversacion_id=cid, turno_actual=2, folio=r['folio'], resolucion='reembolso',
                           codigo=codigo_recibido(app)), r['folio']


def test_reembolso_aprobado_se_ejecuta_en_stripe_sobre_el_pago_original(app, stripe_falso):
    res, folio = _reembolso_aprobado(app, _envio_pagado(app))
    assert res['estado'] == 'aprobado'
    _, params, opciones = stripe_falso.llamadas[-1]
    assert params['payment_intent'] == 'pi_original_1' and params['amount'] == 17500  # 200 - 25 de empaque
    assert opciones['idempotency_key'] == f'reembolso-{folio}'
    with app.app_context():
        caso = sa.session.scalar(select(Caso).where(Caso.folio == folio))
        assert caso.stripe_refund_id == 're_test_1' and caso.reembolso_estado == 'succeeded'


def test_error_de_stripe_se_registra_y_admin_reintenta(client, csrf, app, stripe_falso):
    stripe_falso.error_reembolso = stripe.InvalidRequestError('Charge already refunded', 'payment_intent')
    res, folio = _reembolso_aprobado(app, _envio_pagado(app))
    with app.app_context():
        caso = sa.session.scalar(select(Caso).where(Caso.folio == folio))
        assert caso.estado == 'aprobado_auto' and caso.reembolso_estado == 'error' and 'already refunded' in caso.reembolso_error
    with app.app_context():
        db.crear_usuario(nombre='Admin', correo='a@b.mx', password='Clave12345', rol='admin')
    client.post('/iniciar-sesion', data={'_csrf': csrf, 'correo': 'a@b.mx', 'password': 'Clave12345'})
    assert 'Error al reembolsar' in client.get(f'/admin/reembolsos/{folio}').get_data(as_text=True)
    stripe_falso.error_reembolso = None
    client.post(f'/admin/reembolsos/{folio}/stripe', data={'_csrf': csrf, 'accion': 'reintentar'})
    with app.app_context():
        caso = sa.session.scalar(select(Caso).where(Caso.folio == folio))
        assert caso.reembolso_estado == 'succeeded' and caso.stripe_refund_id
    clave = stripe_falso.llamadas[-1][2]['idempotency_key']
    assert clave.startswith(f'reembolso-{folio}-')  # clave nueva en el reintento
    html = client.get(f'/admin/reembolsos/{folio}').get_data(as_text=True)
    assert 'Reembolsado' in html and 'pi_original_1' in html


def test_admin_consulta_estado_real_en_stripe(client, csrf, app, stripe_falso):
    _, folio = _reembolso_aprobado(app, _envio_pagado(app))
    stripe_falso.reembolsos['re_test_1'] = 'pending'
    with app.app_context():
        db.crear_usuario(nombre='Admin', correo='a@b.mx', password='Clave12345', rol='admin')
    client.post('/iniciar-sesion', data={'_csrf': csrf, 'correo': 'a@b.mx', 'password': 'Clave12345'})
    client.post(f'/admin/reembolsos/{folio}/stripe', data={'_csrf': csrf, 'accion': 'actualizar'})
    with app.app_context():
        assert sa.session.scalar(select(Caso).where(Caso.folio == folio)).reembolso_estado == 'pending'


def test_webhook_de_reembolso_actualiza_el_caso(app, stripe_falso):
    _, folio = _reembolso_aprobado(app, _envio_pagado(app))
    payload, firma = evento_firmado('refund.updated', {'id': 're_test_1', 'object': 'refund', 'status': 'failed', 'failure_reason': 'expired_or_canceled_card'})
    app.test_client().post('/webhooks/stripe', data=payload, headers={'Stripe-Signature': firma}, content_type='application/json')
    with app.app_context():
        caso = sa.session.scalar(select(Caso).where(Caso.folio == folio))
        assert caso.reembolso_estado == 'failed' and caso.reembolso_error == 'expired_or_canceled_card'


def test_admin_aprueba_pendiente_y_se_reembolsa_en_stripe(client, csrf, app, stripe_falso):
    from test_reembolsos import _prueba, codigo_recibido, conv
    guia = _envio_pagado(app, total=450.0, pi='pi_grande')
    with app.app_context():
        e = db.obtener_envio_modelo(guia)
        e.estado = 'entregado'
        sa.session.commit()
    _prueba(app, guia, lat=19.6, lng=-99.3)
    cid = conv(app)
    with app.app_context():
        r = rb.evaluar(conversacion_id=cid, turno=1, guia=guia, ultimos4_telefono='1111', problema='entregado_no_recibido', detalle='x')
    with app.app_context():
        assert rb.ejecutar(conversacion_id=cid, turno_actual=2, folio=r['folio'], resolucion='reembolso',
                           codigo=codigo_recibido(app))['estado'] == 'pendiente_de_aprobacion'
    assert not [x for x in stripe_falso.llamadas if x[0] == 'refund.create']  # pendiente: aún no se mueve dinero
    with app.app_context():
        db.crear_usuario(nombre='Admin', correo='a@b.mx', password='Clave12345', rol='admin')
    client.post('/iniciar-sesion', data={'_csrf': csrf, 'correo': 'a@b.mx', 'password': 'Clave12345'})
    client.post(f"/admin/reembolsos/{r['folio']}/resolver", data={'_csrf': csrf, 'decision': 'aprobar'})
    _, params, _ = stripe_falso.llamadas[-1]
    assert params['payment_intent'] == 'pi_grande' and params['amount'] == 45000


def test_reembolso_de_pago_simulado_queda_manual(app):
    guia = _envio_pagado(app, pi=None)
    _, folio = _reembolso_aprobado(app, guia)
    with app.app_context():
        assert sa.session.scalar(select(Caso).where(Caso.folio == folio)).reembolso_estado == 'manual'


# ---------------------------------------------------------------- lotes de empresas

def test_lote_de_empresa_se_paga_con_stripe(client, csrf, app, stripe_falso, monkeypatch):
    import geo
    from test_clientes import EMPRESA, csv_archivo
    monkeypatch.setattr(geo, 'geocodificar', lambda d, k='': {'lat': 19.4, 'lng': -99.1, 'partes': {}})
    monkeypatch.setattr(geo, 'distancia_km', lambda *a, **k: (12.0, 'osm'))
    folio = client.post('/empresas/cargar', data=EMPRESA | {'_csrf': csrf, 'archivo': csv_archivo(3)},
                        content_type='multipart/form-data').location.rsplit('/', 1)[1]
    r = client.post(f'/empresas/lote/{folio}/pagar', data={'_csrf': csrf})
    assert r.status_code == 303 and 'checkout.stripe.com' in r.location
    sid = list(stripe_falso.sesiones)[0]
    assert client.get(f'/app/pago/exito?session_id={sid}').location.endswith(f'/empresas/lote/{folio}')
    with app.app_context():
        envios = sa.session.query(Envio).filter_by(lote_folio=folio).all()
        assert len(envios) == 3 and all(e.stripe_payment_intent == 'pi_test_1' for e in envios)
        assert sa.session.get(Lote, folio).estado == 'pagado'
