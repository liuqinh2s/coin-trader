# Coin Trader

合约币扫描展示 + Bitget 自动交易的一体化项目。

本项目合并自：

- `coin_scanner`：负责公开行情扫描、标签展示、静态页面构建。
- `bitget_bot`：负责交易所 API、指标计算、筛选策略、实盘/模拟盘自动交易。

`token_trader` 仅作为项目组织方式参考：单次扫描入口、服务器交易入口、运行数据与代码分离。本项目不包含它的 BSC 新币扫描或链上交易业务。

## 合并后的原则

- 策略只维护一份：展示扫描和自动交易都调用 `core/strategy.py`、`core/scanner.py`。
- 仓库只存代码：扫描结果、历史数据、站点数据、日志、密钥配置都默认不入库。
- 展示和交易分入口：展示是单次扫描 + 静态构建，交易是常驻运行。

## 目录

```text
analysis/              技术指标计算
api/                   Bitget/Binance API 封装
core/                  共享筛选策略、行情获取、下单与风控
infra/                 配置、环境变量、日志、通知
public/                展示页面源码
scripts/scan.py        展示用单次扫描入口
scripts/build.py       静态站点数据构建入口
runtime/scans/         扫描 JSON 输出，默认不入库
site/                  静态站点输出，默认不入库
main.py                自动交易入口
```

## 安装

```bash
python3 -m venv .venv
source .venv/bin/activate
pip3 install -r requirements.txt
```

如需本地代理或调整展示扫描参数：

```bash
cp config.local.example.json config.local.json
```

`config.local.json` 不会入库。

消息面评分来自 [Crypto Mint](https://liuqinh2s.github.io/crypto-mint/) 已发布的结果。若要在扫描时自动把“日K趋势向上”的代币批量提交给 Crypto Mint，在 `config.local.json` 打开：

```json
"crypto_mint_auto_dispatch": true
```

并填写 `crypto_mint_github_token`，或通过环境变量 `CRYPTO_MINT_GITHUB_TOKEN` 提供 GitHub token。未配置 token 时，扫描仍会读取已有消息面评分，缺失的币会在前端显示为“待分析”。

## 展示扫描

执行一次公开行情扫描：

```bash
python3 scripts/scan.py
```

默认输出到：

```text
runtime/scans/YYYY-MM-DDTHH-MM-SS.json
```

生成静态站点数据：

```bash
python3 scripts/build.py
```

构建结果在 `site/`，其中 `site/data/` 是生成数据，不入库。

## 自动交易

复制环境变量样例并填写交易所密钥：

```bash
cp .env.example .env
```

启动交易机器人：

```bash
python3 main.py
```

常用环境变量：

- `EXCHANGE=bitget` 或 `binance`
- `BITGET_API_KEY`
- `BITGET_API_SECRET`
- `BITGET_API_PASSPHRASE`
- `BITGET_DEMO=true` 启用 Bitget 模拟盘
- `NEED_PROXY=true`
- `PROXY_HOST`
- `PROXY_PORT`

## 数据不入库

以下路径默认被 `.gitignore` 忽略：

- `runtime/`
- `data/`
- `site/`
- `site/data/`
- `config.local.json`
- `.env`
- `*.log`
- `.sources/`

如果以后要发布 GitHub Pages，建议用 CI 在构建阶段生成 `site/data`，或者发布到单独的部署分支，不要把扫描历史提交到代码分支。
