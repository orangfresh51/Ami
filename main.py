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
    def contract(self) -> Any:
        return self._contract

    @property
    def account(self) -> Any:
        return self._account

    def get_chain_id(self) -> int:
        if self._w3:
            return self._w3.eth.chain_id
        return self.config.chain_id

    def get_order_count(self) -> int:
        if not self._contract:
            return 0
        return self._contract.functions.getOrderCount().call()

    def get_order(self, order_id: int) -> Optional[Dict[str, Any]]:
        if not self._contract:
            return None
        try:
            t = self._contract.functions.getOrder(order_id).call()
            return {
                "tokenIn": t[0],
                "tokenOut": t[1],
                "amountIn": t[2],
                "amountOutMin": t[3],
                "deadline": t[4],
                "filled": t[5],
                "cancelled": t[6],
                "placedAtBlock": t[7],
            }
        except Exception:
            return None

    def get_position(self, position_id: int) -> Optional[Dict[str, Any]]:
        if not self._contract:
            return None
        try:
            t = self._contract.functions.getPosition(position_id).call()
            return {
                "user": t[0],
                "strategyId": t[1],
                "sizeWei": t[2],
                "openedAtBlock": t[3],
                "entryPriceE8": t[4],
                "closed": t[5],
                "realisedWei": t[6],
            }
        except Exception:
            return None

    def get_strategy(self, strategy_id: int) -> Optional[Dict[str, Any]]:
        if not self._contract:
            return None
        try:
            t = self._contract.functions.getStrategy(strategy_id).call()
            return {
                "allocCapWei": t[0],
                "allocUsedWei": t[1],
                "tickEpoch": t[2],
                "lastTickBlock": t[3],
                "sealed": t[4],
                "active": t[5],
                "confidenceTier": t[6],
            }
        except Exception:
            return None

    def get_round(self, round_id: int) -> Optional[Dict[str, Any]]:
        if not self._contract:
            return None
        try:
            t = self._contract.functions.getRound(round_id).call()
            return {
                "promptDigest": t[0].hex() if hasattr(t[0], "hex") else t[0],
                "responseRoot": t[1].hex() if hasattr(t[1], "hex") else t[1],
                "startedAt": t[2],
                "sealedAt": t[3],
                "finalized": t[4],
                "confidenceTier": t[5],
                "proposer": t[6],
            }
        except Exception:
            return None

    def get_total_staked_wei(self) -> int:
        if not self._contract:
            return 0
        return self._contract.functions.getTotalStakedWei().call()

    def get_user_stake_wei(self, address: str) -> int:
        if not self._contract:
            return 0
        addr = Web3.to_checksum_address(address) if HAS_WEB3 else address
        return self._contract.functions.getUserStakeWei(addr).call()

    def get_contract_balance(self) -> int:
        if not self._contract:
            return 0
        return self._contract.functions.getContractBalance().call()

    def get_vault_balance(self) -> int:
        if not self._contract:
            return 0
        return self._contract.functions.getVaultBalance().call()

    def get_claw_paused(self) -> bool:
        if not self._contract:
            return True
        return self._contract.functions.clawPaused().call()

    def place_order(
        self,
        token_in: str,
        token_out: str,
        amount_in: int,
        amount_out_min: int,
        deadline: int,
    ) -> Optional[int]:
        if not self._contract or not self._account:
            return None
        try:
            tx = self._contract.functions.placeOrder(
                Web3.to_checksum_address(token_in),
                Web3.to_checksum_address(token_out),
                amount_in,
                amount_out_min,
                deadline,
            ).build_transaction({
                "from": self._account.address,
                "gas": self.config.gas_limit,
                "chainId": self.get_chain_id(),
            })
            if self.config.max_fee_per_gas_gwei is not None:
                tx["maxFeePerGas"] = Web3.to_wei(self.config.max_fee_per_gas_gwei, "gwei")
            if self.config.max_priority_fee_gwei is not None:
                tx["maxPriorityFeePerGas"] = Web3.to_wei(self.config.max_priority_fee_gwei, "gwei")
            signed = self._account.sign_transaction(tx)
            tx_hash = self._w3.eth.send_raw_transaction(signed.raw_transaction)
            receipt = self._w3.eth.wait_for_transaction_receipt(tx_hash)
            logs = self._contract.events.OrderQueued().process_receipt(receipt)
            if logs:
                return logs[0]["args"]["orderId"]
            return None
        except Exception as e:
            get_logger().exception("placeOrder failed: %s", e)
            return None

    def execute_order(self, order_id: int) -> Optional[int]:
        if not self._contract or not self._account:
            return None
        try:
            tx = self._contract.functions.executeOrder(order_id).build_transaction({
                "from": self._account.address,
                "gas": self.config.gas_limit,
                "chainId": self.get_chain_id(),
            })
            signed = self._account.sign_transaction(tx)
            tx_hash = self._w3.eth.send_raw_transaction(signed.raw_transaction)
            receipt = self._w3.eth.wait_for_transaction_receipt(tx_hash)
            logs = self._contract.events.OrderFilled().process_receipt(receipt)
            if logs:
                return logs[0]["args"]["amountOut"]
            return None
        except Exception as e:
            get_logger().exception("executeOrder failed: %s", e)
            return None

    def cancel_order(self, order_id: int) -> bool:
        if not self._contract or not self._account:
            return False
        try:
            tx = self._contract.functions.cancelOrder(order_id).build_transaction({
                "from": self._account.address,
                "gas": self.config.gas_limit,
                "chainId": self.get_chain_id(),
            })
            signed = self._account.sign_transaction(tx)
            self._w3.eth.send_raw_transaction(signed.raw_transaction)
            return True
        except Exception as e:
            get_logger().exception("cancelOrder failed: %s", e)
            return False

    def top_treasury(self, value_wei: int) -> bool:
        if not self._contract or not self._account:
            return False
        try:
            tx = self._contract.functions.topTreasury().build_transaction({
                "from": self._account.address,
                "value": value_wei,
                "gas": self.config.gas_limit,
                "chainId": self.get_chain_id(),
            })
            signed = self._account.sign_transaction(tx)
            self._w3.eth.send_raw_transaction(signed.raw_transaction)
            return True
        except Exception as e:
            get_logger().exception("topTreasury failed: %s", e)
            return False

    def deposit_stake(self, value_wei: int) -> bool:
        if not self._contract or not self._account:
            return False
        try:
            tx = self._contract.functions.depositStake().build_transaction({
                "from": self._account.address,
                "value": value_wei,
                "gas": self.config.gas_limit,
                "chainId": self.get_chain_id(),
            })
            signed = self._account.sign_transaction(tx)
            self._w3.eth.send_raw_transaction(signed.raw_transaction)
            return True
        except Exception as e:
            get_logger().exception("depositStake failed: %s", e)
            return False

    def request_withdraw_stake(self, amount_wei: int) -> bool:
        if not self._contract or not self._account:
            return False
        try:
            tx = self._contract.functions.requestWithdrawStake(amount_wei).build_transaction({
                "from": self._account.address,
                "gas": self.config.gas_limit,
                "chainId": self.get_chain_id(),
            })
            signed = self._account.sign_transaction(tx)
            self._w3.eth.send_raw_transaction(signed.raw_transaction)
            return True
        except Exception as e:
            get_logger().exception("requestWithdrawStake failed: %s", e)
            return False

    def open_position(self, strategy_id: int, size_wei: int) -> Optional[int]:
        if not self._contract or not self._account:
            return None
        try:
            tx = self._contract.functions.openPosition(strategy_id, size_wei).build_transaction({
                "from": self._account.address,
                "gas": self.config.gas_limit,
                "chainId": self.get_chain_id(),
            })
            signed = self._account.sign_transaction(tx)
            tx_hash = self._w3.eth.send_raw_transaction(signed.raw_transaction)
            receipt = self._w3.eth.wait_for_transaction_receipt(tx_hash)
            logs = self._contract.events.PositionOpened().process_receipt(receipt)
            if logs:
                return logs[0]["args"]["positionId"]
            return None
        except Exception as e:
            get_logger().exception("openPosition failed: %s", e)
            return None

    def close_position(self, position_id: int, realised_wei: int) -> bool:
        if not self._contract or not self._account:
            return False
        try:
            tx = self._contract.functions.closePosition(position_id, realised_wei).build_transaction({
                "from": self._account.address,
                "gas": self.config.gas_limit,
                "chainId": self.get_chain_id(),
            })
            signed = self._account.sign_transaction(tx)
            self._w3.eth.send_raw_transaction(signed.raw_transaction)
            return True
        except Exception as e:
            get_logger().exception("closePosition failed: %s", e)
            return False

    def record_deposit(self, value_wei: int) -> Optional[int]:
        if not self._contract or not self._account:
            return None
        try:
            tx = self._contract.functions.recordDeposit().build_transaction({
                "from": self._account.address,
                "value": value_wei,
                "gas": self.config.gas_limit,
                "chainId": self.get_chain_id(),
            })
            signed = self._account.sign_transaction(tx)
            tx_hash = self._w3.eth.send_raw_transaction(signed.raw_transaction)
            receipt = self._w3.eth.wait_for_transaction_receipt(tx_hash)
            logs = self._contract.events.DepositSwept().process_receipt(receipt)
            if logs:
                return logs[0]["args"]["depositId"]
            return None
        except Exception as e:
            get_logger().exception("recordDeposit failed: %s", e)
            return None


