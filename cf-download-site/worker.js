/**
 * Cloudflare Worker 下载站（前端）
 * ---------------------------------------------------------------
 * 后端是你自己的服务器：mirror.py 定时从 GitHub Releases 拉安装包，
 * 生成 manifest.json。本文件只负责渲染页面，不含任何下载链接。
 *
 * 部署：
 *   - GitHub Actions 自动部署（见 .github/workflows/deploy.yml）
 *   - 或本地 wrangler deploy
 *   - 或 Cloudflare 后台 Quick Edit 粘贴本文件
 *
 * 日常只改 CONFIG 一段即可。
 */

const CONFIG = {
  // ---------- 站点信息 ----------
  siteName: 'BiliLearn 下载中心',
  subtitle: '选择版本和系统，即可开始下载',
  footerText: '所有版本均经过测试后发布 · 如遇问题请前往 GitHub 提交 Issue',
  repoUrl: 'https://github.com/xiaoyaya191/bilibili_learning_bot',
  repoText: '访问 GitHub 仓库',

  // ---------- 后端 manifest 地址 ----------
  // 填你服务器的 manifest.json 完整 URL。这是唯一需要与后端一致的地方。
  manifestUrl: 'https://dl.example.com/manifest.json',

  // 请求超时（毫秒）。后端无响应时前端会降级显示兜底版本。
  manifestTimeout: 8000,

  // ---------- 系统显示 ----------
  osOrder: ['windows', 'mac', 'linux', 'android', 'source'],
  osNames: {
    windows: 'Windows',
    mac: 'macOS',
    linux: 'Linux',
    android: 'Android',
    source: '源码包 (ZIP)'
  },

  // ---------- 后端不可用时的兜底版本 ----------
  // 建议填一个长期有效的地址（如 GitHub Release 固定链接），
  // 这样服务器挂掉时页面至少还能下载。
  fallback: {
    '3.1.4': {
      windows: 'https://github.com/xiaoyaya191/bilibili_learning_bot/releases/download/v3.1.4/BiliLearn-3.1.4-win.zip',
      source: 'https://github.com/xiaoyaya191/bilibili_learning_bot/archive/refs/tags/v3.1.4.zip'
    }
  }
};

// ---------------------------------------------------------------
// 以下为逻辑代码，一般无需修改
// ---------------------------------------------------------------

function normalize(entry) {
  if (!entry) return null;
  if (typeof entry === 'string') return { url: entry };
  return entry;
}

function normalizeVersion(v) {
  const assets = {};
  Object.keys(v.assets || {}).forEach(os => {
    const raw = v.assets[os];
    // 后端可能给单个对象，也可能给数组（同平台多个安装包）
    const list = Array.isArray(raw) ? raw : [raw];
    const items = list.map(normalize).filter(e => e && e.url).map(e => ({
      url: e.url,
      size: e.sizeText || e.size || '',
      date: v.publishedAt || e.date || '',
      note: e.note || '',
      sha256: e.sha256 || '',
      file: e.file || ''
    }));
    if (!items.length) return;
    assets[os] = items.length === 1 ? items[0] : items;
  });
  return assets;
}

function versionSort(a, b) {
  const pa = String(a).split('.').map(n => parseInt(n, 10) || 0);
  const pb = String(b).split('.').map(n => parseInt(n, 10) || 0);
  for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
    const d = (pa[i] || 0) - (pb[i] || 0);
    if (d !== 0) return d;
  }
  return 0;
}

// 拉取后端 manifest；失败时返回 null
async function fetchManifest() {
  const cache = caches.default;
  const cacheKey = new Request(CONFIG.manifestUrl);

  // 有缓存先用（Worker 边缘缓存，1 分钟内），同时后台刷新
  const hit = await cache.match(cacheKey);
  if (hit) {
    try { return await hit.json(); } catch (e) { /* 缓存损坏则继续拉取 */ }
  }
  return await refreshManifest(cacheKey, cache, true);
}

async function refreshManifest(cacheKey, cache, wait) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), CONFIG.manifestTimeout);
  try {
    const res = await fetch(cacheKey, {
      headers: { 'Accept': 'application/json' },
      signal: controller.signal
    });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json();
    const copy = new Response(JSON.stringify(data), {
      headers: { 'Content-Type': 'application/json', 'Cache-Control': 'public, max-age=60' }
    });
    await cache.put(cacheKey, copy);
    return data;
  } catch (e) {
    // 后端超时或不可达：保留旧缓存（若有），否则由调用方降级
    return null;
  } finally {
    clearTimeout(timer);
  }
}

