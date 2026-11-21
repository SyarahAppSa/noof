"""طبقة Claude: يتحقق من شكل الطلب الفعلي عبر SDK (بنقل HTTP وهمي) ومن التعامل مع الأخطاء والرفض."""
import json

import anthropic
import httpx2 as httpx

from app.ai import llm


def _client_with(handler):
    return anthropic.Anthropic(api_key="test", http_client=httpx.Client(transport=httpx.MockTransport(handler)),
                               max_retries=0)


def _resp(text_, stop="end_turn"):
    return {"id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5", "stop_reason": stop,
            "stop_sequence": None, "content": [{"type": "text", "text": text_}],
            "usage": {"input_tokens": 10, "output_tokens": 10}}


def test_classify_request_shape_and_parse(monkeypatch):
    seen = {}

    def handler(req: httpx.Request):
        seen["body"] = json.loads(req.content)
        seen["beta"] = req.headers.get("anthropic-beta")
        out = {"category": "complaint", "urgency": "high", "sentiment": "angry", "summary": "شكوى تأخير",
               "needs_owner": True, "is_sales_opportunity": False, "person_name": None, "company": None,
               "sector": None, "wants_appointment": False, "appointment_text": None,
               "involves_price_or_commitment": False, "suggested_reply": "نعتذر...", "faq_rule_id": None}
        return httpx.Response(200, json=_resp(json.dumps(out, ensure_ascii=False)))

    monkeypatch.setattr(llm, "available", lambda: True)
    monkeypatch.setattr(llm, "_client", _client_with(handler))
    r = llm.classify_inbound("الطلب متأخر!", sender="x", history=[], faqs=[], tone="رسمي")
    assert r["category"] == "complaint"
    b = seen["body"]
    assert b["model"] == "claude-opus-5-5"
    assert b["output_config"]["format"]["type"] == "json_schema"
    assert b["output_config"]["effort"] == "low"
    assert b["fallbacks"] == "default"
    assert "server-side-fallback-2026-07-01" in seen["beta"]
    assert "<message>" in b["messages"][0]["content"]  # فصل بيانات الطرف الخارجي عن التعليمات


def test_refusal_and_errors_fall_back_to_rules(monkeypatch):
    monkeypatch.setattr(llm, "available", lambda: True)
    monkeypatch.setattr(llm, "_client", _client_with(lambda req: httpx.Response(200, json=_resp("", "refusal"))))
    assert llm.parse_command("x", staff=[], now_iso="2026-01-01") is None
    monkeypatch.setattr(llm, "_client", _client_with(lambda req: httpx.Response(500, json={"type": "error", "error": {"type": "api_error", "message": "x"}})))
    assert llm.parse_command("x", staff=[], now_iso="2026-01-01") is None