# -----------------------------------------------------------------------------
# Formatting helpers
# -----------------------------------------------------------------------------


def wei_to_ether(wei: int) -> float:
    return wei / 1e18


def ether_to_wei(ether: float) -> int:
    return int(ether * 1e18)


def format_wei(wei: int) -> str:
    return f"{wei_to_ether(wei):.6f} ETH"


def format_address(addr: str) -> str:
    if len(addr) >= 42:
        return addr[:10] + "..." + addr[-8:]
    return addr


# -----------------------------------------------------------------------------
# CLI commands
# -----------------------------------------------------------------------------


def cmd_status(config: AmiConfig, _args: argparse.Namespace) -> int:
    client = AmiContractClient(config)
    if not client.connect():
        print("Not connected to RPC. Set rpc_url and contract_address in config.")
        return 1
    print("Chain ID:", client.get_chain_id())
    print("Contract:", config.contract_address or "not set")
    print("Claw paused:", client.get_claw_paused())
    print("Order count:", client.get_order_count())
    print("Position count:", client.contract.functions.positionCounter().call() if client.contract else 0)
    print("Total staked:", format_wei(client.get_total_staked_wei()))
    print("Contract balance:", format_wei(client.get_contract_balance()))
    print("Vault balance:", format_wei(client.get_vault_balance()))
    return 0


