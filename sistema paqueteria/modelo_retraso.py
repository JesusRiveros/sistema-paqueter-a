"""Predicción de riesgo de retraso por parada (regresión logística en Python puro, sin dependencias).

Mientras no haya historial real, el modelo se entrena con DATOS SIMULADOS que siguen reglas
razonables (hora pico, distancia, número de parada, servicio, incidencias). Por eso en la interfaz
se etiqueta como demostración.
TODO: cuando haya >= 300 entregas reales, entrenar con ellas (`entrenar(ejemplos_reales)`).
"""
import json
import math
import os
import random

CARACTERISTICAS = ['hora_pico', 'km_parada', 'num_parada', 'es_rapido', 'incidencias_zona', 'fin_de_semana']
_modelo = None


def _vector(x):
    return [1.0, x['hora_pico'], x['km_parada'] / 10, x['num_parada'] / 10, x['es_rapido'],
            x['incidencias_zona'] / 3, x['fin_de_semana']]


def _sigmoide(z):
    return 1 / (1 + math.exp(-max(-30, min(30, z))))


def datos_simulados(n=3000, semilla=7):
    rnd = random.Random(semilla)
    ejemplos = []
    for _ in range(n):
        x = {'hora_pico': float(rnd.random() < 0.35), 'km_parada': rnd.uniform(0.3, 15),
             'num_parada': rnd.randint(1, 30), 'es_rapido': float(rnd.random() < 0.4),
             'incidencias_zona': rnd.choice([0, 0, 0, 1, 1, 2, 3]), 'fin_de_semana': float(rnd.random() < 0.15)}
        # "Verdad" oculta que el modelo debe descubrir
        z = (-2.2 + 1.3 * x['hora_pico'] + 0.09 * x['km_parada'] + 0.05 * x['num_parada']
             + 0.4 * x['es_rapido'] + 0.5 * x['incidencias_zona'] + 0.3 * x['fin_de_semana'])
        ejemplos.append((x, 1 if rnd.random() < _sigmoide(z) else 0))
    return ejemplos


def entrenar(ejemplos, epocas=300, tasa=0.5):
    pesos = [0.0] * (len(CARACTERISTICAS) + 1)
    vectores = [(_vector(x), y) for x, y in ejemplos]
    n = len(vectores)
    for _ in range(epocas):
        grad = [0.0] * len(pesos)
        for v, y in vectores:
            error = _sigmoide(sum(w * xi for w, xi in zip(pesos, v))) - y
            for k, xi in enumerate(v):
                grad[k] += error * xi
        pesos = [w - tasa * g / n for w, g in zip(pesos, grad)]
    aciertos = sum((_sigmoide(sum(w * xi for w, xi in zip(pesos, v))) >= 0.5) == bool(y) for v, y in vectores)
    return {'pesos': pesos, 'exactitud': round(aciertos / n, 3), 'ejemplos': n, 'origen': 'simulado'}


def cargar(ruta_cache=None):
    global _modelo
    if _modelo is None:
        if ruta_cache and os.path.exists(ruta_cache):
            with open(ruta_cache, encoding='utf-8') as f:
                _modelo = json.load(f)
        else:
            _modelo = entrenar(datos_simulados())
            if ruta_cache:
                os.makedirs(os.path.dirname(ruta_cache), exist_ok=True)
                with open(ruta_cache, 'w', encoding='utf-8') as f:
                    json.dump(_modelo, f)
    return _modelo


def predecir(x, ruta_cache=None):
    """Probabilidad (0–1) de que la parada se entregue tarde."""
    pesos = cargar(ruta_cache)['pesos']
    return _sigmoide(sum(w * xi for w, xi in zip(pesos, _vector(x))))


def nivel(prob):
    return 'alto' if prob >= 0.5 else 'medio' if prob >= 0.3 else 'bajo'
