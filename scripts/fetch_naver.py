"""
fetch_naver.py — SSTfolio 현재가 수집기
========================================
GitHub Actions에서 실행 (장중 5분 주기 cron)

역할:
  1. GAS API에서 투자현황 종목 목록 수신
  2. 네이버 m.stock API로 KR 종목 현재가+등락률 병렬 수집
  3. Yahoo Finance로 US 종목 현재가+등락률 병렬 수집
  4. Cloudflare KV portfolio_data 갱신 (GAS 포트폴리오 데이터에 현재가 덮어쓰기)

필요한 GitHub Secrets:
  GAS_WEBAPP_URL       : GAS 웹앱 URL
  CF_ACCOUNT_ID        : Cloudflare Account ID
  CF_API_TOKEN         : KV Storage 편집 권한 토큰
  CF_KV_NAMESPACE_ID   : sstfolio-kv namespace ID
  SSTFOLIO_SECRET      : Worker 인증 키
  WORKER_URL           : Cloudflare Worker URL
"""

import os, json, time, datetime, asyncio
import aiohttp, requests

GAS_URL       = os.environ['GAS_WEBAPP_URL']
CF_ACCOUNT_ID = os.environ['CF_ACCOUNT_ID']
CF_API_TOKEN  = os.environ['CF_API_TOKEN']
CF_KV_NS_ID   = os.environ['CF_KV_NAMESPACE_ID']
WORKER_URL    = os.environ.get('WORKER_URL', '').rstrip('/')
SECRET        = os.environ.get('SSTFOLIO_SECRET', '')

NAVER_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X)',
    'Referer': 'https://m.stock.naver.com/',
}
YAHOO_HEADERS = {
    'User-Agent': 'Mozilla/5.0',
    'Accept': 'application/json',
}

KV_WRITE_URL = (
    f'https://api.cloudflare.com/client/v4/'
    f'accounts/{CF_ACCOUNT_ID}/storage/kv/namespaces/{CF_KV_NS_ID}/values/{{key}}'
)

# ── TTL 계산 ────────────────────────────────────────────────
def calc_ttl():
    """장중: 10분 / 장마감 후: 다음 영업일 10:00까지"""
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    hhmm = now.hour * 100 + now.minute
    if 900 <= hhmm <= 1540 and now.weekday() < 5:
        return 600
    candidate = now.replace(hour=10, minute=0, second=0, microsecond=0)
    if now >= candidate:
        candidate += datetime.timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += datetime.timedelta(days=1)
    return max(int((candidate - now).total_seconds()), 600)

# ── 등락률 파싱 ─────────────────────────────────────────────
def parse_kr_chg(data):
    try:
        r = float(data.get('fluctuationsRatio', ''))
        if not (r != r) and abs(r) <= 35:
            return round(r, 2)
    except:
        pass
    try:
        c = float(str(data.get('closePrice', '')).replace(',', ''))
        d = float(str(data.get('compareToPreviousClosePrice', '')).replace(',', ''))
        if c > 0:
            p = c - d
            if p > 0:
                v = round((c / p - 1) * 100, 2)
                if abs(v) <= 35:
                    return v
    except:
        pass
    return None

def parse_price(data):
    for f in ['closePrice', 'currentPrice', 'nv', 'stockEndPrice', 'price']:
        v = data.get(f)
        if v:
            p = float(str(v).replace(',', ''))
            if p > 0:
                return p
    return 0

