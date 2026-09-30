#!/usr/bin/env python3
"""Read-only protocol probe against the user's current Chrome (port 9222).

No order identifiers, response body, cookies, or business writes are emitted.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from jst_auto_print_app import JSTNativeBrowser, load_settings


PROBE_JS = r"""
(async () => {
  const result = {
    href: String(location.href),
    jqueryReady: Boolean(window.jQuery),
    createPostDataReady: Boolean(window.jQuery && typeof window.jQuery.createPostData === 'function'),
    businessWrites: 0
  };
  try {
    if (!result.createPostDataReady) throw new Error('createPostData unavailable');
    const send = {Method: 'LoadDataToJSON', Args: ['1', '[]', '{}']};
    const body = window.jQuery.createPostData()
      + '__CALLBACKID=' + encodeURIComponent('JTable1')
      + '&__CALLBACKPARAM=' + encodeURIComponent(JSON.stringify(send));
    const url = new URL(location.href);
    url.searchParams.set('ts___', String(Date.now()));
    url.searchParams.set('am___', 'LoadDataToJSON');
    const started = performance.now();
    const response = await fetch(url.href, {
      method: 'POST',
      credentials: 'same-origin',
      cache: 'no-store',
      headers: {
        'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
        'X-Requested-With': 'XMLHttpRequest'
      },
      body
    });
    const text = await response.text();
    const split = text.indexOf('|');
    const numericPrefix = split > 0 && /^\d+$/.test(text.slice(0, split));
    const skipped = numericPrefix ? Number(text.slice(0, split)) : null;
    const jsonOffset = numericPrefix ? split + 1 + skipped : -1;
    let envelopeJson = false;
    if (jsonOffset >= 0 && jsonOffset < text.length) {
      try {
        const parsed = JSON.parse(text.slice(jsonOffset));
        envelopeJson = Boolean(parsed && typeof parsed === 'object' && !Array.isArray(parsed));
      } catch (_) {}
    }
    Object.assign(result, {
      ok: true,
      status: response.status,
      contentType: String(response.headers.get('content-type') || '').slice(0, 100),
      elapsedMs: Math.round((performance.now() - started) * 10) / 10,
      responseLength: text.length,
      pipeIndex: split,
      numericPrefix,
      prefixDigits: numericPrefix ? text.slice(0, split) : '',
      skipped,
      jsonOffset,
      jsonLeadCode: jsonOffset >= 0 && jsonOffset < text.length ? text.charCodeAt(jsonOffset) : null,
      envelopeJson,
      startsHtml: /^\s*</.test(text),
      startsJson: /^\s*[\[{]/.test(text),
      firstCodes: Array.from(text.slice(0, 12)).map(ch => ch.charCodeAt(0))
    });
  } catch (error) {
    result.ok = false;
    result.errorName = String(error && error.name || 'Error');
    result.errorMessage = String(error && error.message || '').slice(0, 200);
  }
  return result;
})()
"""


def main() -> int:
    settings = load_settings()
    if settings.browser_name != "Chrome" or settings.debug_port != 9222:
        raise SystemExit("只允许当前 Chrome/9222，拒绝其他浏览器或专用 profile")
    browser = None
    last_error: Exception | None = None
    for attempt in range(2):
        try:
            browser = JSTNativeBrowser(settings)
            break
        except Exception as exc:
            last_error = exc
            if attempt == 0:
                time.sleep(1.0)
    report: dict[str, object]
    if browser is None:
        report = {
            "mode": "CURRENT_CHROME_READ_ONLY_PROTOCOL_PROBE",
            "port": 9222,
            "business_writes": 0,
            "pass": False,
            "error": f"{type(last_error).__name__}: {last_error}",
        }
    else:
        try:
            result = browser._evaluate(PROBE_JS, timeout=70)
            report = {
                "mode": "CURRENT_CHROME_READ_ONLY_PROTOCOL_PROBE",
                "port": 9222,
                "business_writes": 0,
                "result": result,
                "pass": isinstance(result, dict)
                and result.get("ok") is True
                and result.get("envelopeJson") is True,
            }
        except Exception as exc:
            report = {
                "mode": "CURRENT_CHROME_READ_ONLY_PROTOCOL_PROBE",
                "port": 9222,
                "business_writes": 0,
                "pass": False,
                "error": f"{type(exc).__name__}: {exc}",
            }
        finally:
            browser.close()
    target = Path("test_results/current_chrome_protocol_probe_20260831.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("pass") else 1


if __name__ == "__main__":
    raise SystemExit(main())
