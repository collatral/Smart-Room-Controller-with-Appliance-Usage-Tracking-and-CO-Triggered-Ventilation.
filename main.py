import json
import time
import gc
import network
import uasyncio as asyncio
from machine import Pin, I2C, ADC
from rotary_irq_esp import RotaryIRQ

# ---------------- config ----------------
RELAY_ACTIVE_LOW = True
PIN_PIR = 33
PIN_SDA, PIN_SCL = 21, 22
PIN_CO = 34
CO_WARMUP_S = 180
CO_ALERT_RATIO = 1.5
CO_CLEAR_RATIO = 1.25
CO_FAN_INDEX = 1
OLED_CHIP = "sh1106"
PIN_ENC_CLK, PIN_ENC_DT, PIN_ENC_SW = 18, 19, 25
ENC_DIV = 1
PIR_WARMUP_MS = 60000
TMO_MIN, TMO_MAX, TMO_STEP = 10, 60, 5
LONG_PRESS_MS = 800
SETTINGS_FILE = "settings.json"
WIFI_FILE = "wifi.json"
AP_SSID = "RoomControl"
AP_PASS = "12345678"
TG_TOKEN = ""      # token from @BotFather. Leave "" to switch the bot off.
TG_CHAT_ID = 0     # your numeric Telegram chat id
WATTS_MAX = 10000
HIST_LEN = 60          # points kept for the power graph
HIST_EVERY_S = 60      # one point per minute
DEVICES = [
    # name,     pin, mode,     tmo, service_hours
    ("Light",    4, "auto",   15, 1000),
    ("Fan",     15, "manual", 30, 5000),
    ("Relay 3", 5, "manual", 30, 5000),
    ("Relay 4", 17, "manual", 30, 5000),
]

# ---------------- state ----------------
boot_t = time.ticks_ms()
last_motion = boot_t
warmed = False
dirty = False
sta_ip = None
hist = []


def clamp_tmo(m):
    return max(TMO_MIN, min(TMO_MAX, int(m)))


def clamp_watts(w):
    return max(0.0, min(float(WATTS_MAX), float(w)))


events = []


