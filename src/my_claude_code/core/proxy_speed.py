"""How fast, and how often, each proxy address works for each provider (7.54.0).

The reachability ledger in :mod:`my_claude_code.core.proxy_rotation` answers
one question -- may this address carry traffic right now -- and answers it
with a bench. It cannot say which of forty addresses that *may* is the one to
prefer. Measured on this product's own traffic
(``specs/PR-PROXY-CHECK-VERDICTS-AND-SPEED-SPEC.md`` §5.5), the median time to
the first token through the best proxy was 5.7 s and through the worst 186 s,
and an address that works at all works on about half its tries (§4.3). So a
proxy is described by two numbers kept over time, not by one verdict: how long
its connection takes to set up when it works, and how often it works.

**What is recorded.** Three kinds of sample, per ``(address, provider)``:

* ``check`` -- every check the Proxying page, the fetch, the re-prober or a
  bulk add ran: whether it passed, and the connect, tunnel, TLS and (for a
  request-depth check) first-byte times it measured.
* ``dial`` -- every dial a real request made through a chain, from the request
  log's dial rows: the connect and tunnel times, and whether the request it
  carried was answered.
* ``ttft`` -- a proxied attempt's time to first token divided by the rolling
  median time to first token of the same model on the same provider. Dividing
  by the model's own median cancels the choice of model out, so what is left is
  how much slower this address made the answer than usual.

**Keys.** ``address`` is the masked ``host:port`` label MCC names an address
by everywhere else -- the reachability ledger's key, the request log's
``proxy_label``, the dial rows' ``proxy``. It carries no scheme and no
credentials, and it is the same whatever the address came from: a feed, a
WARP or Tor endpoint, or one the operator typed. ``provider`` is the
destination the sample was measured against, because a tunnel that is quick to
one host says little about another.

**Bounds, all constants, none a setting.** The last :data:`SAMPLE_RING` check
and dial samples per key, and separately the last :data:`SAMPLE_RING` TTFT
samples, so a busy chain's live traffic cannot push its check history out.
Only samples from the last :data:`SCORE_WINDOW_SECONDS` count towards a score.
At most :data:`MAX_KEYS` keys, the least recently updated dropped first, like
``MAX_TRACKED_ENDPOINTS`` in the reachability ledger. The TTFT baseline is the
last :data:`MODEL_TTFT_WINDOW` first-token times per ``(provider, model)`` --
direct and proxied alike -- from the same 24 hours, for at most
:data:`MAX_MODEL_WINDOWS` pairs; an attempt is compared with it only once it
holds :data:`MODEL_TTFT_BASELINE_MIN` times.

**The score** (spec §7.3), one number that can be explained on the row::

    r  = (successes + 1) / (samples + 2)          # one pass is not 100 %
    s  = median(setup) over passing samples       # connect + tunnel [+ tls]
    E  = s + (1 - r) / r * F                      # expected setup per use
    L  = clamp(median TTFT ratio, 0.5, 4.0)       # 1.0 until 3 live samples
    rank_key = E * L                              # lower is better

``F`` is what one failed dial costs a live request. The spec wrote it as the
connect timeout plus the retry sleep "until F1 ships"; F1 shipped in 7.52.1 and
a proxied leg no longer sleeps before it switches, so ``F`` is the operator's
``PROXY_CONNECT_TIMEOUT_SECONDS`` in milliseconds and nothing else. ``core``
cannot read settings, so the caller passes it.

**States** (spec §6.1), labels that never block selection:

* ``untested`` -- no sample in the window;
* ``flaky`` -- ``r < 0.5`` over at least 3 samples;
* ``slow`` -- it has passed, and its median setup is above the operator's
  ``PROXY_CHECK_SLOW_MS``;
* ``working`` -- it has passed, its setup is within that limit, and ``r`` is
  at least 0.8. Because of the smoothing, even a perfect record cannot reach
  0.8 before its third sample (2 of 2 is 0.75), so below three samples a record
  with no failure counts as working;
* ``""`` -- none of those earned: an address that has not passed yet on one or
  two samples, or one between flaky and working. The row's "k of n ok" says
  which.

``dead`` and ``refused`` are not computed here. They stay the reachability and
interception ledgers' verdicts, exactly as before.

**What this module is not.** It never reads a file and never names one: the
application layer loads and flushes it (``application/proxy_speed_store.py``),
so ``core`` keeps importing nothing first-party. Nothing on the request path
writes to it -- dial and TTFT samples are handed over when a request is
finalised, off the hot path, and only when the request log is on.
"""