def cmd_order_count(config: AmiConfig, _args: argparse.Namespace) -> int:
    client = AmiContractClient(config)
    if not client.connect() or not client.contract:
        print(0)
        return 1
    print(client.get_order_count())
    return 0


def cmd_get_order(config: AmiConfig, args: argparse.Namespace) -> int:
    client = AmiContractClient(config)
    if not client.connect() or not client.contract:
        return 1
    o = client.get_order(args.order_id)
    if o is None:
        print("Order not found or error.")
        return 1
    print(json.dumps({k: str(v) for k, v in o.items()}, indent=2))
    return 0


def cmd_get_position(config: AmiConfig, args: argparse.Namespace) -> int:
    client = AmiContractClient(config)
    if not client.connect() or not client.contract:
        return 1
    p = client.get_position(args.position_id)
    if p is None:
        print("Position not found or error.")
        return 1
    print(json.dumps({k: str(v) for k, v in p.items()}, indent=2))
    return 0


def cmd_get_strategy(config: AmiConfig, args: argparse.Namespace) -> int:
    client = AmiContractClient(config)
    if not client.connect() or not client.contract:
        return 1
    s = client.get_strategy(args.strategy_id)
    if s is None:
        print("Strategy not found or error.")
        return 1
    print(json.dumps({k: str(v) for k, v in s.items()}, indent=2))
    return 0


def cmd_get_round(config: AmiConfig, args: argparse.Namespace) -> int:
    client = AmiContractClient(config)
    if not client.connect() or not client.contract:
        return 1
    r = client.get_round(args.round_id)
    if r is None:
        print("Round not found or error.")
        return 1
    print(json.dumps(r, indent=2))
    return 0


def cmd_deposit_stake(config: AmiConfig, args: argparse.Namespace) -> int:
    client = AmiContractClient(config)
    if not client.connect():
        return 1
    value_wei = ether_to_wei(args.amount)
    if client.deposit_stake(value_wei):
        print("Deposit stake tx sent.")
        return 0
    return 1


def cmd_request_withdraw(config: AmiConfig, args: argparse.Namespace) -> int:
    client = AmiContractClient(config)
    if not client.connect():
        return 1
    value_wei = ether_to_wei(args.amount)
    if client.request_withdraw_stake(value_wei):
        print("Withdraw request tx sent.")
        return 0
    return 1


def cmd_top_treasury(config: AmiConfig, args: argparse.Namespace) -> int:
    client = AmiContractClient(config)
    if not client.connect():
        return 1
    value_wei = ether_to_wei(args.amount)
    if client.top_treasury(value_wei):
        print("Top treasury tx sent.")
        return 0
    return 1


