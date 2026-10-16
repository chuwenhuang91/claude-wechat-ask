#!/usr/bin/env python3
"""微信 ClawBot iLink 最小链路验证：扫码登录 -> 主动推送 -> 等待回复。

用法:
    python ilink.py login    扫码绑定，凭据写入 ~/.wechat-ask/config.json
    python ilink.py run      推送一条消息到微信，然后阻塞等你回复
    python ilink.py listen   只收不发，打印所有进来的消息
    python ilink.py status   查看当前凭据状态
"""

import base64
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE_URL = "https://ilinkai.weixin.qq.com"
STATE_DIR = os.path.join(os.path.expanduser("~"), ".wechat-ask")
CONFIG_PATH = os.path.join(STATE_DIR, "config.json")

# getupdates 与 sendmessage 的 channel_version 不同，照抄官方实现，勿擅改
CHANNEL_VERSION_RECV = "1.0.0"
CHANNEL_VERSION_SEND = "1.0.2"


def _wechat_uin():
    n = int.from_bytes(os.urandom(4), "big")
    return base64.b64encode(str(n).encode("ascii")).decode("ascii")


def _headers(token=None, json_body=True):
    h = {"AuthorizationType": "ilink_bot_token", "X-WECHAT-UIN": _wechat_uin()}
    if json_body:
        h["Content-Type"] = "application/json"
    if token:
        h["Authorization"] = "Bearer " + token
    return h


