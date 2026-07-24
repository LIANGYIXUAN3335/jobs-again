#!/usr/bin/env python3
"""mailsearch.py — 只读搜 Gmail(复用 jobscan 已有的 app password,不依赖 MCP OAuth)。

背景:claude.ai 的 Gmail MCP token 会过期,每次都要重新授权。而 jobscan 早就为
「读验证码」配了一个 Gmail **app password**(secrets.yaml: gmail_app_password),
走 IMAP。app password 不会像 OAuth 那样过期 → 这是查邮件的稳定通道。

用法:
    jobscan/.venv/bin/python jobscan/tools/mailsearch.py openart shoestack
    jobscan/.venv/bin/python jobscan/tools/mailsearch.py --days 90 "max insights"
    jobscan/.venv/bin/python jobscan/tools/mailsearch.py --full sample-35c3c3d3@example.com

安全:只读(readonly=True),只在本机跑,绝不打印 password。secrets.yaml 已在 .gitignore。
"""
from __future__ import annotations

import argparse
import datetime
import email
import email.utils
import imaplib
import re
import sys
from email.header import decode_header, make_header
from pathlib import Path

import yaml

BASE = Path(__file__).resolve().parent.parent          # jobscan/


def _creds():
    sec = yaml.safe_load((BASE / "secrets.yaml").read_text(encoding="utf-8")) or {}
    pw = (sec.get("gmail_app_password") or "").strip()
    cfg = yaml.safe_load((BASE / "config.yaml").read_text(encoding="utf-8")) or {}
    addr = ((cfg.get("mailcode") or {}).get("email")
            or (cfg.get("profile") or {}).get("email"))
    if not (pw and addr):
        sys.exit("secrets.yaml 缺 gmail_app_password,或 config.yaml 缺邮箱地址")
    return addr, pw


def _dec(s):
    try:
        return str(make_header(decode_header(s or "")))
    except Exception:
        return s or ""


def _body(msg) -> str:
    parts = []
    for p in (msg.walk() if msg.is_multipart() else [msg]):
        if p.get_content_type() in ("text/plain", "text/html"):
            try:
                parts.append(p.get_payload(decode=True).decode(
                    p.get_content_charset() or "utf-8", "ignore"))
            except Exception:
                pass
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", " ".join(parts)))


# 面试邮件里最常要抓的几类信息
_PATTERNS = [
    (r"(?:zoom\.us|meet\.google\.com|teams\.microsoft\.com|calendly\.com|ashbyhq\.com/meeting)[^\s\"'<>]*", "LINK"),
    (r"\b(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*,?\s+\w+\s+\d{1,2}[^.,;]{0,40}", "DATE"),
    (r"\b\d{1,2}:\d{2}\s*(?:am|pm|AM|PM)[^.,;]{0,30}", "TIME"),
    (r"(?:Software|Product|Growth|Backend|Frontend|Platform|Data|Full[- ]?Stack)\s*\w*\s*Engineer[^.,;\n]{0,40}", "ROLE"),
]


def main():
    ap = argparse.ArgumentParser(description="只读搜 Gmail(IMAP)")
    ap.add_argument("terms", nargs="+", help="搜索关键词(公司名 / 邮箱 / 任意文本)")
    ap.add_argument("--days", type=int, default=45, help="回溯天数(默认 45)")
    ap.add_argument("--limit", type=int, default=10, help="每个关键词最多显示几封")
    ap.add_argument("--full", action="store_true", help="打印更长正文(默认 600 字)")
    args = ap.parse_args()

    addr, pw = _creds()
    print("账号: %s | app password 已加载(不显示)\n" % addr)
    since = (datetime.datetime.now() - datetime.timedelta(days=args.days)).strftime("%d-%b-%Y")
    body_cap = 3000 if args.full else 600

    M = imaplib.IMAP4_SSL("imap.gmail.com")
    M.login(addr, pw)
    try:
        for folder in ('"[Gmail]/All Mail"', "INBOX"):
            try:
                typ, _ = M.select(folder, readonly=True)
                if typ != "OK":
                    continue
            except Exception:
                continue
            seen, found_any = set(), False
            for term in args.terms:
                typ, data = M.search(None, f'(SINCE {since} TEXT "{term}")')
                if typ != "OK" or not data or not data[0]:
                    continue
                for num in data[0].split()[-args.limit:]:
                    if num in seen:
                        continue
                    seen.add(num)
                    found_any = True
                    typ, d = M.fetch(num, "(RFC822)")
                    if not d or not d[0]:
                        continue
                    msg = email.message_from_bytes(d[0][1])
                    b = _body(msg)
                    print("=" * 100)
                    print("MATCH[%s]  %s" % (term, msg.get("Date")))
                    print("FROM :", _dec(msg.get("From")))
                    print("SUBJ :", _dec(msg.get("Subject")))
                    for pat, label in _PATTERNS:
                        hits = list(dict.fromkeys(re.findall(pat, b)))[:4]
                        if hits:
                            print("  %-5s:" % label, " | ".join(h.strip()[:90] for h in hits))
                    print("  BODY :", b[:body_cap])
            if found_any:
                break
    finally:
        try:
            M.logout()
        except Exception:
            pass


if __name__ == "__main__":
    main()
