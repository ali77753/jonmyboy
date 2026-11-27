import os, sys, json, time, subprocess, urllib.request, urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta

TOKEN = os.environ.get("TELEGRAM_TOKEN") or os.environ.get("BOT_TOKEN", "")
CHAT = str(os.environ.get("TELEGRAM_CHAT_ID") or os.environ.get("CHAT_ID", ""))
TZH = float(os.environ.get("TZ_OFFSET", "5"))                 # ваш часовой пояс (Таджикистан = 5)
RUN_SECONDS = int(os.environ.get("RUN_SECONDS", str(5 * 3600 + 50 * 60)))
MAX_COINS = int(os.environ.get("MAX_COINS", "150"))
HOSTS = ["https://data-api.binance.vision", "https://api.binance.com", "https://api1.binance.com",
         "https://api2.binance.com", "https://api3.binance.com", "https://data.binance.com"]
TFS = {"15m": 900, "1h": 3600}
TFL = {"15m": "15 мин", "1h": "1 час"}
VOLA = {"low": {"15m": 0.15, "1h": 0.3}, "mid": {"15m": 0.25, "1h": 0.5}, "high": {"15m": 0.4, "1h": 0.8}}
VOLA_L = {"low": "низкая", "mid": "средняя", "high": "высокая"}
SIDE_L = {"all": "все", "up": "только бычьи", "down": "только медвежьи"}
STABLE = {"USDC", "FDUSD", "TUSD", "USDP", "DAI", "BUSD", "USD1", "USDE", "XUSD", "EUR", "AEUR"}
DEFAULT = {"tfs": ["15m", "1h"], "side": "all", "vol": 20, "vola": "mid", "trend": False, "paused": False}
STATE = "state.json"
HOST_OK = None

# ---------- сеть ----------
def get(path, params=None):
    global HOST_OK
    q = "?" + urllib.parse.urlencode(params) if params else ""
    last = None
    for h in ([HOST_OK] if HOST_OK else []) + [x for x in HOSTS if x != HOST_OK]:
        try:
            with urllib.request.urlopen(urllib.request.Request(h + path + q, headers={"User-Agent": "Mozilla/5.0"}), timeout=20) as r:
                HOST_OK = h
                return json.load(r)
        except Exception as e:
            last = f"{h}: {e}"
    raise RuntimeError(last)

def tg(method, _to=30, **p):
    if not TOKEN:
        print("TG", method, p); return {"ok": True, "result": []}
    data = urllib.parse.urlencode({k: json.dumps(v) if isinstance(v, (dict, list)) else v for k, v in p.items()}).encode()
    try:
        with urllib.request.urlopen("https://api.telegram.org/bot" + TOKEN + "/" + method, data, timeout=_to) as r:
            return json.load(r)
    except Exception as e:
        print("TG error", method, e); return {"ok": False, "result": []}

def say(text, kb=None):
    p = dict(chat_id=CHAT, text=text, parse_mode="HTML", disable_web_page_preview="true")
    if kb: p["reply_markup"] = {"inline_keyboard": kb}
    return tg("sendMessage", **p)

# ---------- состояние ----------
def load():
    try: st = json.load(open(STATE))
    except Exception: st = {}
    if not isinstance(st, dict) or "sent" not in st:
        st = {"sent": {}, "offset": 0, "hist": [], "rank": [], "last_scan": 0, "coins": 0, "cfg": dict(DEFAULT)}
    st["cfg"] = {**DEFAULT, **st.get("cfg", {})}
    st.setdefault("alerts", []); st.setdefault("aid", 0); st.setdefault("await", None)
    return st

def save(st, push=False):
    cut = ms() - 3 * 86400000
    st["sent"] = {k: v for k, v in st["sent"].items() if v > cut}
    json.dump(st, open(STATE, "w"), ensure_ascii=False)
    if push and os.environ.get("GITHUB_ACTIONS"):
        try:
            for c in (["git", "config", "user.name", "bot"], ["git", "config", "user.email", "bot@users.noreply.github.com"], ["git", "add", STATE]):
                subprocess.run(c, check=False)
            if subprocess.run(["git", "diff", "--cached", "--quiet"]).returncode != 0:
                subprocess.run(["git", "commit", "-m", "state"], check=False)
                subprocess.run(["git", "push"], check=False)
        except Exception as e:
            print("git error", e)

