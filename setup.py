# -*- coding: utf-8 -*-
"""
wake_UP 自建前端 —— 安装 / 配置 / 启动
====================================
用法：
    python setup.py            # 检查依赖、引导填写中转站配置，然后启动服务
    python setup.py --config   # 只填配置，不启动
    python setup.py --check    # 只检查依赖

说明：中转站的地址与 key 只写进本机 config.json，不经过任何聊天。
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
CONFIG = BASE / "config.json"


def ensure_dep():
    try:
        import deamo_wake
        print("[OK] deamo_wake 已就绪:", os.path.dirname(deamo_wake.__file__))
        return True
    except ImportError:
        pass

    candidates = [r"D:\机\wake_UP-main", r"D:\机\wake_UP", r".\wake_UP-main", r".\wake_UP"]
    for path in candidates:
        p = Path(path)
        if p.is_dir() and (p / "pyproject.toml").exists():
            print("[..] 正在从", path, "安装 deamo-wake ...")
            try:
                subprocess.check_call([sys.executable, "-m", "pip", "install", "-e", str(p)])
                import deamo_wake
                print("[OK] 安装完成")
                return True
            except Exception as exc:
                print("[!!] 安装失败:", exc)
    print("[!!] 没找到 deamo_wake。请手动执行：")
    print("      cd D:\\机\\wake_UP-main")
    print("      python -m pip install -e .")
    return False


def ask(prompt, default=""):
    suffix = (" [" + default + "]") if default else ""
    try:
        v = input(prompt + suffix + ": ").strip()
    except EOFError:
        v = ""
    return v or default


def do_config():
    cfg = {}
    if CONFIG.exists():
        try:
            cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
        except Exception:
            cfg = {}

    print("\n--- 中转站配置（只写进本机 config.json）---")
    api_base = ask("中转站地址（要带 /v1，例如 https://xxx.com/v1）",
                   cfg.get("api_base", ""))
    api_key = ask("API Key", cfg.get("api_key", ""))
    main_model = ask("主模型 id", cfg.get("main_model", ""))
    slm_model = ask("小模型 id（留空＝用主模型）", cfg.get("slm_model", "")) or main_model
    port = ask("端口（默认 8000）", str(cfg.get("port", 8000)))

    data = {
        "api_base": api_base.rstrip("/"),
        "api_key": api_key,
        "main_model": main_model,
        "slm_model": slm_model,
        "port": int(port) if str(port).isdigit() else 8000,
    }
    CONFIG.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n[OK] 配置已写入", CONFIG)
    return data


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", action="store_true", help="只填配置，不启动")
    ap.add_argument("--check", action="store_true", help="只检查依赖")
    args = ap.parse_args()

    print("=" * 60)
    print(" wake_UP 自建前端 安装程序")
    print(" 目录:", BASE)
    print("=" * 60)

    if args.check:
        ok = ensure_dep()
        print("依赖检查:", "通过" if ok else "未通过")
        return

    do_config()
    if not ensure_dep():
        print("\n依赖没装好，先解决这一步再启动。")
        return

    if args.config:
        print("\n配置完成。启动：python server.py")
        return

    print("\n启动服务…（按 Ctrl+C 停止）\n")
    try:
        subprocess.call([sys.executable, "server.py"], cwd=str(BASE))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
