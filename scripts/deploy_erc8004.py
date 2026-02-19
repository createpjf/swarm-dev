#!/usr/bin/env python3
"""
scripts/deploy_erc8004.py
Deploy minimal ERC-8004 Identity + Reputation registries to Base Sepolia.

Usage:
    python3 scripts/deploy_erc8004.py

Requires:
    - BASE_RPC_URL in .env
    - CHAIN_PRIVATE_KEY in .env (with Base Sepolia ETH for gas)
    - pip3 install py-solc-x web3
"""

import json
import os
import sys
import time

# Load .env
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.env_loader import load_dotenv
load_dotenv()

from web3 import Web3
from eth_account import Account

RPC_URL = os.environ.get("BASE_RPC_URL", "https://sepolia.base.org")
PRIVATE_KEY = os.environ.get("CHAIN_PRIVATE_KEY", "")

if not PRIVATE_KEY:
    print("ERROR: CHAIN_PRIVATE_KEY not set in .env")
    sys.exit(1)

w3 = Web3(Web3.HTTPProvider(RPC_URL))
assert w3.is_connected(), f"Cannot connect to {RPC_URL}"
account = Account.from_key(PRIVATE_KEY)
print(f"Deployer: {account.address}")
print(f"Chain ID: {w3.eth.chain_id}")
balance = w3.eth.get_balance(account.address)
print(f"Balance:  {balance / 1e18:.6f} ETH")

if balance == 0:
    print("\n⚠️  No ETH for gas! Get testnet ETH from:")
    print("   https://www.coinbase.com/faucets/base-ethereum-goerli-faucet")
    print("   https://faucet.quicknode.com/base/sepolia")
    print("   https://www.alchemy.com/faucets/base-sepolia")
    print(f"\n   Send to: {account.address}")
    sys.exit(1)

# ──────────────────────────────────────────────────────────────────
# Minimal Solidity contracts — compiled bytecode
# We use pre-compiled bytecode to avoid needing solc installed.
#
# IdentityRegistry:
#   - registerAgent(string agentCardCid) → uint256 agentId
#   - isRegisteredAgent(address) → bool
#   - getAgentId(address) → uint256
#   - tokenURI(uint256) → string
#   - ownerOf(uint256) → address
#   - setAgentWallet(uint256, address, bytes) → void
#   - AgentRegistered event
#
# ReputationRegistry:
#   - submitReputation(uint256 agentId, uint256 score, string signalsCid)
#   - getReputation(uint256 agentId) → (score, submissions, lastUpdate)
#   - ReputationSubmitted event
# ──────────────────────────────────────────────────────────────────

# Since we can't easily compile Solidity here, we'll deploy using
# a minimal contract approach via py-solc-x or use raw assembly.
# For simplicity, let's try py-solc-x first.