import math
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from statistics import median
from typing import Any

#: How many check and dial samples each ``(address, provider)`` keeps.
SAMPLE_RING = 20

#: Samples older than this do not count towards a score, and are dropped when
#: the ledger is written. A day: long enough to hold a re-prober's hourly rung
#: and a working session's traffic, short enough that yesterday's congestion
#: does not rank today's list.
SCORE_WINDOW_SECONDS = 24 * 3600.0

#: How many ``(address, provider)`` keys are kept, least recently updated
#: dropped first. A fetch of a few hundred addresses against two providers fits
#: several times over.
MAX_KEYS = 2048

#: How many first-token times per ``(provider, model)`` make the baseline a
#: proxied attempt is compared with.
MODEL_TTFT_WINDOW = 50

#: How many first-token times that baseline needs before a ratio is taken. The
#: first attempts on a model have nothing to be compared with.
MODEL_TTFT_BASELINE_MIN = 3

#: How many ``(provider, model)`` baselines are kept.
MAX_MODEL_WINDOWS = 512

#: How many live TTFT ratios an address needs before they move its rank.
LIVE_TTFT_MIN_SAMPLES = 3

#: The clamp on the live factor: no address is more than four times as bad, or
#: twice as good, as typical because of a handful of odd answers.
TTFT_FACTOR_MIN = 0.5
TTFT_FACTOR_MAX = 4.0

#: The success-rate thresholds of spec §6.1.
WORKING_RATE = 0.8
FLAKY_RATE = 0.5
FLAKY_MIN_SAMPLES = 3

KIND_CHECK = "check"
KIND_DIAL = "dial"
KIND_TTFT = "ttft"
SAMPLE_KINDS = (KIND_CHECK, KIND_DIAL, KIND_TTFT)

STATE_WORKING = "working"
STATE_SLOW = "slow"
STATE_FLAKY = "flaky"
STATE_UNTESTED = "untested"
SPEED_STATES = (STATE_WORKING, STATE_SLOW, STATE_FLAKY, STATE_UNTESTED)

#: The dial outcomes (``core/upstream_ladder.py::dial_rows``) that are a
#: verdict. ``answered`` is a pass; ``failed``, and ``switched`` with a
#: recorded reason, are failures. ``dialing`` and a ``switched`` row with no
#: try behind it said nothing about the address and are not samples.
DIAL_ANSWERED = "answered"
DIAL_FAILED = "failed"
DIAL_SWITCHED = "switched"

#: The document's own version, so a later shape can refuse an older reader.
DOCUMENT_VERSION = 1


@dataclass(frozen=True, slots=True)
class SpeedSample:
    """One measurement of one address for one provider.

    Every timing is ``None`` when it was not measured -- a reused connection
    has no connect time, and an older check has no phases -- never zero.
    """

    kind: str
    #: Wall-clock epoch seconds, because the ledger outlives the process.
    at: float
    ok: bool = False
    connect_ms: float | None = None
    #: The CONNECT or SOCKS5 exchange: a check's tunnel, a dial's handshake.
    tunnel_ms: float | None = None
    tls_ms: float | None = None
    first_byte_ms: float | None = None
    #: A check's failure class, or a dial's reason. Empty for a pass.
    failure: str = ""
    #: ``ttft`` only: this attempt's first token over the model's median.
    ratio: float | None = None

    @property
    def setup_ms(self) -> float | None:
        """Connect + tunnel [+ TLS]; a request-depth check's own first byte.

        The same arithmetic as ``ProxyCheckRecord.setup_ms``: a ``request``
        check's ``HEAD`` dialled, tunnelled and handshook for itself, so its
        first byte is the whole setup and adding the dial would count the
        connect twice.
        """

        if self.first_byte_ms is not None:
            return self.first_byte_ms
        phases = [
            value
            for value in (self.connect_ms, self.tunnel_ms, self.tls_ms)
            if value is not None
        ]
        return sum(phases) if phases else None

    def as_document(self) -> dict[str, Any]:
        document: dict[str, Any] = {"kind": self.kind, "at": round(self.at, 3)}
        if self.kind != KIND_TTFT:
            document["ok"] = self.ok
        for key, value in (
            ("connect_ms", self.connect_ms),
            ("tunnel_ms", self.tunnel_ms),
            ("tls_ms", self.tls_ms),
            ("first_byte_ms", self.first_byte_ms),
            ("ratio", self.ratio),
        ):
            if value is not None:
                document[key] = round(value, 4 if key == "ratio" else 1)
        if self.failure:
            document["failure"] = self.failure
        return document

    @classmethod
    def from_document(cls, raw: object) -> SpeedSample | None:
        if not isinstance(raw, Mapping):
            return None
        kind = str(raw.get("kind") or "")
        at = _number(raw.get("at"))
        if kind not in SAMPLE_KINDS or at is None:
            return None
        sample = cls(
            kind=kind,
            at=at,
            ok=bool(raw.get("ok")),
            connect_ms=_number(raw.get("connect_ms")),
            tunnel_ms=_number(raw.get("tunnel_ms")),
            tls_ms=_number(raw.get("tls_ms")),
            first_byte_ms=_number(raw.get("first_byte_ms")),
            failure=str(raw.get("failure") or "")[:80],
            ratio=_number(raw.get("ratio")),
        )
        if kind == KIND_TTFT and (sample.ratio is None or sample.ratio <= 0):
            return None
        return sample


