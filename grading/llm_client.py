import base64
import json
import mimetypes
import re
import requests

from grading.prompts import SYSTEM_PROMPT, build_grading_prompt, build_rubric_draft_prompt
from grading.schemas import safe_json_loads


class LLMResponseError(Exception):
    def __init__(self, message, raw_text=None):
        super().__init__(message)
        self.raw_text = raw_text


def _parse_error_message(text):
    try:
        data = json.loads(text)
        return data.get("error", {}).get("message", text)
    except Exception:
        return text


def _parse_json_from_text(raw_text):
    if raw_text is None:
        return None, "Empty response", ""
    cleaned = raw_text.strip().lstrip("\ufeff")
    data, error = safe_json_loads(cleaned)
    if not error:
        return data, "", cleaned

    fenced = re.search(r"```(?:json)?\\s*(\\{.*?\\})\\s*```", cleaned, re.DOTALL | re.IGNORECASE)
    if fenced:
        candidate = fenced.group(1).strip()
        data, error = safe_json_loads(candidate)
        if not error:
            return data, "", candidate

    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidate = cleaned[start : end + 1].strip()
        data, error = safe_json_loads(candidate)
        if not error:
            return data, "", candidate

    return None, error, cleaned


def _encode_image(path):
    mime_type, _ = mimetypes.guess_type(path)
    if not mime_type:
        mime_type = "image/png"
    with open(path, "rb") as image_file:
        b64_data = base64.b64encode(image_file.read()).decode("ascii")
    return {
        "type": "image_url",
        "image_url": {"url": f"data:{mime_type};base64,{b64_data}"},
    }


def _encode_image_response(path):
    mime_type, _ = mimetypes.guess_type(path)
    if not mime_type:
        mime_type = "image/png"
    with open(path, "rb") as image_file:
        b64_data = base64.b64encode(image_file.read()).decode("ascii")
    return {"type": "input_image", "image_url": f"data:{mime_type};base64,{b64_data}"}


def _use_responses_api(model):
    return model.startswith("gpt-5")


def _build_messages(prompt, image_paths, use_responses):
    if use_responses:
        user_content = [{"type": "input_text", "text": prompt}]
        for path in image_paths:
            user_content.append(_encode_image_response(path))
        return [
            {"role": "system", "content": [{"type": "input_text", "text": SYSTEM_PROMPT}]},
            {"role": "user", "content": user_content},
        ]

    if image_paths:
        content = [{"type": "text", "text": prompt}]
        for path in image_paths:
            content.append(_encode_image(path))
    else:
        content = prompt
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": content},
    ]


def _chat_completion(
    messages,
    model,
    endpoint,
    api_key,
    temperature=0.1,
    max_tokens=800,
    json_mode=False,
    timeout=120,
):
    if not api_key:
        raise ValueError("LLM_API_KEY is not configured")

    url = endpoint.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "messages": messages,
    }
    if model.startswith("gpt-5") or model.startswith("o"):
        payload["max_completion_tokens"] = max_tokens
    else:
        payload["max_tokens"] = max_tokens
    if temperature is not None and not model.startswith(("gpt-5", "o")):
        payload["temperature"] = temperature
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=timeout)
        response.raise_for_status()
        data = response.json()
    except requests.HTTPError as exc:
        payload_keys = ", ".join(sorted(payload.keys()))
        raise LLMResponseError(
            (
                "LLM request failed (Chat Completions). "
                f"HTTP {response.status_code}. "
                f"Provider message: {_parse_error_message(response.text)}. "
                f"Request details: model={model}, json_mode={json_mode}, keys=[{payload_keys}]"
            ),
            raw_text=response.text,
        ) from exc
    except requests.RequestException as exc:
        raise LLMResponseError(f"LLM request failed: {exc}") from exc

    message = data["choices"][0].get("message", {})
    content = message.get("content") or ""
    if not content.strip():
        refusal = message.get("refusal")
        if refusal:
            raise LLMResponseError(f"LLM refusal: {refusal}", raw_text=refusal)
        tool_calls = message.get("tool_calls")
        if tool_calls:
            raise LLMResponseError(
                "LLM returned tool calls instead of content",
                raw_text=json.dumps(message),
            )
        raise LLMResponseError("LLM returned empty content", raw_text=json.dumps(message))
    usage = data.get("usage", {})
    return content, usage


