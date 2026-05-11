"""
fetch_naver.py  v2
==================
GitHub Actions에서 실행 (장중 5분 주기 cron)

역할:
  1. GAS API에서 관심종목 티커 목록 수신 (kr_list)
  2. 네이버 m.stock API로 종목별 실시간 등락률 수집
  3. 섹터별 평균 + 종목별 개별 등락률 계산
  4. Cloudflare KV에 저장 (단일 진실 공급원 → 웹사이트 전용):
     - kr_today  : 섹터 평균 + majorIndex (히트맵/랭킹/트리맵용)
     - kr_stocks : 종목별 개별 등락률 (팝업용)
  5. GAS 시트 동기화 (스프레드시트 반영):
     - KR 관심종목 D열 업데이트
     - KR 섹터등락률 오늘 행 갱신
     - KR Live Summary 갱신시각 기록

장마감/휴일/연휴:
  - KV TTL: 다음 영업일 10:00까지 유지 → 웹사이트 빈칸 없음
  - 시트: 마지막 갱신값 그대로 유지 → 최근 종가 반영

필요한 GitHub Secrets:
  GAS_WEBAPP_URL       : GAS 웹앱 URL
  CF_ACCOUNT_ID        : Cloudflare Account ID
  CF_API_TOKEN         : KV Storage 편집 권한 토큰
  CF_KV_NAMESPACE_ID   : REALTIME_KV namespace ID
  UPDATE_TOKEN         : GAS 시트 갱신 인증 토큰 (선택, GAS Script Properties와 동일값)
"""

import os, json, time, datetime, asyncio
import aiohttp, requests

GAS_URL        = os.environ['GAS_WEBAPP_URL']
CF_ACCOUNT_ID  = os.environ['CF_ACCOUNT_ID']
CF_API_TOKEN   = os.environ['CF_API_TOKEN']
CF_KV_NS_ID    = os.environ['CF_KV_NAMESPACE_ID']
UPDATE_TOKEN   = os.environ.get('UPDATE_TOKEN', '')  # GAS 시트 갱신용 토큰 (선택)

# ── 미국 장 시간 (ET 기준) ─────────────────────────────────
# ET 09:30~16:00 = UTC 14:30~21:00 = KST 23:30~06:00(익일)
# GitHub Actions cron: UTC 14:00~21:00 평일

NAVER_STOCK_URL = 'https://m.stock.naver.com/api/stock/{code}/basic'
NAVER_INDEX_URL = 'https://m.stock.naver.com/api/index/{index}/basic'
NAVER_HEADERS   = {
    'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X)',
    'Referer': 'https://m.stock.naver.com/',
}
BATCH_SIZE = 100

def calc_kv_ttl():
    """
    KV TTL 동적 계산:
      장중 (09:00~15:35): 10분 (5분 주기 갱신에 맞춤)
      장마감 후 / 야간 / 장전: 다음 영업일 10:00까지 유지
        - 익일 09:00 기준이 아닌 10:00 기준으로 설정하여
          첫 번째 cron(09:00~09:05) 실행 전 TTL 만료로 인한
          데이터 공백 구간을 방지
        - 주말(토/일) 포함 장마감 후에는 다음 월요일 10:00까지 유지
    """
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    hhmm = now.hour * 100 + now.minute
    weekday = now.weekday()  # 0=월 ~ 6=일

    # 장중: 10분
    if 900 <= hhmm <= 1535 and weekday < 5:
        return 600

    # 장마감 후 / 야간 / 장전 / 주말:
    # → 다음 영업일(월~금) 10:00까지 TTL 설정
    candidate = now.replace(hour=10, minute=0, second=0, microsecond=0)
    # 아직 오늘 10:00 이전이면 오늘을 후보로, 이미 지났으면 내일부터 탐색
    if now >= candidate:
        candidate += datetime.timedelta(days=1)
    # 주말이면 다음 월요일로 이동
    while candidate.weekday() >= 5:
        candidate += datetime.timedelta(days=1)

    ttl = int((candidate - now).total_seconds())
    return max(ttl, 600)  # 최소 10분

