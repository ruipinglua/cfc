"""
Persistent state management for the wheel strategy agent.
State is written to state.json so the agent survives restarts.
"""

import json
import logging
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path

logger = logging.getLogger(__name__)

STATE_FILE = Path(__file__).parent / "state.json"


class WheelStage(str, Enum):
    IDLE = "IDLE"               # No open position; ready to sell the first put
    SELL_PUT = "SELL_PUT"       # Short put is open, waiting for expiry or assignment
    HAVE_SHARES = "HAVE_SHARES" # Put was assigned; shares owned, need to sell a call
    SELL_CALL = "SELL_CALL"     # Short call is open against owned shares


@dataclass
class WheelState:
    stage: WheelStage = WheelStage.IDLE
    ticker: str = "TSLA"

    # Stock position
    shares_owned: int = 0
    cost_basis_per_share: float = 0.0   # Effective cost after accounting for put premium

    # Current open option
    current_option_symbol: str = ""     # OCC symbol, e.g. TSLA240119P00230000
    current_option_sell_price: float = 0.0  # Per-share price we received when selling

    # Premium accounting
    put_premium_this_cycle: float = 0.0
    call_premium_this_cycle: float = 0.0
    total_premiums_collected: float = 0.0

    # Cycle counter (incremented each time shares are called away)
    cycle_count: int = 0
    last_summary_date: str = ""


class StateManager:
    def __init__(self):
        self.state = self._load()

    # ------------------------------------------------------------------
    # Load / save
    # ------------------------------------------------------------------

    def _load(self) -> WheelState:
        if STATE_FILE.exists():
            try:
                data = json.loads(STATE_FILE.read_text())
                data["stage"] = WheelStage(data["stage"])
                return WheelState(**data)
            except Exception as exc:
                logger.warning("Could not load state file, starting fresh: %s", exc)
        return WheelState()

    def save(self):
        data = asdict(self.state)
        data["stage"] = self.state.stage.value
        STATE_FILE.write_text(json.dumps(data, indent=2))
        logger.debug("State saved: stage=%s", self.state.stage.value)

    def update(self, **kwargs):
        for key, value in kwargs.items():
            if not hasattr(self.state, key):
                raise AttributeError(f"WheelState has no attribute '{key}'")
            setattr(self.state, key, value)
        self.save()

    # ------------------------------------------------------------------
    # Convenience accessor
    # ------------------------------------------------------------------

    @property
    def stage(self) -> WheelStage:
        return self.state.stage
