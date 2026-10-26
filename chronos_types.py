"""
Agent-Chronos Data Types and Wire Protocol Invariants.

Author: Aniket Agrawal (<aniketagrawal@google.com>)
Google Cloud AI Architecture & Inventions

Defines formal models for TrueTime intervals, Causal Lamport-TrueTime Vectors,
Speculative Epochs, Wire Frames, and Transport Pacing States.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class PacingStatus(str, Enum):
    IMMEDIATE = "IMMEDIATE"
    SAFE_READ_WAIT = "SAFE_READ_WAIT"
    STAGE_HELD = "STAGE_HELD"
    PACED_HELD = "PACED_HELD"
    RELEASED = "RELEASED"
    REVOKED = "REVOKED"


class EpochState(str, Enum):
    ACTIVE = "ACTIVE"
    COMMITTED = "COMMITTED"
    ABORTED = "ABORTED"


@dataclass(frozen=True)
class TrueTimeInterval:
    """
    Formal representation of a TrueTime uncertainty interval: [earliest, latest]
    where latest - earliest = 2 * epsilon.
    All timestamps are represented in nanoseconds since UNIX epoch.
    """
    earliest_ns: int
    latest_ns: int
    epsilon_ns: int

    def __post_init__(self) -> None:
        if self.earliest_ns > self.latest_ns:
            raise ValueError(
                f"Invalid TrueTime interval: earliest ({self.earliest_ns}) > latest ({self.latest_ns})"
            )
        expected_diff = 2 * self.epsilon_ns
        actual_diff = self.latest_ns - self.earliest_ns
        if abs(actual_diff - expected_diff) > 2:  # allow 2ns rounding tolerance
            raise ValueError(
                f"Inconsistent uncertainty bounds: latest - earliest ({actual_diff}) != 2*epsilon ({expected_diff})"
            )

    @staticmethod
    def validate_settlement_window(
        t_e_earliest_ns: int,
        t_f_latest_ns: int,
        epsilon_eff_ns: int,
        epsilon_verifier_ns: Optional[int] = None,
    ) -> bool:
        """
        Verifies the generalized framing invariant for composite X-Agent-TrueTime-Window
        settlement tuples <t_{E,earliest}, t_{F,latest}, epsilon_eff> where
        t_{F,latest} = max(t_{E,latest}, t_{max,F}).
        """
        span = t_f_latest_ns - t_e_earliest_ns
        if span <= 0 or epsilon_eff_ns <= 0:
            return False
        if epsilon_verifier_ns is not None and span < 2 * epsilon_verifier_ns:
            return False
        return True

    def before(self, other: TrueTimeInterval) -> bool:
        """Strict linearizable order: guaranteed to occur before 'other' if self.latest < other.earliest."""
        return self.latest_ns < other.earliest_ns

    def after(self, other: TrueTimeInterval) -> bool:
        """Strict linearizable order: guaranteed to occur after 'other' if self.earliest > other.latest."""
        return self.earliest_ns > other.latest_ns

    def overlaps(self, other: TrueTimeInterval) -> bool:
        """True if uncertainty bounds overlap (cannot determine strict linearizable order without commit wait)."""
        return not (self.before(other) or self.after(other))

    def wait_duration_ns(self, current_time_ns: int) -> int:
        """Calculates the physical wait duration in nanoseconds until self.latest_ns is in the past."""
        return max(0, self.latest_ns - current_time_ns)

    def to_wire_header(self) -> str:
        """Wire format: [earliest_ns,latest_ns,epsilon_ns]"""
        return f"[{self.earliest_ns},{self.latest_ns},{self.epsilon_ns}]"

    @classmethod
    def from_wire_header(cls, header_val: str) -> TrueTimeInterval:
        """Parses [earliest,latest,epsilon] wire header format."""
        clean = header_val.strip("[] \t\r\n")
        parts = [int(p.strip()) for p in clean.split(",")]
        if len(parts) != 3:
            raise ValueError(f"Malformed TrueTime wire header: {header_val}")
        return cls(earliest_ns=parts[0], latest_ns=parts[1], epsilon_ns=parts[2])


@dataclass
class CausalVector:
    """
    Composite Lamport-TrueTime Causal Vector V_i = <C_i, t_{max,i}> with active sibling fork bitmap.
    Tracks logical sequence counters per agent ID alongside the maximum TrueTime upper bound observed.
    """
    clocks: Dict[str, int] = field(default_factory=dict)
    max_truetime_ns: int = 0
    sibling_bitmap: int = 0

    def increment(self, agent_id: str, current_truetime_ns: int) -> None:
        self.clocks[agent_id] = self.clocks.get(agent_id, 0) + 1
        if current_truetime_ns > self.max_truetime_ns:
            self.max_truetime_ns = current_truetime_ns

    def merge(self, other: CausalVector) -> None:
        for aid, count in other.clocks.items():
            self.clocks[aid] = max(self.clocks.get(aid, 0), count)
        self.max_truetime_ns = max(self.max_truetime_ns, other.max_truetime_ns)

    def dominates(self, other: CausalVector) -> bool:
        """Returns True if self causally succeeds or equals other."""
        for aid, count in other.clocks.items():
            if self.clocks.get(aid, 0) < count:
                return False
        return True

    def strictly_precedes(self, other: CausalVector) -> bool:
        """Implements V_1 <_V V_2 over D = dom(C_1) U dom(C_2)."""
        support = set(self.clocks.keys()) | set(other.clocks.keys())
        all_leq = all(self.clocks.get(k, 0) <= other.clocks.get(k, 0) for k in support)
        any_lt = any(self.clocks.get(k, 0) < other.clocks.get(k, 0) for k in support)
        return all_leq and any_lt

    def is_concurrent_with(self, other: CausalVector) -> bool:
        """Implements V_1 || V_2 <=> not(V_1 <_V V_2) and not(V_2 <_V V_1)."""
        return not self.strictly_precedes(other) and not other.strictly_precedes(self)

    def to_wire_header(self) -> str:
        payload: Dict[str, Any] = {
            "c": self.clocks,
            "t": self.max_truetime_ns,
        }
        if self.sibling_bitmap:
            payload["b"] = self.sibling_bitmap
        return json.dumps(payload, separators=(",", ":"))

    @classmethod
    def from_wire_header(cls, header_val: str) -> CausalVector:
        data = json.loads(header_val)
        return cls(
            clocks=data.get("c", {}),
            max_truetime_ns=data.get("t", 0),
            sibling_bitmap=data.get("b", 0),
        )


@dataclass
class SpeculativeEpoch:
    """
    Tracks an in-flight speculative execution branch across multi-agent turns.
    """
    epoch_id: str
    origin_agent: str
    truetime_interval: TrueTimeInterval
    state: EpochState = EpochState.ACTIVE
    parent_epochs: List[str] = field(default_factory=list)
    registered_at_ns: int = field(default_factory=lambda: time.time_ns())


@dataclass
class WireFrame:
    """
    Transport payload enriched with wire-level TrueTime and Causal Fencing headers.
    """
    action_id: str
    origin_agent: str
    target_service: str
    method: str
    payload: Dict[str, Any]
    is_mutation: bool
    truetime_interval: TrueTimeInterval
    causal_vector: CausalVector
    speculative_epoch_id: str
    dest_seq: int = 1
    pacing_status: PacingStatus = PacingStatus.IMMEDIATE
    pacing_delay_ms: float = 0.0
    headers: Dict[str, str] = field(default_factory=dict)

    def assemble_headers(self) -> Dict[str, str]:
        """Constructs public wire-observable headers adhering to Agent-Chronos protocol."""
        h = {
            "X-Agent-Origin": self.origin_agent,
            "X-Agent-Target": self.target_service,
            "X-Agent-Action-ID": self.action_id,
            "X-Agent-Speculative-Epoch": self.speculative_epoch_id,
            "X-Agent-TrueTime-Window": self.truetime_interval.to_wire_header(),
            "X-Agent-Causal-Fence-Vector": self.causal_vector.to_wire_header(),
            "X-Agent-Dest-Seq": str(self.dest_seq),
            "X-Agent-Is-Mutation": "true" if self.is_mutation else "false",
            "X-Agent-Pacing-Status": self.pacing_status.value,
            "X-Agent-Pacing-Delay-Ms": f"{self.pacing_delay_ms:.3f}",
        }
        self.headers.update(h)
        return self.headers


@dataclass
class PacingResult:
    """
    Result returned by the transport pacing barrier upon frame dispatch.
    """
    status: str
    elapsed_wait_ms: float
    wire_headers: Dict[str, str]
    dropped_reason: Optional[str] = None

