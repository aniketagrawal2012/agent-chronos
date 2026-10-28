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

    def test_per_destination_sequence_continuity_across_multiple_services(self) -> None:
        # Agent A calls destination d1 (sap_s4hana), then d2 (bigquery), then d1 (sap_s4hana) again.
        # Global CausalVector increments to 3, while X-Agent-Dest-Seq for sap_s4hana must be 1 then 2 (gap-free).
        r1 = self.gateway.dispatch_agent_tool_call(
            origin_agent="agent_a",
            target_service="sap_s4hana",
            method="GET",
            payload={"doc": 1},
            is_mutation=False,
        )
        r2 = self.gateway.dispatch_agent_tool_call(
            origin_agent="agent_a",
            target_service="bigquery",
            method="GET",
            payload={"q": 2},
            is_mutation=False,
        )
        r3 = self.gateway.dispatch_agent_tool_call(
            origin_agent="agent_a",
            target_service="sap_s4hana",
            method="GET",
            payload={"doc": 3},
            is_mutation=False,
        )
        self.assertEqual(r1.wire_headers["X-Agent-Dest-Seq"], "1")
        self.assertEqual(r2.wire_headers["X-Agent-Dest-Seq"], "1")
        self.assertEqual(r3.wire_headers["X-Agent-Dest-Seq"], "2")

        self.gateway.handle_incoming_request(r1.wire_headers, recipient_agent_id="sap_s4hana")
        self.gateway.handle_incoming_request(r3.wire_headers, recipient_agent_id="sap_s4hana")
        self.assertEqual(self.gateway.get_sidecar_delivered_seq("sap_s4hana", "agent_a"), 2)

    def test_causal_precedence_and_normative_commit_wait(self) -> None:
        v1 = CausalVector(clocks={"a1": 1, "a2": 0}, max_truetime_ns=1000, sibling_bitmap=3)
        v2 = CausalVector(clocks={"a1": 1, "a2": 2}, max_truetime_ns=1500)
        v3 = CausalVector(clocks={"a1": 2, "a2": 1}, max_truetime_ns=1600)

        self.assertTrue(v1.strictly_precedes(v2))
        self.assertFalse(v2.strictly_precedes(v1))
        self.assertTrue(v2.is_concurrent_with(v3))

        # Round-trip compact JSON with sibling bitmap
        hdr = v1.to_wire_header()
        self.assertLessEqual(len(hdr), 190)
        v1_parsed = CausalVector.from_wire_header(hdr)
        self.assertEqual(v1_parsed.sibling_bitmap, 3)

        # Generalized settlement window check
        self.assertTrue(
            TrueTimeInterval.validate_settlement_window(
                t_e_earliest_ns=100, t_f_latest_ns=260, epsilon_eff_ns=40, epsilon_verifier_ns=50
            )
        )
        self.assertFalse(
            TrueTimeInterval.validate_settlement_window(
                t_e_earliest_ns=200, t_f_latest_ns=180, epsilon_eff_ns=40
            )
        )

        # Normative Eq. (5) commit-wait calculation
        wait_ns = self.gateway.fence_engine.compute_normative_commit_wait_ns(
            t_e_latest_ns=10_000_000,
            v_f_t_max_ns=12_000_000,
            t_now_earliest_ns=9_000_000,
        )
        self.assertEqual(wait_ns, 3_000_000)

        # Ceiling-rounded epsilon_eff for opposite parity bounds
        self.assertEqual(TrueTimeInterval.compute_epsilon_eff_ns(3_000_001, 4_000_000), 3_500_001)

        # Positional 16-agent JSON byte budget (69-121 B unformatted)
        v16_min = CausalVector(
            clocks={f"a{i}": 1 for i in range(16)},
            max_truetime_ns=1_700_000_000_000_000_000,
            sibling_bitmap=0,
        )
        v16_max = CausalVector(
            clocks={f"a{i}": 9999 for i in range(16)},
            max_truetime_ns=1_700_000_000_000_000_000,
            sibling_bitmap=65535,
        )
        self.assertEqual(len(v16_min.to_positional_wire_header()), 69)
        self.assertEqual(len(v16_max.to_positional_wire_header()), 121)

        # Branch-local tentative pre-staging vector isolation (no mutation of committed state on abort)
        committed_before = dict(self.gateway.fence_engine.get_or_create_vector("agent_iso").clocks)
        tentative = self.gateway.fence_engine.allocate_tentative_vector("agent_iso", 5_000_000)
        self.assertEqual(tentative.clocks["agent_iso"], 1)
        self.assertEqual(
            self.gateway.fence_engine.get_or_create_vector("agent_iso").clocks,
            committed_before,
        )

        # Rule 1(b) sibling commit interval overlap & monotonic key settlement floor
        self.assertTrue(self.gateway.fence_engine.should_chain_sibling_read(100, 120))
        floor1 = self.gateway.fence_engine.advance_key_settlement_floor("sap_s4hana", "ledger_1", 500)
        floor2 = self.gateway.fence_engine.advance_key_settlement_floor("sap_s4hana", "ledger_1", 480)
        self.assertEqual(floor1, 500)
        self.assertEqual(floor2, 501)

        # Stale sibling HTTP 409 rejection advances tombstoned ingress sequence watermark
        code, delivered = self.gateway.reject_stale_sibling_frame("sap_s4hana", "agent_sib", 1)
        self.assertEqual(code, 409)
        self.assertEqual(delivered, 1)

        # Distinguish in-flight speculative dependent (ack_tomb_seq=0) from post-409 recovery write (ack_tomb_seq>=1)
        self.assertTrue(
            self.gateway.should_cascade_tombstone_abort(
                "sap_s4hana", "agent_sib", dep_seq=1, ack_tomb_seq=0
            )
        )
        self.assertFalse(
            self.gateway.should_cascade_tombstone_abort(
                "sap_s4hana", "agent_sib", dep_seq=1, ack_tomb_seq=1
            )
        )

        # Wildcard key intersection and key-floor-adjusted Eq. (5) commit-wait
        self.assertTrue(self.gateway.fence_engine.keys_intersect({"*"}, {"$.ledger_id"}))
        self.assertFalse(self.gateway.fence_engine.keys_intersect({"$.a"}, {"$.b"}))
        wait_key_floor_ns = self.gateway.fence_engine.compute_normative_commit_wait_ns(
            t_e_latest_ns=10_000_000,
            v_f_t_max_ns=11_000_000,
            t_now_earliest_ns=9_000_000,
            key_floor_ns=13_500_000,
            wan_pre_ns=500_000,
        )
        self.assertEqual(wait_key_floor_ns, 5_000_000)

        # Rule 1(a) vs Rule 1(b) write revocation behavior and Rule 3 abort preemption predicate
        self.assertEqual(
            self.gateway.fence_engine.resolve_safe_read_on_write_revocation(is_causal_predecessor=True),
            PacingStatus.REVOKED,
        )
        self.assertEqual(
            self.gateway.fence_engine.resolve_safe_read_on_write_revocation(is_causal_predecessor=False),
            PacingStatus.IMMEDIATE,
        )
        self.assertTrue(self.gateway.fence_engine.should_preempt_on_abort(11_500_000, 12_000_000))
        self.assertFalse(self.gateway.fence_engine.should_preempt_on_abort(12_500_000, 12_000_000))


if __name__ == "__main__":
    unittest.main()
