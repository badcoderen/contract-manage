# 把系统发布到外网：安全方案与操作清单

> 一句话结论：**能不直接把端口映射到公网就别映射**。优先用"打洞/组网"让外网设备进入内网，
> 其次用 Cloudflare Tunnel 这类隧道；实在要映射，就必须按下面第 3 节把 HTTPS 和加固参数全部做到位。

---

## 1. 三种方案对比

| 方案 | 是否暴露端口 | 安全性 | 需要什么 | 适合谁 |
| --- | --- | --- | --- | --- |
| **A. 组网/隧道**（Tailscale、WireGuard、飞牛 FN Connect） | ❌ 不暴露 | ★★★★★ | 装个客户端 | **首选**，手机上装个 App 就能用 |
| **B. Cloudflare Tunnel** | ❌ 不暴露 | ★★★★☆ | 一个域名 | 想用网址访问、又不想开端口 |
| **C. 直接映射 443 + 反向代理 HTTPS** | ✅ 暴露 443 | ★★★☆☆ | 域名 + 证书 | 必须让任何人都能打开时 |

> 端口一旦暴露在公网，全世界每几分钟就会来扫一次。第 2 节的锁定机制能挡住暴力破解，
> 但**暴露本身**就会带来持续的扫描压力和被利用的风险，所以能不打洞就不打洞。

### 方案 A：Tailscale / WireGuard（推荐）

1. 飞牛上装 Tailscale（应用中心或 Docker），加入你自己的私有网络；
2. 手机/笔记本也装 Tailscale 并登录同一账号；
3. 之后直接访问 `http://飞牛的内网IP:8080` 即可，**路由器上不用开任何端口**。

飞牛自带的 **FN Connect** 也是同类思路（P2P 打通 + 加密），开箱即用。

### 方案 B：Cloudflare Tunnel

1. 域名托管在 Cloudflare，创建 Tunnel 指向 `http://127.0.0.1:8080`；
2. Cloudflare 侧开启 Access 策略（邮箱验证码 / Google 登录）——等于免费多了一层身份验证；
3. 源站防火墙只允许 Cloudflare 回源，其他人拿不到你的真实 IP。

---

## 2. 直接映射（方案 C）：必须先做完这些

### 2.1 上 HTTPS —— 这一条不做，其他都白搭

HTTP 明文下，**登录用户名和密码在公网上是裸奔的**，任何中间节点都能拿到。所以：

**Caddy（最省事，自动申请续期证书）**

```bash
# 1) 把 deploy/Caddyfile 里的 contract.example.com 换成你的域名
# 2) 域名解析到你的公网 IP，路由器把 80、443 转发到飞牛
cd /vol1/1000/docker/contract-app
docker compose -f deploy/docker-compose.proxy.yml up -d --build
```

Caddy 会自动搞定证书、自动跳转 HTTPS、自动续期。

**Nginx（已有 Nginx 时）**：用 `deploy/nginx-contract.conf`，改 `server_name` 和证书路径即可。

### 2.2 打开应用的"外网模式"参数

`deploy/docker-compose.proxy.yml` 里已经写好了更严的一组值：

| 变量 | 内网默认 | 外网建议 | 作用 |
| --- | --- | --- | --- |
| `SESSION_TIMEOUT_MIN` | 120 | **30** | 空闲 30 分钟就自动退出 |
| `LOGIN_MAX_FAILS` | 5 | **3** | 失败 3 次即锁 |
| `LOGIN_LOCK_MIN` | 15 | **60** | 锁定 1 小时，让爆破变得不划算 |
| `PWD_MIN_LEN` | 8 | **10** | 密码最长最短要求 |
| `LOGIN_LOG_KEEP_DAYS` | 30 | **90** | 日志留久一点便于追溯 |
| `SESSION_COOKIE_SECURE` | 关 | **1** | 会话 Cookie 只走 HTTPS，防止被嗅探 |
| `FORCE_HTTPS` | 关 | **1** | HTTP 请求 301 到 HTTPS |
| `TRUST_PROXY` | 1 | **1** | 采信反代传来的真实客户端 IP（仅直连方为内网时生效） |
| `IP_HEADER` | 空 | 按需 | 用 Cloudflare 等 CDN 时设 `CF-Connecting-IP`，否则取 `X-Forwarded-For` 末段 |
| `ALLOW_IP_CIDRS` | 空 | 可选 | 只允许固定出口 IP 访问（出口 IP 固定时强烈建议开） |

