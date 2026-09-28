"""Detect and connect local AI tools to LLMWiki without API keys."""
from pathlib import Path
import os
import platform
import shutil
import subprocess
import sys
from typing import Dict, Any

HOME = Path.home()
PROJECT_ROOT = Path(__file__).resolve().parent.parent
ANTIGRAVITY_SKILLS_DIR = HOME / ".gemini" / "config" / "skills" / "llmwiki"
LOCAL_BIN = HOME / ".local" / "bin"
PACKAGED_CLI_DIR = (
    Path(os.environ.get("LOCALAPPDATA", HOME / "AppData" / "Local")) / "LLMWiki" / "bin"
    if os.name == "nt"
    else HOME / ".local" / "share" / "llmwiki" / "bin"
)


def _run(command: list[str], timeout: int = 8) -> subprocess.CompletedProcess:
    workdir = HOME if getattr(sys, "frozen", False) else PROJECT_ROOT
    env = dict(os.environ)
    if not getattr(sys, "frozen", False):
        env["PYTHONPATH"] = str(PROJECT_ROOT)
    return subprocess.run(
        command, cwd=workdir, capture_output=True, text=True, timeout=timeout,
        env=env,
    )


def find_binary(binary: str) -> str | None:
    found = shutil.which(binary)
    if found:
        return found

    names = [binary]
    if os.name == "nt":
        names.extend([f"{binary}.exe", f"{binary}.cmd", f"{binary}.bat"])

    candidate_dirs = [
        HOME / ".local" / "bin",
        HOME / ".cargo" / "bin",
        Path("/opt/homebrew/bin"),
        Path("/usr/local/bin"),
    ]
    if os.name == "nt":
        appdata = Path(os.environ.get("APPDATA", HOME / "AppData" / "Roaming"))
        localappdata = Path(os.environ.get("LOCALAPPDATA", HOME / "AppData" / "Local"))
        candidate_dirs.extend([appdata / "npm", localappdata / "Microsoft" / "WindowsApps"])
    else:
        nvm_root = HOME / ".nvm" / "versions" / "node"
        if nvm_root.exists():
            candidate_dirs.extend(sorted((p / "bin" for p in nvm_root.iterdir() if p.is_dir()), reverse=True))

    for directory in candidate_dirs:
        for name in names:
            candidate = directory / name
            if candidate.is_file():
                return str(candidate)
    return None


def _source_cli() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve()
    return PROJECT_ROOT / "bin" / "llmwiki"


def installed_cli_path() -> Path:
    return PACKAGED_CLI_DIR / ("llmwiki.exe" if os.name == "nt" else "llmwiki")


def _cli_command() -> list[str]:
    installed = installed_cli_path()
    if installed.exists():
        return [str(installed)]
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return ["python3", "-m", "llmwiki"]


def _mcp_status(binary: str) -> Dict[str, Any]:
    path = find_binary(binary)
    if not path:
        return {"detected": False, "path": "", "mcp_installed": False}
    installed = False
    try:
        installed = _run([path, "mcp", "get", "llmwiki"], timeout=12).returncode == 0
    except Exception:
        pass
    return {"detected": True, "path": path, "mcp_installed": installed}


def check_environment() -> Dict[str, Any]:
    """Inspect local inference engines and agent CLIs."""
    ollama_running = False
    ollama_models = []
    try:
        import httpx
        with httpx.Client(timeout=1.5) as client:
            res = client.get("http://localhost:11434/api/tags")
            if res.status_code == 200:
                ollama_running = True
                ollama_models = [m.get("name", "") for m in res.json().get("models", [])]
    except Exception:
        pass

    codex_detected = find_binary("codex") is not None or (HOME / ".codex").exists()
    claude_detected = find_binary("claude") is not None or (HOME / ".claude").exists()
    agy_path = find_binary("agy") or find_binary("antigravity")
    antigravity_detected = (HOME / ".gemini").exists() or agy_path is not None
    recommended = (
        "ollama" if ollama_running and ollama_models
        else "antigravity_cli" if agy_path
        else "codex_cli" if codex_detected
        else "claude_cli" if claude_detected
        else "ollama"
    )
    return {
        "ollama": {"running": ollama_running, "models": ollama_models},
        "codex": {
            "detected": codex_detected,
            "skill_installed": (CODEX_SKILLS_DIR / "SKILL.md").exists(),
        },
        "claude": {
            "detected": claude_detected,
            "skill_installed": (CLAUDE_COMMANDS_DIR / "llmwiki.md").exists(),
        },
        "antigravity": {
            "detected": antigravity_detected,
            "cli_detected": agy_path is not None,
            "cli_path": agy_path or "",
            "skill_installed": (ANTIGRAVITY_SKILLS_DIR / "SKILL.md").exists(),
            "mcp_installed": False,
        },
        "recommended_provider": recommended,
    }


