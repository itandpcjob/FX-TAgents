#!/usr/bin/env python3
# XiaoMu FX feed bot v1.1: builds feed/xiaomu_feed.txt as a rolling agenda.
# - Previews come from the public ForexFactory calendar mirror
#   (faireconomy): schedule + forecast + previous.
# - Actuals come from FRED public CSV endpoints (no API key) for the
#   USD core releases; other tracked events are preview-only.
# - Coverage (user, 2026-11-13): mainly USD / JPY / GBP movers, plus
#   other major FX events (ECB). Commentary: none, facts only.
# - House rules (set by the user): entries are timestamped in JST;
#   previews carry the EVENT time; result entries carry their posting
#   time; every entry disappears ~5 minutes after its own time
#   (a result lives 5 minutes from posting; a preview is replaced by
#   its result, or vanishes 5 minutes after the event).
# State lives in tools/feed_state.json so runs are idempotent.
import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FEED_PATH = os.path.join(ROOT, "feed", "xiaomu_feed.txt")
STATE_PATH = os.path.join(ROOT, "tools", "feed_state.json")
UA = {"User-Agent": "Mozilla/5.0 (feed-bot)"}

CAL_URLS = [
    "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
]
FRED = "https://fred.stlouisfed.org/graph/fredgraph.csv?id="


def fetch_text(url, timeout=30):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def fred_series(series_id):
    """Return list of (date, float) observations, oldest first."""
    out = []
    for line in fetch_text(FRED + series_id).splitlines()[1:]:
        if "," not in line:
            continue
        d, v = line.split(",", 1)
        try:
            out.append((d.strip(), float(v)))
        except ValueError:
            pass
    return out


def fmt_k(v):
    """200000 -> '200K'; 200500 -> '200.5K'."""
    k = v / 1000.0
    return (f"{k:.0f}K" if abs(k - round(k)) < 0.05 else f"{k:.1f}K")


def fmt_pct(x):
    return f"{x:+.1f}%"


# --- actual-value fetchers: each returns a display string or None ---

def actual_claims(ev_dt):
    obs = fred_series("ICSA")
    if not obs:
        return None
    d, v = obs[-1]
    delta = (ev_dt.date() - datetime.fromisoformat(d).date()).days
    if 0 <= delta <= 6:
        return fmt_k(v)
    return None


def _fred_mom(series_id, target_month):
    obs = dict(fred_series(series_id))
    cur = obs.get(target_month)
    y, m = int(target_month[:4]), int(target_month[5:7])
    pm = f"{y - 1:04d}-12-01" if m == 1 else f"{y:04d}-{m - 1:02d}-01"
    prev = obs.get(pm)
    if cur is None or prev in (None, 0):
        return None
    return fmt_pct((cur / prev - 1.0) * 100.0)


def _prev_month_first(ev_dt):
    y, m = ev_dt.year, ev_dt.month
    return f"{y - 1:04d}-12-01" if m == 1 else f"{y:04d}-{m - 1:02d}-01"


def actual_ppi(ev_dt):
    return _fred_mom("WPSFD49207", _prev_month_first(ev_dt))


def actual_cpi(ev_dt):
    return _fred_mom("CPIAUCSL", _prev_month_first(ev_dt))


def actual_nfp(ev_dt):
    obs = dict(fred_series("PAYEMS"))
    tm = _prev_month_first(ev_dt)
    cur = obs.get(tm)
    y, m = int(tm[:4]), int(tm[5:7])
    pm = f"{y - 1:04d}-12-01" if m == 1 else f"{y:04d}-{m - 1:02d}-01"
    prev = obs.get(pm)
    if cur is None or prev is None:
        return None
    return fmt_k((cur - prev) * 1000.0)


def actual_unrate(ev_dt):
    obs = dict(fred_series("UNRATE"))
    v = obs.get(_prev_month_first(ev_dt))
    return None if v is None else f"{v:.1f}%"


def actual_fomc(ev_dt):
    obs = fred_series("DFEDTARU")
    if not obs:
        return None
    d, v = obs[-1]
    if datetime.fromisoformat(d).date() >= ev_dt.date():
        return f"{v:.2f}%"
    return None


def actual_gdp(ev_dt):
    obs = fred_series("A191RL1Q225SBEA")
    if not obs:
        return None
    d, v = obs[-1]
    if (ev_dt.date() - datetime.fromisoformat(d).date()).days <= 100:
        return fmt_pct(v)
    return None


def actual_retail(ev_dt):
    return _fred_mom("RSXFS", _prev_month_first(ev_dt))


