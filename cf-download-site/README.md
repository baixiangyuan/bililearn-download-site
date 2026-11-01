# BiliLearn 下载站 · Cloudflare Worker（前端）+ 服务器镜像（后端）

两段式架构：

```
GitHub Releases ──► mirror.py（你的服务器，定时同步）
                        │
                        ├─ files/<版本>/<平台>/安装包      ← 下载流量走这里
                        └─ manifest.json                 ← 版本清单
                                                        │
Cloudflare Worker（前端）◄───────────────────────────────┘
     只负责渲染页面，不含任何下载链接
```

发布新版本时，`mirror.py` 自动拉到服务器并更新 manifest，前端无需改代码。

## 目录

| 路径 | 作用 |
|---|---|
| `cf-download-site/` | Cloudflare Worker 前端，部署到 GitHub + Workers |
| `server-mirror/` | 服务器端同步脚本 + nginx 配置 + 一键安装 |

## 一、服务器端（先做这个）

```bash
# 把 server-mirror 整个目录传到服务器，然后
sudo bash install.sh
```

安装脚本会：装依赖 → 放文件到 `/opt/dl-mirror` → 生成配置到 `/etc/dl-mirror/mirror.json`
→ 注册 systemd service + 30 分钟定时器 → 写 nginx 配置并重载。

装完必须改两处：

```bash
sudo nano /etc/dl-mirror/mirror.json
```

```json
{
  "repo": "xiaoyaya191/bilibili_learning_bot",   // 改成你的 用户名/仓库名
  "publicBaseUrl": "https://dl.example.com/files" // 改成你的域名
}
```

```bash
sudo nano /etc/nginx/conf.d/download.conf   # 把 dl.example.com 换成你的域名，补证书
sudo nginx -t && sudo systemctl reload nginx
```

手动跑一次并验证：

```bash
sudo systemctl start dl-mirror.service
tail -f /var/lib/dl-mirror/mirror.log
curl -s http://127.0.0.1/manifest.json | python3 -m json.tool
ls -lh /var/lib/dl-mirror/files/
```

## 二、Worker 前端

编辑 `cf-download-site/worker.js` 顶部的 `CONFIG`，改一行指向你的服务器：

```js
manifestUrl: 'https://dl.example.com/manifest.json',
```

`fallback` 建议填一个长期有效的 GitHub Release 地址——服务器挂掉时页面至少还能下载。

部署（三选一）：

**A. GitHub Actions（推荐）**

```bash
cd cf-download-site
git init && git add . && git commit -m "init" && git branch -M main
git remote add origin https://github.com/<用户名>/<仓库名>.git
git push -u origin main
```

然后 Cloudflare Dashboard → My Profile → API Tokens → 用 **Edit Cloudflare Workers** 模板建 token；
GitHub 仓库 → Settings → Secrets → Actions → 新增 `CLOUDFLARE_API_TOKEN`。
之后每次 push 自动部署。

**B. 本地 wrangler**

```bash
npx wrangler login && npx wrangler deploy
```

**C. Cloudflare 后台粘贴**

Workers & Pages → Create → Hello World → Quick Edit → 全选删除 → 粘贴 `worker.js` → Deploy。

## 平台识别规则

按 `mirror.json` 里的 `platforms` 规则归类，**所有匹配的附件都会下载**
（同平台多个包，如 debug/release 两个 apk，全部保留）。判定顺序：

1. 命中任一平台的 `exclude` → 跳过（源码包、SHA256SUMS 走这条）
2. 命中任一平台的 `include` → 归入该平台
3. 都不命中 → 若后缀是 `fallbackExtensions` 里的归档类型，归入 `fallbackPlatform`

已针对 `xiaoyaya191/bilibili_learning_bot` 实测校准，覆盖这些真实命名：

