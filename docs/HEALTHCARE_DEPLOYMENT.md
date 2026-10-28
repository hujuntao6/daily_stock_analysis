# 医药行业日报部署

这套扩展把 `daily_stock_analysis` 的每日分析改造成医药行业观察：基准快照 `config/healthcare-universe.json` 包含 2026-10-27 导出的 A 股 515、港股 302、美股 1,031 个标的（合计 1,848 个），行业分类保留授权 iFinD 导出的原始字段。`config/healthcare.json` 中保留 37 个重点标的的发行人域名、CIK 和事件别名作为重点覆盖。全量快照是一个有日期的可审计输入，不代表会自动随新 IPO、退市或行业分类变化更新。

## 运行入口

在仓库根目录执行：

```bash
python -m src.healthcare --session us --config config/healthcare.json --output reports/healthcare/us
python -m src.healthcare --session ah --config config/healthcare.json --output reports/healthcare/ah
```

`us` 报告对应美股最近一个已完成交易日，供北京时间早晨复盘；`ah` 分别对应 A 股和港股各自最近的已完成交易日，供北京时间傍晚复盘。两地节假日不同时保留各自日期，不强行把旧行情改成同一天。交易日由项目日历和 `exchange_calendars` 验证；日历不可用的显式工作日回退会降低数据质量。添加 `--strict` 可启用验收门槛；严格失败也保存报告和失败原因。

每次运行会写入 Markdown、HTML、JSON 和证据存档。JSON 的 `status` 为 `complete`、`partial` 或失败状态；`partial` 会保留覆盖缺口。事件归因只允许“相关事件”“可能相关”“未找到解释”，不会把模型理由权重当作涨跌的因果比例。

## GitHub Actions

复制 `.github/workflows/healthcare-daily.yml` 到你的 Fork 后启用 Actions：

- `30 23 * * 1-5` UTC：北京时间次日 07:30 生成美股晨报。美国周一收盘对应北京时间周二早晨，周五收盘对应周六早晨。
- `30 10 * * 1-5` UTC：北京时间 18:30 生成 A/H 晚报。
- `workflow_dispatch` 可选 `us`、`ah` 或 `both` 手动运行。

此部署已把原有 `00-daily-analysis.yml` 改为手动运行，避免额外定时任务和重复邮件。医药工作流本身只读仓库内容，报告和原始证据以 artifact 保留 60 天；生成失败会使对应 job 失败，artifact 步骤仍尝试保留已生成文件。

另需遵守 GitHub 平台限制：`schedule` 在高负载时可能延迟，极少数情况下可能丢弃；公开仓库连续 60 天没有仓库活动时，GitHub 可能自动停用定时 workflow。若被停用，需要在 Actions 页面重新启用，并定期维护股票池与仓库活动。详见 [GitHub Actions schedule 官方文档](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)。

## 必要配置

先不配置任何密钥也可以生成离线测试报告。GitHub Actions 的免费行情路径为 `yfinance`，A/H 日报将 `akshare>=1.19` 作为标准备用行情源（A 股 Sina 日线及港股日线适配）；免费接口可能延迟、限流或字段缺失，报告会显示数据源和覆盖率。

建议在仓库 `Settings → Secrets and variables → Actions` 配置：

| Secret | 用途 |
|---|---|
| `EMAIL_SENDER` | SMTP 发件邮箱 |
| `EMAIL_PASSWORD` | 邮箱授权码或应用专用密码，不要提交到仓库 |
| `EMAIL_RECEIVERS` | 一个或多个收件邮箱，逗号或分号分隔 |
| `EMAIL_SMTP_HOST` | SMTP 主机；`qq.com`、`163.com`、`gmail.com` 可按发件域名自动识别，其他域名需提供 |
| `EMAIL_SMTP_PORT` | 可留空；SSL 默认 465，STARTTLS 默认 587 |
| `EMAIL_SMTP_MODE` | 可留空，默认安全的 `ssl`；也可填 `starttls` |
| `SEC_USER_AGENT` | 可选 Secret；SEC 官方接口建议配置含联系信息的标识。未配置时工作流使用不含邮箱的 `HealthcareDaily/1.0`，不会把发件邮箱外发 |

没有完整邮箱配置时，报告仍会保存；邮件步骤会在 Actions Summary/日志输出 `EMAIL_DISABLED`，不会将密码或报告内容写入日志。只有 JSON 状态为 `complete` 或显式允许的 `partial` 时才尝试发送，主题会在有覆盖缺口时加 `[部分覆盖]`。上游项目使用 `EMAIL_RECEIVERS`；本地脚本也兼容 `EMAIL_TO`。iFinD 仅用于生成已提交的 2026-10-27 股票池快照，运行时不读取 iFinD 凭证。

首次部署验收可在提交信息中显式加入 `[deploy-check]`。push-only 的小样本 live smoke 通过后，才会把同一份 `--no-email` 验证报告发到 `EMAIL_RECEIVERS`；主题以 `[部署验收样本]` 开头，并标明本次美股或 A/H 样本的行情覆盖数以及“事件/催化剂按官方来源可得性”。该邮件只覆盖 smoke 配置中的每市场 1–2 只证券和基准，不代表 1,848 个全行业标的；普通 push 不发验收邮件，SMTP 失败会使该 check 失败。

