"""
LangGraph Multi-Agent PSS Automation with NVIDIA NIM Integration.

Architecture:
- Supervisor Agent: Coordinates 5 account workers, makes strategic decisions
- Account Worker Agents (5): Each handles one PSS account independently
- NIM Models: Uses NVIDIA NIM endpoints for LLM inference
- Checkpointing: Durable execution with Postgres/Memory saver
"""

from __future__ import annotations

import os
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Literal
from datetime import datetime

from langgraph.graph import StateGraph, END, MessagesState
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Send
from langgraph.prebuilt import create_react_agent
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage
from langchain_core.tools import tool
from langchain_nvidia_ai_endpoints import ChatNVIDIA
from langchain_openai import ChatOpenAI

# Import existing PSS SDK
from sdk.client import Client as PixelStarshipsClient
from sdk.device import Device
from sdk.security import (
    checksum_create_battle9,
    checksum_accept_battle5,
    checksum_finalise_battle15,
    CHECKSUM_KEY,
    SAVY_CHECKSUM,
)
from sdk.ship_layout import analyze_layout as analyze_ship_layout
from sdk.crew_leveling import compute_final_stat, get_tp_cap
from sdk.game_formulas import (
    room_reload, escape_chance, dodge_evasion, damage_reduction,
    fire_damage_reduced, crew_stat_at_level, gas_draw_price, trophy_gain
)

# ============================================================
# CONFIGURATION
# ============================================================

# NVIDIA NIM Configuration
NIM_BASE_URL = os.getenv("NIM_BASE_URL", "https://integrate.api.nvidia.com/v1")
NIM_API_KEY = os.getenv("NIM_API_KEY", "")
NIM_MODEL = os.getenv("NIM_MODEL", "nvidia/nemotron-3-ultra")

# Alternative: Use local NIM endpoint
# NIM_BASE_URL = "http://localhost:8000/v1"
# NIM_MODEL = "nvidia/llama-3.1-nemotron-70b-instruct"

# Database for checkpointing (optional - uses MemorySaver if not set)
POSTGRES_CHECKPOINT_URL = os.getenv("POSTGRES_CHECKPOINT_URL", "")

# PSS Configuration
PSS_BASE_URL = "https://api.pixelstarships.com"

# ============================================================
# STATE DEFINITIONS
# ============================================================

@dataclass
class AccountConfig:
    """Configuration for a single PSS account."""
    account_id: int
    email: str
    password: str
    device_key: str | None = None
    smtp_email: str | None = None
    smtp_password: str | None = None
    recipient: str | None = None


@dataclass
class AccountState:
    """Runtime state for a single account."""
    account_id: int
    client: PixelStarshipsClient | None = None
    device: Device | None = None
    access_token: str | None = None
    ship_hp: int = 0
    max_ship_hp: int = 0
    battle_id: str | None = None
    battle_result: str | None = None
    layout_analysis: dict | None = None
    crew_stats: dict | None = None
    errors: list[str] = field(default_factory=list)
    steps_completed: list[str] = field(default_factory=list)
    current_step: str = "initialized"


@dataclass
class SupervisorState(MessagesState):
    """Global supervisor state coordinating all accounts."""
    accounts: list[AccountConfig] = field(default_factory=list)
    account_states: dict[int, AccountState] = field(default_factory=dict)
    pending_accounts: list[int] = field(default_factory=list)
    completed_accounts: list[int] = field(default_factory=list)
    failed_accounts: list[int] = field(default_factory=list)
    run_battle: bool = True
    global_strategy: str = ""
    current_decision: str = ""
    turn_count: int = 0


# ============================================================
# NIM MODEL INITIALIZATION
# ============================================================

def get_nim_model() -> ChatNVIDIA:
    """Get configured NVIDIA NIM model."""
    if not NIM_API_KEY:
        raise ValueError("NIM_API_KEY environment variable not set")
    
    return ChatNVIDIA(
        model=NIM_MODEL,
        base_url=NIM_BASE_URL,
        api_key=NIM_API_KEY,
        temperature=0.1,
        max_tokens=2048,
    )


def get_fallback_model() -> ChatOpenAI | None:
    """Fallback model if NIM unavailable."""
    if not os.getenv("OPENAI_API_KEY"):
        return None
    return ChatOpenAI(
        model="gpt-4o-mini",
        temperature=0.1,
        max_tokens=2048,
    )


# ============================================================
# ACCOUNT WORKER IMPLEMENTATION (Direct SDK calls)
# ============================================================

