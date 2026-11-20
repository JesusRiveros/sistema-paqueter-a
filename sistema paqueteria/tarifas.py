"""Tarifas de Balula Express.

Este módulo es la ÚNICA fuente de precios del sistema: el formulario (vía la
API /app/api/cotizar), la página de resultado, el resumen de pago y la guía PDF
leen de aquí. Si cambias un precio, cámbialo solo en este archivo.
"""
from dataclasses import dataclass, field, asdict
from datetime import date, timedelta
import math

# (distancia máxima en km, costo base en MXN)
DISTANCIA_TARIFAS = [
    (50, 60),
    (500, 120),
    (1500, 250),
    (3000, 350),
    (math.inf, 400),
]

KG_INCLUIDOS = 1              # el costo base incluye el primer kilo
COSTO_POR_KG_EXTRA = 10       # MXN por cada kg facturable adicional
COSTO_ENVIO_EXPRES = 20       # MXN
COSTO_CO2 = 5                 # MXN, aportación para reforestar
FACTOR_VOLUMETRICO = 5000     # cm³ / kg (estándar de la industria)
FACTOR_CO2_KG_KM = 0.21 / 1000  # kg de CO₂ por kg transportado por km

LIMITES = {
    'peso_max': 70,        # kg
    'dimension_max': 200,  # cm por lado
}

TIPOS_ENVIO = {
    'fast': {'nombre': 'Rápido', 'dias_min': 1, 'dias_max': 2, 'costo': COSTO_ENVIO_EXPRES,
             'icono': 'fa-bolt', 'descripcion': 'Llega en 1 a 2 días hábiles'},
    'eco': {'nombre': 'Ecológico', 'dias_min': 3, 'dias_max': 5, 'costo': 0,
            'icono': 'fa-leaf', 'descripcion': 'Llega en 3 a 5 días hábiles, rutas consolidadas'},
}

TIPOS_ENTREGA = {
    'recolector': {'nombre': 'Recolección a domicilio', 'icono': 'fa-truck'},
    'centro_autorizado': {'nombre': 'Lo llevo a un centro autorizado', 'icono': 'fa-store'},
}

# Medidas interiores en cm (largo, ancho, alto)
CAJAS = {
    'propia': {'nombre': 'Empaque propio', 'medidas': None, 'peso_max': None, 'costo': 0,
               'img': None, 'descripcion': 'Ya tengo mi paquete listo'},
    'small': {'nombre': 'Sobre A4', 'medidas': (32, 24, 1), 'peso_max': 0.5, 'costo': 10,
              'img': 'img/cajas/sobre.webp', 'descripcion': 'Documentos'},
    'books': {'nombre': 'Caja chica', 'medidas': (23, 14, 4), 'peso_max': 1.5, 'costo': 15,
              'img': 'img/cajas/chica.webp', 'descripcion': 'Uno o dos libros'},
    'shoes': {'nombre': 'Caja mediana', 'medidas': (35, 20, 15), 'peso_max': 3, 'costo': 25,
              'img': 'img/cajas/mediana.webp', 'descripcion': 'Tamaño caja de zapatos'},
    'move': {'nombre': 'Caja grande', 'medidas': (75, 35, 35), 'peso_max': 30, 'costo': 40,
             'img': 'img/cajas/grande.webp', 'descripcion': 'Tamaño mudanza'},
}


class ErrorCotizacion(ValueError):
    """Datos de entrada inválidos para cotizar."""


@dataclass
class Cotizacion:
    distancia_km: float
    peso_real: float
    peso_volumetrico: float
    peso_facturable: float
    largo: float
    ancho: float
    alto: float
    caja: str
    tipo_envio: str
    compensar_co2: bool
    co2_kg: float
    fecha_min: str
    fecha_max: str
    conceptos: list = field(default_factory=list)
    total: float = 0.0

    def to_dict(self):
        return asdict(self)


def costo_base(distancia_km):
    return next(precio for limite, precio in DISTANCIA_TARIFAS if distancia_km <= limite)


def peso_volumetrico(largo, ancho, alto):
    return round(largo * ancho * alto / FACTOR_VOLUMETRICO, 2)


