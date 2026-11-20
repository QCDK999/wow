# VCA Stock Monitor

监控梵克雅宝美国官网上 **Vintage Alhambra bracelet, 5 motifs（VCARD34700，18K 白金 / 玉髓）** 是否可以在线购买。确认可以购买时，系统会给你发一封邮件。

- **检测**：GitHub Actions 每小时运行一次，用 Python 和 Playwright（无头 Chromium）检查官网。你的电脑关机也照常运行。
- **通知**：通过 SMTP 发邮件（Gmail 等）。确认补货只发一封；疑似补货有有限次数的提醒；发送失败会重试；监控失效时会提醒你。
- **状态**：每次检测结果写入仓库的 `monitor-data` 分支（`state.json` 和公开的 `status.json`）。
- **网站**：Netlify 上的 React 静态页面，在浏览器里实时读取 `status.json`。状态更新不需要重新部署。

```
GitHub Actions (每小时)                       Netlify (静态网站)
  ├─ Playwright 打开商品页 ──► VCA 美国官网       │
  ├─ 判定 AVAILABLE / … / UNKNOWN                │
  ├─ 需要时发邮件 ──► 你的邮箱                     │
  └─ push state.json + status.json ──► monitor-data 分支 ◄── 浏览器每 5 分钟 fetch
```

---

## 1. 官网检测研究结果（Phase 1）

这些结论是 2026-11-19 在真实浏览器中访问美国官网得到的。

| 信号 | 目标手链（缺货） | 对照：有货的 Vintage Alhambra 吊坠 VCARP2R300 |
|---|---|---|
| 页面自身请求的 `…/vcard34700---….productinfo.US.json` | `{"sellable": true, "stock": false}` | `{"sellable": true, "stock": true}` |
| JSON-LD `offers.availability` | `OutOfStock` | `InStock` |
| `.vca-pdp-v2-controls[data-show-cta]` | `order-by-phone` | `add-to-bag` |
| “ADD TO BAG” 按钮 | **始终在 DOM 中**，但带 `vca-hide` 被隐藏 | 可见且可点击 |
| dataLayer `view_item.item_available` | `out of stock` | `in stock` |
| “ORDER BY PHONE” | 显示 | **也显示**，所以不能当作缺货信号 |

**陷阱**：只搜索 “Add to Bag” 字符串会误判，因为按钮一直存在，只是被隐藏。服务器返回的原始 HTML 中还有一个误导性的 `item_available: TRUE` 跟踪字段。本项目不使用这两者。

**Available now 筛选**：Alhambra 列表页有 “Available now” 筛选（`facet=availability,true`）。验证结果：筛选后共 137 件（分页到第 7 页返回 404 即为结束）。有货吊坠在结果中，目标手链不在，但它在未筛选列表的第 3 页。说明筛选语义和库存接口一致。由于它需要最多 7 次分页请求，而且“不在列表中”本身不能证明缺货，生产检测**不使用**这个筛选，只把它作为研究验证。

**购物袋验证**：在有货吊坠上实测过，点击 ADD TO BAG 会调用 `POST /api/commerce/unified/addCartLineItem`，返回的购物车 `lineItems` 包含该商品，小购物袋里也出现 “PROCEED TO CHECKOUT”。测试后已移除商品。监控**只在页面所有信号都显示有货时**才做这一步：在一次性的匿名浏览器会话中加入购物袋，确认商品编号匹配、数量为 1，然后尽量移除。系统**绝不**进入结账、下单或付款。

**反爬保护**：官网使用 Akamai Bot Manager 传感器，并预加载了 HUMAN（PerimeterX）“Press & Hold” 验证组件。本项目不伪装、不绕过任何保护。被拦截时结果为 UNKNOWN，网站显示 Monitoring Error。

> ⚠️ **尚未验证的一点**：GitHub Actions 运行在数据中心 IP 上，官网可能对这些 IP 弹验证码或直接拦截。开发环境无法直连官网（出站被策略拦截），所以**从 GitHub Actions 能否稳定拿到真实状态，必须在你部署后第一次手动运行时确认**（见 4.5）。

