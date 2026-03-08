#!/usr/bin/env python3
"""
Ami — Companion app for the Anna clawbot contract.
Provides CLI and programmatic access to trade, invest, deposit, and query
Anna vault, orders, strategies, positions, and rounds.
All outputs in one single file; no split modules.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import hashlib
import struct
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

# Optional deps for EVM; fallback if not installed
try:
    from web3 import Web3
    from web3.contract import Contract
    from web3.types import TxReceipt, Wei
    HAS_WEB3 = True
except ImportError:
    HAS_WEB3 = False
    Web3 = None
    Contract = None
    TxReceipt = None
    Wei = None

# -----------------------------------------------------------------------------
# Constants (Ami-specific; do not reuse in other apps)
# -----------------------------------------------------------------------------

AMI_APP_NAME = "Ami"
AMI_VERSION = "1.0.0"
AMI_CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".ami")
AMI_CONFIG_FILE = os.path.join(AMI_CONFIG_DIR, "config.json")
AMI_DEFAULT_RPC = "https://eth.llamarpc.com"
AMI_CHAIN_ID_MAINNET = 1
AMI_CHAIN_ID_SEPOLIA = 11155111
AMI_CHAIN_ID_BASE = 8453
AMI_GAS_LIMIT_DEFAULT = 300_000
AMI_GAS_MULTIPLIER = 1.2
AMI_MAX_RETRIES = 5
AMI_RETRY_DELAY_SEC = 2.0
AMI_HEX_PREFIX = "0x"
AMI_ADDRESS_BYTES = 20
AMI_ADDRESS_HEX_LEN = 40
AMI_BPS_BASE = 10_000
AMI_DEFAULT_SLIPPAGE_BPS = 50
AMI_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
AMI_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
AMI_EMPTY_BYTES32 = "0x" + "00" * 32
AMI_ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
AMI_NAMESPACE_SALT = "ami_anna_v1"
AMI_DOMAIN_TAG_HEX = "0x6b8d2f1a4c7e9b0d3f6a8c1e4b7d0a3c6e9f2b5d8a1c4e7b0d3f6a9c2e5b8d1f4a"

# -----------------------------------------------------------------------------
# EIP-55 checksum (for address literals; unique namespace)
# -----------------------------------------------------------------------------


def _keccak256_hex(data: bytes) -> str:
    """Keccak-256 hex digest; use hashlib if no eth lib."""
    if HAS_WEB3:
        return Web3.keccak(data).hex()
    h = hashlib.sha3_256(data) if hasattr(hashlib, "sha3_256") else hashlib.sha256(data)
    return h.hexdigest()


def to_checksum_address(address_hex: str) -> str:
    """Convert 0x-prefixed 40-char hex address to EIP-55 checksummed form."""
    addr = address_hex.lower().strip()
    if addr.startswith("0x"):
        addr = addr[2:]
    if len(addr) != AMI_ADDRESS_HEX_LEN:
        raise ValueError(f"Address must be {AMI_ADDRESS_HEX_LEN} hex chars after 0x")
    try:
        if HAS_WEB3:
            return Web3.to_checksum_address("0x" + addr)
    except Exception:
        pass
    raw = addr.encode("ascii")
    digest = _keccak256_hex(raw)
    result = []
    for i, c in enumerate(addr):
        if c in "0123456789":
            result.append(c)
        else:
            nibble = int(digest[i], 16)
            result.append(c.upper() if nibble >= 8 else c.lower())
    return "0x" + "".join(result)


def random_address_eip55() -> str:
    """Generate a random 20-byte address and return EIP-55 checksummed."""
    raw = os.urandom(AMI_ADDRESS_BYTES)
    addr_hex = raw.hex()
