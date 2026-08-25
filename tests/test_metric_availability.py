"""P22：指标可用性语义、窗口补采与公平批处理。"""

from __future__ import annotations

import logging
import math
import threading
from collections import UserDict
from collections.abc import Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import Any

import pytest
from sqlalchemy import event, insert, inspect, select, text
from sqlalchemy.exc import OperationalError, StatementError
from sqlalchemy.orm import Session

import metrics.collector as collector_module
from core import db
from core.models import ContentItem, MetricCollectionAttempt, MetricSnapshot, PublishRecord, new_id
from core.state_machine import ContentStatus, PublishPhase
from core.stats import build_dashboard
from metrics.availability import (
    MetricsPayloadKind,
    _copy_json_value,
    classify_metrics_payload,
    normalize_metrics_payload,
)
from metrics.collector import AttemptOutcome, MetricDatabaseError, collect_all
from metrics.insights import collect_summary
from publishers.base import FakePublisher, RetryableError
from publishers.registry import register, use_fake_publishers
from tests.conftest import make_account, make_item

NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)


class _SingleItemsMapping(Mapping[str, object]):
    """仅允许一次 items 快照；其它 Mapping 协议若被触达就显式失败。"""

    def __init__(self, pairs: list[object]) -> None:
        self.pairs = pairs
        self.items_calls = 0

    def items(self):  # type: ignore[override,no-untyped-def]
        self.items_calls += 1
        if self.items_calls > 1:
            raise RuntimeError("ITEMS_CALLED_TWICE_SECRET")
        return iter(self.pairs)

    def __getitem__(self, key: str) -> object:
        raise AssertionError(f"__getitem__ must not be used: {key}")

    def __iter__(self) -> Iterator[str]:
        raise AssertionError("__iter__ must not be used")

    def __len__(self) -> int:
        raise AssertionError("__len__ must not be used")

    def keys(self):  # type: ignore[no-untyped-def]
        raise AssertionError("keys must not be used")

    def get(self, key: str, default: object = None) -> object:
        raise AssertionError(f"get must not be used: {key}")


class _PairOnce:
    def __init__(self, values: list[object]) -> None:
        self.values = values
        self.materializations = 0

    def __iter__(self) -> Iterator[object]:
        self.materializations += 1
        if self.materializations > 1:
            raise RuntimeError("PAIR_MATERIALIZED_TWICE_SECRET")
        return iter(self.values)


class _BadLengthHintIterator:
    def __init__(self, values: list[object]) -> None:
        self.values = iter(values)

    def __iter__(self) -> _BadLengthHintIterator:
        return self

    def __next__(self) -> object:
        return next(self.values)

    def __length_hint__(self) -> int:
        raise RuntimeError("LENGTH_HINT_SECRET")


class _BadIterCreation:
    def __iter__(self) -> Iterator[object]:
        raise RuntimeError("ITERATOR_CREATION_SECRET")


class _BadItemsMapping(_SingleItemsMapping):
    def __init__(self, mode: str) -> None:
        super().__init__([])
        self.mode = mode

    def items(self):  # type: ignore[override,no-untyped-def]
        self.items_calls += 1
        if self.mode == "items":
            raise RuntimeError("ITEMS_SECRET")
        if self.mode == "iterator_creation":
            return _BadIterCreation()
        if self.mode == "iteration":
            return _RaisesOnNext()
        if self.mode == "length_hint":
            return _BadLengthHintIterator([])
        raise AssertionError(f"unknown mode: {self.mode}")


class _RaisesOnNext:
    def __iter__(self) -> _RaisesOnNext:
        return self

    def __next__(self) -> object:
        raise RuntimeError("ITERATION_SECRET")


class _DirectBaseException(BaseException):
    pass


class ScriptedMetricsPublisher(FakePublisher):
    """让采集器测试可精确观察主路径和标题兜底的返回。"""

    def __init__(
        self,
        *,
        responses: list[object],
        fallback_responses: list[object] | None = None,
    ) -> None:
        super().__init__("availability-account", platform="xhs")
        self.responses = responses
        self.fallback_responses = fallback_responses or []
        self.fetch_post_ids: list[str] = []
        self.fallback_titles: list[str] = []
        self.health_calls = 0

    @staticmethod
    def _at(values: list[object], index: int) -> object:
        value = values[min(index, len(values) - 1)]
        return dict(value) if isinstance(value, dict) else value

    def fetch_metrics(self, platform_post_id: str) -> object:
        self.fetch_post_ids.append(platform_post_id)
        return self._at(self.responses, len(self.fetch_post_ids) - 1)

    def fetch_metrics_for_title(self, title: str) -> object:
        self.fallback_titles.append(title)
        return self._at(self.fallback_responses, len(self.fallback_titles) - 1)

    def health(self):  # type: ignore[no-untyped-def]
        self.health_calls += 1
        return super().health()


class FailingMetricsPublisher(FakePublisher):
    def __init__(self) -> None:
        super().__init__("availability-account", platform="xhs")
        self.fetch_calls = 0
        self.health_calls = 0

    def fetch_metrics(self, platform_post_id: str) -> dict[str, Any]:
        self.fetch_calls += 1
        raise RetryableError(f"EXCEPTION_SECRET for {platform_post_id}")

    def health(self):  # type: ignore[no-untyped-def]
        self.health_calls += 1
        return super().health()


class ConcurrentMetricsPublisher(FakePublisher):
    def __init__(self, barrier: threading.Barrier) -> None:
        super().__init__("availability-account", platform="xhs")
        self.barrier = barrier
        self.lock = threading.Lock()
        self.fetch_post_ids: list[str] = []

    def fetch_metrics(self, platform_post_id: str) -> dict[str, Any]:
        with self.lock:
            self.fetch_post_ids.append(platform_post_id)
        # claim 已提交且没有持有写事务时，另一 tick 才能认领另一候选走到这里。
        self.barrier.wait(timeout=5)
        return {"available": False}


class HealthFailingPublisher(ScriptedMetricsPublisher):
    def health(self):  # type: ignore[no-untyped-def]
        self.health_calls += 1
        raise RuntimeError("HEALTH_SECRET")


class FallbackFailingPublisher(ScriptedMetricsPublisher):
    def fetch_metrics_for_title(self, title: str) -> object:
        self.fallback_titles.append(title)
        raise RuntimeError("FALLBACK_EXCEPTION_SECRET")


class PostIdMetricsPublisher(FakePublisher):
    """按 post id 选 payload，避免候选 SQL 排序影响安全用例。"""

    def __init__(self, responses: dict[str, object]) -> None:
        super().__init__("availability-account", platform="xhs")
        self.responses = responses

    def fetch_metrics(self, platform_post_id: str) -> object:
        return self.responses[platform_post_id]


class _MutatingSnapshotMapping(_SingleItemsMapping):
    """items 返回固定快照后修改自己，模拟无异常的源端并发变更。"""

    def items(self):  # type: ignore[override,no-untyped-def]
        self.items_calls += 1
        if self.items_calls > 1:
            raise RuntimeError("ITEMS_CALLED_TWICE_SECRET")
        snapshot = list(self.pairs)
        self.pairs.append(("late", "must-not-appear"))
        return iter(snapshot)


def _published(
    session,
    account,
    *,
    published_at: datetime,
    post_id: str = "post-1",
    title: str = "敏感测试标题",
):
    item = make_item(session, account, status=ContentStatus.PUBLISHED.value, title=title)
    record = PublishRecord(
        id=new_id("pub"),
        content_item_id=item.id,
        idem_key=new_id("idem"),
        phase=PublishPhase.DONE.value,
        platform_post_id=post_id,
        attempts=1,
        created_at=published_at,
        updated_at=published_at,
    )
    session.add(record)
    session.flush()
    # ``updated_at`` 有 onupdate，按窗口锚点写回，避免 flush 变成墙钟时间。
    record.updated_at = published_at
    session.flush()
    return item, record


def _register(publisher: FakePublisher) -> None:
    register("xhs", lambda _account_id, **_kwargs: publisher)


def _first_candidate(moment: datetime = NOW):
    read_session = db.get_session_factory()()
    try:
        stats = {
            "scanned": 0,
            "skipped": 0,
            "not_due": 0,
            "history_unavailable": 0,
            "history_malformed": 0,
        }
        candidates = collector_module._collect_candidates(
            read_session,
            moment=moment,
            respect_windows=True,
            stats=stats,
        )
        return candidates[0]
    finally:
        read_session.close()