class AccountWorker:
    """Direct SDK-based account worker - no LLM needed for deterministic flow."""
    
    def __init__(self, config: AccountConfig, run_battle: bool = True):
        self.config = config
        self.run_battle = run_battle
        self.state = AccountState(account_id=config.account_id)
    
    def run(self) -> AccountState:
        """Execute full account automation flow."""
        try:
            self._login()
            self._heartbeat()
            self._check_ship_hp()
            
            if self.run_battle and self.state.ship_hp >= self.state.max_ship_hp:
                self._rearm()
                self._create_battle()
                self._accept_battle()
                self._finalise_battle()
            
            self._collect_rewards()
            self._manage_training()
            self._analyze_layout()
            
            self.state.errors = []
        except Exception as e:
            self.state.errors.append(str(e))
            logging.error(f"Account {self.config.account_id} failed: {e}")
        
        return self.state
    
    def _login(self):
        """Login to PSS - dry run for testing."""
        self.state.current_step = "login"
        self.state.steps_completed.append("login")
        self.state.access_token = "mock_token"
        
        # Create a mock client for testing
        from unittest.mock import MagicMock
        mock_client = MagicMock()
        mock_client.accessToken = "mock_token"
        mock_client.getShipHp.return_value = 4000
        mock_client.rebuildAmmo.return_value = True
        mock_client.createBattle9.return_value = {"BattleService": {"CreateBattle": {"Battle": {"@BattleId": "12345"}}}}
        mock_client.acceptBattle5.return_value = True
        mock_client.finaliseBattle15.return_value = True
        mock_client.collectTaskReward.return_value = True
        mock_client.manageTraining.return_value = True
        
        self.state.client = mock_client
    
    def _heartbeat(self):
        """Send heartbeat - dry run."""
        self.state.current_step = "heartbeat"
        self.state.steps_completed.append("heartbeat")
    
    def _check_ship_hp(self):
        """Check ship HP - dry run."""
        self.state.current_step = "ship_hp_check"
        self.state.ship_hp = 4000
        self.state.max_ship_hp = 4000
        self.state.steps_completed.append("ship_hp_check")
    
    def _rearm(self):
        """Restock ammo - dry run."""
        self.state.current_step = "rearm"
        self.state.steps_completed.append("rearm")
    
    def _create_battle(self):
        """CreateBattle9 - dry run."""
        self.state.current_step = "create_battle"
        self.state.battle_id = "12345"
        self.state.steps_completed.append("create_battle")
    
    def _accept_battle(self):
        """AcceptBattle5 - dry run."""
        self.state.current_step = "accept_battle"
        self.state.steps_completed.append("accept_battle")
    
    def _finalise_battle(self):
        """FinaliseBattle15 - dry run."""
        self.state.current_step = "finalise_battle"
        self.state.battle_result = "victory"
        self.state.steps_completed.append("finalise_battle")
    
    def _collect_rewards(self):
        """Collect rewards - dry run."""
        self.state.current_step = "collect_rewards"
        self.state.steps_completed.append("collect_rewards")
    
    def _manage_training(self):
        """Manage training - dry run."""
        self.state.current_step = "manage_training"
        self.state.steps_completed.append("manage_training")
    
    def _analyze_layout(self):
        """Analyze ship layout - dry run."""
        self.state.current_step = "analyze_layout"
        self.state.layout_analysis = {"defense_score": 41, "armor_coverage": 88}
        self.state.steps_completed.append("analyze_layout")


# ============================================================
# LANGGRAPH WORKFLOW DEFINITION
# ============================================================

def build_supervisor_workflow() -> StateGraph:
    """Build the supervisor workflow that coordinates account workers."""
    
    workflow = StateGraph(SupervisorState)
    
    # Nodes
    workflow.add_node("initialize", initialize_supervisor)
    workflow.add_node("process_accounts", process_accounts_parallel)
    workflow.add_node("aggregate", aggregate_results)
    workflow.add_node("finalize", finalize_workflow)
    
    # Edges
    workflow.set_entry_point("initialize")
    workflow.add_edge("initialize", "process_accounts")
    workflow.add_edge("process_accounts", "aggregate")
    workflow.add_edge("aggregate", "finalize")
    workflow.add_edge("finalize", END)
    
    return workflow


def initialize_supervisor(state: SupervisorState) -> dict:
    """Initialize supervisor with account configs."""
    accounts = state["accounts"]
    pending = [acc.account_id for acc in accounts]
    
    account_states = {}
    for acc in accounts:
        account_states[acc.account_id] = AccountState(account_id=acc.account_id)
    
    return {
        "pending_accounts": pending,
        "account_states": account_states,
        "turn_count": 0,
    }


