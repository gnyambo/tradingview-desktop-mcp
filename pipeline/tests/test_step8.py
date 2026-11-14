# -*- coding: utf-8 -*-
"""Tests for the Step-8 forward test plumbing (no network, no Telegram, no database server).
Run from pipeline/:  python3 -m pytest tests -q"""
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
_TMP = tempfile.mkdtemp()
os.environ["TV_PLANS_DIR"] = _TMP  # before common is imported

import common  # noqa: E402
import heartbeat  # noqa: E402
import report  # noqa: E402
import resolver  # noqa: E402
import step8_test  # noqa: E402
import tv_worker  # noqa: E402

M15 = 15 * 60 * 1000


def obs(symbol="XRPUSDT", bias="bearish", run=2, oid=10, at="2026-11-09T10:00:05+00:00", **rec):
    raw = {"stop_s1": 4.10, "stop_s2": 4.08, "s2_entry_reference": 3.95, "s2_sweep_extreme": 4.075,
           "zone_timeframe": "1h", "zone_tf_atr": 0.12, "zone_watch_only": True}
    raw.update(rec)
    return {"id": oid, "scan_run_id": run, "observed_at": at, "symbol": symbol, "primary_state": "ACTIONABLE",
            "trade_bias": bias, "signal_grade": "A", "overall_score": 80.0, "alignment_score": 97.5,
            "macro_context": "ALIGNED", "entry_type": "LIMIT_ZONE", "entry_price": 4.00, "stop_loss": 4.045,
            "target_1": 3.80, "target_2": 3.70, "risk_reward": 4.4, "structure_15m_confirmed": 1,
            "raw_record_json": json.dumps(raw)}


def candles(start_ms, rows):
    return [{"t": start_ms + i * M15, "ct": start_ms + (i + 1) * M15 - 1, "o": o, "h": h, "l": l, "c": c}
            for i, (o, h, l, c) in enumerate(rows)]


class DetectionTests(unittest.TestCase):
    def test_only_new_actionable(self):
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        cols = step8_test.OBS_FIELDS
        con.execute(f"CREATE TABLE symbol_observations ({cols})")
        for o in (obs("AUSDT", run=1, oid=1), obs("AUSDT", run=2, oid=2), obs("BUSDT", run=2, oid=3)):
            con.execute(f"INSERT INTO symbol_observations ({cols}) VALUES ({','.join('?' * len(o))})",
                        [o[k.strip()] for k in cols.split(",")])
        con.execute("INSERT INTO symbol_observations (id, scan_run_id, symbol, primary_state) VALUES (4, 2, 'CUSDT', 'WAITING')")
        self.assertEqual([r["symbol"] for r in step8_test.new_actionable_in_run(con, 1)], ["AUSDT"])
        self.assertEqual([r["symbol"] for r in step8_test.new_actionable_in_run(con, 2)], ["BUSDT"])


class PlanTests(unittest.TestCase):
    def test_build_plan(self):
        pl = step8_test.build_plan(obs())
        self.assertEqual(pl["kind"], "step8")
        self.assertTrue(pl["id"].endswith("_XRPUSDT_S8"))
        s = pl["step8"]
        self.assertEqual((s["direction"], s["entry"], s["sl_s0"], s["sl_s1"], s["sl_s2"]), ("short", 4.0, 4.045, 4.10, 4.08))
        self.assertTrue(s["via_zone_watch"])

    def test_missing_variant_stop_is_none(self):
        pl = step8_test.build_plan(dict(obs(), raw_record_json="not json"))
        self.assertIsNone(pl["step8"]["sl_s1"])
        res = resolver.replay(step8_test.leg_specs(pl)["step8_s1"], [])
        self.assertEqual(res["state"], "NO_LEVELS")

    def test_active_plan_dedupe(self):
        pl = step8_test.build_plan(obs())
        self.assertEqual(step8_test.active_plan_for("XRPUSDT", "bearish", [pl]), pl["id"])
        self.assertIsNone(step8_test.active_plan_for("XRPUSDT", "bullish", [pl]))
        old = dict(pl, created_at_utc=common.fmt_utc(common.utcnow() - timedelta(hours=97)))
        self.assertIsNone(step8_test.active_plan_for("XRPUSDT", "bearish", [old]))
        mtf = dict(pl, kind=None)
        self.assertIsNone(step8_test.active_plan_for("XRPUSDT", "bearish", [mtf]))