如需在首次部署同时验证全量运行，可在同一提交信息加入 `[deploy-full]`（例如同时含 `[deploy-check][deploy-full]`）。此时完整美股/A/H jobs 会等待离线校验通过、使用 1,848 标的配置并各发一封普通日报；小样本 job 仍保留 artifact，但不会重复发样本邮件。完整运行设有 45 分钟上限，行情或官方证据缺口会进入报告状态，不会被当作成功覆盖。

## 刷新全量股票池

授权导出文件应由用户显式传入，不要把本机路径、令牌或市值导出提交到仓库。刷新命令示例：

```bash
python scripts/generate_healthcare_universe.py \
  --cn path/to/ifind-cn.json \
  --hk path/to/ifind-hk.json \
  --us-large path/to/ifind-us-large.json \
  --us-small path/to/ifind-us-small.json \
  --focus-config config/healthcare.json \
  --output config/healthcare-universe.json
```

脚本会校验 A/H 行数、合并美股 large/small 并拒绝重复规范化代码；它把中国 `.SH` 转为 `.SS`，港股代码转为四位 `.HK`，去掉美股导出的 `.O/.N/.A` 交易所后缀，并把美股 share class 的点号改为 Yahoo 需要的连字符（例如 `BIO.B.N` → `BIO-B`），同时保留 `original_symbol`、`source` 和 `source_asof`。刷新后应人工抽查新 IPO、退市和代码变更，再运行离线 smoke。

## 行业会议节点与权威来源规则

`config/healthcare-calendar.json` 只记录已核验的 ASH、SITC 等行业会议时间节点，并由 `source_policy.calendar_files` 在运行时载入。行业节点不代表任何公司已入选、某项试验会披露数据，或公司一定在该日期发布读出；报告必须继续核对正式摘要、公司公告或监管文件。该文件中的 `published_at` 可为空，表示官方网页未披露发布时间；`verified_at` 保存核对时间，两者不能互相替代。

## 覆盖边界与失败状态

全量股票池用于行情复盘，不代表每只股票的全部管线都已建档。官方取证范围为当次最多30只异动证券，加上预设重点公司并去重；异动数量上限不把重点公司挤出。A/H 公告采集、SEC 披露和临床注册采集的实际覆盖数与行情覆盖数分列；美股取证仍依赖已核验的 CIK、NCT、申办方或公司 IR 映射，未建档的异动股会保留缺口。行业会议节点也单独保留，不能冒充单家公司催化剂。

官方接口遇到 403、超时、正文不可读或未发现可核验事件时，会保存具体来源缺口，并把报告标为部分覆盖；不会使用搜索摘要填补。对 SEC、公司 IR、巨潮、HKEX 等来源，保守日期提取失败时不会回退到整篇文本的年份/“将”正则，因此历史财务期间、已获批/已完成事件不会进入未来催化剂。月、季度和年度事件保留原始窗口及精度，窗口已开始时按窗口剩余时间保守归类，不把 `as_of + 1` 制造成精确的“未来7天”。零行情或无法解析交易日期会保存失败报告并返回非零退出码。未部署成功、尚未跑过 GitHub 或尚未收到 SMTP 成功状态时，不能将本地离线测试称为上线验收。

`source_policy` 拒绝把搜索结果摘要作为证据。每条事件需要 URL、发布方、发布时间、抓取时间和标识符；域名必须来自配置的权威名单或公司的官方域名。默认权威入口包括：[巨潮资讯](https://www.cninfo.com.cn/new/index)、[上交所](https://www.sse.com.cn/disclosure/listedinfo/announcement/)、[深交所](https://www.szse.cn/disclosure/listed/notice/index.html)、[HKEXnews](https://www.hkexnews.hk/index.htm?sc_lang=en)、[SEC EDGAR](https://www.sec.gov/search-filings) 和 [ClinicalTrials.gov API](https://clinicaltrials.gov/data-api/about-api)。

临床注册中的 `primary completion date` 或 `study completion date` 只标为“预计试验里程碑”，不能直接写成临床数据读出；没有公开日期的 PDUFA、会议或数据读出不会被补成确定日期。

## 更新股票池

每个主体至少维护市场、交易代码、名称、子行业和官方域名；美股可补 SEC CIK，临床项目可补 NCT 编号。A/H 代码采用 Yahoo Finance 可识别的 `.SS`、`.SZ`、`.HK` 后缀。新增代码前请从交易所或发行人 IR 页面核对，不要根据新闻标题猜代码。`universe_sources` 记录了初始池的公告和监管入口。

## 邮件适配器本地检查

不发送邮件的离线检查：

```bash
python scripts/healthcare_email.py --report-dir reports/healthcare/us --session us
```

没有 SMTP secrets 时应得到 `EMAIL_DISABLED` 并返回 0；不要在本地 shell、Issue 或日志中回显授权码。
