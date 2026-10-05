<div align="center">

# number4.im Checker

**phone:pass combo checker for number4.im**

[![Python](https://img.shields.io/badge/python-3.9%2B-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Telegram](https://img.shields.io/badge/telegram-mr__crkz-26A5E4)](https://t.me/mr_crkz)

> **First public release** by [`@mr_crkz`](https://t.me/mr_crkz)

</div>

---

## features

- proxy pool with live/dead filtering
- per-proxy rate limiting and automatic cooldowns
- chrome TLS fingerprint via `curl_cffi` (`impersonate="chrome146"`)
- adaptive backoff with jitter on rate limits
- automatic second pass for rate-limited combos
- real-time progress bar
- clean hit output with name, phone, balance
- persistent hit file that survives Ctrl+C

## install

```bash
git clone https://github.com/mr-crkz/number4-checker.git
cd number4-checker
pip install -r requirements.txt
```

## usage

```bash
python number4.py
```

The script walks you through 5 prompts:

| prompt | default | notes |
|--------|---------|-------|
| Use proxies? | `y` | answering `n` runs direct |
| Proxy file path | `proxies.txt` | one per line, see formats below |
| Combo file path | `combo.txt` | one combo per line |
| Output txt path | `number4_hits.txt` | plaintext hits file |
| Check threads | `10` | combo check concurrency |

## proxy formats accepted

```
host:port
host:port:user:pass
user:pass@host:port
http://user:pass@host:port
socks5://user:pass@host:port
```

## combo formats accepted

```
phone:password
phone|password
phone;password
```

## output

Hits are written live to `number4_hits.txt`:

```
════════════════════════════════════════════════════════════════════
 number4.im Hits  ·  zal checker
 @mr_crkz
 generated 2026-01-01 00:00 UTC
 12 hits  ·  843 checked  ·  47 proxies
════════════════════════════════════════════════════════════════════

 [1]  2026-01-01T00:14:22Z
      phone    : 09123456789
      pass     : password123
      name     : علی رضایی
      balance  : 125000
```

Live proxies are written to `proxies_live.txt` after step 1.

## how it works

1. **proxy check** — every proxy is tested against `api.ipify.org` and `httpbin.org/ip` in parallel. live ones are sorted by latency and saved.
2. **combo check** — each `phone:pass` line hits `/api/login` through a rotating live proxy. response is parsed for `code`, `msg`, tokens, and session cookies.
3. **rate-limit handling** — when a proxy hits a rate limit it gets a 180s cooldown. the combo is requeued with exponential backoff up to 4 retries.
4. **second pass** — anything still rate-limited after retries is held for 90s and retried on a fresh proxy set.

### status codes

| status | meaning |
|--------|---------|
| `HIT` | login successful — combo saved to hits file |
| `ACTIVATE` | account exists, needs activation |
| `FAIL` | wrong credentials |
| `RATE_LIMIT` | proxy or endpoint rate-limited — requeued |
| `ERROR` | network / proxy failure — skipped |

## tuning

All timing knobs are at the top of `number4.py`:

```python
PER_PROXY_MAX_USES   = 5      # requests per proxy per window
PER_PROXY_WINDOW     = 60     # window in seconds
RATE_LIMIT_COOLDOWN  = 180    # cooldown after a rate limit
MAX_RETRIES_PER_ITEM = 3      # requeue attempts
BASE_BACKOFF         = 30     # initial backoff seconds
MAX_BACKOFF          = 300    # backoff ceiling
CHECK_THREADS        = 10     # combo check concurrency
RL_MAX_RETRIES       = 4      # rate-limit specific retries
SECOND_PASS_DELAY    = 90     # delay before second pass
```

## requirements

- Python 3.9+
- `curl_cffi` (for Chrome TLS fingerprint)

## credits

- **first public release** — [`@mr_crkz`](https://t.me/mr_crkz)

## license

MIT — see [LICENSE](LICENSE).