def _extract_responses_text(data):
    output_text = data.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text

    texts = []
    for item in data.get("output", []) or []:
        if item.get("type") != "message":
            continue
        for content in item.get("content", []) or []:
            content_type = content.get("type")
            if content_type in {"output_text", "text"}:
                text = content.get("text", "")
                if text:
                    texts.append(text)
            elif content_type == "refusal":
                refusal = content.get("refusal") or ""
                raise LLMResponseError(
                    f"LLM refusal: {refusal}", raw_text=json.dumps(data)
                )

    return "".join(texts)


def _normalize_usage(data):
    usage = data.get("usage", {}) or {}
    if "prompt_tokens" in usage or "completion_tokens" in usage:
        return usage

    input_tokens = usage.get("input_tokens", 0) or 0
    output_tokens = usage.get("output_tokens", 0) or 0
    total_tokens = usage.get("total_tokens", input_tokens + output_tokens) or 0
    return {
        "prompt_tokens": input_tokens,
        "completion_tokens": output_tokens,
        "total_tokens": total_tokens,
    }


def _responses_completion(
    messages,
    model,
    endpoint,
    api_key,
    temperature=0.1,
    max_tokens=800,
    json_mode=False,
    timeout=120,
):
    if not api_key:
        raise ValueError("LLM_API_KEY is not configured")

    url = endpoint.rstrip("/") + "/responses"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    def _post(payload):
        try:
            response = requests.post(url, headers=headers, json=payload, timeout=timeout)
            response.raise_for_status()
            return response.json()
        except requests.HTTPError as exc:
            payload_keys = ", ".join(sorted(payload.keys()))
            raise LLMResponseError(
                (
                    "LLM request failed (Responses API). "
                    f"HTTP {response.status_code}. "
                    f"Provider message: {_parse_error_message(response.text)}. "
                    f"Request details: model={model}, json_mode={json_mode}, keys=[{payload_keys}]"
                ),
                raw_text=response.text,
            ) from exc
        except requests.RequestException as exc:
            raise LLMResponseError(f"LLM request failed: {exc}") from exc

    base_payload = {
        "model": model,
        "input": messages,
        "max_output_tokens": max_tokens,
    }
    if temperature is not None and not model.startswith("gpt-5"):
        base_payload["temperature"] = temperature

    if json_mode:
        try:
            payload = dict(base_payload)
            payload["response_format"] = {"type": "json_object"}
            data = _post(payload)
        except LLMResponseError as exc:
            message = str(exc)
            if "text.format" in message or "response_format" in message:
                try:
                    payload = dict(base_payload)
                    payload["text"] = {"format": {"type": "json_object"}}
                    data = _post(payload)
                except LLMResponseError as exc_text:
                    if "Unrecognized request argument supplied: text" in str(exc_text):
                        data = _post(base_payload)
                    else:
                        raise
            else:
                raise
    else:
        data = _post(base_payload)

    if data.get("status") == "incomplete":
        reason = (data.get("incomplete_details") or {}).get("reason", "unknown")
        max_tokens_used = data.get("max_output_tokens")
        raise LLMResponseError(
            f"LLM response incomplete: reason={reason}, max_output_tokens={max_tokens_used}",
            raw_text=json.dumps(data),
        )

    content = _extract_responses_text(data)
    if not content.strip():
        raise LLMResponseError(
            "LLM returned empty content", raw_text=json.dumps(data)
        )
    usage = _normalize_usage(data)
    return content, usage


