"""Respaldo de mapas: Google → OpenStreetMap → navegador → estimación. Sin red (todo simulado)."""
import pytest
import requests

import geo


@pytest.fixture(autouse=True)
def sin_red(monkeypatch):
    def prohibido(*a, **k):
        raise AssertionError('Las pruebas no deben salir a internet')
    monkeypatch.setattr(geo.requests, 'get', prohibido)


def test_google_ok(monkeypatch):
    monkeypatch.setattr(geo, '_google_distancia', lambda o, d, k: 512.3)
    assert geo.distancia_km('CDMX', 'GDL', 'key') == (512.3, 'google')


def test_google_rechazado_usa_openstreetmap(monkeypatch):
    monkeypatch.setattr(geo, '_google_distancia', lambda o, d, k: None)  # REQUEST_DENIED
    monkeypatch.setattr(geo, '_osm_geocode', lambda d: {'lat': 19.43, 'lng': -99.13, 'partes': {}})
    monkeypatch.setattr(geo, '_osrm_distancia', lambda *c: 543.5)
    assert geo.distancia_km('CDMX', 'GDL', 'key') == (543.5, 'osm')


def test_distancia_del_navegador_tiene_prioridad_sobre_osm(monkeypatch):
    monkeypatch.setattr(geo, '_google_distancia', lambda o, d, k: None)
    assert geo.distancia_km('CDMX', 'GDL', 'key', distancia_cliente='540') == (540.0, 'navegador')


def test_osrm_caido_usa_linea_recta(monkeypatch):
    def caido(*a):
        raise requests.ConnectionError('osrm caído')
    monkeypatch.setattr(geo, '_osrm_distancia', caido)
    km, fuente = geo.distancia_km('A', 'B', '', coords=[19.43, -99.13, 20.67, -103.35])
    assert fuente == 'estimada' and 500 < km < 600


def test_ningun_proveedor(monkeypatch):
    monkeypatch.setattr(geo, '_osm_geocode', lambda d: None)
    with pytest.raises(geo.SinProveedores):
        geo.distancia_km('???', '???', '')


def test_cotizacion_avanza_aunque_google_falle(client, csrf, monkeypatch):
    import app as app_mod
    monkeypatch.setattr(app_mod.geo, 'distancia_km', lambda *a, **k: (543.5, 'osm'))
    monkeypatch.setattr(app_mod.geo, 'geocodificar', lambda *a, **k: None)
    r = client.post('/app/calculate', data={
        '_csrf': csrf, 'origen': 'Reforma 222, CDMX', 'destino': 'Juárez 976, Guadalajara', 'peso': '1',
        'largo': '10', 'ancho': '10', 'alto': '10', 'caja': 'propia', 'tipo_entrega': 'recolector', 'tipo_envio': 'eco'})
    assert r.status_code == 302 and r.location.endswith('/app/cotizacion')


def test_error_de_distancia_se_muestra_arriba(client, csrf, monkeypatch):
    import app as app_mod

    def falla(*a, **k):
        raise geo.SinProveedores('x')
    monkeypatch.setattr(app_mod.geo, 'distancia_km', falla)
    r = client.post('/app/calculate', data={
        '_csrf': csrf, 'origen': 'Lugar inexistente', 'destino': 'Otro inexistente', 'peso': '1',
        'largo': '10', 'ancho': '10', 'alto': '10', 'caja': 'propia', 'tipo_entrega': 'recolector', 'tipo_envio': 'eco'})
    html = r.get_data(as_text=True)
    assert r.status_code == 400 and 'id="errores-cotizacion"' in html and 'No pudimos cotizar' in html
