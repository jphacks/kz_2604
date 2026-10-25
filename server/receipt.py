"""レシート読み取り（主の経路：Gemini API。Claude API にも切り替えられる）。

LLM には「品名の行の文字起こし」「各行のJANコード（印字があれば）」「購入日」だけをさせる。
どの行がどの食材かの照合は server/ocr_match.py（コード）で行い、食材表は渡さない。

どちらを使うかは .env の RECEIPT_ENGINE（gemini / claude）。
未指定なら GEMINI_API_KEY があれば Gemini、なければ ANTHROPIC_API_KEY があれば Claude。
"""
import base64
import json
import os
import re

# 混雑（503/504）やタイムアウトのときは次のモデルに切り替える。先頭は .env の GEMINI_MODEL で変えられる
GEMINI_MODELS = list(dict.fromkeys([
    os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite"),
    "gemini-3.6-flash",
    "gemini-3.8-flash",
]))
GEMINI_TIMEOUT_SECONDS = 20  # 1モデルあたり（3.5 Flash-Lite で実物のレシートが約5秒）
CLAUDE_MODEL = "claude-opus-5-5"
TIMEOUT_SECONDS = 45

PROMPT = """これはスーパーのレシートの写真です。複数枚あるときは、1枚のレシートを上から順に分けて撮ったもので、つなぎ目は少し重なっています。

次のJSONだけを返してください。説明文やコードブロックは付けないでください。
{"purchased_at": "YYYY-MM-DD", "lines": [{"name": "品名", "jan": "4902820208719"}, ...]}

ルール:
- lines には、買った品物の品名の行だけを、レシートの上から順に、印字のとおりに name に入れる
- 品名のすぐ上か横に13桁または8桁のJANコードが印字されていれば jan に入れる。なければ null
- 店名、住所、電話番号、案内文、日時、小計、合計、税、お預り、お釣り、ポイント、ポイント倍率、支払い方法、値引きの行は入れない
- 値段や数量は入れない（品名の部分だけ）
- 重なっている部分で同じ行が2回写っていたら、1回だけ入れる
- 読めない行は推測せず、入れない
- purchased_at はレシートに印字された日付。読めなければ null。JANコードも読めなければ null"""

# Gemini の出力をこの形に固定する
RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "purchased_at": {"type": ["string", "null"], "description": "YYYY-MM-DD。読めなければ null"},
        "lines": {"type": "array", "items": {
            "type": "object",
            "properties": {"name": {"type": "string"}, "jan": {"type": ["string", "null"]}},
            "required": ["name", "jan"],
        }},
    },
    "required": ["purchased_at", "lines"],
}


class ReceiptError(Exception):
    pass


def engine():
    choice = os.environ.get("RECEIPT_ENGINE", "").strip().lower()
    if choice in ("gemini", "claude"):
        return choice
    if os.environ.get("GEMINI_API_KEY"):
        return "gemini"
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "claude"
    return None


def _parse(text):
    m = re.search(r"\{.*\}", (text or "").strip(), re.S)
    if not m:
        raise ReceiptError("読み取り結果がJSONではありませんでした")
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError as e:
        raise ReceiptError(f"読み取り結果を解析できませんでした: {e}")
    lines = []
    for x in data.get("lines") or []:
        if isinstance(x, str):  # 古い形（品名だけ）にも対応する
            x = {"name": x, "jan": None}
        name = str(x.get("name") or "").strip()
        jan = re.sub(r"\D", "", str(x.get("jan") or "")) or None
        if jan and len(jan) not in (8, 13):
            jan = None
        if name:
            lines.append({"name": name, "jan": jan})
    purchased_at = data.get("purchased_at")
    if purchased_at and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(purchased_at)):
        purchased_at = None
    if not lines:
        raise ReceiptError("品名の行を読み取れませんでした")
    return {"purchased_at": purchased_at, "lines": lines}


def read_receipt(images):
    """images: [{"data": base64, "media_type": "image/jpeg"}, ...]（上から順）
    戻り値: {"purchased_at", "lines", "engine", "model"}"""
    which = engine()
    if which == "gemini":
        result, model = _read_gemini(images)
    elif which == "claude":
        result, model = _read_claude(images), CLAUDE_MODEL
    else:
        raise ReceiptError("GEMINI_API_KEY が設定されていません（.env を確認）")
    return {**result, "engine": which, "model": model}


def _read_gemini(images):
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise ReceiptError("GEMINI_API_KEY が設定されていません（.env を確認）")
    try:
        from google import genai
        from google.genai import errors, types
    except ImportError:
        raise ReceiptError("google-genai パッケージが入っていません（pip install -r requirements.txt）")

    client = genai.Client(api_key=key, http_options=types.HttpOptions(timeout=GEMINI_TIMEOUT_SECONDS * 1000))
    # 画像を先に、指示を後に。小さい文字を読むので高い解像度で渡す
    contents = [types.Part.from_bytes(data=base64.b64decode(img["data"]),
                                      mime_type=img.get("media_type", "image/jpeg"),
                                      media_resolution=types.PartMediaResolutionLevel.MEDIA_RESOLUTION_HIGH)
                for img in images]
    contents.append(PROMPT)
    config = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_json_schema=RESULT_SCHEMA,
        temperature=0,
    )

    failures = []
    for model in GEMINI_MODELS:
        try:
            response = client.models.generate_content(model=model, contents=contents, config=config)
        except errors.ClientError as e:
            # キーが違う・リクエストが不正など。モデルを変えても直らないので止める（429 の混雑だけは次へ）
            if e.code != 429:
                raise ReceiptError(f"Gemini APIエラー（{e.code}）: {e.message}")
            failures.append(f"{model}: 混雑（429）")
            continue
        except errors.ServerError as e:
            failures.append(f"{model}: {e.code}")
            continue
        except Exception as e:  # タイムアウト・接続エラーなど
            failures.append(f"{model}: {type(e).__name__}")
            continue

        try:
            text = response.text
        except Exception:
            text = None
        if not text:
            reason = getattr(getattr(response, "prompt_feedback", None), "block_reason", None)
            raise ReceiptError(f"読み取り結果が空でした{f'（{reason}）' if reason else ''}")
        return _parse(text), model

    raise ReceiptError("Gemini APIが混雑していて読み取れませんでした（" + "、".join(failures) + "）")


def _read_claude(images):
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise ReceiptError("ANTHROPIC_API_KEY が設定されていません（.env を確認）")
    try:
        import anthropic
    except ImportError:
        raise ReceiptError("anthropic パッケージが入っていません（pip install -r requirements.txt）")

    client = anthropic.Anthropic(timeout=TIMEOUT_SECONDS, max_retries=1)
    content = [{"type": "image", "source": {"type": "base64", "media_type": img.get("media_type", "image/jpeg"),
                                             "data": img["data"]}} for img in images]
    content.append({"type": "text", "text": PROMPT})  # 画像を先に、指示を後に
    try:
        response = client.beta.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=4000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": "low"},
            messages=[{"role": "user", "content": content}],
        )
    except anthropic.APITimeoutError:
        raise ReceiptError("APIの応答がありませんでした（タイムアウト）")
    except anthropic.APIConnectionError as e:
        raise ReceiptError(f"APIに接続できませんでした: {e}")
    except anthropic.APIStatusError as e:
        raise ReceiptError(f"APIエラー（{e.status_code}）: {e.message}")

    if response.stop_reason == "refusal":
        raise ReceiptError("読み取りを断られました")
    text = "".join(b.text for b in response.content if b.type == "text")
    return _parse(text)
