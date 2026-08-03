// theme-init.js — set the theme attribute before first paint (no FOUC).
// Loaded synchronously in <head>; keep this file dependency-free.
(function () {
  var t = localStorage.getItem('lg-theme');
  if (!t) {
    t = (window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches) ? 'light' : 'dark';
  }
  document.documentElement.setAttribute('data-theme', t);
})();
