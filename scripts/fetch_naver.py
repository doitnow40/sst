"""
fetch_naver.py
==============
GitHub Actions에서 실행 (장중 5분 주기 cron)

역할:
  1. GAS API에서 관심종목 티커 목록 수신 (kr_list)
  2. 네이버 m.stock API로 종목별 실시간 등락률 수집
  3. 섹터별 평균 계산
  4. Cloudflare KV에 kr_today 저장

필요한 GitHub Secrets:
  GAS_WEBAPP_URL       : GAS 웹앱 URL (기존 그대로)
  CF_ACCOUNT_ID        : Cloudflare Account ID
  CF_API_TOKEN         : KV Storage 편집 권한 토큰
  CF_KV_NAMESPACE_ID   : REALTIME_KV namespace ID
"""

import os
import json
import time
import datetime
import asyncio
import aiohttp
import requests

# ── 환경변수 ──────────────────────────────────────────────
GAS_URL          = os.environ['GAS_WEBAPP_URL']
CF_ACCOUNT_ID    = os.environ['CF_ACCOUNT_ID']
CF_API_TOKEN     = os.environ['CF_API_TOKEN']
CF_KV_NS_ID      = os.environ['CF_KV_NAMESPACE_ID']

# ── 상수 ──────────────────────────────────────────────────
NAVER_STOCK_URL  = 'https://m.stock.naver.com/api/stock/{code}/basic'
NAVER_INDEX_URL  = 'https://m.stock.naver.com/api/index/{index}/basic'
NAVER_HEADERS    = {
    'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X)',
    'Referer': 'https://m.stock.naver.com/',
}
BATCH_SIZE       = 100        # 동시 요청 수
REQUEST_TIMEOUT  = 10         # 초
KV_TTL           = 600        # 10분 (KV TTL, 초)

# KV write API URL
KV_WRITE_URL = (
    f'https://api.cloudflare.com/client/v4/'
    f'accounts/{CF_ACCOUNT_ID}/storage/kv/namespaces/{CF_KV_NS_ID}/values/{{key}}'
)


# ── 1. GAS에서 관심종목 목록 가져오기 ────────────────────
def fetch_kr_list():
    """GAS kr_list API 호출 → 섹터/종목/티커 목록 반환"""
    print('[1/4] GAS kr_list 호출 중...')
    resp = requests.get(
        GAS_URL,
        params={'type': 'kr_list', 'range': '0'},
        timeout=30
    )
    resp.raise_for_status()
    body = resp.json()
    if not body.get('ok'):
        raise RuntimeError(f"GAS 오류: {body.get('error')}")
    sectors = body['data']['sectors']
    print(f'  → {len(sectors)}개 섹터 수신')
    return sectors


def extract_codes(sectors):
    """섹터 목록에서 {code: [섹터명, ...]} 매핑 생성"""
    code_to_sectors = {}
    for sec in sectors:
        sector_name = sec['sector']
        for stock in sec.get('stocks', []):
            ticker = stock.get('ticker', '')
            if not ticker or ticker == '-':
                continue
            # "KOSPI:005930" → "005930"
            code = ticker.split(':')[-1].strip()
            code = ''.join(filter(str.isdigit, code)).zfill(6)
            if len(code) != 6:
                continue
            if code not in code_to_sectors:
                code_to_sectors[code] = []
            if sector_name not in code_to_sectors[code]:
                code_to_sectors[code].append(sector_name)
    return code_to_sectors


# ── 2. 네이버 API 병렬 호출 ───────────────────────────────
def parse_naver_response(data: dict) -> float | None:
    """네이버 /basic 응답에서 등락률(%) 파싱 (GAS 로직과 동일)"""
    # 방법1: fluctuationsRatio 직접 사용 (부호 포함)
    try:
        ratio = float(data.get('fluctuationsRatio', ''))
        if not (ratio != ratio) and abs(ratio) <= 35:  # NaN 체크
            return round(ratio, 2)
    except (ValueError, TypeError):
        pass

    # 방법2: closePrice / compareToPreviousClosePrice 계산
    try:
        close = float(str(data.get('closePrice', '')).replace(',', ''))
        diff  = float(str(data.get('compareToPreviousClosePrice', '')).replace(',', ''))
        if close > 0:
            prev = close - diff
            if prev > 0:
                chg = round((close / prev - 1) * 100, 2)
                if abs(chg) <= 35:
                    return chg
    except (ValueError, TypeError):
        pass

    return None


async def fetch_one(session: aiohttp.ClientSession, code: str) -> tuple[str, float | None]:
    url = NAVER_STOCK_URL.format(code=code)
    try:
        async with session.get(url, headers=NAVER_HEADERS, timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)) as resp:
            if resp.status != 200:
                return code, None
            data = await resp.json(content_type=None)
            return code, parse_naver_response(data)
    except Exception as e:
        return code, None