## 2. 状态判定

每次运行只打开一次商品页（加上页面自己发出的请求）。然后：

1. **前置检查**：页面加载成功、HTTP 200、URL 仍是 `/us/en/…vcard34700…`、JSON-LD 的 SKU 是 `VCARD34700`、dataLayer 国家为 `US`、货币为 `USD`。任一项失败 → **UNKNOWN**。
2. **5 个库存信号投票**：productinfo、JSON-LD、CTA、按钮可见且可用、dataLayer。

| 结果 | 条件 |
|---|---|
| **AVAILABLE** | productinfo、CTA、按钮都为“有货”，没有任何“无货”信号，**且**购物袋验证通过（编号匹配、数量 1） |
| **POSSIBLY_AVAILABLE** | 至少有一个“有货”信号，但信号不一致，或购物袋验证失败或未完成 |
| **UNAVAILABLE** | 没有“有货”信号，且 productinfo 或 JSON-LD 为“无货”，再加至少一个其他“无货”信号 |
| **UNKNOWN** | 前置检查失败，或信号缺失、不足（例如网站改版） |

UNKNOWN 永远不会被当作 UNAVAILABLE，也不会触发补货通知（见第 3 节“补发”的例外）。

**验证码 / HUMAN（PerimeterX）的处理**

VCA 的正常页面也会预加载 HUMAN 的脚本和隐藏的 “Press & Hold” 组件，所以“页面里有验证码组件”不能等同于“被拦截”。只有下列情况算作**实际触发的访问挑战**：

- 页面标题是拦截页（如 “Access to this page has been denied”）；
- 文档或库存接口返回 HTTP 401/403/429，或库存接口返回网页而不是数据；
- 验证码元素或 “Press & Hold” 真实显示在用户面前：已渲染、尺寸不小于 40×16、在视口内、自身及祖先都不透明且不是 `aria-hidden`，并且是该位置最上层的元素（用 `elementFromPoint` 判断）。

被遮挡、在屏幕外、透明、`display:none` 的组件都只记为“已预加载（正常）”，不影响判断。

| 情况 | 结果 |
|---|---|
| 只是预加载 | 正常判断 |
| 实际挑战 + 页面已拿到的库存信号全部为有货 | **POSSIBLY_AVAILABLE（强）**，不点击购物袋，**绝不**判为 AVAILABLE |
| 实际挑战 + 其他情况 | **UNKNOWN（blocked）** |

## 3. 邮件、去重和监控健康

### 3.1 补货邮件

| 情况 | 动作 |
|---|---|
| 任意状态（含首次运行）→ AVAILABLE | 发送 “🎉 VCA 五花手链可以在线购买了！”，每次补货只发一封 |
| POSSIBLY_AVAILABLE → AVAILABLE | 发送正式补货通知 |
| AVAILABLE → AVAILABLE | 不发 |
| 首次出现 POSSIBLY_AVAILABLE | 发送 “🔔 VCA 手链疑似补货，请人工确认” |
| POSSIBLY_AVAILABLE 持续 | **有限提醒**：首封后 2 小时、再过 6 小时、之后每 24 小时各一次，每次补货最多 6 封（`POSSIBLY_MAX_EMAILS`）。即使第一封进了垃圾箱或被忽略，也不会永远错过 |
| 证据由弱变强（页面所有信号都显示有货，只是购物袋验证失败或出现访问挑战） | **立即**发送一封升级提醒 “🔔🔔 强烈疑似补货”，标记为高优先级（每次补货只升级一次） |
| 已发过 AVAILABLE 后出现 POSSIBLY | 不发 |
| → UNAVAILABLE | 结束本次补货，下次补货重新计数 |
| → UNKNOWN | 不重置任何补货状态 |

“疑似补货”邮件可以用 `NOTIFY_POSSIBLY=false` 关闭。

**补发**：如果补货邮件发送失败，而之后的检测因为官网拦截变成 UNKNOWN，系统仍会在 24 小时内补发这封邮件。补发使用原来那次检测的时间和依据，并在邮件中注明是补发。