def test_metrics_payload_classification_is_total_and_identity_based():
    assert (
        classify_metrics_payload({"available": False}) is MetricsPayloadKind.EXPLICITLY_UNAVAILABLE
    )
    assert classify_metrics_payload({}) is MetricsPayloadKind.USABLE
    for value in (0, None, "", [], True):
        assert classify_metrics_payload({"available": value}) is MetricsPayloadKind.USABLE
    for value in (None, [], "bad", 0, 1.5):
        assert classify_metrics_payload(value) is MetricsPayloadKind.MALFORMED


def test_normalizer_copies_supported_mapping_containers_and_exact_scalars():
    shared = {"inner": (None, "x", True, 7, 2.5)}
    payload = UserDict(
        {
            "proxy": MappingProxyType({"shared": shared}),
            "again": shared,
            "custom": _SingleItemsMapping([("tuple", ("a", "b"))]),
        }
    )

    kind, copied = normalize_metrics_payload(payload)

    assert kind is MetricsPayloadKind.USABLE
    assert copied == {
        "proxy": {"shared": {"inner": [None, "x", True, 7, 2.5]}},
        "again": {"inner": [None, "x", True, 7, 2.5]},
        "custom": {"tuple": ["a", "b"]},
    }
    assert type(copied) is dict
    assert type(copied["proxy"]) is dict
    assert copied["proxy"] is not copied["again"]


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        (),
        "not-a-mapping",
        {1: "top-int"},
        {True: "top-bool"},
        {"nested": {1: "nested-int"}},
        {"nested": {False: "nested-bool"}},
        {"value": math.nan},
        {"value": math.inf},
        {"value": -math.inf},
        {"value": object()},
        {"value": {1, 2}},
    ],
)
def test_normalizer_rejects_non_json_or_non_exact_key_payloads(payload: object):
    kind, copied = normalize_metrics_payload(payload)
    assert kind is MetricsPayloadKind.MALFORMED
    assert copied is None


def test_normalizer_rejects_key_and_scalar_subclasses_and_duplicate_exact_keys():
    class StringSubclass(str):
        pass

    class IntSubclass(int):
        pass

    assert normalize_metrics_payload({StringSubclass("key"): 1}) == (
        MetricsPayloadKind.MALFORMED,
        None,
    )
    assert normalize_metrics_payload({"value": IntSubclass(1)}) == (
        MetricsPayloadKind.MALFORMED,
        None,
    )
    duplicate = _SingleItemsMapping([("same", 1), ("same", 2)])
    assert normalize_metrics_payload(duplicate) == (MetricsPayloadKind.MALFORMED, None)
    assert duplicate.items_calls == 1


def test_normalizer_rejects_cycles_but_allows_shared_references():
    self_cycle: dict[str, object] = {}
    self_cycle["self"] = self_cycle
    indirect: list[object] = []
    nested = {"child": indirect}
    indirect.append(nested)
    shared = {"value": 1}

    assert normalize_metrics_payload(self_cycle) == (MetricsPayloadKind.MALFORMED, None)
    assert normalize_metrics_payload({"indirect": indirect}) == (
        MetricsPayloadKind.MALFORMED,
        None,
    )
    assert normalize_metrics_payload({"one": shared, "two": shared}) == (
        MetricsPayloadKind.USABLE,
        {"one": {"value": 1}, "two": {"value": 1}},
    )


def test_copy_helper_cleans_external_active_set_after_recursive_failure():
    active: set[int] = set()

    with pytest.raises(TypeError):
        _copy_json_value({"nested": {"invalid": object()}}, active)

    assert active == set()


def test_normalizer_snapshots_all_pairs_before_recursing_into_any_child():
    class ChildThatMutatesSecondPair(Mapping[str, object]):
        def __init__(self, second_pair: _PairOnce) -> None:
            self.second_pair = second_pair
            self.items_calls = 0

        def items(self):  # type: ignore[override,no-untyped-def]
            self.items_calls += 1
            self.second_pair.values[1] = "AFTER"
            return iter([("child", "ok")])

        def __getitem__(self, key: str) -> object:
            raise AssertionError(f"__getitem__ must not be used: {key}")

        def __iter__(self) -> Iterator[str]:
            raise AssertionError("__iter__ must not be used")

        def __len__(self) -> int:
            raise AssertionError("__len__ must not be used")

    second_pair = _PairOnce(["second", "BEFORE"])
    first_child = ChildThatMutatesSecondPair(second_pair)
    first_pair = _PairOnce(["first", first_child])
    source = _SingleItemsMapping([first_pair, second_pair])

    kind, copied = normalize_metrics_payload(source)

    assert (kind, copied) == (
        MetricsPayloadKind.USABLE,
        {"first": {"child": "ok"}, "second": "BEFORE"},
    )
    assert source.items_calls == first_child.items_calls == 1
    assert first_pair.materializations == second_pair.materializations == 1


@pytest.mark.parametrize("mode", ["items", "iterator_creation", "iteration", "length_hint"])
def test_normalizer_treats_mapping_items_protocol_exceptions_as_malformed(mode: str):
    kind, copied = normalize_metrics_payload(_BadItemsMapping(mode))
    assert kind is MetricsPayloadKind.MALFORMED
    assert copied is None


@pytest.mark.parametrize(
    "pair",
    [
        object(),
        ("only",),
        ("one", 1, "extra"),
        _RaisesOnNext(),
        _BadLengthHintIterator(["key", 1]),
    ],
)
def test_normalizer_treats_bad_pairs_as_malformed(pair: object):
    kind, copied = normalize_metrics_payload(_SingleItemsMapping([pair]))
    assert kind is MetricsPayloadKind.MALFORMED
    assert copied is None


def test_normalizer_materializes_mapping_items_and_each_pair_once():
    pair = _PairOnce(["tuple", ("one", "two")])
    payload = _SingleItemsMapping([pair])

    kind, copied = normalize_metrics_payload(payload)

    assert (kind, copied) == (MetricsPayloadKind.USABLE, {"tuple": ["one", "two"]})
    assert payload.items_calls == 1
    assert pair.materializations == 1


@pytest.mark.parametrize(
    "exception_type",
    [_DirectBaseException, KeyboardInterrupt, SystemExit, GeneratorExit],
)
def test_normalizer_propagates_base_exceptions(exception_type: type[BaseException]):
    class RaisingMapping(_SingleItemsMapping):
        def items(self):  # type: ignore[override,no-untyped-def]
            raise exception_type()

    with pytest.raises(exception_type):
        normalize_metrics_payload(RaisingMapping([]))


