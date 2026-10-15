"""
Physical Egress Socket Serializer and Transport Pacing Engine.

Author: Aniket Agrawal (<aniketagrawal@google.com>)
Google Cloud AI Architecture & Inventions

Enforces network transport pacing fences on outbound agent frames.
Physically stalls mutating tool requests until TrueTime uncertainty intervals expire,
guaranteeing linearizable consistency and preventing phantom side effects.
"""

from __future__ import annotations

import time
from typing import Callable, Dict, List, Optional, Tuple

from causal_fence_engine import CausalFenceEngine
from chronos_types import PacingResult, PacingStatus, WireFrame
from truetime_clock import TrueTimeClock


class TransportPacer:
    """
    Egress transport pacing barrier.
    Sits between agent runtimes (ADK / FastMCP / Vertex AI) and physical network sockets.
    """

    def __init__(
        self,
        clock: TrueTimeClock,
        fence_engine: CausalFenceEngine,
        mock_network_sender: Optional[Callable[[WireFrame], Dict[str, any]]] = None
    ) -> None:
        self.clock: TrueTimeClock = clock
        self.fence_engine: CausalFenceEngine = fence_engine
        self.network_sender = mock_network_sender or self._default_mock_sender
        
        # Telemetry metrics
        self.total_frames_processed: int = 0
        self.total_mutations_paced: int = 0
        self.total_frames_revoked_on_wire: int = 0
        self.cumulative_pacing_wait_ms: float = 0.0

    def _default_mock_sender(self, frame: WireFrame) -> Dict[str, any]:
        """Default simulated network transmission."""
        return {
            "status_code": 200,
            "action_id": frame.action_id,
            "wire_headers": frame.headers,
            "delivered": True
        }

    def dispatch_frame(self, frame: WireFrame) -> PacingResult:
        """
        Dispatches a wire frame through the TrueTime physical pacing barrier.
        
        Returns:
            PacingResult detailing final transmission status, elapsed wait, and headers.
        """
        self.total_frames_processed += 1
        
        # Step 1: Initial barrier evaluation
        status, wait_ms, reason = self.fence_engine.evaluate_fencing_barrier(frame)
        
        if status == PacingStatus.REVOKED:
            self.total_frames_revoked_on_wire += 1
            frame.pacing_status = PacingStatus.REVOKED
            frame.assemble_headers()
            return PacingResult(
                status=PacingStatus.REVOKED.value,
                elapsed_wait_ms=0.0,
                wire_headers=frame.headers,
                dropped_reason=reason
            )

        elapsed_pacing_ms = 0.0

        # Step 2: If PACED_HELD, execute physical commit-wait on TrueTime clock
        if status == PacingStatus.PACED_HELD:
            epoch = self.fence_engine._epochs.get(frame.speculative_epoch_id)
            if epoch:
                # Halt execution until TrueTime uncertainty window elapses
                elapsed_pacing_ms = self.clock.wait_until_past(epoch.truetime_interval)
                self.cumulative_pacing_wait_ms += elapsed_pacing_ms
                self.total_mutations_paced += 1

                # Re-evaluate after wait: verify epoch was not revoked during sleep
                post_status, _, post_reason = self.fence_engine.evaluate_fencing_barrier(frame)
                if post_status == PacingStatus.REVOKED:
                    self.total_frames_revoked_on_wire += 1
                    frame.pacing_status = PacingStatus.REVOKED
                    frame.assemble_headers()
                    return PacingResult(
                        status=PacingStatus.REVOKED.value,
                        elapsed_wait_ms=elapsed_pacing_ms,
                        wire_headers=frame.headers,
                        dropped_reason=f"Revoked during TrueTime wait: {post_reason}"
                    )
            
            frame.pacing_status = PacingStatus.RELEASED
        else:
            frame.pacing_status = status

        frame.pacing_delay_ms = elapsed_pacing_ms
        frame.assemble_headers()

        # Step 3: Transmit onto physical network socket
        self.network_sender(frame)

        return PacingResult(
            status=frame.pacing_status.value,
            elapsed_wait_ms=elapsed_pacing_ms,
            wire_headers=frame.headers,
            dropped_reason=None
        )