async def fetch_all_stocks(codes: list[str]) -> dict[str, float | None]:
    """모든 종목 코드를 BATCH_SIZE 단위로 병렬 호출"""
    print(f'[2/4] 네이버 API 호출 중... ({len(codes)}개 종목)')
    results = {}
    async with aiohttp.ClientSession() as session:
        for i in range(0, len(codes), BATCH_SIZE):
            batch = codes[i:i + BATCH_SIZE]
            tasks = [fetch_one(session, code) for code in batch]
            batch_results = await asyncio.gather(*tasks)
            for code, chg in batch_results:
                results[code] = chg
            if i + BATCH_SIZE < len(codes):
                await asyncio.sleep(0.3)  # 배치 간 0.3초 대기
    success = sum(1 for v in results.values() if v is not None)
    print(f'  → {success}/{len(codes)}개 성공')
    return results


async def fetch_index_live() -> dict:
    """코스피/코스닥 지수 실시간 조회"""
    result = {}
    async with aiohttp.ClientSession() as session:
        for index_name, key in [('KOSPI', '코스피'), ('KOSDAQ', '코스닥')]:
            url = NAVER_INDEX_URL.format(index=index_name)
            try:
                async with session.get(url, headers=NAVER_HEADERS, timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)) as resp:
                    if resp.status == 200:
                        data = await resp.json(content_type=None)
                        chg = float(data.get('fluctuationsRatio', 'NaN'))
                        if not (chg != chg):
                            result[key] = chg
            except Exception:
                pass
    return result


# ── 3. 섹터별 평균 계산 ───────────────────────────────────
def calc_sector_avg(code_to_sectors: dict, chg_map: dict) -> dict:
    """종목별 등락률 → 섹터별 평균"""
    sector_vals: dict[str, list[float]] = {}
    for code, sectors in code_to_sectors.items():
        chg = chg_map.get(code)
        if chg is None:
            continue
        for sector in sectors:
            sector_vals.setdefault(sector, []).append(chg)

    sector_avg = {}
    for sector, vals in sector_vals.items():
        if vals:
            sector_avg[sector] = round(sum(vals) / len(vals), 2)
    return sector_avg


# ── 4. Cloudflare KV에 저장 ───────────────────────────────
def write_to_kv(key: str, value: dict):
    """Cloudflare KV REST API로 데이터 저장"""
    print(f'[4/4] KV 저장 중... (key={key})')
    url = KV_WRITE_URL.format(key=key)
    headers = {
        'Authorization': f'Bearer {CF_API_TOKEN}',
        'Content-Type': 'application/json',
    }
    # KV TTL 설정 (10분 후 자동 만료)
    params = {'expiration_ttl': KV_TTL}
    resp = requests.put(
        url,
        headers=headers,
        params=params,
        data=json.dumps(value, ensure_ascii=False),
        timeout=15
    )
    if not resp.ok:
        raise RuntimeError(f'KV 저장 실패: {resp.status_code} {resp.text}')
    print(f'  → KV 저장 완료')


# ── 메인 ──────────────────────────────────────────────────
async def main():
    start = time.time()
    now_kst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    today_str = now_kst.strftime('%Y-%m-%d')
    updated_at = now_kst.strftime('%Y-%m-%d %H:%M')

    print(f'=== fetch_naver.py 시작: {updated_at} KST ===')

    # 1. 관심종목 목록
    sectors = fetch_kr_list()
    code_to_sectors = extract_codes(sectors)
    all_codes = list(code_to_sectors.keys())
    print(f'  → 고유 종목코드: {len(all_codes)}개')

    # 2. 네이버 병렬 호출
    chg_map = await fetch_all_stocks(all_codes)

    # 3. 지수 조회 (코스피/코스닥)
    print('[3/4] 지수 조회 중...')
    major_index = await fetch_index_live()
    print(f'  → {major_index}')

    # 4. 섹터 평균 계산
    sector_avg = calc_sector_avg(code_to_sectors, chg_map)
    print(f'  → {len(sector_avg)}개 섹터 평균 계산 완료')

    # KV에 저장할 데이터 (GAS getKrToday() 응답과 동일 포맷)
    payload = {
        'sectors':    sector_avg,
        'majorIndex': major_index,
        'date':       today_str,
        'delayed':    False,
        'updatedAt':  updated_at,
        'source':     'naver_api_github_actions',
    }

    # 5. KV 저장
    write_to_kv('kr_today', payload)

    elapsed = round(time.time() - start, 1)
    success_rate = round(sum(1 for v in chg_map.values() if v is not None) / max(len(all_codes), 1) * 100, 1)
    print(f'=== 완료: {elapsed}초, 성공률 {success_rate}% ===')


if __name__ == '__main__':
    asyncio.run(main())
