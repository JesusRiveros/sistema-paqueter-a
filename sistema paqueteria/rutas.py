"""Optimización de rutas de reparto y rutas alternas (gratis, sin APIs de pago).

- Orden de paradas: vecino más cercano + mejora 2-opt (heurística clásica del problema del
  agente viajero). Para 5–40 paradas da resultados cercanos al óptimo en milisegundos.
- Rutas alternas: servidor público de OSRM (OpenStreetMap) con `alternatives=true`; los tiempos se
  ajustan con un perfil de tráfico por hora para CDMX (demostración: TODO reemplazar con datos
  reales de tiempos de entrega cuando existan).
"""
import logging
from datetime import datetime
from functools import lru_cache

import requests

from geo import USER_AGENT, haversine_km

log = logging.getLogger(__name__)
FACTOR_CARRETERA = 1.3  # la distancia por calle suele ser ~30% mayor que en línea recta

# Multiplicador de tiempo de manejo por hora del día (1.0 = flujo libre). Perfil de demostración.
TRAFICO_POR_HORA = {h: 1.0 for h in range(24)}
TRAFICO_POR_HORA.update({7: 1.5, 8: 1.8, 9: 1.6, 10: 1.3, 13: 1.3, 14: 1.4, 15: 1.3, 17: 1.5, 18: 1.8, 19: 1.7, 20: 1.4})


def km_entre(a, b):
    return haversine_km(a[0], a[1], b[0], b[1]) * FACTOR_CARRETERA


def longitud_ruta(inicio, puntos):
    total, actual = 0.0, inicio
    for p in puntos:
        total += km_entre(actual, p)
        actual = p
    return total


def _vecino_mas_cercano(inicio, puntos):
    pendientes = list(range(len(puntos)))
    orden, actual = [], inicio
    while pendientes:
        i = min(pendientes, key=lambda k: km_entre(actual, puntos[k]))
        orden.append(i)
        pendientes.remove(i)
        actual = puntos[i]
    return orden


def _dos_opt(inicio, puntos, orden, max_vueltas=50):
    """Invierte tramos mientras eso acorte la ruta (ruta abierta: no regresa al inicio)."""
    mejor = orden[:]
    mejor_km = longitud_ruta(inicio, [puntos[i] for i in mejor])
    for _ in range(max_vueltas):
        mejoro = False
        for i in range(len(mejor) - 1):
            for j in range(i + 1, len(mejor)):
                candidato = mejor[:i] + mejor[i:j + 1][::-1] + mejor[j + 1:]
                km = longitud_ruta(inicio, [puntos[k] for k in candidato])
                if km + 1e-9 < mejor_km:
                    mejor, mejor_km, mejoro = candidato, km, True
        if not mejoro:
            break
    return mejor, mejor_km


def optimizar(inicio, paradas):
    """inicio: (lat, lng). paradas: lista de dicts con 'lat' y 'lng' (y lo que quieras).

    Devuelve dict con las paradas ordenadas, km estimados y km ahorrados frente al orden original.
    Las paradas sin coordenadas se mandan al final, sin reordenar.
    """
    con = [p for p in paradas if p.get('lat') is not None and p.get('lng') is not None]
    sin = [p for p in paradas if p not in con]
    puntos = [(p['lat'], p['lng']) for p in con]
    km_original = longitud_ruta(inicio, puntos)
    if len(con) <= 1:
        return {'paradas': con + sin, 'km': round(km_original, 1), 'km_original': round(km_original, 1),
                'km_ahorrados': 0.0}
    orden, km = _dos_opt(inicio, puntos, _vecino_mas_cercano(inicio, puntos))
    return {'paradas': [con[i] for i in orden] + sin, 'km': round(km, 1), 'km_original': round(km_original, 1),
            'km_ahorrados': round(max(0.0, km_original - km), 1)}


def factor_trafico(cuando=None):
    return TRAFICO_POR_HORA[(cuando or datetime.now()).hour]


@lru_cache(maxsize=256)
def _osrm_alternativas(lat1, lng1, lat2, lng2):
    url = f'https://router.project-osrm.org/route/v1/driving/{lng1},{lat1};{lng2},{lat2}'
    resp = requests.get(url, params={'alternatives': 'true', 'overview': 'full', 'geometries': 'geojson',
                                     'steps': 'false'}, headers={'User-Agent': USER_AGENT}, timeout=8)
    resp.raise_for_status()
    data = resp.json()
    if data.get('code') != 'Ok':
        return ()
    return tuple((r['distance'], r['duration'], tuple(tuple(c) for c in r['geometry']['coordinates']))
                 for r in data['routes'])


def rutas_alternas(origen, destino, cuando=None):
    """Hasta 3 rutas por carretera, con tiempo ajustado por tráfico y una recomendación.

    Si OSRM no responde, devuelve una estimación en línea recta (sin geometría)."""
    trafico = factor_trafico(cuando)
    try:
        crudas = _osrm_alternativas(round(origen[0], 5), round(origen[1], 5), round(destino[0], 5), round(destino[1], 5))
    except (requests.RequestException, KeyError, ValueError) as e:
        log.warning('OSRM sin respuesta: %s', e)
        crudas = ()
    if not crudas:
        km = km_entre(origen, destino)
        return {'fuente': 'estimada', 'trafico': trafico, 'rutas': [
            {'nombre': 'Ruta estimada', 'km': round(km, 1), 'min': round(km / 25 * 60 * trafico),
             'geometria': [[origen[0], origen[1]], [destino[0], destino[1]]], 'recomendada': True}]}
    rutas = []
    for i, (metros, segundos, coords) in enumerate(crudas):
        # Rutas alternas suelen ir por vías secundarias: les afecta menos la hora pico.
        ajuste = trafico if i == 0 else 1 + (trafico - 1) * 0.6
        rutas.append({'nombre': 'Ruta principal' if i == 0 else f'Alterna {i}', 'km': round(metros / 1000, 1),
                      'min': round(segundos / 60 * ajuste), 'geometria': [[lat, lng] for lng, lat in coords],
                      'recomendada': False})
    min(rutas, key=lambda r: r['min'])['recomendada'] = True
    return {'fuente': 'osrm', 'trafico': trafico, 'rutas': rutas}
