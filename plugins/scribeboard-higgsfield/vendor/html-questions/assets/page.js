// html-questions page script. Inlined into every generated page after
// `window.HQ = {...}` (page, gate, submitted); --mobile pages link it as
// page.js after <stem>.config.js instead. Works from file:// with no
// server: side files (gate manifest, submitted answers) load as <script>.
(function () {
  'use strict';
  var HQ = window.HQ || {};
  var body = document.body;
  var statusEl = document.getElementById('hqStatus');
  var copyBtn = document.getElementById('hqCopy');
  var pageUrl = new URL(window.location.href);
  var draftKey = 'html-questions:' + pageUrl.pathname;
  function say(m) { if (statusEl) statusEl.textContent = m; }
  function each(list, fn) { Array.prototype.forEach.call(list, fn); }
  function loadScript(src) {
    return new Promise(function (resolve, reject) {
      var s = document.createElement('script');
      var url = new URL(src, pageUrl);
      url.searchParams.set('t', String(Date.now()));
      s.src = url.href;
      s.onload = function () { resolve(url); };
      s.onerror = function () { reject(new Error('unreadable: ' + src)); };
      document.head.appendChild(s);
    });
  }
  function samePath(a, b) {
    try { return decodeURIComponent(a).toLowerCase() === decodeURIComponent(b).toLowerCase(); }
    catch (e) { return a === b; }
  }

  // ---- Theme toggle (dark default) ---------------------------------------
  var savedTheme = null;
  try { savedTheme = sessionStorage.getItem('html-questions-theme'); } catch (e) {}
  if (savedTheme) body.setAttribute('data-theme', savedTheme);
  document.getElementById('themeToggle').addEventListener('click', function () {
    var next = body.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
    body.setAttribute('data-theme', next);
    try { sessionStorage.setItem('html-questions-theme', next); } catch (e) {}
  });

  // ---- Contents sidebar -------------------------------------------------
  each(document.querySelectorAll('.toc-toggle'), function (t) {
    t.addEventListener('click', function (e) {
      e.preventDefault();
      t.closest('.toc-section').classList.toggle('collapsed');
    });
  });
  document.getElementById('tocExpandAll').addEventListener('click', function () {
    each(document.querySelectorAll('.toc-section'), function (s) { s.classList.remove('collapsed'); });
  });
  document.getElementById('tocCollapseAll').addEventListener('click', function () {
    each(document.querySelectorAll('.toc-section'), function (s) { s.classList.add('collapsed'); });
  });

  // ---- Gate rail: read the shared manifest at view time ------------------
  // Shown only when the chain has two or more gates, so a lone page looks
  // like a plain questionnaire. Older gate pages pick up newer gates here
  // without being re-rendered.
  var railRow = document.getElementById('blRailRow');
  var rail = document.getElementById('blRail');
  if (HQ.gate && HQ.gate.manifest && rail) {
    loadScript(HQ.gate.manifest).then(function (manifestUrl) {
      var man = window.BACKLOT;
      if (!man || !Array.isArray(man.stages)) { say('gate manifest has no stages'); return; }
      if (man.stages.length < 2) return;
      var stages = man.stages.slice().sort(function (a, b) { return (a.n || 0) - (b.n || 0); });
      var byHref = stages.some(function (s) {
        return s.href && samePath(new URL(s.href, manifestUrl).pathname, pageUrl.pathname);
      });
      stages.forEach(function (s) {
        var href = s.href ? new URL(s.href, manifestUrl) : null;
        var current = byHref
          ? !!(href && samePath(href.pathname, pageUrl.pathname))
          : (HQ.gate.n != null ? s.n === HQ.gate.n
            : String(s.label || '').toLowerCase() === String(HQ.gate.label || '').toLowerCase());
        var a = document.createElement(href ? 'a' : 'span');
        a.className = 'bl-gate' + (current ? ' bl-current' : '');
        if (href) a.href = href.href;
        var st = String(s.state || 'waiting');
        var dot = document.createElement('span');
        dot.className = 'bl-dot bl-dot-' + (/^(answered|accepted|skipped)$/.test(st) ? st : 'waiting');
        a.appendChild(dot);
        var num = document.createElement('span');
        num.className = 'bl-num';
        num.textContent = s.n != null ? String(s.n) : '';
        a.appendChild(num);
        a.appendChild(document.createTextNode(' ' + (s.label || '')));
        a.title = (s.label || '') + ' - ' + st;
        rail.appendChild(a);
      });
      railRow.hidden = false;
    }, function () { say('gate manifest unreadable'); });
  }

  // ---- Choices: whole-item click ----------------------------------------
  // On a phone an option's image fills the card and is already full width,
  // so tapping it picks the option instead of opening the lightbox.
  var phone = window.matchMedia('(max-width: 640px)');
  each(document.querySelectorAll('li.bl-item'), function (li) {
    li.addEventListener('click', function (e) {
      if (e.target.closest(phone.matches ? 'input, a, audio, video, textarea, button'
        : 'input, a, img, audio, video, textarea, button')) return;
      var inp = li.querySelector(':scope > input.bl-input');
      if (!inp) return;
      if (inp.type === 'checkbox') inp.checked = !inp.checked; else inp.checked = true;
      inp.dispatchEvent(new Event('change', { bubbles: true }));
    });
  });

  // ---- Image lightbox -----------------------------------------------------
  var lightbox = document.createElement('div');
  lightbox.className = 'bl-lightbox';
  lightbox.setAttribute('role', 'dialog');
  lightbox.setAttribute('aria-modal', 'true');
  lightbox.setAttribute('aria-label', 'Enlarged image');
  var lightboxImage = document.createElement('img');
  lightbox.appendChild(lightboxImage);
  body.appendChild(lightbox);
  function closeLightbox() {
    lightbox.classList.remove('bl-open');
    body.classList.remove('bl-lightbox-open');
  }
  function openLightbox(img) {
    lightboxImage.src = img.currentSrc || img.src;
    lightboxImage.alt = img.alt || '';
    lightbox.classList.add('bl-open');
    body.classList.add('bl-lightbox-open');
  }
  each(document.querySelectorAll('article.doc img'), function (img) {
    img.classList.add('bl-zoomable');
    img.setAttribute('tabindex', '0');
    img.addEventListener('click', function (e) {
      if (phone.matches && img.closest('li.bl-item')) return;
      e.preventDefault();
      e.stopPropagation();
      openLightbox(img);
    });
    img.addEventListener('keydown', function (e) {
      if (e.key !== 'Enter' && e.key !== ' ') return;
      e.preventDefault();
      openLightbox(img);
    });
  });
  lightbox.addEventListener('click', function (e) { if (e.target === lightbox) closeLightbox(); });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && lightbox.classList.contains('bl-open')) closeLightbox();
  });

  // ---- Answers ------------------------------------------------------------
  var questions = document.querySelectorAll('section.hq-question');
  function titleOf(q) {
    var h = q.querySelector('h2');
    return h ? h.textContent.replace(/\s+/g, ' ').trim() : '';
  }
  function collect() {
    var answers = [];
    each(questions, function (q) {
      var section = q.getAttribute('data-section');
      var title = titleOf(q);
      var picked = q.querySelector('input.bl-input[type=radio]:checked');
      if (picked) answers.push({ kind: 'pick', section: section, title: title, value: picked.value });
      var checks = q.querySelectorAll('input.bl-input[type=checkbox]:checked');
      if (checks.length) {
        answers.push({ kind: 'check', section: section, title: title,
          value: Array.prototype.map.call(checks, function (c) { return c.value; }) });
      }
      var note = q.querySelector('textarea.bl-input');
      if (note && note.value.trim()) answers.push({ kind: 'note', section: section, title: title, value: note.value.trim() });
    });
    return answers;
  }
  function apply(answers) {
    each(questions, function (q) {
      each(q.querySelectorAll('input.bl-input'), function (i) { i.checked = false; });
      var note = q.querySelector('textarea.bl-input');
      if (note) note.value = '';
    });
    (answers || []).forEach(function (a) {
      var q = document.querySelector('section.hq-question[data-section="' + CSS.escape(String(a.section)) + '"]');
      if (!q) return;
      if (a.kind === 'pick' || a.kind === 'check') {
        var values = a.kind === 'pick' ? [a.value] : (a.value || []);
        each(q.querySelectorAll('input.bl-input'), function (i) {
          if (values.indexOf(i.value) !== -1) i.checked = true;
        });
      } else if (a.kind === 'note') {
        var ta = q.querySelector('textarea.bl-input');
        if (ta) ta.value = a.value || '';
      }
    });
  }

  // Draft: survives reloads and moving gate to gate.
  function saveDraft() {
    try {
      localStorage.setItem(draftKey, JSON.stringify({ savedAt: new Date().toISOString(), answers: collect() }));
      return true;
    } catch (e) { return false; }
  }
  document.addEventListener('change', function (e) {
    if (e.target.classList && e.target.classList.contains('bl-input') && saveDraft()) say('draft saved');
  });
  document.addEventListener('input', function (e) {
    if (e.target.tagName === 'TEXTAREA' && e.target.classList.contains('bl-input')) saveDraft();
  });
  function readDraft() {
    try {
      var raw = localStorage.getItem(draftKey);
      return raw ? JSON.parse(raw) : null;
    } catch (e) { return null; }
  }

  // Submitted answers (written by `read` after validation) win over an older
  // draft; a draft edited after submission wins over the submission.
  function restore(submitted) {
    var draft = readDraft();
    var sub = submitted && Array.isArray(submitted.answers) ? submitted : null;
    if (sub && !(draft && draft.savedAt > sub.savedAt)) {
      apply(sub.answers);
      say('submitted ' + String(sub.savedAt).slice(0, 10));
    } else if (draft && Array.isArray(draft.answers)) {
      apply(draft.answers);
      say(sub ? 'draft restored (edited after submission)' : 'draft restored');
    }
  }
  if (HQ.submitted) {
    loadScript(HQ.submitted).then(function () { restore(window.HQ_SUBMITTED); },
      function () { restore(null); });
  } else {
    restore(null);
  }

  // ---- Copy answers -------------------------------------------------------
  function copyText(text) {
    var area = document.createElement('textarea');
    area.value = text;
    area.setAttribute('readonly', '');
    area.style.cssText = 'position:fixed;left:-10000px;top:0';
    body.appendChild(area);
    area.select();
    var copied = false;
    try { copied = document.execCommand('copy'); } catch (e) {}
    area.remove();
    if (copied) return Promise.resolve(true);
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(text).then(function () { return true; }, function () { return false; });
    }
    return Promise.resolve(false);
  }
  function showFallback(text) {
    var panel = document.createElement('div');
    panel.className = 'hq-fallback';
    var msg = document.createElement('p');
    msg.textContent = 'Clipboard access was blocked. Copy the selected JSON (Ctrl+C, or long-press and Copy), then paste it into chat. Esc closes.';
    var area = document.createElement('textarea');
    area.value = text;
    var close = document.createElement('button');
    close.type = 'button';
    close.className = 'btn';
    close.textContent = 'Close';
    close.addEventListener('click', function () { panel.remove(); });
    panel.appendChild(msg);
    panel.appendChild(area);
    panel.appendChild(close);
    body.appendChild(panel);
    area.focus();
    area.select();
    area.addEventListener('keydown', function (e) { if (e.key === 'Escape') panel.remove(); });
  }
  copyBtn.addEventListener('click', function () {
    var missing = [];
    each(questions, function (q) {
      if (q.getAttribute('data-required') !== '1') return;
      var hasChoice = q.querySelector('input.bl-input');
      var note = q.querySelector('textarea.bl-input');
      var done = hasChoice ? q.querySelector('input.bl-input:checked') : (note && note.value.trim());
      if (!done) missing.push(q);
    });
    if (missing.length) {
      say('answer the required question' + (missing.length > 1 ? 's' : '') + ' first: ' +
          missing.map(titleOf).join('; '));
      missing[0].scrollIntoView({ behavior: 'smooth', block: 'center' });
      return;
    }
    var answers = collect();
    if (!answers.length) { say('nothing answered yet'); return; }
    var payload = {
      page: HQ.page,
      stage: HQ.gate ? { n: HQ.gate.n, label: HQ.gate.label } : null,
      answers: answers,
      savedAt: new Date().toISOString()
    };
    var json = JSON.stringify(payload, null, 2);
    copyText(json).then(function (ok) {
      if (!ok) showFallback(json);
      say(ok ? 'answers copied to clipboard' : 'copy the selected answers');
    });
  });
})();