# ---------- вспомогательное ----------
def ms(): return int(time.time() * 1000)
def hhmm(t): return (datetime.fromtimestamp(t / 1000, timezone.utc) + timedelta(hours=TZH)).strftime("%H:%M")
TZL = f"UTC{TZH:+g}"
def price(x): return f"{x:.8f}".rstrip("0") if x < 0.001 else f"{x:.6g}"
def ago(t):
    m = int((ms() - t) / 60000)
    return "только что" if m < 1 else f"{m} мин назад"

def nowick(k, min_range):
    o, h, l, c = map(float, k[1:5])
    if o <= 0 or (h - l) / o * 100 < min_range: return None
    if c > o and o == l: return "UP"
    if c < o and o == h: return "DOWN"
    return None

def ema(vals, n=20):
    a = 2 / (n + 1); e = vals[0]
    for v in vals[1:]: e = v * a + e * (1 - a)
    return e

def fetch(a):
    try: return a[0], a[1], get("/api/v3/klines", {"symbol": a[0], "interval": a[1], "limit": 60})
    except Exception: return a[0], a[1], None

# ---------- сканирование ----------
def scan(st, manual=False, startup=False):
    cfg = st["cfg"]; now = ms()
    tick = get("/api/v3/ticker/24hr")
    qv = {t["symbol"]: float(t["quoteVolume"]) for t in tick if t["symbol"].endswith("USDT")
          and t["symbol"][:-4] not in STABLE and float(t["quoteVolume"]) >= cfg["vol"] * 1e6}
    syms = sorted(qv, key=lambda s: -qv[s])[:MAX_COINS]
    minute = datetime.now(timezone.utc).minute
    tfs = [tf for tf in cfg["tfs"] if tf == "15m" or minute < 20 or manual or startup]
    with ThreadPoolExecutor(10) as ex:
        res = list(ex.map(fetch, [(s, tf) for s in syms for tf in tfs]))
    out, rank = [], []
    look = 4 if manual else 2
    for sym, tf, ks in res:
        if not ks: continue
        iv = TFS[tf] * 1000
        closed = [k for k in ks if int(k[0]) + iv <= now]            # ТОЛЬКО закрытые свечи
        if len(closed) < 20: continue
        last = closed[-48:]
        avg = sum((float(k[2]) - float(k[3])) / float(k[1]) for k in last) / len(last) * 100
        if tf == "15m": rank.append((sym, round(avg, 3)))
        thr = VOLA[cfg["vola"]][tf]
        if avg < thr: continue
        for k in closed[-look:]:
            kind = nowick(k, thr)
            if not kind: continue
            if cfg["side"] == "up" and kind != "UP": continue
            if cfg["side"] == "down" and kind != "DOWN": continue
            key = f"{sym}|{tf}|{k[0]}"
            if not manual and key in st["sent"]: continue
            o, c = float(k[1]), float(k[4])
            out.append(dict(key=key, sym=sym, tf=tf, kind=kind, o=o, c=c, rng=(float(k[2]) - float(k[3])) / o * 100,
                            t0=int(k[0]), t1=int(k[6]) + 1, qv=qv[sym]))
    if cfg["trend"] and out:                                          # фильтр по тренду 4ч (EMA20)
        keep = []
        with ThreadPoolExecutor(10) as ex:
            tr = {r[0]: r[2] for r in ex.map(fetch, [(s, "4h") for s in {x["sym"] for x in out}])}
        for x in out:
            ks = [k for k in (tr.get(x["sym"]) or []) if int(k[0]) + 4 * 3600000 <= now]
            if len(ks) < 20: continue
            e = ema([float(k[4]) for k in ks])
            if (x["kind"] == "UP" and x["c"] > e) or (x["kind"] == "DOWN" and x["c"] < e): keep.append(x)
        out = keep
    out.sort(key=lambda x: (x["t1"], x["sym"]))
    st["rank"] = sorted(rank, key=lambda r: -r[1])[:10]; st["last_scan"] = now; st["coins"] = len(syms)
    return out

# ---------- оформление ----------
def base(s): return s[:-4]
def links(x):
    iv = "15" if x["tf"] == "15m" else "60"
    return [[{"text": "📈 TradingView", "url": f"https://www.tradingview.com/chart/?symbol=BINANCE:{x['sym']}&interval={iv}"},
             {"text": "🟡 Binance", "url": f"https://www.binance.com/en/trade/{base(x['sym'])}_USDT"}],
            [{"text": "🔔 Алерт на цену", "callback_data": "a:ask:" + x["sym"]}]]

