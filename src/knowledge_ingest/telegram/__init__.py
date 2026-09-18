"""Telegram Source Runtime（冻结设计 §5.1）。

TG2 范围：仅本地状态层（SQLite 事实库 / Source Registry /
review & status 纯本地 CLI / maintenance lock）。
watcher、Telethon adapter、WxPusher 由后续 TG 落地。
"""
