"""
agents/base.py
==============
Base Agent class.  
Responsibilities:
- Holds the agent's role name and system prompt.
- Provides a synchronous ``call()`` method that talks to the Ollama
/api/chat endpoint using only the Python standard library (no httpx,
requests, etc.).
- Provides a static helper for safely parsing JSON from LLM responses
that may be wrapped in markdown code-fences.
"""  
from __future__ import annotations  
import json
import urllib.error
import urllib.request
from typing import Any
import logging

_logger = logging.getLogger("log_monitor.base")

class Agent:
    """
    Base class for all LLM-backed agents in the pipeline.  
    Parameters
    ----------
    role : str
    Human-readable name shown in the UI (e.g. "Diagnostic Agent").
    system_prompt : str
    The instruction text sent as the ``system`` role message.
    model : str
    Ollama model name, e.g. ``"llama3.2"``.
    base_url : str
    Root URL of the Ollama API, e.g. ``"http://192.168.1.100:11434"``.
    timeout : int
    HTTP request timeout in seconds.
    """  
def __init__(
    self,
    role: str,
    system_prompt: str,
    model: str,
    base_url: str,
    timeout: int = 60
) -> None:
    self.role = role
    self.system_prompt = system_prompt
    self.model = model
    self.base_url = base_url
    self.timeout = timeout
    _logger.debug(f"Initialized {role} with model {model}")

def call(self, user_message: str) -> str:
    """
    Send a message to the Ollama API and return the response text.
    Uses only Python standard library for HTTP requests.
    """
    url = f"{self.base_url}/api/chat"
    data = json.dumps({
        "model": self.model,
        "messages": [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": user_message}
        ],
        "stream": False
    }).encode("utf-8")
    
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    
    try:
        _logger.debug(f"{self.role} calling LLM at {url}")
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            response_json = json.loads(resp.read().decode("utf-8"))
            return response_json["message"]["content"]
    except urllib.error.URLError as exc:
        _logger.error(f"{self.role} network error: {exc.reason}")
        raise RuntimeError(
            f"[{self.role}] Cannot reach Ollama at {self.base_url}: {exc.reason}"
        ) from exc
    except (KeyError, json.JSONDecodeError) as exc:
        _logger.error(f"{self.role} invalid response format: {exc}")
        raise RuntimeError(
            f"[{self.role}] Unexpected Ollama response format: {exc}"
        ) from exc

@staticmethod
def parse_json(text: str) -> Any:
    """
    Safely parse JSON from LLM responses that may be wrapped in markdown.
    """
    # Strip markdown code fences if present
    text = text.strip()
    if text.startswith("```"):
        text = text[text.find("```", 1):]
        text = text[text.find("}") + 1:].strip() if "}" in text else text
    return json.loads(text)
