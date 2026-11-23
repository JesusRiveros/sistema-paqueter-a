"""Políticas de reembolsos y compensaciones, editables desde el admin (tabla `configuracion`).

Los valores por defecto son los que definió Balula; si alguien los cambia en el admin, se guarda el
nuevo valor en la base de datos y tiene prioridad.
"""
from models import Configuracion, ahora, sa

# clave: (valor por defecto, tipo, etiqueta, ayuda)
POLITICAS = {
    'reembolso_tope_mxn': (300, 'dinero', 'Monto máximo de reembolso automático (MXN)',
                           'Por caso. Nunca se reembolsa más de lo que el cliente pagó. Arriba de esto: pendiente de aprobación.'),
    'limite_casos_cliente': (2, 'entero', 'Compensaciones automáticas por cliente',
                             'Del siguiente en adelante, el caso queda pendiente de aprobación aunque el monto sea bajo.'),
    'ventana_dias_cliente': (30, 'entero', 'Periodo para contar el límite (días)', ''),
    'retraso_umbral_horas': (24, 'entero', 'Horas que separan un retraso menor de uno mayor', ''),
    'cupon_retraso_menor_pct': (10, 'porcentaje', 'Cupón por retraso menor (%)', 'Junto con una disculpa.'),
    'cupon_retraso_mayor_pct': (20, 'porcentaje', 'Cupón por retraso mayor (%)',
                                'El cliente elige entre este cupón o el reembolso del costo del envío.'),
    'cupon_trato_pct': (10, 'porcentaje', 'Cupón por queja del trato del repartidor (%)', ''),
    'cupon_vigencia_dias': (90, 'entero', 'Vigencia de los cupones (días)', ''),
    'evidencia_distancia_m': (300, 'entero', 'Distancia máxima para considerar válida la ubicación de entrega (m)',
                              'Si el repartidor marcó la entrega más lejos que esto del domicilio, la evidencia no se considera clara.'),
    'perdido_horas_sin_movimiento': (72, 'entero', 'Horas sin movimiento para considerar un paquete perdido',
                                     'Además, la fecha estimada de entrega debe haber pasado.'),
    'tiempo_respuesta_pendientes': ('24 horas hábiles', 'texto', 'Tiempo de respuesta para casos pendientes', 'Lo que el asistente le dice al cliente.'),
    'tiempo_reflejo_reembolso': ('5 a 10 días hábiles', 'texto', 'Tiempo en que se refleja un reembolso', 'Depende del banco del cliente.'),
}


def obtener(clave):
    if clave not in POLITICAS:
        raise KeyError(clave)
    fila = sa.session.get(Configuracion, clave)
    return fila.valor['v'] if fila else POLITICAS[clave][0]


def todas():
    return {k: obtener(k) for k in POLITICAS}


def convertir(clave, texto):
    """Valida y convierte lo que llega del formulario. Lanza ValueError con un mensaje claro."""
    _, tipo, etiqueta, _ = POLITICAS[clave]
    texto = (texto or '').strip()
    if tipo == 'texto':
        if not 3 <= len(texto) <= 60:
            raise ValueError(f'{etiqueta}: escribe entre 3 y 60 caracteres.')
        return texto
    try:
        numero = float(texto) if tipo == 'dinero' else int(texto)
    except ValueError:
        raise ValueError(f'{etiqueta}: debe ser un número.')
    limites = {'dinero': (0, 100000), 'entero': (0, 10000), 'porcentaje': (1, 100)}[tipo]
    if not limites[0] <= numero <= limites[1]:
        raise ValueError(f'{etiqueta}: debe estar entre {limites[0]} y {limites[1]}.')
    return numero


def guardar(valores, usuario):
    """valores: dict clave→texto del formulario. Valida todo antes de guardar nada."""
    convertidos = {k: convertir(k, v) for k, v in valores.items() if k in POLITICAS}
    for clave, valor in convertidos.items():
        fila = sa.session.get(Configuracion, clave) or Configuracion(clave=clave, valor={'v': None})
        fila.valor = {'v': valor}
        fila.actualizado, fila.actualizado_por = ahora(), usuario
        sa.session.add(fila)
    sa.session.commit()
    return convertidos
