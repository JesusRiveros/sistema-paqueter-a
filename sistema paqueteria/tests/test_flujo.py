import re

import db

FORM_PASO1 = dict(origen='Av. Reforma 100, CDMX', destino='Av. Juárez 50, Guadalajara', peso='2', largo='30',
                  ancho='20', alto='15', caja='shoes', tipo_entrega='recolector', tipo_envio='fast',
                  compensar='si', distancia_ruta_km='540')


def persona(prefijo, nombre):
    return {f'{prefijo}_nombre': nombre, f'{prefijo}_telefono': '55 1234 5678', f'{prefijo}_calle': 'Calle 1',
            f'{prefijo}_colonia': 'Centro', f'{prefijo}_ciudad': 'Guadalajara', f'{prefijo}_estado': 'Jalisco',
            f'{prefijo}_cp': '44100'}


def hasta_pago(client, csrf):
    assert client.post('/app/calculate', data={'_csrf': csrf, **FORM_PASO1}).status_code == 302
    r = client.post('/app/completar', data={'_csrf': csrf, **persona('remitente', 'Ana Pérez'),
                                            **persona('destinatario', 'Luis Gómez')})
    assert r.location.endswith('/app/pago')


def test_paginas_publicas(client):
    for url in ['/', '/rastreo', '/iniciar-sesion', '/app/']:
        assert client.get(url).status_code == 200
    assert client.get('/no-existe').status_code == 404
    assert client.get('/formulario').status_code == 301


def test_no_se_pueden_saltar_pasos(client):
    for url in ['/app/cotizacion', '/app/completar', '/app/pago', '/app/checkout']:
        assert client.get(url).location.endswith('/app/')


def test_post_sin_csrf_es_rechazado(client):
    r = client.post('/app/calculate', data=FORM_PASO1)
    assert r.status_code == 302
    assert client.get('/app/cotizacion').status_code == 302  # no se guardó nada


def test_validacion_del_servidor(client, csrf):
    r = client.post('/app/calculate', data={'_csrf': csrf, **FORM_PASO1, 'peso': '-3'})
    assert r.status_code == 400
    assert 'peso' in r.get_data(as_text=True)


def test_desglose_coincide_con_total(client, csrf):
    client.post('/app/calculate', data={'_csrf': csrf, **FORM_PASO1})
    html = client.get('/app/cotizacion').get_data(as_text=True)
    for importe in ['$250.00', '$10.00', '$25.00', '$20.00', '$5.00', '$310.00']:
        assert importe in html


def test_api_cotizar(client, csrf):
    r = client.post('/app/api/cotizar', headers={'X-CSRF-Token': csrf},
                    json=dict(distancia_km=320, peso=2, largo=30, ancho=20, alto=15, caja='shoes', tipo_envio='eco'))
    assert r.get_json()['cotizacion']['total'] == 120 + 10 + 25
    r = client.post('/app/api/cotizar', headers={'X-CSRF-Token': csrf}, json=dict(peso='x'))
    assert r.status_code == 422


def test_flujo_completo_genera_envio_rastreable(client, csrf, app):
    hasta_pago(client, csrf)
    assert client.post('/app/procesar_pago', data={'_csrf': csrf}).location.endswith('/app/checkout')
    r = client.post('/app/checkout', data={'_csrf': csrf, 'resultado': 'success'})
    guia = r.location.rsplit('/', 1)[1]
    assert re.match(r'^BLL-[0-9A-F]{8}$', guia)

    assert guia in client.get(r.location).get_data(as_text=True)
    pdf = client.get(f'/app/guia/{guia}.pdf')
    assert pdf.mimetype == 'application/pdf' and pdf.data.startswith(b'%PDF')

    rastreo = client.get(f'/rastreo?guia={guia.lower()}').get_data(as_text=True)
    assert 'Guía generada' in rastreo and 'Guadalajara' in rastreo
    assert 'Calle 1' not in rastreo  # no se exponen direcciones completas

    with app.app_context():
        db.registrar_evento(guia, 'en_transito', ubicacion='CEDIS Vallejo')
    assert 'CEDIS Vallejo' in client.get(f'/rastreo?guia={guia}').get_data(as_text=True)

    # la sesión se limpia: no se puede pagar dos veces
    assert client.get('/app/pago').location.endswith('/app/')


def test_guia_solo_la_descarga_quien_la_creo(client, csrf, app):
    hasta_pago(client, csrf)
    guia = client.post('/app/checkout', data={'_csrf': csrf, 'resultado': 'success'}).location.rsplit('/', 1)[1]
    otro = app.test_client()
    assert otro.get(f'/app/guia/{guia}.pdf').status_code == 404
    assert otro.get(f'/app/envio/{guia}').status_code == 404


def test_pago_fallido_conserva_la_cotizacion(client, csrf):
    hasta_pago(client, csrf)
    r = client.post('/app/checkout', data={'_csrf': csrf, 'resultado': 'failure'})
    assert r.location.endswith('/app/pago_fallido')
    assert client.get('/app/pago').status_code == 200


def test_rastreo_guia_inexistente(client):
    html = client.get('/rastreo?guia=BLL-00000000').get_data(as_text=True)
    assert 'No encontramos la guía' in html
    assert 'solo puede contener' in client.get('/rastreo?guia=<script>').get_data(as_text=True)


def test_contacto(client, csrf, app):
    assert client.post('/contacto', data={'_csrf': csrf, 'nombre': 'Ana', 'correo': 'mal', 'mensaje': 'x'}).status_code == 302
    client.post('/contacto', data={'_csrf': csrf, 'nombre': 'Ana', 'correo': 'ana@ejemplo.com', 'mensaje': 'Quiero información'})
    with app.app_context():
        assert db.sa.session.query(db.MensajeContacto).count() == 1


def test_pagos_simulados_desactivados(client, csrf, app):
    app.config['PAGOS_SIMULADOS'] = False
    hasta_pago(client, csrf)
    assert client.post('/app/procesar_pago', data={'_csrf': csrf}).location.endswith('/app/pago')
    assert client.get('/app/checkout').status_code == 404