def _call(path, token=None, payload=None, extra_headers=None, timeout=45):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    h = _headers(token, json_body=data is not None)
    if extra_headers:
        h.update(extra_headers)
    req = urllib.request.Request(
        BASE_URL + path, data=data, headers=h, method="POST" if data else "GET"
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8")
    return json.loads(body) if body.strip() else {}


def load_config():
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_config(cfg):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def _print_qr(content):
    try:
        import qrcode
    except ImportError:
        print("未安装 qrcode 库，请运行: pip install qrcode")
        print("二维码原始内容:", content)
        return
    qr = qrcode.QRCode(border=2)
    qr.add_data(content)
    qr.make(fit=True)
    qr.print_ascii(invert=True)
    os.makedirs(STATE_DIR, exist_ok=True)
    png_path = os.path.join(STATE_DIR, "qrcode.png")
    try:
        qr.make_image().save(png_path)
        print(f"(终端二维码糊了的话，打开这个文件扫: {png_path})")
    except Exception:
        print("(终端二维码糊了的话，用下面这段内容自行生成二维码)")
        print(content)


def cmd_login():
    cfg = load_config()
    while True:
        with urllib.request.urlopen(
            BASE_URL + "/ilink/bot/get_bot_qrcode?bot_type=3", timeout=20
        ) as resp:
            d = json.loads(resp.read().decode("utf-8"))
        handle, content = d.get("qrcode"), d.get("qrcode_img_content")
        if not handle or not content:
            print("获取二维码失败，原始响应:", d)
            return 1
        _print_qr(content)
        print("\n请用微信扫描上面的二维码并在手机上确认...\n")

        path = "/ilink/bot/get_qrcode_status?qrcode=" + urllib.parse.quote(handle, safe="")
        scanned = False
        expired = False
        while not expired:
            try:
                s = _call(path, extra_headers={"iLink-App-ClientVersion": "1"}, timeout=40)
            except (urllib.error.URLError, TimeoutError, OSError):
                time.sleep(1)
                continue
            status = s.get("status")
            if status == "confirmed":
                cfg.update(
                    {
                        "bot_token": s.get("bot_token"),
                        "bot_id": s.get("ilink_bot_id"),
                        "user_id": s.get("ilink_user_id"),
                    }
                )
                save_config(cfg)
                print(f"登录成功。bot_id={cfg['bot_id']}  user_id={cfg['user_id']}")
                print(f"凭据已保存到 {CONFIG_PATH}")
                return 0
            if status == "expired":
                print("二维码已过期，正在重新获取...")
                expired = True
                continue
            if status == "scaned" and not scanned:
                scanned = True
                print("已扫描，请在手机上点击确认")
            time.sleep(1)


def get_updates(cfg, timeout=45):
    """长轮询拉取新消息。顺带刷新 context_token 和游标。"""
    payload = {
        "get_updates_buf": cfg.get("get_updates_buf", ""),
        "base_info": {"channel_version": CHANNEL_VERSION_RECV},
    }
    d = _call("/ilink/bot/getupdates", token=cfg["bot_token"], payload=payload, timeout=timeout)
    if d.get("ret") or d.get("errcode"):
        raise RuntimeError(f"getupdates 失败: {d}")

    dirty = False
    if d.get("get_updates_buf"):
        cfg["get_updates_buf"] = d["get_updates_buf"]
        dirty = True
    msgs = d.get("msgs") or []
    for m in msgs:
        if m.get("context_token"):
            cfg["context_token"] = m["context_token"]
            dirty = True
    if dirty:
        save_config(cfg)
    return msgs


def poll_once(cfg):
    """长轮询一轮，超时返回空列表而不是报错。"""
    try:
        return get_updates(cfg)
    except (urllib.error.URLError, TimeoutError, OSError):
        return []


def msg_text(m):
    parts = []
    for item in m.get("item_list") or []:
        if item.get("type") == 1:
            text = (item.get("text_item") or {}).get("text")
            if text:
                parts.append(text)
    return "\n".join(parts)


def send_message(cfg, text):
    msg = {
        "from_user_id": "",
        "to_user_id": cfg["user_id"],
        "client_id": f"clawtest-{int(time.time() * 1000)}-{os.urandom(4).hex()}",
        "message_type": 2,
        "message_state": 2,
        "context_token": cfg["context_token"],
        "item_list": [{"type": 1, "text_item": {"text": text}}],
    }
    d = _call(
        "/ilink/bot/sendmessage",
        token=cfg["bot_token"],
        payload={"msg": msg, "base_info": {"channel_version": CHANNEL_VERSION_SEND}},
        timeout=20,
    )
    if d.get("ret") or d.get("errcode"):
        raise RuntimeError(f"发送失败: {d.get('errmsg') or d.get('err_msg') or d}")


def _require_login(cfg):
    if not cfg.get("bot_token"):
        print("还没登录，先运行: python ilink.py login")
        return False
    return True


def _ensure_context_token(cfg):
    """context_token 只能从用户发来的消息里捞，没有就没法主动推送。"""
    if cfg.get("context_token"):
        return True
    print("还没有 context_token —— 这是主动推送的前提。")
    print("请现在打开微信，给 ClawBot 随便发一条消息（比如 hi），我在等...\n")
    while not cfg.get("context_token"):
        for m in poll_once(cfg):
            text = msg_text(m)
            if text:
                print(f"收到: {text}")
    print("已拿到 context_token，可以主动推送了。\n")
    return True


def cmd_run():
    cfg = load_config()
    if not _require_login(cfg):
        return 1
    _ensure_context_token(cfg)

    stamp = time.strftime("%H:%M:%S")
    send_message(cfg, f"[{stamp}] 链路测试：这条消息由你电脑上的脚本主动推送。\n请直接回复任意内容，脚本正在等待。")
    print("已推送到微信，正在等待你的回复...  (Ctrl-C 退出)\n")

    while True:
        for m in poll_once(cfg):
            text = msg_text(m)
            if text:
                print(f"收到回复: {text}")
                print("\n链路验证通过：发得出去，也收得回来。")
                return 0


def cmd_listen():
    cfg = load_config()
    if not _require_login(cfg):
        return 1
    print("监听中，微信里发消息给 ClawBot 试试...  (Ctrl-C 退出)\n")
    while True:
        for m in poll_once(cfg):
            text = msg_text(m)
            print(f"[{time.strftime('%H:%M:%S')}] {text or '(非文本消息)'}")


def cmd_status():
    cfg = load_config()
    if not cfg:
        print(f"无配置文件 ({CONFIG_PATH})，先运行 login")
        return 1
    token = cfg.get("bot_token") or ""
    print(f"配置文件:      {CONFIG_PATH}")
    print(f"bot_id:        {cfg.get('bot_id') or '(无)'}")
    print(f"user_id:       {cfg.get('user_id') or '(无)'}")
    print(f"bot_token:     {token[:12] + '...' if token else '(无)'}")
    print(f"context_token: {'有' if cfg.get('context_token') else '(无 —— 需先在微信给 bot 发条消息)'}")
    print(f"可主动推送:    {'是' if cfg.get('bot_token') and cfg.get('context_token') else '否'}")
    return 0


COMMANDS = {"login": cmd_login, "run": cmd_run, "listen": cmd_listen, "status": cmd_status}


def main():
    # line_buffering 不可省：输出重定向到文件/管道时默认块缓冲，长驻的 listen 会看不到任何输出
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd not in COMMANDS:
        print(__doc__)
        return 1
    try:
        return COMMANDS[cmd]()
    except KeyboardInterrupt:
        print("\n已退出")
        return 130
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:500]}")
        if e.code == 401:
            print("token 失效，重新运行 login")
        return 1
    except RuntimeError as e:
        print(e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