KV_WRITE_URL = (
    f'https://api.cloudflare.com/client/v4/'
    f'accounts/{CF_ACCOUNT_ID}/storage/kv/namespaces/{CF_KV_NS_ID}/values/{{key}}'
)

# ── 1. GAS kr_list 호출 ──────────────────────────────────
def fetch_kr_list():
    print('[1/5] GAS kr_list 호출 중...')
    resp = requests.get(GAS_URL, params={'type':'kr_list','range':'0'}, timeout=30)
    resp.raise_for_status()
    body = resp.json()
    if not body.get('ok'):
        raise RuntimeError(f"GAS 오류: {body.get('error')}")
    sectors = body['data']['sectors']
    print(f'  → {len(sectors)}개 섹터 수신')
    return sectors

def extract_stock_info(sectors):
    code_to_sectors = {}
    sector_stocks   = {}
    for sec in sectors:
        sname = sec['sector']
        sector_stocks[sname] = []
        for st in sec.get('stocks', []):
            ticker = st.get('ticker','')
            code = ''
            if ticker and ticker != '-':
                raw = ticker.split(':')[-1].strip()
                code = ''.join(filter(str.isdigit, raw)).zfill(6)
                if len(code) == 6:
                    code_to_sectors.setdefault(code, [])
                    if sname not in code_to_sectors[code]:
                        code_to_sectors[code].append(sname)
                else:
                    code = ''
            sector_stocks[sname].append({
                'name':      st.get('name',''),
                'ticker':    ticker,
                'code':      code,
                'prevChg':   st.get('prevChg'),
                'marketCap': st.get('marketCap'),
                'memo':      st.get('memo',''),
            })
    return code_to_sectors, sector_stocks

# ── 2. 네이버 병렬 호출 ──────────────────────────────────
def parse_chg(data):
    try:
        r = float(data.get('fluctuationsRatio',''))
        if not (r!=r) and abs(r)<=35: return round(r,2)
    except: pass
    try:
        c = float(str(data.get('closePrice','')).replace(',',''))
        d = float(str(data.get('compareToPreviousClosePrice','')).replace(',',''))
        if c>0:
            p = c-d
            if p>0:
                v = round((c/p-1)*100,2)
                if abs(v)<=35: return v
    except: pass
    return None

