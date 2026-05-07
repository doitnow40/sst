/**
 * Cloudflare Worker: realtime-proxy
 *
 * 지원 type:
 *   kr_today  : 섹터 평균 등락률 (히트맵용)
 *   kr_stocks : 종목별 실시간 등락률 (팝업용) ← 신규
 *   us_watch_today : 미국 섹터
 */

export default {
  async fetch(request, env) {
    const corsHeaders = {
      'Access-Control-Allow-Origin': '*',
      'Access-Control-Allow-Methods': 'GET, OPTIONS',
      'Access-Control-Allow-Headers': 'Content-Type',
      'Content-Type': 'application/json; charset=utf-8',
    };

    if (request.method === 'OPTIONS') {
      return new Response(null, { status: 204, headers: corsHeaders });
    }

    const url  = new URL(request.url);
    const type = url.searchParams.get('type') || '';
    const SUPPORTED = ['kr_today', 'kr_stocks', 'us_watch_today'];

    if (!SUPPORTED.includes(type)) {
      return new Response(
        JSON.stringify({ ok: false, error: `지원하지 않는 type: ${type}` }),
        { status: 400, headers: corsHeaders }
      );
    }

    try {
      const raw = await env.REALTIME_KV.get(type);
      if (!raw) {
        return new Response(
          JSON.stringify({ ok: false, error: 'KV에 데이터 없음 (장 시작 전이거나 수집 대기 중)', type }),
          { status: 404, headers: corsHeaders }
        );
      }
      return new Response(
        JSON.stringify({ ok: true, data: JSON.parse(raw) }),
        { status: 200, headers: corsHeaders }
      );
    } catch (e) {
      return new Response(
        JSON.stringify({ ok: false, error: `Worker 오류: ${e.message}` }),
        { status: 500, headers: corsHeaders }
      );
    }
  }
};