def card(x):
    up = x["kind"] == "UP"
    return (f"{'🟢' if up else '🔴'} <b>{base(x['sym'])}/USDT</b> · {TFL[x['tf']]}\n"
            f"{'Бычья свеча без нижней тени' if up else 'Медвежья свеча без верхней тени'}\n\n"
            f"💵 Закрытие: <b>{price(x['c'])}</b> ({(x['c'] - x['o']) / x['o'] * 100:+.2f}%)\n"
            f"📏 Размах свечи: {x['rng']:.2f}%\n"
            f"🕒 Свеча {hhmm(x['t0'])}–{hhmm(x['t1'])} ({TZL}), закрылась {ago(x['t1'])}\n"
            f"💰 Оборот 24ч: ${x['qv'] / 1e6:.0f}M")

def line(x): return f"{'🟢' if x['kind'] == 'UP' else '🔴'} <b>{base(x['sym'])}</b> {x['tf']} · {price(x['c'])} ({(x['c'] - x['o']) / x['o'] * 100:+.2f}%) · {hhmm(x['t1'])}"

def digest(items, title):
    txt = f"<b>{title}</b>\n\n" + "\n".join(line(x) for x in items[:25])
    return txt + (f"\n… и ещё {len(items) - 25}" if len(items) > 25 else "")

def send_auto(st, out):
    for x in out:
        st["sent"][x["key"]] = x["t0"]
        st["hist"] = (st["hist"] + [dict(sym=x["sym"], tf=x["tf"], kind=x["kind"], c=x["c"], t=x["t1"])])[-30:]
    if len(out) <= 6:
        for x in out: say(card(x), links(x))
    elif out:
        say(digest(out, f"🔔 Новых сигналов: {len(out)}"))

# ---------- алерты на цену ----------
def norm(t):
    t = t.upper().replace("/", "").replace("-", "").strip()
    return t if t.endswith("USDT") else t + "USDT"

def now_price(sym): return float(get("/api/v3/ticker/price", {"symbol": sym})["price"])

def ask_price(st, sym):
    try: p = now_price(sym)
    except Exception: say("⚠️ Не нашёл такую монету на Binance (нужна пара к USDT)."); return
    st["await"] = {"stage": "price", "sym": sym, "t": ms()}
    lv = lambda pc: price(float(f"{p * (1 + pc / 100):.4g}"))
    row = lambda L: [{"text": f"{pc:+d}% · {lv(pc)}", "callback_data": f"a:set:{sym}:{lv(pc)}"} for pc in L]
    say(f"🔔 <b>Алерт: {base(sym)}/USDT</b>\nСейчас: <b>{price(p)}</b>\n\nВыберите уровень кнопкой или пришлите свою цену числом (например {lv(-5)}).",
        [row((-3, -5, -10)), row((3, 5, 10)), [{"text": "✖️ Отмена", "callback_data": "a:cancel"}]])

def add_alert(st, sym, target):
    st["await"] = None
    if target <= 0: say("Цена должна быть больше нуля."); return
    if len(st["alerts"]) >= 20: say("Максимум 20 алертов. Удалите ненужные в разделе «🔔 Алерты»."); return
    try: p = now_price(sym)
    except Exception: say("⚠️ Не нашёл такую монету на Binance (нужна пара к USDT)."); return
    d = "down" if target < p else "up"
    st["aid"] += 1; t = ms()
    st["alerts"].append(dict(id=st["aid"], sym=sym, target=target, dir=d, t=t, chk=t, p0=p))
    st["dirty"] = st["push"] = True
    say(f"✅ <b>Алерт создан</b>\n{base(sym)}/USDT: уведомлю, когда цена {'опустится' if d == 'down' else 'поднимется'} до <b>{price(target)}</b>\n"
        f"Сейчас {price(p)} ({(target - p) / p * 100:+.1f}%)",
        [[{"text": "🗑 Удалить", "callback_data": f"a:del:{st['aid']}"}, {"text": "🔔 Мои алерты", "callback_data": "m:alerts"}]])

