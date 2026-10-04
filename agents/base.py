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
        timeout: int = 120,
        *,
        json_mode: bool = False,
    ) -> None:
        self.role = role
        self.system_prompt = system_prompt
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.json_mode = json_mode

    # ──────────────────────────────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────────────────────────────

    def call(self, user_message: str) -> str:
        """
        Send *user_message* to Ollama and return the assistant reply as text.

        Raises
        ------
        RuntimeError
            On HTTP errors or unreachable host.
        """
        payload = json.dumps(
            {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": self.system_prompt},
                    {"role": "user", "content": user_message},
                ],
                "stream": False,
                **({"format": "json", "options": {"temperature": 0}} if self.json_mode else {}),
            }
        ).encode("utf-8")

        request = urllib.request.Request(
            url=f"{self.base_url}/api/chat",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return data["message"]["content"]
        except TimeoutError as exc:
            raise RuntimeError(f"[{self.role}] Ollama request timed out after {self.timeout}s") from exc
        except urllib.error.HTTPError as exc:
            raise RuntimeError(
                f"[{self.role}] Ollama HTTP {exc.code}: {exc.reason}"
            ) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(
                f"[{self.role}] Cannot reach Ollama at {self.base_url}: {exc.reason}"
            ) from exc
        except (KeyError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"[{self.role}] Unexpected Ollama response format: {exc}"
            ) from exc

    # ──────────────────────────────────────────────────────────────────
    # Helpers
    # ──────────────────────────────────────────────────────────────────

    @staticmethod
    def parse_json(text: str) -> Any:
        """
        Parse JSON from an LLM response, allowing markdown fences and prose
        after a complete object or array. Never recover nested fragments from
        malformed JSON; callers remain responsible for schema validation.

        Parameters
        ----------
        text : str
            Raw text returned by the model.

        Returns
        -------
        Any
            Parsed JSON value (list, dict, …).

        Raises
        ------
        ValueError
            If the text cannot be parsed as JSON after fence stripping.
        """
        text = text.strip()

        if text.startswith("```"):
            lines = text.splitlines()
            # Drop the opening fence line (```json or ```)
            lines = lines[1:]
            # Drop the closing fence line if present
            if lines and lines[-1].strip().startswith("```"):
                lines = lines[:-1]
            text = "\n".join(lines).strip()

        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            if exc.msg == "Extra data" and text.startswith(("{", "[")):
                # Decode the complete leading value, rather than slicing at
                # the last brace (which may belong to explanatory text).
                value, _ = json.JSONDecoder().raw_decode(text)
                return value
            raise ValueError(
                f"Cannot parse LLM response as JSON: {exc}\n"
                f"Raw (first 300 chars): {text[:300]!r}"
            ) from exc

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(role={self.role!r}, model={self.model!r})"
