/*
 * AppVault Heimdall Customization
 * Adds: ⚡ AppVault, 📋 App List, 🏠 Home, ⚙️ Settings as a fixed top bar
 * Works with Heimdall's side-nav layout
 */
(function() {
  'use strict';

  const API_BASE = 'http://localhost:8086';

  // ── Inject CSS ──
  function injectStyles() {
    const css = `
      /* AppVault top bar — fixed at top */
      #av-topbar {
        position: fixed; top: 0; left: 0; right: 0; z-index: 10001;
        display: flex; align-items: center; justify-content: space-between;
        padding: 8px 20px; background: #0f172a; border-bottom: 1px solid #1e293b;
        height: 44px;
      }
      #av-topbar .logo {
        font-size: 15px; font-weight: 700;
        background: linear-gradient(135deg, #38bdf8, #818cf8);
        -webkit-background-clip: text; -webkit-text-fill-color: transparent; background-clip: text;
      }
      #av-topbar .nav { display: flex; gap: 2px; }
      #av-topbar .nav a {
        color: #94a3b8; text-decoration: none; font-size: 12px; font-weight: 600;
        padding: 6px 12px; border-radius: 6px; transition: all 0.2s; cursor: pointer;
      }
      #av-topbar .nav a:hover { background: #1e293b; color: #e2e8f0; }
      #av-topbar .nav a.active { background: linear-gradient(135deg, #38bdf8, #818cf8); color: #fff; }

      /* Push Heimdall content down to make room */
      body { padding-top: 44px !important; }

      /* AppVault page area */
      .av-page {
        padding: 20px 24px; min-height: calc(100vh - 44px);
        margin-left: 0 !important;
        background: #0f172a; color: #e2e8f0;
      }
      .av-page h2 { font-size: 22px; font-weight: 700; margin-bottom: 20px; color: #f1f5f9; }
      .av-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 14px; }
      .av-card { background: #1e293b; border-radius: 12px; padding: 18px; border: 1px solid #334155; transition: border-color 0.2s; }
      .av-card:hover { border-color: #60a5fa; }
      .av-card-header { display: flex; align-items: center; gap: 10px; margin-bottom: 8px; }
      .av-icon { width: 40px; height: 40px; border-radius: 8px; background: #334155; display: flex; align-items: center; justify-content: center; font-size: 18px; }
      .av-name { font-size: 15px; font-weight: 700; color: #fff; }
      .av-cat { font-size: 10px; color: #94a3b8; text-transform: uppercase; }
      .av-desc { font-size: 12px; color: #94a3b8; margin-bottom: 10px; line-height: 1.5; }
      .av-status { display: inline-block; padding: 2px 8px; border-radius: 8px; font-size: 10px; font-weight: 700; margin-left: 6px; }
      .av-status-installed { background: rgba(34,197,94,0.15); color: #22c55e; }
      .av-status-available { background: rgba(100,116,139,0.15); color: #94a3b8; }
      .av-status-stopped { background: rgba(234,179,8,0.15); color: #eab308; }
      .av-btn { padding: 7px 14px; border: none; border-radius: 6px; cursor: pointer; font-weight: 600; font-size: 12px; transition: opacity 0.2s; margin-right: 4px; }
      .av-btn:hover { opacity: 0.85; }
      .av-btn-install { background: #22c55e; color: #fff; }
      .av-btn-uninstall { background: #ef4444; color: #fff; }
      .av-btn-launch { background: #3b82f6; color: #fff; }
      .av-btn-spinner { background: #64748b; color: #fff; cursor: not-allowed; }
      .av-search { padding: 8px 14px; border-radius: 8px; border: 1px solid #334155; background: #1e293b; color: #e2e8f0; font-size: 13px; width: 100%; max-width: 280px; margin-bottom: 14px; }
      .av-loading { text-align: center; padding: 60px; color: #64748b; }
      .av-empty { text-align: center; padding: 40px; color: #64748b; }
      .av-toast { position: fixed; top: 54px; right: 20px; padding: 10px 18px; border-radius: 8px; color: #fff; font-weight: 600; z-index: 99999; transform: translateX(120%); transition: transform 0.3s; }
      .av-toast.show { transform: translateX(0); }
      .av-toast-success { background: #22c55e; }
      .av-toast-error { background: #ef4444; }
      /* Settings */
      .av-section { background: #1e293b; border: 1px solid #334155; border-radius: 10px; padding: 18px; margin-bottom: 14px; }
      .av-section h3 { font-size: 14px; font-weight: 700; margin-bottom: 10px; color: #f1f5f9; }
      .av-row { display: flex; justify-content: space-between; align-items: center; padding: 8px 0; border-bottom: 1px solid #1e293b; }
      .av-row:last-child { border-bottom: none; }
      .av-row label { font-size: 13px; color: #cbd5e1; }
      .av-row .val { font-size: 12px; color: #94a3b8; }
    `;
    const style = document.createElement('style');
    style.textContent = css;
    document.head.appendChild(style);
  }

  // ── Toast ──
  function showToast(msg, type) {
    let t = document.getElementById('av-toast');
    if (!t) { t = document.createElement('div'); t.id = 'av-toast'; document.body.appendChild(t); }
    t.textContent = msg; t.className = 'av-toast av-toast-' + type + ' show';
    setTimeout(() => t.classList.remove('show'), 3000);
  }

  async function api(path) {
    try { const r = await fetch(path); return await r.json(); }
    catch(e) { return { error: e.message }; }
  }

  // ── Card HTML ──
  function cardHtml(app) {
    const s = app.status || 'available';
    const badge = '<span class="av-status av-status-' + s + '">' + s + '</span>';
    var buttons = '';
    if (s === 'installed') {
      buttons = '<button class="av-btn av-btn-launch" onclick="window.open(\'http://localhost:' + (app.container_port||'') + '\',\'_blank\')">\uD83D\uDE80 Launch</button>'
        + '<button class="av-btn av-btn-uninstall" onclick="avUninstall(\'' + app.id + '\',\'' + app.name + '\')">\uD83D\uDDD1 Uninstall</button>';
    } else if (s === 'stopped') {
      buttons = '<button class="av-btn av-btn-launch" onclick="avRestart(\'' + app.id + '\')">\uD83D\uDD01 Start</button>'
        + '<button class="av-btn av-btn-uninstall" onclick="avUninstall(\'' + app.id + '\',\'' + app.name + '\')">\uD83D\uDDD1 Remove</button>';
    } else {
      buttons = '<button class="av-btn av-btn-install" data-id="' + app.id + '" onclick="avInstall(this)">\u2B07 Install</button>';
    }
    return '<div class="av-card">'
      + '<div class="av-card-header"><div class="av-icon">\uD83D\uDCE6</div><div><div class="av-name">' + app.name + badge + '</div><div class="av-cat">' + (app.category||'app') + '</div></div></div>'
      + '<div class="av-desc">' + (app.description||'') + '</div>'
      + buttons + '</div>';
  }

  // ── Actions ──
  window.avInstall = function(btn) {
    var id = btn.getAttribute('data-id');
    btn.textContent = '\u23F3...'; btn.className = 'av-btn av-btn-spinner'; btn.disabled = true;
    fetch(API_BASE + '/api/install/' + id, { method: 'POST' })
      .then(function(r){ return r.json(); })
      .then(function(r){
        if (r.error) { showToast('\u274C ' + r.error, 'error'); btn.textContent = '\u2B07 Install'; btn.className = 'av-btn av-btn-install'; btn.disabled = false; }
        else { showToast('\u2705 ' + id + ' installed!', 'success'); renderAppList(); }
      });
  };

  window.avUninstall = function(id, name) {
    if (!confirm('Uninstall ' + name + '?')) return;
    fetch(API_BASE + '/api/uninstall/' + id, { method: 'POST' })
      .then(function(r){ return r.json(); })
      .then(function(r){
        if (!r.error) { showToast('\uD83D\uDDD1 ' + name + ' uninstalled', 'success'); renderAppList(); }
        else { showToast('\u274C ' + r.error, 'error'); }
      });
  };

  window.avRestart = function(id) {
    fetch(API_BASE + '/api/restart/' + id, { method: 'POST' })
      .then(function(r){ return r.json(); })
      .then(function(r){
        if (!r.error) { showToast('\uD83D\uDD01 Restarted!', 'success'); renderAppList(); }
        else { showToast('\u274C ' + r.error, 'error'); }
      });
  };

  // ── Pages ──
  function renderAppVault() {
    var main = document.querySelector('.av-page') || document.querySelector('main') || document.querySelector('.container-fluid') || document.body;
    var page = document.createElement('div'); page.className = 'av-page';
    page.innerHTML = '<h2>\uD83D\uDCE6 AppVault Store</h2><input class="av-search" id="av-search-input" placeholder="Search apps..." onkeyup="searchStore()"><div id="av-grid" class="av-grid"><div class="av-loading">Loading...</div></div>';
    replaceMain(main, page);
    loadApps();
  }

  function replaceMain(oldEl, newEl) {
    if (oldEl && oldEl.parentNode) {
      var parent = oldEl.parentNode;
      // Hide the Heimdall side nav and old content
      var sidenav = document.querySelector('.sidenav');
      if (sidenav) sidenav.style.display = 'none';
      parent.innerHTML = '';
      parent.appendChild(newEl);
    }
  }

  var catalogCache = null;

  function loadApps() {
    fetch(API_BASE + '/api/catalog')
      .then(function(r){ return r.json(); })
      .then(function(data){
        catalogCache = data;
        var grid = document.getElementById('av-grid');
        if (!data || !data.apps) { grid.innerHTML = '<div class="av-empty">Cannot connect</div>'; return; }
        grid.innerHTML = data.apps.map(cardHtml).join('');
      });
  }

  window.searchStore = function() {
    var q = (document.getElementById('av-search-input') || {}).value || '';
    var grid = document.getElementById('av-grid');
    if (!catalogCache || !catalogCache.apps) return;
    var filtered = q ? catalogCache.apps.filter(function(a){ return a.name.toLowerCase().indexOf(q.toLowerCase()) >= 0 || (a.description||'').toLowerCase().indexOf(q.toLowerCase()) >= 0; }) : catalogCache.apps;
    grid.innerHTML = filtered.map(cardHtml).join('');
  };

  function renderAppList() {
    var main = document.querySelector('.av-page') || document.querySelector('main') || document.querySelector('.container-fluid') || document.body;
    var page = document.createElement('div'); page.className = 'av-page';
    page.innerHTML = '<h2>\uD83D\uDCCB App List</h2><div id="av-grid" class="av-grid"><div class="av-loading">Loading...</div></div>';
    replaceMain(main, page);
    fetch(API_BASE + '/api/catalog')
      .then(function(r){ return r.json(); })
      .then(function(data){
        var grid = document.getElementById('av-grid');
        var installed = (data.apps||[]).filter(function(a){ return a.status === 'installed' || a.status === 'stopped'; });
        if (installed.length === 0) { grid.innerHTML = '<div class="av-empty">No apps installed yet</div>'; return; }
        grid.innerHTML = installed.map(cardHtml).join('');
      });
  }

  function renderSettings() {
    var main = document.querySelector('.av-page') || document.querySelector('main') || document.querySelector('.container-fluid') || document.body;
    var page = document.createElement('div'); page.className = 'av-page';
    page.innerHTML = '<h2>\u2699\uFE0F Settings</h2><div class="av-loading">Loading...</div>';
    replaceMain(main, page);

    Promise.all([
      fetch(API_BASE + '/api/health').then(function(r){ return r.json(); }),
      fetch(API_BASE + '/api/catalog').then(function(r){ return r.json(); })
    ]).then(function(results){
      var health = results[0], catalog = results[1];
      var apps = catalog.apps || [];
      var installed = apps.filter(function(a){ return a.status === 'installed' || a.status === 'stopped'; });
      page.innerHTML = '<h2>\u2699\uFE0F Settings</h2>'
        + '<div class="av-section"><h3>\uD83D\uDD04 System</h3>'
        + '<div class="av-row"><label>Status</label><span class="val"><span class="av-status-installed" style="padding:2px 8px;border-radius:8px">Running</span></span></div>'
        + '<div class="av-row"><label>Agent</label><span class="val" style="font-family:monospace;font-size:11px">' + (health.agent_id||'N/A').slice(0,16) + '</span></div>'
        + '<div class="av-row"><label>Docker</label><span class="val">' + (health.docker_version||'?') + '</span></div>'
        + '<div class="av-row"><label>Catalog</label><span class="val">v' + (health.catalog_version||'?') + ' · ' + apps.length + ' apps</span></div>'
        + '<div class="av-row"><label>Installed</label><span class="val">' + installed.length + ' apps</span></div>'
        + '</div>'
        + '<div class="av-section"><h3>\uD83D\uDEE0 Connections</h3>'
        + '<div class="av-row"><label>AppVault</label><span class="val"><a href="http://localhost:8085" style="color:#60a5fa">http://localhost:8085</a></span></div>'
        + '<div class="av-row"><label>Agent API</label><span class="val"><a href="http://localhost:8086" style="color:#60a5fa">http://localhost:8086</a></span></div>'
        + '<div class="av-row"><label>Admin</label><span class="val"><a href="http://localhost:8001/admin" style="color:#60a5fa">http://localhost:8001/admin</a></span></div>'
        + '</div>';
      if (installed.length > 0) {
        page.innerHTML += '<div class="av-section"><h3>\uD83D\uDCE6 Installed Apps</h3>'
          + installed.map(function(a){ return '<div class="av-row"><label>' + a.name + '</label><span class="val">' + a.status + ':' + (a.container_port||'?') + '</span></div>'; }).join('')
          + '</div>';
      }
    });
  }

  // ── Build top bar ──
  function buildTopBar() {
    var bar = document.createElement('div');
    bar.id = 'av-topbar';
    bar.innerHTML = '<div class="logo">\u26A1 AppVault</div>'
      + '<div class="nav">'
      + '<a data-page="appvault">\uD83D\uDCE6 AppVault</a>'
      + '<a data-page="applist">\uD83D\uDCCB App List</a>'
      + '<a data-page="home" href="/index.php">\uD83C\uDFE0 Home</a>'
      + '<a data-page="settings">\u2699\uFE0F Settings</a>'
      + '</div>';

    document.body.insertBefore(bar, document.body.firstChild);

    // Nav click handlers
    bar.querySelectorAll('.nav a[data-page]').forEach(function(a) {
      a.addEventListener('click', function(e) {
        e.preventDefault();
        bar.querySelectorAll('.nav a').forEach(function(x) { x.classList.remove('active'); });
        this.classList.add('active');
        switch(this.getAttribute('data-page')) {
          case 'appvault': renderAppVault(); break;
          case 'applist': renderAppList(); break;
          case 'settings': renderSettings(); break;
        }
      });
    });
  }

  // ── Init ──
  function init() {
    try {
      console.log('AppVault: loading');
      injectStyles();
      buildTopBar();
      console.log('AppVault: injected successfully');
      console.log('AppVault: API at ' + API_BASE);
    } catch(e) {
      console.log('AppVault error: ' + e.message);
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