@dataclass(frozen=True, slots=True)
class SpeedScore:
    """The §7.3 score of one ``(address, provider)``, with its working."""

    #: Check and dial samples in the window, and how many of them passed.
    samples: int
    successes: int
    #: ``(successes + 1) / (samples + 2)``.
    success_rate: float
    #: Median setup over the passing samples that timed one, or ``None``.
    setup_ms: float | None
    #: ``setup_ms`` plus the expected cost of the failures, or ``None`` when
    #: there is no setup to start from.
    expected_ms: float | None
    #: Live TTFT ratios in the window, and the factor they give (1.0 below
    #: :data:`LIVE_TTFT_MIN_SAMPLES`).
    live_samples: int
    ttft_factor: float
    #: ``expected_ms * ttft_factor``; ``None`` when there is no expected setup.
    #: Lower is better, and a ``None`` sorts after every number.
    rank_key: float | None
    state: str

    def as_document(self) -> dict[str, Any]:
        return {
            "setup_ms": None if self.setup_ms is None else round(self.setup_ms),
            "success_rate": round(self.success_rate, 4),
            "successes": self.successes,
            "samples": self.samples,
            "live_samples": self.live_samples,
            "ttft_factor": round(self.ttft_factor, 3),
            "expected_ms": None
            if self.expected_ms is None
            else round(self.expected_ms),
            "rank_key": None if self.rank_key is None else round(self.rank_key, 1),
            "state": self.state,
        }


def success_rate(successes: int, samples: int) -> float:
    """``(successes + 1) / (samples + 2)``: a single pass is not 100 %."""

    return (max(0, successes) + 1) / (max(0, samples) + 2)


def expected_setup_ms(setup_ms: float, rate: float, failure_cost_ms: float) -> float:
    """``s + (1 - r) / r * F``: the setup a live request should expect.

    Each failed dial costs ``F`` before the chain moves on, and at a success
    rate ``r`` a request meets ``(1 - r) / r`` of them on average before one
    works.
    """

    rate = min(1.0, max(1e-6, rate))
    return setup_ms + (1.0 - rate) / rate * max(0.0, failure_cost_ms)


def speed_state(
    *,
    samples: int,
    successes: int,
    rate: float,
    setup_ms: float | None,
    slow_ms: float,
) -> str:
    """The §6.1 label for one key. See the module docstring for the order."""

    if samples <= 0:
        return STATE_UNTESTED
    if samples >= FLAKY_MIN_SAMPLES and rate < FLAKY_RATE:
        return STATE_FLAKY
    if successes <= 0:
        return ""
    if setup_ms is not None and setup_ms > slow_ms:
        return STATE_SLOW
    if rate >= WORKING_RATE or (samples < FLAKY_MIN_SAMPLES and successes == samples):
        return STATE_WORKING
    return ""


