/* Balula Express — portal del repartidor: mapas (Leaflet + OpenStreetMap), GPS, firma y rutas alternas */
(function () {
  'use strict';
  var NARANJA = '#FD3700';
  var COLOR_RIESGO = { alto: '#C42B1C', medio: '#B26A00', bajo: '#1E8A4C' };

  function mapaBase(id, centro, zoom) {
    var el = document.getElementById(id);
    if (!el || !window.L) return null;
    el.innerHTML = '';
    var m = L.map(el, { scrollWheelZoom: false }).setView(centro, zoom || 12);
    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
      maxZoom: 19, attribution: '&copy; OpenStreetMap'
    }).addTo(m);
    return m;
  }

  function marcadorNumero(lat, lng, texto, color) {
    return L.marker([lat, lng], {
      icon: L.divIcon({ className: 'pin-num', html: '<span style="background:' + color + '">' + texto + '</span>', iconSize: [30, 30], iconAnchor: [15, 15] })
    });
  }

  // ---------------------------------------------------- Mapa de la ruta del día
  var cfg = window.BALULA_RUTA;
  if (cfg) {
    var mapa = mapaBase('mapa-ruta', cfg.inicio, 12);
    if (mapa) {
      var puntos = [cfg.inicio];
      marcadorNumero(cfg.inicio[0], cfg.inicio[1], '<i class="fa-solid fa-truck"></i>', '#3D1405').addTo(mapa).bindPopup('Punto de salida');
      cfg.paradas.forEach(function (p) {
        puntos.push([p.lat, p.lng]);
        marcadorNumero(p.lat, p.lng, p.num, COLOR_RIESGO[p.riesgo] || NARANJA).addTo(mapa)
          .bindPopup('<strong>' + p.num + '. ' + (p.nombre || '').replace(/</g, '&lt;') + '</strong><br><a href="' + p.url + '">Ver entrega</a>');
      });
      if (puntos.length > 1) {
        L.polyline(puntos, { color: NARANJA, weight: 4, opacity: .7, dashArray: '6 8' }).addTo(mapa);
        mapa.fitBounds(puntos, { padding: [30, 30] });
      }
    }
  }

  // ---------------------------------------------------- GPS
  var ultimaPos = null;
  function rellenarUbicacion() {
    if (!ultimaPos) return;
    document.querySelectorAll('form.con-ubicacion').forEach(function (f) {
      f.querySelector('[name=lat]').value = ultimaPos.lat;
      f.querySelector('[name=lng]').value = ultimaPos.lng;
    });
  }
  var ultimoEnvio = 0;
  if (navigator.geolocation) {
    navigator.geolocation.watchPosition(function (pos) {
      ultimaPos = { lat: pos.coords.latitude, lng: pos.coords.longitude, precision: pos.coords.accuracy };
      rellenarUbicacion();
      // Comparte la posición con operaciones y con el rastreo en vivo del cliente (cada 60 s máx.)
      var urlUbic = (cfg && cfg.ubicacionUrl) || '/repartidor/ubicacion';
      var csrf = (cfg && cfg.csrf) || (document.querySelector('[name=_csrf]') || {}).value;
      if (csrf && Date.now() - ultimoEnvio > 60000) {
        ultimoEnvio = Date.now();
        fetch(urlUbic, { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf }, body: JSON.stringify(ultimaPos) });
      }
    }, function () { /* sin permiso: se sigue sin ubicación */ }, { enableHighAccuracy: true, maximumAge: 30000, timeout: 20000 });
  }

  // ---------------------------------------------------- Firma
  var lienzo = document.getElementById('firma');
  if (lienzo) {
    var ctx = lienzo.getContext('2d');
    var dibujando = false, hayFirma = false;
    ctx.lineWidth = 3; ctx.lineCap = 'round'; ctx.strokeStyle = '#261812';
    function punto(e) {
      var r = lienzo.getBoundingClientRect();
      return { x: (e.clientX - r.left) * lienzo.width / r.width, y: (e.clientY - r.top) * lienzo.height / r.height };
    }
    lienzo.addEventListener('pointerdown', function (e) { dibujando = true; var p = punto(e); ctx.beginPath(); ctx.moveTo(p.x, p.y); lienzo.setPointerCapture(e.pointerId); });
    lienzo.addEventListener('pointermove', function (e) { if (!dibujando) return; var p = punto(e); ctx.lineTo(p.x, p.y); ctx.stroke(); hayFirma = true; });
    ['pointerup', 'pointercancel'].forEach(function (t) { lienzo.addEventListener(t, function () { dibujando = false; }); });
    document.getElementById('borrar-firma').addEventListener('click', function () { ctx.clearRect(0, 0, lienzo.width, lienzo.height); hayFirma = false; });
    document.getElementById('form-entrega').addEventListener('submit', function (e) {
      if (!hayFirma) { e.preventDefault(); alert('Falta la firma de quien recibe.'); return; }
      document.getElementById('firma-dato').value = lienzo.toDataURL('image/png');
    });
  }

  // ---------------------------------------------------- Rutas alternas
  var botonRutas = document.getElementById('ver-rutas');
  if (botonRutas) {
    var caja = document.getElementById('mapa-detalle');
    var destino = [parseFloat(caja.dataset.lat), parseFloat(caja.dataset.lng)];
    var mapaD = mapaBase('mapa-detalle', destino, 14);
    if (mapaD) marcadorNumero(destino[0], destino[1], '<i class="fa-solid fa-house"></i>', NARANJA).addTo(mapaD);
    var capas = [];
    botonRutas.addEventListener('click', function () {
      botonRutas.disabled = true;
      botonRutas.textContent = 'Calculando…';
      fetch(botonRutas.dataset.url, { credentials: 'same-origin' }).then(function (r) { return r.json(); }).then(function (d) {
        var lista = document.getElementById('lista-rutas');
        lista.innerHTML = '';
        if (!d.ok) { lista.innerHTML = '<li><span>' + d.error + '</span></li>'; return; }
        capas.forEach(function (c) { mapaD.removeLayer(c); });
        var todos = [];
        d.rutas.slice().reverse().forEach(function (r) {
          var capa = L.polyline(r.geometria, { color: r.recomendada ? NARANJA : '#8C7D76', weight: r.recomendada ? 6 : 4, opacity: r.recomendada ? .9 : .6 }).addTo(mapaD);
          capas.push(capa); todos = todos.concat(r.geometria);
        });
        if (todos.length) mapaD.fitBounds(todos, { padding: [20, 20] });
        d.rutas.forEach(function (r) {
          lista.insertAdjacentHTML('beforeend', '<li><span>' + (r.recomendada ? '<i class="fa-solid fa-star"></i>' : '<i class="fa-solid fa-route"></i>') +
            r.nombre + (r.recomendada ? ' <strong>(recomendada)</strong>' : '') + '</span><span>' + r.km + ' km · ' + r.min + ' min</span></li>');
        });
        lista.insertAdjacentHTML('beforeend', '<li><span class="small text-muted">Tráfico estimado ×' + d.trafico + ' para esta hora · fuente: ' +
          (d.fuente === 'osrm' ? 'OpenStreetMap' : 'estimación') + '</span><span></span></li>');
      }).finally(function () { botonRutas.disabled = false; botonRutas.textContent = 'Recalcular'; });
    });
  }
})();