# slug, title matcher (exact unless prefix '~' = substring), country,
# preview title, result title, event name for details, actual fetcher,
# style ("release" | "speech"), core-sibling calendar title (or None)
TRACKED = [
    # --- USD core, with FRED actuals ---
    ("claims", "Unemployment Claims", "USD", "美国初请公布", "初请失业金结果", "美国初请失业金", actual_claims, "release", None),
    ("ppi", "PPI m/m", "USD", "美国PPI公布", "美国PPI结果", "美国PPI(生产者物价)环比", actual_ppi, "release", "Core PPI m/m"),
    ("cpi", "CPI m/m", "USD", "美国CPI公布", "美国CPI结果", "美国CPI(消费者物价)环比", actual_cpi, "release", "Core CPI m/m"),
    ("nfp", "Non-Farm Employment Change", "USD", "美国非农公布", "非农就业结果", "美国非农就业人数", actual_nfp, "release", None),
    ("unrate", "Unemployment Rate", "USD", "美国失业率公布", "美国失业率结果", "美国失业率", actual_unrate, "release", None),
    ("fomc", "Federal Funds Rate", "USD", "美联储利率决议", "美联储利率决议", "美联储利率决议(联邦基金利率上限)", actual_fomc, "release", None),
    ("gdp", "~GDP q/q", "USD", "美国GDP公布", "美国GDP结果", "美国GDP环比年率", actual_gdp, "release", None),
    ("retail", "Retail Sales m/m", "USD", "美国零售销售公布", "美国零售销售结果", "美国零售销售环比", actual_retail, "release", None),
    # --- USD, preview-only ---
    ("ism_mfg", "ISM Manufacturing PMI", "USD", "美国ISM制造业PMI公布", "美国ISM制造业PMI公布", "美国ISM制造业PMI", None, "release", None),
    ("ism_svc", "ISM Services PMI", "USD", "美国ISM服务业PMI公布", "美国ISM服务业PMI公布", "美国ISM服务业PMI", None, "release", None),
    ("jolts", "JOLTS Job Openings", "USD", "美国JOLTS职位空缺公布", "美国JOLTS职位空缺公布", "美国JOLTS职位空缺", None, "release", None),
    ("adp", "ADP Non-Farm Employment Change", "USD", "美国ADP就业公布", "美国ADP就业公布", "美国ADP非农就业变化", None, "release", None),
    ("fomc_minutes", "FOMC Meeting Minutes", "USD", "美联储会议纪要公布", "美联储会议纪要公布", "美联储FOMC会议纪要", None, "release", None),
    ("powell", "~Powell Speaks", "USD", "美联储主席讲话", "美联储主席讲话", "美联储主席鲍威尔讲话", None, "speech", None),
    # --- JPY, preview-only ---
    ("boj", "BOJ Policy Rate", "JPY", "日本央行利率决议", "日本央行利率决议", "日本央行政策利率", None, "release", None),
    ("gdp_jp", "Prelim GDP q/q", "JPY", "日本GDP公布", "日本GDP公布", "日本GDP环比(初值)", None, "release", None),
    ("gdp_jp_f", "Final GDP q/q", "JPY", "日本GDP终值公布", "日本GDP终值公布", "日本GDP环比(终值)", None, "release", None),
    ("tokyo_cpi", "Tokyo Core CPI y/y", "JPY", "东京CPI公布", "东京CPI公布", "东京核心CPI同比", None, "release", None),
    ("jp_cpi", "National Core CPI y/y", "JPY", "日本CPI公布", "日本CPI公布", "日本全国核心CPI同比", None, "release", None),
    # --- GBP, preview-only ---
    ("boe", "Official Bank Rate", "GBP", "英国央行利率决议", "英国央行利率决议", "英国央行政策利率", None, "release", None),
    ("uk_cpi", "CPI y/y", "GBP", "英国CPI公布", "英国CPI公布", "英国CPI同比", None, "release", None),
    ("uk_gdp", "Prelim GDP q/q", "GBP", "英国GDP公布", "英国GDP公布", "英国GDP环比(初值)", None, "release", None),
    ("uk_claims", "Claimant Count Change", "GBP", "英国失业金申请公布", "英国失业金申请公布", "英国申请失业救济人数变化", None, "release", None),
    ("uk_retail", "Retail Sales m/m", "GBP", "英国零售销售公布", "英国零售销售公布", "英国零售销售环比", None, "release", None),
    ("bailey", "~Bailey Speaks", "GBP", "英国央行行长讲话", "英国央行行长讲话", "英国央行行长贝利讲话", None, "speech", None),
    # --- EUR, preview-only ---
    ("ecb", "Main Refinancing Rate", "EUR", "欧洲央行利率决议", "欧洲央行利率决议", "欧洲央行主要再融资利率", None, "release", None),
    ("eu_cpi", "CPI y/y", "EUR", "欧元区CPI公布", "欧元区CPI公布", "欧元区CPI同比", None, "release", None),
    ("lagarde", "~Lagarde Speaks", "EUR", "欧洲央行行长讲话", "欧洲央行行长讲话", "欧洲央行行长拉加德讲话", None, "speech", None),
]


