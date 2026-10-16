from dataclasses import FrozenInstanceError
from datetime import timedelta
import unittest
from unittest.mock import MagicMock

from subconscious.event_bus import EventBus, ObservationEvent


class EventBusTests(unittest.TestCase):
    def test_observation_has_source_content_unique_id_and_utc_time(self):
        first = ObservationEvent(source="signal", content="A message arrived")
        second = ObservationEvent(source="camera", content="Someone entered")
        self.assertEqual(first.source, "signal")
        self.assertEqual(first.content, "A message arrived")
        self.assertNotEqual(first.event_id, second.event_id)
        self.assertEqual(first.occurred_at.utcoffset(), timedelta(0))
        with self.assertRaises(FrozenInstanceError):
            first.content = "Changed"

    def test_events_route_to_matching_type_only(self):
        bus = EventBus()
        observations = MagicMock()
        other_events = MagicMock()
        bus.subscribe(ObservationEvent, observations)
        bus.subscribe(str, other_events)
        event = ObservationEvent(source="signal", content="A message arrived")
        self.assertTrue(bus.publish(event))
        observations.assert_called_once_with(event)
        other_events.assert_not_called()
        self.assertTrue(bus.publish("Other event"))
        other_events.assert_called_once_with("Other event")
        self.assertFalse(bus.publish(42))

    def test_multiple_subscribers_receive_events_in_publication_order(self):
        bus = EventBus()
        calls = []
        bus.subscribe(ObservationEvent, lambda event: calls.append(("attention", event.source)))
        bus.subscribe(ObservationEvent, lambda event: calls.append(("other", event.source)))
        for source in ("signal", "camera", "screen"):
            bus.publish(ObservationEvent(source=source, content="Observation"))
        self.assertEqual(calls, [
            ("attention", "signal"), ("other", "signal"),
            ("attention", "camera"), ("other", "camera"),
            ("attention", "screen"), ("other", "screen"),
        ])

    def test_unsubscribe_is_idempotent_and_removes_only_its_registration(self):
        bus = EventBus()
        handler = MagicMock()
        unsubscribe = bus.subscribe(ObservationEvent, handler)
        bus.subscribe(ObservationEvent, handler)
        unsubscribe()
        unsubscribe()
        event = ObservationEvent(source="test", content="Test")
        self.assertTrue(bus.publish(event))
        handler.assert_called_once_with(event)

    def test_subscription_changes_take_effect_on_next_publication(self):
        bus = EventBus()
        later = MagicMock()
        first = MagicMock(side_effect=lambda event: bus.subscribe(ObservationEvent, later))
        unsubscribe = bus.subscribe(ObservationEvent, first)
        event = ObservationEvent(source="test", content="Test")
        bus.publish(event)
        later.assert_not_called()
        unsubscribe()
        bus.publish(event)
        later.assert_called_once_with(event)

    def test_subscriber_failure_does_not_stop_other_subscribers_or_retry(self):
        error_handler = MagicMock()
        bus = EventBus(on_error=error_handler)
        error = OSError("private error details")
        broken = MagicMock(side_effect=error)
        healthy = MagicMock()
        bus.subscribe(ObservationEvent, broken)
        bus.subscribe(ObservationEvent, healthy)
        event = ObservationEvent(source="test", content="private event text")
        self.assertTrue(bus.publish(event))
        broken.assert_called_once_with(event)
        healthy.assert_called_once_with(event)
        error_handler.assert_called_once_with(error)

    def test_default_error_logging_omits_private_text(self):
        bus = EventBus()
        bus.subscribe(ObservationEvent, MagicMock(side_effect=ValueError("private details")))
        with self.assertLogs("subconscious.event_bus", level="WARNING") as logs:
            self.assertFalse(bus.publish(ObservationEvent(source="test", content="private event")))
        self.assertIn("ValueError", "\n".join(logs.output))
        self.assertNotIn("private", "\n".join(logs.output))

    def test_close_prevents_publications_and_new_subscriptions(self):
        bus = EventBus()
        handler = MagicMock()
        unsubscribe = bus.subscribe(ObservationEvent, handler)
        bus.close()
        bus.close()
        unsubscribe()
        self.assertFalse(bus.publish(ObservationEvent(source="test", content="Test")))
        handler.assert_not_called()
        with self.assertRaises(RuntimeError):
            bus.subscribe(ObservationEvent, handler)

    def test_close_during_delivery_stops_remaining_subscribers(self):
        bus = EventBus()
        later = MagicMock()
        bus.subscribe(ObservationEvent, lambda event: bus.close())
        bus.subscribe(ObservationEvent, later)
        bus.publish(ObservationEvent(source="test", content="Test"))
        later.assert_not_called()