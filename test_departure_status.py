from __future__ import annotations

import unittest

from app.departure_status import (
    DepartureTracker, apply_departure_message, grace_seconds, origin_departure_at, schedule_context, timestamp,
)


def at(clock: str, date: str = "2026-10-26") -> int:
    return timestamp(f"{date}T{clock}:00+08:00")


def timetable(clock: str, *, direction=0, origin="A", days=None, trip="trip") -> dict:
    return {
        "direction": direction, "kind": "timetable",
        "service_days": days or {day: 1 for day in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")},
        "payload": {"trip_id": trip, "stop_times": [
            {"seq": 1, "stopid": origin, "departure": clock},
            {"seq": 2, "stopid": "B", "departure": "11:00"},
        ]},
    }


class DepartureStatusTests(unittest.TestCase):
    def setUp(self):
        self.tracker = DepartureTracker()
        self.paths = {
            0: {"stops": [{"seq": 1, "stopid": "A"}, {"seq": 2, "stopid": "B"}]},
            1: {"stops": [{"seq": 1, "stopid": "C"}]},
        }
        self.rows = [timetable("10:00"), timetable("10:20"), timetable("10:40")]

    def update(self, items, clock="10:05"):
        return self.tracker.update("TXG500", self.paths, items, now_ts=at(clock), load_schedules=lambda: self.rows)

    def item(self, **extra):
        return {"StopID": "A", "Direction": 0, "StopStatus": 1,
                "ScheduledTime": "10:00", "UpdateTime": "2026-10-26T10:05:00+08:00", **extra}

    def test_counts_from_origin_and_preserves_source_time(self):
        result = self.update([self.item()])[0]
        self.assertEqual(result["scheduled_at"], at("10:00"))
        self.assertEqual(result["observed_at"], at("10:05"))
        self.assertEqual(result["status"], "not_departed")
        self.assertEqual(result["grace_seconds"], 600)
        self.assertEqual(result["next_scheduled_at"], at("10:20"))

    def test_downstream_and_opposite_direction_do_not_identify_departure(self):
        self.assertEqual(self.update([self.item(StopID="B")]), {})
        self.assertEqual(self.update([self.item(Direction=1)]), {})

    def test_missing_gps_or_frequency_does_not_invent_a_trip(self):
        self.rows = [{"direction": 0, "kind": "frequency", "service_days": {"mon": 1},
                      "payload": {"start": "05:00", "end": "23:00", "max_headway": 10}}]
        self.assertEqual(self.update([]), {})
        self.assertEqual(self.update([self.item(ScheduledTime=None)]), {})
        result = self.update([self.item()])[0]
        self.assertEqual(result["grace_seconds"], 300)
        self.assertIsNone(result["next_scheduled_at"])

    def test_feed_advance_keeps_previous_trip_without_claiming_cancellation(self):
        self.update([self.item()], "10:05")
        pending = self.update([self.item(ScheduledTime="10:20", UpdateTime="2026-10-26T10:06:00+08:00")], "10:06")[0]
        self.assertEqual(pending["scheduled_at"], at("10:00"))
        self.assertEqual(pending["next_scheduled_at"], at("10:20"))
        self.assertEqual(pending["observed_at"], at("10:05"))
        self.assertEqual(pending["status"], "unconfirmed")
        next_trip = self.update([self.item(ScheduledTime="10:20", UpdateTime="2026-10-26T10:21:00+08:00")], "10:21")[0]
        self.assertEqual(next_trip["scheduled_at"], at("10:20"))

    def test_missing_observation_is_unknown_and_does_not_restart_clock(self):
        self.update([self.item()])
        pending = self.update([], "10:08")[0]
        self.assertEqual(pending["scheduled_at"], at("10:00"))
        self.assertEqual(pending["observed_at"], at("10:05"))
        self.assertEqual(pending["status"], "unconfirmed")

    def test_old_trip_position_is_not_departure_evidence(self):
        self.update([self.item()])
        result = self.update([{"StopID": "B", "Direction": 0, "EstimateTime": 0,
                               "PlateNumb": "LOOP-500", "UpdateTime": "2026-10-26T10:06:00+08:00"}], "10:06")
        self.assertEqual(result[0]["scheduled_at"], at("10:00"))
        self.assertEqual(result[0]["status"], "unconfirmed")

    def test_service_end_overrides_old_pending_trip(self):
        self.update([self.item()])
        self.assertEqual(self.update([self.item(StopStatus=3)]), {})
        self.assertEqual(self.update([]), {})

    def test_grace_period_scales_but_is_bounded(self):
        self.assertEqual(grace_seconds(None), 300)
        self.assertEqual(grace_seconds(300), 180)
        self.assertEqual(grace_seconds(1200), 600)
        self.assertEqual(grace_seconds(7200), 900)

    def test_midnight_uses_source_date_and_extended_service_hours(self):
        item = self.item(ScheduledTime="23:58", UpdateTime="2026-10-27T00:03:00+08:00")
        self.assertEqual(origin_departure_at(item, at("00:03", "2026-10-27")), at("23:58"))
        item = self.item(ScheduledTime="24:10", UpdateTime="2026-10-26T23:55:00+08:00")
        self.assertEqual(origin_departure_at(item, at("23:55")), at("00:10", "2026-10-27"))
        item["NextBusTime"] = "2026-10-27T00:20:00+08:00"
        self.assertEqual(origin_departure_at(item, at("23:55")), at("00:10", "2026-10-27"))
        item.pop("ScheduledTime")
        self.assertEqual(origin_departure_at(item, at("23:55")), at("00:20", "2026-10-27"))

    def test_next_bus_time_does_not_replace_current_scheduled_time(self):
        pending = self.update([self.item(NextBusTime="2026-10-26T10:20:00+08:00")])[0]
        self.assertEqual(pending["scheduled_at"], at("10:00"))
        self.assertEqual(pending["next_scheduled_at"], at("10:20"))

    def test_reported_origin_pair_supplies_headway_without_static_timetable(self):
        self.rows = []
        pending = self.update([self.item(NextBusTime="2026-10-26T10:20:00+08:00")])[0]
        self.assertEqual(pending["grace_seconds"], 600)
        self.assertEqual(pending["next_scheduled_at"], at("10:20"))

    def test_legacy_message_counts_up_and_stale_data_is_not_confirmed(self):
        departure = self.update([self.item()])[0]
        stop = {"departure": departure, "eta": None}
        apply_departure_message(stop, at("10:05"))
        self.assertEqual(stop["message"], "未發車 5分")
        apply_departure_message(stop, at("10:06"))
        self.assertEqual(stop["message"], "未發車 6分")
        apply_departure_message(stop, at("10:07"))
        self.assertEqual(stop["message"], "未確認 逾7分")
        apply_departure_message(stop, at("10:10"))
        self.assertEqual(stop["message"], "下班表定 10:20（前班動態未確認）")
        stop.update(eta=720, updated_at=at("10:12"))
        apply_departure_message(stop, at("10:12"))
        self.assertEqual(stop["eta"], 720)
        self.assertEqual(stop["message"], "")
        apply_departure_message(stop, at("10:14"))
        self.assertIsNone(stop["eta"])
        self.assertEqual(departure["observed_at"], at("10:05"))

    def test_next_trip_respects_origin_direction_and_service_day(self):
        rows = [timetable("10:00"), timetable("10:05", direction=1),
                timetable("10:10", days={"tue": 1}), timetable("10:15", origin="X"), timetable("10:20")]
        context = schedule_context(rows, 0, "A", at("10:00"))
        self.assertEqual(context["next_scheduled_at"], at("10:20"))
        self.assertEqual(context["grace_seconds"], 600)

    def test_next_trip_can_cross_midnight_but_never_repeat_a_past_time(self):
        context = schedule_context([timetable("23:58"), timetable("24:10")], 0, "A", at("23:58"))
        self.assertEqual(context["next_scheduled_at"], at("00:10", "2026-10-27"))
        context = schedule_context(self.rows, 0, "A", at("10:00"), now_ts=at("10:21"))
        self.assertEqual(context["next_scheduled_at"], at("10:40"))
        self.assertEqual(context["grace_seconds"], 600)

    def test_stale_origin_report_never_resets_to_receipt_time(self):
        result = self.update([self.item(UpdateTime="2026-10-26T09:59:00+08:00")])[0]
        self.assertEqual(result["observed_at"], at("09:59"))
        self.assertEqual(result["scheduled_at"], at("10:00"))


if __name__ == "__main__":
    unittest.main()