const HTML_PAGE = `<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>__SITE_NAME__</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:"Microsoft YaHei",Arial,sans-serif;background:linear-gradient(135deg,#667eea 0%,#764ba2 100%);min-height:100vh;display:flex;justify-content:center;align-items:center;padding:20px}
.container{background:#fff;border-radius:16px;padding:40px;max-width:760px;width:100%;box-shadow:0 20px 60px rgba(0,0,0,.3)}
h1{color:#333;font-size:28px;text-align:center;margin-bottom:5px}
.subtitle{text-align:center;color:#888;margin-bottom:25px;font-size:14px}
.selector{display:flex;gap:10px;margin-bottom:20px;flex-wrap:wrap}
.selector select{flex:1;padding:12px 16px;border:2px solid #ddd;border-radius:8px;font-size:16px;background:#fff;min-width:130px}
.selector select:focus{border-color:#667eea;outline:none}
.btn-main{padding:12px 30px;background:#667eea;color:#fff;border:none;border-radius:8px;font-size:16px;cursor:pointer;transition:background .3s}
.btn-main:hover{background:#5a67d8}
.current-os{text-align:center;color:#666;margin-bottom:20px;font-size:14px;padding:8px;background:#f0f4ff;border-radius:8px}
.version-list{margin-top:10px}
.version-item{display:flex;justify-content:space-between;align-items:center;padding:14px 18px;margin-bottom:8px;background:#f8f9fa;border-radius:8px;border-left:4px solid #667eea;transition:all .3s;gap:10px;flex-wrap:wrap}
.version-item:hover{background:#e9ecef;transform:translateX(5px)}
.version-item .v{font-weight:bold;color:#333}
.version-item .meta{color:#888;font-size:13px;text-align:right;flex:1;min-width:140px}
.note{display:inline-block;background:#edf2ff;color:#5a67d8;border-radius:10px;padding:1px 8px;font-size:11px;margin-left:6px}
.badge{background:#48bb78;color:#fff;padding:2px 10px;border-radius:12px;font-size:11px;margin-left:8px}
.actions{display:flex;gap:8px;align-items:center}
.btn-download{background:#667eea;color:#fff;border:none;padding:6px 18px;border-radius:20px;cursor:pointer;text-decoration:none;font-size:13px;transition:background .3s}
.btn-download:hover{background:#5a67d8}
.btn-copy{background:#fff;color:#667eea;border:1px solid #667eea;padding:5px 12px;border-radius:20px;cursor:pointer;font-size:12px}
.btn-copy:hover{background:#f0f4ff}
.btn-disabled{background:#ccc;color:#999;padding:6px 18px;border-radius:20px;font-size:13px;cursor:not-allowed}
.loading{text-align:center;color:#999;padding:30px 0}
.notice{background:#fffaf0;border:1px solid #f6ad55;color:#975a16;border-radius:8px;padding:10px 14px;font-size:13px;margin-bottom:15px;text-align:center}
.repo-link{display:inline-block;margin-top:15px;padding:10px 24px;background:#24292e;color:#fff;border-radius:8px;text-decoration:none;font-size:14px;transition:background .3s}
.repo-link:hover{background:#1a1e22}
.footer{text-align:center;margin-top:25px;padding-top:20px;border-top:1px solid #eee;color:#999;font-size:12px}
.sha{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:10px;color:#aaa;word-break:break-all;max-width:220px}
.sub-list{margin:-4px 0 10px 0;border-left:2px solid #e2e8f0;padding-left:12px}
.sub-item{display:flex;justify-content:space-between;align-items:center;gap:10px;padding:9px 12px;margin-bottom:6px;background:#fff;border-radius:6px;border:1px solid #edf0f5;flex-wrap:wrap}
.sub-item:hover{background:#f8f9fa}
.sub-name{font-size:13px;color:#444;word-break:break-all;flex:1;min-width:180px}
.sub-meta{font-size:11px;color:#999;font-family:ui-monospace,Menlo,Consolas,monospace}
@media (max-width:600px){.container{padding:20px}.selector{flex-direction:column}.version-item .meta{text-align:left}.sha{max-width:100%}}
</style>
</head>
<body>
<div class="container">
  <h1>📦 __SITE_NAME__</h1>
  <p class="subtitle">__SUBTITLE__</p>

  <div id="notice" class="notice" style="display:none">⚠️ 无法连接下载服务，正在显示备用版本</div>

  <div class="selector">
    <select id="versionSelect"></select>
    <select id="osSelect"></select>
    <button class="btn-main" onclick="doDownload()">⬇ 下载</button>
  </div>

  <div class="current-os">🖥 当前选择：<strong id="currentOs"></strong> 版本</div>

  <div class="version-list" id="versionList"><div class="loading">加载中…</div></div>

  <div style="text-align:center">
    <a class="repo-link" target="_blank" href="__REPO_URL__">__REPO_TEXT__</a>
  </div>

  <div class="footer"><p>__FOOTER__</p></div>
</div>

<script>
var CFG = null;
var STATE = { links: {}, osOrder: [], osNames: {}, degraded: false };

function norm(e){ return e ? (typeof e === 'string' ? { url: e } : e) : null; }

function fillSelect(id, items, labels) {
  var sel = document.getElementById(id);
  sel.innerHTML = '';
  items.forEach(function (val) {
    var opt = document.createElement('option');
    opt.value = val;
    opt.textContent = labels ? labels[val] : val;
    sel.appendChild(opt);
  });
}

function render() {
  if (!CFG) return;
  STATE.links = CFG.links || {};
  STATE.osOrder = CFG.osOrder || [];
  STATE.osNames = CFG.osNames || {};
  fillSelect('versionSelect', Object.keys(STATE.links).sort().reverse());
  renderOsOptions();
  renderList();
}

function renderOsOptions() {
  var sel = document.getElementById('osSelect');
  var prev = sel.value;
  sel.innerHTML = '';
  STATE.osOrder.forEach(function (os) {
    var exists = Object.keys(STATE.links).some(function (v) {
      return STATE.links[v].hasOwnProperty(os);
    });
    if (exists) {
      var opt = document.createElement('option');
      opt.value = os;
      opt.textContent = STATE.osNames[os] || os;
      sel.appendChild(opt);
    }
  });
  if (prev) sel.value = prev;
}

function currentEntries() {
  var v = document.getElementById('versionSelect').value;
  var os = document.getElementById('osSelect').value;
  var raw = STATE.links[v] && STATE.links[v][os];
  if (!raw) return [];
  var list = Array.isArray(raw) ? raw : [raw];
  return list.map(norm).filter(function (e) { return e && e.url; });
}

// 取该平台首个安装包，供顶部「下载」按钮用
function currentEntry() {
  var items = currentEntries();
  return items.length ? items[0] : null;
}

function doDownload() {
  var e = currentEntry();
  if (e && e.url) { window.location.href = e.url; }
  else { alert('该版本暂无此系统安装包'); }
}

function copyUrl(btn, idx) {
  var items = currentEntries();
  var e = items[idx || 0];
  if (!e || !e.url) return;
  var done = function () { btn.textContent = '已复制'; setTimeout(function(){ btn.textContent = '复制链接'; }, 1500); };
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(e.url).then(done, function(){ fallbackCopy(e.url); done(); });
  } else { fallbackCopy(e.url); done(); }
}

function fallbackCopy(text) {
  var ta = document.createElement('textarea');
  ta.value = text;
  document.body.appendChild(ta);
  ta.select();
  try { document.execCommand('copy'); } catch (e) {}
  document.body.removeChild(ta);
}

// 从文件名猜一个可读标签，如 xxx-windows-x64.zip -> x64
function fileLabel(name, os, idx) {
  if (!name) return idx === 0 ? '' : ('包 ' + (idx + 1));
  var m = name.match(/(x64|x86|amd64|arm64|aarch64|win64|win32|universal)/i);
  if (m) return m[1].toLowerCase();
  return idx === 0 ? '' : ('包 ' + (idx + 1));
}

function renderList() {
  var os = document.getElementById('osSelect').value;
  var list = document.getElementById('versionList');
  var html = '';
  var versions = Object.keys(STATE.links).sort().reverse();

  document.getElementById('currentOs').textContent = STATE.osNames[os] || os;

  versions.forEach(function (v, index) {
    var badge = index === 0 ? ' <span class="badge">最新</span>' : '';
    var items = currentEntriesFor(v, os);
    var meta = [];
    if (items.length) {
      if (items[0].size) meta.push(items[0].size);
      if (items[0].date) meta.push(items[0].date);
      if (items.length > 1) meta.push(items.length + ' 个安装包');
    }
    html += '<div class="version-item">';
    html += '<span class="v">v' + v + badge + '</span>';
    html += '<span class="meta">' + (meta.join(' · ') || '&nbsp;') +
            (items.length && items[0].note ? '<span class="note">' + items[0].note + '</span>' : '') + '</span>';
    html += '<span class="actions">';
    if (items.length === 1) {
      html += '<a class="btn-download" href="' + items[0].url + '">下载</a>';
      html += '<button class="btn-copy" onclick="copyUrl(this,0)">复制链接</button>';
    } else if (items.length > 1) {
      html += '<button class="btn-main" style="padding:6px 18px;font-size:13px" onclick="toggleExpand(this)">展开 ' + items.length + ' 个安装包</button>';
    } else {
      html += '<span class="btn-disabled">不可用</span>';
    }
    html += '</span></div>';
    // 多安装包时逐条展开
    if (items.length > 1) {
      html += '<div class="sub-list" style="display:none">';
      items.forEach(function (e, i) {
        var label = fileLabel(e.file, os, i);
        html += '<div class="sub-item">';
        html += '<span class="sub-name">' + (e.file || (label || ('安装包 ' + (i + 1)))) + '</span>';
        html += '<span class="sub-meta">' + (e.size || '') + (e.sha256 ? ' · SHA256 ' + e.sha256.slice(0, 12) + '…' : '') + '</span>';
        html += '<span class="actions">';
        html += '<a class="btn-download" href="' + e.url + '">下载</a>';
        html += '<button class="btn-copy" onclick="copyUrl(this,' + i + ')">复制</button>';
        html += '</span></div>';
      });
      html += '</div>';
    } else if (items.length === 1 && items[0].sha256) {
      html += '<div style="margin:-6px 0 10px 22px"><span class="sha">SHA256 ' + items[0].sha256 + '</span></div>';
    }
  });
  list.innerHTML = html || '<div class="loading">暂无版本</div>';
}

function currentEntriesFor(v, os) {
  var raw = STATE.links[v] && STATE.links[v][os];
  if (!raw) return [];
  var list = Array.isArray(raw) ? raw : [raw];
  return list.map(norm).filter(function (e) { return e && e.url; });
}

function toggleExpand(btn) {
  var item = btn.closest('.version-item');
  var sub = item && item.nextElementSibling;
  if (!sub || !sub.classList.contains('sub-list')) return;
  var open = sub.style.display === 'block';
  sub.style.display = open ? 'none' : 'block';
  var n = sub.children.length;
  btn.textContent = open ? ('展开 ' + n + ' 个安装包') : '收起';
}

document.getElementById('versionSelect').addEventListener('change', renderList);
document.getElementById('osSelect').addEventListener('change', renderList);

fetch('/api/links')
  .then(function (r) { return r.json(); })
  .then(function (data) {
    CFG = data;
    if (data.degraded) document.getElementById('notice').style.display = 'block';
    render();
  })
  .catch(function () {
    document.getElementById('versionList').innerHTML = '<div class="loading">加载失败，请刷新重试</div>';
  });
</script>
</body>
</html>`;

