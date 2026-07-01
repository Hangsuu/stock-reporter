/**
 * 전략 로그 탭 추가 — Google Apps Script 웹훅 확장 (추가분)
 * ------------------------------------------------------------------
 * stock note와 "같은 스프레드시트"에 '전략' 탭을 만들어 날짜/태그/전략을 기록한다.
 *
 * 적용 방법:
 *   1) 기존 note 스프레드시트의 Apps Script 편집기 열기
 *      (스프레드시트 → 확장 프로그램 → Apps Script)
 *   2) 기존 doPost(e) 함수 "맨 위"에 아래 [A] 가드 3줄을 추가
 *   3) 이 파일의 recordStrategy 함수 [B]를 스크립트에 통째로 추가(붙여넣기)
 *   4) 배포 → 배포 관리 → 기존 웹 앱 배포를 "새 버전"으로 업데이트
 *      (NOTE_SHEET_URL은 그대로 유지됨)
 *
 * 동작:
 *   - 파이썬이 보내는 매매 기록에는 type 필드가 없다 → 기존 로직 그대로 실행.
 *   - 전략 기록만 {type:"strategy", date, tag, strategy}로 온다 → '전략' 탭에 append.
 *   - '전략' 탭이 없으면 헤더(날짜/태그/전략)와 함께 자동 생성.
 */

// ── [A] 기존 doPost(e) 함수의 맨 첫 줄들에 추가 ──
// function doPost(e) {
//   var data = JSON.parse(e.postData.contents);
//   if (data.type === 'strategy') return recordStrategy(data);   // ← 이 한 줄 추가
//   ... (아래는 기존 매매 기록 로직 그대로) ...
// }

// ── [B] 아래 함수를 스크립트에 추가 ──
function recordStrategy(data) {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var sh = ss.getSheetByName('전략');
  if (!sh) {
    sh = ss.insertSheet('전략');
    sh.appendRow(['날짜', '태그', '전략']);
    sh.getRange('A1:C1').setFontWeight('bold');
    sh.setFrozenRows(1);
  }
  sh.appendRow([data.date || '', data.tag || '', data.strategy || '']);
  return ContentService
    .createTextOutput(JSON.stringify({ ok: true }))
    .setMimeType(ContentService.MimeType.JSON);
}
