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

  function enviar(texto) {
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
      body: JSON.stringify({ texto: texto, canal: 'web' })
    })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        escribiendo(false);
        if (!d.ok) { burbuja('asistente', d.error || 'No pude responder. Intenta de nuevo.', 'error'); return; }
        burbuja('asistente', d.texto);
        if (d.escalado) burbuja('asistente', 'Tu caso ya está con una persona del equipo.', 'nota');
      })
      .catch(function () {
        escribiendo(false);
        burbuja('asistente', 'Parece que se perdió la conexión. Revisa tu internet e intenta de nuevo.', 'error');
      })
      .finally(function () { ocupado = false; entrada.focus(); });
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
  document.querySelectorAll('[data-abrir-lula]').forEach(function (el) {
    el.addEventListener('click', function (e) { e.preventDefault(); abrir(); });
  });
})();
