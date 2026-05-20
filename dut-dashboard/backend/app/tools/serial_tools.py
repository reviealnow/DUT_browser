from __future__ import annotations

import glob
from typing import Literal

from serial.tools import list_ports

from app.tools.context import AppContext
from app.tools.registry import tool


@tool(name="list_serial_ports", description="List detected serial/USB ports on the host", tags=["serial"])
def list_serial_ports(ctx: AppContext) -> dict:  # noqa: ARG001
    ports = [
        {"device": p.device, "description": p.description or "", "hwid": p.hwid or ""}
        for p in list_ports.comports()
    ]
    glob_devices = sorted(set(glob.glob("/dev/cu.*")))
    return {"ports": ports, "glob_devices": glob_devices}


@tool(name="open_serial", description="Open a serial port or start log replay", tags=["serial"])
def open_serial(
    ctx: AppContext,
    port: str = "",
    baudrate: int = 115200,
    mode: Literal["serial", "replay"] = "serial",
    replay_path: str | None = None,
    replay_interval_ms: int = 100,
) -> dict:
    try:
        ctx.serial_worker.open(
            port=port,
            baudrate=baudrate,
            mode=mode,
            replay_path=replay_path,
            replay_interval_ms=replay_interval_ms,
        )
        return {"ok": True, "mode": mode, "log_path": ctx.serial_worker.current_log_path}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@tool(name="close_serial", description="Close the current serial connection", tags=["serial"])
def close_serial(ctx: AppContext) -> dict:
    try:
        ctx.serial_worker.close()
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@tool(name="send_serial", description="Send text to the open serial port", tags=["serial"])
def send_serial(ctx: AppContext, text: str) -> dict:
    ctx.serial_worker.send(text)
    return {"ok": True}


@tool(name="get_efficiency_report", description="Get serial read efficiency statistics", tags=["serial", "stats"])
def get_efficiency_report(ctx: AppContext) -> dict:
    return ctx.parser.efficiency_report()
