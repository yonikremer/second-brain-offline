import json
import re
import sys
import time
from openai import OpenAI

_client_instance = None
_client_key = None

def get_openai_client(api_base: str, api_key: str) -> OpenAI:
    global _client_instance, _client_key
    current_key = (api_base, api_key)
    if _client_instance is None or _client_key != current_key:
        _client_instance = OpenAI(base_url=api_base, api_key=api_key, timeout=60.0)
        _client_key = current_key
    return _client_instance

def call_llm(config: dict, system_prompt: str, user_prompt: str) -> str:
    api_base = config["llm"]["api_base"]
    api_key = config["llm"]["api_key"]
    model = config["llm"]["model"]
    
    client = get_openai_client(api_base, api_key)
    
    max_retries = 5
    backoff_factor = 2.0
    initial_delay = 1.0
    
    for attempt in range(max_retries):
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                temperature=0.0
            )
            return response.choices[0].message.content.strip()
        except Exception as e:
            if attempt == max_retries - 1:
                print(f"Error connecting to LLM at {api_base} (Attempt {attempt + 1}/{max_retries}): {e}", file=sys.stderr)
                print("Please check if Ollama or your LLM server is running.", file=sys.stderr)
                raise
            else:
                delay = initial_delay * (backoff_factor ** attempt)
                print(f"LLM call failed: {e}. Retrying in {delay:.1f}s (Attempt {attempt + 1}/{max_retries})...", file=sys.stderr)
                time.sleep(delay)

def parse_json_response(response_text: str) -> dict:
    cleaned = response_text.strip()
    if cleaned.startswith("```"):
        nl_idx = cleaned.find("\n")
        if nl_idx != -1:
            cleaned = cleaned[nl_idx:].strip()
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3].strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as e:
        m = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
        raise ValueError(f"Response not valid JSON: {response_text}") from e
