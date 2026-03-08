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
    return to_checksum_address("0x" + addr_hex)


def generate_unique_addresses(count: int = 8) -> List[str]:
    """Generate `count` unique EIP-55 addresses (e.g. for contract deployment)."""
    seen: set = set()
    out: List[str] = []
    while len(out) < count:
        a = random_address_eip55()
        if a not in seen:
            seen.add(a)
            out.append(a)
    return out


# -----------------------------------------------------------------------------
# Logging
# -----------------------------------------------------------------------------

import logging

_logger: Optional[logging.Logger] = None


def get_logger(name: str = "ami") -> logging.Logger:
    global _logger
    if _logger is None:
        _logger = logging.getLogger(name)
        if not _logger.handlers:
            h = logging.StreamHandler(sys.stderr)
            h.setFormatter(logging.Formatter(AMI_LOG_FORMAT, AMI_DATE_FORMAT))
            _logger.addHandler(h)
            _logger.setLevel(logging.INFO)
    return _logger


def set_verbose(verbose: bool) -> None:
    get_logger().setLevel(logging.DEBUG if verbose else logging.INFO)


# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------


@dataclass
class AmiConfig:
    rpc_url: str = AMI_DEFAULT_RPC
    chain_id: int = AMI_CHAIN_ID_MAINNET
    contract_address: Optional[str] = None
    private_key: Optional[str] = None
    gas_limit: int = AMI_GAS_LIMIT_DEFAULT
    gas_multiplier: float = AMI_GAS_MULTIPLIER
    max_fee_per_gas_gwei: Optional[float] = None
    max_priority_fee_gwei: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rpc_url": self.rpc_url,
            "chain_id": self.chain_id,
            "contract_address": self.contract_address,
            "gas_limit": self.gas_limit,
            "gas_multiplier": self.gas_multiplier,
            "max_fee_per_gas_gwei": self.max_fee_per_gas_gwei,
            "max_priority_fee_gwei": self.max_priority_fee_gwei,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> AmiConfig:
        return cls(
            rpc_url=d.get("rpc_url", AMI_DEFAULT_RPC),
            chain_id=int(d.get("chain_id", AMI_CHAIN_ID_MAINNET)),
            contract_address=d.get("contract_address"),
            private_key=d.get("private_key"),
            gas_limit=int(d.get("gas_limit", AMI_GAS_LIMIT_DEFAULT)),
            gas_multiplier=float(d.get("gas_multiplier", AMI_GAS_MULTIPLIER)),
            max_fee_per_gas_gwei=d.get("max_fee_per_gas_gwei"),
            max_priority_fee_gwei=d.get("max_priority_fee_gwei"),
        )

    def save(self, path: Optional[str] = None) -> None:
        path = path or AMI_CONFIG_FILE
        d = self.to_dict()
        if self.private_key:
            d["private_key"] = self.private_key
        Path(os.path.dirname(path)).mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            json.dump(d, f, indent=2)

    @classmethod
    def load(cls, path: Optional[str] = None) -> AmiConfig:
        path = path or AMI_CONFIG_FILE
        if not os.path.isfile(path):
            return cls()
        with open(path) as f:
            return cls.from_dict(json.load(f))


# -----------------------------------------------------------------------------
# Anna ABI (minimal for Ami; extend as needed)
# -----------------------------------------------------------------------------

