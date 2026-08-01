import json
import re
import sys
import time
from openai import OpenAI

def call_llm(config: dict, system_prompt: str, user_prompt: str) -> str:
    api_base = config["llm"]["api_base"]
    api_key = config["llm"]["api_key"]
    model = config["llm"]["model"]
    
    client = OpenAI(base_url=api_base, api_key=api_key)
    
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