### 3.2 监控健康（UNKNOWN 不会被悄悄吞掉）

| 情况 | 动作 |
|---|---|
| 1–2 次 UNKNOWN | Actions 运行显示黄色警告，运行本身仍为成功；网站显示 Monitoring Error |
| 连续 3 次 UNKNOWN（`HEALTH_ALERT_AFTER`） | 发送 “⚠️ 监控失效” 邮件（说明是被拦截还是出错、从何时开始、最近一次成功检测时间），**本次运行标红**（退出码 3） |
| 仍然失效 | 每 24 小时（`HEALTH_REMIND_HOURS`）再提醒一次，期间的运行不标红，所以不会每小时收到故障邮件 |
| 恢复（任意一次非 UNKNOWN） | 发送一封 “✅ 已恢复”，计数清零 |
| 运行根本没有发生（定时被停用、Actions 故障） | 程序本身无法发现。网站约 2.5 小时后显示数据过期；如果配置了 `HEALTHCHECK_URL`，外部服务会发邮件提醒（见 4.2） |

退出码：`0` 正常（含单次 UNKNOWN）、`1` 状态保存失败、`2` 补货邮件发送失败、`3` 监控失效。任何非 0 都会让 Actions 运行变红，GitHub 会把失败通知发给**最后修改定时设置的用户**。

### 3.3 claim → send → confirm 和无法完全避免的重复

1. **认领**：先把 “准备发送 X” 写入状态分支（`git push` 必须快进）。push 被拒绝（另一个运行先写了）→ 重新读取后再判断。push 因其他原因失败（权限、网络）→ 退避重试，最多 6 次。
2. **发送**：先发补货邮件，再发健康邮件。每封邮件在一次运行内最多尝试 3 次，密码错误不重试。
3. **确认**：成功则记录已发送；失败则释放认领，下次运行重试，本次运行标红。确认的保存也会退避重试最多 6 次，冲突时会合并别人的修改。

| 故障 | 处理 | 结果 |
|---|---|---|
| 认领写不进去（无写权限等） | **仍然发送补货邮件**，运行标红（退出码 1）；不发健康邮件 | 不漏报；状态修复之前每小时可能重复 |
| 邮件发送失败 | 释放认领，下次重试；官网被拦截时也会补发（见 3.1） | 不漏报（只要 SMTP 最终恢复） |
| 发送成功，但确认写不进去 | 记录仍是“已认领”；15 分钟后视为未发送 | **可能重复一封** |
| 认领后进程被杀 / 超时 | 同上 | 可能重复一封，也可能是第一封 |
| 两个运行同时执行 | `concurrency` 排队，加上乐观锁 | 最多一封 |
| 状态文件丢失或损坏 | 从默认状态重建 | 如果当时正处于已通知的补货中，可能重复一封 |

**为什么重复无法完全避免**：SMTP 服务器接受邮件和我们写入状态是两个独立的系统，中间崩溃时无法知道邮件是否已发出。本项目选择“宁可重复，不可漏报”。

**降低重复的措施**：同一封逻辑邮件（例如同一次补货的 AVAILABLE 通知）在重发时使用**相同的 Message-ID**，Gmail 等服务通常会自动丢弃这种重复，但这不是所有邮件服务都保证的行为。

**无法检测的漏报**：邮件被服务器接受后进入垃圾箱，或被你忽略。建议做三件事：给 VCA Stock Monitor 的发件地址设置“永不进垃圾箱”的过滤器；先用 test-email 确认能收到；保留“疑似补货”提醒（它会重复几次）。

### 3.4 其他

- **状态持久化**：`state.json` 存在独立的 `monitor-data` 分支（只有数据），不会触发 Netlify 构建或工作流，所以没有提交循环。旧版（schema 1）状态文件会自动迁移。
- **跳过检测**：只有在一切平静（上次是 UNAVAILABLE、没有待发邮件、没有 UNKNOWN）且未到检测间隔时，才会跳过一次检测。有疑似补货、欠发的邮件或故障时，每小时都检测。