class ReplayTests(unittest.TestCase):
    """Same signal, three stops: a sweep to 4.05 stops S0 out; S1 and S2 survive to TP1."""

    def test_three_rules_on_one_signal(self):
        pl = step8_test.build_plan(obs())
        specs = step8_test.leg_specs(pl)
        self.assertEqual(set(specs), set(step8_test.LEGS))
        start = common.to_ms(specs["step8_s0"]["start"]) // M15 * M15 + M15
        cs = candles(start, [(3.95, 3.97, 3.94, 3.96),    # signal: price below the zone
                             (3.96, 4.05, 3.95, 4.01),    # sweep: fills the 4.00 limit, S0 stop 4.045 hit
                             (4.00, 4.01, 3.79, 3.80)])   # falls to TP1 3.80
        s0 = resolver.replay(specs["step8_s0"], cs)
        s1 = resolver.replay(specs["step8_s1"], cs)
        s2 = resolver.replay(dict(specs["step8_s2"], start=common.from_ms(start)), cs)
        self.assertEqual((s0["state"], s0["r"]), ("LOSS", -1.0))
        self.assertEqual((s1["result"], s1["state"]), ("TP1", "OPEN_AFTER_TP1"))  # TP2 3.70 still open
        self.assertAlmostEqual(s1["r"], (4.00 - 3.80) / (4.10 - 4.00), places=3)
        self.assertEqual(s2["result"], "TP1")
        self.assertEqual(s2["fill_price"], 3.95)
        self.assertAlmostEqual(s2["r"], (3.95 - 3.80) / (4.08 - 3.95), places=3)

    def test_resolver_uses_step8_legs_only(self):
        pl = step8_test.build_plan(obs())
        self.assertEqual(set(resolver.leg_specs(pl)), set(step8_test.LEGS))
        mtf = {"source": {"trade_bias": "bearish", "observed_at": "2026-11-09T10:00:05+00:00"}, "tv": None}
        self.assertIn("rule_wide", resolver.leg_specs(mtf))


def done_leg(result, r, fill=4.0, closed="2026-11-09 12:00"):
    return {"final": True, "state": result, "result": result, "r": r, "fill_price": fill, "closed_at": closed,
            "filled_at": "2026-11-09 11:00"}


class ReportTests(unittest.TestCase):
    def test_step8_section_and_kinds_apart(self):
        s8 = step8_test.build_plan(obs())
        s8["created_at_utc"] = "2026-11-09 10:00"
        s8["final"] = True
        s8["legs"] = {"step8_s0": done_leg("LOSS", -1.0), "step8_s1": done_leg("TP1", 2.0),
                      "step8_s2": dict(done_leg("TP1", 1.15), fill_price=3.95)}
        mtf = {"created_at_utc": "2026-11-09 10:30", "source": {"symbol": "XRPUSDT", "trade_bias": "bearish"},
               "legs": {}, "final": False}
        self.assertEqual(report.mark_repeats([s8, mtf]), 0)  # different kinds never repeat each other
        text = "\n".join(report.step8_lines([s8]))
        self.assertIn("signals 1 (found by the zone watch: 1", text)
        self.assertIn("S1 zone-ATR stop", text)
        self.assertIn("not yet - 1/30 resolved trades", text)

    def test_step8_repeat_left_out(self):
        a = step8_test.build_plan(obs())
        a.update(created_at_utc="2026-11-09 10:00", final=False)
        b = step8_test.build_plan(obs(at="2026-11-09T12:00:05+00:00"))
        b.update(created_at_utc="2026-11-09 12:00")
        self.assertEqual(report.mark_repeats([a, b]), 1)
        self.assertIn("repeats left out: 1", "\n".join(report.step8_lines([a, b])))


class HeartbeatTests(unittest.TestCase):
    def test_resolved_since(self):
        since = common.parse_utc("2026-11-09 00:00")
        pl = {"legs": {"step8_s0": done_leg("LOSS", -1), "step8_s2": done_leg("TP1", 1.1),
                       "rule_wide": done_leg("TP1", 1.0), "rule_atr": done_leg("TP1", 1.0),
                       "step8_s1": dict(done_leg("TP1", 2), closed_at="2026-11-08 10:00")}}
        res = heartbeat.resolved_since([pl], since)
        self.assertEqual(res, {"S0": {"LOSS": 1}, "S2": {"TP1": 1}, "rule wide": {"TP1": 1}})
        self.assertEqual(heartbeat.resolved_text(res), "S0 LOSS 1 · S2 TP1 1 · rule wide TP1 1")
        self.assertEqual(heartbeat.resolved_text({}), "none")