def _is_antigravity_mcp_installed() -> bool:
    import json
    mcp_configs = [
        HOME / ".gemini" / "antigravity" / "mcp_config.json",
        HOME / ".gemini" / "config" / "mcp_config.json",
    ]
    for p in mcp_configs:
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                if "llmwiki" in data.get("mcpServers", {}):
                    return True
            except Exception:
                pass
    return False


def install_antigravity_mcp() -> bool:
    """Register LLMWiki MCP server inside Antigravity's mcp_config.json."""
    import json
    mcp_configs = [
        HOME / ".gemini" / "antigravity" / "mcp_config.json",
        HOME / ".gemini" / "config" / "mcp_config.json",
    ]
    cli_command = _cli_command()
    target_entry = {
        "command": cli_command[0],
        "args": [*cli_command[1:], "serve-mcp"]
    }
    success = False
    for cfg_path in mcp_configs:
        try:
            cfg_path.parent.mkdir(parents=True, exist_ok=True)
            data = {}
            if cfg_path.exists():
                try:
                    data = json.loads(cfg_path.read_text(encoding="utf-8"))
                except Exception:
                    data = {}
            servers = data.setdefault("mcpServers", {})
            servers["llmwiki"] = target_entry
            cfg_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            success = True
        except Exception:
            pass
    return success


CODEX_SKILLS_DIR = HOME / ".codex" / "skills" / "llmwiki"
CLAUDE_COMMANDS_DIR = HOME / ".claude" / "commands"


def install_antigravity_skill() -> bool:
    try:
        candidates = [
            PROJECT_ROOT / ".agents" / "skills" / "llmwiki" / "SKILL.md",
            PROJECT_ROOT / "llmwiki" / "SKILL.md",
        ]
        if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
            candidates.insert(0, Path(sys._MEIPASS) / "llmwiki_skill" / "SKILL.md")
        source = next((path for path in candidates if path.exists()), None)
        if source is None:
            return False
        ANTIGRAVITY_SKILLS_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, ANTIGRAVITY_SKILLS_DIR / "SKILL.md")
        return True
    except Exception:
        return False


def install_codex_skill() -> bool:
    try:
        source = PROJECT_ROOT / ".agents" / "skills" / "llmwiki" / "SKILL.md"
        if not source.exists():
            return False
        CODEX_SKILLS_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, CODEX_SKILLS_DIR / "SKILL.md")
        return True
    except Exception:
        return False


def install_claude_skill() -> bool:
    try:
        source = PROJECT_ROOT / ".agents" / "skills" / "llmwiki" / "SKILL.md"
        if not source.exists():
            return False
        CLAUDE_COMMANDS_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, CLAUDE_COMMANDS_DIR / "llmwiki.md")
        return True
    except Exception:
        return False


def uninstall_mcp_servers() -> None:
    """Safely remove any leftover MCP daemon registrations from Codex and Claude."""
    import json
    import re

    # 1. Clean ~/.codex/config.toml
    codex_cfg = HOME / ".codex" / "config.toml"
    if codex_cfg.exists():
        try:
            content = codex_cfg.read_text(encoding="utf-8")
            new_content = re.sub(r'\[mcp_servers\.llmwiki\][\s\S]*?(?=\n\[|\Z)', '', content)
            new_content = re.sub(r'\[mcp_servers\.llmwiki\.env\][\s\S]*?(?=\n\[|\Z)', '', new_content)
            if new_content != content:
                codex_cfg.write_text(new_content, encoding="utf-8")
        except Exception:
            pass

    # 2. Clean ~/.claude.json
    claude_cfg = HOME / ".claude.json"
    if claude_cfg.exists():
        try:
            data = json.loads(claude_cfg.read_text(encoding="utf-8"))
            if "mcpServers" in data and "llmwiki" in data["mcpServers"]:
                del data["mcpServers"]["llmwiki"]
                claude_cfg.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass


