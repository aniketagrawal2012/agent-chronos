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
    Gateway middleware for multi-agent systems (Google ADK / FastMCP / Gemini Enterprise Agent Platform).
    Intercepts wire traffic and guarantees linearizable external consistency.
    """

    def __init__(
        self,
        clock_uncertainty_ns: int = TrueTimeClock.DEFAULT_EPSILON_NS,
        abort_propagation_ns: int = 0,
        project_id: str = "aniket-personal",
        location: str = "global",
        model_name: str = "gemini-3.8-flash",
    ) -> None:
        self.clock: TrueTimeClock = TrueTimeClock(uncertainty_epsilon_ns=clock_uncertainty_ns)
        self.fence_engine: CausalFenceEngine = CausalFenceEngine(
            self.clock, abort_propagation_ns=abort_propagation_ns
        )
        self.pacer: TransportPacer = TransportPacer(self.clock, self.fence_engine)
        self.project_id = project_id
        self.location = location
        self.model_name = model_name
        self._sidecar_delivered_seq: Dict[Tuple[str, str], int] = {}

    def verify_speculative_epoch_with_gemini(
        self,
        epoch_id: str,
        tool_name: str,
        payload: Dict[str, Any],
    ) -> Tuple[bool, str]:
        """
        Verifies a speculative tool invocation using live Gemini models on the
        Gemini Enterprise Agent Platform (`project="aniket-personal"`, `location="global"`)
        via the Google Gen AI SDK (`google.genai`).
        """
        import subprocess
        from google import genai
        from google.oauth2.credentials import Credentials

        token = subprocess.check_output(
            ["gcloud", "auth", "print-access-token"], text=True, timeout=5.0
        ).strip()
        creds = Credentials(token=token, quota_project_id=self.project_id) if token else None
        client = genai.Client(
            vertexai=True,
            project=self.project_id,
            location=self.location,
            credentials=creds,
        )
        prompt = (
            "Respond strictly with JSON {\"approved\": true} if the following tool payload "
            "has a positive amount and valid structure, else {\"approved\": false}: "
            f"tool={tool_name}, payload={payload}"
        )
        resp = client.models.generate_content(model=self.model_name, contents=prompt)
        txt = (resp.text or "").strip().lower()
        approved = '"approved": true' in txt or '"approved":true' in txt
        if not approved:
            self.abort_speculative_branch(epoch_id)
        return approved, resp.text or ""

    def handle_incoming_request(
        self,
        headers: Dict[str, str],
        recipient_agent_id: str
    ) -> Tuple[Optional[TrueTimeInterval], Optional[CausalVector], Optional[str]]:
        """
        Processes inbound wire headers from an upstream agent.
        Extracts TrueTime interval, causal fence vector, per-destination sequence counter,
        and speculative epoch ID, and merges them into local causality state.
        """
        tt_header = headers.get("X-Agent-TrueTime-Window")
        cv_header = headers.get("X-Agent-Causal-Fence-Vector")
        epoch_header = headers.get("X-Agent-Speculative-Epoch")
        dest_seq_header = headers.get("X-Agent-Dest-Seq")
        origin_header = headers.get("X-Agent-Origin", "")
        target_header = headers.get("X-Agent-Target", recipient_agent_id)

        inbound_tt: Optional[TrueTimeInterval] = None
        inbound_cv: Optional[CausalVector] = None

        if tt_header:
            inbound_tt = TrueTimeInterval.from_wire_header(tt_header)

        if cv_header:
            inbound_cv = CausalVector.from_wire_header(cv_header)
            local_vec = self.fence_engine.get_or_create_vector(recipient_agent_id)
            local_vec.merge(inbound_cv)

        if dest_seq_header and origin_header:
            seq_val = int(dest_seq_header)
            key = (target_header, origin_header)
            prev_seq = self._sidecar_delivered_seq.get(key, 0)
            if seq_val == prev_seq + 1:
                self._sidecar_delivered_seq[key] = seq_val

        return (inbound_tt, inbound_cv, epoch_header)

    def get_sidecar_delivered_seq(self, target_service: str, origin_agent: str) -> int:
        """Returns the highest contiguous per-destination sequence number delivered at target_service."""
        return self._sidecar_delivered_seq.get((target_service, origin_agent), 0)

    def reject_stale_sibling_frame(
        self, target_service: str, origin_agent: str, seq_val: int
    ) -> Tuple[int, int]:
        """
        Handles HTTP 409 Stale Sibling Timestamp rejection at the ingress sidecar by
        advancing Seq_{loc,d}[a_s] to Seq_{dest,F}[d, a_s] as a tombstoned coordinate
        so subsequent valid frames from origin_agent do not stall.
        """
        key = (target_service, origin_agent)
        prev_seq = self._sidecar_delivered_seq.get(key, 0)
        if seq_val == prev_seq + 1:
            self._sidecar_delivered_seq[key] = seq_val
        if not hasattr(self, "_tombstoned_coords"):
            self._tombstoned_coords: Dict[Tuple[str, str], set[int]] = {}
        self._tombstoned_coords.setdefault(key, set()).add(seq_val)
        return (409, self._sidecar_delivered_seq.get(key, 0))

    def should_cascade_tombstone_abort(
        self,
        target_service: str,
        dep_agent: str,
        dep_seq: int,
        ack_tomb_seq: int = 0,
    ) -> bool:
        """
        Distinguishes in-flight speculative dependents of a tombstoned coordinate s_tomb
        (Dep_{d,F}[a_s] >= s_tomb > AckTomb_{d,F}[a_s], which cascade HTTP 409) from
        legitimate post-409 recovery writes (which attach AckTomb_{d,F}[a_s] >= s_tomb).
        """
        tomb_set = getattr(self, "_tombstoned_coords", {}).get((target_service, dep_agent), set())
        return any(dep_seq >= s_tomb > ack_tomb_seq for s_tomb in tomb_set)

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
        Automatically registers an epoch, increments the per-destination sequence counter,
        assembles the wire frame, and passes through the transport pacer.
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
        dest_seq = self.fence_engine.next_dest_sequence(target_service, origin_agent)

        frame = WireFrame(
            action_id=action_id,
            origin_agent=origin_agent,
            target_service=target_service,
            method=method,
            payload=payload,
            is_mutation=is_mutation,
            truetime_interval=epoch.truetime_interval,
            causal_vector=vec,
            speculative_epoch_id=epoch_id,
            dest_seq=dest_seq,
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
