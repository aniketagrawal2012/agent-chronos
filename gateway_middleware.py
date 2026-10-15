"""
Agent-Chronos FastMCP & HTTP/2 Gateway Middleware.

Author: Aniket Agrawal (<aniketagrawal@google.com>)
Google Cloud AI Architecture & Inventions

Integrates TrueTime Causal Fencing into standard FastMCP, gRPC, and HTTP/2
gateways, providing wire-level header interception, causal vector merging,
and automated egress transport pacing.
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, Optional, Tuple

from causal_fence_engine import CausalFenceEngine
from chronos_types import (
    CausalVector,
    PacingResult,
    PacingStatus,
    TrueTimeInterval,
    WireFrame,
)
from transport_pacer import TransportPacer
from truetime_clock import TrueTimeClock


class AgentChronosGateway:
    """
    Gateway middleware for multi-agent systems (Google ADK / FastMCP / Vertex AI).
    Intercepts wire traffic and guarantees linearizable external consistency.
    """

    def __init__(self, clock_uncertainty_ns: int = TrueTimeClock.DEFAULT_EPSILON_NS) -> None:
        self.clock: TrueTimeClock = TrueTimeClock(uncertainty_epsilon_ns=clock_uncertainty_ns)
        self.fence_engine: CausalFenceEngine = CausalFenceEngine(self.clock)
        self.pacer: TransportPacer = TransportPacer(self.clock, self.fence_engine)

    def handle_incoming_request(
        self,
        headers: Dict[str, str],
        recipient_agent_id: str
    ) -> Tuple[Optional[TrueTimeInterval], Optional[CausalVector], Optional[str]]:
        """
        Processes inbound wire headers from an upstream agent.
        Extracts TrueTime interval, causal fence vector, and speculative epoch ID,
        and merges them into local causality state.
        """
        tt_header = headers.get("X-Agent-TrueTime-Window")
        cv_header = headers.get("X-Agent-Causal-Fence-Vector")
        epoch_header = headers.get("X-Agent-Speculative-Epoch")

        inbound_tt: Optional[TrueTimeInterval] = None
        inbound_cv: Optional[CausalVector] = None

        if tt_header:
            inbound_tt = TrueTimeInterval.from_wire_header(tt_header)

        if cv_header:
            inbound_cv = CausalVector.from_wire_header(cv_header)
            local_vec = self.fence_engine.get_or_create_vector(recipient_agent_id)
            local_vec.merge(inbound_cv)

        return (inbound_tt, inbound_cv, epoch_header)

    def dispatch_agent_tool_call(
        self,
        origin_agent: str,
        target_service: str,
        method: str,
        payload: Dict[str, Any],
        is_mutation: bool,
        parent_epochs: Optional[list[str]] = None
    ) -> PacingResult:
        """
        High-level API for an agent issuing a tool execution.
        Automatically registers an epoch, assembles wire frame, and passes through pacer.
        """
        action_id = f"act_{uuid.uuid4().hex[:8]}"
        epoch_id = f"ep_{uuid.uuid4().hex[:8]}"

        # Register speculative epoch
        epoch = self.fence_engine.register_epoch(
            epoch_id=epoch_id,
            origin_agent=origin_agent,
            parent_epochs=parent_epochs
        )

        vec = self.fence_engine.get_or_create_vector(origin_agent)

        frame = WireFrame(
            action_id=action_id,
            origin_agent=origin_agent,
            target_service=target_service,
            method=method,
            payload=payload,
            is_mutation=is_mutation,
            truetime_interval=epoch.truetime_interval,
            causal_vector=vec,
            speculative_epoch_id=epoch_id
        )

        # Dispatch via transport pacer
        result = self.pacer.dispatch_frame(frame)
        return result

    def abort_speculative_branch(self, epoch_id: str) -> list[str]:
        """Revokes an entire speculative subtree across all agents."""
        return self.fence_engine.abort_epoch(epoch_id)

    def commit_speculative_branch(self, epoch_id: str) -> TrueTimeInterval:
        """Commits a speculative epoch with TrueTime linearizable certainty."""
        return self.fence_engine.commit_epoch(epoch_id)
