// main.js — shared utilities (bulb animation, toast)

// Stagger bulb animation so they don't all blink in sync
document.querySelectorAll('.bulb.on').forEach((b, i) => {
  b.style.animationDelay = `${(i * 0.15) % 3}s`;
});

// Toast helper exposed globally
window.toast = function(msg, type = '') {
  const el = document.getElementById('toast');
  if (!el) return;
  el.textContent = msg;
  el.className = 'toast show ' + type;
  clearTimeout(el._t);
  el._t = setTimeout(() => { el.className = 'toast'; }, 3200);
};
