/**
 * Cloudflare Worker: realtime-proxy
 *
 * 역할: GitHub Actions가 KV에 저장한 실시간 데이터를 브라우저에 서빙
 *   - kr_today  : 한국 섹터 실시간 등락률
 *   - (향후 확장) us_today 등 동일 패턴으로 추가 가능
 *
 * 호출 예시:
 *   GET https://realtime.your-domain.workers.dev/?type=kr_today
 *
 * KV 바인딩 이름: REALTIME_KV  (wrangler.toml에서 설정)
 */

export default {
  async fetch(request, env) {
    // CORS 헤더 (Cloudflare Pages 도메인에서 호출하므로 전체 허용)
    const corsHeaders = {
      'Access-Control-Allow-Origin': '*',
      'Access-Control-Allow-Methods': 'GET, OPTIONS',
      'Access-Control-Allow-Headers': 'Content-Type',
      'Content-Type': 'application/json; charset=utf-8',
    };

    // OPTIONS preflight
    if (request.method === 'OPTIONS') {
      return new Response(null, { status: 204, headers: corsHeaders });
    }

    const url = new URL(request.url);
    const type = url.searchParams.get('type') || '';

    // ── 지원 type 목록 ──────────────────────────────────
    const SUPPORTED = ['kr_today', 'us_watch_today'];

    if (!SUPPORTED.includes(type)) {
      return new Response(
        JSON.stringify({ ok: false, error: `지원하지 않는 type: ${type}` }),
        { status: 400, headers: corsHeaders }
      );
    }

    try {
      // KV에서 데이터 읽기
      const raw = await env.REALTIME_KV.get(type);

      if (!raw) {
        // KV에 데이터 없음 → 장 시작 전이거나 아직 첫 수집 전
        return new Response(
          JSON.stringify({
            ok: false,
            error: 'KV에 데이터 없음 (장 시작 전이거나 수집 대기 중)',
            type: type,
          }),
          { status: 404, headers: corsHeaders }
        );
      }

      const data = JSON.parse(raw);

      // GAS 웹앱과 동일한 응답 포맷 유지 ({ ok: true, data: ... })
      return new Response(
        JSON.stringify({ ok: true, data: data }),
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