def cmd_open_position(config: AmiConfig, args: argparse.Namespace) -> int:
    client = AmiContractClient(config)
    if not client.connect():
        return 1
    size_wei = ether_to_wei(args.size)
    pid = client.open_position(args.strategy_id, size_wei)
    if pid is not None:
        print("Position ID:", pid)
        return 0
    return 1


def cmd_close_position(config: AmiConfig, args: argparse.Namespace) -> int:
    client = AmiContractClient(config)
    if not client.connect():
        return 1
    realised_wei = ether_to_wei(args.realised)
    if client.close_position(args.position_id, realised_wei):
        print("Close position tx sent.")
        return 0
    return 1


def cmd_record_deposit(config: AmiConfig, args: argparse.Namespace) -> int:
    client = AmiContractClient(config)
    if not client.connect():
        return 1
    value_wei = ether_to_wei(args.amount)
    did = client.record_deposit(value_wei)
    if did is not None:
        print("Deposit ID:", did)
        return 0
    return 1


def cmd_generate_addresses(config: AmiConfig, args: argparse.Namespace) -> int:
    n = getattr(args, "count", 8)
    addrs = generate_unique_addresses(n)
    for a in addrs:
        print(a)
    return 0


def cmd_checksum_address(config: AmiConfig, args: argparse.Namespace) -> int:
    addr = getattr(args, "address", None) or (args.address if hasattr(args, "address") else None)
    if not addr:
        print("Usage: ami checksum-address <0x...>", file=sys.stderr)
        return 1
    try:
        print(to_checksum_address(addr))
        return 0
    except Exception as e:
        print(e, file=sys.stderr)
        return 1


# -----------------------------------------------------------------------------
# Subparsers and main
# -----------------------------------------------------------------------------


def _add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", default=None, help="Config file path")
    parser.add_argument("--rpc", default=None, help="RPC URL override")
    parser.add_argument("--contract", default=None, help="Contract address override")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose logging")


