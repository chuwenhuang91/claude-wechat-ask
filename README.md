# wechat-ask

让 Claude 需要你拿主意时**当面问**，你不在就把问题**推到微信**，在手机上回一句就能让它继续干活。

- 问题画在 Claude 界面的输入框上方，点选项、打序号、或者直接打字都行
- 默认 30 秒没人理，自动推送到微信；在微信回复什么，Claude 就收到什么
- 本地答了微信就完全不响；想慢慢想点「我在想，别发微信」取消倒计时
- 不想回答就点关闭，Claude 会收到明确的"已取消"，而不是干等到超时

## 前置条件

1. **微信 ClawBot**。目前是灰度功能，你的微信里得能找到 ClawBot（安卓和 iOS 都行）。**这条过不去后面都白搭，先确认**
2. **Python 3.9+**，且 `python` 在 PATH 里
3. **依赖**：`pip install mcp qrcode`（`qrcode` 只在扫码登录时用到）
4. **Claude Code 2.1.2xx 以上**。本插件用的是新式 function hooks（`hooks.json` 的 `modules` 字段），老版本不认

## 安装

```bash
/plugin marketplace add <owner>/<repo>
```

```bash
/plugin install wechat-ask@wechat-ask
```

装完**必须重开一个会话**——`.mcp.json` 和工具清单都只在会话启动时加载，不重开会看起来像没装上。

本地试用也可以把这个目录塞进 `CLAUDE_CODE_PLUGIN_DIRS`。

## 首次登录

对 Claude 说一句「登录微信 ClawBot」，它会调用 `wechat_login`：

1. 弹出一张二维码图片（存在 `~/.wechat-ask/qrcode.png`）
2. 手机微信扫码并确认
3. **最后一步别漏**：在微信里给 ClawBot 随便发一条消息

第 3 步是必须的。微信的 iLink 协议要求先有一条你发来的消息，才能拿到主动推送所需的 `context_token`——没有它就只能收不能发。

验证是否就绪：

```bash
python server/ilink.py status
```

`可主动推送: 是` 才算齐。

## 用法

装好之后不用管，Claude 需要确认时自己会调。想让它更主动，在 `CLAUDE.md` 里写一条规则即可，注意**工具全名带 `plugin_` 前缀**：

```
mcp__plugin_wechat-ask_wechat-ask__ask_remote
```

插件自带的 MCP server 走 `mcp__plugin_<插件名>_<server名>__<工具名>`，和手动在 `~/.claude.json` 里注册时的 `mcp__<server名>__<工具名>` 不一样。写错会报 `No such tool available`，很容易误判成插件没装上。

手动调试用的 CLI：

```bash
python server/ilink.py login     # 扫码绑定（和 wechat_login 等价）
python server/ilink.py status    # 查看凭据状态
python server/ilink.py listen    # 只收不发，看消息进来
python server/ilink.py run       # 推一条测试消息并等回复
```

## 它由什么组成

| 文件 | 作用 |
|---|---|
| `server/ilink.py` | 微信 ClawBot iLink 协议客户端 + 调试 CLI |
| `server/mcp_server.py` | `ask_remote` / `wechat_login` / `debug_client_info` 三个 MCP 工具 |
| `server/ask_dialog.py` | tkinter 兜底窗口（插件没加载时用） |
| `hooks/register.tsx` | 把提问画进 Claude 界面 |

状态都在 `~/.wechat-ask/`：`config.json` 是登录凭据，`pre-answer.json` 和 `hold.json` 是界面与 MCP server 之间的交接信号。**插件更新不会动这个目录**，所以不用重新登录。

## 三条本地通道

`ask_remote` 会自动挑一条，不用配置：

1. **Claude 界面内提问** —— 装了本插件时
2. **MCP elicitation** —— 客户端支持时（终端 CLI 支持，桌面应用目前不支持）
3. **tkinter 窗口** —— 兜底

## 已知限制

- **多个会话同时等微信回复会互相抢消息**。`getupdates` 的游标在账号维度只有一份，没法分流
- **`context_token` 会过期**。长期不互动后推送可能失败，在微信里补发一条消息即可恢复
- **ClawBot 处于灰度**，接口可能变动
- 界面里按钮和输入框的字号跟随宿主、改不了；问题和选项是用 SVG 画的，所以能放大
