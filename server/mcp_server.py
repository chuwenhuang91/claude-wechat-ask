#!/usr/bin/env python3
"""ask_remote MCP server：本地弹窗与微信双通道竞速询问，谁先回答用谁。"""

import asyncio
import json
import os
import subprocess
import sys
import time
import urllib.parse
from typing import Literal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ilink as claw

try:
    from mcp.server.mcpserver import Context, MCPServer
    from pydantic import create_model
except ImportError:
    sys.stderr.write(
        "wechat-ask: 缺少依赖。请运行:  pip install mcp qrcode\n"
        "（qrcode 只在扫码登录时用到，mcp 是必需的）\n"
    )
    raise SystemExit(1)

DIALOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ask_dialog.py")
STATE_DIR = claw.STATE_DIR
PRE_ANSWER_PATH = os.path.join(STATE_DIR, "pre-answer.json")
HOLD_PATH = os.path.join(STATE_DIR, "hold.json")


def _take_hold():
    """用户在 Claude 界面点了「我在想」——取走这个信号，别再惊动微信。"""
    try:
        os.remove(HOLD_PATH)
    except OSError:
        return False
    return True


async def _wait_unless_held(delay):
    """等 delay 秒再推微信；期间收到 hold 信号就返回 False，放弃推送。"""
    deadline = time.monotonic() + delay
    while time.monotonic() < deadline:
        if _take_hold():
            return False
        await asyncio.sleep(0.25)

    return True


ERROR_LOG_PATH = os.path.join(STATE_DIR, "last-error.json")


def _log_error(info):
    try:
        with open(ERROR_LOG_PATH, "w", encoding="utf-8") as f:
            json.dump(info, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


# 用户按了关闭：和"没回答"要区分开，前者是明确表态，不该再等微信
CANCELLED = "\x00cancelled"


def _take_pre_answer():
    """取走 wechat-ask-ui plugin 在 Claude 界面里问到的答案。

    plugin 无法直接替工具产出结果（返回 {result} 不被采纳），所以改由它留下
    答案、这里取走。取一次即删，并且只认 10 秒内写的，避免读到上一轮的残留。
    """
    try:
        with open(PRE_ANSWER_PATH, encoding="utf-8") as f:
            data = json.load(f)
        os.remove(PRE_ANSWER_PATH)
    except (OSError, ValueError):
        return None

    if time.time() * 1000 - float(data.get("at") or 0) > 60000:
        return None
    if data.get("cancelled"):
        return CANCELLED
    return data.get("answer") or None

# getupdates 的游标在账号维度只有一份，并发轮询会互相吃掉消息
_poll_lock = asyncio.Lock()

server = MCPServer(name="wechat-ask")


def _resolve_option(text, options):
    answer = text.strip()
    if options and answer.isdigit():
        index = int(answer) - 1
        if 0 <= index < len(options):
            return options[index]
    return answer


ELICIT_UNAVAILABLE = object()


async def _poll_pre_answer():
    """等 wechat-ask-ui plugin 把用户在 Claude 界面里的点击写进来。

    plugin 的 hook 只有 10 秒预算，没法自己等人回答（超时引擎会替它放行），
    所以等待这件事必须落在这边——这里没有预算限制。
    """
    while True:
        answer = _take_pre_answer()
        if answer:
            return answer
        await asyncio.sleep(0.2)


async def _ask_via_elicit(ctx, question, options):
    """走 MCP elicitation，由客户端用自己的原生 UI 渲染。

    返回 ELICIT_UNAVAILABLE 表示这条路不通（桌面应用会声明 elicitation 能力，
    但连接没有反向通道，发请求直接抛 NoBackChannelError），调用方应降级到本地
    窗口，而不是把整个本地通道judged为"用户不想答"。
    """
    field = (Literal[tuple(options)], ...) if options else (str, ...)
    schema = create_model("RemoteAnswer", answer=field)

    try:
        result = await ctx.elicit(message=question, schema=schema)
    except asyncio.CancelledError:
        raise
    except Exception:
        return ELICIT_UNAVAILABLE

    if getattr(result, "action", None) != "accept":
        return None
    return getattr(getattr(result, "data", None), "answer", None)


async def _ask_local_channel(ctx, question, options, closed_event, native, state):
    if native:
        answer = await _ask_via_elicit(ctx, question, options)
        if answer is not ELICIT_UNAVAILABLE:
            state["local_kind"] = "Claude 原生弹窗"
            if not answer:
                closed_event.set()
            return answer
    return await _ask_local(question, options, closed_event)


async def _ask_local(question, options, closed_event):
    payload = json.dumps({"question": question, "options": options}, ensure_ascii=False)
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        DIALOG_PATH,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        **kwargs,
    )
    try:
        out, _ = await proc.communicate(payload.encode("utf-8"))
    except asyncio.CancelledError:
        if proc.returncode is None:
            proc.kill()
        raise

    out = out.decode("utf-8").strip()
    answer = json.loads(out).get("answer") if out else None
    if not answer:
        # 用户主动关掉了弹窗，不必再等满 local_wait_seconds，立刻转微信
        closed_event.set()
    return answer