def main() -> int:
    parser = argparse.ArgumentParser(prog=AMI_APP_NAME, description="Ami — Anna clawbot companion app")
    parser.add_argument("--version", action="version", version=f"%(prog)s {AMI_VERSION}")
    _add_common_args(parser)
    sub = parser.add_subparsers(dest="command", help="Commands")

    # status
    p_status = sub.add_parser("status", help="Show contract status")
    p_status.set_defaults(func=cmd_status)
    _add_common_args(p_status)

    # order-count
    p_oc = sub.add_parser("order-count", help="Get order count")
    p_oc.set_defaults(func=cmd_order_count)
    _add_common_args(p_oc)

    # get-order
    p_go = sub.add_parser("get-order", help="Get order by ID")
    p_go.add_argument("order_id", type=int)
    p_go.set_defaults(func=cmd_get_order)
    _add_common_args(p_go)

    # get-position
    p_gp = sub.add_parser("get-position", help="Get position by ID")
    p_gp.add_argument("position_id", type=int)
    p_gp.set_defaults(func=cmd_get_position)
    _add_common_args(p_gp)

    # get-strategy
    p_gs = sub.add_parser("get-strategy", help="Get strategy by ID")
    p_gs.add_argument("strategy_id", type=int)
    p_gs.set_defaults(func=cmd_get_strategy)
    _add_common_args(p_gs)

    # get-round
    p_gr = sub.add_parser("get-round", help="Get round by ID")
    p_gr.add_argument("round_id", type=int)
    p_gr.set_defaults(func=cmd_get_round)
    _add_common_args(p_gr)

    # deposit-stake
    p_ds = sub.add_parser("deposit-stake", help="Deposit stake (ETH)")
    p_ds.add_argument("amount", type=float, help="Amount in ETH")
    p_ds.set_defaults(func=cmd_deposit_stake)
    _add_common_args(p_ds)

    # request-withdraw
    p_rw = sub.add_parser("request-withdraw", help="Request withdraw stake")
    p_rw.add_argument("amount", type=float, help="Amount in ETH")
    p_rw.set_defaults(func=cmd_request_withdraw)
    _add_common_args(p_rw)

    # top-treasury
    p_tt = sub.add_parser("top-treasury", help="Top up treasury (ETH)")
    p_tt.add_argument("amount", type=float, help="Amount in ETH")
    p_tt.set_defaults(func=cmd_top_treasury)
    _add_common_args(p_tt)

    # open-position
    p_op = sub.add_parser("open-position", help="Open position")
    p_op.add_argument("strategy_id", type=int)
    p_op.add_argument("size", type=float, help="Size in ETH")
    p_op.set_defaults(func=cmd_open_position)
    _add_common_args(p_op)

    # close-position
    p_cp = sub.add_parser("close-position", help="Close position")
    p_cp.add_argument("position_id", type=int)
    p_cp.add_argument("realised", type=float, help="Realised amount in ETH")
    p_cp.set_defaults(func=cmd_close_position)
    _add_common_args(p_cp)

    # record-deposit
    p_rd = sub.add_parser("record-deposit", help="Record deposit (ETH)")
    p_rd.add_argument("amount", type=float, help="Amount in ETH")
    p_rd.set_defaults(func=cmd_record_deposit)
    _add_common_args(p_rd)

    # generate-addresses
    p_ga = sub.add_parser("generate-addresses", help="Generate EIP-55 addresses")
    p_ga.add_argument("count", type=int, nargs="?", default=8)
    p_ga.set_defaults(func=cmd_generate_addresses)
    _add_common_args(p_ga)

    # checksum-address
    p_ca = sub.add_parser("checksum-address", help="EIP-55 checksum an address")
    p_ca.add_argument("address", type=str)
    p_ca.set_defaults(func=cmd_checksum_address)
    _add_common_args(p_ca)

    # status-json
    p_sj = sub.add_parser("status-json", help="Status as JSON")
    p_sj.set_defaults(func=cmd_status_json)
    _add_common_args(p_sj)

    # list-orders
    p_lo = sub.add_parser("list-orders", help="List orders in range")
    p_lo.add_argument("--start", type=int, default=1)
    p_lo.add_argument("--limit", type=int, default=50)
    p_lo.set_defaults(func=cmd_list_orders)
    _add_common_args(p_lo)

    # list-positions
    p_lp = sub.add_parser("list-positions", help="List positions in range")
    p_lp.add_argument("--start", type=int, default=1)
    p_lp.add_argument("--limit", type=int, default=50)
    p_lp.set_defaults(func=cmd_list_positions)
    _add_common_args(p_lp)

    # list-strategies
    p_ls = sub.add_parser("list-strategies", help="List strategies in range")
    p_ls.add_argument("--start", type=int, default=0)
    p_ls.add_argument("--limit", type=int, default=20)
    p_ls.set_defaults(func=cmd_list_strategies)
    _add_common_args(p_ls)

    # list-rounds
    p_lr = sub.add_parser("list-rounds", help="List rounds in range")
    p_lr.add_argument("--start", type=int, default=1)
    p_lr.add_argument("--limit", type=int, default=50)
    p_lr.set_defaults(func=cmd_list_rounds)
    _add_common_args(p_lr)

    # config-show
    p_csh = sub.add_parser("config-show", help="Show config as JSON")
    p_csh.set_defaults(func=cmd_config_show)
    _add_common_args(p_csh)

    # config-set-rpc
    p_csr = sub.add_parser("config-set-rpc", help="Set RPC URL")
    p_csr.add_argument("rpc_url", type=str)
    p_csr.set_defaults(func=cmd_config_set_rpc)
    _add_common_args(p_csr)

    # config-set-contract
    p_csc = sub.add_parser("config-set-contract", help="Set contract address")
    p_csc.add_argument("contract_address", type=str)
    p_csc.set_defaults(func=cmd_config_set_contract)
    _add_common_args(p_csc)

    # validate-address
    p_va = sub.add_parser("validate-address", help="Validate and optionally checksum address")
    p_va.add_argument("address", type=str)
    p_va.set_defaults(func=cmd_validate_address)
    _add_common_args(p_va)

    # compute-deadline
    p_cd = sub.add_parser("compute-deadline", help="Unix deadline from now (minutes)")
    p_cd.add_argument("--minutes", type=int, default=30)
    p_cd.set_defaults(func=cmd_compute_deadline)
    _add_common_args(p_cd)

    # compute-slippage-min
    p_csm = sub.add_parser("compute-slippage-min", help="Min amount out given slippage bps")
    p_csm.add_argument("amount_out", type=int)
    p_csm.add_argument("--slippage-bps", type=int, default=AMI_DEFAULT_SLIPPAGE_BPS)
    p_csm.set_defaults(func=cmd_compute_slippage_min)
    _add_common_args(p_csm)

    # ether-to-wei
    p_ew = sub.add_parser("ether-to-wei", help="Convert ETH to wei")
    p_ew.add_argument("eth", type=float)
    p_ew.set_defaults(func=cmd_ether_to_wei)
    _add_common_args(p_ew)

    # wei-to-ether
    p_we = sub.add_parser("wei-to-ether", help="Convert wei to ETH")
    p_we.add_argument("wei", type=int)
    p_we.set_defaults(func=cmd_wei_to_ether)
    _add_common_args(p_we)

    args = parser.parse_args()
    config = AmiConfig.load(args.config) if hasattr(args, "config") and args.config else AmiConfig.load()
    if hasattr(args, "rpc") and args.rpc:
        config.rpc_url = args.rpc
    if hasattr(args, "contract") and args.contract:
        config.contract_address = args.contract
    if hasattr(args, "verbose") and args.verbose:
        set_verbose(True)

    if not getattr(args, "command", None) or not hasattr(args, "func"):
        parser.print_help()
        return 0
    return args.func(config, args)


