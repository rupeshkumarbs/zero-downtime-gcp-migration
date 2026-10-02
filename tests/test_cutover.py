import unittest

from cutover.backfill import Checkpoint, backfill
from cutover.dual_write import DualWriteRepository, IllegalTransition, Mode
from cutover.gitops import render_virtualservice
from cutover.simulate import run
from cutover.stores import SqliteStore, StoreError, postgres_dsn_from_env
from cutover.traffic import DEFAULT_PLAN, SLO, Decision, Phase, Status, TrafficController, WindowMetrics
from cutover.verifier import verify


def stores():
    return SqliteStore("src", seed=1), SqliteStore("tgt", seed=2)


class StoreTests(unittest.TestCase):
    def test_version_guard_keeps_newer_row(self):
        s, _ = stores()
        s.upsert("k", {"v": 2}, version=2)
        s.upsert("k", {"v": 1}, version=1)  # stale write, e.g. a late backfill
        record, _ = s.get("k")
        self.assertEqual(record.value, {"v": 2})

    def test_injected_errors(self):
        s, _ = stores()
        s.faults.error_rate = 1.0
        with self.assertRaises(StoreError):
            s.upsert("k", {}, 1)


class DualWriteTests(unittest.TestCase):
    def test_failed_mirror_write_is_parked_then_repaired(self):
        s, t = stores()
        repo = DualWriteRepository(s, t, Mode.DUAL_SOURCE_PRIMARY)
        t.faults.error_rate = 1.0
        repo.write("k", {"v": 1})  # user request still succeeds
        self.assertEqual(list(repo.reconcile_queue), ["k"])
        t.faults.error_rate = 0.0
        self.assertEqual(repo.drain_reconcile_queue(), (1, 0))
        self.assertTrue(verify(s, t).consistent)

    def test_primary_failure_fails_the_request(self):
        s, t = stores()
        repo = DualWriteRepository(s, t, Mode.DUAL_SOURCE_PRIMARY)
        s.faults.error_rate = 1.0
        with self.assertRaises(StoreError):
            repo.write("k", {"v": 1})
        self.assertEqual(t.count(), 0)  # nothing half-written to the secondary

    def test_target_primary_keeps_source_in_sync_for_rollback(self):
        s, t = stores()
        repo = DualWriteRepository(s, t, Mode.DUAL_TARGET_PRIMARY)
        repo.write("k", {"v": 1})
        self.assertIs(repo.primary, t)
        self.assertTrue(verify(s, t).consistent)

    def test_transitions_are_single_steps(self):
        repo = DualWriteRepository(*stores())
        with self.assertRaises(IllegalTransition):
            repo.transition(Mode.TARGET_ONLY)
        repo.transition(Mode.DUAL_SOURCE_PRIMARY)
        repo.transition(Mode.SOURCE_ONLY)  # one step back = rollback
        for mode in (Mode.DUAL_SOURCE_PRIMARY, Mode.DUAL_TARGET_PRIMARY, Mode.TARGET_ONLY):
            repo.transition(mode)
        with self.assertRaises(IllegalTransition):
            repo.transition(Mode.DUAL_TARGET_PRIMARY)  # source already decommissioned


class BackfillTests(unittest.TestCase):
    def seeded(self, n=1000):
        s, t = stores()
        for i in range(n):
            s.upsert(f"k{i:05d}", {"i": i}, version=1)
        return s, t

    def test_resumes_from_checkpoint_after_failure(self):
        s, t = self.seeded()
        cp = backfill(s, t, batch_size=100, max_batches=3)
        self.assertEqual(cp.copied, 300)
        t.faults.error_rate = 1.0
        with self.assertRaises(StoreError):
            backfill(s, t, batch_size=100, checkpoint=cp, row_attempts=2)
        self.assertEqual(cp.copied, 300)  # failed batch did not advance the checkpoint
        t.faults.error_rate = 0.0
        backfill(s, t, batch_size=100, checkpoint=cp)
        self.assertTrue(cp.done)
        self.assertTrue(verify(s, t).consistent)

    def test_does_not_overwrite_newer_dual_written_rows(self):
        s, t = self.seeded(10)
        t.upsert("k00003", {"i": "new"}, version=5)
        s.upsert("k00003", {"i": "new"}, version=5)
        backfill(s, t, batch_size=4)
        record, _ = t.get("k00003")
        self.assertEqual(record.value, {"i": "new"})

    def test_high_water_mark_stops_the_backfill(self):
        s, t = self.seeded(10)
        cp = backfill(s, t, batch_size=3, high_water_key="k00004")
        self.assertEqual((cp.copied, cp.done), (5, True))


