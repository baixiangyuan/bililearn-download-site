// Worker 端到端冒烟测试：用 mirror.py 生成的 manifest 喂给 worker
import fs from 'node:fs';
import vm from 'node:vm';
import path from 'node:path';

const file = path.resolve(process.argv[2]);
const src = fs.readFileSync(file, 'utf8');

// --- 最小 Cloudflare 环境模拟 ---
// Cloudflare 的 cache.match() 每次返回一个可独立读取的 Response 副本，
// 这里存 body 文本，命中时重新构造 Response。
const cacheStore = new Map();
globalThis.caches = {
  default: {
    async match(req) {
      const body = cacheStore.get(req.url);
      if (body === undefined) return undefined;
      return new Response(body, {
        status: 200,
        headers: { 'Content-Type': 'application/json', 'Cache-Control': 'public, max-age=60' }
      });
    },
    async put(req, res) { cacheStore.set(req.url, await res.text()); }
  }
};

// 指向本地假 manifest 服务器
let manifestBody = null;
globalThis.fetch = async (input) => {
  const url = typeof input === 'string' ? input : input.url;
  if (url.includes('/manifest.json')) {
    if (manifestBody === null) throw new Error('backend down');
    return new Response(JSON.stringify(manifestBody), {
      status: 200,
      headers: { 'Content-Type': 'application/json' }
    });
  }
  return new Response('not mocked', { status: 500 });
};

const mod = new vm.SourceTextModule(src);
await mod.link(() => { throw new Error('no external imports expected'); });
await mod.evaluate();
const worker = mod.namespace.default;

let fail = 0;
function check(name, cond, extra) {
  console.log((cond ? 'PASS  ' : 'FAIL  ') + name + (extra ? '  -> ' + extra : ''));
  if (!cond) fail++;
}

// ============ 场景 1：正常 manifest（含多附件） ============
manifestBody = {
  schema: 1,
  generatedAt: '2026-11-01T07:00:00Z',
  repo: 'xiaoyaya191/bilibili_learning_bot',
  latest: '3.2.0',
  versions: {
    '3.2.0': {
      tag: 'v3.2.0',
      publishedAt: '2026-10-28',
      assets: {
        windows: [
          { url: 'https://dl.example.com/files/3.2.0/windows/a-x64.zip', size: 1048576, sizeText: '1.0 MB', sha256: 'a'.repeat(64), file: 'a-x64.zip' },
          { url: 'https://dl.example.com/files/3.2.0/windows/a-setup.exe', size: 65536, sizeText: '64.0 KB', sha256: 'b'.repeat(64), file: 'a-setup.exe' }
        ],
        mac: { url: 'https://dl.example.com/files/3.2.0/mac/a.dmg', size: 524288, sizeText: '512.0 KB', sha256: 'c'.repeat(64), file: 'a.dmg' },
        source: { url: 'https://github.com/x/archive/refs/tags/v3.2.0.zip', sizeText: 'ZIP', note: 'GitHub 源码' }
      }
    },
    '3.1.4': {
      tag: 'v3.1.4',
      publishedAt: '2026-10-20',
      assets: {
        windows: { url: 'https://dl.example.com/files/3.1.4/windows/b.zip', size: 2097152, sizeText: '2.0 MB', sha256: 'd'.repeat(64), file: 'b.zip' }
      }
    }
  }
};

console.log('=== 场景 1：正常 manifest ===');
let api = await worker.fetch(new Request('https://x.com/api/links'));
check('api 200', api.status === 200);
let data = await api.json();
check('未标记降级', data.degraded === false);
check('版本齐全', Object.keys(data.links).length === 2, Object.keys(data.links));
check('版本降序', JSON.stringify(Object.keys(data.links)) === JSON.stringify(['3.2.0', '3.1.4']));

const w = data.links['3.2.0'].windows;
check('多附件 windows 保留为数组', Array.isArray(w), typeof w);
check('两个 windows 包都在', w.length === 2, w.length);
check('每个包有 url', w.every(x => x.url));
check('每个包有 size', w.every(x => x.size === '1.0 MB' || x.size === '64.0 KB'), w.map(x => x.size));
check('每个包有 sha256', w.every(x => x.sha256.length === 64));
check('发布日期注入', data.links['3.2.0'].windows[0].date === '2026-10-28');
check('单附件平台为对象', typeof data.links['3.1.4'].windows === 'object' && !Array.isArray(data.links['3.1.4'].windows));
check('source 注入', data.links['3.2.0'].source.url.includes('archive/refs/tags/v3.2.0'));
check('osOrder 下发', data.osOrder[0] === 'windows');

// 首页
let home = await worker.fetch(new Request('https://x.com/'));
let html = await home.text();
check('首页 200', home.status === 200);
check('首页含站点名', html.includes('BiliLearn 下载中心'));
check('首页无未替换占位符', !html.includes('__SITE_NAME__') && !html.includes('__REPO_URL__'));
check('首页含多附件展开逻辑', html.includes('toggleExpand'));
check('首页含子列表样式', html.includes('.sub-list'));

// ============ 场景 2：后端挂了，降级到 fallback ============
console.log('\n=== 场景 2：后端不可用（降级）===');
manifestBody = null;
cacheStore.clear();
api = await worker.fetch(new Request('https://x.com/api/links'));
check('降级时 api 仍 200', api.status === 200);
data = await api.json();
check('标记为降级', data.degraded === true);
check('降级后有兜底版本', Object.keys(data.links).length > 0, Object.keys(data.links));
const fb = data.links['3.1.6'] || data.links['3.1.4'];
check('兜底版本可用', fb && fb.windows && fb.windows.url.startsWith('http'), fb && fb.windows && fb.windows.url);
check('兜底走 gh-proxy 加速', fb && fb.windows.url.includes('gh-proxy.org'), fb && fb.windows.url);
check('兜底有体积信息', !!(fb && fb.windows.size), fb && fb.windows.size);
check('兜底指向真实存在的附件', fb && fb.windows.url.includes('BiliLearn-Web-'), fb && fb.windows.url);
home = await worker.fetch(new Request('https://x.com/'));
html = await home.text();
check('降级时首页仍可渲染', html.includes('无法连接下载服务'));

// ============ 场景 3：manifest 结构为空 ============
console.log('\n=== 场景 3：空 manifest ===');
manifestBody = { schema: 1, versions: {} };
cacheStore.clear();
api = await worker.fetch(new Request('https://x.com/api/links'));
data = await api.json();
check('空 manifest 触发降级', data.degraded === true);
check('空 manifest 仍有兜底', Object.keys(data.links).length > 0);

// ============ 场景 4：404 ============
console.log('\n=== 场景 4：未知路径 ===');
const nf = await worker.fetch(new Request('https://x.com/nope'));
check('404', nf.status === 404);

console.log(fail === 0 ? '\n全部通过' : '\n有 ' + fail + ' 项失败');
process.exit(fail === 0 ? 0 : 1);