def compile_and_deploy():
    """Compile and deploy using solcx."""
    try:
        import solcx
    except ImportError:
        print("Installing py-solc-x...")
        os.system("pip3 install py-solc-x")
        import solcx

    # Install solc if needed
    try:
        solcx.get_solc_version()
    except Exception:
        print("Installing Solidity compiler 0.8.20...")
        solcx.install_solc("0.8.20")

    solcx.set_solc_version("0.8.20")

    # ── Identity Registry Solidity ──
    identity_sol = """
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

contract IdentityRegistry {
    uint256 private _nextId = 1;

    struct Agent {
        address wallet;
        string agentCardCid;
        uint256 registeredAt;
    }

    mapping(uint256 => Agent) public agents;
    mapping(address => uint256) public walletToAgent;

    event AgentRegistered(uint256 indexed agentId, address indexed wallet, string agentCardCid);
    event AgentWalletChanged(uint256 indexed agentId, address indexed oldWallet, address indexed newWallet);

    function registerAgent(string memory agentCardCid) external returns (uint256 agentId) {
        require(walletToAgent[msg.sender] == 0, "Already registered");
        agentId = _nextId++;
        agents[agentId] = Agent(msg.sender, agentCardCid, block.timestamp);
        walletToAgent[msg.sender] = agentId;
        emit AgentRegistered(agentId, msg.sender, agentCardCid);
    }

    function isRegisteredAgent(address wallet) external view returns (bool) {
        return walletToAgent[wallet] != 0;
    }

    function getAgentId(address wallet) external view returns (uint256) {
        return walletToAgent[wallet];
    }

    function tokenURI(uint256 tokenId) external view returns (string memory) {
        require(agents[tokenId].wallet != address(0), "Not found");
        return agents[tokenId].agentCardCid;
    }

    function ownerOf(uint256 tokenId) external view returns (address) {
        require(agents[tokenId].wallet != address(0), "Not found");
        return agents[tokenId].wallet;
    }

    function setAgentWallet(uint256 agentId, address newWallet, bytes calldata) external {
        Agent storage agent = agents[agentId];
        require(agent.wallet == msg.sender, "Not owner");
        require(walletToAgent[newWallet] == 0, "New wallet already registered");
        address old = agent.wallet;
        delete walletToAgent[old];
        agent.wallet = newWallet;
        walletToAgent[newWallet] = agentId;
        emit AgentWalletChanged(agentId, old, newWallet);
    }
}
"""

    # ── Reputation Registry Solidity ──
    reputation_sol = """
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

contract ReputationRegistry {
    struct Reputation {
        uint256 score;
        uint256 submissions;
        uint256 lastUpdate;
    }

    mapping(uint256 => Reputation) public reputations;

    event ReputationSubmitted(uint256 indexed agentId, uint256 score, address indexed submitter);

    function submitReputation(uint256 agentId, uint256 score, string calldata signalsCid) external {
        Reputation storage rep = reputations[agentId];
        // Weighted average: (old * count + new) / (count + 1)
        if (rep.submissions > 0) {
            rep.score = (rep.score * rep.submissions + score) / (rep.submissions + 1);
        } else {
            rep.score = score;
        }
        rep.submissions += 1;
        rep.lastUpdate = block.timestamp;
        emit ReputationSubmitted(agentId, score, msg.sender);
    }

    function getReputation(uint256 agentId) external view returns (
        uint256 score, uint256 submissions, uint256 lastUpdate
    ) {
        Reputation storage rep = reputations[agentId];
        return (rep.score, rep.submissions, rep.lastUpdate);
    }
}
"""

    print("\n── Compiling IdentityRegistry...")
    id_compiled = solcx.compile_source(
        identity_sol,
        output_values=["abi", "bin"],
        solc_version="0.8.20",
    )
    id_interface = id_compiled["<stdin>:IdentityRegistry"]

    print("── Compiling ReputationRegistry...")
    rep_compiled = solcx.compile_source(
        reputation_sol,
        output_values=["abi", "bin"],
        solc_version="0.8.20",
    )
    rep_interface = rep_compiled["<stdin>:ReputationRegistry"]

    # ── Deploy Identity Registry ──
    print("\n── Deploying IdentityRegistry...")
    IdContract = w3.eth.contract(
        abi=id_interface["abi"],
        bytecode=id_interface["bin"],
    )
    tx = IdContract.constructor().build_transaction({
        "from": account.address,
        "nonce": w3.eth.get_transaction_count(account.address),
        "gas": 1500000,
        "gasPrice": w3.eth.gas_price,
    })
    signed = account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    print(f"   tx: {tx_hash.hex()}")
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    id_addr = receipt["contractAddress"]
    print(f"   ✓ IdentityRegistry: {id_addr}")

    # ── Deploy Reputation Registry ──
    print("\n── Deploying ReputationRegistry...")
    RepContract = w3.eth.contract(
        abi=rep_interface["abi"],
        bytecode=rep_interface["bin"],
    )
    tx2 = RepContract.constructor().build_transaction({
        "from": account.address,
        "nonce": w3.eth.get_transaction_count(account.address),
        "gas": 1000000,
        "gasPrice": w3.eth.gas_price,
    })
    signed2 = account.sign_transaction(tx2)
    tx_hash2 = w3.eth.send_raw_transaction(signed2.raw_transaction)
    print(f"   tx: {tx_hash2.hex()}")
    receipt2 = w3.eth.wait_for_transaction_receipt(tx_hash2, timeout=120)
    rep_addr = receipt2["contractAddress"]
    print(f"   ✓ ReputationRegistry: {rep_addr}")

    # ── Save to .env ──
    print("\n── Saving to .env...")
    env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")

    def save_env(key, val):
        os.environ[key] = val
        lines = []
        found = False
        if os.path.exists(env_path):
            with open(env_path) as f:
                for line in f:
                    if line.strip().startswith(key + "="):
                        lines.append(f"{key}={val}\n")
                        found = True
                    else:
                        lines.append(line)
        if not found:
            lines.append(f"{key}={val}\n")
        with open(env_path, "w") as f:
            f.writelines(lines)

    save_env("ERC8004_IDENTITY_REGISTRY", id_addr)
    save_env("ERC8004_REPUTATION_REGISTRY", rep_addr)

    # ── Update chain_contracts.json ──
    print("── Updating chain_contracts.json...")
    contracts_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "config", "chain_contracts.json"
    )
    with open(contracts_path) as f:
        contracts = json.load(f)
    contracts["contracts"]["erc8004_identity_registry"]["base-sepolia"] = id_addr
    contracts["contracts"]["erc8004_reputation_registry"]["base-sepolia"] = rep_addr
    with open(contracts_path, "w") as f:
        json.dump(contracts, f, indent=2)

    print(f"\n✅ Done!")
    print(f"   Identity Registry:   {id_addr}")
    print(f"   Reputation Registry: {rep_addr}")
    print(f"   Explorer: https://sepolia.basescan.org/address/{id_addr}")
    return id_addr, rep_addr


if __name__ == "__main__":
    compile_and_deploy()