def log(msg):
    events.append("[%dm] %s" % (time.ticks_ms() // 60000, msg))
    if len(events) > 10:
        events.pop(0)
    print(events[-1])


class Device:
    def __init__(self, name, pin, mode, tmo, service):
        self.name = name
        self.service = service
        self.mode = mode
        self.tmo = tmo
        self.watts = 0.0
        self.relay = Pin(pin, Pin.OUT, value=1 if RELAY_ACTIVE_LOW else 0)
        self.on = False
        self.total = 0.0
        self.mark = time.ticks_ms()
        self.by = "boot"

    @property
    def due(self):
        return self.total / 3600 >= self.service

    @property
    def kwh(self):
        return self.total / 3600 * self.watts / 1000

    def update(self, now):
        global dirty
        if self.on:
            self.total += time.ticks_diff(now, self.mark) / 1000
            dirty = True
        self.mark = now

    def set(self, on, by):
        global dirty
        self.update(time.ticks_ms())
        if on != self.on:
            self.on = on
            self.relay.value(int(on != RELAY_ACTIVE_LOW))
            dirty = True
            log("%s %s (%s)" % (self.name, "ON" if on else "OFF", by))
        self.by = by


devs = [Device(*d) for d in DEVICES]


def load():
    try:
        with open(SETTINGS_FILE) as f:
            data = json.load(f)
        for d, s in zip(devs, data):
            d.name = str(s.get("name", d.name))[:12]
            d.mode = s.get("mode", d.mode)
            d.tmo = clamp_tmo(s.get("tmo", d.tmo))
            d.total = float(s.get("total", 0))
            d.watts = clamp_watts(s.get("watts", 0))
    except (OSError, ValueError, TypeError):
        pass


def save():
    global dirty
    now = time.ticks_ms()
    for d in devs:
        d.update(now)
    data = [{"name": d.name, "mode": d.mode, "tmo": d.tmo,
             "total": int(d.total), "watts": d.watts} for d in devs]
    with open(SETTINGS_FILE, "w") as f:
        json.dump(data, f)
    dirty = False


# ---------------- wifi ----------------
def load_wifi():
    try:
        with open(WIFI_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def save_wifi(ssid, pwd):
    with open(WIFI_FILE, "w") as f:
        json.dump({"ssid": ssid, "pwd": pwd}, f)


def clear_wifi():
    try:
        import uos
        uos.remove(WIFI_FILE)
    except OSError:
        pass


def start_ap():
    ap = network.WLAN(network.AP_IF)
    ap.active(True)
    ap.config(essid=AP_SSID, password=AP_PASS)
    log("AP started: " + AP_SSID)
    return ap


async def try_sta(ssid, pwd, timeout=15):
    global sta_ip
    sta = network.WLAN(network.STA_IF)
    sta.active(True)
    sta.connect(ssid, pwd)
    for _ in range(timeout * 5):
        if sta.isconnected():
            sta_ip = sta.ifconfig()[0]
            log("STA connected: " + sta_ip)
            return sta_ip
        await asyncio.sleep_ms(200)
    sta.active(False)
    sta_ip = None
    log("STA failed")
    return None


def disconnect_sta():
    global sta_ip
    sta = network.WLAN(network.STA_IF)
    sta.active(False)
    sta_ip = None
    log("STA disconnected")


def ip():
    if sta_ip:
        return sta_ip
    return "192.168.4.1"


# ---------------- actions ----------------
def restart_timer():
    global last_motion
    last_motion = time.ticks_ms()


def act_toggle(d, by):
    d.mode = "manual"
    d.set(not d.on, by)
    save()


def act_mode(d, mode, by):
    if mode not in ("auto", "manual"):
        raise ValueError
    d.mode = mode
    d.by = by
    if mode == "auto":
        restart_timer()
    save()


def act_timeout(d, minutes):
    d.tmo = clamp_tmo(minutes)
    save()


def act_name(d, name):
    name = name.strip()[:12]
    if name:
        d.name = name
    save()


def act_watts(d, w):
    d.update(time.ticks_ms())
    d.watts = clamp_watts(w)
    log("%s rating %dW" % (d.name, d.watts))
    save()


def act_reset(d):
    d.update(time.ticks_ms())
    d.total = 0.0
    log("%s hours reset" % d.name)
    save()


# ---------------- CO monitor ----------------
co_adc = ADC(Pin(PIN_CO))
co_adc.atten(ADC.ATTN_11DB)
co_fan = devs[CO_FAN_INDEX]
co_base = None
co_ratio = 1.0
co_alarm = False
co_forced = False


def read_co():
    t = 0
    for _ in range(16):
        t += co_adc.read_u16()
    return t / 16


def co_warm_left():
    return max(0, CO_WARMUP_S - time.ticks_diff(time.ticks_ms(), boot_t) // 1000)


def co_zero():
    global co_base, co_alarm
    co_base = max(read_co(), 1000)
    co_alarm = False
    log("CO baseline reset")


async def co_task():
    global co_base, co_ratio, co_alarm, co_forced
    while True:
        await asyncio.sleep_ms(1000)
        if co_base is None:
            if co_warm_left() == 0:
                co_base = max(read_co(), 1000)
                log("CO baseline set")
            continue
        co_ratio = read_co() / co_base
        if not co_alarm and co_ratio >= CO_ALERT_RATIO:
            co_alarm = True
            log("CO HIGH %.1fx" % co_ratio)
            if not co_fan.on:
                co_fan.set(True, "co")
                co_forced = True
        elif co_alarm and co_ratio <= CO_CLEAR_RATIO:
            co_alarm = False
            log("CO normal")
            if co_forced and co_fan.on:
                co_fan.set(False, "co")
            co_forced = False


# ---------------- PIR / relay logic ----------------
pir = Pin(PIN_PIR, Pin.IN)


async def logic_task():
    global last_motion, warmed
    while True:
        now = time.ticks_ms()
        if not warmed and time.ticks_diff(now, boot_t) >= PIR_WARMUP_MS:
            warmed = True
        motion = warmed and pir.value() == 1
        if motion:
            last_motion = now
        for d in devs:
            d.update(now)
            if d.mode != "auto" or not warmed or (co_alarm and d is co_fan):
                continue
            if motion and not d.on:
                d.set(True, "pir")
            elif d.on and time.ticks_diff(now, last_motion) >= d.tmo * 60000:
                d.set(False, "pir")
        await asyncio.sleep_ms(200)


def remaining(d):
    if d.on and d.mode == "auto":
        left = d.tmo * 60 - time.ticks_diff(time.ticks_ms(), last_motion) // 1000
        return max(0, left)
    return None


def power_now():
    return sum(d.watts for d in devs if d.on)


# ---------------- power history ----------------
async def hist_task():
    while True:
        hist.append(int(power_now()))
        if len(hist) > HIST_LEN:
            hist.pop(0)
        await asyncio.sleep(HIST_EVERY_S)


# ---------------- OLED ----------------
i2c = I2C(0, scl=Pin(PIN_SCL), sda=Pin(PIN_SDA), freq=400000)
if OLED_CHIP == "sh1106":
    import sh1106
    oled = sh1106.SH1106_I2C(128, 64, i2c)
else:
    import ssd1306
    oled = ssd1306.SSD1306_I2C(128, 64, i2c)

ITEMS = ("state", "mode", "timeout", "reset", "back")
ui = {"mode": "home", "dev": 0, "cur": 0, "val": 15}


def draw():
    o = oled
    o.fill(0)
    m = ui["mode"]
    d = devs[ui["dev"]]
    d.update(time.ticks_ms())
    if m == "home":
        o.text(d.name[:12], 0, 0)
        o.text("%d/%d" % (ui["dev"] + 1, len(devs)), 104, 0)
        o.text("ON " if d.on else "OFF", 0, 14)
        o.text(d.mode.upper(), 48, 14)
        o.text("Hrs %.2f h" % (d.total / 3600), 0, 28)
        r = remaining(d)
        if d.due and (time.ticks_ms() // 500) % 2:
            o.text("SERVICE DUE!", 0, 42)
        elif r is not None:
            o.text("Off in %02d:%02d" % (r // 60, r % 60), 0, 42)
        elif d.mode == "auto" and not warmed:
            o.text("PIR warming up", 0, 42)
        if sta_ip and (time.ticks_ms() // 3000) % 2:
            o.text(sta_ip, 0, 56)
        elif co_base is None:
            o.text("CO warm %ds" % co_warm_left(), 0, 56)
        else:
            o.text("CO %.1fx%s" % (co_ratio, " HIGH!" if co_alarm else ""), 0, 56)
    elif m == "menu":
        o.text(d.name[:12], 0, 0)
        labels = (
            "State: " + ("ON" if d.on else "OFF"),
            "Mode: " + d.mode.upper(),
            "Timeout: %dm" % d.tmo,
            "Reset hours",
            "Back",
        )
        for i, t in enumerate(labels):
            o.text((">" if i == ui["cur"] else " ") + t, 0, 12 + i * 10)
    elif m == "edit":
        o.text("PIR timeout", 0, 0)
        o.text("%d min" % ui["val"], 32, 26)
        o.text("Click=OK Hold=No", 0, 56)
    elif m == "confirm":
        o.text("Reset hours?", 0, 0)
        o.text(d.name[:12], 0, 20)
        o.text("Click=Y  Hold=N", 0, 56)
    o.show()


async def display_task():
    while True:
        draw()
        await asyncio.sleep_ms(250)


# ---------------- encoder / button ----------------
def on_rotate(n):
    m = ui["mode"]
    if m == "home":
        ui["dev"] = (ui["dev"] + n) % len(devs)
    elif m == "menu":
        ui["cur"] = (ui["cur"] + n) % len(ITEMS)
    elif m == "edit":
        ui["val"] = max(TMO_MIN, min(TMO_MAX, ui["val"] + n * TMO_STEP))


def on_press(long):
    m = ui["mode"]
    d = devs[ui["dev"]]
    if long:
        ui["mode"] = "home" if m in ("home", "menu") else "menu"
        return
    if m == "home":
        ui["mode"], ui["cur"] = "menu", 0
    elif m == "menu":
        item = ITEMS[ui["cur"]]
        if item == "state":
            act_toggle(d, "encoder")
        elif item == "mode":
            act_mode(d, "manual" if d.mode == "auto" else "auto", "encoder")
        elif item == "timeout":
            ui["val"] = d.tmo
            ui["mode"] = "edit"
        elif item == "reset":
            ui["mode"] = "confirm"
        else:
            ui["mode"] = "home"
    elif m == "edit":
        act_timeout(d, ui["val"])
        ui["mode"] = "menu"
    elif m == "confirm":
        act_reset(d)
        ui["mode"] = "menu"


async def input_task():
    enc = RotaryIRQ(PIN_ENC_CLK, PIN_ENC_DT, range_mode=RotaryIRQ.RANGE_UNBOUNDED, pull_up=True)
    sw = Pin(PIN_ENC_SW, Pin.IN, Pin.PULL_UP)
    last = enc.value()
    acc = 0
    down_at = None
    while True:
        v = enc.value()
        acc += v - last
        last = v
        steps = int(acc / ENC_DIV)
        if steps:
            acc -= steps * ENC_DIV
            on_rotate(steps)
        pressed = sw.value() == 0
        now = time.ticks_ms()
        if pressed and down_at is None:
            down_at = now
        elif not pressed and down_at is not None:
            dur = time.ticks_diff(now, down_at)
            down_at = None
            if dur >= LONG_PRESS_MS:
                on_press(True)
            elif dur >= 40:
                on_press(False)
        await asyncio.sleep_ms(10)


# ---------------- web panel ----------------
PAGE = """<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>Room Control</title>
<style>
body{font-family:sans-serif;background:#111;color:#eee;margin:0;padding:12px}
.c{background:#222;border-radius:10px;padding:12px;margin-bottom:12px;line-height:2}
.on{color:#4c4}.off{color:#c55}
button,input,select{font-size:16px;padding:6px 10px;margin-right:4px}
small{color:#999}
canvas{width:100%;display:block;background:#1a1a1a;border-radius:8px;margin:6px 0}
.wifi{background:#1a1a2e;border-radius:10px;padding:12px;margin-bottom:12px}
.badge{display:inline-block;padding:2px 8px;border-radius:6px;font-size:13px}
.ap{background:#444}.sta{background:#2a5}
</style></head><body><h2>Room Control</h2>

<div class=wifi>
  <b>WiFi</b> <span id=wbadge class=badge>...</span>
  <span id=wip style="color:#aaa"></span><br>
  <small id=wsaved style="color:#aaa"></small><br>
  <div id=staform>
    <input id=wssid placeholder="WiFi name (SSID)" style="width:160px">
    <input id=wpwd placeholder="Password" type=password style="width:140px"><br><br>
    <button onclick="connectSTA()">Save & Connect</button>
    <button id=btncon onclick="post('wifi_connect_saved',{})"
      style="display:none;background:#2a5;color:#fff">Connect Saved</button>
  </div>
  <div id=stadis style="display:none">
    <button onclick="post('wifi_disconnect',{})">Disconnect & back to AP</button>
  </div>
</div>

<div class=c><b id=co></b><br>
<button onclick="post('co_zero',{})">Recalibrate CO (clean air)</button></div>

<div class=c>
  <b>Energy used</b> <span id=kt></span><br>
  <canvas id=cb width=360 height=170></canvas>
  <b>Power now</b> <span id=pn></span><br>
  <canvas id=cl width=360 height=150></canvas>
  <small>kWh = rating (W) x hours ON / 1000. Set each appliance's rating below.
  Power graph: last hour, 1 point per minute.</small>
</div>

<div id=d></div>
<h3>Activity</h3><pre id=ev style="white-space:pre-wrap;color:#aaa"></pre>

<script>
const $=i=>document.getElementById(i);
async function post(a,b){
  await fetch('/api/'+a,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)});
  load();
}
async function connectSTA(){
  const s=$('wssid').value.trim(),p=$('wpwd').value;
  if(!s)return alert('Enter WiFi name');
  await post('wifi_connect',{ssid:s,pwd:p});
}
function bars(cv,labels,vals){
  const x=cv.getContext('2d'),W=cv.width,H=cv.height;
  x.clearRect(0,0,W,H);
  const m=Math.max(Math.max(...vals),0.001),bw=W/labels.length;
  x.font='12px sans-serif';x.textAlign='center';
  vals.forEach((v,i)=>{
    const h=(H-45)*v/m;
    x.fillStyle='#4c8';x.fillRect(i*bw+bw*0.2,H-22-h,bw*0.6,h);
    x.fillStyle='#eee';x.fillText(v.toFixed(3)+' kWh',i*bw+bw/2,H-26-h);
    x.fillStyle='#aaa';x.fillText(labels[i],i*bw+bw/2,H-6);
  });
}
function line(cv,v){
  const x=cv.getContext('2d'),W=cv.width,H=cv.height;
  x.clearRect(0,0,W,H);
  const m=Math.max(Math.max(...v,1)*1.1,10);
  x.strokeStyle='#444';x.strokeRect(40,5,W-45,H-25);
  x.fillStyle='#aaa';x.font='11px sans-serif';
  x.textAlign='right';x.fillText(Math.round(m)+'W',37,16);x.fillText('0',37,H-22);
  x.textAlign='left';x.fillText('-'+v.length+' min',42,H-6);
  x.textAlign='right';x.fillText('now',W-6,H-6);
  if(v.length<2)return;
  x.strokeStyle='#4af';x.lineWidth=2;x.beginPath();
  v.forEach((y,i)=>{
    const px=40+(W-45)*i/(v.length-1),py=5+(H-25)*(1-y/m);
    if(i)x.lineTo(px,py);else x.moveTo(px,py);
  });
  x.stroke();
}
async function load(){
  const r=await (await fetch('/api/state')).json();
  $('co').textContent=r.co.warm>=0?'CO sensor warming: '+r.co.warm+'s':'CO level: '+r.co.r.toFixed(2)+'x clean-air'+(r.co.alarm?' - HIGH!':'');
  $('ev').textContent=r.ev.join('\\n');
  const w=r.wifi;
  $('wbadge').textContent=w.mode;
  $('wbadge').className='badge '+(w.mode==='STA'?'sta':'ap');
  $('wip').textContent=w.ip;
  $('wsaved').textContent=w.saved?'Saved network: '+w.saved:'No saved network';
  $('btncon').style.display=w.saved&&w.mode==='AP'?'inline-block':'none';
  $('staform').style.display=w.mode==='STA'?'none':'block';
  $('stadis').style.display=w.mode==='STA'?'block':'none';
  r.d.forEach(d=>{
    const i=d.id;
    if(!$('c'+i)){
      const c=document.createElement('div');c.id='c'+i;c.className='c';
      c.innerHTML=`<b id=n${i}></b> <b id=s${i}></b><br><small id=i${i}></small><br>
      <button onclick="post('toggle',{id:${i}})">Toggle</button>
      <select id=m${i} onchange="post('mode',{id:${i},mode:this.value})">
        <option value=auto>Auto (PIR)</option><option value=manual>Manual</option>
      </select><br>
      Timeout <input id=t${i} type=number min=10 max=60 step=5 style="width:64px"> min
      <button onclick="post('timeout',{id:${i},min:+$('t${i}').value})">Set</button><br>
      Power <input id=w${i} type=number min=0 max=10000 step=1 style="width:84px"> W
      <button onclick="post('watts',{id:${i},w:+$('w${i}').value})">Set</button><br>
      Name <input id=e${i} maxlength=12 style="width:120px">
      <button onclick="post('name',{id:${i},name:$('e${i}').value})">Save</button><br>
      <button onclick="if(confirm('Reset hours and kWh?'))post('reset',{id:${i}})">Reset hours</button>`;
      $('d').appendChild(c);
    }
    $('n'+i).textContent=d.name;
    const s=$('s'+i);s.textContent=d.on?'ON':'OFF';s.className=d.on?'on':'off';
    let t='Hours: '+d.hours.toFixed(2)+' h | '+d.kwh.toFixed(3)+' kWh';
    if(d.left>=0)t+=' | off in '+Math.floor(d.left/60)+':'+String(d.left%60).padStart(2,'0');
    if(d.due)t+=' | SERVICE DUE';
    $('c'+i).style.borderLeft=d.due?'5px solid #c55':'none';
    $('i'+i).textContent=t+' | last: '+d.by;
    const f=document.activeElement;
    if($('m'+i)!==f)$('m'+i).value=d.mode;
    if($('t'+i)!==f)$('t'+i).value=d.tmo;
    if($('w'+i)!==f)$('w'+i).value=d.watts;
    if($('e'+i)!==f)$('e'+i).value=d.name;
  });
  $('kt').textContent='Total: '+r.kwh.toFixed(3)+' kWh';
  $('pn').textContent=r.pw+' W';
  bars($('cb'),r.d.map(d=>d.name),r.d.map(d=>d.kwh));
  line($('cl'),r.hist.concat([r.pw]));
}
setInterval(load,2000);load();
</script></body></html>"""


def api_state():
    now = time.ticks_ms()
    out = []
    total_kwh = 0.0
    for i, d in enumerate(devs):
        d.update(now)
        r = remaining(d)
        total_kwh += d.kwh
        out.append({
            "id": i, "name": d.name, "on": d.on, "mode": d.mode, "tmo": d.tmo,
            "hours": round(d.total / 3600, 2), "left": -1 if r is None else r,
            "by": d.by, "due": d.due,
            "watts": int(d.watts), "kwh": round(d.kwh, 3),
        })
    co = {"r": round(co_ratio, 2), "alarm": co_alarm,
          "warm": co_warm_left() if co_base is None else -1}
    creds = load_wifi()
    wifi = {
        "mode": "STA" if sta_ip else "AP",
        "ip": ip(),
        "saved": creds["ssid"] if creds else None
    }
    return {"d": out, "ev": events[::-1], "co": co, "wifi": wifi,
            "kwh": round(total_kwh, 3), "pw": int(power_now()), "hist": hist}


async def handle_wifi_connect(b):
    ssid = str(b.get("ssid", "")).strip()
    pwd = str(b.get("pwd", ""))
    if not ssid:
        return {"error": "no ssid"}, 400
    save_wifi(ssid, pwd)
    result = await try_sta(ssid, pwd)
    if result:
        return {"ok": True, "ip": result}, 200
    return {"error": "failed, credentials saved, check ssid/pass"}, 400


def api_post_sync(action, b):
    if action == "co_zero":
        co_zero()
        return {"ok": True}, 200
    if action == "wifi_disconnect":
        disconnect_sta()
        return {"ok": True}, 200
    if action == "wifi_connect_saved":
        creds = load_wifi()
        if not creds:
            return {"error": "no saved credentials"}, 400
        asyncio.create_task(try_sta(creds["ssid"], creds["pwd"]))
        return {"ok": True, "msg": "connecting..."}, 200
    try:
        d = devs[int(b["id"])]
        if action == "toggle":
            act_toggle(d, "web")
        elif action == "mode":
            act_mode(d, b["mode"], "web")
        elif action == "timeout":
            act_timeout(d, b["min"])
        elif action == "watts":
            act_watts(d, b["w"])
        elif action == "name":
            act_name(d, str(b["name"]))
        elif action == "reset":
            act_reset(d)
        else:
            return {"error": "unknown"}, 404
    except (KeyError, ValueError, TypeError, IndexError):
        return {"error": "bad request"}, 400
    return {"ok": True}, 200


async def handle(reader, writer):
    try:
        req = (await reader.readline()).decode().split()
        clen = 0
        while True:
            h = await reader.readline()
            if h in (b"\r\n", b"\n", b""):
                break
            if h.lower().startswith(b"content-length:"):
                clen = int(h.split(b":")[1])
        body = await reader.readexactly(clen) if clen else b""
        method, path = req[0], req[1]
        code, ctype = 200, "application/json"
        if method == "GET" and path == "/":
            ctype, out = "text/html", PAGE
        elif method == "GET" and path == "/api/state":
            out = json.dumps(api_state())
        elif method == "POST" and path.startswith("/api/"):
            try:
                b = json.loads(body) if body else {}
            except ValueError:
                b = None
            if isinstance(b, dict):
                action = path[5:]
                if action == "wifi_connect":
                    res, code = await handle_wifi_connect(b)
                else:
                    res, code = api_post_sync(action, b)
            else:
                res, code = {"error": "bad json"}, 400
            out = json.dumps(res)
        else:
            code, out = 404, "{}"
        writer.write(("HTTP/1.0 %d X\r\nContent-Type: %s\r\nConnection: close\r\n\r\n" % (code, ctype)).encode())
        writer.write(out.encode())
        await writer.drain()
    except Exception:
        pass
    finally:
        writer.close()
        await writer.wait_closed()


# ---------------- housekeeping ----------------
async def saver_task():
    while True:
        await asyncio.sleep(120)
        if dirty:
            save()
        gc.collect()


# ---------------- main ----------------
async def main():
    load()

    # Always boot into AP mode
    start_ap()

    oled.fill(0)
    oled.text("Room Control", 0, 0)
    oled.text("AP: " + AP_SSID, 0, 16)
    oled.text("192.168.4.1", 0, 28)
    oled.text("PIR warms 60s", 0, 40)
    # show saved wifi hint on boot
    creds = load_wifi()
    if creds:
        oled.text("Saved:" + creds["ssid"][:8], 0, 56)
    oled.show()
    await asyncio.sleep(2)

    for t in (logic_task, co_task, input_task, display_task, saver_task, hist_task):
        asyncio.create_task(t())

    if TG_TOKEN:
        try:
            import tgbot
            ctx = {
                "devs": devs, "save": save, "act_mode": act_mode, "events": events,
                "co": lambda: (co_base, co_ratio, co_alarm),
                "online": lambda: sta_ip is not None,
            }
            asyncio.create_task(tgbot.run(TG_TOKEN, TG_CHAT_ID, ctx))
        except Exception as e:
            log("telegram off: " + str(e))

    await asyncio.start_server(handle, "0.0.0.0", 80)
    while True:
        await asyncio.sleep(3600)


asyncio.run(main())
