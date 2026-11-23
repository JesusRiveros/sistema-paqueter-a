from datetime import date

import pytest

import tarifas


def cotizar(**kw):
    base = dict(distancia_km=100, peso=1, largo=10, ancho=10, alto=10, hoy=date(2026, 11, 20))
    base.update(kw)
    return tarifas.cotizar(**base)


@pytest.mark.parametrize('km, esperado', [(0, 60), (50, 60), (50.1, 120), (500, 120), (1500, 250), (3000, 350), (3001, 400)])
def test_tarifa_base_por_distancia(km, esperado):
    assert tarifas.costo_base(km) == esperado


def test_el_total_es_la_suma_del_desglose():
    c = cotizar(distancia_km=540, peso=2, largo=30, ancho=20, alto=15, caja='shoes', tipo_envio='fast', compensar_co2=True)
    assert c.total == 250 + 10 + 25 + 20 + 5
    assert c.total == sum(x['importe'] for x in c.conceptos)


def test_primer_kilo_incluido():
    assert cotizar(peso=1).total == 120


def test_se_cobra_peso_volumetrico_si_es_mayor():
    c = cotizar(peso=1, largo=50, ancho=40, alto=30)  # 60000 / 5000 = 12 kg
    assert c.peso_volumetrico == 12
    assert c.peso_facturable == 12
    assert c.total == 120 + 11 * tarifas.COSTO_POR_KG_EXTRA


@pytest.mark.parametrize('caja, costo', [('small', 10), ('books', 15), ('shoes', 25), ('move', 40), ('propia', 0)])
def test_precios_de_cajas_respetados(caja, costo):
    assert tarifas.CAJAS[caja]['costo'] == costo


def test_caja_que_no_cabe_es_rechazada():
    with pytest.raises(tarifas.ErrorCotizacion):
        cotizar(largo=40, ancho=10, alto=10, caja='shoes')


def test_caja_cabe_en_cualquier_orientacion():
    assert tarifas.caja_le_queda('shoes', 15, 35, 20, 1)


def test_cajas_disponibles_respeta_peso():
    assert tarifas.cajas_disponibles(20, 10, 3, 2) == ['propia', 'shoes', 'move']


@pytest.mark.parametrize('campo, valor', [('peso', 0), ('peso', -1), ('peso', 'abc'), ('largo', 500), ('peso', float('nan'))])
def test_valores_invalidos(campo, valor):
    with pytest.raises(tarifas.ErrorCotizacion):
        cotizar(**{campo: valor})


def test_fechas_en_dias_habiles():
    # viernes 20 nov 2026: rápido = lun 23 – mar 24; eco = mié 25 – vie 27
    rapido = cotizar(tipo_envio='fast')
    assert (rapido.fecha_min, rapido.fecha_max) == ('2026-11-23', '2026-11-24')
    eco = cotizar(tipo_envio='eco')
    assert (eco.fecha_min, eco.fecha_max) == ('2026-11-25', '2026-11-27')


def test_co2_compensable_en_ambos_servicios():
    assert cotizar(tipo_envio='eco', compensar_co2=True).total == 120 + 5