function page(title) {
  return HTML_PAGE
    .replace(/__SITE_NAME__/g, title || CONFIG.siteName)
    .replace(/__SUBTITLE__/g, CONFIG.subtitle)
    .replace(/__REPO_URL__/g, CONFIG.repoUrl)
    .replace(/__REPO_TEXT__/g, CONFIG.repoText)
    .replace(/__FOOTER__/g, CONFIG.footerText);
}

function buildLinks(manifest) {
  const links = {};
  const versions = manifest && manifest.versions;
  if (!versions || typeof versions !== 'object') return links;
  Object.keys(versions).sort(versionSort).reverse().forEach(v => {
    links[v] = normalizeVersion(versions[v]);
  });
  return links;
}

export default {
  async fetch(request) {
    const url = new URL(request.url);

    if (url.pathname === '/api/links') {
      const manifest = await fetchManifest();
      let links, degraded = false;

      if (manifest && manifest.versions && Object.keys(manifest.versions).length) {
        links = buildLinks(manifest);
      } else {
        // 后端挂了或没数据：降级到兜底版本
        links = CONFIG.fallback;
        degraded = true;
      }

      // 归一化：兼容单对象与同平台多附件（数组）两种形态
      const normalized = {};
      Object.keys(links).forEach(v => {
        normalized[v] = {};
        Object.keys(links[v]).forEach(os => {
          const raw = links[v][os];
          const list = Array.isArray(raw) ? raw : [raw];
          const items = list.map(normalize).filter(e => e && e.url);
          if (items.length) {
            normalized[v][os] = items.length === 1 ? items[0] : items;
          }
        });
      });

      return new Response(JSON.stringify({
        links: normalized,
        osNames: CONFIG.osNames,
        osOrder: CONFIG.osOrder,
        degraded: degraded
      }), {
        headers: {
          'Content-Type': 'application/json;charset=UTF-8',
          'Cache-Control': 'public, max-age=60'
        }
      });
    }

    if (url.pathname === '/' || url.pathname === '/index.html') {
      return new Response(page(), {
        headers: { 'Content-Type': 'text/html;charset=UTF-8' }
      });
    }

    return new Response('Not Found', { status: 404 });
  }
};