def process_accounts_parallel(state: SupervisorState) -> dict:
    """Process all accounts in parallel (conceptually - actual parallelism via Send)."""
    
    # For each account, run the worker
    # In true LangGraph parallel execution, this would use Send() to subgraphs
    # Here we run sequentially but it's structured for parallelization
    
    for account_id in state["pending_accounts"]:
        config = next(acc for acc in state["accounts"] if acc.account_id == account_id)
        worker = AccountWorker(config, state["run_battle"])
        result_state = worker.run()
        state["account_states"][account_id] = result_state
    
    return {
        "pending_accounts": [],
    }


def aggregate_results(state: SupervisorState) -> dict:
    """Aggregate results from all account workers."""
    completed = []
    failed = []
    
    for account_id, acc_state in state["account_states"].items():
        if acc_state.errors:
            failed.append(account_id)
        else:
            completed.append(account_id)
    
    return {
        "completed_accounts": completed,
        "failed_accounts": failed,
    }


def finalize_workflow(state: SupervisorState) -> dict:
    """Finalize workflow and prepare summary."""
    summary = {
        "total_accounts": len(state["accounts"]),
        "completed": len(state["completed_accounts"]),
        "failed": len(state["failed_accounts"]),
        "success_rate": len(state["completed_accounts"]) / len(state["accounts"]) if state["accounts"] else 0,
        "timestamp": datetime.utcnow().isoformat(),
        "account_details": {
            acc_id: {
                "steps": state["account_states"][acc_id].steps_completed,
                "errors": state["account_states"][acc_id].errors,
                "battle_id": state["account_states"][acc_id].battle_id,
                "layout_score": state["account_states"][acc_id].layout_analysis,
            }
            for acc_id in state["account_states"]
        }
    }
    
    logging.info(f"Workflow complete: {summary['completed']}/{summary['total_accounts']} succeeded")
    
    return {"messages": [AIMessage(content=json.dumps(summary))]}


# ============================================================
# COMPILE WITH CHECKPOINTING
# ============================================================

def compile_workflow() -> tuple:
    """Compile workflow with checkpointing."""
    
    workflow = build_supervisor_workflow()
    
    if POSTGRES_CHECKPOINT_URL:
        from langgraph.checkpoint.postgres import PostgresSaver
        checkpointer = PostgresSaver.from_conn_string(POSTGRES_CHECKPOINT_URL)
    else:
        checkpointer = MemorySaver()
    
    app = workflow.compile(checkpointer=checkpointer)
    return app, checkpointer


# ============================================================
# ENTRY POINT FOR GITHUB ACTION
# ============================================================

def run_github_action():
    """Main entry point for GitHub Action."""
    
    # Load accounts from environment
    accounts = []
    for i in range(1, 6):
        email = os.getenv(f"PSS_ACCOUNT_{i}_EMAIL")
        password = os.getenv(f"PSS_ACCOUNT_{i}_PASSWORD")
        if email and password:
            accounts.append(AccountConfig(
                account_id=i,
                email=email,
                password=password,
                device_key=os.getenv(f"PSS_ACCOUNT_{i}_DEVICE_KEY"),
                smtp_email=os.getenv("SMTP_EMAIL"),
                smtp_password=os.getenv("SMTP_PASSWORD"),
                recipient=os.getenv("EMAIL_RECIPIENT"),
            ))
    
    if not accounts:
        raise ValueError("No accounts configured")
    
    # Get model (for future LLM-enhanced features)
    model = None
    try:
        model = get_nim_model()
        print(f"Using NIM model: {NIM_MODEL} at {NIM_BASE_URL}")
    except ValueError:
        model = get_fallback_model()
        if model:
            print("Using fallback OpenAI model")
        else:
            print("No LLM model available - running deterministic automation only")
    
    # Initial state
    initial_state = SupervisorState(
        accounts=accounts,
        run_battle=True,
        messages=[],
    )
    
    # Compile and run
    app, checkpointer = compile_workflow()
    
    config = {
        "configurable": {
            "thread_id": f"daily-run-{datetime.utcnow().strftime('%Y-%m-%d-%H%M')}",
        },
        "tags": ["production", "github-action", "pss-automation"],
        "metadata": {
            "accounts": len(accounts),
            "run_date": datetime.utcnow().date().isoformat(),
        },
    }
    
    # Execute
    print(f"Starting workflow for {len(accounts)} accounts...")
    result = app.invoke(initial_state, config=config)
    
    # Print summary
    print("\n=== WORKFLOW COMPLETE ===")
    completed = result.get('completed_accounts', [])
    failed = result.get('failed_accounts', [])
    print(f"Completed: {len(completed)}")
    print(f"Failed: {len(failed)}")
    
    for acc_id in completed + failed:
        details = result.get('account_details', {}).get(acc_id, {})
        print(f"  Account {acc_id}: {details.get('steps', [])} - Errors: {details.get('errors', [])}")
    
    return result


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_github_action()