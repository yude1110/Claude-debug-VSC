"""Claude Debug — 把 VS Code 目前的檔案整份送給 Claude 除錯，結果直接放回剪貼簿。支援 Windows / macOS。

安裝（每台電腦執行一次，會寫入 VS Code 的 tasks.json 與 keybindings.json）：
  Windows：python  claude_debug.py --install
  macOS  ：python3 claude_debug.py --install

快捷鍵（在編輯器內按）：
  Ctrl+Alt+D        剪貼簿 = 修正後的完整程式碼
  Ctrl+Alt+Shift+D  剪貼簿 = 問題說明 + 修正後程式碼（Markdown）
  （macOS 上是 Control+Option+D / Control+Option+Shift+D）

透過已登入的 Claude Code CLI（claude -p）呼叫 Claude，不需要 API key。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

MODEL = None    # 例如 "claude-opus-5-5"；None = 跟隨 Claude Code 的預設模型
EFFORT = None   # "low" / "medium" / "high" / "xhigh" / "max"；None = 跟隨 Claude Code 的設定
TIMEOUT_SECONDS = 600

IS_MAC = sys.platform == "darwin"

PROMPT = """請幫我除錯下面這份程式碼檔案。

要求：
1. 找出語法錯誤、執行期錯誤、邏輯錯誤，以及明顯的邊界條件問題。
2. 只修改有問題的地方；保留原本的結構、命名、註解、縮排與排版風格，不要做無關的重構或美化。
3. 嚴格依照下列格式輸出，標籤之外不要有任何文字：

<report>
用繁體中文條列每個問題：位置（函式名稱）、引用出問題的那行原始碼、原因、修正方式。
沒有發現問題就寫「未發現明顯錯誤」。
</report>
<fixed_code>
修正後的完整檔案內容，從第一行到最後一行，不可省略任何部分，不要用 ``` 包起來。
</fixed_code>

<source_file name="{name}">
{code}
</source_file>
"""


def read_source(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "cp950"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            pass
    return raw.decode("utf-8", errors="replace")


def find_claude() -> str:
    found = shutil.which("claude")
    if found:
        # Windows npm 安裝時找到的是 claude.cmd；直接呼叫真正的 exe，避免參數經過 cmd.exe 轉譯
        exe = Path(found).parent / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
        return str(exe) if exe.is_file() else found
    # 從 Dock 開啟的 VS Code 不一定帶有完整 PATH
    for candidate in (Path.home() / ".local/bin/claude", Path.home() / ".claude/local/claude",
                      Path("/opt/homebrew/bin/claude"), Path("/usr/local/bin/claude")):
        if candidate.is_file():
            return str(candidate)
    sys.exit("找不到 claude 指令，請先安裝並登入 Claude Code。")


def ask_claude(prompt: str, cwd: Path) -> str:
    cmd = [find_claude(), "-p", "--tools", "", "--no-session-persistence"]
    if MODEL:
        cmd += ["--model", MODEL]
    if EFFORT:
        cmd += ["--effort", EFFORT]
    try:
        proc = subprocess.run(cmd, cwd=cwd, input=prompt.encode("utf-8"), capture_output=True,
                              timeout=TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        sys.exit(f"逾時（超過 {TIMEOUT_SECONDS} 秒），已取消。")
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).decode("utf-8", errors="replace")
        sys.exit(f"claude 執行失敗（exit {proc.returncode}）：\n{detail}")
    return proc.stdout.decode("utf-8", errors="replace")


def extract(tag: str, text: str, greedy: bool = False) -> str | None:
    body = ".*" if greedy else ".*?"
    match = re.search(rf"<{tag}>\n?({body})\n?</{tag}>", text, re.S)
    return match.group(1) if match else None


def clean_code(fixed: str, original: str) -> str:
    fenced = re.fullmatch(r"```[^\n]*\n(.*?)\n?```", fixed.strip(), re.S)
    if fenced and not original.lstrip().startswith("```"):
        fixed = fenced.group(1)
    # 結尾換行跟原檔一致，貼回去才不會多出或少掉最後一行
    return fixed.rstrip("\n") + ("\n" if original.endswith("\n") else "")


def set_clipboard(text: str) -> None:
    if IS_MAC:
        subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=True,
                       env={**os.environ, "LANG": "en_US.UTF-8"})
    else:
        _set_clipboard_windows(text)


def _set_clipboard_windows(text: str) -> None:
    import ctypes
    from ctypes import wintypes

    CF_UNICODETEXT, GMEM_MOVEABLE = 13, 0x0002
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = wintypes.LPVOID
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.SetClipboardData.restype = wintypes.HANDLE

    data = text.encode("utf-16-le") + b"\x00\x00"
    for _ in range(20):  # 剪貼簿可能正被其他程式佔用，稍等重試
        if user32.OpenClipboard(None):
            break
        time.sleep(0.1)
    else:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        user32.EmptyClipboard()
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        ctypes.memmove(kernel32.GlobalLock(handle), data, len(data))
        kernel32.GlobalUnlock(handle)
        if not user32.SetClipboardData(CF_UNICODETEXT, handle):
            kernel32.GlobalFree(handle)
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        user32.CloseClipboard()