def caja_le_queda(caja_id, largo, ancho, alto, peso):
    """True si el paquete cabe (en cualquier orientación) y no excede el peso de la caja."""
    caja = CAJAS[caja_id]
    if caja['medidas'] is None:
        return True
    paquete = sorted((largo, ancho, alto), reverse=True)
    interior = sorted(caja['medidas'], reverse=True)
    return all(p <= i for p, i in zip(paquete, interior)) and peso <= caja['peso_max']


def cajas_disponibles(largo, ancho, alto, peso):
    return [cid for cid in CAJAS if caja_le_queda(cid, largo, ancho, alto, peso)]


def sumar_dias_habiles(inicio, dias):
    actual = inicio
    restantes = dias
    while restantes > 0:
        actual += timedelta(days=1)
        if actual.weekday() < 5:  # lunes a viernes
            restantes -= 1
    return actual


def _numero(valor, nombre, minimo, maximo):
    try:
        n = float(valor)
    except (TypeError, ValueError):
        raise ErrorCotizacion(f'Ingresa un valor numérico para {nombre}.')
    if not math.isfinite(n) or n < minimo or n > maximo:
        raise ErrorCotizacion(f'{nombre.capitalize()} debe estar entre {minimo:g} y {maximo:g}.')
    return n


def cotizar(*, distancia_km, peso, largo, ancho, alto, caja='propia', tipo_envio='eco',
            compensar_co2=False, hoy=None):
    """Calcula la cotización completa. Lanza ErrorCotizacion si algún dato es inválido."""
    distancia_km = _numero(distancia_km, 'la distancia', 0, 10000)
    peso = _numero(peso, 'el peso', 0.01, LIMITES['peso_max'])
    largo = _numero(largo, 'el largo', 1, LIMITES['dimension_max'])
    ancho = _numero(ancho, 'el ancho', 1, LIMITES['dimension_max'])
    alto = _numero(alto, 'el alto', 0.1, LIMITES['dimension_max'])

    caja = caja or 'propia'
    if caja not in CAJAS:
        raise ErrorCotizacion('Selecciona un tipo de caja válido.')
    if not caja_le_queda(caja, largo, ancho, alto, peso):
        raise ErrorCotizacion(f'Tu paquete no cabe en la opción "{CAJAS[caja]["nombre"]}".')
    if tipo_envio not in TIPOS_ENVIO:
        raise ErrorCotizacion('Selecciona un tipo de envío válido.')

    vol = peso_volumetrico(largo, ancho, alto)
    facturable = round(max(peso, vol), 2)
    kg_extra = round(max(0.0, facturable - KG_INCLUIDOS), 2)

    conceptos = [{'concepto': f'Tarifa base por distancia ({distancia_km:g} km)',
                  'importe': float(costo_base(distancia_km)), 'icono': 'fa-route'}]
    if kg_extra > 0:
        conceptos.append({'concepto': f'Peso adicional ({kg_extra:g} kg × ${COSTO_POR_KG_EXTRA})',
                          'importe': round(kg_extra * COSTO_POR_KG_EXTRA, 2), 'icono': 'fa-weight-hanging'})
    if CAJAS[caja]['costo']:
        conceptos.append({'concepto': f'Empaque: {CAJAS[caja]["nombre"]}',
                          'importe': float(CAJAS[caja]['costo']), 'icono': 'fa-box-open'})
    if TIPOS_ENVIO[tipo_envio]['costo']:
        conceptos.append({'concepto': 'Envío rápido', 'importe': float(TIPOS_ENVIO[tipo_envio]['costo']),
                          'icono': 'fa-bolt'})
    if compensar_co2:
        conceptos.append({'concepto': 'Compensación de CO₂', 'importe': float(COSTO_CO2), 'icono': 'fa-seedling'})

    hoy = hoy or date.today()
    servicio = TIPOS_ENVIO[tipo_envio]
    return Cotizacion(
        distancia_km=distancia_km,
        peso_real=peso,
        peso_volumetrico=vol,
        peso_facturable=facturable,
        largo=largo, ancho=ancho, alto=alto,
        caja=caja,
        tipo_envio=tipo_envio,
        compensar_co2=bool(compensar_co2),
        co2_kg=round(peso * distancia_km * FACTOR_CO2_KG_KM, 2),
        fecha_min=sumar_dias_habiles(hoy, servicio['dias_min']).isoformat(),
        fecha_max=sumar_dias_habiles(hoy, servicio['dias_max']).isoformat(),
        conceptos=conceptos,
        total=round(sum(c['importe'] for c in conceptos), 2),
    )