class WorkerTests(unittest.TestCase):
    def setUp(self):
        for p in Path(_TMP).glob("*.json"):
            p.unlink()

    def test_process_step8_saves_and_messages_once(self):
        sent = []
        acct = {"equity_usdt": 4685.0, "positions": [], "drawdown_pct": 7.2}
        with mock.patch.object(tv_worker, "telegram_send", side_effect=lambda t: sent.append(t) or True), \
                mock.patch.object(tv_worker, "get_account", return_value=(acct, None)), \
                mock.patch.object(tv_worker, "price_now", return_value=3.95):
            tv_worker.process_step8(obs(), {}, dry_run=False)
            tv_worker.process_step8(obs(), {}, dry_run=False)  # same observation again: skipped
        files = list(Path(_TMP).glob("*_S8.json"))
        self.assertEqual(len(files), 1)
        self.assertEqual(len(sent), 1)
        msg = sent[0]
        self.assertIn("🧪 STEP-8 SIGNAL · XRPUSDT SHORT", msg)
        self.assertIn("Found by the zone watch", msg)
        for v in ("S0 limit 4", "S1 limit 4", "S2 market ≈ 3.95"):
            self.assertIn(v, msg)
        self.assertIn("1% risk of $4,685 = $47", msg)

    def test_step8_plans_do_not_block_mtf_plans(self):
        pl = step8_test.build_plan(obs())
        common.save_json(Path(_TMP) / f"{pl['id']}.json", pl)
        self.assertIsNone(tv_worker.active_plan_for("XRPUSDT", "bearish"))
        self.assertEqual(tv_worker.open_same_direction("short"), [])

    def test_message_without_stops(self):
        pl = step8_test.build_plan(dict(obs(), raw_record_json="{}"))
        text = step8_test.message(pl, None, 47.0, "1% risk")
        self.assertIn("S1 limit · no valid stop (not tracked)", text)
        self.assertNotIn("Found by the zone watch", text)


def shadow_obs(**kw):
    o = obs(**kw)
    raw = json.loads(o["raw_record_json"])
    raw.update(shadow_sequence=True, shadow_blockers=["volume"])
    return dict(o, primary_state="WAITING", raw_record_json=json.dumps(raw))


class ShadowTests(unittest.TestCase):
    def setUp(self):
        for p in Path(_TMP).glob("*.json"):
            p.unlink()

    def test_only_new_shadows(self):
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        cols = step8_test.OBS_FIELDS
        con.execute(f"CREATE TABLE symbol_observations ({cols})")
        rows = (shadow_obs(symbol="AUSDT", run=1, oid=1), shadow_obs(symbol="AUSDT", run=2, oid=2),
                shadow_obs(symbol="BUSDT", run=2, oid=3), dict(obs("CUSDT", run=2, oid=4), primary_state="WAITING"))
        for o in rows:
            con.execute(f"INSERT INTO symbol_observations ({cols}) VALUES ({','.join('?' * len(o))})",
                        [o[k.strip()] for k in cols.split(",")])
        self.assertEqual([r["symbol"] for r in step8_test.new_shadow_in_run(con, 1)], ["AUSDT"])
        self.assertEqual([r["symbol"] for r in step8_test.new_shadow_in_run(con, 2)], ["BUSDT"])   # A repeats, C is not one
        self.assertEqual(step8_test.new_actionable_in_run(con, 2), [])

    def test_shadow_plan_fields(self):
        pl = step8_test.build_plan(shadow_obs(), shadow=True)
        self.assertTrue(pl["id"].endswith("_XRPUSDT_S8X"))
        self.assertEqual((pl["kind"], pl["shadow"], pl["shadow_blockers"]), ("step8", True, ["volume"]))
        self.assertEqual(set(resolver.leg_specs(pl)), set(step8_test.LEGS))
        real = step8_test.build_plan(obs())
        self.assertEqual((real["shadow"], real["shadow_blockers"]), (False, []))

    def test_shadow_is_silent_and_never_blocks_a_real_signal(self):
        sent = []
        with mock.patch.object(tv_worker, "telegram_send", side_effect=lambda t: sent.append(t) or True), \
                mock.patch.object(tv_worker, "get_account", return_value=(None, None)), \
                mock.patch.object(tv_worker, "price_now", return_value=3.95):
            tv_worker.process_step8(shadow_obs(), {}, dry_run=False, shadow=True)
            self.assertEqual((len(list(Path(_TMP).glob("*_S8X.json"))), sent), (1, []))
            tv_worker.process_step8(obs(at="2026-11-09T10:15:05+00:00"), {}, dry_run=False)        # real signal later
            self.assertEqual((len(list(Path(_TMP).glob("*_S8.json"))), len(sent)), (1, 1))
            tv_worker.process_step8(shadow_obs(at="2026-11-09T10:30:05+00:00"), {}, dry_run=False, shadow=True)
        self.assertEqual(len(list(Path(_TMP).glob("*_S8X.json"))), 1)                             # a plan is already running

    def test_report_keeps_the_groups_apart(self):
        real = step8_test.build_plan(obs())
        real.update(created_at_utc="2026-11-14 10:00", final=True,
                    legs={"step8_s0": done_leg("TP1", 2.0), "step8_s1": done_leg("TP1", 1.0), "step8_s2": done_leg("TP1", 1.0)})
        sh = step8_test.build_plan(shadow_obs(at="2026-11-14T11:00:05+00:00"), shadow=True)
        sh.update(created_at_utc="2026-11-14 11:00", final=True,
                  legs={"step8_s0": done_leg("LOSS", -1.0), "step8_s1": done_leg("LOSS", -1.0), "step8_s2": done_leg("LOSS", -1.0)})
        self.assertEqual(report.mark_repeats([real, sh]), 0)
        main, shadow = "\n".join(report.step8_lines([real, sh])), "\n".join(report.step8_lines([real, sh], shadow=True))
        self.assertIn("signals 1 ", main)
        self.assertIn("TP1   1 SL   0", main)
        self.assertIn("setups 1 (rejected for: volume 1", shadow)
        self.assertIn("TP1   0 SL   1", shadow)
        self.assertNotIn("VERDICT", shadow)
        self.assertEqual(heartbeat.resolved_text(heartbeat.resolved_since([real, sh], common.parse_utc("2026-11-09 00:00"))),
                         "S0 TP1 1 · S1 TP1 1 · S2 TP1 1 · shadow S0 LOSS 1 · shadow S1 LOSS 1 · shadow S2 LOSS 1")