| 附件 | 结果 |
|---|---|
| `BiliLearn-Web-3.1.6-Windows-x64.zip` | windows |
| `BiliLearn-Web-3.1.4.zip` | windows（无平台名，靠 fallback） |
| `bilibili_learning_bot-3.1.3-EXE.zip` | windows（`EXE.zip` 需 `\.exe\.` 规则） |
| `bilibili_learning_bot3.0.2.zip` | windows |
| `BiliLearn-debug.apk` / `BiliLearn-release.apk` | android（两个都下） |
| `BiliLearn-Web-3.1.6-PC-Source.zip` | 跳过 |
| `BiliLearn-Android-v3.1.5-beta-source.zip` | 跳过（名字带 android 但是源码，靠 exclude） |
| `BiliLearn-3.1.6-SHA256SUMS.txt` / `SHA256SUMS.txt` | 跳过 |

注意 `exclude` 优先于 `include`，所以「名字里带 android 的源码包」不会被误下。

某平台有多个安装包时，页面显示「展开 N 个安装包」，点开逐条下载，并显示各自大小和 SHA-256。

## 常用操作

```bash
# 手动同步
sudo systemctl start dl-mirror.service

# 看日志
tail -f /var/lib/dl-mirror/mirror.log

# 预览将要下载什么（不落盘不写 manifest）
python3 /opt/dl-mirror/mirror.py --config /etc/dl-mirror/mirror.json --dry-run

# 强制重新校验已有文件
python3 /opt/dl-mirror/mirror.py --config /etc/dl-mirror/mirror.json --verify

# 暂停 / 恢复自动同步
sudo systemctl disable --now dl-mirror.timer
sudo systemctl enable --now dl-mirror.timer

# 查看下次执行时间
systemctl list-timers dl-mirror.timer
```

## 配置项

| 键 | 说明 |
|---|---|
| `repo` | `用户名/仓库名`，必填 |
| `token` | 公开仓库留空；私有仓库填 GitHub PAT |
| `filesDir` | 安装包存放目录 |
| `manifestPath` | manifest.json 输出路径（nginx 根目录下） |
| `publicBaseUrl` | 对外下载地址前缀 |
| `urlTemplate` | 下载 URL 模板，可用 `{base}{version}{platform}{tag}{file}` |
| `platforms` | 平台识别规则，每个平台可配 `include` / `exclude` / `defaultPlatform` |
| `fallbackPlatform` | 无平台关键词的归档归到哪个平台，`null` 表示丢弃 |
| `fallbackExtensions` | 只有这些后缀才启用 fallback，避免把 pdf 也当安装包 |
| `includePrerelease` | 是否同步预发布版本，默认 `false` |
| `keepVersions` | 保留最近 N 个版本，旧的自动删，默认 `5` |
| `downloadWorkers` | 并发下载数，默认 `3` |
| `resume` | 断点续传，默认 `true` |
| `strict` | 有附件失败时以非零退出（供监控告警），默认 `false` |

## 行为保证

- **API 故障不影响线上**：GitHub 限流或不可达时，保留现有 manifest 原样退出，cron 不会报错
- **manifest 原子写入**：先写 `.tmp` 再 `os.replace`，前端永远读到完整文件
- **下载完整性**：基于 `Content-Range` 推算服务端总大小校验，续传不会误报"不完整"
- **SHA-256 覆盖全文件**：续传后重算，避免只哈希到本次片段
- **孤儿清理**：附件改名或从 Release 移除后，磁盘上的旧文件会被删掉
- **Worker 降级**：后端无响应时显示提示并使用 `fallback` 链接，页面不会白屏
- **单实例锁**：定时器重叠触发时自动跳过，不会并发下载

## 测试

```bash
# 服务器端（50 项检查，离线模拟 GitHub API）
python3 test-mirror.py

# 平台归类（31 项检查，用该仓库真实附件名校准）
python3 test-repo-classify.py

# Worker 前端（27 项检查，离线模拟 manifest）
node --experimental-vm-modules smoke-worker.mjs cf-download-site/worker.js
```