> 改了这些参数要 `docker compose up -d` 重建容器才生效。

### 2.3 账号层面

- **管理员用户名别用 `admin`**。公网上 "admin + 弱密码" 是被扫得最多的组合。
  改法：新建一个自己的管理员账号（用户管理 → 新增），再把 admin 停用或删除。
- **密码换成长口令**。`示例弱口令` 这类"大小写 + 连续数字"在爆破字典里非常靠前
  （10 位、纯字母数字、含 12345678 连续序列），面对公网扫描撑不了多久。
  建议 16 位以上的短语，例如 `Na6-YunPan-2026-#ZL` 这种好记又难猜的组合。
- 给每个使用人**单独开账号**（角色选"录入查询"），不要共用管理员账号——
  出问题能查日志定位到人，离职停用即可。
- 定期看 **系统管理 → 登录日志**：出现大量失败记录就是有人在试探。

### 2.4 不要一起暴露的东西

- 飞牛自己的 **Web 管理界面（5668）** 千万不要映射到公网，那是 NAS 的最高权限入口；
- **SSH（122）** 同理，只在需要时临时开；
- 数据库文件（`data/app.db`）不要放到能被 Web 直接访问的共享目录里。

---

## 3. 部署完怎么验证真的安全了（自检清单）

```bash
# ① HTTP 是否强制跳到 HTTPS（应返回 301）
curl -I http://你的域名/

# ② 证书与协议（应看到 TLSv1.3 / 有效期正常）
curl -vI https://你的域名/ 2>&1 | grep -E "SSL connection|subject:|expire"

# ③ 安全响应头是否齐全
curl -sD - -o /dev/null https://你的域名/login | grep -iE \
  "strict-transport|x-frame|x-content-type|content-security|referrer"

# ④ 登录失败次数限制是否生效（连发 4 次错误密码，第 4 次应提示锁定）
for i in 1 2 3 4; do
  curl -s -d "username=admin&password=wrong" https://你的域名/login | grep -o "次数过多[^<]*"
done

# ⑤ 伪造代理头不能绕过 IP 锁定
for h in "X-Forwarded-For: 1.2.3.4" "X-Real-IP: 5.6.7.8"; do
  curl -s -H "$h" -d "username=test&password=x" https://你的域名/login >/dev/null
done
#   然后去 系统管理 → 登录日志 看：来源 IP 应该都是你的真实出口 IP，
#   不应该出现 1.2.3.4 / 5.6.7.8（出现就说明代理头信任策略失效了）

# ⑥ 从外网扫描一下自己（可选，确认只有 443 开着）
nmap -Pn --top-ports 100 你的公网IP
```

另外确认：**飞牛的 5668 和 122 从外网连不上**。

## 4. 万一出问题

| 情况 | 处理 |
| --- | --- |
| 怀疑被人爆破 | 登录日志看来源 IP；`ALLOW_IP_CIDRS` 拉白名单；改管理员密码；必要时先停掉端口映射 |
| 账号被盗用 | 用户管理里停用该账号；`docker exec -it contract-app python tools/reset_password.py 用户名 新密码 --must-change` |
| 误删了数据 | 用备份恢复：`tar czf backup.tar.gz -C /vol1/1000/docker/contract-app data` 定期做，恢复就是覆盖 `data` 目录后重启容器 |
| 想临时关闭外网 | 关掉路由器上的 443 转发即可，内网照用 |

## 5. 备份（外网暴露时更要紧）

```bash
# 建议加进飞牛的定时任务，每天一次
cd /vol1/1000/docker/contract-app
tar czf /vol1/backup/contract-$(date +%F).tar.gz data
find /vol1/backup -name 'contract-*.tar.gz' -mtime +30 -delete   # 只留最近 30 天
```

`data/` 目录里就是全部身家：`app.db`（数据）+ `uploads/`（合同原件）。