def notify_done() -> None:
    if IS_MAC:
        subprocess.run(["osascript", "-e", 'display notification "已複製到剪貼簿" '
                        'with title "Claude Debug" sound name "Glass"'], check=False)
    else:
        import winsound
        winsound.MessageBeep(winsound.MB_OK)


def vscode_user_dir() -> Path:
    if IS_MAC:
        return Path.home() / "Library/Application Support/Code/User"
    return Path(os.environ["APPDATA"]) / "Code" / "User"


def load_jsonc(path: Path, default: object) -> object:
    if not path.exists():
        return default
    text = path.read_text(encoding="utf-8")
    # 只去掉整行的 // 註解（VS Code 預設檔案的寫法）；其他 JSONC 語法就請使用者手動處理
    stripped = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("//"))
    try:
        return json.loads(stripped) if stripped.strip() else default
    except json.JSONDecodeError:
        sys.exit(f"{path} 含有無法自動解析的內容，請手動加入設定。")


def install() -> None:
    user_dir = vscode_user_dir()
    if not user_dir.is_dir():
        sys.exit(f"找不到 VS Code 使用者設定資料夾：{user_dir}")
    script = str(Path(__file__).resolve())
    modes = {"code": "ctrl+alt+d", "report": "ctrl+alt+shift+d"}

    tasks_path = user_dir / "tasks.json"
    tasks = load_jsonc(tasks_path, {"version": "2.0.0", "tasks": []})
    labels = {f"Claude Debug ({mode})" for mode in modes}
    tasks["tasks"] = [t for t in tasks.get("tasks", []) if t.get("label") not in labels]
    for mode in modes:
        tasks["tasks"].append({
            "label": f"Claude Debug ({mode})",
            "type": "process",
            "command": sys.executable,
            "args": [script, "${file}", "--copy", mode],
            "options": {"cwd": "${fileDirname}"},
            "presentation": {"reveal": "always", "focus": False, "panel": "shared",
                             "clear": True, "showReuseMessage": False},
            "problemMatcher": [],
        })

    keys_path = user_dir / "keybindings.json"
    keys = load_jsonc(keys_path, [])
    keys = [k for k in keys if "Claude Debug" not in json.dumps(k.get("args", ""))]
    for mode, key in modes.items():
        keys.append({
            "key": key,
            "command": "runCommands",
            "args": {"commands": [
                "workbench.action.files.save",
                {"command": "workbench.action.tasks.runTask", "args": f"Claude Debug ({mode})"},
            ]},
            "when": "editorTextFocus",
        })

    for path, data in ((tasks_path, tasks), (keys_path, keys)):
        if path.exists():
            shutil.copyfile(path, path.with_name(path.name + ".bak"))
        path.write_text(json.dumps(data, ensure_ascii=False, indent=4) + "\n", encoding="utf-8")
    print(f"done：已寫入 {tasks_path} 與 {keys_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="把整份程式碼送給 Claude 除錯，結果放回剪貼簿")
    parser.add_argument("file", type=Path, nargs="?")
    parser.add_argument("--copy", choices=("code", "report"), default="code",
                        help="code：修正後的完整程式碼；report：問題說明 + 修正後程式碼")
    parser.add_argument("--install", action="store_true", help="寫入 VS Code 的快捷鍵設定")
    args = parser.parse_args()

    if args.install:
        install()
        return
    if args.file is None:
        parser.error("請指定檔案")

    path = args.file.resolve()
    if not path.is_file():
        sys.exit(f"找不到檔案：{path}（未命名的新檔請先存檔）")
    source = read_source(path)
    if not source.strip():
        sys.exit("檔案是空的。")

    print("⏳ Claude debugging…", flush=True)
    reply = ask_claude(PROMPT.format(name=path.name, code=source), cwd=path.parent)

    fixed = extract("fixed_code", reply, greedy=True)
    if fixed is None:
        set_clipboard(reply)
        sys.exit("⚠️ 回應格式不如預期，已把 Claude 的完整回覆複製到剪貼簿。")
    fixed = clean_code(fixed, source)

    if args.copy == "code":
        set_clipboard(fixed)
    else:
        report = (extract("report", reply) or "").strip()
        lang = path.suffix.lstrip(".")
        code = fixed.rstrip("\n")
        set_clipboard(f"## 問題說明\n\n{report}\n\n## 修正後程式碼\n\n```{lang}\n{code}\n```\n")
    print("✅ done")
    notify_done()


if __name__ == "__main__":
    main()