def match_event(ev):
    for slug, pat, country, pt, rt, name, fn, style, sibling in TRACKED:
        if ev.get("country") != country:
            continue
        title = ev.get("title", "")
        if pat.startswith("~"):
            if pat[1:] in title:
                return slug, pt, rt, name, fn, style, sibling
        elif title == pat:
            return slug, pt, rt, name, fn, style, sibling
    return None


def fact_line(prefix, forecast, previous):
    parts = [prefix]
    if forecast:
        parts.append(f"预期{forecast}")
    if previous:
        parts.append(f"前值{previous}")
    return ", ".join(parts) + "。"


def main():
    dry = "--dry-run" in sys.argv
    now = datetime.now(JST)

    state = {}
    if os.path.exists(STATE_PATH):
        try:
            state = json.load(open(STATE_PATH, encoding="utf-8"))
        except Exception:
            state = {}

    events = []
    seen = set()
    for url in CAL_URLS:
        try:
            raw = fetch_text(url)
        except Exception as e:
            print(f"calendar fetch failed {url}: {e}", file=sys.stderr)
            continue
        for ev in json.loads(raw):
            k = (ev.get("title"), ev.get("date"))
            if k not in seen:
                seen.add(k)
                events.append(ev)
    if not events:
        print("no calendar data; leaving feed untouched", file=sys.stderr)
        return 0

    by_title_date = {(ev.get("title"), ev.get("date")): ev for ev in events}

    entries = []
    new_state = {}
    for ev in events:
        m = match_event(ev)
        if not m:
            continue
        slug, ptitle, rtitle, ename, fn, style, sibling = m
        try:
            ev_dt = datetime.fromisoformat(ev["date"])
        except Exception:
            continue
        ev_jst = ev_dt.astimezone(JST)
        if ev_jst < now - timedelta(days=1) or ev_jst > now + timedelta(days=10):
            continue
        key = f"{slug}|{ev_dt.date().isoformat()}"
        forecast = (ev.get("forecast") or "").strip()
        previous = (ev.get("previous") or "").strip()
        st = state.get(key, {})

        if st.get("stage") == "done":
            new_state[key] = st
            continue

        def preview_detail():
            if style == "speech":
                return f"{ev_jst.month}月{ev_jst.day}日 {ev_jst:%H:%M} {ename}。"
            d = fact_line(
                f"{ev_jst.month}月{ev_jst.day}日 {ev_jst:%H:%M} {ename}公布",
                forecast, previous)
            if sibling:
                sib = by_title_date.get((sibling, ev.get("date")))
                if sib:
                    sfc = (sib.get("forecast") or "").strip()
                    sprev = (sib.get("previous") or "").strip()
                    if sfc or sprev:
                        clause = "核心"
                        if sfc:
                            clause += f"预期{sfc}"
                        if sprev:
                            clause += f", 前值{sprev}"
                        d += clause + "。"
            return d

        if now < ev_jst:
            entries.append((ev_jst, ptitle, preview_detail()))
            new_state[key] = {"stage": "preview"}
            continue

        if st.get("stage") == "result":
            rts = datetime.fromisoformat(st["result_ts"])
            if now <= rts + timedelta(minutes=5):
                entries.append((rts, rtitle, st["detail"]))
                new_state[key] = st
            else:
                new_state[key] = {"stage": "done"}
            continue

        actual = None
        if fn is not None and now <= ev_jst + timedelta(hours=6):
            try:
                actual = fn(ev_dt)
            except Exception as e:
                print(f"actual fetch failed {key}: {e}", file=sys.stderr)
        if actual:
            detail = fact_line(f"实际{actual}", forecast, previous)
            entries.append((now, rtitle, detail))
            new_state[key] = {"stage": "result",
                              "result_ts": now.isoformat(),
                              "detail": detail}
        elif now <= ev_jst + timedelta(minutes=5):
            entries.append((ev_jst, ptitle, preview_detail()))
            new_state[key] = {"stage": "preview"}
        else:
            new_state[key] = {"stage": "waiting"}

    entries.sort(key=lambda x: x[0])
    blocks = [f"{ts:%Y.%m.%d %H:%M}\n{title}\n{detail}" for ts, title, detail in entries]
    content = "\n%%\n".join(blocks) + ("\n" if blocks else "")

    if dry:
        print(content)
        print("--- state ---")
        print(json.dumps(new_state, ensure_ascii=False, indent=1))
        return 0

    os.makedirs(os.path.dirname(FEED_PATH), exist_ok=True)
    with open(FEED_PATH, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(new_state, f, ensure_ascii=False, indent=1)
    print(f"feed entries: {len(entries)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