def show_alerts(st):
    al = st["alerts"]
    if not al:
        say("🔔 <b>Алерты на цену</b>\nАктивных алертов нет.", [[{"text": "➕ Новый алерт", "callback_data": "a:new"}], [{"text": "⬅️ Меню", "callback_data": "m:menu"}]]); return
    try: pr = {x["symbol"]: float(x["price"]) for x in get("/api/v3/ticker/price", {"symbols": json.dumps([a["sym"] for a in al], separators=(",", ":"))})}
    except Exception: pr = {}
    lines = []
    for i, a in enumerate(al, 1):
        cur = pr.get(a["sym"])
        lines.append(f"{i}. <b>{base(a['sym'])}</b> {'↓' if a['dir'] == 'down' else '↑'} {price(a['target'])}" + (f" · сейчас {price(cur)} ({(a['target'] - cur) / cur * 100:+.1f}%)" if cur else ""))
    kb = [[{"text": f"🗑 {base(a['sym'])} {price(a['target'])}", "callback_data": f"a:del:{a['id']}"}] for a in al]
    say("🔔 <b>Активные алерты</b>\n\n" + "\n".join(lines), kb + [[{"text": "➕ Новый алерт", "callback_data": "a:new"}, {"text": "⬅️ Меню", "callback_data": "m:menu"}]])

def check_alerts(st):
    if not st["alerts"]: return
    now = ms()
    def one(a):
        try: return a, get("/api/v3/klines", {"symbol": a["sym"], "interval": "1m", "startTime": max(a["chk"], a["t"]) - 60000, "limit": 1000})
        except Exception: return a, None
    with ThreadPoolExecutor(5) as ex: res = list(ex.map(one, st["alerts"]))
    keep = []
    for a, ks in res:
        hit = None
        for k in (ks or []):
            if (a["dir"] == "down" and float(k[3]) <= a["target"]) or (a["dir"] == "up" and float(k[2]) >= a["target"]): hit = k; break
        if ks is None: keep.append(a); continue
        if hit is None: a["chk"] = now; keep.append(a); continue
        try: cur = now_price(a["sym"])
        except Exception: cur = float(ks[-1][4])
        x = dict(sym=a["sym"], tf="15m")
        say(f"🔔 <b>{base(a['sym'])}/USDT</b> дошла до вашего уровня\n\n🎯 Уровень: <b>{price(a['target'])}</b> ({'↓' if a['dir'] == 'down' else '↑'})\n"
            f"💵 Цена сейчас: {price(cur)}\n🕒 Касание: {hhmm(int(hit[0]))} ({TZL})",
            links(x)[:1] + [[{"text": "🔔 Новый алерт", "callback_data": "a:ask:" + a["sym"]}]])
        st["dirty"] = st["push"] = True
    st["alerts"] = keep

# ---------- меню и команды ----------
def menu_kb(st):
    return [[{"text": "🔎 Сигналы сейчас", "callback_data": "m:signal"}, {"text": "📊 Топ волатильных", "callback_data": "m:top"}],
            [{"text": "🔔 Алерты на цену", "callback_data": "m:alerts"}, {"text": "📜 История", "callback_data": "m:hist"}],
            [{"text": "ℹ️ Статус", "callback_data": "m:status"}, {"text": "⚙️ Настройки", "callback_data": "m:settings"}],
            [{"text": "▶️ Продолжить" if st["cfg"]["paused"] else "⏸ Пауза", "callback_data": "m:pause"}, {"text": "❓ Помощь", "callback_data": "m:help"}]]

def set_kb(st):
    c = st["cfg"]; f = lambda b: "✅" if b else "⬜"
    return [[{"text": f"{f('15m' in c['tfs'])} 15 минут", "callback_data": "s:15m"}, {"text": f"{f('1h' in c['tfs'])} 1 час", "callback_data": "s:1h"}],
            [{"text": f"Направление: {SIDE_L[c['side']]}", "callback_data": "s:side"}],
            [{"text": f"Оборот 24ч от ${c['vol']}M", "callback_data": "s:vol"}],
            [{"text": f"Волатильность: {VOLA_L[c['vola']]}", "callback_data": "s:vola"}],
            [{"text": f"{f(c['trend'])} Фильтр тренда 4ч", "callback_data": "s:trend"}],
            [{"text": "⬅️ Меню", "callback_data": "m:menu"}]]

