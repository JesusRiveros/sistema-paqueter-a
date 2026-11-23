/* Balula Express — cotizador (paso 1)
 * El precio SIEMPRE lo calcula el servidor (/app/api/cotizar, misma lógica que el cálculo final).
 * Este script solo recolecta datos, pinta el resumen y maneja Google Maps.
 */
(function () {
  'use strict';
  var cfg = window.BALULA || {};
  var form = document.getElementById('form-cotizar');
  if (!form) return;

  var $ = function (id) { return document.getElementById(id); };
  var moneda = new Intl.NumberFormat('es-MX', { style: 'currency', currency: 'MXN' });
  var DIAS = ['dom', 'lun', 'mar', 'mié', 'jue', 'vie', 'sáb'];
  var MESES = ['ene', 'feb', 'mar', 'abr', 'may', 'jun', 'jul', 'ago', 'sep', 'oct', 'nov', 'dic'];

  function fechaCorta(iso) {
    var p = iso.split('-');
    var d = new Date(+p[0], +p[1] - 1, +p[2]);
    return DIAS[d.getDay()] + ' ' + d.getDate() + ' ' + MESES[d.getMonth()];
  }

  function escapar(t) {
    var div = document.createElement('div');
    div.textContent = t;
    return div.innerHTML;
  }

  function valor(nombre) {
    var el = form.querySelector('[name="' + nombre + '"]:checked') || form.elements[nombre];
    return el ? el.value : '';
  }

  // ---------------------------------------------------------------- Cotización en vivo
  var temporizador = null;
  var peticion = 0;

  function datosActuales() {
    return {
      distancia_km: parseFloat($('distancia_ruta_km').value) || 0,
      peso: $('peso').value, largo: $('largo').value, ancho: $('ancho').value, alto: $('alto').value,
      caja: valor('caja') || 'propia',
      tipo_envio: valor('tipo_envio') || 'eco',
      compensar: $('compensar').checked
    };
  }

  function paqueteCompleto(d) {
    return ['peso', 'largo', 'ancho', 'alto'].every(function (k) { return parseFloat(d[k]) > 0; });
  }

  function programarCotizacion() {
    clearTimeout(temporizador);
    temporizador = setTimeout(cotizar, 250);
  }

  function cotizar() {
    var d = datosActuales();
    if (!paqueteCompleto(d)) { pintarVacio(); return; }
    var id = ++peticion;
    $('total').classList.add('loading');
    fetch(cfg.apiCotizar, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': cfg.csrf },
      body: JSON.stringify(d)
    })
      .then(function (r) { return r.json(); })
      .then(function (res) {
        if (id !== peticion) return; // respuesta vieja
        $('total').classList.remove('loading');
        if (!res.ok) { pintarError(res.error); return; }
        actualizarCajas(res.cajas_disponibles);
        pintar(res.cotizacion, d.distancia_km > 0);
      })
      .catch(function () {
        $('total').classList.remove('loading');
        pintarError('No pudimos calcular el precio. Revisa tu conexión.');
      });
  }

  function pintarVacio() {
    $('conceptos').innerHTML = '<li><span>Completa los datos de tu paquete para ver el precio.</span></li>';
    $('total').textContent = '—';
    $('total-label').textContent = 'Total';
    $('estimado-nota').textContent = '';
    $('fechas').textContent = '';
    $('co2-kg').textContent = '—';
    $('peso-detalle').textContent = '';
  }

  function pintarError(msg) {
    $('conceptos').innerHTML = '<li><span class="text-danger"><i class="fa-solid fa-circle-exclamation"></i>' + escapar(msg) + '</span></li>';
    $('total').textContent = '—';
  }

  function pintar(c, conDistancia) {
    var html = c.conceptos.map(function (item) {
      var concepto = item.concepto;
      if (!conDistancia && item.icono === 'fa-route') concepto = 'Tarifa base (desde)';
      return '<li><span><i class="fa-solid ' + item.icono + '"></i>' + escapar(concepto) + '</span><span>' +
        moneda.format(item.importe) + '</span></li>';
    }).join('');
    $('conceptos').innerHTML = html;
    $('total').textContent = moneda.format(c.total);
    $('total-label').textContent = conDistancia ? 'Total' : 'Desde';
    $('estimado-nota').innerHTML = conDistancia
      ? '<i class="fa-solid fa-circle-check text-success me-1"></i>Distancia por carretera: ' + c.distancia_km + ' km.'
      : '<i class="fa-solid fa-circle-info me-1"></i>Elige origen y destino de la lista para calcular la tarifa por distancia.';
    $('fechas').innerHTML = '<i class="fa-regular fa-calendar me-1 text-brand"></i>Entrega estimada: <strong>' +
      fechaCorta(c.fecha_min) + ' – ' + fechaCorta(c.fecha_max) + '</strong>';
    $('co2-kg').textContent = conDistancia ? c.co2_kg + ' kg' : '— (falta la ruta)';
    var facturable = c.peso_volumetrico > c.peso_real ? 'volumétrico' : 'real';
    $('peso-detalle').innerHTML = '<br>Real: <strong>' + c.peso_real + ' kg</strong> · Volumétrico: <strong>' +
      c.peso_volumetrico + ' kg</strong> → se cobra el ' + facturable + ': <strong>' + c.peso_facturable + ' kg</strong>.';
  }

  function actualizarCajas(disponibles) {
    var recomendada = null;
    document.querySelectorAll('[data-caja]').forEach(function (label) {
      var id = label.dataset.caja;
      var input = label.querySelector('input');
      var cabe = disponibles.indexOf(id) !== -1;
      input.disabled = !cabe;
      label.querySelector('[data-no-cabe]').classList.toggle('d-none', cabe);
      var tag = label.querySelector('.tag');
      if (tag) tag.remove();
      if (cabe && id !== 'propia' && !recomendada) recomendada = label;
      if (!cabe && input.checked) {
        input.checked = false;
        form.querySelector('[data-caja="propia"] input').checked = true;
      }
    });
    if (recomendada) {
      var t = document.createElement('span');
      t.className = 'tag';
      t.textContent = 'Recomendada';
      recomendada.appendChild(t);
    }
  }

  form.addEventListener('input', function (e) {
    if (['peso', 'largo', 'ancho', 'alto'].indexOf(e.target.id) !== -1) programarCotizacion();
  });
  form.addEventListener('change', function (e) {
    if (['caja', 'tipo_envio', 'compensar'].indexOf(e.target.name) !== -1) programarCotizacion();
  });

  // ---------------------------------------------------------------- Google Maps
  var lugares = { origen: null, destino: null };
  var mapsCaido = false;

  function mapaNoDisponible() {
    mapsCaido = true;
    $('mapa').innerHTML = '<div class="text-center px-3"><i class="fa-solid fa-map fa-2x mb-2" style="color: var(--b-300)"></i><br>' +
      'El mapa no está disponible en este momento. Puedes continuar: calcularemos la distancia al enviar el formulario.</div>';
    $('ruta-info').textContent = '';
  }
  // Google llama a esta función si la API key es rechazada (facturación, dominio no autorizado, etc.)
  window.gm_authFailure = mapaNoDisponible;
  var mapa = null, renderer = null;

  ['origen', 'destino'].forEach(function (campo) {
    $(campo).addEventListener('input', function () {
      // si el usuario edita a mano, la ruta anterior deja de ser válida
      lugares[campo] = null;
      $(campo + '_lat').value = '';
      $(campo + '_lng').value = '';
      if ($('distancia_ruta_km').value) {
        $('distancia_ruta_km').value = '';
        $('ruta-info').textContent = '';
        programarCotizacion();
      }
    });
  });

  window.balulaInitMaps = function () {
    var opciones = { fields: ['geometry', 'formatted_address'], componentRestrictions: { country: 'mx' } };
    ['origen', 'destino'].forEach(function (campo) {
      var input = $(campo);
      var auto = new google.maps.places.Autocomplete(input, opciones);
      input.addEventListener('keydown', function (e) { if (e.key === 'Enter') e.preventDefault(); });
      auto.addListener('place_changed', function () {
        var lugar = auto.getPlace();
        if (!lugar.geometry || !lugar.geometry.location) return;
        lugares[campo] = lugar.geometry.location;
        if (lugar.formatted_address) input.value = lugar.formatted_address;
        $(campo + '_lat').value = lugar.geometry.location.lat();
        $(campo + '_lng').value = lugar.geometry.location.lng();
        input.classList.remove('is-invalid');
        trazarRuta();
      });
    });
    // si se vuelve con direcciones ya escritas (p. ej. desde "Modificar"), trazar con texto
    if ($('origen').value && $('destino').value) trazarRuta(true);
  };

  function trazarRuta(usarTexto) {
    var o = lugares.origen || (usarTexto && $('origen').value);
    var d = lugares.destino || (usarTexto && $('destino').value);
    if (!o || !d || mapsCaido) return;
    if (!mapa) {
      $('mapa').innerHTML = '';
      mapa = new google.maps.Map($('mapa'), {
        center: { lat: 19.4326, lng: -99.1332 }, zoom: 6,
        disableDefaultUI: true, zoomControl: true
      });
      renderer = new google.maps.DirectionsRenderer({
        map: mapa, polylineOptions: { strokeColor: '#FD3700', strokeWeight: 5, strokeOpacity: .85 }
      });
    }
    new google.maps.DirectionsService().route(
      { origin: o, destination: d, travelMode: google.maps.TravelMode.DRIVING, region: 'mx' },
      function (res, estado) {
        if (estado === 'REQUEST_DENIED' || estado === 'OVER_QUERY_LIMIT') { mapaNoDisponible(); return; }
        if (estado !== 'OK') {
          $('ruta-info').innerHTML = '<span class="text-danger"><i class="fa-solid fa-triangle-exclamation me-1"></i>No encontramos una ruta por carretera entre esas direcciones.</span>';
          return;
        }
        renderer.setDirections(res);
        var tramo = res.routes[0].legs[0];
        $('distancia_ruta_km').value = (tramo.distance.value / 1000).toFixed(1);
        $('ruta-info').innerHTML = '<i class="fa-solid fa-route text-brand me-1"></i>' + tramo.distance.text +
          ' por carretera · aprox. ' + tramo.duration.text + ' de manejo';
        programarCotizacion();
      });
  }

  if (cfg.mapsKey) {
    var s = document.createElement('script');
    s.src = 'https://maps.googleapis.com/maps/api/js?key=' + encodeURIComponent(cfg.mapsKey) +
      '&libraries=places&language=es&region=MX&loading=async&callback=balulaInitMaps';
    s.async = true;
    s.onerror = mapaNoDisponible;
    document.head.appendChild(s);
  } else {
    $('mapa').innerHTML = '<div class="text-center px-3">Mapa no disponible. El precio por distancia se calculará al continuar.</div>';
  }

  programarCotizacion();
})();