ANNA_ABI = [
    {"inputs": [], "stateMutability": "nonpayable", "type": "constructor"},
    {"inputs": [], "name": "Anna_ClawDenied", "type": "error"},
    {"inputs": [], "name": "Anna_AllocOverflow", "type": "error"},
    {"inputs": [], "name": "Anna_VaultSweepFailed", "type": "error"},
    {"inputs": [], "name": "Anna_ZeroAmount", "type": "error"},
    {"inputs": [], "name": "Anna_ZeroAddress", "type": "error"},
    {"inputs": [], "name": "Anna_TransferReverted", "type": "error"},
    {"inputs": [], "name": "Anna_RouterReverted", "type": "error"},
    {"inputs": [], "name": "Anna_ClawPaused", "type": "error"},
    {"inputs": [], "name": "Anna_OrderMissing", "type": "error"},
    {"inputs": [], "name": "Anna_OrderAlreadySettled", "type": "error"},
    {"inputs": [], "name": "Anna_OrderCancelled", "type": "error"},
    {"inputs": [], "name": "Anna_VaultInsufficient", "type": "error"},
    {"inputs": [], "name": "Anna_DeadlinePassed", "type": "error"},
    {"inputs": [], "name": "Anna_NotOperator", "type": "error"},
    {"inputs": [], "name": "Anna_NotGovernor", "type": "error"},
    {"inputs": [], "name": "Anna_NotTreasury", "type": "error"},
    {"inputs": [], "name": "Anna_Reentrant", "type": "error"},
    {"inputs": [], "name": "Anna_InvalidStrategyId", "type": "error"},
    {"inputs": [], "name": "Anna_StrategySealed", "type": "error"},
    {"inputs": [], "name": "Anna_WithdrawOverCap", "type": "error"},
    {"inputs": [], "name": "Anna_PositionNotFound", "type": "error"},
    {"inputs": [], "name": "Anna_InvalidRoundId", "type": "error"},
    {"inputs": [], "name": "Anna_RoundNotSealed", "type": "error"},
    {"inputs": [], "name": "Anna_PathLengthInvalid", "type": "error"},
    {"inputs": [], "name": "Anna_InvalidBps", "type": "error"},
    {"inputs": [{"name": "allocId", "type": "uint256"}, {"name": "beneficiary", "type": "address"}, {"name": "amountWei", "type": "uint256"}, {"name": "strategyId", "type": "uint256"}, {"name": "atBlock", "type": "uint40"}], "name": "ClawAllocation", "type": "event"},
    {"inputs": [{"name": "orderId", "type": "uint256"}, {"name": "tokenIn", "type": "address"}, {"name": "tokenOut", "type": "address"}, {"name": "amountIn", "type": "uint256"}, {"name": "amountOutMin", "type": "uint256"}, {"name": "deadline", "type": "uint256"}], "name": "OrderQueued", "type": "event"},
    {"inputs": [{"name": "orderId", "type": "uint256"}, {"name": "amountOut", "type": "uint256"}, {"name": "filledAtBlock", "type": "uint256"}], "name": "OrderFilled", "type": "event"},
    {"inputs": [{"name": "orderId", "type": "uint256"}, {"name": "atBlock", "type": "uint256"}], "name": "OrderCancelled", "type": "event"},
    {"inputs": [{"name": "from", "type": "address"}, {"name": "amountWei", "type": "uint256"}], "name": "TreasuryTopped", "type": "event"},
    {"inputs": [{"name": "user", "type": "address"}, {"name": "positionId", "type": "uint256"}, {"name": "sizeWei", "type": "uint256"}, {"name": "strategyId", "type": "uint256"}], "name": "PositionOpened", "type": "event"},
    {"inputs": [{"name": "user", "type": "address"}, {"name": "positionId", "type": "uint256"}, {"name": "realisedWei", "type": "uint256"}], "name": "PositionClosed", "type": "event"},
    {"inputs": [{"name": "from", "type": "address"}, {"name": "amount", "type": "uint256"}], "name": "StakeDeposited", "type": "event"},
    {"inputs": [], "name": "governor", "outputs": [{"name": "", "type": "address"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "treasury", "outputs": [{"name": "", "type": "address"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "vault", "outputs": [{"name": "", "type": "address"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "operator", "outputs": [{"name": "", "type": "address"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "router", "outputs": [{"name": "", "type": "address"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "clawPaused", "outputs": [{"name": "", "type": "bool"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "orderCounter", "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "positionCounter", "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "genesisBlock", "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "orderId", "type": "uint256"}], "name": "getOrder", "outputs": [{"name": "tokenIn", "type": "address"}, {"name": "tokenOut", "type": "address"}, {"name": "amountIn", "type": "uint256"}, {"name": "amountOutMin", "type": "uint256"}, {"name": "deadline", "type": "uint256"}, {"name": "filled", "type": "bool"}, {"name": "cancelled", "type": "bool"}, {"name": "placedAtBlock", "type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "positionId", "type": "uint256"}], "name": "getPosition", "outputs": [{"name": "user", "type": "address"}, {"name": "strategyId", "type": "uint256"}, {"name": "sizeWei", "type": "uint256"}, {"name": "openedAtBlock", "type": "uint256"}, {"name": "entryPriceE8", "type": "uint256"}, {"name": "closed", "type": "bool"}, {"name": "realisedWei", "type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "strategyId", "type": "uint256"}], "name": "getStrategy", "outputs": [{"name": "allocCapWei", "type": "uint256"}, {"name": "allocUsedWei", "type": "uint256"}, {"name": "tickEpoch", "type": "uint256"}, {"name": "lastTickBlock", "type": "uint256"}, {"name": "sealed", "type": "bool"}, {"name": "active", "type": "bool"}, {"name": "confidenceTier", "type": "uint8"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "getOrderCount", "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "getTotalStakedWei", "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "user", "type": "address"}], "name": "getUserStakeWei", "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "user", "type": "address"}], "name": "getUserPositionCount", "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "tokenIn", "type": "address"}, {"name": "tokenOut", "type": "address"}, {"name": "amountIn", "type": "uint256"}, {"name": "amountOutMin", "type": "uint256"}, {"name": "deadline", "type": "uint256"}], "name": "placeOrder", "outputs": [{"name": "orderId", "type": "uint256"}], "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"name": "orderId", "type": "uint256"}], "name": "executeOrder", "outputs": [{"name": "amountOut", "type": "uint256"}], "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"name": "orderId", "type": "uint256"}], "name": "cancelOrder", "outputs": [], "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [], "name": "topTreasury", "outputs": [], "stateMutability": "payable", "type": "function"},
    {"inputs": [], "name": "depositStake", "outputs": [], "stateMutability": "payable", "type": "function"},
    {"inputs": [{"name": "amountWei", "type": "uint256"}], "name": "requestWithdrawStake", "outputs": [], "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"name": "strategyId", "type": "uint256"}, {"name": "sizeWei", "type": "uint256"}], "name": "openPosition", "outputs": [{"name": "positionId", "type": "uint256"}], "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"name": "positionId", "type": "uint256"}, {"name": "realisedWei", "type": "uint256"}], "name": "closePosition", "outputs": [], "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [], "name": "recordDeposit", "outputs": [{"name": "depositId", "type": "uint256"}], "stateMutability": "payable", "type": "function"},
    {"inputs": [], "name": "getContractBalance", "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "getVaultBalance", "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "roundId", "type": "uint256"}], "name": "getRound", "outputs": [{"name": "promptDigest", "type": "bytes32"}, {"name": "responseRoot", "type": "bytes32"}, {"name": "startedAt", "type": "uint256"}, {"name": "sealedAt", "type": "uint256"}, {"name": "finalized", "type": "bool"}, {"name": "confidenceTier", "type": "uint8"}, {"name": "proposer", "type": "address"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "getRoundCounter", "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"},
]


# -----------------------------------------------------------------------------
# Web3 / Contract client
# -----------------------------------------------------------------------------


class AmiContractClient:
    """Client for Anna contract; uses Web3 when available."""

    def __init__(self, config: AmiConfig) -> None:
        self.config = config
        self._w3: Any = None
        self._contract: Any = None
        self._account: Any = None
        if not HAS_WEB3:
            get_logger().warning("web3 not installed; only offline/address utilities available.")

    def connect(self) -> bool:
        if not HAS_WEB3:
            return False
        try:
            self._w3 = Web3(Web3.HTTPProvider(self.config.rpc_url))
            if not self._w3.is_connected():
                get_logger().error("RPC not connected: %s", self.config.rpc_url)
                return False
            if self.config.contract_address:
                self._contract = self._w3.eth.contract(
                    address=Web3.to_checksum_address(self.config.contract_address),
                    abi=ANNA_ABI,
                )
            if self.config.private_key:
                self._account = self._w3.eth.account.from_key(self.config.private_key)
            return True
        except Exception as e:
            get_logger().exception("Connect failed: %s", e)
            return False

    @property
    def w3(self) -> Any:
        return self._w3

    @property
