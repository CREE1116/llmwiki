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
        if self.provider == "codex_cli":
            return find_binary("codex") is not None
        if self.provider == "claude_cli":
            return find_binary("claude") is not None
        return False

    def generate(self, prompt: str, system: Optional[str] = None, json_format: bool = False, temperature: float = 0.2) -> str:
        if self.provider == "codex_cli":
            return self._generate_codex(prompt, system)
        if self.provider == "claude_cli":
            return self._generate_claude(prompt, system)
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

    @staticmethod
    def _combined_prompt(prompt: str, system: Optional[str]) -> str:
        return f"{system}\n\n{prompt}" if system else prompt

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
