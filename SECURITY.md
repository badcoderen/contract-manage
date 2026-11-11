# 安全说明

本仓库是「工程合同 / 费用 / 发票 / 工资」管理系统的源码，默认部署在**内网**。

## 不要提交的东西

- `.env`（SECRET_KEY / 管理员初始密码）—— 已在 `.gitignore` 里
- `data/`、`*.db`（业务数据库）、`uploads/`（合同与发票原件）

## 部署前必做

1. `cp .env.example .env`，把 `SECRET_KEY` 换成 `openssl rand -hex 24` 生成的随机值；
2. 管理员密码用长口令，别用示例值；
3. 公网暴露时务必上 HTTPS（见 `deploy/`），并配置 `ALLOW_IP_CIDRS` 白名单。

## 报告问题

发现安全问题请开 issue（不要贴真实数据 / 密钥）。
