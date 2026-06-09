/* game.js — slot machine spin, film selection, draft API calls */

// ── State ────────────────────────────────────────────────────────────────────
let selectedFilm     = null;
let currentPool      = [];
let currentOpenSlots = [];
let drafting         = false;

// ── DOM refs ─────────────────────────────────────────────────────────────────
const phaseSpin  = document.getElementById('phase-spin');
const phaseDraft = document.getElementById('phase-draft');
const phaseDone  = document.getElementById('phase-done');
const btnSpin    = document.getElementById('btn-spin');
const filmPool   = document.getElementById('film-pool');
const slotPicker = document.getElementById('slot-picker');
const pickerSlots = document.getElementById('picker-slots');
const pickerTitle = document.getElementById('picker-film-name');
const btnCancelPick = document.getElementById('btn-cancel-pick');
const spinResult = document.getElementById('spin-result');

// Reel elements
const stripEra    = document.getElementById('strip-era');
const stripStudio = document.getElementById('strip-studio');

// ── Reel animation ───────────────────────────────────────────────────────────
const ERA_LIST    = ['70s','80s','90s','00s','10s','20s'];
const STUDIO_LIST = ['Disney','Warner Bros','Universal','Paramount','Sony','Fox','MGM/UA','Indie'];

// Maps DB studio names → reel display labels
const STUDIO_DISPLAY = {
  'Disney':           'Disney',
  'Warner Brothers':  'Warner Bros',
  'Universal':        'Universal',
  'Paramount':        'Paramount',
  'Sony/Columbia':    'Sony',
  '20th Century Fox': 'Fox',
  'MGM/UA':           'MGM/UA',
  'Independent':      'Indie',
};
const ITEM_H = 64; // px — must match CSS .reel-item height

function animateReel(strip, items, finalIdx, duration = 1800) {
  return new Promise(resolve => {
    const total = items.length;
    let startTime = null;
    const spins   = 3;  // full loops before landing
    const totalItems = spins * total + finalIdx;
    const totalPx    = totalItems * ITEM_H;

    // Clone enough items so the strip is long enough
    strip.innerHTML = '';
    const needed = totalItems + 2;
    for (let i = 0; i < needed; i++) {
      const div = document.createElement('div');
      div.className = 'reel-item';
      div.textContent = items[i % total];
      strip.appendChild(div);
    }
    strip.style.transform = 'translateY(0)';

    function easeOut(t) { return 1 - Math.pow(1 - t, 3); }

    function step(ts) {
      if (!startTime) startTime = ts;
      const elapsed = ts - startTime;
      const progress = Math.min(elapsed / duration, 1);
      const eased    = easeOut(progress);
      const px       = Math.round(eased * totalPx);
      strip.style.transform = `translateY(-${px}px)`;
      if (progress < 1) {
        requestAnimationFrame(step);
      } else {
        resolve();
      }
    }
    requestAnimationFrame(step);
  });
}

// ── Respin button ────────────────────────────────────────────────────────────
const btnRespin = document.getElementById('btn-respin');
if (btnRespin) {
  btnRespin.addEventListener('click', async () => {
    btnRespin.disabled = true;
    btnRespin.innerHTML = '<span>Spinning…</span>';

    try {
      const res  = await fetch('/game/respin', { method: 'POST' });
      const data = await res.json();

      if (data.error) {
        toast(data.error, 'error');
        btnRespin.disabled = false;
        btnRespin.innerHTML = '🎲 Respin <span class="respin-badge">1×</span>';
        return;
      }

      // Show spin phase so the animation is visible
      phaseDraft.style.display = 'none';
      phaseSpin.style.display  = '';

      const eraIdx    = ERA_LIST.indexOf(data.era);
      const studioIdx = STUDIO_LIST.indexOf(STUDIO_DISPLAY[data.studio] ?? data.studio);

      await Promise.all([
        animateReel(stripEra,    ERA_LIST,    eraIdx    >= 0 ? eraIdx    : 0),
        animateReel(stripStudio, STUDIO_LIST, studioIdx >= 0 ? studioIdx : 0),
      ]);

      // Switch back to draft with new pool
      phaseSpin.style.display  = 'none';
      phaseDraft.style.display = '';

      currentPool      = data.pool;
      currentOpenSlots = data.open_slots;

      spinResult.innerHTML = `
        <span class="result-era">${data.era}</span>
        <span class="result-sep">×</span>
        <span class="result-studio">${data.studio}</span>
      `;

      renderPool(data.pool);
      btnRespin.disabled = true;
      btnRespin.classList.add('respin-spent');
      btnRespin.innerHTML = '🎲 Respin <span class="respin-badge">Used</span>';

    } catch (err) {
      toast('Network error — please try again.', 'error');
      btnRespin.disabled = false;
      btnRespin.innerHTML = '🎲 Respin <span class="respin-badge">1×</span>';
    }
  });
}

