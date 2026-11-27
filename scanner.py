import os, sys, json, time, urllib.request, urllib.parse

HOSTS = ["https://data-api.binance.vision", "https://api.binance.com", "https://api1.binance.com",
         "https://api2.binance.com", "https://api3.binance.com", "https://data.binance.com"]
TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")
MIN_QUOTE_VOL = float(os.environ.get("MIN_QUOTE_VOL", "20000000"))  # оборот за 24ч, USDT
MAX_COINS = int(os.environ.get("MAX_COINS", "150"))
# таймфрейм: (секунд в свече, мин. размах свечи %, мин. средний размах свечи %, окно "свежести" сек)
TF = {"15m": (900, 0.25, 0.25, 2700), "1h": (3600, 0.4, 0.5, 9000)}
STABLE = {"USDC", "FDUSD", "TUSD", "USDP", "DAI", "BUSD", "USD1", "USDE", "XUSD", "EUR", "AEUR"}
STATE = "state.json"
HOST_OK = None

def get(path, params=None):
    global HOST_OK
    q = "?" + urllib.parse.urlencode(params) if params else ""
    last = None
    for h in ([HOST_OK] if HOST_OK else []) + [x for x in HOSTS if x != HOST_OK]:
        try:
            req = urllib.request.Request(h + path + q, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=20) as r:
                HOST_OK = h
                return json.load(r)
        except Exception as e:
            last = f"{h}: {e}"
    raise RuntimeError(last)

def send(text):
    if not TOKEN or not CHAT:
        print("НЕТ TELEGRAM_TOKEN/CHAT_ID\n" + text); return
    data = urllib.parse.urlencode({"chat_id": CHAT, "text": text}).encode()
    urllib.request.urlopen("https://api.telegram.org/bot" + TOKEN + "/sendMessage", data, timeout=20).read()

def nowick(k, min_range):
    o, h, l, c = map(float, k[1:5])
    if o <= 0 or (h - l) / o * 100 < min_range:
        return None
    if c > o and o == l: return "UP"      # бычья, без нижней тени
    if c < o and o == h: return "DOWN"    # медвежья, без верхней тени
    return None

def main():
    test = "--test" in sys.argv
    try: state = json.load(open(STATE))
    except Exception: state = {}
    now = int(time.time() * 1000)
    try:
        tick = get("/api/v3/ticker/24hr")
    except Exception as e:
        print("ОШИБКА доступа к Binance:", e)
        if test: send("⚠️ Бот запущен, но Binance недоступен с сервера GitHub:\n" + str(e)[:300])
        json.dump(state, open(STATE, "w")); return
    coins = [t for t in tick if t["symbol"].endswith("USDT") and t["symbol"][:-4] not in STABLE
             and float(t["quoteVolume"]) >= MIN_QUOTE_VOL]
    coins.sort(key=lambda t: -float(t["quoteVolume"]))
    coins = coins[:MAX_COINS]
    lines = []
    for t in coins:
        sym = t["symbol"]
        for tf, (sec, min_rng, min_avg, fresh) in TF.items():
            try: ks = get("/api/v3/klines", {"symbol": sym, "interval": tf, "limit": 60})
            except Exception: continue
            ks = [k for k in ks if k[6] < now]            # только закрытые свечи
            if len(ks) < 20: continue
            avg = sum((float(k[2]) - float(k[3])) / float(k[1]) for k in ks[-48:]) / len(ks[-48:]) * 100
            if avg < min_avg: continue                    # слишком вялая монета
            for k in ks:
                if now - k[6] > fresh * 1000: continue    # только свежие свечи
                key = f"{sym}|{tf}|{k[0]}"
                kind = nowick(k, min_rng)
                if not kind or key in state: continue
                state[key] = k[0]
                o, c = float(k[1]), float(k[4])
                side = "🟢 бычья, без нижней тени" if kind == "UP" else "🔴 медвежья, без верхней тени"
                lines.append(f"{sym} {tf}: {side}\nзакрытие {c:g} ({(c-o)/o*100:+.2f}%), оборот 24ч ${float(t['quoteVolume'])/1e6:.0f}M")
    cut = now - 3 * 86400 * 1000
    state = {k: v for k, v in state.items() if v > cut}
    json.dump(state, open(STATE, "w"))
    if lines:
        msg = "\n\n".join(lines)
        for i in range(0, len(msg), 3800): send(msg[i:i + 3800])
    if test:
        send(f"✅ Бот работает. Источник: {HOST_OK}. Монет прошло фильтр по обороту: {len(coins)}. Новых сигналов: {len(lines)}.")
    print(f"монет: {len(coins)}, сигналов: {len(lines)}")

if __name__ == "__main__":
    main()