class VerifierTests(unittest.TestCase):
    def test_detects_each_kind_of_drift(self):
        s, t = stores()
        s.upsert("a", {"x": 1}, 1)
        s.upsert("b", {"x": 1}, 1)
        t.upsert("b", {"x": 2}, 1)
        t.upsert("c", {"x": 1}, 1)
        report = verify(s, t)
        self.assertFalse(report.consistent)
        self.assertEqual(report.missing_in_target, ["a"])
        self.assertEqual(report.mismatched, ["b"])
        self.assertEqual(report.extra_in_target, ["c"])


class TrafficTests(unittest.TestCase):
    def window(self, n=300, latency=5.0, errors=0):
        w = WindowMetrics()
        for _ in range(n):
            w.record(True, latency)
        for _ in range(errors):
            w.record(False, 0)
        return w

    def test_promotes_through_plan(self):
        c = TrafficController()
        for _ in DEFAULT_PLAN:
            self.assertEqual(c.evaluate(self.window())[0], Decision.PROMOTE)
        self.assertIs(c.status, Status.COMPLETED)

    def test_holds_until_enough_traffic(self):
        c = TrafficController((Phase("canary", 5, min_requests=100),))
        self.assertEqual(c.evaluate(self.window(n=10))[0], Decision.HOLD)

    def test_rolls_back_on_latency_and_errors(self):
        c = TrafficController(slo=SLO(max_p99_ms=50))
        self.assertEqual(c.evaluate(self.window(latency=120))[0], Decision.ROLLBACK)
        self.assertEqual(c.target_weight, 0)
        c = TrafficController(slo=SLO(max_error_rate=0.01))
        self.assertEqual(c.evaluate(self.window(errors=10))[0], Decision.ROLLBACK)


class GitOpsTests(unittest.TestCase):
    def test_renders_weights_and_mirror(self):
        shadow = render_virtualservice(DEFAULT_PLAN[0])
        self.assertIn("mirrorPercentage", shadow)
        self.assertIn("weight: 100", shadow)
        canary = render_virtualservice(Phase("canary-25", 25))
        self.assertIn("weight: 75", canary)
        self.assertIn("weight: 25", canary)
        self.assertNotIn("mirror:", canary)
        rollback = render_virtualservice(Phase("canary-25", 25), rolled_back=True)
        self.assertIn("weight: 0", rollback)


class ConfigTests(unittest.TestCase):
    def test_dsn_from_cloud_run_env(self):
        dsn = postgres_dsn_from_env({"TARGET_DB_HOST": "10.1.2.3", "TARGET_DB_PASSWORD": "pw"})
        self.assertIn("host=10.1.2.3", dsn)
        self.assertIn("sslmode=require", dsn)
        self.assertIsNone(postgres_dsn_from_env({}))


class EndToEndTests(unittest.TestCase):
    def test_happy_path_completes_with_zero_data_loss(self):
        result = run(records=800, seed=3)
        self.assertIs(result.status, Status.COMPLETED)
        self.assertIs(result.final_mode, Mode.TARGET_ONLY)
        self.assertTrue(result.zero_data_loss)

    def test_latency_fault_rolls_back_with_zero_data_loss(self):
        result = run(records=800, seed=3, inject_fault="canary-25")
        self.assertIs(result.status, Status.ROLLED_BACK)
        self.assertIs(result.final_mode, Mode.DUAL_SOURCE_PRIMARY)
        self.assertTrue(result.zero_data_loss)
        self.assertEqual(result.manifests[-1][0], "rollback")

    def test_error_fault_rolls_back_with_zero_data_loss(self):
        result = run(records=800, seed=3, inject_fault="canary-5", fault_kind="errors")
        self.assertIs(result.status, Status.ROLLED_BACK)
        self.assertTrue(result.zero_data_loss)


if __name__ == "__main__":
    unittest.main()
