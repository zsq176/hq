# hq — 本轮执行结束邮件、19:00 日报与本机仪表板

一个零依赖（Python 3.11+）的命令行邮件工具：登记项目里的 ZCode 执行 AI 跑完
一轮（宿主 turn-ended/completed 确认，不是整个任务完成）且结束时刻在
**北京时间 19:00–次日 10:00**，自动发一封提醒邮件；每天 19:00 发日报——
此前 24 小时的已记录轮次 + 各登记项目的 STATUS/历史验收快照（只读拼装，
不跑模型）。白天完成的轮次只进日报。

对用户公开的命令只有 `hq mail observe|tick|status|test|setup` 与
`hq dashboard`（本机单页仪表板）。

CLI 自动执行、复审与继续接力仍在验证，尚未接入产品。当前人工接力可配合
邮件和仪表板使用；本地测试通过不代表已安装 Hook、计划任务或验证真实收件。

## 安装（Windows）

```powershell
git clone https://github.com/zsq176/hq && cd hq
copy hq.toml.example hq.toml   # 登记你自己的项目（个人数据，已被 .gitignore 排除）
```

`bin\hq.cmd` 自动挑选 ≥3.11 的 Python（可用 `HQ_PYTHON` 指定完整路径）；把 `bin`
加进 PATH 后可直接敲 `hq`。hq 自身输出恒为 UTF-8；PowerShell 5.1/7 的 `>`
重定向会按控制台代码页对原生产出**再编码**（中文控制台 CP936 下管道里的中文
会乱码）——需要落文件时用 `cmd /c "hq … > out.txt"`（字节保真），或先
`chcp 65001`。

## 配置 hq.toml

```toml
[[projects]]
path = "C:/code/demo-api"      # 登记要看日报的项目（绝对路径）

[mail]
enabled = false                # 配置齐备后改 true
smtp_host = "smtp.example.com"
smtp_port = 465                # 留空按 security 取默认（ssl=465，starttls=587）
security = "ssl"               # ssl | starttls（强制证书校验，不降级）
username = "you@example.com"
password_env = "HQ_SMTP_PASSWORD"   # 密码只放环境变量，绝不写进本文件
from_addr = "you@example.com"
to_addr = "you@example.com"    # 只支持一个收件地址
```

## 一次性安装

```powershell
# 1) SMTP 密码只放环境变量
setx HQ_SMTP_PASSWORD "你的SMTP授权码"      # 名字对应 password_env
#   注意：setx 只对新开的终端生效——当前窗口要么重开，要么当次先
#   $env:HQ_SMTP_PASSWORD="你的SMTP授权码" 再跑下面的验证命令。
# 2) 安装 Stop 观察 hook（只登记候选，不阻挡、不联网；需要新会话才可能生效）
bin\hq.cmd mail setup --dry-run             # 先看将写入什么
bin\hq.cmd mail setup
# 3) 安装每分钟的计划任务（负责确认与发信；当前用户，隐藏窗口）
powershell -File scripts\install-mail-task.ps1 -DryRun
powershell -File scripts\install-mail-task.ps1
# 4) 验证 SMTP 与查看运行状态
bin\hq.cmd mail test
bin\hq.cmd mail status
```

`mail setup` 只合并 hq 自己的观察条目到用户级 `~/.zcode/cli/config.json`
（ZCode 忽略项目级 hooks），不动其他 handler 与全局开关；预览发现配置解析/
结构/IO 问题时退出码非零并拒绝写入。安装成功 ≠ 真实触发，待自然触发后用
`mail status` 确认。

## 仪表板（本机单页）

```powershell
hq dashboard            # 固定监听 http://127.0.0.1:8765 并自动打开浏览器
hq dashboard --port 9000 --no-open
```

- **只读聚合**：轮次记录（完成时间（北京时间）/项目/执行结果/邮件状态/需注意
  状态）、近 24h 与全库计数（含轮次/日报异常分项）、日报列表与正文（侧栏有界
  分页；勾选"需注意"时按同一异常判据先过滤再分页）、接入事实（观察 hook、
  计划任务、密码存在布尔）。只展示数据库与配置里已有的事实，不推测 AI 是否
  在线或任务是否全部完成；时间字段存在但无法解析时按数据异常展示原值与原因，
  不当作正常待处理。业务库处于 WAL 模式或有
  活跃日志时，读取前拒绝并给出原因（不产生 -wal/-shm 副本）。