def test_normalizer_keeps_a_payload_larger_than_five_mib_exactly():
    text_value = "测" * ((5 * 1024 * 1024) // len("测"))
    kind, copied = normalize_metrics_payload({"text": text_value})
    assert kind is MetricsPayloadKind.USABLE
    assert copied == {"text": text_value}


def test_collect_all_uses_one_safe_snapshot_and_ignores_later_source_mutation(session):
    account = make_account(session, account_id="availability-account")
    _published(session, account, published_at=NOW - timedelta(hours=25), post_id="snapshot-once")
    session.commit()
    pair = _PairOnce(["nested", ("one", "two")])
    source = _MutatingSnapshotMapping([pair])
    _register(PostIdMetricsPublisher({"snapshot-once": source}))

    stats = collect_all(now=NOW, respect_windows=True)

    assert stats["attempted"] == stats["snapshots"] == 1
    assert stats["malformed"] == stats["errors"] == 0
    assert source.items_calls == pair.materializations == 1
    source.pairs[0] = ("nested", ("changed",))
    with db.session_scope() as check:
        snapshot = check.scalars(select(MetricSnapshot)).one()
        assert snapshot.metrics_json == {"nested": ["one", "two"]}


def test_collect_all_isolates_mapping_protocol_error_and_continues_other_candidate(session, caplog):
    account = make_account(session, account_id="availability-account")
    bad_item, bad_record = _published(
        session,
        account,
        published_at=NOW - timedelta(hours=25),
        post_id="BAD_POST_ID_SENTINEL",
        title="BAD_TITLE_SENTINEL",
    )
    good_item, _good_record = _published(
        session,
        account,
        published_at=NOW - timedelta(hours=25),
        post_id="good-post",
    )
    session.commit()
    malicious = _BadItemsMapping("items")
    safe = _SingleItemsMapping([("nested", ("safe",))])
    _register(
        PostIdMetricsPublisher(
            {
                "BAD_POST_ID_SENTINEL": malicious,
                "good-post": safe,
            }
        )
    )

    with caplog.at_level(logging.WARNING, logger="social_workflow.metrics"):
        stats = collect_all(now=NOW, respect_windows=True)

    assert stats["attempted"] == 2
    assert stats["malformed"] == stats["snapshots"] == 1
    assert stats["errors"] == stats["unavailable"] == 0
    assert malicious.items_calls == safe.items_calls == 1
    for sentinel in (
        "ITEMS_SECRET",
        "BAD_TITLE_SENTINEL",
        "BAD_POST_ID_SENTINEL",
        "https://example.invalid/URL_SENTINEL",
    ):
        assert sentinel not in caplog.text
    with db.session_scope() as check:
        bad_attempt = check.get(MetricCollectionAttempt, bad_item.id)
        good_attempt = check.get(MetricCollectionAttempt, good_item.id)
        assert bad_attempt is not None and bad_attempt.last_outcome == "malformed"
        assert good_attempt is not None and good_attempt.last_outcome == "success"
        assert check.get(ContentItem, bad_item.id).status == ContentStatus.PUBLISHED.value
        assert check.get(ContentItem, good_item.id).status == ContentStatus.MEASURED.value
        snapshots = list(check.scalars(select(MetricSnapshot)))
        assert len(snapshots) == 1 and snapshots[0].content_item_id == good_item.id
        assert check.get(PublishRecord, bad_record.id).platform_post_id == "BAD_POST_ID_SENTINEL"


def test_collect_all_persists_a_payload_larger_than_five_mib_without_truncating(session):
    account = make_account(session, account_id="availability-account")
    _published(session, account, published_at=NOW - timedelta(hours=25), post_id="large-payload")
    session.commit()
    text_value = "x" * (5 * 1024 * 1024)
    _register(PostIdMetricsPublisher({"large-payload": {"text": text_value}}))

    stats = collect_all(now=NOW, respect_windows=True)

    assert stats["snapshots"] == 1
    with db.session_scope() as check:
        assert check.scalars(select(MetricSnapshot)).one().metrics_json == {"text": text_value}


@pytest.mark.parametrize(
    ("endpoint", "is_api"),
    [
        ("/dev/tick/metrics?respect_windows=true", False),
        ("/api/v1/system/ticks/metrics?respect_windows=true", True),
    ],
)
def test_manual_metrics_ticks_treat_mapping_protocol_exceptions_as_malformed(
    session, client, caplog, endpoint: str, is_api: bool
):
    account = make_account(session, account_id="availability-account")
    _published(
        session,
        account,
        published_at=NOW - timedelta(hours=25),
        post_id="HTTP_POST_ID_SENTINEL",
        title="HTTP_TITLE_SENTINEL",
    )
    session.commit()
    _register(PostIdMetricsPublisher({"HTTP_POST_ID_SENTINEL": _BadItemsMapping("items")}))

    with caplog.at_level(logging.WARNING):
        response = client.post(endpoint)

    assert response.status_code == 200
    body = response.json()
    stats = body["data"]["stats"] if is_api else body["stats"]
    assert stats["malformed"] == 1
    assert stats["errors"] == 0
    for sentinel in ("ITEMS_SECRET", "HTTP_POST_ID_SENTINEL", "HTTP_TITLE_SENTINEL"):
        assert sentinel not in response.text
        assert sentinel not in caplog.text


@pytest.mark.parametrize(
    "payload_factory",
    [
        pytest.param(lambda: {"secret": "OBJECT_SECRET", "value": object()}, id="object"),
        pytest.param(lambda: {"secret": "SET_SECRET", "value": {1, 2}}, id="set"),
        pytest.param(lambda: {"secret": "NAN_SECRET", "value": math.nan}, id="nan"),
        pytest.param(lambda: {"secret": "INF_SECRET", "value": math.inf}, id="infinity"),
        pytest.param(
            lambda: _cyclic_secret_payload("CYCLE_SECRET"),
            id="cycle",
        ),
    ],
)
def test_non_json_mapping_is_malformed_without_payload_leak_or_business_writes(
    session, caplog, payload_factory
):
    account = make_account(session, account_id="availability-account")
    item, record = _published(
        session,
        account,
        published_at=NOW - timedelta(hours=25),
        post_id="xhs-unresolved-json-validation",
    )
    session.commit()
    payload = payload_factory()
    publisher = ScriptedMetricsPublisher(responses=[payload])
    _register(publisher)

    with caplog.at_level(logging.WARNING, logger="social_workflow.metrics"):
        stats = collect_all(now=NOW, respect_windows=True)

    assert stats["attempted"] == stats["malformed"] == 1
    assert stats["snapshots"] == stats["unavailable"] == stats["errors"] == 0
    for secret in ("OBJECT_SECRET", "SET_SECRET", "NAN_SECRET", "INF_SECRET", "CYCLE_SECRET"):
        assert secret not in caplog.text
    with db.session_scope() as check:
        assert check.scalars(select(MetricSnapshot)).all() == []
        saved_item = check.get(ContentItem, item.id)
        saved_record = check.get(PublishRecord, record.id)
        attempt = check.get(MetricCollectionAttempt, item.id)
        assert saved_item is not None and saved_item.status == ContentStatus.PUBLISHED.value
        assert saved_record is not None
        assert saved_record.platform_post_id == "xhs-unresolved-json-validation"
        assert attempt is not None and attempt.last_outcome == "malformed"


def _cyclic_secret_payload(secret: str) -> dict[str, object]:
    payload: dict[str, object] = {"secret": secret}
    payload["self"] = payload
    return payload


def test_unavailable_does_not_persist_or_advance_and_next_tick_retries(session, caplog):
    account = make_account(session, account_id="availability-account")
    item, record = _published(
        session,
        account,
        published_at=NOW - timedelta(hours=25),
        post_id="xhs-unresolved-old",
    )
    session.commit()
    publisher = ScriptedMetricsPublisher(
        responses=[
            {"available": False, "reason": "PRIMARY_SECRET", "platform_post_id": "bad-primary"},
            {"available": True, "views": 42},
        ],
        fallback_responses=[
            {
                "available": False,
                "reason": "FALLBACK_SECRET",
                "platform_post_id": "bad-fallback",
                "raw_metrics": "RAW_METRICS_SECRET",
                "url": "https://example.invalid/URL_SECRET",
            }
        ],
    )
    _register(publisher)

    with caplog.at_level(logging.WARNING, logger="social_workflow.metrics"):
        first = collect_all(now=NOW, respect_windows=True)

    assert first["scanned"] == first["attempted"] == first["unavailable"] == 1
    assert first["snapshots"] == first["malformed"] == first["errors"] == 0
    assert publisher.health_calls == 1, "不可用结果仍应写入独立健康巡检结果"
    assert publisher.fallback_titles == ["敏感测试标题"]
    assert "PRIMARY_SECRET" not in caplog.text
    assert "FALLBACK_SECRET" not in caplog.text
    assert "敏感测试标题" not in caplog.text
    assert "bad-primary" not in caplog.text and "bad-fallback" not in caplog.text
    assert "RAW_METRICS_SECRET" not in caplog.text and "URL_SECRET" not in caplog.text

    with db.session_scope() as check:
        assert check.scalars(select(MetricSnapshot)).all() == []
        saved_item = check.get(ContentItem, item.id)
        saved_record = check.get(PublishRecord, record.id)
        attempt = check.get(MetricCollectionAttempt, item.id)
        assert saved_item is not None and saved_item.status == ContentStatus.PUBLISHED.value
        assert saved_record is not None and saved_record.platform_post_id == "xhs-unresolved-old"
        assert attempt is not None
        assert attempt.last_attempt_at == NOW
        assert attempt.last_outcome == "unavailable"

    second = collect_all(now=NOW + timedelta(hours=6), respect_windows=True)
    assert second["attempted"] == second["snapshots"] == 1
    assert second["unavailable"] == 0
    with db.session_scope() as check:
        snapshot = check.scalars(select(MetricSnapshot)).one()
        saved_item = check.get(ContentItem, item.id)
        attempt = check.get(MetricCollectionAttempt, item.id)
        assert snapshot.metrics_json["views"] == 42
        assert saved_item is not None and saved_item.status == ContentStatus.MEASURED.value
        assert attempt is not None and attempt.last_outcome == "success"


def test_metric_failure_log_does_not_include_exception_body_or_title(session, caplog):
    account = make_account(session, account_id="availability-account")
    _published(
        session,
        account,
        published_at=NOW - timedelta(hours=25),
        post_id="exception-post-id",
        title="EXCEPTION_TITLE_SECRET",
    )
    session.commit()
    publisher = FailingMetricsPublisher()
    register("xhs", lambda _account_id, **_kwargs: publisher)

    with caplog.at_level(logging.WARNING, logger="social_workflow.metrics"):
        stats = collect_all(now=NOW, respect_windows=True)

    assert stats["attempted"] == stats["errors"] == 1
    assert stats["skipped"] == 0
    assert stats["unavailable"] == stats["snapshots"] == 0
    assert "EXCEPTION_SECRET" not in caplog.text
    assert "EXCEPTION_TITLE_SECRET" not in caplog.text
    assert "exception-post-id" not in caplog.text
    assert publisher.health_calls == 1
    with db.session_scope() as check:
        attempt = check.scalars(select(MetricCollectionAttempt)).one()
        assert attempt.last_outcome == "error"


def test_publisher_init_failure_is_attempted_and_not_repeated_in_same_bucket(session, caplog):
    account = make_account(session, account_id="availability-account")
    item, _record = _published(
        session,
        account,
        published_at=NOW - timedelta(hours=25),
        post_id="init-failure-post",
    )
    session.commit()
    init_calls = 0

    def failing_factory(_account_id, **_kwargs):  # type: ignore[no-untyped-def]
        nonlocal init_calls
        init_calls += 1
        raise RuntimeError("INIT_SECRET")

    register("xhs", failing_factory)

    with caplog.at_level(logging.WARNING, logger="social_workflow.metrics"):
        first = collect_all(now=NOW, respect_windows=True)
        second = collect_all(now=NOW, respect_windows=True)

    assert first["attempted"] == first["errors"] == 1
    assert first["skipped"] == 0
    assert second["attempted"] == second["errors"] == 0
    assert init_calls == 1
    assert "INIT_SECRET" not in caplog.text
    with db.session_scope() as check:
        attempt = check.get(MetricCollectionAttempt, item.id)
        assert attempt is not None and attempt.last_outcome == "error"


def test_claim_bucket_is_monotonic_across_newer_older_newer_ticks(session):
    account = make_account(session, account_id="availability-account")
    item, _record = _published(
        session,
        account,
        published_at=NOW - timedelta(hours=25),
        post_id="monotonic-claim",
    )
    session.commit()
    publisher = ScriptedMetricsPublisher(
        responses=[{"available": False}],
        fallback_responses=[{"available": False}],
    )
    _register(publisher)

    first = collect_all(max_items=1, now=NOW, respect_windows=True)
    older = collect_all(max_items=1, now=NOW - timedelta(hours=6), respect_windows=True)
    newer = collect_all(max_items=1, now=NOW + timedelta(hours=6), respect_windows=True)

    assert first["attempted"] == newer["attempted"] == 1
    assert older["attempted"] == 0
    assert publisher.fetch_post_ids == ["monotonic-claim", "monotonic-claim"]
    with db.session_scope() as check:
        attempt = check.get(MetricCollectionAttempt, item.id)
        assert attempt is not None
        assert attempt.last_attempt_bucket == collector_module._bucket_number(
            NOW + timedelta(hours=6)
        )


def test_claim_same_bucket_and_out_of_order_multiconnection_stress(session):
    account = make_account(session, account_id="availability-account")
    item, _record = _published(
        session,
        account,
        published_at=NOW - timedelta(hours=25),
        post_id="claim-stress",
    )
    session.commit()
    candidate = _first_candidate()

    with ThreadPoolExecutor(max_workers=8) as executor:
        same_bucket = list(
            executor.map(
                lambda _index: collector_module._claim_attempt(candidate, moment=NOW),
                range(32),
            )
        )
    assert same_bucket.count(True) == 1

    moments = [
        NOW - timedelta(hours=12),
        NOW - timedelta(hours=6),
        NOW,
        NOW + timedelta(hours=6),
        NOW + timedelta(hours=12),
        NOW + timedelta(hours=18),
        NOW + timedelta(hours=24),
    ] * 8
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(
            executor.map(
                lambda target: collector_module._claim_attempt(candidate, moment=target), moments
            )
        )
    assert any(results)
    with db.session_scope() as check:
        attempt = check.get(MetricCollectionAttempt, item.id)
        assert attempt is not None
        assert attempt.last_attempt_bucket == collector_module._bucket_number(max(moments))


def test_late_old_bucket_result_cannot_write_after_new_bucket_claim(session):
    account = make_account(session, account_id="availability-account")
    item, record = _published(
        session,
        account,
        published_at=NOW - timedelta(hours=25),
        post_id="xhs-unresolved-late-result",
    )
    session.commit()
    candidate = _first_candidate()
    newer_moment = NOW + timedelta(hours=6)

    assert collector_module._claim_attempt(candidate, moment=NOW) is True
    assert collector_module._claim_attempt(candidate, moment=newer_moment) is True
    assert (
        collector_module._persist_result(
            candidate,
            moment=NOW,
            outcome=AttemptOutcome.SUCCESS,
            metrics={
                "views": 1,
                "platform_post_id": "late-result-must-not-repair",
            },
        )
        is False
    )

    with db.session_scope() as check:
        saved_item = check.get(ContentItem, item.id)
        saved_record = check.get(PublishRecord, record.id)
        attempt = check.get(MetricCollectionAttempt, item.id)
        assert saved_item is not None and saved_item.status == ContentStatus.PUBLISHED.value
        assert saved_record is not None
        assert saved_record.platform_post_id == "xhs-unresolved-late-result"
        assert check.scalars(select(MetricSnapshot)).all() == []
        assert attempt is not None and attempt.last_outcome == "claimed"

    assert collector_module._persist_result(
        candidate,
        moment=newer_moment,
        outcome=AttemptOutcome.SUCCESS,
        metrics={"views": 2},
    )
    with db.session_scope() as check:
        snapshots = list(check.scalars(select(MetricSnapshot)))
        assert len(snapshots) == 1 and snapshots[0].metrics_json["views"] == 2


def test_concurrent_duplicate_results_append_exactly_one_snapshot(session):
    account = make_account(session, account_id="availability-account")
    item, _record = _published(
        session,
        account,
        published_at=NOW - timedelta(hours=25),
        post_id="duplicate-result",
    )
    session.commit()
    candidate = _first_candidate()
    assert collector_module._claim_attempt(candidate, moment=NOW) is True

    def persist(_index: int) -> bool:
        return collector_module._persist_result(
            candidate,
            moment=NOW,
            outcome=AttemptOutcome.SUCCESS,
            metrics={"views": 9},
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(persist, range(32)))
    assert results.count(True) == 1
    with db.session_scope() as check:
        snapshots = list(check.scalars(select(MetricSnapshot)))
        attempt = check.get(MetricCollectionAttempt, item.id)
        assert len(snapshots) == 1
        assert attempt is not None and attempt.last_outcome == "success"


def test_fallback_and_health_exceptions_are_redacted_and_outcomes_persist(session, caplog):
    account = make_account(session, account_id="availability-account")
    fallback_item, _record = _published(
        session,
        account,
        published_at=NOW - timedelta(hours=25),
        post_id="fallback-exception-post",
        title="FALLBACK_TITLE_SECRET",
    )
    session.commit()
    fallback_publisher = FallbackFailingPublisher(
        responses=[{"available": False, "reason": "PRIMARY_RESPONSE_SECRET"}]
    )
    _register(fallback_publisher)

    with caplog.at_level(logging.WARNING, logger="social_workflow.metrics"):
        fallback_stats = collect_all(max_items=1, now=NOW, respect_windows=True)

    assert fallback_stats["attempted"] == fallback_stats["errors"] == 1
    assert fallback_publisher.health_calls == 1
    assert "category=fallback_fetch" in caplog.text
    assert "FALLBACK_EXCEPTION_SECRET" not in caplog.text
    assert "FALLBACK_TITLE_SECRET" not in caplog.text
    assert "PRIMARY_RESPONSE_SECRET" not in caplog.text
    with db.session_scope() as check:
        attempt = check.get(MetricCollectionAttempt, fallback_item.id)
        assert attempt is not None and attempt.last_outcome == "error"

    health_item, _record = _published(
        session,
        account,
        published_at=NOW - timedelta(hours=25),
        post_id="health-exception-post",
    )
    session.commit()
    health_publisher = HealthFailingPublisher(responses=[{"views": 17}])
    _register(health_publisher)
    caplog.clear()

    with caplog.at_level(logging.WARNING, logger="social_workflow.metrics"):
        health_stats = collect_all(max_items=1, now=NOW, respect_windows=True)

    assert health_stats["attempted"] == health_stats["snapshots"] == 1
    assert health_stats["health_errors"] == 1
    assert "HEALTH_SECRET" not in caplog.text
    with db.session_scope() as check:
        attempt = check.get(MetricCollectionAttempt, health_item.id)
        assert attempt is not None and attempt.last_outcome == "success"


@pytest.mark.parametrize("iteration", range(3))
def test_concurrent_ticks_claim_once_and_loser_continues_to_next_candidate(
    session, monkeypatch, iteration
):
    account = make_account(session, account_id="availability-account")
    post_ids = {f"concurrent-{iteration}-1", f"concurrent-{iteration}-2"}
    for post_id in post_ids:
        _published(
            session,
            account,
            published_at=NOW - timedelta(hours=25),
            post_id=post_id,
        )
    session.commit()

    scan_barrier = threading.Barrier(2)
    publisher_barrier = threading.Barrier(2)
    publisher = ConcurrentMetricsPublisher(publisher_barrier)
    _register(publisher)
    original_collect_candidates = collector_module._collect_candidates
    original_claim = collector_module._claim_attempt
    claim_conflicts = 0
    claim_lock = threading.Lock()

    def synchronized_candidates(*args, **kwargs):  # type: ignore[no-untyped-def]
        candidates = original_collect_candidates(*args, **kwargs)
        scan_barrier.wait(timeout=5)
        return candidates

    def observed_claim(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal claim_conflicts
        claimed = original_claim(*args, **kwargs)
        if not claimed:
            with claim_lock:
                claim_conflicts += 1
        return claimed

    monkeypatch.setattr(collector_module, "_collect_candidates", synchronized_candidates)
    monkeypatch.setattr(collector_module, "_claim_attempt", observed_claim)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(collect_all, max_items=1, now=NOW, respect_windows=True)
            for _ in range(2)
        ]
        results = [future.result(timeout=10) for future in futures]

    assert claim_conflicts >= 1
    assert sum(result["attempted"] for result in results) == 2
    assert sum(result["unavailable"] for result in results) == 2
    assert set(publisher.fetch_post_ids) == post_ids
    assert len(publisher.fetch_post_ids) == len(set(publisher.fetch_post_ids)) == 2
    with db.session_scope() as check:
        attempts = list(check.scalars(select(MetricCollectionAttempt)))
        assert len(attempts) == 2
        assert {attempt.last_outcome for attempt in attempts} == {"unavailable"}


def _metric_database_source_logs(caplog):  # type: ignore[no-untyped-def]
    return [
        record
        for record in caplog.records
        if record.name == "social_workflow.metrics"
        and record.levelno >= logging.ERROR
        and "category=database" in record.getMessage()
    ]


class _ExplosiveContextString(str):
    def __new__(cls, value: str, calls: dict[str, int]):
        instance = super().__new__(cls, value)
        instance.calls = calls
        return instance

    def __str__(self) -> str:
        self.calls["str"] += 1
        raise AssertionError("untrusted __str__ must not be called")

    def __repr__(self) -> str:
        self.calls["repr"] += 1
        raise AssertionError("untrusted __repr__ must not be called")


def _database_context_candidate(
    *,
    item_id: object = "safe-item",
    platform: object = "xhs",
    account_id: object = "safe-account",
    window: object = "24h",
):
    return collector_module.MetricCandidate(
        item_id=item_id,  # type: ignore[arg-type]
        title="CONTEXT_TITLE_SENTINEL",
        record_id="CONTEXT_RECORD_SENTINEL",
        platform_post_id="CONTEXT_POST_ID_SENTINEL",
        account_id=account_id,  # type: ignore[arg-type]
        platform=platform,  # type: ignore[arg-type]
        window=window,  # type: ignore[arg-type]
        due_at=None,
        last_attempt_at=None,
        last_attempt_bucket=None,
    )


@pytest.mark.parametrize("field", ["item_id", "platform", "account_id", "window"])
def test_metric_database_error_freezes_only_exact_candidate_context_values(field: str):
    calls = {"str": 0, "repr": 0}
    candidate = _database_context_candidate(
        **{field: _ExplosiveContextString(f"{field}_SECRET", calls)}
    )

    error = MetricDatabaseError(
        "metric claim database operation failed",
        operation="claim",
        candidate=candidate,
    )

    assert calls == {"str": 0, "repr": 0}
    assert error.operation == error.context["operation"] == "claim"
    assert error.context[field] == "<invalid>"
    assert all(type(value) is str or value is None for value in error.context.values())
    assert "SECRET" not in repr(error)
    assert "SECRET" not in repr(error.args)


def test_metric_database_error_context_does_not_retain_mutable_candidate_values():
    mutable_values = ["MUTABLE_SECRET"]
    candidate = _database_context_candidate(
        item_id=mutable_values,
        platform=mutable_values,
        account_id=mutable_values,
        window=mutable_values,
    )

    error = MetricDatabaseError(
        "metric result database operation failed",
        operation="result",
        candidate=candidate,
    )
    mutable_values.append("MUTATED_SECRET")

    assert dict(error.context) == {
        "operation": "result",
        "item_id": "<invalid>",
        "platform": "<invalid>",
        "account_id": "<invalid>",
        "window": "<invalid>",
    }
    assert all(type(value) is str or value is None for value in error.context.values())
    assert "MUTABLE_SECRET" not in repr(error)
    assert "MUTATED_SECRET" not in repr(error.args)


@pytest.mark.parametrize("operation", ["claim", "result"])
def test_database_source_logs_only_safe_frozen_candidate_context(
    monkeypatch, caplog, operation: str
):
    calls = {"str": 0, "repr": 0}
    candidate = _database_context_candidate(
        item_id=_ExplosiveContextString("ITEM_SECRET", calls),
        platform=_ExplosiveContextString("PLATFORM_SECRET", calls),
        account_id=_ExplosiveContextString("ACCOUNT_SECRET", calls),
        window=_ExplosiveContextString("WINDOW_SECRET", calls),
    )

    if operation == "claim":

        def failing_insert(*_args, **_kwargs):  # type: ignore[no-untyped-def]
            raise OperationalError(
                "SOURCE_SQL_SENTINEL",
                {"parameter": "SOURCE_PARAMS_SENTINEL"},
                RuntimeError("SOURCE_DRIVER_SENTINEL"),
            )

        monkeypatch.setattr(collector_module, "sqlite_insert", failing_insert)

        def call_source() -> bool:
            return collector_module._claim_attempt(candidate, moment=NOW)

        expected_message = "metric claim database operation failed"
    else:

        class FailingSession:
            def execute(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
                raise OperationalError(
                    "SOURCE_SQL_SENTINEL",
                    {"parameter": "SOURCE_PARAMS_SENTINEL"},
                    RuntimeError("SOURCE_DRIVER_SENTINEL"),
                )

            def rollback(self) -> None:
                pass

            def close(self) -> None:
                pass

        monkeypatch.setattr(
            collector_module, "get_session_factory", lambda: lambda: FailingSession()
        )

        def call_source() -> bool:
            return collector_module._persist_result(
                candidate,
                moment=NOW,
                outcome=AttemptOutcome.SUCCESS,
            )

        expected_message = "metric result database operation failed"

    with (
        caplog.at_level(logging.ERROR, logger="social_workflow.metrics"),
        pytest.raises(MetricDatabaseError, match=expected_message) as exc,
    ):
        call_source()

    error = exc.value
    assert calls == {"str": 0, "repr": 0}
    assert dict(error.context) == {
        "operation": operation,
        "item_id": "<invalid>",
        "platform": "<invalid>",
        "account_id": "<invalid>",
        "window": "<invalid>",
    }
    logs = _metric_database_source_logs(caplog)
    assert len(logs) == 1
    message = logs[0].getMessage()
    assert message.count("<invalid>") == 4
    assert f"operation={operation}" in message
    for sentinel in (
        "ITEM_SECRET",
        "PLATFORM_SECRET",
        "ACCOUNT_SECRET",
        "WINDOW_SECRET",
        "SOURCE_SQL_SENTINEL",
        "SOURCE_PARAMS_SENTINEL",
        "SOURCE_DRIVER_SENTINEL",
        "0x",
    ):
        assert sentinel not in message


def test_candidate_query_database_error_has_only_safe_operation_context(
    session, monkeypatch, caplog
):
    account = make_account(session, account_id="candidate-account")
    _published(
        session,
        account,
        published_at=NOW - timedelta(hours=25),
        post_id="CANDIDATE_POST_ID_SENTINEL",
        title="CANDIDATE_TITLE_SENTINEL",
    )
    session.commit()

    def failing_execute(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise OperationalError(
            "CANDIDATE_SQL_SENTINEL",
            {"parameter": "CANDIDATE_PARAMS_SENTINEL"},
            RuntimeError("CANDIDATE_DRIVER_SENTINEL"),
        )

    monkeypatch.setattr(Session, "execute", failing_execute)
    with (
        caplog.at_level(logging.ERROR, logger="social_workflow.metrics"),
        pytest.raises(
            MetricDatabaseError, match="metric candidate database operation failed"
        ) as exc,
    ):
        collect_all(now=NOW, respect_windows=True)

    error = exc.value
    assert str(error) == "metric candidate database operation failed"
    assert error.__cause__ is None
    assert error.operation == "candidate_query"
    assert dict(error.context) == {"operation": "candidate_query"}
    with pytest.raises(TypeError):
        error.context["operation"] = "mutated"  # type: ignore[index]
    logs = _metric_database_source_logs(caplog)
    assert len(logs) == 1
    assert logs[0].levelno == logging.ERROR
    assert "operation=candidate_query" in logs[0].getMessage()
    for sentinel in (
        "CANDIDATE_SQL_SENTINEL",
        "CANDIDATE_PARAMS_SENTINEL",
        "CANDIDATE_DRIVER_SENTINEL",
        "CANDIDATE_TITLE_SENTINEL",
        "CANDIDATE_POST_ID_SENTINEL",
    ):
        assert sentinel not in str(error)
        assert sentinel not in str(dict(error.context))
        assert sentinel not in caplog.text


def test_claim_database_error_has_safe_candidate_context(session, monkeypatch, caplog):
    account = make_account(session, account_id="claim-account")
    item, _record = _published(
        session,
        account,
        published_at=NOW - timedelta(hours=25),
        post_id="CLAIM_POST_ID_SENTINEL",
        title="CLAIM_TITLE_SENTINEL",
    )
    session.commit()

    def failing_insert(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise StatementError(
            "CLAIM_DRIVER_SENTINEL",
            "CLAIM_SQL_SENTINEL",
            {"parameter": "CLAIM_PARAMS_SENTINEL"},
            RuntimeError("CLAIM_DRIVER_SENTINEL"),
        )

    monkeypatch.setattr(collector_module, "sqlite_insert", failing_insert)
    with (
        caplog.at_level(logging.ERROR, logger="social_workflow.metrics"),
        pytest.raises(MetricDatabaseError, match="metric claim database operation failed") as exc,
    ):
        collect_all(now=NOW, respect_windows=True)

    error = exc.value
    assert str(error) == "metric claim database operation failed"
    assert error.__cause__ is None
    assert error.operation == "claim"
    assert dict(error.context) == {
        "operation": "claim",
        "item_id": item.id,
        "platform": "xhs",
        "account_id": account.id,
        "window": "24h",
    }
    logs = _metric_database_source_logs(caplog)
    assert len(logs) == 1
    message = logs[0].getMessage()
    assert logs[0].levelno == logging.ERROR
    for field in (f"item_id={item.id}", "platform=xhs", f"account_id={account.id}", "window=24h"):
        assert field in message
    assert "operation=claim" in message
    for sentinel in (
        "CLAIM_SQL_SENTINEL",
        "CLAIM_PARAMS_SENTINEL",
        "CLAIM_DRIVER_SENTINEL",
        "CLAIM_TITLE_SENTINEL",
        "CLAIM_POST_ID_SENTINEL",
    ):
        assert sentinel not in str(error)
        assert sentinel not in str(dict(error.context))
        assert sentinel not in caplog.text


@pytest.mark.parametrize(
    "error_factory",
    [
        pytest.param(
            lambda: OperationalError(
                "UPDATE accounts SET extra=:payload",
                {"payload": "OPERATIONAL_DB_SECRET"},
                RuntimeError("OPERATIONAL_DB_SECRET"),
            ),
            id="operational-error",
        ),
        pytest.param(
            lambda: StatementError(
                "STATEMENT_DB_SECRET",
                "UPDATE accounts SET extra=:payload",
                {"payload": "STATEMENT_DB_SECRET"},
                RuntimeError("STATEMENT_DB_SECRET"),
            ),
            id="statement-error",
        ),
    ],
)
def test_apply_health_db_failure_is_redacted_without_rolling_back_prior_item(
    session, monkeypatch, caplog, error_factory
):
    account = make_account(session, account_id="availability-account")
    published_items: dict[str, ContentItem] = {}
    for post_id in ("persist-one", "persist-two"):
        item, _record = _published(
            session,
            account,
            published_at=NOW - timedelta(hours=25),
            post_id=post_id,
            title="RESULT_TITLE_SENTINEL",
        )
        published_items[post_id] = item
    session.commit()
    publisher = ScriptedMetricsPublisher(
        responses=[
            {"views": 1, "url": "https://example.invalid/RESULT_URL_SENTINEL"},
            {"views": 2, "url": "https://example.invalid/RESULT_URL_SENTINEL"},
        ]
    )
    _register(publisher)
    original_apply_health = collector_module.apply_health
    apply_calls = 0

    def failing_second_apply(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal apply_calls
        apply_calls += 1
        if apply_calls == 2:
            raise error_factory()
        return original_apply_health(*args, **kwargs)

    monkeypatch.setattr(collector_module, "apply_health", failing_second_apply)
    with (
        caplog.at_level(logging.WARNING, logger="social_workflow.metrics"),
        pytest.raises(MetricDatabaseError, match="metric result database operation failed") as exc,
    ):
        collect_all(max_items=2, now=NOW, respect_windows=True)

    error = exc.value
    assert error.__cause__ is None
    assert error.operation == "result"
    assert "SECRET" not in str(error)
    assert "SECRET" not in caplog.text
    assert "UPDATE accounts" not in caplog.text
    assert "category=health_exception" not in caplog.text
    with db.session_scope() as check:
        snapshots = list(check.scalars(select(MetricSnapshot)))
        attempts = list(check.scalars(select(MetricCollectionAttempt)))
        assert len(snapshots) == 1
        assert {attempt.last_outcome for attempt in attempts} == {"success", "claimed"}
        claimed = next(attempt for attempt in attempts if attempt.last_outcome == "claimed")
        assert error.context == {
            "operation": "result",
            "item_id": claimed.content_item_id,
            "platform": "xhs",
            "account_id": account.id,
            "window": "24h",
        }
        assert claimed.content_item_id in {item.id for item in published_items.values()}
    logs = _metric_database_source_logs(caplog)
    assert len(logs) == 1
    message = logs[0].getMessage()
    assert logs[0].levelno == logging.ERROR
    assert "operation=result" in message
    assert f"item_id={error.context['item_id']}" in message
    for sentinel in ("RESULT_TITLE_SENTINEL", "RESULT_URL_SENTINEL", "persist-one", "persist-two"):
        assert sentinel not in str(error)
        assert sentinel not in str(dict(error.context))
        assert sentinel not in caplog.text


def test_crash_after_claim_skips_same_bucket_and_retries_next_bucket(session, monkeypatch):
    account = make_account(session, account_id="availability-account")
    item, _record = _published(
        session,
        account,
        published_at=NOW - timedelta(hours=25),
        post_id="crash-after-claim",
    )
    session.commit()
    publisher = ScriptedMetricsPublisher(responses=[{"views": 3}])
    _register(publisher)
    original_persist_result = collector_module._persist_result

    def crash_before_result(*_args, **_kwargs):
        raise RuntimeError("simulated process crash")

    monkeypatch.setattr(collector_module, "_persist_result", crash_before_result)
    with pytest.raises(RuntimeError, match="simulated process crash"):
        collect_all(max_items=1, now=NOW, respect_windows=True)

    with db.session_scope() as restarted_session:
        attempt = restarted_session.get(MetricCollectionAttempt, item.id)
        assert attempt is not None and attempt.last_outcome == "claimed"
        assert restarted_session.scalars(select(MetricSnapshot)).all() == []

    monkeypatch.setattr(collector_module, "_persist_result", original_persist_result)
    same_bucket = collect_all(max_items=1, now=NOW, respect_windows=True)
    next_bucket = collect_all(
        max_items=1,
        now=NOW + timedelta(hours=6),
        respect_windows=True,
    )

    assert same_bucket["attempted"] == 0
    assert next_bucket["attempted"] == next_bucket["snapshots"] == 1
    assert publisher.fetch_post_ids == ["crash-after-claim", "crash-after-claim"]


def test_title_fallback_success_repairs_post_id_but_unavailable_fallback_does_not(session):
    account = make_account(session, account_id="availability-account")
    _, record = _published(
        session,
        account,
        published_at=NOW - timedelta(hours=25),
        post_id="xhs-unresolved-fallback",
    )
    session.commit()
    publisher = ScriptedMetricsPublisher(
        responses=[{"available": False, "reason": "not found"}],
        fallback_responses=[
            {
                "available": True,
                "views": 9,
                "platform_post_id": "resolved-post-9",
                "url": "https://example.invalid/resolved-post-9",
            }
        ],
    )
    _register(publisher)

    stats = collect_all(now=NOW, respect_windows=True)
    assert stats["attempted"] == stats["snapshots"] == 1
    assert stats["unavailable"] == 0
    with db.session_scope() as check:
        saved = check.get(PublishRecord, record.id)
        assert saved is not None
        assert saved.platform_post_id == "resolved-post-9"
        assert saved.url == "https://example.invalid/resolved-post-9"


def test_fake_publisher_without_available_field_remains_successful(session):
    account = make_account(session, account_id="availability-account")
    _published(session, account, published_at=NOW - timedelta(hours=25))
    session.commit()
    use_fake_publishers()

    stats = collect_all(now=NOW, respect_windows=True)
    assert stats["attempted"] == stats["snapshots"] == 1
    assert stats["unavailable"] == 0


def test_same_bucket_drains_unattempted_candidates_without_repeating_content(session):
    account = make_account(session, account_id="availability-account")
    post_ids: set[str] = set()
    for index in range(5):
        post_id = f"post-{index}"
        post_ids.add(post_id)
        _published(
            session,
            account,
            published_at=NOW - timedelta(hours=25),
            post_id=post_id,
            title=f"公平性标题 {index}",
        )
    session.commit()
    publisher = ScriptedMetricsPublisher(
        responses=[{"available": False, "reason": "still unavailable"}],
        fallback_responses=[{"available": False, "reason": "still unavailable"}],
    )
    _register(publisher)

    first = collect_all(max_items=2, now=NOW, respect_windows=True)
    first_ids = publisher.fetch_post_ids[-2:]
    same_bucket = collect_all(max_items=2, now=NOW, respect_windows=True)
    second_ids = publisher.fetch_post_ids[-2:]
    last_in_bucket = collect_all(max_items=100, now=NOW, respect_windows=True)
    exhausted = collect_all(max_items=2, now=NOW, respect_windows=True)

    for stats in (first, same_bucket):
        assert stats["scanned"] == 5
        assert stats["attempted"] == stats["unavailable"] == 2
        assert stats["snapshots"] == 0
    assert last_in_bucket["attempted"] == last_in_bucket["unavailable"] == 1
    assert exhausted["attempted"] == 0
    assert set(first_ids).isdisjoint(second_ids)
    assert set(publisher.fetch_post_ids) == post_ids


def test_persistent_attempt_queue_services_old_candidates_when_backlog_changes(session):
    account = make_account(session, account_id="availability-account")
    old: dict[str, str] = {}
    for index in range(4):
        item, _record = _published(
            session,
            account,
            published_at=NOW - timedelta(hours=25),
            post_id=f"old-{index}",
        )
        old[f"old-{index}"] = item.id
    session.commit()
    publisher = ScriptedMetricsPublisher(
        responses=[{"available": False, "reason": "still unavailable"}],
        fallback_responses=[{"available": False, "reason": "still unavailable"}],
    )
    _register(publisher)

    assert collect_all(max_items=1, now=NOW, respect_windows=True)["attempted"] == 1
    selected_first = publisher.fetch_post_ids[-1]
    removed_post_id = next(post_id for post_id in old if post_id != selected_first)
    with db.session_scope() as check:
        removed = check.get(ContentItem, old[removed_post_id])
        assert removed is not None
        check.delete(removed)

    for step in range(1, 3):
        moment = NOW + timedelta(hours=6 * step)
        _published(
            session,
            account,
            published_at=moment - timedelta(hours=25),
            post_id=f"new-{step}",
        )
        session.commit()
        assert collect_all(max_items=1, now=moment, respect_windows=True)["attempted"] == 1

    surviving_old = set(old).difference({removed_post_id})
    assert surviving_old.issubset(set(publisher.fetch_post_ids))


def test_limit_contract_and_one_late_success_covers_both_windows(
    session,
):
    account = make_account(session, account_id="availability-account")
    _published(session, account, published_at=NOW - timedelta(days=8))
    session.commit()
    publisher = ScriptedMetricsPublisher(responses=[{"available": True, "views": 5}])
    _register(publisher)

    zero = collect_all(max_items=0, now=NOW, respect_windows=True)
    assert all(value == 0 for value in zero.values())
    with pytest.raises(ValueError, match="max_items"):
        collect_all(max_items=-1, now=NOW, respect_windows=True)
    with pytest.raises(ValueError, match="max_items"):
        collect_all(max_items=True, now=NOW, respect_windows=True)
    assert publisher.fetch_post_ids == []

    first = collect_all(max_items=None, now=NOW, respect_windows=True)
    second = collect_all(max_items=1, now=NOW + timedelta(hours=6), respect_windows=True)
    assert first["snapshots"] == 1 and first["attempted"] == 1
    assert second["not_due"] == 1 and second["attempted"] == 0
    with db.session_scope() as check:
        assert len(check.scalars(select(MetricSnapshot)).all()) == 1


def test_malformed_publisher_payload_isolated_and_other_candidates_continue(session):
    account = make_account(session, account_id="availability-account")
    _published(session, account, published_at=NOW - timedelta(hours=25), post_id="bad-payload")
    _published(session, account, published_at=NOW - timedelta(hours=25), post_id="good-payload")
    session.commit()
    publisher = ScriptedMetricsPublisher(responses=[[], {"views": 12}])
    _register(publisher)

    stats = collect_all(now=NOW, respect_windows=True)
    assert stats["attempted"] == 2
    assert stats["malformed"] == stats["snapshots"] == 1
    assert stats["unavailable"] == stats["errors"] == 0
    with db.session_scope() as check:
        attempts = list(check.scalars(select(MetricCollectionAttempt)))
        assert {attempt.last_outcome for attempt in attempts} == {"malformed", "success"}
        assert len(check.scalars(select(MetricSnapshot)).all()) == 1


def test_dev_metrics_tick_catches_runtime_error_without_secret_leak(session, client, caplog):
    account = make_account(session, account_id="availability-account")
    _published(
        session,
        account,
        published_at=NOW - timedelta(hours=25),
        post_id="api-secret-post",
        title="API_SECRET_TITLE",
    )
    session.commit()
    publisher = FailingMetricsPublisher()
    _register(publisher)

    with caplog.at_level(logging.WARNING):
        response = client.post("/dev/tick/metrics?respect_windows=true")

    assert response.status_code == 200 and response.json()["ok"] is True
    assert response.json()["stats"]["errors"] == 1
    assert "EXCEPTION_SECRET" not in response.text
    assert "API_SECRET_TITLE" not in response.text
    assert "EXCEPTION_SECRET" not in caplog.text


def test_manual_metrics_tick_db_errors_are_redacted_in_both_http_surfaces(
    client, monkeypatch, caplog
):
    from core import scheduler

    def fail_tick(_name: str, **_kwargs):
        raise OperationalError(
            "INSERT INTO metric_snapshots(metrics_json) VALUES (:payload)",
            {"payload": "API_SQL_PAYLOAD_SECRET"},
            RuntimeError("API_SQL_PAYLOAD_SECRET"),
        )

    monkeypatch.setattr(scheduler, "run_tick", fail_tick)
    with caplog.at_level(logging.WARNING):
        dev_response = client.post("/dev/tick/metrics?respect_windows=true")
        api_response = client.post("/api/v1/system/ticks/metrics?respect_windows=true")

    assert dev_response.status_code == api_response.status_code == 500
    assert dev_response.json() == {
        "ok": False,
        "tick": "metrics",
        "error": "metrics tick failed",
    }
    api_error = api_response.json()["error"]
    assert api_error["code"] == "tick_failed"
    assert api_error["message"] == "metrics tick failed"
    assert "API_SQL_PAYLOAD_SECRET" not in dev_response.text
    assert "API_SQL_PAYLOAD_SECRET" not in api_response.text
    assert "API_SQL_PAYLOAD_SECRET" not in caplog.text
    assert "INSERT INTO metric_snapshots" not in caplog.text


@pytest.mark.parametrize(
    ("endpoint", "is_api"),
    [
        ("/dev/tick/metrics?respect_windows=true", False),
        ("/api/v1/system/ticks/metrics?respect_windows=true", True),
    ],
)
def test_manual_metrics_ticks_keep_fixed_database_error_shells(
    session, client, monkeypatch, caplog, endpoint: str, is_api: bool
):
    account = make_account(session, account_id="manual-db-account")
    _published(
        session,
        account,
        published_at=NOW - timedelta(hours=25),
        post_id="MANUAL_POST_ID_SENTINEL",
        title="MANUAL_TITLE_SENTINEL",
    )
    session.commit()

    def failing_execute(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise OperationalError(
            "MANUAL_SQL_SENTINEL",
            {"parameter": "MANUAL_PARAMS_SENTINEL"},
            RuntimeError("MANUAL_DRIVER_SENTINEL"),
        )

    monkeypatch.setattr(Session, "execute", failing_execute)
    with caplog.at_level(logging.ERROR):
        response = client.post(endpoint)

    assert response.status_code == 500
    if is_api:
        error = response.json()["error"]
        assert error["code"] == "tick_failed"
        assert error["message"] == "metrics tick failed"
        assert error["detail"] is None
    else:
        assert response.json() == {
            "ok": False,
            "tick": "metrics",
            "error": "metrics tick failed",
        }
    logs = _metric_database_source_logs(caplog)
    assert len(logs) == 1
    assert "operation=candidate_query" in logs[0].getMessage()
    for sentinel in (
        "MANUAL_SQL_SENTINEL",
        "MANUAL_PARAMS_SENTINEL",
        "MANUAL_DRIVER_SENTINEL",
        "MANUAL_TITLE_SENTINEL",
        "MANUAL_POST_ID_SENTINEL",
        "candidate_query",
    ):
        assert sentinel not in response.text
    for sentinel in (
        "MANUAL_SQL_SENTINEL",
        "MANUAL_PARAMS_SENTINEL",
        "MANUAL_DRIVER_SENTINEL",
        "MANUAL_TITLE_SENTINEL",
        "MANUAL_POST_ID_SENTINEL",
    ):
        assert sentinel not in caplog.text


def _query_probe(callback) -> tuple[int, int]:
    engine = db.get_engine()
    count = 0
    max_bind_count = 0

    def before_cursor_execute(*_args):
        nonlocal count, max_bind_count
        statement = str(_args[2]).lstrip().upper()
        if statement.startswith("SELECT"):
            count += 1
        parameters = _args[3]
        if isinstance(parameters, (list, tuple)):
            if parameters and isinstance(parameters[0], (list, tuple, dict)):
                max_bind_count = max(max_bind_count, len(parameters[0]))
            else:
                max_bind_count = max(max_bind_count, len(parameters))
        elif isinstance(parameters, dict):
            max_bind_count = max(max_bind_count, len(parameters))

    event.listen(engine, "before_cursor_execute", before_cursor_execute)
    try:
        callback()
    finally:
        event.remove(engine, "before_cursor_execute", before_cursor_execute)
    return count, max_bind_count


def test_collector_stats_and_insights_query_count_stays_bounded_through_1000_items(session):
    account = make_account(session, account_id="availability-account")
    _published(session, account, published_at=NOW - timedelta(hours=25), post_id="one")
    session.commit()
    publisher = ScriptedMetricsPublisher(
        responses=[{"available": False, "reason": "unavailable"}],
        fallback_responses=[{"available": False, "reason": "unavailable"}],
    )
    _register(publisher)

    probes: dict[int, tuple[tuple[int, int], tuple[int, int], tuple[int, int]]] = {}
    sizes = ((1, 0), (100, 1), (1000, 100), (1001, 1000))
    for step, (size, previous_size) in enumerate(sizes):
        for index in range(previous_size, size):
            if index == 0:
                continue
            _published(
                session,
                account,
                published_at=NOW - timedelta(hours=25),
                post_id=f"bulk-{index}",
            )
        session.commit()
        moment = NOW + timedelta(hours=6 * step)
        collector_probe = _query_probe(
            lambda moment=moment: collect_all(
                max_items=1,
                now=moment,
                respect_windows=True,
            )
        )
        stats_probe = _query_probe(lambda: build_dashboard(session, now=NOW, window_days=30))
        insights_probe = _query_probe(
            lambda: collect_summary(session, account, now=NOW, window_days=30)
        )
        probes[size] = (collector_probe, stats_probe, insights_probe)

    collector_counts = [probes[size][0][0] for size, _previous_size in sizes]
    stats_counts = [probes[size][1][0] for size, _previous_size in sizes]
    insights_counts = [probes[size][2][0] for size, _previous_size in sizes]
    assert max(collector_counts) <= min(collector_counts) + 1
    assert max(stats_counts) <= min(stats_counts) + 1
    assert max(insights_counts) <= min(insights_counts) + 1
    assert max(probe[0][1] for probe in probes.values()) < 100
    assert max(probe[1][1] for probe in probes.values()) < 100
    assert max(probe[2][1] for probe in probes.values()) < 100


def test_insights_handles_32768_single_account_items_without_large_in_bind(session):
    account = make_account(session, account_id="availability-account")
    session.execute(
        insert(ContentItem),
        [
            {
                "id": f"itm-insights-scale-{index:05d}",
                "account_id": account.id,
                "status": ContentStatus.PUBLISHED.value,
                "bundle_json": {},
                "created_at": NOW - timedelta(days=1),
                "updated_at": NOW - timedelta(days=1),
            }
            for index in range(32_768)
        ],
    )
    session.commit()

    query_count, max_bind_count = _query_probe(
        lambda: collect_summary(session, account, now=NOW, window_days=7)
    )

    assert query_count <= 5
    assert max_bind_count < 100


def test_attempt_schema_is_idempotently_added_to_existing_sqlite_and_cascades(session):
    account = make_account(session, account_id="availability-account")
    item, _record = _published(
        session,
        account,
        published_at=NOW - timedelta(hours=25),
        post_id="schema-upgrade",
    )
    session.commit()
    engine = db.get_engine()
    MetricCollectionAttempt.__table__.drop(engine)

    db.init_db()
    db.init_db()
    inspector = inspect(engine)
    assert "metric_collection_attempts" in inspector.get_table_names()
    assert inspector.get_pk_constraint("metric_collection_attempts")["constrained_columns"] == [
        "content_item_id"
    ]
    foreign_keys = inspector.get_foreign_keys("metric_collection_attempts")
    assert foreign_keys[0]["options"].get("ondelete") == "CASCADE"
    assert {index["name"] for index in inspector.get_indexes("metric_collection_attempts")} == {
        "ix_metric_attempts_at_item"
    }
    stats = collect_all(max_items=1, now=NOW, respect_windows=True)
    assert stats["attempted"] == 1
    with db.session_scope() as check:
        assert check.get(ContentItem, item.id) is not None, "schema upgrade must preserve old rows"
        attempt = check.get(MetricCollectionAttempt, item.id)
        assert attempt is not None
        assert attempt.last_attempt_at == NOW and attempt.last_attempt_at.tzinfo is UTC
        assert attempt.updated_at.tzinfo is UTC
        check.execute(text("DELETE FROM content_items WHERE id = :item_id"), {"item_id": item.id})
    with db.session_scope() as check:
        assert check.get(MetricCollectionAttempt, item.id) is None


def test_sqlite_url_timeout_is_not_overridden_globally(tmp_path):
    engine = db.configure(f"sqlite:///{tmp_path / 'custom-timeout.db'}?timeout=0.125")
    with engine.connect() as connection:
        assert connection.scalar(text("PRAGMA busy_timeout")) == 125