// ── Spin button ───────────────────────────────────────────────────────────────
if (btnSpin) {
  btnSpin.addEventListener('click', async () => {
    btnSpin.disabled = true;
    btnSpin.innerHTML = '<span>Spinning…</span>';

    try {
      const res  = await fetch('/game/spin', { method: 'POST' });
      const data = await res.json();

      if (data.error) {
        toast(data.error, 'error');
        btnSpin.disabled = false;
        btnSpin.innerHTML = '<span>🎰 Spin the Reels</span>';
        return;
      }

      // Find the correct final index for the landed value
      const eraIdx    = ERA_LIST.indexOf(data.era);
      const studioIdx = STUDIO_LIST.indexOf(STUDIO_DISPLAY[data.studio] ?? data.studio);

      await Promise.all([
        animateReel(stripEra,    ERA_LIST,    eraIdx    >= 0 ? eraIdx    : 0),
        animateReel(stripStudio, STUDIO_LIST, studioIdx >= 0 ? studioIdx : 0),
      ]);

      // Store for later
      currentPool      = data.pool;
      currentOpenSlots = data.open_slots;

      // Update spin result badge
      spinResult.innerHTML = `
        <span class="result-era">${data.era}</span>
        <span class="result-sep">×</span>
        <span class="result-studio">${data.studio}</span>
      `;

      // Render film pool
      renderPool(data.pool);

      // Switch phase
      phaseSpin.style.display  = 'none';
      phaseDraft.style.display = '';

      // Enable respin now that a spin has happened
      if (btnRespin && !btnRespin.classList.contains('respin-spent')) {
        btnRespin.disabled = false;
      }

    } catch (err) {
      toast('Network error — please try again.', 'error');
      btnSpin.disabled = false;
      btnSpin.innerHTML = '<span>🎰 Spin the Reels</span>';
    }
  });
}

// ── Render film pool ──────────────────────────────────────────────────────────
function renderPool(pool) {
  if (!filmPool) return;
  if (!pool.length) {
    filmPool.innerHTML = '<p style="color:var(--smoke);text-align:center;padding:40px">No films available for this combination.</p>';
    return;
  }
  filmPool.innerHTML = pool.map(film => `
    <div class="film-card" data-id="${film.id}" data-genres="${film.genre_str || ''}">
      ${film.poster_url
        ? `<img src="${film.poster_url}" alt="${escHtml(film.title)}" class="film-poster" loading="lazy">`
        : `<div class="film-poster-placeholder">🎬</div>`}
      <div class="film-card-body">
        <h3 class="film-title">${escHtml(film.title)}</h3>
        <p class="film-meta">${film.year} · ${escHtml(film.studio || '')}</p>
        <div class="film-tags">
          ${(film.genre_tags || film.genre_str?.split('|') || []).map(t => `<span class="tag">${escHtml(t)}</span>`).join('')}
        </div>
        ${film.gross_m != null ? `<div class="film-financials"><span class="gross">$${fmt(film.gross_m)}M worldwide</span></div>` : ''}
      </div>
      <button class="btn-select" data-id="${film.id}">Select</button>
    </div>
  `).join('');

  // Attach click listeners
  filmPool.querySelectorAll('.film-card, .btn-select').forEach(el => {
    el.addEventListener('click', e => {
      const card = el.closest('.film-card') || el;
      const id   = parseInt(card.dataset.id);
      selectFilm(id);
    });
  });
}

// ── Film selection ────────────────────────────────────────────────────────────
function selectFilm(filmId) {
  selectedFilm = currentPool.find(f => f.id === filmId);
  if (!selectedFilm) return;

  // Highlight selected card
  filmPool.querySelectorAll('.film-card').forEach(c => {
    c.classList.toggle('selected', parseInt(c.dataset.id) === filmId);
  });

  // Open slot picker
  openSlotPicker(selectedFilm);
}

function openSlotPicker(film) {
  pickerTitle.textContent = film.title;
  const filmGenres = (film.genre_tags || film.genre_str?.split('|') || []);

  pickerSlots.innerHTML = currentOpenSlots.map(slot => {
    const eligible = slot.genre === null || filmGenres.includes(slot.genre);
    return `
      <button class="picker-slot-btn ${eligible ? '' : 'disabled'}"
              data-slot="${slot.slot_number}"
              ${eligible ? '' : 'disabled'}>
        <span>${slot.icon || '🎬'}</span>
        <span>${escHtml(slot.label)}</span>
        ${eligible ? '' : '<span style="margin-left:auto;font-size:.72rem;opacity:.6">Not eligible</span>'}
      </button>
    `;
  }).join('');

  pickerSlots.querySelectorAll('.picker-slot-btn:not(.disabled)').forEach(btn => {
    btn.addEventListener('click', () => draftFilm(film.id, parseInt(btn.dataset.slot)));
  });

  slotPicker.style.display = 'flex';
}

if (btnCancelPick) {
  btnCancelPick.addEventListener('click', () => {
    slotPicker.style.display = 'none';
    selectedFilm = null;
    filmPool.querySelectorAll('.film-card').forEach(c => c.classList.remove('selected'));
  });
}

// Close picker on backdrop click
if (slotPicker) {
  slotPicker.addEventListener('click', e => {
    if (e.target === slotPicker) btnCancelPick.click();
  });
}

// ── Draft API call ────────────────────────────────────────────────────────────
async function draftFilm(filmId, slotNumber) {
  if (drafting) return;
  drafting = true;
  slotPicker.style.display = 'none';

  try {
    const res  = await fetch('/game/draft', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ film_id: filmId, slot_number: slotNumber }),
    });
    const data = await res.json();

    if (data.error) {
      toast(data.error, 'error');
      drafting = false;
      return;
    }

    if (data.phase === 'done') {
      window.location.href = '/game/score';
      return;
    }

    window.location.reload();

  } catch (err) {
    toast('Network error — please try again.', 'error');
    drafting = false;
  }
}

// ── Helpers ───────────────────────────────────────────────────────────────────
function fmt(n) {
  if (n == null) return '—';
  return Number(n).toLocaleString('en-US', { maximumFractionDigits: 0 });
}
function escHtml(str) {
  return String(str ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