- **受限配置**：登记项目目录（必须是绝对路径）、编辑 SMTP 字段、启停自动邮件；
  保存按 revision（配置内容哈希）比对，外部修改后拒绝覆盖并提示重新加载；原
  配置自动备份到 `HQ_HOME/.local/backups/dashboard-config/`。配置文件不存在时
  首建"暂停草稿"（服务端同样拒绝首建直接启用）；结构损坏的组件（登记条目缺
  path、enabled 非布尔等）按配置错误持久显示且不经仪表板替换，未知字段原样
  保留；已损坏/不可读的配置拒绝覆盖，面板只显示错误不提供重建表单。
  密码字段检测环境变量**非空**，只显示"是否已设置"布尔值，
  值不回传、不落盘。
- **显式 TEST**：页面上手动发一封测试邮件验证 SMTP。与 `mail test` 的区别：
  页面 TEST 在自动邮件**暂停**时也可发送（显式动作、单封、不运行 tick、不发
  历史待发），`mail test` 则要求 enabled。每次"检查接入"显式查询，刷新页面
  **不会**触发 tick 或任何发送。
- **安全**：只绑定 127.0.0.1，跨站 Origin/Host 校验 + CSRF token；请求目标
  仅接受本页使用的 origin-form（以 `/` 开始），absolute-form/畸形目标、
  Transfer-Encoding、重复/非法 Content-Length、带正文 GET 一律结构化拒绝
  （400/403/413）并断开。拒绝口径是"先判定拒绝，再按声明的 Content-Length
  有限排空正文，然后回应并关连接"（超长数字 Content-Length 按超上限分流，
  不会触发解释器数字转换限制）。请求正文阶段有 5 秒总预算（从开始读取/排空
  正文到结束；慢滴正文超预算即断开，不进入业务处理），该预算不施加在
  keep-alive 空闲等待上（10s 轮询的复用连接不受影响）。访问日志只记录固定
  路由（未知/解析失败记占位常量，不记录原始请求行与正文）；关闭终端
  或 Ctrl+C 即结束，不安装任何常驻服务。

## 命令

| 命令 | 作用 |
|---|---|
| `hq mail observe` | ZCode Stop 观察入口：登记候选，stdout 恒 `{}`、恒退出 0 |
| `hq mail tick` | 确认候选 → 按截止入队 → 逐封发送（计划任务每分钟调用） |
| `hq mail status` | 数据库现状：事件、通知状态、待核对项 |
| `hq mail test` | 发一封测试邮件验证 SMTP 配置 |
| `hq mail setup` | 安装观察 hook（`--dry-run` 只预览不改文件） |
| `hq dashboard` | 本机单页仪表板（127.0.0.1:8765，`--port`/`--no-open`） |

## 口径与边界

- 只通知 hq.toml 登记项目里的 ZCode 会话；Codex 决策/复核轮不在通知范围。
- 同轮次重复 Stop/重复 tick 不会重复发；每轮完成通知按该轮完成时刻计算
  **自己的次日 10:00 截止**，入队与每封实际发送前都重新校验当前时刻，过期
  一律 skipped 只进日报，不在下一晚补发。
- 单项目资料损坏（STATUS.md/verify.json 缺失或不可读）不影响其他通知与发送。
- SMTP 明确拒绝显示"失败"；结果不明（断网/进程中断）显示"结果待核对"，
  **不自动重发**；遗留 sending 行在 `mail status` 展示为"发送中/结果待核对"，
  由人工核对邮箱，另一个 tick 不会替它改判。
- 睡眠/关机期间不保证准点，恢复后日报只补最近一期；旧日报不补发。
- 旧 `hq gate` / `hq hook` 入口仅作外部引用的安全兼容：恒输出 `{}`、退出 0，
  无任何业务。

## 设计原则

- 机器能记录的交给机器（终态确认、截止校验、状态落库），判断仍由人工接力；
- observe/tick fail-open：邮件工具自身出问题绝不阻挡宿主会话；
- 个人数据与工具分离：登记表、项目资料、品味库全部 gitignore，仓库里只有机制。

## 开发与目录

- `hqlib/`：邮件、仪表板及共享代码；`hqlib/web/`：仪表板页面。
- `bin/`：Windows 与 Python 入口；`scripts/`：邮件计划任务安装脚本。
- `tests/`：长期回归；`hq.toml.example`：不含凭据的配置示例。
- `hq.toml`、`.hq/`、`backups/` 是个人实例数据，始终排除提交。
- 本地 `AGENTS.md`、`docs/`、`.local/` 和 Agent/CodeGraph 配置保留在本机，
  用于交接、审计与历史追溯，不随产品发布。

```powershell
python -X utf8 -m unittest discover -s tests -v
```

生产代码只需要 Python 3.11+ 标准库；运行前端脚本回归时还需 Node.js。

## License

MIT
