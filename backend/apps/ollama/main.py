from fastapi import FastAPI, Request, Response, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.concurrency import run_in_threadpool

import requests
import logging
import json
import uuid
from typing import Optional
from pathlib import Path
from pydantic import BaseModel

from apps.web.models.users import Users
from constants import ERROR_MESSAGES
from utils.utils import decode_token, get_current_user
from config import OLLAMA_API_BASE_URL, WEBUI_AUTH

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.state.OLLAMA_API_BASE_URL = OLLAMA_API_BASE_URL


logger = logging.getLogger(__name__)

# TARGET_SERVER_URL = OLLAMA_API_BASE_URL


REQUEST_POOL = []


_NEMO_RAILS = None


def _load_nemo_guardrails():
    global _NEMO_RAILS
    if _NEMO_RAILS is not None:
        return _NEMO_RAILS

    try:
        from nemoguardrails import RailsConfig, LLMRails

        config_path = Path(__file__).resolve().parent / "nemo_guardrails"
        cfg = RailsConfig.from_path(str(config_path))
        _NEMO_RAILS = LLMRails(cfg)
        return _NEMO_RAILS
    except Exception:
        _NEMO_RAILS = False
        return _NEMO_RAILS


def _nemo_input_check(user_message: str) -> Optional[str]:
    rails = _load_nemo_guardrails()
    if rails is False:
        return None

    if not isinstance(user_message, str) or user_message == "":
        return None

    try:
        res = rails.generate(
            messages=[{"role": "user", "content": user_message}],
            options={"rails": ["input"]},
        )
        content = res.get("content") if isinstance(res, dict) else None
        if not isinstance(content, str):
            return None
        if content != user_message:
            return content
        return None
    except Exception:
        return None


def _maybe_fallback_ollama_base_url(url: str) -> Optional[str]:
    if not isinstance(url, str) or url == "":
        return None

    if url.startswith("http://ollama:11434"):
        return "http://host.docker.internal:11434/api"

    return None


def _guardrails_block_message(user_message: str) -> Optional[str]:
    if not isinstance(user_message, str) or user_message == "":
        return None

    lowered = user_message.lower()
    triggers = [
        "ignore previous",
        "игнорируй",
        "забудь все правила",
        "developer mode",
    ]
    if any(t in lowered for t in triggers):
        return "Я не могу игнорировать правила или инструкции. Сформулируй вопрос без попыток обхода политики."

    return None


@app.get("/url")
async def get_ollama_api_url(user=Depends(get_current_user)):
    if user and user.role == "admin":
        return {"OLLAMA_API_BASE_URL": app.state.OLLAMA_API_BASE_URL}
    else:
        raise HTTPException(status_code=401, detail=ERROR_MESSAGES.ACCESS_PROHIBITED)


class UrlUpdateForm(BaseModel):
    url: str


@app.post("/url/update")
async def update_ollama_api_url(
    form_data: UrlUpdateForm, user=Depends(get_current_user)
):
    if user and user.role == "admin":
        app.state.OLLAMA_API_BASE_URL = form_data.url
        return {"OLLAMA_API_BASE_URL": app.state.OLLAMA_API_BASE_URL}
    else:
        raise HTTPException(status_code=401, detail=ERROR_MESSAGES.ACCESS_PROHIBITED)