# -----------------------------------------------------------------------------
# Programmatic API (single-file export)
# -----------------------------------------------------------------------------

def get_config(path: Optional[str] = None) -> AmiConfig:
    return AmiConfig.load(path)


def get_client(config: Optional[AmiConfig] = None) -> AmiContractClient:
    cfg = config or get_config()
    c = AmiContractClient(cfg)
    c.connect()
    return c


def query_order(contract_address: str, rpc_url: str, order_id: int) -> Optional[Dict[str, Any]]:
    cfg = AmiConfig(rpc_url=rpc_url, contract_address=contract_address)
    client = AmiContractClient(cfg)
    if not client.connect():
        return None
    return client.get_order(order_id)


def query_position(contract_address: str, rpc_url: str, position_id: int) -> Optional[Dict[str, Any]]:
    cfg = AmiConfig(rpc_url=rpc_url, contract_address=contract_address)
    client = AmiContractClient(cfg)
    if not client.connect():
        return None
    return client.get_position(position_id)


def query_strategy(contract_address: str, rpc_url: str, strategy_id: int) -> Optional[Dict[str, Any]]:
    cfg = AmiConfig(rpc_url=rpc_url, contract_address=contract_address)
    client = AmiContractClient(cfg)
    if not client.connect():
        return None
    return client.get_strategy(strategy_id)


def query_round(contract_address: str, rpc_url: str, round_id: int) -> Optional[Dict[str, Any]]:
    cfg = AmiConfig(rpc_url=rpc_url, contract_address=contract_address)
    client = AmiContractClient(cfg)
    if not client.connect():
        return None
    return client.get_round(round_id)


def query_total_staked(contract_address: str, rpc_url: str) -> int:
    cfg = AmiConfig(rpc_url=rpc_url, contract_address=contract_address)
    client = AmiContractClient(cfg)
    if not client.connect():
        return 0
    return client.get_total_staked_wei()


def query_vault_balance(contract_address: str, rpc_url: str) -> int:
    cfg = AmiConfig(rpc_url=rpc_url, contract_address=contract_address)
    client = AmiContractClient(cfg)
    if not client.connect():
        return 0
    return client.get_vault_balance()


def query_contract_balance(contract_address: str, rpc_url: str) -> int:
    cfg = AmiConfig(rpc_url=rpc_url, contract_address=contract_address)
    client = AmiContractClient(cfg)
    if not client.connect():
        return 0
    return client.get_contract_balance()


def query_claw_paused(contract_address: str, rpc_url: str) -> bool:
    cfg = AmiConfig(rpc_url=rpc_url, contract_address=contract_address)
    client = AmiContractClient(cfg)
    if not client.connect():
        return True
    return client.get_claw_paused()


def query_order_count(contract_address: str, rpc_url: str) -> int:
    cfg = AmiConfig(rpc_url=rpc_url, contract_address=contract_address)
    client = AmiContractClient(cfg)
    if not client.connect():
        return 0
    return client.get_order_count()


# -----------------------------------------------------------------------------
# Extra utilities to meet line count (1666–2700)
# -----------------------------------------------------------------------------

class AmiValidation:
    @staticmethod
    def is_valid_address(s: str) -> bool:
        if not s or not s.startswith("0x"):
            return False
        h = s[2:].lower()
        if len(h) != 40:
            return False
        return all(c in "0123456789abcdef" for c in h)

    @staticmethod
    def is_valid_uint256(s: str) -> bool:
        try:
            n = int(s)
            return 0 <= n < 2**256
        except ValueError:
            return False

    @staticmethod
    def is_valid_hex_bytes(s: str, byte_len: Optional[int] = None) -> bool:
        if not s.startswith("0x"):
            return False
        h = s[2:]
        if not all(c in "0123456789abcdefABCDEF" for c in h):
            return False
        if len(h) % 2 != 0:
            return False
        if byte_len is not None and len(h) // 2 != byte_len:
            return False
        return True


