"""Geocodificación y distancias por carretera.

Orden de proveedores:
  1. Google Maps (si hay API key y el proyecto tiene facturación activa).
  2. OpenStreetMap: Nominatim (geocodificación) + OSRM (ruta por carretera). Gratis, sin key.
  3. Distancia que calculó el navegador / línea recta × 1.25 (último recurso).

Nominatim pide máximo 1 petición por segundo e identificarse con un User-Agent; por eso
se cachea y se espacia. Para mucho volumen conviene un servidor propio o un proveedor de pago.
"""
import logging
import math
import threading
import time
from functools import lru_cache

import requests

log = logging.getLogger(__name__)

USER_AGENT = 'BalulaExpress/1.0 (cotizador de envios)'
TIMEOUT = 8
_ultimo_nominatim = 0.0
_lock = threading.Lock()


class SinProveedores(Exception):
    """Ningún proveedor pudo responder."""


# ---------------------------------------------------------------- utilidades

def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def float_o_none(valor):
    try:
        n = float(valor)
        return n if math.isfinite(n) else None
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- Google

def _google_distancia(origen, destino, api_key):
    data = requests.get('https://maps.googleapis.com/maps/api/distancematrix/json',
                        params={'origins': origen, 'destinations': destino, 'key': api_key,
                                'region': 'mx'},
                        timeout=TIMEOUT).json()
    if data.get('status') != 'OK':
        log.error('Google Distance Matrix rechazó la petición: %s %s',
                  data.get('status'), data.get('error_message', ''))
        return None
    elemento = data['rows'][0]['elements'][0]
    if elemento.get('status') != 'OK':
        return None
    return round(elemento['distance']['value'] / 1000, 1)


def _google_geocode(direccion, api_key):
    resp = requests.get('https://maps.googleapis.com/maps/api/geocode/json',
                        params={'address': direccion, 'key': api_key, 'language': 'es', 'region': 'mx'},
                        timeout=TIMEOUT).json()
    if resp.get('status') != 'OK':
        log.error('Google Geocoding rechazó la petición: %s', resp.get('status'))
        return None
    r = resp['results'][0]
    partes = {'calle': '', 'colonia': '', 'ciudad': '', 'estado': '', 'cp': ''}
    numero = ruta = ''
    for comp in r['address_components']:
        tipos = comp['types']
        if 'street_number' in tipos:
            numero = comp['long_name']
        elif 'route' in tipos:
            ruta = comp['long_name']
        elif 'sublocality_level_1' in tipos or 'neighborhood' in tipos:
            partes['colonia'] = comp['long_name']
        elif 'locality' in tipos:
            partes['ciudad'] = comp['long_name']
        elif 'administrative_area_level_1' in tipos:
            partes['estado'] = comp['long_name']
        elif 'postal_code' in tipos:
            partes['cp'] = comp['long_name']
    partes['calle'] = f'{ruta} {numero}'.strip()
    loc = r['geometry']['location']
    return {'lat': loc['lat'], 'lng': loc['lng'], 'partes': partes}


# ---------------------------------------------------------------- OpenStreetMap

def _esperar_turno_nominatim():
    global _ultimo_nominatim
    with _lock:
        espera = 1.05 - (time.monotonic() - _ultimo_nominatim)
        if espera > 0:
            time.sleep(espera)
        _ultimo_nominatim = time.monotonic()


@lru_cache(maxsize=512)
def _osm_geocode(direccion):
    _esperar_turno_nominatim()
    resp = requests.get('https://nominatim.openstreetmap.org/search',
                        params={'q': direccion, 'format': 'jsonv2', 'limit': 1, 'countrycodes': 'mx',
                                'addressdetails': 1, 'accept-language': 'es'},
                        headers={'User-Agent': USER_AGENT}, timeout=TIMEOUT)
    resp.raise_for_status()
    datos = resp.json()
    if not datos:
        return None
    r = datos[0]
    a = r.get('address', {})
    partes = {
        'calle': ' '.join(v for v in (a.get('road', ''), a.get('house_number', '')) if v),
        'colonia': a.get('suburb') or a.get('neighbourhood') or a.get('quarter') or '',
        'ciudad': a.get('city') or a.get('town') or a.get('village') or a.get('municipality') or '',
        'estado': a.get('state', ''),
        'cp': a.get('postcode', ''),
    }
    return {'lat': float(r['lat']), 'lng': float(r['lon']), 'partes': partes}


@lru_cache(maxsize=512)
def _osrm_distancia(lat1, lng1, lat2, lng2):
    url = f'https://router.project-osrm.org/route/v1/driving/{lng1},{lat1};{lng2},{lat2}'
    resp = requests.get(url, params={'overview': 'false'}, headers={'User-Agent': USER_AGENT}, timeout=TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    if data.get('code') != 'Ok' or not data.get('routes'):
        return None
    return round(data['routes'][0]['distance'] / 1000, 1)


# ---------------------------------------------------------------- API pública

def geocodificar(direccion, api_key=''):
    """Devuelve {'lat', 'lng', 'partes'} o None. Prueba Google y luego OpenStreetMap."""
    if not direccion:
        return None
    if api_key:
        try:
            r = _google_geocode(direccion, api_key)
            if r:
                return r
        except (requests.RequestException, KeyError, IndexError, ValueError) as e:
            log.warning('Google Geocoding falló: %s', e)
    try:
        return _osm_geocode(direccion.strip())
    except (requests.RequestException, KeyError, IndexError, ValueError) as e:
        log.warning('Nominatim falló: %s', e)
        return None


def distancia_km(origen, destino, api_key='', coords=None, distancia_cliente=None):
    """Distancia por carretera en km y el proveedor que la dio: (km, 'google'|'osm'|'navegador'|'estimada').

    Lanza SinProveedores si nadie pudo calcularla.
    """
    if api_key:
        try:
            km = _google_distancia(origen, destino, api_key)
            if km is not None:
                return km, 'google'
        except (requests.RequestException, KeyError, IndexError, ValueError) as e:
            log.warning('Distance Matrix falló: %s', e)

    distancia_cliente = float_o_none(distancia_cliente)
    if distancia_cliente and 0 < distancia_cliente < 10000:
        return round(distancia_cliente, 1), 'navegador'

    coords = list(coords or [None] * 4)
    if any(c is None for c in coords):
        o = geocodificar(origen) if None in coords[:2] else None
        d = geocodificar(destino) if None in coords[2:] else None
        if o:
            coords[0], coords[1] = o['lat'], o['lng']
        if d:
            coords[2], coords[3] = d['lat'], d['lng']

    if all(c is not None for c in coords):
        try:
            km = _osrm_distancia(*[round(c, 5) for c in coords])
            if km is not None:
                return km, 'osm'
        except (requests.RequestException, KeyError, IndexError, ValueError) as e:
            log.warning('OSRM falló: %s', e)
        return round(haversine_km(*coords) * 1.25, 1), 'estimada'

    raise SinProveedores('No se pudo ubicar el origen o el destino.')