@app.get("/cancel/{request_id}")
async def cancel_ollama_request(request_id: str, user=Depends(get_current_user)):
    if user:
        if request_id in REQUEST_POOL:
            REQUEST_POOL.remove(request_id)
        return True
    else:
        raise HTTPException(status_code=401, detail=ERROR_MESSAGES.ACCESS_PROHIBITED)


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def proxy(path: str, request: Request, user=Depends(get_current_user)):
    target_url = f"{app.state.OLLAMA_API_BASE_URL}/{path}"

    body = await request.body()
    headers = dict(request.headers)

    if user.role in ["user", "admin"]:
        if path in ["pull", "delete", "push", "copy", "create"]:
            if user.role != "admin":
                raise HTTPException(
                    status_code=401, detail=ERROR_MESSAGES.ACCESS_PROHIBITED
                )
    else:
        raise HTTPException(status_code=401, detail=ERROR_MESSAGES.ACCESS_PROHIBITED)

    headers.pop("host", None)
    headers.pop("authorization", None)
    headers.pop("origin", None)
    headers.pop("referer", None)

    r = None

    def get_request():
        nonlocal r, body, target_url

        request_id = str(uuid.uuid4())
        try:
            REQUEST_POOL.append(request_id)

            if path in ["chat"] and request.method.upper() == "POST":
                try:
                    body_json = json.loads(body.decode("utf-8")) if body else {}
                except Exception:
                    body_json = None

                if isinstance(body_json, dict):
                    guardrails_enabled = bool(body_json.pop("guardrailsEnabled", False))

                    if guardrails_enabled:
                        messages = body_json.get("messages")
                        user_message = None
                        if isinstance(messages, list) and messages:
                            for msg in reversed(messages):
                                if isinstance(msg, dict) and msg.get("role") == "user":
                                    user_message = msg.get("content")
                                    break

                        nemo_result = _nemo_input_check(user_message or "")
                        blocked = _guardrails_block_message(user_message or "")
                        if blocked or (isinstance(nemo_result, str) and nemo_result != ""):
                            response_text = blocked if blocked else nemo_result
                            def stream_blocked():
                                try:
                                    yield json.dumps({"id": request_id, "done": False}) + "\n"
                                    yield (
                                        json.dumps(
                                            {
                                                "model": body_json.get("model", ""),
                                                "message": {"role": "assistant", "content": response_text},
                                                "done": False,
                                            }
                                        )
                                        + "\n"
                                    )
                                    yield json.dumps(
                                        {
                                            "model": body_json.get("model", ""),
                                            "message": {"role": "assistant", "content": ""},
                                            "done": True,
                                        }
                                    ) + "\n"
                                finally:
                                    if request_id in REQUEST_POOL:
                                        REQUEST_POOL.remove(request_id)

                            return StreamingResponse(
                                stream_blocked(),
                                status_code=200,
                                media_type="text/event-stream",
                            )

                    body = json.dumps(body_json).encode("utf-8")

            def stream_content():
                try:
                    if path in ["chat"]:
                        yield json.dumps({"id": request_id, "done": False}) + "\n"

                    for chunk in r.iter_content(chunk_size=8192):
                        if request_id in REQUEST_POOL:
                            yield chunk
                        else:
                            print("User: canceled request")
                            break
                finally:
                    if hasattr(r, "close"):
                        r.close()
                        REQUEST_POOL.remove(request_id)

            r = requests.request(
                method=request.method,
                url=target_url,
                data=body,
                headers=headers,
                stream=True,
                timeout=None if path in ["chat"] else 10,
            )

            try:
                r.raise_for_status()
            except requests.exceptions.RequestException as e:
                fallback = _maybe_fallback_ollama_base_url(app.state.OLLAMA_API_BASE_URL)
                if fallback is not None:
                    app.state.OLLAMA_API_BASE_URL = fallback
                    target_url_retry = f"{app.state.OLLAMA_API_BASE_URL}/{path}"
                    r = requests.request(
                        method=request.method,
                        url=target_url_retry,
                        data=body,
                        headers=headers,
                        stream=True,
                        timeout=None if path in ["chat"] else 10,
                    )
                    r.raise_for_status()
                else:
                    raise e

            # r.close()

            return StreamingResponse(
                stream_content(),
                status_code=r.status_code,
                headers=dict(r.headers),
            )
        except Exception as e:
            raise e

    try:
        return await run_in_threadpool(get_request)
    except Exception as e:
        logger.exception("Ollama proxy error")
        error_detail = f"Ollama WebUI: Server Connection Error ({e})"
        if r is not None:
            try:
                res = r.json()
                if "error" in res:
                    error_detail = f"Ollama: {res['error']}"
            except:
                error_detail = f"Ollama: {e}"

        raise HTTPException(
            status_code=r.status_code if r else 500,
            detail=error_detail,
        )
