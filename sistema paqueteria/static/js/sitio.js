/* Balula Express — comportamiento común del sitio */
(function () {
  'use strict';

  // Animación de entrada al hacer scroll
  var elementos = document.querySelectorAll('.reveal');
  if ('IntersectionObserver' in window && elementos.length) {
    var obs = new IntersectionObserver(function (entradas) {
      entradas.forEach(function (e) {
        if (e.isIntersecting) { e.target.classList.add('visible'); obs.unobserve(e.target); }
      });
    }, { threshold: 0.12, rootMargin: '0px 0px -40px 0px' });
    elementos.forEach(function (el) { obs.observe(el); });
  } else {
    elementos.forEach(function (el) { el.classList.add('visible'); });
  }

  // Pestañas simples (tarjeta rápida de la portada)
  document.querySelectorAll('[data-tabs]').forEach(function (grupo) {
    var botones = grupo.querySelectorAll('[data-tab]');
    botones.forEach(function (btn) {
      btn.addEventListener('click', function () {
        botones.forEach(function (b) {
          var activo = b === btn;
          b.classList.toggle('active', activo);
          b.setAttribute('aria-selected', activo ? 'true' : 'false');
          document.getElementById(b.dataset.tab).hidden = !activo;
        });
      });
    });
  });

  // Validación nativa con estilos de Bootstrap
  document.querySelectorAll('form.needs-validation').forEach(function (form) {
    form.addEventListener('submit', function (ev) {
      if (!form.checkValidity()) {
        ev.preventDefault();
        ev.stopPropagation();
        var primero = form.querySelector(':invalid');
        if (primero) primero.focus();
      } else {
        var btn = form.querySelector('[type=submit][data-loading]');
        if (btn) {
          btn.disabled = true;
          btn.innerHTML = '<span class="spinner-border spinner-border-sm me-2" aria-hidden="true"></span>' + btn.dataset.loading;
        }
      }
      form.classList.add('was-validated');
    });
  });
})();
