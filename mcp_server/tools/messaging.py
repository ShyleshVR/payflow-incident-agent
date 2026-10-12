"""Whether messages are flowing between the services: Kafka, each consumer's lag, each outbox and
the dead-letter topics, from Prometheus."""
from pydantic import BaseModel, Field

DEFAULT_MINUTES = 15
MAX_MINUTES = 120

# The same numbers as PayFlow's alert rules, so the tool agrees with the alert that fired.
LAG_ALERT = 500                # KafkaConsumerLag
OUTBOX_AGE_ALERT = 120         # OutboxBacklog: seconds the oldest message has waited
OUTBOX_PENDING_ALERT = 1000    # OutboxBacklog
# Below a tenth of the alert threshold a rise is ordinary traffic, not a trend worth naming.
LAG_TREND_FLOOR = LAG_ALERT // 10
PENDING_TREND_FLOOR = OUTBOX_PENDING_ALERT // 10

# The services that publish through an outbox. Listed here so that one whose metrics are missing
# is reported as missing instead of silently left out.
OUTBOX_SERVICES = ("ledger-service", "payment-service", "payout-service")

_DEAD_LETTER_OFFSETS = 'kafka_topic_partition_current_offset{topic=~".+[.]DLT"}'

Rows = list[tuple[dict[str, str], float]]


def clamp_minutes(minutes: int) -> int:
    return max(1, min(MAX_MINUTES, minutes))


def queries(minutes: int) -> dict[str, str]:
    """"Now" and "`minutes` ago" of each number, as labelled rows (see prometheus.vectors)."""
    ago = f"offset {minutes}m"
    return {
        "kafka_up": 'max(up{job="kafka-exporter"})',
        "kafka_up_before": f'max(up{{job="kafka-exporter"}} {ago})',
        "brokers": "max(kafka_brokers)",
        "lag": "sum by (consumergroup, topic) (kafka_consumergroup_lag)",
        "lag_before": f"sum by (consumergroup, topic) (kafka_consumergroup_lag {ago})",
        "consumers": "max by (consumergroup) (kafka_consumergroup_members)",
        # every replica reports the same database count, so max, not sum
        "outbox_pending": "max by (application) (outbox_pending)",
        "outbox_pending_before": f"max by (application) (outbox_pending {ago})",
        "outbox_oldest_seconds": "max by (application) (outbox_oldest_pending_age_seconds)",
        "outbox_parked": "max by (application) (outbox_failed)",
        "dead_letters": f"sum by (topic) ({_DEAD_LETTER_OFFSETS})",
        "dead_letters_before": f"sum by (topic) ({_DEAD_LETTER_OFFSETS} {ago})",
    }


class ConsumerLag(BaseModel):
    group: str = Field(description="The consumer group: the service that reads the topic")
    topic: str
    lag: int = Field(description="Messages published to the topic that this group has not processed yet")
    change: int | None = Field(description="Lag now minus lag `minutes` ago (negative: catching up); None: no earlier data")
    consumers: int | None = Field(description="Consumer threads connected for this group; 0: nobody is reading")


class Outbox(BaseModel):
    service: str
    pending: int | None = Field(description="Messages committed but not yet published to Kafka; None: the service reports no outbox metrics")
    change: int | None = Field(description="Pending now minus pending `minutes` ago; None: no earlier data")
    oldest_pending_seconds: int | None = Field(description="How long the oldest pending message has waited")
    parked: int | None = Field(description="Messages that failed to publish and were set aside; each blocks the later messages of its payment")


class DeadLetters(BaseModel):
    topic: str
    arrived: int = Field(description="Messages that arrived in the window: each is one a consumer gave up on")


class MessagingStatus(BaseModel):
    minutes: int = Field(description="The window used for `change` and for dead letters")
    kafka_reachable: bool | None = Field(description="None: Prometheus has no data about Kafka")
    brokers: int | None
    consumers: list[ConsumerLag] = Field(description="Every consumer group and topic, most behind first")
    outboxes: list[Outbox]
    dead_letters: list[DeadLetters] = Field(description="Dead-letter topics that received messages in the window; empty: none did")
    concerns: list[str] = Field(description="What looks wrong in these numbers; empty when nothing does")


def assess(minutes: int, values: dict[str, Rows]) -> MessagingStatus:
    """The numbers, plus the concerns a person would read off them (the thresholds are PayFlow's
    alert rules; a rise is named only once it is large enough to matter)."""
    kafka_up = _number(values["kafka_up"])
    reachable = None if kafka_up is None else kafka_up >= 1
    brokers = _number(values["brokers"])

    concerns = []
    if reachable is None:
        concerns.append("Prometheus has no data from the Kafka exporter: Kafka's state, lag and dead letters are unknown")
    elif not reachable:
        concerns.append("Kafka cannot be reached (the exporter's last check failed): lag and dead letters may be missing below")

    consumers = _consumers(values)
    if reachable and not consumers:
        concerns.append("Kafka reports no consumer group: no service is reading any topic")
    for row in consumers:
        concerns.extend(_lag_concerns(row, minutes))

    outboxes = _outboxes(values)
    for row in outboxes:
        concerns.extend(_outbox_concerns(row, minutes))

    dead_letters = _dead_letters(values)
    for row in dead_letters:
        concerns.append(f"{row.arrived} message(s) arrived in {row.topic} in the last {minutes} minutes: a consumer gave up on them")

    return MessagingStatus(
        minutes=minutes,
        kafka_reachable=reachable,
        brokers=None if brokers is None else int(brokers),
        consumers=consumers,
        outboxes=outboxes,
        dead_letters=dead_letters,
        concerns=concerns,
    )