async def _ask_wechat(cfg, question, options, delay, closed_event, state, allow_hold=False):
    if allow_hold:
        if not await _wait_unless_held(max(0, delay)):
            state["held"] = True
            return None
    else:
        try:
            await asyncio.wait_for(closed_event.wait(), timeout=max(0, delay))
        except asyncio.TimeoutError:
            pass

    text = question
    if options:
        listed = "\n".join(f"{i + 1}. {o}" for i, o in enumerate(options))
        text = f"{question}\n\n{listed}\n\n直接回复内容或序号。"
    await asyncio.to_thread(claw.send_message, cfg, text)
    state["pushed"] = True

    async with _poll_lock:
        while True:
            for m in await asyncio.to_thread(claw.poll_once, cfg):
                reply = claw.msg_text(m)
                if reply:
                    return _resolve_option(reply, options)


@server.tool(
    description=(
        "向用户提问并等待回答。先在用户电脑上弹出输入框；若 local_wait_seconds 秒内"
        "无人操作（或用户按 Esc 关掉弹窗），自动把问题推送到用户微信，用户可在微信直接回复。"
        "两个通道谁先回答用谁。适用于任何需要用户决策、确认或选择的场景——"
        "尤其是用户可能不在电脑前时。比干等着不动要好。"
    )
)
async def ask_remote(
    question: str,
    ctx: Context,
    options: list[str] | None = None,
    local_wait_seconds: float = 30,
    timeout_seconds: float = 600,
) -> str:
    """向用户提问，本地弹窗与微信双通道竞速。

    Args:
        question: 要问用户的问题，写完整，用户在微信上看不到上下文。
        options: 可选的候选项；会变成弹窗按钮，微信端可回复序号。
        local_wait_seconds: 等本地多久没动静才推微信，默认 30 秒。
        timeout_seconds: 总超时，默认 600 秒。
    """
    if question == "__debug__":
        caps = getattr(ctx, "client_capabilities", None)
        elicitation = getattr(caps, "elicitation", None)
        return json.dumps(
            {
                "protocol_version": str(getattr(ctx, "protocol_version", None)),
                "elicitation_raw": repr(elicitation),
                "would_use_native_ui": bool(elicitation),
                "capabilities_repr": repr(caps)[:1000],
            },
            ensure_ascii=False,
            indent=2,
        )

    cfg = claw.load_config()
    if not cfg.get("bot_token"):
        return "微信 ClawBot 未登录。让我调用 wechat_login 工具扫码绑定即可。"
    if not cfg.get("context_token"):
        return "微信 ClawBot 缺少 context_token，请先在微信里给 ClawBot 发任意一条消息。"

    options = options or []
    state = {"pushed": False, "local_kind": "本地弹窗"}
    closed_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    # 负数：wechat-ask-ui plugin 已在 Claude 界面里发问，答案会落到 pre-answer.json，
    # 绝对值是推微信前的等待秒数
    allow_hold = local_wait_seconds < 0
    if allow_hold:
        state["local_kind"] = "Claude 界面"
        delay = -local_wait_seconds
        _take_hold()  # 丢掉上一轮可能残留的信号
        local = asyncio.create_task(_poll_pre_answer())
    else:
        delay = local_wait_seconds
        # 客户端声明了 elicitation 不代表真能用，走不通时 _ask_local_channel 会退回本地窗口
        native = bool(getattr(getattr(ctx, "client_capabilities", None), "elicitation", None))
        local = asyncio.create_task(
            _ask_local_channel(ctx, question, options, closed_event, native, state)
        )

    remote = asyncio.create_task(
        _ask_wechat(cfg, question, options, delay, closed_event, state, allow_hold=allow_hold)
    )
    pending = {local, remote}

    answer = None
    source = None
    deadline = loop.time() + timeout_seconds

    while pending and answer is None:
        remaining = deadline - loop.time()
        if remaining <= 0:
            break
        done, pending = await asyncio.wait(
            pending, timeout=remaining, return_when=asyncio.FIRST_COMPLETED
        )
        if not done:
            break
        for task in done:
            try:
                result = task.result()
            except Exception as exc:
                # 别静默吞掉：一个 NameError 曾让整条本地通道无声失效
                _log_error({"task_failed": repr(exc)})
                result = None
            if result and answer is None:
                answer = result
                source = state["local_kind"] if task is local else "微信"

    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)

    if answer == CANCELLED:
        return "用户关闭了这次询问，没有作答。别再追问同一件事，按你自己的判断继续或停下。"

    if answer is None:
        if state.get("held"):
            return (
                f"用户点了「我在想」取消了微信推送，但最终没有在 {timeout_seconds:.0f} 秒内回答。"
            )
        return f"用户未在 {timeout_seconds:.0f} 秒内回答（本地弹窗和微信都没有响应）。"

    if source != "微信" and state["pushed"]:
        try:
            await asyncio.to_thread(claw.send_message, cfg, f"（已在电脑上回答：{answer}）")
        except Exception:
            pass

    return f"[{source}] {answer}"