SET_TXT = "⚙️ <b>Настройки</b>\nНажимайте кнопки, чтобы менять. Изменения действуют сразу."
HELP = ("❓ <b>Как это работает</b>\nБот каждые 15 минут (и по закрытию часовой свечи) проверяет монеты Binance к USDT "
        "и присылает ЗАКРЫТЫЕ свечи без тени: бычью без нижней или медвежью без верхней.\n\n"
        "Команды: /menu, /signal, /top, /history, /status, /settings, /pause, /resume\n🔔 Алерты: /alert INIT 0.1 (уведомлю, когда цена дойдёт до уровня), /alerts, кнопка «Алерт на цену» под сигналом.\n\n"
        "Фильтры: оборот за 24ч и средний размах свечи отсекают вялые монеты. «Тренд 4ч» оставляет сигналы по направлению EMA20 на 4ч (простая проверка).\n\n"
        "⚠️ Это только оповещения, а не торговые советы: тесты свечи без тени на BTC и AUDUSD не показали преимущества.")
CYC = {"side": ["all", "up", "down"], "vol": [10, 20, 50, 100], "vola": ["low", "mid", "high"]}

def act(st, data, mid=None):
    c = st["cfg"]
    if data == "m:menu": say("📋 <b>Меню</b>", menu_kb(st))
    elif data == "m:help": say(HELP)
    elif data == "m:settings": say(SET_TXT, set_kb(st))
    elif data == "m:pause":
        c["paused"] = not c["paused"]; st["dirty"] = st["push"] = True
        say("⏸ Сканирование на паузе. /resume чтобы продолжить." if c["paused"] else "▶️ Сканирование включено.", menu_kb(st))
    elif data == "m:status":
        ls = f"{hhmm(st['last_scan'])} ({TZL}), {ago(st['last_scan'])}" if st["last_scan"] else "ещё не было"
        say(f"ℹ️ <b>Статус</b>\n{'⏸ на паузе' if c['paused'] else '▶️ работает'}\nПоследняя проверка: {ls}\n"
            f"Монет в проверке: {st['coins']}\nТаймфреймы: {', '.join(TFL[t] for t in c['tfs']) or 'выключены'}\n"
            f"Направление: {SIDE_L[c['side']]}\nОборот от ${c['vol']}M · волатильность: {VOLA_L[c['vola']]}\n"
            f"Тренд 4ч: {'вкл' if c['trend'] else 'выкл'}\nИсточник данных: {HOST_OK or '—'}")
    elif data == "m:top":
        if not st["rank"]: say("Данных пока нет, подождите первую проверку.")
        else: say("📊 <b>Самые волатильные монеты</b>\n(средний размах 15-мин свечи за 12 ч)\n\n" +
                  "\n".join(f"{i + 1}. <b>{base(s)}</b> — {a:.2f}%" for i, (s, a) in enumerate(st["rank"])))
    elif data == "m:hist":
        h = st["hist"][-10:][::-1]
        say("📜 <b>Последние сигналы</b>\n\n" + "\n".join(f"{'🟢' if x['kind'] == 'UP' else '🔴'} <b>{base(x['sym'])}</b> {x['tf']} · {price(x['c'])} · {hhmm(x['t'])}" for x in h) if h else "Сигналов ещё не было.")
    elif data == "m:signal":
        say("🔎 Проверяю…")
        try: out = scan(st, manual=True)
        except Exception as e: say(f"⚠️ Не удалось получить данные Binance:\n{str(e)[:200]}"); return
        say(digest(out, f"🔎 Закрытые свечи без тени за последние несколько свечей: {len(out)}") if out else "Сейчас таких свечей нет.")
    elif data == "m:alerts": show_alerts(st)
    elif data.startswith("a:"):
        p = data.split(":")
        if p[1] == "ask": ask_price(st, p[2])
        elif p[1] == "set": add_alert(st, p[2], float(p[3]))
        elif p[1] == "new": st["await"] = {"stage": "sym", "t": ms()}; say("🔔 Какая монета? Напишите тикер, например: <b>INIT</b>")
        elif p[1] == "del":
            st["alerts"] = [a for a in st["alerts"] if str(a["id"]) != p[2]]; st["dirty"] = st["push"] = True; show_alerts(st)
        elif p[1] == "cancel": st["await"] = None; say("Отменено.")
    elif data.startswith("s:"):
        k = data[2:]
        if k in ("15m", "1h"):
            if k in c["tfs"]: c["tfs"].remove(k)
            else: c["tfs"].append(k)
        elif k == "trend": c["trend"] = not c["trend"]
        elif k in CYC: o = CYC[k]; c[k] = o[(o.index(c[k]) + 1) % len(o)]
        st["dirty"] = st["push"] = True
        if mid: tg("editMessageText", chat_id=CHAT, message_id=mid, text=SET_TXT, parse_mode="HTML", reply_markup={"inline_keyboard": set_kb(st)})
        else: say(SET_TXT, set_kb(st))