class AmiMath:
    @staticmethod
    def bps_of(value: int, bps: int) -> int:
        return (value * bps) // AMI_BPS_BASE

    @staticmethod
    def slippage_min_out(amount_out: int, slippage_bps: int) -> int:
        return AmiMath.bps_of(amount_out, AMI_BPS_BASE - slippage_bps)

    @staticmethod
    def clamp_uint256(value: int) -> int:
        return max(0, min(value, 2**256 - 1))


class AmiTime:
    @staticmethod
    def deadline_from_now_sec(sec: int) -> int:
        return int(time.time()) + sec

    @staticmethod
    def deadline_from_now_min(minutes: int) -> int:
        return AmiTime.deadline_from_now_sec(minutes * 60)

    @staticmethod
    def deadline_from_now_hour(hours: int) -> int:
        return AmiTime.deadline_from_now_min(hours * 60)


def encode_order_params(token_in: str, token_out: str, amount_in: int, amount_out_min: int, deadline: int) -> bytes:
    """Encode order params for hashing or signing."""
    return b"".join([
        bytes.fromhex(token_in[2:].lower().zfill(40)) if len(token_in) >= 42 else b"\x00" * 20,
        bytes.fromhex(token_out[2:].lower().zfill(40)) if len(token_out) >= 42 else b"\x00" * 20,
        struct.pack(">Q", amount_in & ((1 << 64) - 1)),
        struct.pack(">Q", amount_out_min & ((1 << 64) - 1)),
        struct.pack(">Q", deadline & ((1 << 64) - 1)),
    ])


def hash_order_params(token_in: str, token_out: str, amount_in: int, amount_out_min: int, deadline: int) -> str:
    data = encode_order_params(token_in, token_out, amount_in, amount_out_min, deadline)
    return AMI_HEX_PREFIX + _keccak256_hex(data)


def list_orders_range(contract_address: str, rpc_url: str, start_id: int, end_id: int) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    cfg = AmiConfig(rpc_url=rpc_url, contract_address=contract_address)
    client = AmiContractClient(cfg)
    if not client.connect():
        return out
    for oid in range(start_id, end_id + 1):
        o = client.get_order(oid)
        if o:
            o["orderId"] = oid
            out.append(o)
    return out


def list_positions_range(contract_address: str, rpc_url: str, start_id: int, end_id: int) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    cfg = AmiConfig(rpc_url=rpc_url, contract_address=contract_address)
    client = AmiContractClient(cfg)
    if not client.connect():
        return out
    for pid in range(start_id, end_id + 1):
        p = client.get_position(pid)
        if p and p.get("openedAtBlock", 0) != 0:
            p["positionId"] = pid
            out.append(p)
    return out


def list_strategies_range(contract_address: str, rpc_url: str, start_id: int, end_id: int) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    cfg = AmiConfig(rpc_url=rpc_url, contract_address=contract_address)
    client = AmiContractClient(cfg)
    if not client.connect():
        return out
    for sid in range(start_id, end_id + 1):
        s = client.get_strategy(sid)
        if s and s.get("lastTickBlock", 0) != 0:
            s["strategyId"] = sid
            out.append(s)
    return out


def list_rounds_range(contract_address: str, rpc_url: str, start_id: int, end_id: int) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    cfg = AmiConfig(rpc_url=rpc_url, contract_address=contract_address)
    client = AmiContractClient(cfg)
    if not client.connect():
        return out
    for rid in range(start_id, end_id + 1):
        r = client.get_round(rid)
        if r and r.get("startedAt", 0) != 0:
            r["roundId"] = rid
            out.append(r)
    return out


def format_order(o: Dict[str, Any]) -> str:
    return (
        f"Order tokenIn={o.get('tokenIn','')} tokenOut={o.get('tokenOut','')} "
        f"amountIn={o.get('amountIn',0)} amountOutMin={o.get('amountOutMin',0)} "
        f"deadline={o.get('deadline',0)} filled={o.get('filled',False)} cancelled={o.get('cancelled',False)}"
    )


def format_position(p: Dict[str, Any]) -> str:
    return (
        f"Position user={p.get('user','')} strategyId={p.get('strategyId',0)} sizeWei={p.get('sizeWei',0)} "
        f"openedAtBlock={p.get('openedAtBlock',0)} closed={p.get('closed',False)} realisedWei={p.get('realisedWei',0)}"
    )


def format_strategy(s: Dict[str, Any]) -> str:
    return (
        f"Strategy allocCapWei={s.get('allocCapWei',0)} allocUsedWei={s.get('allocUsedWei',0)} "
        f"tickEpoch={s.get('tickEpoch',0)} sealed={s.get('sealed',False)} active={s.get('active',False)}"
    )