async def fetch_one(session, code):
    try:
        async with session.get(NAVER_STOCK_URL.format(code=code), headers=NAVER_HEADERS,
                               timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status==200:
                return code, parse_chg(await r.json(content_type=None))
    except: pass
    return code, None

async def fetch_all_stocks(codes):
    print(f'[2/5] 네이버 API 호출 중... ({len(codes)}개)')
    results = {}
    async with aiohttp.ClientSession() as session:
        for i in range(0, len(codes), BATCH_SIZE):
            batch = codes[i:i+BATCH_SIZE]
            for code, chg in await asyncio.gather(*[fetch_one(session,c) for c in batch]):
                results[code] = chg
            if i+BATCH_SIZE < len(codes):
                await asyncio.sleep(0.3)
    ok = sum(1 for v in results.values() if v is not None)
    print(f'  → {ok}/{len(codes)}개 성공')
    return results

async def fetch_index_live():
    result = {}
    async with aiohttp.ClientSession() as session:
        for idx, key in [('KOSPI','코스피'),('KOSDAQ','코스닥')]:
            try:
                async with session.get(NAVER_INDEX_URL.format(index=idx), headers=NAVER_HEADERS,
                                       timeout=aiohttp.ClientTimeout(total=10)) as r:
                    if r.status==200:
                        d = await r.json(content_type=None)
                        v = float(d.get('fluctuationsRatio','NaN'))
                        if not (v!=v): result[key]=v
            except: pass
    return result

# ── 3. 섹터 평균 계산 ────────────────────────────────────
def calc_sector_avg(code_to_sectors, chg_map):
    sv = {}
    for code, sectors in code_to_sectors.items():
        chg = chg_map.get(code)
        if chg is None: continue
        for s in sectors:
            sv.setdefault(s,[]).append(chg)
    return {s: round(sum(v)/len(v),2) for s,v in sv.items() if v}

# ── 4. 종목별 데이터 구성 (팝업용) ──────────────────────
def build_stock_data(sector_stocks, chg_map):
    print('[3/5] 종목별 데이터 구성 중...')
    result = {}
    for sname, stocks in sector_stocks.items():
        result[sname] = [{
            'name':      st['name'],
            'ticker':    st['ticker'],
            'chg':       chg_map.get(st['code']) if st['code'] else None,
            'prevChg':   st['prevChg'],
            'marketCap': st['marketCap'],
            'memo':      st['memo'],
        } for st in stocks]
    total = sum(len(v) for v in result.values())
    ok    = sum(1 for v in result.values() for st in v if st['chg'] is not None)
    print(f'  → 총 {total}개 종목, 네이버 수신 {ok}개')
    return result

# ── 5. KV 저장 ───────────────────────────────────────────
def write_to_kv(key, value):
    print(f'[KV] 저장: {key}')
    resp = requests.put(
        KV_WRITE_URL.format(key=key),
        headers={'Authorization':f'Bearer {CF_API_TOKEN}','Content-Type':'application/json'},
        params={'expiration_ttl': calc_kv_ttl()},
        data=json.dumps(value, ensure_ascii=False),
        timeout=15
    )
    if not resp.ok:
        raise RuntimeError(f'KV 저장 실패: {resp.status_code} {resp.text}')
    print(f'  → 완료')

# ── US 장 시간 유틸 ──────────────────────────────────────
def is_kr_market_open():
    """KST 기준 장중 여부 (09:00~15:35, 평일)"""
    now_kst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    if now_kst.weekday() >= 5:
        return False
    hhmm = now_kst.hour * 100 + now_kst.minute
    return 900 <= hhmm <= 1535

def is_us_market_open():
    """미국 ET 기준 장중 여부 (09:30~16:00, 평일)"""
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    now_et  = now_utc + datetime.timedelta(hours=-4)  # EDT
    if now_et.weekday() >= 5:
        return False
    hhmm = now_et.hour * 100 + now_et.minute
    return 930 <= hhmm <= 1600

def calc_us_kv_ttl():
    """US KV TTL: 장중 10분 / 장외 다음 영업일 ET 10:30까지"""
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    now_et  = now_utc + datetime.timedelta(hours=-4)
    hhmm    = now_et.hour * 100 + now_et.minute
    if now_et.weekday() < 5 and 930 <= hhmm <= 1600:
        return 600
    candidate = now_et.replace(hour=10, minute=30, second=0, microsecond=0)
    if now_et >= candidate:
        candidate += datetime.timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += datetime.timedelta(days=1)
    return max(int((candidate - now_et).total_seconds()), 600)

# ── US 섹터 수집: GAS GOOGLEFINANCE 기반 ──────────────────
# Yahoo Finance 등 외부 API는 GitHub Actions IP 차단됨
# GAS의 GOOGLEFINANCE 수식(D열)이 이미 계산한 섹터평균을 읽어 반환
def fetch_us_sector_avg():
    """
    GAS getUsSectorAvg() 호출
    → GOOGLEFINANCE 기반 섹터평균 + 종목별 등락률 반환
    반환: (sector_avg, stocks_map, date_str, updated_at)
    """
    print('  [US] GAS getUsSectorAvg() 호출 중...')
    resp = requests.get(
        GAS_URL,
        params={'type': 'us_sector_avg', '_t': int(time.time())},
        timeout=30,
    )
    resp.raise_for_status()
    body = resp.json()
    if not body.get('ok'):
        raise RuntimeError(f'GAS 오류: {body.get("error")}')
    d = body['data']
    sector_avg = d.get('sectors', {})
    stocks_map = d.get('stocks',  {})
    date_str   = d.get('date',    '')
    updated_at = d.get('updatedAt', '')
    ok_count   = sum(1 for v in sector_avg.values() if v is not None)
    print(f'  [US] 섹터 {ok_count}/{len(sector_avg)}개 유효 (source: {d.get("source","?")})')
    return sector_avg, stocks_map, date_str, updated_at

# ── 6. GAS 시트 갱신 호출 ──────────────────────────────
# ── 6. GAS 시트 갱신 호출 ──────────────────────────────
def update_gas_sheet_us(chg_map, sector_avg, date_str, updated_at):
    """GitHub Actions → GAS POST → US 시트 갱신 (US관심종목 D열 + US섹터등락률 오늘행)"""
    print('  [US GAS] 시트 갱신 중...')
    try:
        url = f'{GAS_URL}?type=update_us_today&_t={int(time.time())}'
        payload = {
            'type':       'update_us_today',
            'chg_map':    json.dumps(chg_map, ensure_ascii=False),
            'sector_avg': json.dumps(sector_avg, ensure_ascii=False),
            'date_str':   date_str,
            'updated_at': updated_at,
            'token':      UPDATE_TOKEN,
        }
        resp = requests.post(
            url,
            data=json.dumps(payload, ensure_ascii=False),
            headers={'Content-Type': 'application/json'},
            timeout=45,
            allow_redirects=True,
        )
        if resp.ok:
            body = resp.json()
            if body.get('ok'):
                d = body.get('data', {})
                print(f'  [US GAS] 완료: 섹터 {d.get("sectors","?")}개, {d.get("updatedAt","")}')
            else:
                print(f'  [US GAS] 오류: {body.get("error")}')
        else:
            print(f'  [US GAS] HTTP 오류: {resp.status_code}')
    except Exception as e:
        print(f'  [US GAS] 실패 (비중요): {e}')

def update_gas_sheet(chg_map, sector_avg, major_index, updated_at):
    """
    GitHub Actions → GAS 웹앱 POST → 시트 직접 갱신
    - chg_map이 628개 종목 JSON(~10KB)이라 GET URL 한계 초과
    - POST body로 전달해야 정확히 수신됨
    - KR 관심종목 D열 (종목별 실시간 등락률)
    - KR 섹터등락률 오늘 행
    - KR Live Summary 갱신시각
    """
    print('[6/6] GAS 시트 갱신 중...')
    try:
        # GAS는 GET/POST 모두 doGet으로 처리하지만
        # 대용량 데이터는 POST body로 전달
        # GAS 웹앱 URL에 type만 쿼리파라미터로, 나머지는 body에
        url = f'{GAS_URL}?type=update_kr_today&_t={int(time.time())}'
        payload = {
            'type':        'update_kr_today',
            'chg_map':     json.dumps(chg_map, ensure_ascii=False),
            'sector_avg':  json.dumps(sector_avg, ensure_ascii=False),
            'major_index': json.dumps(major_index, ensure_ascii=False),
            'updated_at':  updated_at,
            'token':       UPDATE_TOKEN,
        }
        resp = requests.post(
            url,
            data=json.dumps(payload, ensure_ascii=False),
            headers={'Content-Type': 'application/json'},
            timeout=45,
            allow_redirects=True,
        )
        if resp.ok:
            body = resp.json()
            if body.get('ok'):
                d = body.get('data', {})
                print(f'  → GAS 시트 갱신 완료: 섹터 {d.get("sectors","?")}개, {d.get("updatedAt","")}')
            else:
                print(f'  → GAS 응답 오류: {body.get("error")}')
        else:
            print(f'  → GAS HTTP 오류: {resp.status_code} {resp.text[:200]}')
    except Exception as e:
        print(f'  → GAS 시트 갱신 실패 (비중요, KV는 정상): {e}')

# ── 메인 ─────────────────────────────────────────────────
async def main():
    start = time.time()
    now_kst    = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    today_str  = now_kst.strftime('%Y-%m-%d')
    updated_at = now_kst.strftime('%Y-%m-%d %H:%M')
    print(f'=== fetch_naver.py 시작: {updated_at} KST ===')

    kr_open = is_kr_market_open()
    us_open = is_us_market_open()
    print(f'  KR장중: {kr_open}, US장중: {us_open}')

    if not kr_open and not us_open:
        print('=== KR/US 모두 장외 — 스킵 ===')
        return

    # ── KR 수집 (장중일 때만) ─────────────────────────────────────
    chg_map = {}
    code_to_sectors = {}
    sector_stocks = {}
    major_index = {}
    stock_data = {}
    sector_avg = {}

    if kr_open:
        sectors = fetch_kr_list()
        code_to_sectors, sector_stocks = extract_stock_info(sectors)
        all_codes = list(code_to_sectors.keys())
        print(f'  → 고유 종목코드: {len(all_codes)}개')
        chg_map = await fetch_all_stocks(all_codes)

    if kr_open:
        stock_data = build_stock_data(sector_stocks, chg_map)

        print('[4/5] 지수 조회 중...')
        major_index = await fetch_index_live()
        print(f'  → {major_index}')

        print('[5/5] KV 저장 중...')
        sector_avg = calc_sector_avg(code_to_sectors, chg_map)

        write_to_kv('kr_today', {
            'sectors':    sector_avg,
            'majorIndex': major_index,
            'date':       today_str,
            'delayed':    False,
            'updatedAt':  updated_at,
            'source':     'naver_api_github_actions',
        })
        write_to_kv('kr_stocks', {
            'stocks':    stock_data,
            'date':      today_str,
            'updatedAt': updated_at,
            'source':    'naver_api_github_actions',
        })

        # ── 6. GAS 시트 갱신 (KR) ────────────────────────────────
        update_gas_sheet(chg_map, sector_avg, major_index, updated_at)

    # ── 7. US 섹터 수집 및 KV/GAS 갱신 (US 장중일 때만) ───────────
    # 진실 공급원: GAS GOOGLEFINANCE (D열 수식 → 섹터평균)
    # Yahoo Finance 등 외부 API는 GitHub Actions IP 차단됨
    if us_open:
        print('[7] US 섹터 수집 중...')
        try:
            us_sector_avg, us_stocks_map, us_date_str, us_updated_at = fetch_us_sector_avg()
            us_ttl = calc_us_kv_ttl()

            # KV us_watch_stocks (종목별 — 팝업용)
            # ★ us_watch_today KV 저장 제거 — 웹사이트는 us_watch_history(GAS) 단일 소스 사용
            if us_stocks_map:
                requests.put(
                    KV_WRITE_URL.format(key='us_watch_stocks'),
                    headers={'Authorization': f'Bearer {CF_API_TOKEN}', 'Content-Type': 'application/json'},
                    params={'expiration_ttl': us_ttl},
                    data=json.dumps({
                        'stocks':    us_stocks_map,
                        'date':      us_date_str,
                        'updatedAt': us_updated_at,
                        'source':    'googlefinance_gas',
                    }, ensure_ascii=False),
                    timeout=15,
                )
                print(f'  → KV us_watch_stocks ✓ (종목 포함)')

            # GAS 시트 갱신 (US섹터등락률 오늘행 + J1셀 갱신시각)
            # D열은 GOOGLEFINANCE 수식 전용 — chg_map 전달 불필요
            update_gas_sheet_us({}, us_sector_avg, us_date_str, us_updated_at)

        except Exception as e:
            print(f'  → US 수집 실패 (비중요): {e}')

    elapsed = round(time.time()-start, 1)
    kr_ok = sum(1 for v in chg_map.values() if v is not None) if chg_map else 0
    kr_total = len(chg_map) if chg_map else 0
    ok_rate = round(kr_ok / max(kr_total, 1) * 100, 1)
    print(f'=== 완료: {elapsed}초, KR성공률 {ok_rate}% ===')

if __name__ == '__main__':
    asyncio.run(main())