def _consumers(values: dict[str, Rows]) -> list[ConsumerLag]:
    before = _keyed(values["lag_before"], "consumergroup", "topic")
    connected = _keyed(values["consumers"], "consumergroup")
    rows = []
    for (group, topic), lag in _keyed(values["lag"], "consumergroup", "topic").items():
        earlier = before.get((group, topic))
        rows.append(ConsumerLag(
            group=group,
            topic=topic,
            lag=_count(lag),
            change=None if earlier is None else _count(lag) - _count(earlier),
            consumers=None if (group,) not in connected else int(connected[(group,)]),
        ))
    return sorted(rows, key=lambda row: (-row.lag, row.group, row.topic))


def _lag_concerns(row: ConsumerLag, minutes: int) -> list[str]:
    concerns = []
    if row.consumers == 0:
        concerns.append(f"{row.group} has no consumer connected: nothing is reading {row.topic}")
    if row.lag > LAG_ALERT:
        concerns.append(f"{row.group} is {row.lag} messages behind on {row.topic} (the alert fires above {LAG_ALERT})"
                        + _trend(row.lag, row.change, minutes))
    elif row.lag > LAG_TREND_FLOOR and row.change is not None and row.change > 0:
        concerns.append(f"{row.group}'s lag on {row.topic} is growing: {row.lag - row.change} to {row.lag} in {minutes} minutes")
    return concerns


def _outboxes(values: dict[str, Rows]) -> list[Outbox]:
    pending = _keyed(values["outbox_pending"], "application")
    before = _keyed(values["outbox_pending_before"], "application")
    oldest = _keyed(values["outbox_oldest_seconds"], "application")
    parked = _keyed(values["outbox_parked"], "application")
    rows = []
    for service in sorted({key[0] for key in pending} | set(OUTBOX_SERVICES)):
        key = (service,)
        now = None if key not in pending else int(pending[key])
        rows.append(Outbox(
            service=service,
            pending=now,
            change=None if now is None or key not in before else now - int(before[key]),
            oldest_pending_seconds=None if key not in oldest else int(oldest[key]),
            parked=None if key not in parked else int(parked[key]),
        ))
    return rows


def _outbox_concerns(row: Outbox, minutes: int) -> list[str]:
    if row.pending is None:
        return [f"{row.service} reports no outbox metrics: it may be down"]
    concerns = []
    if row.parked:
        concerns.append(f"{row.service} has {row.parked} parked outbox message(s): each blocks the later messages of its payment")
    oldest = row.oldest_pending_seconds or 0
    if oldest > OUTBOX_AGE_ALERT or row.pending > OUTBOX_PENDING_ALERT:
        concerns.append(f"{row.service}'s outbox is not draining: {row.pending} pending, the oldest for {oldest} seconds"
                        + _trend(row.pending, row.change, minutes))
    elif row.pending > PENDING_TREND_FLOOR and row.change is not None and row.change > 0:
        concerns.append(f"{row.service}'s outbox is growing: {row.pending - row.change} to {row.pending} pending in {minutes} minutes")
    return concerns


def _dead_letters(values: dict[str, Rows]) -> list[DeadLetters]:
    """Arrivals = the topic's end offset now minus `minutes` ago. A topic with no earlier value is
    new (its first dead letter created it) if Kafka was being watched then; otherwise there is
    simply no history, and counting its whole content as "arrived" would be wrong."""
    before = _keyed(values["dead_letters_before"], "topic")
    watched_before = _number(values["kafka_up_before"]) is not None
    rows = []
    for key, now in _keyed(values["dead_letters"], "topic").items():
        if key in before:
            arrived = int(now) - int(before[key])
        elif watched_before:
            arrived = int(now)
        else:
            continue
        if arrived > 0:
            rows.append(DeadLetters(topic=key[0], arrived=arrived))
    return sorted(rows, key=lambda row: (-row.arrived, row.topic))


def _trend(now: int, change: int | None, minutes: int) -> str:
    if change is None:
        return ""
    if change == 0:
        return f", unchanged over {minutes} minutes"
    return f", {'up' if change > 0 else 'down'} from {now - change} {minutes} minutes ago"


def _keyed(rows: Rows, *labels: str) -> dict[tuple[str, ...], float]:
    return {tuple(metric.get(label, "") for label in labels): value for metric, value in rows}


def _number(rows: Rows) -> float | None:
    return rows[0][1] if rows else None


def _count(value: float) -> int:
    """The exporter briefly reports a small negative lag when offsets are read mid-commit."""
    return max(0, int(value))