## 4. 部署

### 4.1 创建 GitHub 仓库并上传代码

1. 在 GitHub 新建仓库，例如 `vca-stock-monitor`，选择 **Public**。原因：公开仓库的 Actions 分钟数免费且不限量（私有仓库每月 2000 分钟，每小时运行勉强够用）；Netlify 页面也要能匿名读取 `status.json`。仓库里没有任何秘密，邮箱、密码都在 Secrets 中，`status.json` 不包含邮箱地址。
2. 在项目目录中运行：
   ```bash
   git init -b main
   git add -A
   git commit -m "VCA Stock Monitor"
   git remote add origin https://github.com/<你的用户名>/vca-stock-monitor.git
   git push -u origin main
   ```

### 4.2 配置邮件 Secrets

仓库 → **Settings → Secrets and variables → Actions → Secrets → New repository secret**：

| Secret | 示例（Gmail） |
|---|---|
| `SMTP_HOST` | `smtp.gmail.com` |
| `SMTP_PORT` | `587`（STARTTLS）或 `465`（SSL） |
| `SMTP_USERNAME` | `yourname@gmail.com` |
| `SMTP_PASSWORD` | 16 位应用专用密码（不是 Gmail 登录密码） |
| `NOTIFY_TO` | 接收提醒的邮箱 |
| `SMTP_FROM` | 可选，默认等于 `SMTP_USERNAME` |
| `HEALTHCHECK_URL` | 可选但**推荐**：外部“心跳”服务的检查地址（例如 healthchecks.io 免费版创建的 check，周期 1 小时，宽限 2 小时）。每次正常运行时 ping 一次，监控失效时 ping `/fail`；**运行完全停止时**由该服务给你发邮件。这是唯一能发现 “GitHub 停用了定时任务” 的主动提醒 |

**Gmail 应用专用密码**：Google 账号需要先开启两步验证。然后打开 <https://myaccount.google.com/apppasswords>，新建一个名为 “VCA Monitor” 的密码，把生成的 16 位密码（去掉空格）填入 `SMTP_PASSWORD`。请不要把密码发到聊天里。

可选变量（同一页面的 **Variables** 标签）：

| Variable | 默认 | 说明 |
|---|---|---|
| `CHECK_INTERVAL_MINUTES` | `60` | 检测间隔，最小 60。例如设为 `120` 即每 2 小时检测一次，不需要改代码 |
| `MONITOR_PAUSED` | `false` | 设为 `true` 暂停检测，网站显示 Monitoring Paused |
| `VERIFY_CART` | `true` | 是否做购物袋验证。设为 `false` 时，页面信号全部为有货即判定 AVAILABLE |
| `NOTIFY_POSSIBLY` | `true` | 是否发送“疑似补货”邮件 |
| `POSSIBLY_MAX_EMAILS` | `6` | 每次补货最多发送几封“疑似补货”邮件（含提醒） |
| `HEALTH_ALERT_AFTER` | `3` | 连续几次 UNKNOWN 后发送监控失效提醒 |
| `HEALTH_REMIND_HOURS` | `24` | 失效持续时，提醒间隔（小时） |

### 4.3 启用 Actions

1. 打开仓库的 **Actions** 页面。如果提示启用 workflows，点击确认。
2. **Settings → Actions → General → Workflow permissions**：通常保持默认即可，因为工作流文件里已声明 `contents: write` 和 `actions: write`。如果你的账号或组织强制只读，请改为 *Read and write permissions*。
3. 不要给 `monitor-data` 分支设置分支保护。
4. 如果没有写权限，第一次运行的 “Check out monitor-data branch” 步骤会直接失败，并提示如何修改，而不会带着无法保存的状态继续运行。

### 4.4 发送测试邮件

**Actions → VCA stock monitor → Run workflow → mode = `test-email` → Run**。几分钟内应收到标题为 “[测试邮件] VCA Stock Monitor 邮件配置测试” 的邮件（也检查一下垃圾邮件文件夹）。网站的 Email notifications 卡片会显示“测试邮件成功”。

