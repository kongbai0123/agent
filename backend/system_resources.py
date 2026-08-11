"""Portable RAM and GPU resource sampling."""

from __future__ import annotations

import platform
import subprocess
from typing import Any, Dict, List

from subprocess_env import agent_subprocess_env


def detect_ram() -> Dict[str, Any]:
    try:
        import psutil

        vm = psutil.virtual_memory()
        return {
            "ram_total_gb": round(vm.total / 1024**3, 1),
            "ram_free_gb": round(vm.available / 1024**3, 1),
        }
    except Exception:
        pass
    try:
        if platform.system() == "Windows":
            import ctypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
            return {
                "ram_total_gb": round(stat.ullTotalPhys / 1024**3, 1),
                "ram_free_gb": round(stat.ullAvailPhys / 1024**3, 1),
            }
    except Exception:
        pass
    return {"ram_total_gb": None, "ram_free_gb": None}


def detect_gpus() -> List[Dict[str, Any]]:
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total,memory.free",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=8,
            env=agent_subprocess_env(),
        )
        if result.returncode != 0:
            return []
        gpus = []
        for line in result.stdout.strip().splitlines():
            parts = [part.strip() for part in line.split(",")]
            if len(parts) >= 3:
                gpus.append(
                    {
                        "name": parts[0],
                        "vram_total_gb": round(float(parts[1]) / 1024, 1),
                        "vram_free_gb": round(float(parts[2]) / 1024, 1),
                    }
                )
        return gpus
    except Exception:
        return []


def sample_system_resources() -> Dict[str, float]:
    ram = detect_ram()
    gpus = detect_gpus()
    return {
        "ram_free_gb": float(ram.get("ram_free_gb") or 0.0),
        "vram_free_gb": max(
            (float(gpu.get("vram_free_gb") or 0.0) for gpu in gpus),
            default=0.0,
        ),
    }