WORDS = {"start": "m:menu", "menu": "m:menu", "меню": "m:menu", "signal": "m:signal", "сигнал": "m:signal", "сигналы": "m:signal",
         "top": "m:top", "топ": "m:top", "history": "m:hist", "история": "m:hist", "status": "m:status", "статус": "m:status",
         "settings": "m:settings", "настройки": "m:settings", "help": "m:help", "помощь": "m:help", "pause": "m:pause", "пауза": "m:pause"}

def handle(st, u):
    cb = u.get("callback_query")
    if cb:
        if str(cb["message"]["chat"]["id"]) != CHAT: return
        tg("answerCallbackQuery", callback_query_id=cb["id"])
        act(st, cb["data"], cb["message"]["message_id"]); return
    m = u.get("message")
    if not m or str(m["chat"]["id"]) != CHAT: return
    raw = (m.get("text") or "").strip()
    aw = st.get("await")
    if aw and ms() - aw["t"] > 600000: aw = st["await"] = None
    if aw and raw and not raw.startswith("/"):
        if aw["stage"] == "sym": ask_price(st, norm(raw))
        else:
            try: add_alert(st, aw["sym"], float(raw.replace(",", ".").replace(" ", "").lstrip("$")))
            except ValueError: say("Не понял число. Пришлите цену, например 0.1, или нажмите «Отмена».")
        return
    parts = raw.split()
    w = parts[0].lower().lstrip("/").split("@")[0] if parts else ""
    if w in ("alert", "алерт"):
        if len(parts) >= 3:
            try: add_alert(st, norm(parts[1]), float(parts[2].replace(",", ".")))
            except ValueError: say("Формат: /alert INIT 0.1")
        elif len(parts) == 2: ask_price(st, norm(parts[1]))
        else: st["await"] = {"stage": "sym", "t": ms()}; say("🔔 Какая монета? Напишите тикер, например: <b>INIT</b>")
        return
    if w in ("alerts", "алерты"): show_alerts(st); return
    st["await"] = None
    if w == "resume":
        st["cfg"]["paused"] = False; st["dirty"] = st["push"] = True; say("▶️ Сканирование включено.", menu_kb(st))
    elif w == "pause":
        if not st["cfg"]["paused"]: act(st, "m:pause")
        else: say("Уже на паузе.")
    elif w in WORDS: act(st, WORDS[w])

# ---------- главный цикл ----------
def nxt(t): return (int(t) // 900 + 1) * 900 + 10     # 10 секунд после закрытия 15-мин свечи

def main():
    st = load(); t0 = time.time(); next_scan = t0; first = True; last_push = t0; last_chk = 0
    if os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch":
        say("✅ <b>Бот запущен</b>\nСигналы приходят сами по закрытию свечей. Нажмите «Меню».", menu_kb(st))
    while time.time() - t0 < RUN_SECONDS:
        if time.time() >= next_scan:
            if not st["cfg"]["paused"]:
                try: send_auto(st, scan(st, startup=first))
                except Exception as e: print("scan error", e)
            first = False; next_scan = nxt(time.time()); st["dirty"] = True
        if st["alerts"] and time.time() - last_chk >= 10:
            try: check_alerts(st)
            except Exception as e: print("alert error", e)
            last_chk = time.time()
        wait = max(1, min(25, int(next_scan - time.time())))
        if st["alerts"]: wait = min(wait, 10)
        r = tg("getUpdates", _to=wait + 15, offset=st["offset"], timeout=wait, allowed_updates=["message", "callback_query"])
        if not TOKEN: time.sleep(wait)
        for u in r.get("result", []):
            st["offset"] = u["update_id"] + 1
            try: handle(st, u)
            except Exception as e: print("handle error", e)
        want_push = st.pop("push", False) or time.time() - last_push > 3600
        if st.pop("dirty", False) or want_push: save(st, push=want_push)
        if want_push: last_push = time.time()
    save(st, push=True)

if __name__ == "__main__":
    main()
