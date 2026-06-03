/**
 * Cloudflare Worker: realtime-proxy
 *
 * ── 역할 ──────────────────────────────────────────────────────
 * [fetch 핸들러]     브라우저 요청 → KV 데이터 반환
 * [scheduled 핸들러] Cron 트리거(5분) → 네이버 API → KV 저장
 *
 * ── Cron 스케줄 (wrangler.toml에 설정) ───────────────────────
 *   "*/5 0-6 * * 1-5"  UTC = KST 09:00~15:55 평일 5분마다
 *   Cloudflare Cron은 GitHub Actions와 달리 정확도 보장
 *
 * ── 지원 type (fetch 핸들러) ──────────────────────────────────
 *   kr_today       : 섹터 평균 등락률 + 주요 지수 (히트맵용)
 *   kr_stocks      : 종목별 실시간 등락률 (팝업용)
 *   us_watch_today : 미국 섹터 (기존 GAS 경유)
 *
 * ── 필요한 Worker Secret ──────────────────────────────────────
 *   GAS_WEBAPP_URL : GAS 웹앱 URL
 *   → npx wrangler secret put GAS_WEBAPP_URL 로 등록
 *
 * ── KV 바인딩 (wrangler.toml) ────────────────────────────────
 *   REALTIME_KV
 */

// ── 상수 ──────────────────────────────────────────────────────
const NAVER_STOCK_URL = code => `https://m.stock.naver.com/api/stock/${code}/basic`;
const NAVER_INDEX_URL = idx  => `https://m.stock.naver.com/api/index/${idx}/basic`;
const NAVER_HEADERS = {
  'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X)',
  'Referer': 'https://m.stock.naver.com/',
};
const BATCH_SIZE     = 20;   // Worker 동시 연결 수 제한 고려
const BATCH_DELAY_MS = 200;  // 배치 간 딜레이

// ── KST 유틸 ──────────────────────────────────────────────────
function nowKST() {
  return new Date(new Date().toLocaleString('en-US', { timeZone: 'Asia/Seoul' }));
}
function pad(n) { return String(n).padStart(2, '0'); }
function formatKST(d) {
  return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}