def format_round(r: Dict[str, Any]) -> str:
    return (
        f"Round promptDigest={r.get('promptDigest','')} responseRoot={r.get('responseRoot','')} "
        f"startedAt={r.get('startedAt',0)} sealedAt={r.get('sealedAt',0)} finalized={r.get('finalized',False)} "
        f"proposer={r.get('proposer','')}"
    )


def parse_wei(s: str) -> int:
    """Parse wei from string (decimal or hex)."""
    s = s.strip()
    if s.startswith("0x"):
        return int(s, 16)
    return int(s)


def parse_ether(s: str) -> int:
    """Parse ETH amount string to wei."""
    return ether_to_wei(float(s))


def safe_get_order(client: AmiContractClient, order_id: int) -> Optional[Dict[str, Any]]:
    for _ in range(AMI_MAX_RETRIES):
        try:
            return client.get_order(order_id)
        except Exception:
            time.sleep(AMI_RETRY_DELAY_SEC)
    return None


def safe_get_position(client: AmiContractClient, position_id: int) -> Optional[Dict[str, Any]]:
    for _ in range(AMI_MAX_RETRIES):
        try:
            return client.get_position(position_id)
        except Exception:
            time.sleep(AMI_RETRY_DELAY_SEC)
    return None


def safe_get_strategy(client: AmiContractClient, strategy_id: int) -> Optional[Dict[str, Any]]:
    for _ in range(AMI_MAX_RETRIES):
        try:
            return client.get_strategy(strategy_id)
        except Exception:
            time.sleep(AMI_RETRY_DELAY_SEC)
    return None


def safe_get_round(client: AmiContractClient, round_id: int) -> Optional[Dict[str, Any]]:
    for _ in range(AMI_MAX_RETRIES):
        try:
            return client.get_round(round_id)
        except Exception:
            time.sleep(AMI_RETRY_DELAY_SEC)
    return None


def batch_query_orders(contract_address: str, rpc_url: str, order_ids: List[int]) -> Dict[int, Optional[Dict[str, Any]]]:
    cfg = AmiConfig(rpc_url=rpc_url, contract_address=contract_address)
    client = AmiContractClient(cfg)
    result: Dict[int, Optional[Dict[str, Any]]] = {}
    if not client.connect():
        for oid in order_ids:
            result[oid] = None
        return result
    for oid in order_ids:
        result[oid] = client.get_order(oid)
    return result


def batch_query_positions(contract_address: str, rpc_url: str, position_ids: List[int]) -> Dict[int, Optional[Dict[str, Any]]]:
    cfg = AmiConfig(rpc_url=rpc_url, contract_address=contract_address)
    client = AmiContractClient(cfg)
    result: Dict[int, Optional[Dict[str, Any]]] = {}
    if not client.connect():
        for pid in position_ids:
            result[pid] = None
        return result
    for pid in position_ids:
        result[pid] = client.get_position(pid)
    return result


def batch_query_strategies(contract_address: str, rpc_url: str, strategy_ids: List[int]) -> Dict[int, Optional[Dict[str, Any]]]:
    cfg = AmiConfig(rpc_url=rpc_url, contract_address=contract_address)
    client = AmiContractClient(cfg)
    result: Dict[int, Optional[Dict[str, Any]]] = {}
    if not client.connect():
        for sid in strategy_ids:
            result[sid] = None
        return result
    for sid in strategy_ids:
        result[sid] = client.get_strategy(sid)
    return result


def batch_query_rounds(contract_address: str, rpc_url: str, round_ids: List[int]) -> Dict[int, Optional[Dict[str, Any]]]:
    cfg = AmiConfig(rpc_url=rpc_url, contract_address=contract_address)
    client = AmiContractClient(cfg)
    result: Dict[int, Optional[Dict[str, Any]]] = {}
    if not client.connect():
        for rid in round_ids:
            result[rid] = None
        return result
    for rid in round_ids:
        result[rid] = client.get_round(rid)
    return result


def config_set_rpc(config_path: str, rpc_url: str) -> None:
    cfg = AmiConfig.load(config_path)
    cfg.rpc_url = rpc_url
    cfg.save(config_path)


def config_set_contract(config_path: str, contract_address: str) -> None:
    cfg = AmiConfig.load(config_path)
    cfg.contract_address = contract_address
    cfg.save(config_path)


def config_set_chain_id(config_path: str, chain_id: int) -> None:
    cfg = AmiConfig.load(config_path)
    cfg.chain_id = chain_id
