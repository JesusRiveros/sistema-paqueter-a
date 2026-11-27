/* Balula Express — widget del asistente Lula (chat de texto) */
(function () {
  'use strict';
  var raiz = document.getElementById('lula');
  if (!raiz) return;

  var $ = function (id) { return document.getElementById(id); };
  var panel = $('lula-panel'), lista = $('lula-mensajes'), form = $('lula-form'), entrada = $('lula-texto');
  var abrirBtn = $('lula-abrir');
  var cargado = false, ocupado = false;
  var SALUDO = '¡Hola! Soy Lula, de Balula Express 👋 ¿En qué te puedo ayudar hoy? Puedo rastrear tu paquete, cotizar un envío o ayudarte con cualquier problema.';

  function escapar(t) {
    var d = document.createElement('div');
    d.textContent = t;
    return d.innerHTML;
  }

  // Formato mínimo y seguro: **negritas**, saltos de línea y guías BLL- como enlace al rastreo.
  function formatear(t) {
    return escapar(t)
      .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
      .replace(/\b(BLL-[0-9A-F]{8})\b/g, '<a href="/rastreo?guia=$1">$1</a>')
      .replace(/(^|\s|\()(\/evidencia\/[A-Za-z0-9_-]{10,})/g, '$1<a href="$2" target="_blank" rel="noopener">Subir foto aquí</a>')
      .replace(/\n/g, '<br>');
  }

  function burbuja(rol, texto, extra) {
    var div = document.createElement('div');
    div.className = 'lula-msg ' + (rol === 'cliente' ? 'yo' : 'ella') + (extra ? ' ' + extra : '');
    div.innerHTML = formatear(texto);
    lista.appendChild(div);
    lista.scrollTop = lista.scrollHeight;
    return div;
  }

  function escribiendo(mostrar) {
    var el = $('lula-escribiendo');
    if (mostrar && !el) {
      el = document.createElement('div');
      el.id = 'lula-escribiendo';
      el.className = 'lula-msg ella lula-typing';
      el.setAttribute('aria-label', 'Lula está escribiendo');
      el.innerHTML = '<span></span><span></span><span></span>';
      lista.appendChild(el);
      lista.scrollTop = lista.scrollHeight;
    } else if (!mostrar && el) {
      el.remove();
    }
  }

  function cargarHistorial() {
    if (cargado) return Promise.resolve();
    cargado = true;
    return fetch(raiz.dataset.historial, { credentials: 'same-origin' })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        lista.innerHTML = '';
        burbuja('asistente', SALUDO);
        (d.mensajes || []).forEach(function (m) { burbuja(m.rol, m.texto); });
        $('lula-sugerencias').hidden = (d.mensajes || []).length > 0;
      })
      .catch(function () { burbuja('asistente', SALUDO); });
  }

  function abrir() {
    panel.hidden = false;
    raiz.classList.add('abierto');
    abrirBtn.setAttribute('aria-expanded', 'true');
    cargarHistorial().then(function () { entrada.focus(); });
  }

  function cerrar() {
    panel.hidden = true;
    raiz.classList.remove('abierto');
    abrirBtn.setAttribute('aria-expanded', 'false');
    abrirBtn.focus();
  }

  function enviar(texto, canal) {
    canal = canal || 'web';
    texto = (texto || '').trim();
    if (!texto || ocupado) return;
    ocupado = true;
    $('lula-sugerencias').hidden = true;
    burbuja('cliente', texto);
    entrada.value = '';
    ajustarAltura();
    escribiendo(true);
    fetch(raiz.dataset.api, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': raiz.dataset.csrf },
      body: JSON.stringify({ texto: texto, canal: canal })
    })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        escribiendo(false);
        if (!d.ok) {
          var err = d.error || 'No pude responder. Intenta de nuevo.';
          burbuja('asistente', err, 'error');
          if (canal === 'voz') hablar(err);
          return;
        }
        burbuja('asistente', d.texto);
        if (d.escalado) burbuja('asistente', 'Tu caso ya está con una persona del equipo.', 'nota');
        if (canal === 'voz') hablar(d.texto, manosLibres ? escuchar : null);
      })
      .catch(function () {
        escribiendo(false);
        burbuja('asistente', 'Parece que se perdió la conexión. Revisa tu internet e intenta de nuevo.', 'error');
      })
      .finally(function () { ocupado = false; if (canal !== 'voz') entrada.focus(); });
  }

  function ajustarAltura() {
    entrada.style.height = 'auto';
    entrada.style.height = Math.min(entrada.scrollHeight, 120) + 'px';
  }

  abrirBtn.addEventListener('click', function () { panel.hidden ? abrir() : cerrar(); });
  $('lula-cerrar').addEventListener('click', cerrar);
  $('lula-nueva').addEventListener('click', function () {
    fetch(raiz.dataset.reiniciar, { method: 'POST', credentials: 'same-origin', headers: { 'X-CSRF-Token': raiz.dataset.csrf } })
      .finally(function () { cargado = false; $('lula-sugerencias').hidden = false; cargarHistorial(); });
  });
  form.addEventListener('submit', function (e) { e.preventDefault(); enviar(entrada.value); });
  entrada.addEventListener('keydown', function (e) {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); enviar(entrada.value); }
  });
  entrada.addEventListener('input', ajustarAltura);
  panel.addEventListener('keydown', function (e) { if (e.key === 'Escape') cerrar(); });
  $('lula-sugerencias').addEventListener('click', function (e) {
    var b = e.target.closest('button[data-texto]');
    if (b) enviar(b.dataset.texto);
  });

  // ------------------------------------------------------------------ Voz (gratis, en el navegador)
  // Voz a texto: Web Speech API (Chrome, Edge, Safari). Texto a voz: speechSynthesis (todos).
  var Reconocimiento = window.SpeechRecognition || window.webkitSpeechRecognition;
  var sintesis = window.speechSynthesis;
  var reconocedor = null, escuchando = false, manosLibres = false;
  var micBtn = $('lula-mic'), vozBtn = $('lula-voz');

  function vozPreferida() {
    if (!sintesis) return null;
    var voces = sintesis.getVoices().filter(function (v) { return /^es(-|_)/i.test(v.lang); });
    var puntaje = function (v) {
      var p = 0;
      if (/es(-|_)MX/i.test(v.lang)) p += 10;
      if (/natural|neural|online/i.test(v.name)) p += 6;   // voces neuronales (p. ej. "Dalia" en Edge)
      if (/dalia|paulina|google/i.test(v.name)) p += 3;
      if (/es(-|_)US/i.test(v.lang)) p += 2;
      return p;
    };
    return voces.sort(function (a, b) { return puntaje(b) - puntaje(a); })[0] || null;
  }

  function textoParaHablar(t) {
    return t.replace(/\*\*/g, '')
      .replace(/\/evidencia\/[A-Za-z0-9_-]+/g, 'el enlace que te dejé en el chat')
      .replace(/https?:\/\/\S+/g, 'el enlace del chat')
      .replace(/\bBLL-([0-9A-F]{4})([0-9A-F]{4})\b/g, function (_, a, b) { return 'B L L, ' + a.split('').join(' ') + ', ' + b.split('').join(' '); })
      .replace(/[•·#>_`]/g, ' ');
  }

  function hablar(texto, alTerminar) {
    if (!sintesis) { if (alTerminar) alTerminar(); return; }
    sintesis.cancel();
    var u = new SpeechSynthesisUtterance(textoParaHablar(texto));
    var v = vozPreferida();
    if (v) u.voice = v;
    u.lang = v ? v.lang : 'es-MX';
    u.rate = 1.03;
    u.onstart = function () { $('lula-estado').textContent = 'Hablando…'; };
    u.onend = u.onerror = function () {
      $('lula-estado').textContent = manosLibres ? 'Conversación por voz activa' : 'Atención a clientes · en línea';
      if (alTerminar && manosLibres) alTerminar();
    };
    sintesis.speak(u);
  }

  function mostrarEscucha(si, texto) {
    $('lula-escuchando').hidden = !si;
    micBtn.classList.toggle('activo', si);
    if (texto !== undefined) $('lula-parcial').textContent = texto;
  }

  function escuchar() {
    if (!Reconocimiento || escuchando || ocupado) return;
    if (sintesis) sintesis.cancel();  // si Lula está hablando, se le puede interrumpir
    reconocedor = new Reconocimiento();
    reconocedor.lang = 'es-MX';
    reconocedor.interimResults = true;
    reconocedor.continuous = false;
    var final = '';
    reconocedor.onresult = function (e) {
      var parcial = '';
      for (var i = e.resultIndex; i < e.results.length; i++) {
        if (e.results[i].isFinal) final += e.results[i][0].transcript;
        else parcial += e.results[i][0].transcript;
      }
      mostrarEscucha(true, (final + ' ' + parcial).trim() || 'Te escucho…');
    };
    reconocedor.onerror = function (e) {
      if (e.error === 'not-allowed' || e.error === 'service-not-allowed') {
        manosLibres = false; actualizarVozBtn();
        burbuja('asistente', 'No tengo permiso para usar el micrófono. Actívalo en tu navegador o escríbeme.', 'error');
      }
    };
    reconocedor.onend = function () {
      escuchando = false;
      mostrarEscucha(false);
      if (final.trim()) enviar(final, 'voz');
      else if (manosLibres) setTimeout(escuchar, 400);  // silencio: volver a escuchar
    };
    escuchando = true;
    mostrarEscucha(true, 'Te escucho…');
    try { reconocedor.start(); } catch (err) { escuchando = false; mostrarEscucha(false); }
  }

  function detenerTodo() {
    manosLibres = false;
    actualizarVozBtn();
    if (reconocedor && escuchando) reconocedor.abort();
    if (sintesis) sintesis.cancel();
    mostrarEscucha(false);
  }

  function actualizarVozBtn() {
    vozBtn.setAttribute('aria-pressed', manosLibres ? 'true' : 'false');
    vozBtn.classList.toggle('activo', manosLibres);
    $('lula-estado').textContent = manosLibres ? 'Conversación por voz activa' : 'Atención a clientes · en línea';
  }

  if (Reconocimiento) {
    micBtn.hidden = false;
    vozBtn.hidden = false;
    if (sintesis && sintesis.onvoiceschanged !== undefined) sintesis.onvoiceschanged = function () {};
    micBtn.addEventListener('click', function () {
      if (escuchando) { reconocedor.stop(); return; }
      escuchar();
    });
    vozBtn.addEventListener('click', function () {
      if (manosLibres) { detenerTodo(); return; }
      manosLibres = true;
      actualizarVozBtn();
      hablar('Hola, soy Lula. Te escucho, ¿en qué te ayudo?', escuchar);
    });
  }
  $('lula-cerrar').addEventListener('click', detenerTodo);
  document.querySelectorAll('[data-abrir-lula]').forEach(function (el) {
    el.addEventListener('click', function (e) { e.preventDefault(); abrir(); });
  });
})();