也可以在本地测试：`set -a; source .env; set +a; python -m monitor.notifier --test`

### 4.5 手动运行一次检测（关键）

**Run workflow → mode = `check`**。打开这次运行，查看 **Summary** 中的 “VCA check” 结果：

- **`UNAVAILABLE`**，检测依据中列出 `productinfo=无货`、`jsonld=无货` 等：从 GitHub 访问官网成功，监控可用。
- **`UNKNOWN`**，`unknown_kind=blocked`（验证码、HTTP 403，或库存接口被拦截）：官网拦截了 GitHub 的服务器。请看第 6 节。偶尔一次没有关系；连续 3 次你会收到监控失效邮件。
- Summary 中的 `HUMAN preloaded=True` 是正常现象，表示组件只是预加载，并未触发。

每次运行的原始检测数据（不含任何秘密）作为 artifact `check-result-*` 保留 7 天，方便排查。

### 4.6 定时运行

不需要额外操作。工作流在每小时第 17 分钟自动运行，结果写入 `monitor-data` 分支。GitHub 的定时任务可能延迟几分钟到几十分钟，偶尔也会跳过一次。

**定时任务被停用的风险**：公开仓库 60 天没有“仓库活动”时，GitHub 会停用定时任务。数据分支上的机器人提交是否算作活动，GitHub 没有明确说明。工作流每次都会调用 “enable workflow” API，这是常见的做法，但同样**没有官方保证**能重置这个计时。因此不要只依赖它，另外还有三层保护：

1. GitHub 会在停用前给仓库所有者发邮件，收到后打开 Actions 页面重新启用即可；
2. 网站约 2.5 小时没有更新就显示过期；
3. **推荐配置 `HEALTHCHECK_URL`**，运行停止时会主动收到提醒。

另外建议每 1–2 个月 push 一次 `main`（哪怕只改 README），这一定算作仓库活动。

### 4.7 部署 Netlify 网站

1. Netlify → **Add new site → Import an existing project → GitHub**，选择这个仓库。
2. 构建设置会自动从 `netlify.toml` 读取。如果手动填写：
   - Base directory：`frontend`
   - Build command：`npm test && npm run build`
   - Publish directory：`frontend/dist`
   - Production branch：`main`
3. 点击 **Deploy**。Netlify 在构建时会根据仓库地址自动生成数据地址 `https://raw.githubusercontent.com/<用户>/<仓库>/monitor-data/status.json`，不需要配置。如果你把数据放在别处，可以在 Netlify 环境变量中设置 `VITE_STATUS_URL`。
4. **验证读取的是真实状态**：打开网站，“Last check” 应与最近一次 Actions 运行时间一致（美国东部时间），“检测依据”展开后与 Actions Summary 相同。
5. **验证同步刷新**：手动运行一次 check，约 5 分钟后刷新网页（GitHub raw 有约 5 分钟 CDN 缓存，页面本身每 5 分钟自动刷新），Last check 应该更新。

**过期保护**：`status.json` 带有 `stale_after`（2 倍检测间隔加 30 分钟）。超过这个时间没有更新时，网站显示 **Monitoring Error** 和 **Unknown**，并标注“最后已知状态”，不会把旧结果当作当前状态。

## 5. 测试

```bash
pip install -e ".[dev]" && python -m playwright install chromium
python -m pytest -q            # 全部为 Mock/本地夹具，不访问官网
cd frontend && npm install && npm test && npm run build
```

推送到 `main` 时，`.github/workflows/tests.yml` 会自动运行这些测试。