def install_cli_symlink() -> bool:
    try:
        source = _source_cli()
        if not source.exists():
            return False

        if not getattr(sys, "frozen", False) and os.name != "nt":
            LOCAL_BIN.mkdir(parents=True, exist_ok=True)
            target = LOCAL_BIN / "llmwiki"
            if target.exists() or target.is_symlink():
                target.unlink()
            target.symlink_to(source)
            _ensure_unix_path()
            return True

        PACKAGED_CLI_DIR.mkdir(parents=True, exist_ok=True)
        installed = installed_cli_path()
        if installed.exists() or installed.is_symlink():
            installed.unlink()
        shutil.copy2(source, installed)
        if os.name != "nt":
            installed.chmod(installed.stat().st_mode | 0o111)
            LOCAL_BIN.mkdir(parents=True, exist_ok=True)
            target = LOCAL_BIN / "llmwiki"
            if target.exists() or target.is_symlink():
                target.unlink()
            target.symlink_to(installed)
            _ensure_unix_path()
        else:
            _ensure_windows_path(str(PACKAGED_CLI_DIR))
        return True
    except Exception:
        return False


def _ensure_unix_path() -> None:
    current = os.environ.get("PATH", "").split(os.pathsep)
    if str(LOCAL_BIN) in current:
        return
    shell = Path(os.environ.get("SHELL", "")).name
    if shell == "fish":
        profile = HOME / ".config" / "fish" / "config.fish"
        line = "fish_add_path -m $HOME/.local/bin"
    elif shell == "zsh" or platform.system() == "Darwin":
        profile = HOME / ".zprofile"
        line = 'export PATH="$HOME/.local/bin:$PATH"'
    else:
        profile = HOME / ".profile"
        line = 'export PATH="$HOME/.local/bin:$PATH"'
    profile.parent.mkdir(parents=True, exist_ok=True)
    content = profile.read_text(encoding="utf-8") if profile.exists() else ""
    marker = "# LLMWiki CLI"
    if marker not in content:
        prefix = "" if not content or content.endswith("\n") else "\n"
        profile.write_text(content + prefix + marker + "\n" + line + "\n", encoding="utf-8")


def _ensure_windows_path(bin_dir: str) -> None:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ | winreg.KEY_WRITE) as key:
            try:
                current, kind = winreg.QueryValueEx(key, "Path")
            except FileNotFoundError:
                current, kind = "", winreg.REG_EXPAND_SZ
            entries = [item.strip() for item in current.split(";") if item.strip()]
            if bin_dir.lower() not in {item.lower() for item in entries}:
                entries.append(bin_dir)
                winreg.SetValueEx(key, "Path", 0, kind, ";".join(entries))
    except Exception:
        pass


def bootstrap_installation() -> Dict[str, Any]:
    """Idempotent first-run setup for a packaged desktop installation."""
    from .config import load_config, save_config

    cli_ok = install_cli_symlink()
    env = check_environment()
    results: Dict[str, Any] = {
        "cli": cli_ok,
        "cli_path": str(installed_cli_path()) if cli_ok else "",
        "codex": None,
        "claude": None,
        "antigravity": None,
    }

    # Clean up any obsolete background MCP servers
    uninstall_mcp_servers()

    if env["codex"]["detected"]:
        results["codex"] = install_codex_skill()
    if env["claude"]["detected"]:
        results["claude"] = install_claude_skill()
    if env["antigravity"]["detected"]:
        results["antigravity"] = install_antigravity_skill()

    cfg = load_config()

    updates: Dict[str, Any] = {"initialized": True}
    if not cfg.get("initialized"):
        provider = env.get("recommended_provider", "ollama")
        updates["provider"] = provider
        if provider == "ollama" and env["ollama"]["models"]:
            models = env["ollama"]["models"]
            preferred = next((m for m in models if m.startswith(("qwen", "gemma", "llama"))), models[0])
            updates["model"] = preferred
    results["config"] = save_config(updates)
    return results
