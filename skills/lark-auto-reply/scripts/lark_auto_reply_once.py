#!/usr/bin/env python3
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def run_cmd(cmd: List[str]) -> Dict[str, Any]:
    p = subprocess.run(cmd, capture_output=True, text=True)
    out = (p.stdout or "").strip()
    err = (p.stderr or "").strip()
    raw = out or err
    if p.returncode != 0:
        raise RuntimeError(f"command failed: {' '.join(cmd)}\n{raw}")
    try:
        return json.loads(out)
    except Exception:
        raise RuntimeError(f"non-json output from {' '.join(cmd)}\n{raw}")


def build_lark(profile: Optional[str]) -> List[str]:
    base = ["lark-cli"]
    if profile:
        base += ["--profile", profile]
    return base


def get_self_open_id(lark: List[str]) -> Optional[str]:
    d = run_cmd(lark + ["auth", "status"])
    return d.get("userOpenId")


def resolve_user_open_id(lark: List[str], query: str) -> Tuple[str, str]:
    d = run_cmd(lark + ["contact", "+search-user", "--query", query, "--format", "json"])
    users = (((d or {}).get("data") or {}).get("users") or [])
    if not users:
        raise RuntimeError(f"no user found for query={query}")

    # Prefer exact name match, else first result
    selected = None
    for u in users:
        if str(u.get("name", "")).strip() == query.strip():
            selected = u
            break
    if not selected:
        selected = users[0]

    return selected.get("open_id"), selected.get("name", "")


def resolve_chat_id(lark: List[str], query: str) -> Tuple[str, str]:
    d = run_cmd(lark + ["im", "+chat-search", "--query", query, "--format", "json"])
    data = d.get("data") or {}
    chats = data.get("items") or data.get("chats") or []
    if not chats:
        raise RuntimeError(f"no chat found for query={query}")

    selected = None
    for c in chats:
        name = c.get("name") or c.get("chat_name") or ""
        if str(name).strip() == query.strip():
            selected = c
            break
    if not selected:
        selected = chats[0]

    cid = selected.get("chat_id") or selected.get("id")
    cname = selected.get("name") or selected.get("chat_name") or ""
    return cid, cname


def fetch_messages(lark: List[str], *, user_open_id: Optional[str], chat_id: Optional[str], page_size: int) -> List[Dict[str, Any]]:
    cmd = lark + ["im", "+chat-messages-list", "--page-size", str(page_size), "--sort", "desc", "--format", "json"]
    if user_open_id:
        cmd += ["--user-id", user_open_id]
    elif chat_id:
        cmd += ["--chat-id", chat_id]
    else:
        raise RuntimeError("either user_open_id or chat_id required")

    d = run_cmd(cmd)
    msgs = ((d.get("data") or {}).get("messages") or [])
    return msgs


def normalize_text(s: str) -> str:
    s = re.sub(r"\s+", " ", s or "").strip().lower()
    s = re.sub(r"[\u200b\u200c\u200d]", "", s)
    return s


def extract_content_text(msg: Dict[str, Any]) -> str:
    c = msg.get("content")
    if isinstance(c, str):
        # Usually plain text already; for JSON string content try best-effort extraction.
        cc = c.strip()
        if cc.startswith("{") and cc.endswith("}"):
            try:
                j = json.loads(cc)
                if isinstance(j, dict):
                    t = j.get("text")
                    if isinstance(t, str):
                        return t
            except Exception:
                pass
        return c
    if isinstance(c, dict):
        if isinstance(c.get("text"), str):
            return c["text"]
    return ""


def should_reply(msg_text: str, keywords: List[str]) -> bool:
    text = normalize_text(msg_text)
    if not text:
        return False
    if keywords:
        for k in keywords:
            if normalize_text(k) in text:
                return True
        return False
    # default heuristic
    return ("?" in msg_text) or ("？" in msg_text) or ("怎么" in msg_text) or ("如何" in msg_text) or ("帮" in msg_text)


