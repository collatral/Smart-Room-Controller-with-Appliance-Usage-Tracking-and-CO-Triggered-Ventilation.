# tgbot.py - optional Telegram control for the room controller.
# Works only while the board is joined to a home WiFi that has internet.
# Plain polling: every few seconds it asks Telegram for new messages.
# Only the chat id you set in main.py is obeyed; everyone else is ignored.
import json
import gc
import time
import uasyncio as asyncio

HOST = "api.telegram.org"
POLL_S = 4
HELP = ("Commands:\n/status\n/on <number or name>\n/off <number or name>\n"
        "/auto <number or name>\n/manual <number or name>\n/co\n/log")


def _q(s):
    """URL-encode text for the query string."""
    out = ""
    for c in s:
        o = ord(c)
        if (48 <= o <= 57) or (65 <= o <= 90) or (97 <= o <= 122) or c in "-_.~":
            out += c
        else:
            for b in c.encode():
                out += "%%%02X" % b
    return out


async def _call(token, method, query=""):
    """One HTTPS GET to the Bot API. Returns the parsed JSON, or None."""
    w = None
    try:
        r, w = await asyncio.open_connection(HOST, 443, ssl=True)
        req = "GET /bot%s/%s%s HTTP/1.0\r\nHost: %s\r\nConnection: close\r\n\r\n" % (
            token, method, query, HOST)
        w.write(req.encode())
        await w.drain()
        data = await r.read()
        i = data.find(b"\r\n\r\n")
        if i < 0:
            return None
        return json.loads(data[i + 4:])
    finally:
        if w:
            w.close()


async def _send(token, chat_id, text):
    q = "?chat_id=%d&text=%s" % (chat_id, _q(text[:700]))
    res = await asyncio.wait_for(_call(token, "sendMessage", q), 25)
    if not (res and res.get("ok")):
        print("telegram: send failed:", res)


def _co_text(ctx):
    base, ratio, alarm = ctx["co"]()
    if base is None:
        return "CO: sensor warming up"
    return "CO: %.2fx clean-air%s" % (ratio, " - HIGH" if alarm else "")


def _status(ctx):
    now = time.ticks_ms()
    lines = []
    for i, d in enumerate(ctx["devs"]):
        d.update(now)
        lines.append("%d %s: %s (%s) %.2f h, %.3f kWh%s" % (
            i + 1, d.name, "ON" if d.on else "OFF", d.mode,
            d.total / 3600, d.kwh, " SERVICE DUE" if d.due else ""))
    lines.append(_co_text(ctx))
    return "\n".join(lines)


def _find(ctx, arg):
    arg = arg.strip().lower()
    devs = ctx["devs"]
    if arg.isdigit() and 1 <= int(arg) <= len(devs):
        return devs[int(arg) - 1]
    for d in devs:
        if d.name.lower() == arg:
            return d
    return None


def handle(ctx, text):
    """Turn one command into a reply string (None = say nothing)."""
    parts = text.strip().split(None, 1)
    if not parts:
        return None
    cmd = parts[0].lower().split("@")[0]
    arg = parts[1] if len(parts) > 1 else ""
    if cmd in ("/start", "/help"):
        return HELP
    if cmd == "/status":
        return _status(ctx)
    if cmd == "/co":
        return _co_text(ctx)
    if cmd == "/log":
        return "\n".join(ctx["events"][::-1]) or "No events yet"
    if cmd in ("/on", "/off", "/auto", "/manual"):
        d = _find(ctx, arg)
        if d is None:
            return "Which device? Example: %s 1" % cmd
        if cmd in ("/on", "/off"):
            d.mode = "manual"                  # same rule as the web page and encoder
            d.set(cmd == "/on", "telegram")
            ctx["save"]()
        else:
            ctx["act_mode"](d, cmd[1:], "telegram")
        return "%s: %s (%s)" % (d.name, "ON" if d.on else "OFF", d.mode)
    return "Unknown command. Send /help"


async def run(token, chat_id, ctx):
    chat_id = int(chat_id)
    offset = 0
    primed = False          # first poll only skips old messages
    last_alarm = None
    due_sent = set()
    waited = False
    await asyncio.sleep(10)
    while True:
        try:
            if not ctx["online"]() and not waited:
                waited = True
                print("telegram: waiting for home WiFi (press Connect Saved)")
            if ctx["online"]():
                waited = False
                res = await asyncio.wait_for(
                    _call(token, "getUpdates", "?timeout=0&limit=5&offset=%d" % offset), 25)
                if not (res and res.get("ok")):
                    print("telegram: poll failed:", res)
                if res and res.get("ok"):
                    for u in res["result"]:
                        offset = u["update_id"] + 1
                        if not primed:
                            continue                       # ignore old backlog
                        m = u.get("message") or {}
                        if m.get("chat", {}).get("id") != chat_id:
                            print("telegram: ignored a message from chat id",
                                  m.get("chat", {}).get("id"), "- TG_CHAT_ID is", chat_id)
                            continue                       # not you
                        t = m.get("text") or ""
                        if t.startswith("/"):
                            reply = handle(ctx, t)
                            if reply:
                                await _send(token, chat_id, reply)
                    if not primed:
                        primed = True
                        print("telegram: connected, listening")
                        await _send(token, chat_id, "Room controller online. Send /help")

                    # alerts: CO alarm changes and service-due
                    base, ratio, alarm = ctx["co"]()
                    if last_alarm is None:
                        last_alarm = alarm
                    elif alarm != last_alarm:
                        last_alarm = alarm
                        await _send(token, chat_id, "CO HIGH (%.2fx clean-air)" % ratio
                                    if alarm else "CO back to normal")
                    for i, d in enumerate(ctx["devs"]):
                        if d.due and i not in due_sent:
                            due_sent.add(i)
                            await _send(token, chat_id, "SERVICE DUE: " + d.name)
                        elif not d.due:
                            due_sent.discard(i)
        except Exception as e:
            print("telegram:", repr(e))
        gc.collect()
        await asyncio.sleep(POLL_S)