@server.tool(description="诊断用：返回当前 MCP 客户端声明的能力，不弹窗也不推送微信。")
async def debug_client_info(ctx: Context) -> str:
    caps = getattr(ctx, "client_capabilities", None)
    elicitation = getattr(caps, "elicitation", None)
    info = {
        "protocol_version": str(getattr(ctx, "protocol_version", None)),
        "elicitation_raw": repr(elicitation),
        "elicitation_truthy": bool(elicitation),
        "would_use_native_ui": bool(elicitation),
        "capabilities_repr": repr(caps)[:1000],
    }
    return json.dumps(info, ensure_ascii=False, indent=2)


@server.tool(
    description=(
        "登录（绑定）微信 ClawBot。会弹出一张二维码图片，用手机微信扫码并确认后即完成绑定，"
        "凭据存到 ~/.wechat-ask/config.json。ask_remote 报告未登录时用它。"
    )
)
async def wechat_login(ctx: Context) -> str:
    """扫码绑定微信 ClawBot。二维码会用系统默认看图程序自动打开。"""
    cfg = claw.load_config()
    if cfg.get("bot_token"):
        return "已经登录过了。要重新绑定请先删除 ~/.wechat-ask/config.json。"

    def _run():
        import urllib.request

        with urllib.request.urlopen(
            claw.BASE_URL + "/ilink/bot/get_bot_qrcode?bot_type=3", timeout=20
        ) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        handle, content = data.get("qrcode"), data.get("qrcode_img_content")
        if not handle or not content:
            return None, f"获取二维码失败：{data}"

        png = None
        try:
            import qrcode

            os.makedirs(claw.STATE_DIR, exist_ok=True)
            png = os.path.join(claw.STATE_DIR, "qrcode.png")
            qr = qrcode.QRCode(border=2)
            qr.add_data(content)
            qr.make(fit=True)
            qr.make_image().save(png)
            if hasattr(os, "startfile"):
                os.startfile(png)  # noqa: S606
        except Exception:
            png = None

        return (handle, png), content

    prepared, detail = await asyncio.to_thread(_run)
    if prepared is None:
        return detail

    handle, png = prepared
    where = f"二维码已打开（{png}）" if png else f"用这个链接自行生成二维码：{detail}"

    deadline = time.time() + 180
    path = "/ilink/bot/get_qrcode_status?qrcode=" + urllib.parse.quote(handle, safe="")
    while time.time() < deadline:
        try:
            status = await asyncio.to_thread(
                claw._call, path, None, None, {"iLink-App-ClientVersion": "1"}, 40
            )
        except Exception:
            await asyncio.sleep(1)
            continue

        state = status.get("status")
        if state == "confirmed":
            cfg.update(
                {
                    "bot_token": status.get("bot_token"),
                    "bot_id": status.get("ilink_bot_id"),
                    "user_id": status.get("ilink_user_id"),
                }
            )
            await asyncio.to_thread(claw.save_config, cfg)
            return (
                f"绑定成功（bot_id={cfg['bot_id']}）。还差最后一步：请在微信里给 ClawBot "
                "随便发一条消息，这样才能拿到主动推送所需的 context_token。"
            )
        if state == "expired":
            return f"{where}，但二维码已过期，请再调用一次 wechat_login。"
        await asyncio.sleep(1)

    return f"{where}，但 180 秒内没有扫码确认，请再调用一次 wechat_login。"


if __name__ == "__main__":
    server.run(transport="stdio")