def load_cooldown(path: Path) -> Dict[str, float]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_cooldown(path: Path, data: Dict[str, float]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def build_reply(text: str) -> str:
    brief = text.strip().replace("\n", " ")
    brief = brief[:80] + ("..." if len(brief) > 80 else "")
    return (
        "我看到了，你这个问题可以这样落地：先把目标会话最近消息拉齐，再做是否需要回复判断，"
        "命中后按‘先结论后步骤’生成1-3段回复并发送；最后加去重冷却避免重复。"
        f"\n你刚这条我也已纳入处理：{brief}"
    )


def send_message(lark: List[str], *, user_open_id: Optional[str], chat_id: Optional[str], text: str) -> Dict[str, Any]:
    cmd = lark + ["im", "+messages-send", "--as", "user", "--text", text]
    if user_open_id:
        cmd += ["--user-id", user_open_id]
    elif chat_id:
        cmd += ["--chat-id", chat_id]
    else:
        raise RuntimeError("either user_open_id or chat_id required")
    return run_cmd(cmd)


def main() -> int:
    ap = argparse.ArgumentParser(description="Run one cycle of Lark monitor->judge->auto-reply")
    ap.add_argument("--profile", default=None)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--user-open-id")
    g.add_argument("--user-query")
    g.add_argument("--chat-id")
    g.add_argument("--chat-query")

    ap.add_argument("--history-limit", type=int, default=50)
    ap.add_argument("--keywords", default="", help="comma-separated keywords")
    ap.add_argument("--cooldown-seconds", type=int, default=1800)
    ap.add_argument("--cooldown-file", default=".state/lark-auto-reply-cooldown.json")
    ap.add_argument("--reply-text", default="")
    ap.add_argument("--signature", default="（本条消息由 ClawPhone 助手代发）")
    ap.add_argument("--dry-run", action="store_true")

    args = ap.parse_args()

    lark = build_lark(args.profile)
    self_open_id = get_self_open_id(lark)

    target: Dict[str, str] = {"type": "", "id": "", "name": ""}
    user_open_id = args.user_open_id
    chat_id = args.chat_id

    if args.user_query:
        uid, uname = resolve_user_open_id(lark, args.user_query)
        user_open_id = uid
        target = {"type": "user", "id": uid or "", "name": uname or args.user_query}
    elif args.chat_query:
        cid, cname = resolve_chat_id(lark, args.chat_query)
        chat_id = cid
        target = {"type": "chat", "id": cid or "", "name": cname or args.chat_query}
    elif args.user_open_id:
        target = {"type": "user", "id": args.user_open_id, "name": args.user_open_id}
    elif args.chat_id:
        target = {"type": "chat", "id": args.chat_id, "name": args.chat_id}

    msgs = fetch_messages(lark, user_open_id=user_open_id, chat_id=chat_id, page_size=args.history_limit)

    # pick latest inbound meaningful message
    selected = None
    for m in msgs:
        if m.get("deleted"):
            continue
        sender = m.get("sender") or {}
        if self_open_id and sender.get("id") == self_open_id:
            continue
        if sender.get("sender_type") == "app":
            continue
        text = extract_content_text(m)
        if not text.strip():
            continue
        selected = (m, text)
        break

    result = {
        "target": target,
        "fetched": len(msgs),
        "selected_message_id": None,
        "should_reply": False,
        "reason": "",
        "sent": False,
        "send_result": None,
    }

    if not selected:
        result["reason"] = "no inbound message matched"
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    msg, text = selected
    result["selected_message_id"] = msg.get("message_id")

    keywords = [k.strip() for k in args.keywords.split(",") if k.strip()]
    if not should_reply(text, keywords):
        result["reason"] = "latest inbound does not match reply rules"
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    fingerprint_base = f"{target.get('type')}|{target.get('id')}|{normalize_text(text)}"
    fp = hashlib.sha1(fingerprint_base.encode("utf-8")).hexdigest()

    cooldown_path = Path(args.cooldown_file)
    db = load_cooldown(cooldown_path)
    now = time.time()
    last_ts = float(db.get(fp, 0))
    if now - last_ts < args.cooldown_seconds:
        result["should_reply"] = False
        result["reason"] = f"cooldown active ({int(now-last_ts)}s < {args.cooldown_seconds}s)"
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    result["should_reply"] = True
    reply = args.reply_text.strip() or build_reply(text)
    if args.signature.strip():
        reply = f"{reply}\n{args.signature.strip()}"

    if args.dry_run:
        result["reason"] = "dry-run"
        result["preview_reply"] = reply
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    send_res = send_message(lark, user_open_id=user_open_id, chat_id=chat_id, text=reply)
    db[fp] = now
    save_cooldown(cooldown_path, db)

    result["sent"] = True
    result["send_result"] = send_res
    result["reason"] = "sent"
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False, indent=2))
        raise SystemExit(1)
