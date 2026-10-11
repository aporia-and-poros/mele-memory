"""Mele Memory 的 MCP 服务。用法见 README「接到 Claude 桌面版」。"""
from .server import Service, Settings, build_http_app, build_server, main

__all__ = ["Service", "Settings", "build_http_app", "build_server", "main"]