function todayKST(d) {
  return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}`;
}

// ── KST 기준 장중 여부 ─────────────────────────────────────────
function isKrMarketOpen() {
  const now = nowKST();
  const dow  = now.getDay(); // 0=일, 6=토
  if (dow === 0 || dow === 6) return false;
  const hhmm = now.getHours() * 100 + now.getMinutes();
  return hhmm >= 900 && hhmm <= 1535;
}

// ── KV TTL 계산 ───────────────────────────────────────────────
// 장중: 10분 / 그 외: 다음 영업일 10:00까지 (공백 방지)
function calcKvTtl() {
  const now = nowKST();
  const dow  = now.getDay();
  const hhmm = now.getHours() * 100 + now.getMinutes();
  if (dow >= 1 && dow <= 5 && hhmm >= 900 && hhmm <= 1535) return 600;

  const candidate = new Date(now);
  candidate.setHours(10, 0, 0, 0);
  if (now >= candidate) candidate.setDate(candidate.getDate() + 1);
  while (candidate.getDay() === 0 || candidate.getDay() === 6) {
    candidate.setDate(candidate.getDate() + 1);
  }
  return Math.max(Math.floor((candidate - now) / 1000), 600);
}

// ── 네이버 등락률 파싱 ────────────────────────────────────────
function parseChg(data) {
  try {
    const r = parseFloat(data.fluctuationsRatio);
    if (!isNaN(r) && Math.abs(r) <= 35) return Math.round(r * 100) / 100;
  } catch {}
  try {
    const c = parseFloat(String(data.closePrice).replace(/,/g, ''));
    const d = parseFloat(String(data.compareToPreviousClosePrice).replace(/,/g, ''));
    if (c > 0 && c - d > 0) {
      const v = Math.round((c / (c - d) - 1) * 10000) / 100;
      if (Math.abs(v) <= 35) return v;
    }
  } catch {}
  return null;
}

// ── 종목 1개 네이버 호출 ──────────────────────────────────────
async function fetchOneStock(code) {
  try {
    const res = await fetch(NAVER_STOCK_URL(code), {
      headers: NAVER_HEADERS,
      signal: AbortSignal.timeout(8000),
    });
    if (res.ok) return [code, parseChg(await res.json())];
  } catch {}
  return [code, null];
}

// ── 전체 종목 배치 병렬 호출 ──────────────────────────────────
async function fetchAllStocks(codes) {
  const results = {};
  for (let i = 0; i < codes.length; i += BATCH_SIZE) {
    const batch = codes.slice(i, i + BATCH_SIZE);
    const settled = await Promise.allSettled(batch.map(fetchOneStock));
    for (const s of settled) {
      if (s.status === 'fulfilled') {
        const [code, chg] = s.value;
        results[code] = chg;
      }
    }
    if (i + BATCH_SIZE < codes.length) {
      await new Promise(r => setTimeout(r, BATCH_DELAY_MS));
    }
  }
  return results;
}

// ── 주요 지수(KOSPI/KOSDAQ) 조회 ─────────────────────────────
async function fetchIndexLive() {
  const result = {};
  await Promise.allSettled(
    [['KOSPI','코스피'], ['KOSDAQ','코스닥']].map(async ([idx, key]) => {
      try {
        const res = await fetch(NAVER_INDEX_URL(idx), {
          headers: NAVER_HEADERS,
          signal: AbortSignal.timeout(8000),
        });
        if (res.ok) {
          const v = parseFloat((await res.json()).fluctuationsRatio);
          if (!isNaN(v)) result[key] = v;
        }
      } catch {}
    })
  );
  return result;
}

// ── GAS kr_list → 종목 목록 ───────────────────────────────────
async function fetchKrList(gasUrl) {
  const url = `${gasUrl}?type=kr_list&range=0&_t=${Date.now()}`;
  const res = await fetch(url, { signal: AbortSignal.timeout(25000) });
  if (!res.ok) throw new Error(`GAS HTTP ${res.status}`);
  const body = await res.json();
  if (!body.ok) throw new Error(`GAS 오류: ${body.error}`);
  return body.data.sectors;
}

// ── ticker → 6자리 종목코드 ───────────────────────────────────
function extractCode(ticker) {
  if (!ticker || ticker === '-') return '';
  const digits = ticker.split(':').pop().trim().replace(/\D/g, '').padStart(6, '0');
  return digits.length === 6 ? digits : '';
}

// ── 섹터 목록 파싱 ────────────────────────────────────────────
function extractStockInfo(sectors) {
  const codeToSectors = {};
  const sectorStocks  = {};
  for (const sec of sectors) {
    const sname = sec.sector;
    sectorStocks[sname] = [];
    for (const st of (sec.stocks || [])) {
      const code = extractCode(st.ticker || '');
      if (code) {
        if (!codeToSectors[code]) codeToSectors[code] = [];
        if (!codeToSectors[code].includes(sname)) codeToSectors[code].push(sname);
      }
      sectorStocks[sname].push({
        name: st.name || '', ticker: st.ticker || '', code,
        prevChg: st.prevChg ?? null, marketCap: st.marketCap ?? null, memo: st.memo || '',
      });
    }
  }
  return { codeToSectors, sectorStocks };
}

// ── 섹터 평균 ─────────────────────────────────────────────────
function calcSectorAvg(codeToSectors, chgMap) {
  const sv = {};
  for (const [code, secs] of Object.entries(codeToSectors)) {
    const chg = chgMap[code];
    if (chg == null) continue;
    for (const s of secs) { (sv[s] = sv[s] || []).push(chg); }
  }
  return Object.fromEntries(
    Object.entries(sv).filter(([,v]) => v.length).map(([s,v]) =>
      [s, Math.round(v.reduce((a,b)=>a+b,0)/v.length*100)/100]
    )
  );
}

// ── 종목별 데이터 구성 ────────────────────────────────────────
function buildStockData(sectorStocks, chgMap) {
  return Object.fromEntries(
    Object.entries(sectorStocks).map(([sname, stocks]) => [
      sname,
      stocks.map(st => ({
        name: st.name, ticker: st.ticker,
        chg: st.code ? (chgMap[st.code] ?? null) : null,
        prevChg: st.prevChg, marketCap: st.marketCap, memo: st.memo,
      })),
    ])
  );
}

// ── KV write ──────────────────────────────────────────────────
async function writeKV(env, key, value) {
  await env.REALTIME_KV.put(key, JSON.stringify(value), { expirationTtl: calcKvTtl() });
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
// ★ 핵심: 수집 → KV 저장 (Cron에서 호출)
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
async function collectAndStore(env) {
  const now       = nowKST();
  const todayStr  = todayKST(now);
  const updatedAt = formatKST(now);
  console.log(`[cron] 수집 시작: ${updatedAt} KST`);

  if (!env.GAS_WEBAPP_URL) throw new Error('GAS_WEBAPP_URL secret 미설정');

  const sectors = await fetchKrList(env.GAS_WEBAPP_URL);
  console.log(`[cron] 섹터 ${sectors.length}개`);

  const { codeToSectors, sectorStocks } = extractStockInfo(sectors);
  const allCodes = Object.keys(codeToSectors);
  console.log(`[cron] 종목코드 ${allCodes.length}개`);

  const chgMap     = await fetchAllStocks(allCodes);
  const okCount    = Object.values(chgMap).filter(v => v != null).length;
  console.log(`[cron] 네이버 ${okCount}/${allCodes.length}개 성공`);

  const majorIndex = await fetchIndexLive();
  console.log(`[cron] 지수: ${JSON.stringify(majorIndex)}`);

  await writeKV(env, 'kr_today', {
    sectors: calcSectorAvg(codeToSectors, chgMap),
    majorIndex, date: todayStr, delayed: false,
    updatedAt, source: 'naver_api_cf_cron',
  });
  console.log('[cron] kr_today ✓');

  await writeKV(env, 'kr_stocks', {
    stocks: buildStockData(sectorStocks, chgMap),
    date: todayStr, updatedAt, source: 'naver_api_cf_cron',
  });
  console.log('[cron] kr_stocks ✓');

  const okRate = Math.round(okCount / Math.max(allCodes.length, 1) * 1000) / 10;
  console.log(`[cron] 완료: 성공률 ${okRate}%`);
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
// Scheduled 핸들러 (Cron 트리거)
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
async function handleScheduled(event, env, ctx) {
  if (!isKrMarketOpen()) {
    console.log('[cron] 장외/주말 - 스킵');
    return;
  }

  // ── GAS 휴장일 체크 (임시공휴일·선거일 등) ──────────────────
  if (env.GAS_WEBAPP_URL) {
    try {
      const res = await fetch(
        `${env.GAS_WEBAPP_URL}?type=is_kr_trading_day&_t=${Date.now()}`,
        { signal: AbortSignal.timeout(10000) }
      );
      if (res.ok) {
        const body = await res.json();
        const td = body.data || body;
        if (td.is_open === false) {
          console.log(`[cron] GAS 휴장일 감지: ${td.reason||'휴장'} → 스킵`);
          return;
        }
      }
    } catch (e) {
      // 조회 실패 시 개장일로 간주 (안전 우선)
      console.log(`[cron] 휴장일 체크 실패 (개장일로 간주): ${e.message}`);
    }
  }

  ctx.waitUntil(collectAndStore(env));
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
// Fetch 핸들러 (브라우저 요청)
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
const CORS = {
  'Access-Control-Allow-Origin': '*',
  'Access-Control-Allow-Methods': 'GET, OPTIONS',
  'Access-Control-Allow-Headers': 'Content-Type',
  'Content-Type': 'application/json; charset=utf-8',
};
const SUPPORTED = new Set(['kr_today', 'kr_stocks', 'us_watch_today']);

async function handleFetch(request, env) {
  if (request.method === 'OPTIONS') return new Response(null, { status: 204, headers: CORS });

  const type = new URL(request.url).searchParams.get('type') || '';
  if (!SUPPORTED.has(type)) {
    return new Response(JSON.stringify({ ok: false, error: `지원하지 않는 type: ${type}` }),
      { status: 400, headers: CORS });
  }
  try {
    const raw = await env.REALTIME_KV.get(type);
    if (!raw) {
      return new Response(
        JSON.stringify({ ok: false, error: 'KV에 데이터 없음 (장 시작 전이거나 수집 대기 중)', type }),
        { status: 404, headers: CORS });
    }
    const parsed = JSON.parse(raw);

    // ── 날짜 검증: KV 데이터가 오늘 날짜가 아니면 noTrading 반환 ──
    // 날짜가 바뀌는 순간 자동 무효화 (수동 삭제 불필요)
    if (parsed.date && parsed.date !== todayKST(nowKST())) {
      console.log(`[fetch] KV 날짜 불일치: KV=${parsed.date}, today=${todayKST(nowKST())} → noTrading 반환`);
      return new Response(
        JSON.stringify({ ok: true, data: { noTrading: true, reason: '날짜 변경 (전일 캐시)', date: todayKST(nowKST()) } }),
        { status: 200, headers: CORS });
    }

    return new Response(JSON.stringify({ ok: true, data: parsed }),
      { status: 200, headers: CORS });
  } catch (e) {
    return new Response(JSON.stringify({ ok: false, error: `Worker 오류: ${e.message}` }),
      { status: 500, headers: CORS });
  }
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
export default {
  fetch:     handleFetch,
  scheduled: handleScheduled,
};
