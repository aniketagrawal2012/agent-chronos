"""
Unit and Invariant Verification Test Suite for Agent-Chronos.

Author: Aniket Agrawal (<aniketagrawal@google.com>)
Google Cloud AI Architecture & Inventions

Tests:
1. TrueTime uncertainty interval ordering invariants.
2. Read-only actions bypassing transport pacing fences.
3. Mutating actions enforcing physical commit-wait pacing.
4. In-flight speculative branch revocation dropping frames before wire egress.
5. Cross-agent causal vector propagation and header deserialization.
"""

import time
import unittest

from chronos_types import CausalVector, PacingStatus, TrueTimeInterval, WireFrame
from gateway_middleware import AgentChronosGateway
from truetime_clock import TrueTimeClock


class TestAgentChronos(unittest.TestCase):

    def setUp(self) -> None:
        # Use 3ms uncertainty for ultra-fast unit testing
        self.gateway = AgentChronosGateway(clock_uncertainty_ns=3_000_000)

    def test_truetime_interval_invariants(self) -> None:
        t1 = TrueTimeInterval(100, 200, 50)
        t2 = TrueTimeInterval(250, 350, 50)
        t3 = TrueTimeInterval(150, 250, 50)

        # Strict linearizability
        self.assertTrue(t1.before(t2))
        self.assertTrue(t2.after(t1))
        self.assertFalse(t1.before(t3))
        self.assertTrue(t1.overlaps(t3))

        # Wire serialization round-trip
        header = t1.to_wire_header()
        self.assertEqual(header, "[100,200,50]")
        parsed = TrueTimeInterval.from_wire_header(header)
        self.assertEqual(parsed, t1)

    def test_read_only_tool_bypasses_pacing(self) -> None:
        res = self.gateway.dispatch_agent_tool_call(
            origin_agent="agent_analytics",
            target_service="bigquery",
            method="SELECT",
            payload={"query": "SELECT revenue FROM metrics"},
            is_mutation=False
        )
        self.assertEqual(res.status, PacingStatus.IMMEDIATE.value)
        self.assertEqual(res.elapsed_wait_ms, 0.0)
        self.assertEqual(res.wire_headers["X-Agent-Pacing-Status"], "IMMEDIATE")
        self.assertEqual(res.wire_headers["X-Agent-Is-Mutation"], "false")

    def test_mutating_tool_enforces_physical_pacing(self) -> None:
        # Mutating action must experience TrueTime uncertainty wait
        start = time.time()
        res = self.gateway.dispatch_agent_tool_call(
            origin_agent="agent_finance",
            target_service="sap_s4hana",
            method="POST",
            payload={"bapi": "BAPI_ACC_DOCUMENT_POST", "amount": 50000},
            is_mutation=True
        )
        duration_ms = (time.time() - start) * 1000.0

        self.assertEqual(res.status, PacingStatus.RELEASED.value)
        self.assertGreater(res.elapsed_wait_ms, 0.0)
        self.assertGreaterEqual(duration_ms, 2.5)  # bounded by uncertainty epsilon
        self.assertEqual(res.wire_headers["X-Agent-Pacing-Status"], "RELEASED")
        self.assertEqual(res.wire_headers["X-Agent-Is-Mutation"], "true")
        self.assertIn("X-Agent-TrueTime-Window", res.wire_headers)

    def test_in_flight_speculative_revocation(self) -> None:
        # Create an upstream speculative branch
        ep1 = self.gateway.fence_engine.register_epoch("ep_branch_1", "agent_upstream")
        
        # Abort the upstream branch before downstream mutation dispatches
        self.gateway.abort_speculative_branch("ep_branch_1")

        # Attempt to dispatch downstream dependent tool call
        res = self.gateway.dispatch_agent_tool_call(
            origin_agent="agent_downstream",
            target_service="stripe",
            method="POST",
            payload={"charge": 100},
            is_mutation=True,
            parent_epochs=["ep_branch_1"]
        )

        # Must be revoked immediately and never transmitted
        self.assertEqual(res.status, PacingStatus.REVOKED.value)
        self.assertIsNotNone(res.dropped_reason)
        self.assertEqual(res.wire_headers["X-Agent-Pacing-Status"], "REVOKED")

    def test_cross_agent_causal_vector_propagation(self) -> None:
        res1 = self.gateway.dispatch_agent_tool_call(
            origin_agent="agent_a",
            target_service="service_inventory",
            method="GET",
            payload={"sku": "ITEM_992"},
            is_mutation=False
        )

        headers = res1.wire_headers
        tt, cv, ep = self.gateway.handle_incoming_request(headers, recipient_agent_id="agent_b")

        self.assertIsNotNone(tt)
        self.assertIsNotNone(cv)
        self.assertIn("agent_a", cv.clocks)
        self.assertGreaterEqual(cv.clocks["agent_a"], 1)


if __name__ == "__main__":
    unittest.main()