# ── KR 병렬 조회 ────────────────────────────────────────────
async def fetch_kr_one(session, code):
    url = f'https://m.stock.naver.com/api/stock/{code}/basic'
    try:
        async with session.get(url, headers=NAVER_HEADERS,
                               timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status == 200:
                data = await r.json(content_type=None)
                price = parse_price(data)
                chg   = parse_kr_chg(data)
                if price > 0:
                    if chg is None:
                        debug = {k: data.get(k) for k in
                            ['fluctuationsRatio','closePrice','compareToPreviousClosePrice',
                             'stockEndPrice','currentPrice','changeRate','rate']}
                        print(f'  [WARN] {code} 등락률 파싱 실패: {debug}')
                    chg_str = (f'+{chg:.2f}%' if chg >= 0 else f'{chg:.2f}%') if chg is not None else ''
                    return code, price, chg_str
    except Exception as e:
        print(f'  [ERR] {code} 조회 실패: {e}')
    return code, 0, ''

async def fetch_kr_all(codes):
    print(f'  [KR] {len(codes)}개 병렬 조회 중...')
    results = {}
    BATCH = 50
    async with aiohttp.ClientSession() as session:
        for i in range(0, len(codes), BATCH):
            batch = codes[i:i+BATCH]
            tasks = [fetch_kr_one(session, c) for c in batch]
            for code, price, chg in await asyncio.gather(*tasks):
                if price > 0:
                    results[code] = {'price': price, 'change_rate': chg}
            if i + BATCH < len(codes):
                await asyncio.sleep(0.2)
    ok = len(results)
    print(f'  [KR] {ok}/{len(codes)}개 성공')
    return results

# ── US 병렬 조회 (Yahoo Finance) ───────────────────────────
async def fetch_us_one(session, ticker):
    url = f'https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=1d'
    try:
        async with session.get(url, headers=YAHOO_HEADERS,
                               timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status == 200:
                data = await r.json(content_type=None)
                meta  = data['chart']['result'][0]['meta']
                price = float(meta.get('regularMarketPrice') or meta.get('previousClose') or 0)
                prev  = float(meta.get('chartPreviousClose') or meta.get('previousClose') or 0)
                if price > 0 and prev > 0:
                    chg = round((price - prev) / prev * 100, 2)
                    chg_str = f'+{chg:.2f}%' if chg >= 0 else f'{chg:.2f}%'
                    return ticker, price, chg_str
    except:
        pass
    return ticker, 0, ''

async def fetch_us_all(tickers):
    print(f'  [US] {len(tickers)}개 병렬 조회 중...')
    results = {}
    BATCH = 20
    async with aiohttp.ClientSession() as session:
        for i in range(0, len(tickers), BATCH):
            batch = tickers[i:i+BATCH]
            tasks = [fetch_us_one(session, t) for t in batch]
            for ticker, price, chg in await asyncio.gather(*tasks):
                if price > 0:
                    results[ticker] = {'price': price, 'change_rate': chg}
            if i + BATCH < len(tickers):
                await asyncio.sleep(0.3)
    ok = len(results)
    print(f'  [US] {ok}/{len(tickers)}개 성공')
    return results

# ── GAS에서 포트폴리오 데이터 가져오기 ─────────────────────
def fetch_portfolio_from_gas():
    print('[1] GAS 포트폴리오 데이터 조회 중...')
    resp = requests.get(GAS_URL, params={'action': 'portfolio'}, timeout=30)
    resp.raise_for_status()
    text = resp.text
    print(f'  → GAS 응답 크기: {len(text)} bytes')
    try:
        data = resp.json()
        if isinstance(data, dict) and 'holdings' in data:
            print(f'  → 정상 JSON: holdings {len(data.get("holdings",[]))}개')
            return data
        else:
            raise ValueError(f'예상치 못한 응답 구조: {str(data)[:200]}')
    except Exception as e:
        print(f'GAS JSON 파싱 실패: {e}')
        print(f'응답 앞 500자: {text[:500]}')
        raise

# ── KV에 저장 ───────────────────────────────────────────────
def write_to_kv(key, value, ttl):
    print(f'[KV] 저장: {key} (TTL={ttl}s)')
    # value가 dict이면 JSON 직렬화, 이미 문자열이면 그대로
    if isinstance(value, (dict, list)):
        data_str = json.dumps(value, ensure_ascii=False)
    else:
        data_str = str(value)
    print(f'  → 저장 크기: {len(data_str)} bytes')
    resp = requests.put(
        KV_WRITE_URL.format(key=key),
        headers={
            'Authorization': f'Bearer {CF_API_TOKEN}',
            'Content-Type': 'application/json',
        },
        params={'expiration_ttl': ttl},
        data=data_str.encode('utf-8'),
        timeout=15,
    )
    if not resp.ok:
        raise RuntimeError(f'KV 저장 실패: {resp.status_code} {resp.text[:200]}')
    print(f'  → KV 저장 완료')

# ── 포트폴리오 데이터에 현재가 덮어쓰기 ────────────────────
def apply_prices(portfolio, kr_map, us_map):
    """
    GAS 원본 JSON 구조를 완전히 보존하면서
    holdings[].current_price, change_rate 와
    prices[] 배열만 덮어씀
    """
    # ── holdings 업데이트 ──────────────────────────────────
    holdings = portfolio.get('holdings', [])
    updated = 0
    for h in holdings:
        ticker = h.get('ticker', '')
        market = (h.get('market') or 'KR').upper()
        if not ticker or h.get('is_cash'):
            continue
        p = kr_map.get(ticker) if market == 'KR' else us_map.get(ticker)
        if not p:
            continue
        h['current_price'] = p['price']
        h['change_rate']   = p['change_rate']
        updated += 1

    # ── prices 배열 업데이트 ───────────────────────────────
    # GAS가 반환한 prices 배열에서 현재가만 패치
    prices = portfolio.get('prices', [])
    for pr in prices:
        ticker = pr.get('ticker', '')
        market = (pr.get('market') or 'KR').upper()
        p = kr_map.get(ticker) if market == 'KR' else us_map.get(ticker)
        if p:
            pr['price']       = p['price']
            pr['change_rate'] = p['change_rate']

    print(f'  → holdings {updated}개 / prices {len(prices)}개 현재가 패치 완료')
    return portfolio  # GAS 원본 구조 그대로 반환

# ── 장중 여부 ───────────────────────────────────────────────
def is_kr_open():
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    if now.weekday() >= 5:
        return False
    hhmm = now.hour * 100 + now.minute
    return 900 <= hhmm <= 1540

def is_us_open():
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    now_et  = now_utc - datetime.timedelta(hours=4)
    if now_et.weekday() >= 5:
        return False
    hhmm = now_et.hour * 100 + now_et.minute
    return 930 <= hhmm <= 1700

# ── 메인 ────────────────────────────────────────────────────
async def main():
    start      = time.time()
    now_kst    = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    updated_at = now_kst.strftime('%Y-%m-%d %H:%M')
    print(f'=== SSTfolio fetch_naver.py 시작: {updated_at} KST ===')

    force = os.environ.get('FORCE_FETCH', '').lower() in ('1', 'true', 'yes')
    kr_open = force or is_kr_open()
    us_open = force or is_us_open()
    print(f'  KR장중: {kr_open}, US장중: {us_open}, FORCE: {force}')

    if not kr_open and not us_open:
        print('=== KR/US 모두 장외 — 스킵 ===')
        return

    # GAS에서 포트폴리오 데이터 가져오기
    try:
        portfolio = fetch_portfolio_from_gas()
    except Exception as e:
        print(f'GAS 조회 실패: {e}')
        return

    holdings = portfolio.get('holdings', [])
    kr_tickers = list(set(h['ticker'] for h in holdings
                          if h.get('market','KR').upper() == 'KR'
                          and h.get('ticker') and not h.get('is_cash')))
    us_tickers = list(set(h['ticker'] for h in holdings
                          if h.get('market','KR').upper() == 'US'
                          and h.get('ticker') and not h.get('is_cash')))

    print(f'  KR종목: {len(kr_tickers)}개, US종목: {len(us_tickers)}개')

    # 현재가 병렬 조회
    kr_map, us_map = {}, {}
    tasks = []
    if kr_open and kr_tickers:
        kr_map = await fetch_kr_all(kr_tickers)
    if us_open and us_tickers:
        us_map = await fetch_us_all(us_tickers)

    # 포트폴리오 데이터에 현재가 반영
    portfolio = apply_prices(portfolio, kr_map, us_map)
    portfolio['updated_at'] = updated_at
    portfolio['source']     = 'github_actions_python'

    # KV 저장
    ttl = calc_ttl()
    write_to_kv('portfolio_data', portfolio, ttl)

    elapsed = round(time.time() - start, 1)
    print(f'=== 완료: {elapsed}초 ===')

if __name__ == '__main__':
    asyncio.run(main())
