"""One place that talks to Gemini.

ask(prompt, Schema) returns a filled-in Pydantic object. It tries each model in
GEMINI_MODELS (.env) in order: 503 = retry then next model, 404/429 = next model.
"""
import os
import time

from dotenv import load_dotenv

load_dotenv()
_client = None


def models():
    raw = os.getenv("GEMINI_MODELS", "gemini-3.1-flash-lite,gemini-3.5-flash-lite")
    return [m.strip() for m in raw.split(",") if m.strip()]


def client():
    global _client
    if _client is None:
        from google import genai
        from google.genai import types
        _client = genai.Client(http_options=types.HttpOptions(timeout=90_000))
    return _client


def ask(prompt, schema, tries=3):
    from google.genai import errors, types
    config = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=schema,
        temperature=0,
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )
    last = None
    for model in models():
        for attempt in range(tries):
            try:
                r = client().models.generate_content(model=model, contents=prompt, config=config)
                return schema.model_validate_json(r.text)
            except errors.APIError as e:
                last = e
                if e.code in (404, 429):
                    break                      # this model is unusable right now
                if e.code in (500, 503):
                    time.sleep(3 * 2 ** attempt)
                    continue
                raise
    raise RuntimeError(f"All models failed: {last}")
