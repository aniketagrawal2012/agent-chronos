"""
Causal Fencing and Speculative Epoch Dependency Engine.

Author: Aniket Agrawal (<aniketagrawal@google.com>)
Google Cloud AI Architecture & Inventions

Tracks distributed causal graphs across multi-agent turns.
Resolves Lamport-TrueTime composite vectors and computes linearizable
ordering barriers for speculative tool executions.
"""

from __future__ import annotations

import threading
from typing import Dict, List, Optional, Set, Tuple

from chronos_types import (
    CausalVector,
    EpochState,
    PacingStatus,
    SpeculativeEpoch,
    TrueTimeInterval,
    WireFrame,
)
from truetime_clock import TrueTimeClock


class CausalFenceEngine:
    """
    Manages speculative epochs, causal vector synchronization,
    and barrier resolutions across multi-agent swarms.
    """

    def __init__(self, clock: TrueTimeClock) -> None:
        self.clock: TrueTimeClock = clock
        self._lock = threading.RLock()
        self._epochs: Dict[str, SpeculativeEpoch] = {}
        self._agent_vectors: Dict[str, CausalVector] = {}
        self._aborted_epochs: Set[str] = set()

    def get_or_create_vector(self, agent_id: str) -> CausalVector:
        with self._lock:
            if agent_id not in self._agent_vectors:
                self._agent_vectors[agent_id] = CausalVector()
            return self._agent_vectors[agent_id]

    def register_epoch(
        self,
        epoch_id: str,
        origin_agent: str,
        parent_epochs: Optional[List[str]] = None
    ) -> SpeculativeEpoch:
        """Registers a new speculative execution epoch with current TrueTime uncertainty bounds."""
        with self._lock:
            interval = self.clock.now()
            epoch = SpeculativeEpoch(
                epoch_id=epoch_id,
                origin_agent=origin_agent,
                truetime_interval=interval,
                state=EpochState.ACTIVE,
                parent_epochs=parent_epochs or []
            )
            self._epochs[epoch_id] = epoch
            
            # Update agent's causal vector
            vec = self.get_or_create_vector(origin_agent)
            vec.increment(origin_agent, interval.latest_ns)
            return epoch

    def commit_epoch(self, epoch_id: str) -> TrueTimeInterval:
        """
        Commits an epoch and performs TrueTime uncertainty commit wait to guarantee
        external consistency before the epoch is considered definitively irreversible.
        """
        with self._lock:
            if epoch_id not in self._epochs:
                raise KeyError(f"Unknown epoch: {epoch_id}")
            epoch = self._epochs[epoch_id]
            if epoch.state == EpochState.ABORTED:
                raise RuntimeError(f"Cannot commit aborted epoch {epoch_id}")
                
            # Perform commit-wait on the epoch interval
            self.clock.wait_until_past(epoch.truetime_interval)
            epoch.state = EpochState.COMMITTED
            return epoch.truetime_interval

    def abort_epoch(self, epoch_id: str) -> List[str]:
        """
        Aborts an epoch and all transitive descendant epochs across the multi-agent swarm.
        Returns the list of all revoked epoch IDs.
        """
        with self._lock:
            revoked: List[str] = []
            to_abort = [epoch_id]
            
            while to_abort:
                curr_id = to_abort.pop()
                if curr_id in self._aborted_epochs:
                    continue
                self._aborted_epochs.add(curr_id)
                revoked.append(curr_id)
                
                if curr_id in self._epochs:
                    self._epochs[curr_id].state = EpochState.ABORTED
                    
                # Find all children depending on curr_id
                for child_id, child_epoch in self._epochs.items():
                    if curr_id in child_epoch.parent_epochs and child_id not in self._aborted_epochs:
                        to_abort.append(child_id)
                        
            return revoked

    def evaluate_fencing_barrier(
        self,
        frame: WireFrame
    ) -> Tuple[PacingStatus, float, Optional[str]]:
        """
        Evaluates whether an outbound wire frame requires physical transport pacing.
        
        Rules:
        1. If the frame belongs to an aborted epoch or depends on an aborted epoch:
           Status = REVOKED (drop immediately on the wire).
        2. If the frame is NOT a mutation (read-only query):
           Status = IMMEDIATE (no pacing required).
        3. If the frame is a MUTATION and depends on an uncommitted speculative epoch
           whose TrueTime interval overlaps with current physical time:
           Status = PACED_HELD (must stall until the TrueTime uncertainty window elapses).
        4. Otherwise:
           Status = RELEASED (safe for immediate transmission).
        """
        with self._lock:
            # Check for revocation
            if frame.speculative_epoch_id in self._aborted_epochs:
                return (PacingStatus.REVOKED, 0.0, f"Epoch {frame.speculative_epoch_id} has been revoked")

            epoch = self._epochs.get(frame.speculative_epoch_id)
            if epoch:
                for parent in epoch.parent_epochs:
                    if parent in self._aborted_epochs:
                        return (PacingStatus.REVOKED, 0.0, f"Parent epoch {parent} has been revoked")

            # Non-mutating calls (GET / read queries) bypass pacing fences
            if not frame.is_mutation:
                return (PacingStatus.IMMEDIATE, 0.0, None)

            # For mutating actions, enforce TrueTime linearizable ordering
            if epoch and epoch.state != EpochState.COMMITTED:
                # Calculate wait required until epoch.latest_ns is in the past
                hw_now = self.clock.get_hardware_time_ns()
                wait_ns = epoch.truetime_interval.wait_duration_ns(hw_now)
                if wait_ns > 0:
                    wait_ms = wait_ns / 1_000_000.0
                    return (PacingStatus.PACED_HELD, wait_ms, None)

            return (PacingStatus.RELEASED, 0.0, None)