def wide_plan(created, result, r, direction="bearish", closed=None):
    leg = dict(done_leg(result, r, fill=100.0, closed=closed or created), levels={"sl": 102.0 if direction == "bearish" else 98.0})
    return {"created_at_utc": created, "source": {"symbol": f"S{created}", "trade_bias": direction}, "legs": {"rule_wide": leg}}


class RuleWideTests(unittest.TestCase):
    def test_test1_frozen_between_not_counted_test2_counts(self):
        plans = [wide_plan("2026-11-01 10:00", "TP1", 1.0),            # test 1: frozen text, not recomputed
                 wide_plan("2026-11-10 10:00", "TP1", 1.0),            # between the tests
                 wide_plan("2026-11-12 10:00", "TP1", 1.0),
                 wide_plan("2026-11-14 12:00", "LOSS", -1.0),          # test 2
                 wide_plan("2026-11-15 12:00", "TP1", 1.0, "bullish")]
        text = "\n".join(report.rule_wide_lines(plans))
        self.assertIn("Test 1 (2026-10-26 to 2026-11-09): FAILED at its 30-trade mark", text)
        self.assertRegex(text, r"between tests\s+plans\s+2 .*TP1\s+2 SL\s+0.*not counted")
        self.assertIn("alerts 2 ", text)
        self.assertRegex(text, r"rule wide, test 2\s+plans\s+2 .*TP1\s+1 SL\s+1")
        self.assertIn("VERDICT (rule wide, test 2): not yet - 2/60 resolved trades, 1/10 longs", text)

    def test_verdict_is_judged_on_the_first_n_only(self):
        early = [-1.0] * 6 + [1.0] * 4                 # first 10: -0.2R
        late = [1.0] * 30                               # a later winning run cannot rescue it
        self.assertTrue(report.verdict(early + late, 12, n_required=10, min_longs=10).startswith("FAIL - avg -0.200R"))
        self.assertTrue(report.verdict([1.0] * 10 + [-1.0] * 30, 12, n_required=10, min_longs=10).startswith("PASS"))
        self.assertIn("only 3 longs", report.verdict([1.0] * 10, 3, n_required=10, min_longs=10))
        self.assertEqual(report.verdict([1.0] * 9, 3, n_required=10, min_longs=10), "not yet - 9/10 resolved trades, 3/10 longs")

    def test_test2_orders_trades_by_when_they_resolved(self):
        plans = [wide_plan("2026-11-14 12:00", "TP1", 1.0, closed="2026-11-16 09:00"),     # created first, resolved last
                 wide_plan("2026-11-14 13:00", "LOSS", -1.0, closed="2026-11-14 14:00"),
                 wide_plan("2026-11-14 14:00", "LOSS", -1.0, closed="2026-11-14 15:00")]
        with mock.patch.object(report, "TEST2_N", 2), mock.patch.object(report, "TEST2_MIN_LONGS", 0):
            text = "\n".join(report.rule_wide_lines(plans))
        self.assertIn("VERDICT (rule wide, test 2): FAIL - avg -1.", text)                 # the two losses came first


if __name__ == "__main__":
    unittest.main()
