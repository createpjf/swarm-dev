#!/usr/bin/env python3
"""
scripts/mint_naga_pkp.py
Mint a Lit Protocol PKP on Naga network for a Cleo agent.

Usage:
    python scripts/mint_naga_pkp.py                  # mint for all agents
    python scripts/mint_naga_pkp.py leo              # mint for specific agent
    python scripts/mint_naga_pkp.py --network naga   # use mainnet

Requires:
    pip install lit-python-sdk eth-account
    CHAIN_PRIVATE_KEY in .env (operator wallet for gas)
"""

from __future__ import annotations
import json
import os
import sys
import time

# Add project root to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from core.env_loader import load_dotenv

load_dotenv()

from lit_python_sdk import LitClient
from eth_account import Account


def _save_env_var(key: str, value: str, env_path: str = ".env"):
    """Save a key=value to .env file (create or update)."""
    os.environ[key] = value
    lines = []
    found = False
    if os.path.exists(env_path):
        with open(env_path, "r") as f:
            for line in f:
                stripped = line.strip()
                if stripped and not stripped.startswith("#") and "=" in stripped:
                    k = stripped.split("=", 1)[0].strip()
                    if k == key:
                        lines.append(f"{key}={value}\n")
                        found = True
                        continue
                lines.append(line)
    if not found:
        lines.append(f"{key}={value}\n")
    with open(env_path, "w") as f:
        f.writelines(lines)


def mint_pkp_for_agent(agent_id: str, network: str = "naga-dev") -> dict:
    """Mint a new PKP on Lit Naga network for the given agent."""
    private_key = os.environ.get("CHAIN_PRIVATE_KEY", "")
    if not private_key:
        print("[ERROR] CHAIN_PRIVATE_KEY not set in .env — needed for gas fees")
        print("  Generate one: python -c \"from eth_account import Account; a=Account.create(); print(a.key.hex())\"")
        sys.exit(1)

    print(f"\n{'='*60}")
    print(f"  Minting PKP for agent: {agent_id}")
    print(f"  Network: {network}")
    print(f"{'='*60}\n")

    # Step 1: Initialize Lit client
    print("[1/4] Initializing Lit client...")
    client = LitClient()
    client.new(lit_network=network, debug=False)

    print("[2/4] Connecting to Lit Naga network...")
    client.connect()

    # Step 2: Initialize contracts client for minting
    print("[3/4] Initializing contracts client...")
    client.new_lit_contracts_client(
        private_key=private_key,
        network=network,
        debug=False,
    )

    # Step 3: Mint PKP with eth wallet auth
    print("[4/4] Minting PKP...")
    auth_method = {
        "authMethodType": 1,  # EthWallet
        "accessToken": "",    # Will be populated by the SDK
    }
    mint_result = client.mint_with_auth(
        auth_method=auth_method,
        scopes=[1, 2],  # SignAnything, PersonalSign
    )

    pkp_info = {
        "agent_id": agent_id,
        "token_id": mint_result.get("tokenId", mint_result.get("pkp", {}).get("tokenId", "")),
        "public_key": mint_result.get("publicKey", mint_result.get("pkp", {}).get("publicKey", "")),
        "eth_address": mint_result.get("ethAddress", mint_result.get("pkp", {}).get("ethAddress", "")),
        "network": network,
        "minted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    # Save to .env
    prefix = agent_id.upper()
    _save_env_var(f"{prefix}_PKP_TOKEN_ID", pkp_info["token_id"])
    _save_env_var(f"{prefix}_PKP_PUBLIC_KEY", pkp_info["public_key"])
    _save_env_var(f"{prefix}_PKP_ETH_ADDRESS", pkp_info["eth_address"])
    _save_env_var("LIT_NETWORK", network)

    print(f"\n  PKP Minted Successfully!")
    print(f"  Token ID:    {pkp_info['token_id']}")
    print(f"  Public Key:  {pkp_info['public_key'][:20]}...{pkp_info['public_key'][-8:]}")
    print(f"  ETH Address: {pkp_info['eth_address']}")
    print(f"  Saved to .env as {prefix}_PKP_*")

    # Disconnect
    client.disconnect()
    return pkp_info


def main():
    import yaml

    network = "naga-dev"

    # Parse args
    agent_ids = []
    for arg in sys.argv[1:]:
        if arg.startswith("--network="):
            network = arg.split("=", 1)[1]
        elif arg == "--network":
            continue  # next arg is value
        elif sys.argv[sys.argv.index(arg) - 1] == "--network" if sys.argv.index(arg) > 0 else False:
            network = arg
        else:
            agent_ids.append(arg)

    # If no agents specified, read from config
    if not agent_ids:
        config_path = "config/agents.yaml"
        if os.path.exists(config_path):
            with open(config_path) as f:
                cfg = yaml.safe_load(f) or {}
            agent_ids = [a["id"] for a in cfg.get("agents", [])]
        if not agent_ids:
            print("[ERROR] No agent IDs specified and no config/agents.yaml found")
            sys.exit(1)
        print(f"Found agents from config: {agent_ids}")

    results = []
    for agent_id in agent_ids:
        try:
            info = mint_pkp_for_agent(agent_id, network=network)
            results.append(info)
        except Exception as e:
            print(f"\n[ERROR] Failed to mint PKP for {agent_id}: {e}")
            import traceback
            traceback.print_exc()

    # Summary
    print(f"\n{'='*60}")
    print(f"  MINT SUMMARY")
    print(f"{'='*60}")
    for r in results:
        print(f"  {r['agent_id']:12s}  {r['eth_address']}  {r['network']}")
    print(f"\n  Total: {len(results)}/{len(agent_ids)} minted")

    # Save full results to JSON for backup
    backup_path = f"memory/pkp_mint_backup_{int(time.time())}.json"
    os.makedirs("memory", exist_ok=True)
    with open(backup_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"  Backup saved to: {backup_path}")
    print(f"\n  IMPORTANT: Backup the .env file and {backup_path}")


if __name__ == "__main__":
    main()
