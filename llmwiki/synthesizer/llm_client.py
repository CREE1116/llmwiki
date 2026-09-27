"""LLM client for local Ollama or an authenticated local agent CLI."""
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

import httpx

from ..config import load_config, OLLAMA_HOST, DEFAULT_MODEL
from ..installer import find_binary


class LLMClient:
    def __init__(self):
        self.config = load_config()
        self.provider = self.config.get("provider", "ollama")
        self.model = self.config.get("model", DEFAULT_MODEL)
        self.host = self.config.get("ollama_host", OLLAMA_HOST).rstrip("/")

    def is_available(self) -> bool:
        if self.provider == "ollama":
            try:
                with httpx.Client(timeout=1.5) as client:
                    return client.get(f"{self.host}/api/tags").status_code == 200
            except Exception:
                return False
        if self.provider in ("antigravity_cli", "agy_cli", "antigravity"):
            return (find_binary("agy") or find_binary("antigravity")) is not None
        if self.provider == "codex_cli":
            return find_binary("codex") is not None
        if self.provider == "claude_cli":
            return find_binary("claude") is not None
        if self.provider in ("openai", "vllm", "lmstudio"):
            api_key = self.config.get("api_key") or os.environ.get("OPENAI_API_KEY", "")
            base_url = self.config.get("openai_base_url") or os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
            return bool(api_key or "localhost" in base_url or "127.0.0.1" in base_url)
        return False

    def generate(self, prompt: str, system: Optional[str] = None, json_format: bool = False, temperature: float = 0.2) -> str:
        if self.provider in ("antigravity_cli", "agy_cli", "antigravity"):
            return self._generate_antigravity(prompt, system)
        if self.provider == "codex_cli":
            return self._generate_codex(prompt, system)
        if self.provider == "claude_cli":
            return self._generate_claude(prompt, system)
        if self.provider in ("openai", "vllm", "lmstudio"):
            return self._generate_openai(prompt, system, json_format, temperature)
        return self._generate_ollama(prompt, system, json_format, temperature)

    def _generate_ollama(self, prompt: str, system: Optional[str], json_format: bool, temperature: float) -> str:
        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": temperature, "top_p": 0.9},
        }
        if system:
            payload["system"] = system
        if json_format:
            payload["format"] = "json"
        with httpx.Client(timeout=180.0) as client:
            res = client.post(f"{self.host}/api/generate", json=payload)
            res.raise_for_status()
            return res.json().get("response", "")

    def _generate_openai(self, prompt: str, system: Optional[str], json_format: bool, temperature: float) -> str:
        """Call any OpenAI-compatible API endpoint (OpenAI, vLLM, LMStudio, DeepSeek, Groq, Ollama /v1)."""
        import time
        base_url = (self.config.get("openai_base_url") or os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")).rstrip("/")
        api_key = self.config.get("api_key") or os.environ.get("OPENAI_API_KEY", "dummy-key")
        model = self.config.get("model") or "gpt-4o-mini"

        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
        }
        if json_format:
            payload["response_format"] = {"type": "json_object"}

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }

        # Exponential backoff retry
        last_err = None
        for attempt in range(3):
            try:
                with httpx.Client(timeout=120.0) as client:
                    res = client.post(f"{base_url}/chat/completions", json=payload, headers=headers)
                    res.raise_for_status()
                    data = res.json()
                    return data["choices"][0]["message"]["content"]
            except Exception as e:
                last_err = e
                time.sleep(2 ** attempt)

        raise RuntimeError(f"OpenAI API call failed after 3 attempts: {last_err}")

    @staticmethod
    def _combined_prompt(prompt: str, system: Optional[str]) -> str:
        return f"{system}\n\n{prompt}" if system else prompt

    def _generate_antigravity(self, prompt: str, system: Optional[str]) -> str:
        """Execute distillation via local Antigravity CLI (agy)."""
        binary = find_binary("agy") or find_binary("antigravity")
        if not binary:
            raise RuntimeError("Antigravity CLI (agy)를 찾지 못했습니다.")

        combined = self._combined_prompt(prompt, system)
        # Try running non-interactive headless prompt execution
        candidates = [
            [binary, "prompt", "--non-interactive", "-"],
            [binary, "exec", "--color", "never", "-"],
            [binary, "--print", "-"]
        ]

        last_err = None
        for cmd in candidates:
            try:
                result = subprocess.run(
                    cmd, input=combined, text=True,
                    capture_output=True, timeout=240,
                    env={**os.environ, "NO_COLOR": "1"}
                )
                if result.returncode == 0 and result.stdout.strip():
                    return result.stdout.strip()
                last_err = result.stderr or result.stdout
            except Exception as e:
                last_err = str(e)
                continue

        raise RuntimeError(f"Antigravity CLI 실행 실패: {last_err}")

    def _generate_codex(self, prompt: str, system: Optional[str]) -> str:
        binary = find_binary("codex")
        if not binary:
            raise RuntimeError("Codex CLI를 찾지 못했습니다.")
        with tempfile.TemporaryDirectory(prefix="llmwiki-") as temp_dir:
            output_path = Path(temp_dir) / "result.txt"
            command = [
                binary, "exec", "--ephemeral", "--skip-git-repo-check",
                "--sandbox", "read-only", "--color", "never",
                "--output-last-message", str(output_path), "-",
            ]
            result = subprocess.run(
                command, input=self._combined_prompt(prompt, system), text=True,
                capture_output=True, timeout=240, env={**os.environ, "NO_COLOR": "1"},
            )
            if result.returncode != 0:
                raise RuntimeError((result.stderr or result.stdout).strip() or "Codex CLI 실행 실패")
            return output_path.read_text(encoding="utf-8").strip()

    def _generate_claude(self, prompt: str, system: Optional[str]) -> str:
        binary = find_binary("claude")
        if not binary:
            raise RuntimeError("Claude Code CLI를 찾지 못했습니다.")
        command = [
            binary, "--print", "--no-session-persistence", "--permission-mode", "dontAsk",
            "--tools", "", "--output-format", "text",
        ]
        if system:
            command.extend(["--system-prompt", system])
        result = subprocess.run(command, input=prompt, text=True, capture_output=True, timeout=240)
        if result.returncode != 0:
            raise RuntimeError((result.stderr or result.stdout).strip() or "Claude Code CLI 실행 실패")
        return result.stdout.strip()