def grade_submission_and_raw(
    assignment_text,
    rubric_text,
    reference_solution_text,
    student_text,
    image_paths,
    model,
    endpoint,
    api_key,
    formatted_output=False,
    additional_instructions="",
    json_mode=True,
    max_tokens=800,
    timeout=120,
):
    prompt = build_grading_prompt(
        assignment_text,
        rubric_text,
        reference_solution_text,
        student_text,
        formatted_output=formatted_output,
        additional_instructions=additional_instructions,
    )
    use_responses = _use_responses_api(model)
    responses_messages = _build_messages(prompt, image_paths, True)
    chat_messages = _build_messages(prompt, image_paths, False)

    api_used = "responses" if use_responses else "chat"
    api_fallback = False
    json_fallback = False

    def _call(api, json_enabled):
        if api == "responses":
            return _responses_completion(
                responses_messages,
                model,
                endpoint,
                api_key,
                json_mode=json_enabled,
                max_tokens=max_tokens,
                timeout=timeout,
            )
        return _chat_completion(
            chat_messages,
            model,
            endpoint,
            api_key,
            json_mode=json_enabled,
            max_tokens=max_tokens,
            timeout=timeout,
        )

    try:
        raw_text, usage = _call(api_used, json_mode)
    except LLMResponseError as exc:
        message = str(exc)
        if api_used == "responses" and "Unrecognized request argument supplied: text" in message:
            api_used = "chat"
            api_fallback = True
            raw_text, usage = _call(api_used, json_mode)
        elif json_mode and "empty content" in message.lower():
            raw_text, usage = _call(api_used, False)
            json_fallback = True
        elif "Unsupported parameter" in message and "text.format" in message:
            api_used = "responses"
            api_fallback = False
            raw_text, usage = _call(api_used, False)
        else:
            raise
    data, error, _parsed = _parse_json_from_text(raw_text)
    if error:
        raise LLMResponseError(f"Invalid JSON from LLM: {error}", raw_text=raw_text)
    meta = {
        "json_mode_fallback": json_fallback,
        "api_used": api_used,
        "api_fallback": api_fallback,
    }
    return data, raw_text, usage, meta


def grade_submission(
    assignment_text,
    rubric_text,
    reference_solution_text,
    student_text,
    image_paths,
    model,
    endpoint,
    api_key,
    formatted_output=False,
    additional_instructions="",
):
    data, _raw, _usage, _meta = grade_submission_and_raw(
        assignment_text,
        rubric_text,
        reference_solution_text,
        student_text,
        image_paths,
        model,
        endpoint,
        api_key,
        formatted_output=formatted_output,
        additional_instructions=additional_instructions,
        json_mode=True,
    )
    return data


def generate_rubric_draft(
    assignment_text,
    model,
    endpoint,
    api_key,
    formatted_output=False,
    additional_instructions="",
    json_mode=True,
    max_tokens=800,
    timeout=120,
):
    prompt = build_rubric_draft_prompt(
        assignment_text,
        formatted_output=formatted_output,
        additional_instructions=additional_instructions,
    )
    use_responses = _use_responses_api(model)
    responses_messages = _build_messages(prompt, [], True)
    chat_messages = _build_messages(prompt, [], False)

    api_used = "responses" if use_responses else "chat"
    api_fallback = False
    json_fallback = False

    def _call(api, json_enabled):
        if api == "responses":
            return _responses_completion(
                responses_messages,
                model,
                endpoint,
                api_key,
                json_mode=json_enabled,
                max_tokens=max_tokens,
                timeout=timeout,
            )
        return _chat_completion(
            chat_messages,
            model,
            endpoint,
            api_key,
            json_mode=json_enabled,
            max_tokens=max_tokens,
            timeout=timeout,
        )

    try:
        raw_text, usage = _call(api_used, json_mode)
    except LLMResponseError as exc:
        message = str(exc)
        if api_used == "responses" and "Unrecognized request argument supplied: text" in message:
            api_used = "chat"
            api_fallback = True
            raw_text, usage = _call(api_used, json_mode)
        elif json_mode and "empty content" in message.lower():
            raw_text, usage = _call(api_used, False)
            json_fallback = True
        elif "Unsupported parameter" in message and "text.format" in message:
            api_used = "responses"
            api_fallback = False
            raw_text, usage = _call(api_used, False)
        else:
            raise
    data, error, _parsed = _parse_json_from_text(raw_text)
    if error:
        raise LLMResponseError(f"Invalid JSON from LLM: {error}", raw_text=raw_text)
    meta = {
        "json_mode_fallback": json_fallback,
        "api_used": api_used,
        "api_fallback": api_fallback,
    }
    return data, usage, raw_text, meta