| 场景 | 测试 |
|---|---|
| 1 可以购买 | `test_browser::test_1_*`、`test_classify::test_available` |
| 2 无法购买（复刻真实页面状态） | `test_browser::test_2_*`、`test_classify::test_live_snapshot_is_unavailable` |
| 3 疑似补货 | `test_browser::test_3_*` |
| 4 页面加载失败 / HTTP 500 | `test_browser::test_4_*`、`test_4b_*` |
| 5 验证码 / 403 | `test_browser::test_5_*`、`test_5b_*`、`test_*human*`、`test_*challenge*`、`test_stock_endpoint_*` |
| 6 编号不匹配 / 非美国 | `test_browser::test_6_*`、`test_6b_*` |
| 7 按钮存在但不可点击 | `test_browser::test_7_*`、`test_7b_*` |
| 8 加入购物袋失败 | `test_browser::test_8_*`、`test_8b_*` |
| 9 邮件发送失败 | `test_state_and_run::test_9_*`、`test_notifier::*` |
| 10 连续 AVAILABLE | `test_10_*` |
| 11 UNAVAILABLE → AVAILABLE | `test_11_*` |
| 12 UNKNOWN → AVAILABLE | `test_12_*` |
| 13 首次运行即 AVAILABLE | `test_13_*` |
| 14 两个任务同时执行 | `test_14_concurrent_runs_send_once`、`test_14_git_store_conflict` |
| 15 状态文件不存在 / 损坏 | `test_15_*` |
| 16 网站显示过期数据 | `frontend/src/status.test.ts` |
| 连续 UNKNOWN、健康提醒和恢复 | `test_reliability::test_health_*`、`test_checker_crash_counts_as_unknown` |
| 疑似补货提醒、升级和上限 | `test_reliability::test_possibly_*`、`test_escalation_*`、`test_first_possibly_lost_*` |
| 补发欠发的补货邮件 | `test_reliability::test_failed_available_email_*`、`test_owed_email_expires` |
| claim/send/confirm 故障（写失败、进程被杀、确认失败、push 冲突、非冲突 push 错误） | `test_reliability::test_claim_*`、`test_sent_but_confirm_*`、`test_process_killed_*`、`test_git_*` |
| 工作流结构、数据分支创建、无写权限 | `tests/test_workflow.py` |

浏览器测试会启动真实的无头 Chromium，但所有对 vancleefarpels.com 的请求都被拦截，返回按真实页面结构制作的本地夹具。**它们证明代码逻辑正确，不证明能从 GitHub 访问官网。**

## 6. 局限和备选方案

- **GitHub IP 可能被拦截**：这是最大的不确定因素。如果 4.5 的结果持续为 UNKNOWN（验证码或 403）：
  1. 先把 `CHECK_INTERVAL_MINUTES` 调到 `180` 观察，降低频率有时会减少拦截。
  2. 最可靠的免费替代方案是在一台常开的设备上运行同样的代码，例如 Raspberry Pi、NAS 或家里的旧电脑。可以注册为 GitHub self-hosted runner：把 `monitor.yml` 中的 `runs-on` 改为 `self-hosted`，其他都不用改。
  3. 本项目**不会**加入伪装 UA、代理池或验证码破解，这些违反官网的反机器人措施。
- “立即”的含义是最多约 1 小时（加上 GitHub 定时延迟），不是秒级。
- 购物袋验证只确认能加入购物袋，不保证结账时仍有货。邮件里也写明了“以官网实际结账结果为准”。
- 如果官网改版，检测结果会变成 UNKNOWN，网站显示 Monitoring Error，而不会误报有货。届时需要更新 `monitor/check_stock.py` 中的选择器。

## 7. 项目结构

```
monitor/
  config.py        商品信息和环境变量配置
  check_stock.py   Playwright 检测、信号提取、状态判定（可单独运行：python -m monitor.check_stock）
  state.py         状态文件、去重、认领机制、文件存储和 git 分支存储
  notifier.py      SMTP 邮件（python -m monitor.notifier --test）
  run.py           一次完整运行：检测 → 判定 → 认领 → 通知 → 发布 status.json
scripts/prepare_data_branch.sh   检出或创建 monitor-data 分支
frontend/          React + TypeScript + Vite + Tailwind 状态网站
tests/             pytest 测试（Mock 和本地夹具）
.github/workflows/ monitor.yml（定时监控）、tests.yml（CI）
netlify.toml
```