class _Record:
    """One key's two rings."""

    __slots__ = ("samples", "ttft", "updated")

    def __init__(self) -> None:
        self.samples: deque[SpeedSample] = deque(maxlen=SAMPLE_RING)
        self.ttft: deque[SpeedSample] = deque(maxlen=SAMPLE_RING)
        self.updated = 0.0


class ProxySpeedLedger:
    """The process-wide table. Thread-safe: samples arrive from worker threads."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: OrderedDict[tuple[str, str], _Record] = OrderedDict()
        self._models: OrderedDict[tuple[str, str], deque[tuple[float, float]]] = (
            OrderedDict()
        )
        self._dirty = False

    # ------------------------------------------------------------ writes

    def note(self, address: str, provider: str, sample: SpeedSample) -> None:
        """Record one check or dial sample (or a TTFT ratio) for a key."""

        key = _key(address, provider)
        if key is None or sample.kind not in SAMPLE_KINDS:
            return
        with self._lock:
            record = self._records.get(key)
            if record is None:
                record = _Record()
                self._records[key] = record
            else:
                self._records.move_to_end(key)
            ring = record.ttft if sample.kind == KIND_TTFT else record.samples
            ring.append(sample)
            record.updated = max(record.updated, sample.at)
            self._dirty = True
            while len(self._records) > MAX_KEYS:
                self._records.popitem(last=False)

    def note_dial_rows(
        self,
        provider: str,
        rows: Iterable[Mapping[str, Any]],
        *,
        now: float | None = None,
    ) -> int:
        """Record a finished attempt's dial rows. Returns how many were samples.

        The rows are ``core/upstream_ladder.py::dial_rows`` output as stored in
        the request log: ``proxy``, ``connect_ms``, ``handshake_ms``,
        ``outcome``, ``reason``. The direct rung and a row that said nothing
        about its address are skipped.
        """

        wall = time.time() if now is None else now
        taken = 0
        for row in rows:
            sample = dial_sample(row, at=wall)
            address = str(row.get("proxy") or "")
            if sample is None or not address or address == "direct":
                continue
            self.note(address, provider, sample)
            taken += 1
        return taken

    def note_ttft(
        self,
        provider: str,
        model: str,
        ttft_ms: float,
        *,
        address: str | None = None,
        now: float | None = None,
    ) -> float | None:
        """Add one first-token time to the model's baseline; rate a proxy by it.

        Every attempt with a first token calls this, direct or proxied, so the
        baseline is "what this model usually takes on this provider". A proxied
        attempt (``address`` given) is compared with the baseline as it stood
        *before* this answer joined it, and the ratio is recorded against the
        address. Returns the ratio, or ``None`` when none was taken.
        """

        provider_key = str(provider or "").strip().lower()
        model_key = str(model or "").strip()
        if not provider_key or not model_key or not _positive(ttft_ms):
            return None
        wall = time.time() if now is None else now
        horizon = wall - SCORE_WINDOW_SECONDS
        pair = (provider_key, model_key)
        ratio: float | None = None
        with self._lock:
            window = self._models.get(pair)
            if window is None:
                window = deque(maxlen=MODEL_TTFT_WINDOW)
                self._models[pair] = window
            else:
                self._models.move_to_end(pair)
            recent = [value for at, value in window if at >= horizon]
            if address and len(recent) >= MODEL_TTFT_BASELINE_MIN:
                baseline = median(recent)
                if baseline > 0:
                    ratio = float(ttft_ms) / baseline
            window.append((wall, float(ttft_ms)))
            self._dirty = True
            while len(self._models) > MAX_MODEL_WINDOWS:
                self._models.popitem(last=False)
        if ratio is not None and address:
            self.note(address, provider_key, SpeedSample(KIND_TTFT, wall, ratio=ratio))
        return ratio

    # ------------------------------------------------------------- reads

    def score(
        self,
        address: str,
        provider: str,
        *,
        failure_cost_ms: float,
        slow_ms: float,
        now: float | None = None,
    ) -> SpeedScore:
        """The §7.3 score of one key, from the samples inside the window."""

        key = _key(address, provider)
        wall = time.time() if now is None else now
        horizon = wall - SCORE_WINDOW_SECONDS
        with self._lock:
            record = None if key is None else self._records.get(key)
            samples = (
                []
                if record is None
                else [sample for sample in record.samples if sample.at >= horizon]
            )
            ratios = (
                []
                if record is None
                else [
                    sample.ratio
                    for sample in record.ttft
                    if sample.at >= horizon and sample.ratio is not None
                ]
            )
        return score_samples(
            samples, ratios, failure_cost_ms=failure_cost_ms, slow_ms=slow_ms
        )

    def samples(self, address: str, provider: str) -> list[SpeedSample]:
        """Every stored sample of one key, both rings, oldest first. For tests."""

        key = _key(address, provider)
        with self._lock:
            record = None if key is None else self._records.get(key)
            if record is None:
                return []
            return sorted([*record.samples, *record.ttft], key=lambda item: item.at)

    def __len__(self) -> int:
        with self._lock:
            return len(self._records)

    @property
    def dirty(self) -> bool:
        return self._dirty

    # ------------------------------------------------------- persistence

    def snapshot(self, *, now: float | None = None) -> dict[str, Any]:
        """The whole ledger as a JSON-ready document, samples past the window dropped.

        Clears the dirty flag: the caller is about to write what it returns.
        """

        wall = time.time() if now is None else now
        horizon = wall - SCORE_WINDOW_SECONDS
        with self._lock:
            keys = []
            for (address, provider), record in self._records.items():
                samples = [s.as_document() for s in record.samples if s.at >= horizon]
                ttft = [s.as_document() for s in record.ttft if s.at >= horizon]
                if not samples and not ttft:
                    continue
                entry: dict[str, Any] = {
                    "address": address,
                    "provider": provider,
                    "samples": samples,
                }
                if ttft:
                    entry["ttft"] = ttft
                keys.append(entry)
            models = [
                {
                    "provider": provider,
                    "model": model,
                    "ttft_ms": [
                        [round(at, 3), round(value, 1)]
                        for at, value in window
                        if at >= horizon
                    ],
                }
                for (provider, model), window in self._models.items()
            ]
            self._dirty = False
        return {
            "version": DOCUMENT_VERSION,
            "keys": keys,
            "models": [entry for entry in models if entry["ttft_ms"]],
        }

    def mark_dirty(self) -> None:
        """Put the flag back after a snapshot that could not be written."""

        self._dirty = True

    def restore(self, document: object) -> int:
        """Replace the ledger with a stored document. Returns the keys loaded.

        Anything unreadable is skipped, never raised: a damaged file costs the
        history, not the server.
        """

        with self._lock:
            self._records.clear()
            self._models.clear()
            self._dirty = False
        if not isinstance(document, Mapping):
            return 0
        raw_keys = document.get("keys")
        loaded = 0
        if isinstance(raw_keys, list):
            for raw in raw_keys:
                if not isinstance(raw, Mapping):
                    continue
                key = _key(
                    str(raw.get("address") or ""), str(raw.get("provider") or "")
                )
                if key is None:
                    continue
                samples = _samples(raw.get("samples"), ttft=False)
                ttft = _samples(raw.get("ttft"), ttft=True)
                if not samples and not ttft:
                    continue
                with self._lock:
                    record = self._records.get(key) or _Record()
                    record.samples.extend(samples)
                    record.ttft.extend(ttft)
                    record.updated = max(
                        [
                            record.updated,
                            *(s.at for s in samples),
                            *(s.at for s in ttft),
                        ]
                    )
                    self._records[key] = record
                loaded += 1
        raw_models = document.get("models")
        if isinstance(raw_models, list):
            for raw in raw_models:
                if not isinstance(raw, Mapping):
                    continue
                provider = str(raw.get("provider") or "").strip().lower()
                model = str(raw.get("model") or "").strip()
                values = raw.get("ttft_ms")
                if not provider or not model or not isinstance(values, list):
                    continue
                window: deque[tuple[float, float]] = deque(maxlen=MODEL_TTFT_WINDOW)
                for pair in values:
                    if isinstance(pair, list | tuple) and len(pair) == 2:
                        at, value = _number(pair[0]), _number(pair[1])
                        if at is not None and value is not None and value > 0:
                            window.append((at, value))
                if window:
                    with self._lock:
                        self._models[(provider, model)] = window
        with self._lock:
            # Least recently updated first, which is the order the cap prunes in.
            ordered = sorted(self._records.items(), key=lambda item: item[1].updated)
            self._records = OrderedDict(ordered[-MAX_KEYS:])
            while len(self._models) > MAX_MODEL_WINDOWS:
                self._models.popitem(last=False)
            self._dirty = False
        return min(loaded, MAX_KEYS)

    def reset(self) -> None:
        """Forget everything. For tests and for a runtime shutting down."""

        with self._lock:
            self._records.clear()
            self._models.clear()
            self._dirty = False


def score_samples(
    samples: Iterable[SpeedSample],
    ratios: Iterable[float],
    *,
    failure_cost_ms: float,
    slow_ms: float,
) -> SpeedScore:
    """The §7.3 arithmetic over already-windowed samples."""

    counted = [sample for sample in samples if sample.kind != KIND_TTFT]
    total = len(counted)
    passed = [sample for sample in counted if sample.ok]
    rate = success_rate(len(passed), total)
    timed = [
        setup
        for sample in passed
        if (setup := sample.setup_ms) is not None and math.isfinite(setup)
    ]
    setup = float(median(timed)) if timed else None
    expected = (
        None if setup is None else expected_setup_ms(setup, rate, failure_cost_ms)
    )
    live = [ratio for ratio in ratios if _positive(ratio)]
    factor = (
        min(TTFT_FACTOR_MAX, max(TTFT_FACTOR_MIN, float(median(live))))
        if len(live) >= LIVE_TTFT_MIN_SAMPLES
        else 1.0
    )
    return SpeedScore(
        samples=total,
        successes=len(passed),
        success_rate=rate,
        setup_ms=setup,
        expected_ms=expected,
        live_samples=len(live),
        ttft_factor=factor,
        rank_key=None if expected is None else expected * factor,
        state=speed_state(
            samples=total,
            successes=len(passed),
            rate=rate,
            setup_ms=setup,
            slow_ms=float(slow_ms),
        ),
    )


def dial_sample(row: Mapping[str, Any], *, at: float) -> SpeedSample | None:
    """One stored dial row as a sample, or ``None`` when it was no verdict."""

    outcome = str(row.get("outcome") or "")
    reason = str(row.get("reason") or "")
    if outcome == DIAL_ANSWERED:
        ok = True
    elif outcome == DIAL_FAILED or (outcome == DIAL_SWITCHED and reason):
        ok = False
    else:
        return None
    return SpeedSample(
        kind=KIND_DIAL,
        at=at,
        ok=ok,
        connect_ms=_number(row.get("connect_ms")),
        tunnel_ms=_number(row.get("handshake_ms")),
        failure="" if ok else reason[:80],
    )


def _samples(raw: object, *, ttft: bool) -> list[SpeedSample]:
    if not isinstance(raw, list):
        return []
    parsed = [SpeedSample.from_document(item) for item in raw]
    kept = [
        sample
        for sample in parsed
        if sample is not None and (sample.kind == KIND_TTFT) == ttft
    ]
    kept.sort(key=lambda sample: sample.at)
    return kept[-SAMPLE_RING:]


def _key(address: str, provider: str) -> tuple[str, str] | None:
    address_key = str(address or "").strip()
    provider_key = str(provider or "").strip().lower()
    if not address_key or not provider_key:
        return None
    return address_key, provider_key


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _positive(value: object) -> bool:
    number = _number(value)
    return number is not None and number > 0


#: The one ledger the process keeps, like ``PROXY_REACHABILITY``.
PROXY_SPEED = ProxySpeedLedger()


__all__ = [
    "DOCUMENT_VERSION",
    "KIND_CHECK",
    "KIND_DIAL",
    "KIND_TTFT",
    "LIVE_TTFT_MIN_SAMPLES",
    "MAX_KEYS",
    "MAX_MODEL_WINDOWS",
    "MODEL_TTFT_BASELINE_MIN",
    "MODEL_TTFT_WINDOW",
    "PROXY_SPEED",
    "SAMPLE_RING",
    "SCORE_WINDOW_SECONDS",
    "SPEED_STATES",
    "STATE_FLAKY",
    "STATE_SLOW",
    "STATE_UNTESTED",
    "STATE_WORKING",
    "TTFT_FACTOR_MAX",
    "TTFT_FACTOR_MIN",
    "ProxySpeedLedger",
    "SpeedSample",
    "SpeedScore",
    "dial_sample",
    "expected_setup_ms",
    "score_samples",
    "speed_state",
    "success_rate",
]
