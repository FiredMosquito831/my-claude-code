"""What one provider has learned from its host's own rejections.

Three tables, one shape. All are written only from something the upstream
itself said, and all only ever narrow what MCC will ask for next time.

Since 6.52.0 they are also **durable**. They used to be per process and per
provider instance on purpose -- a restart or a config apply forgot everything,
so a host that was briefly misconfigured healed by itself. That property is not
given up, only moved onto a clock: the store behind :attr:`sink`
(:mod:`my_claude_code.providers.recovery.store`) writes each fact to
``~/.mcc/learned_facts.json`` with the time it was last confirmed, and stops
applying it once its evidence class has expired. Self-healing on a TTL rather
than on a restart, and visible on the Models page either way.

A bare ``RecoveryMemory()`` with no sink is still exactly what it always was:
a per-instance memory that persists nothing.
"""

from dataclasses import dataclass, field
from datetime import date

from .facts import (
    FACT_EFFORT_VALUE_REJECTED,
    FACT_OUTPUT_CAP,
    FACT_REASONING_FIELD_REJECTED,
    FACT_RESPONSES_TOOL_CHOICE_AUTO_ONLY,
    FACT_RESPONSES_TOOL_NAME_MAX_LENGTH,
    FACT_RESPONSES_TOOL_SCHEMA_KEYWORD,
    FACT_RESPONSES_TOOLS_MAX_COUNT,
    FACT_STREAM_USAGE_UNSUPPORTED,
    PROVIDER_WIDE_MODEL_ID,
    FactSink,
)


@dataclass(slots=True)
class RecoveryMemory:
    """Per-model caps and refused fields learned from upstream 400s."""

    #: Bare model id -> the smallest output-token maximum this host has stated.
    output_caps: dict[str, int] = field(default_factory=dict)

    #: Bare model id -> {refused reasoning field: ISO date it was learned}.
    #: The date is what the Models page shows next to "learned from the host's
    #: own rejection".
    rejected_reasoning_fields: dict[str, dict[str, str]] = field(default_factory=dict)

    #: Bare model id -> {reasoning field: {refused values}}.
    #:
    #: The fine-grained sibling of :attr:`rejected_reasoning_fields`. That one
    #: says "this model has no such knob"; this says "it has the knob and not
    #: that setting", which is the smallest thing a 400 on ``xhigh`` proves.
    #: Until 7.12.0 only the coarse one existed, so one 400 on ``xhigh`` cost
    #: the model every effort it *would* have accepted, for the whole TTL.
    rejected_effort_values: dict[str, dict[str, set[str]]] = field(default_factory=dict)

    #: Bare model ids whose host answered ``stream_options.include_usage``
    #: with a 400. Before 6.52.0 nothing recorded this at all, so every single
    #: request to such a host paid a failed try and a retry -- not once per
    #: process, once per request.
    stream_usage_unsupported: set[str] = field(default_factory=set)

    #: The longest tool name this host's Responses surface accepts, or
    #: ``None`` when it has never said. Host-wide rather than per model: the
    #: request validator that states it sits in front of the deployment.
    responses_tool_name_max_length: int | None = None

    #: Bare model id -> the ISO date its Responses surface was proven to
    #: refuse every ``tool_choice`` but ``auto``. Per model, because that is
    #: the scope the refusal was measured at; the date is what the request
    #: log's marker and the Models page both show.
    responses_tool_choice_auto_only: dict[str, str] = field(default_factory=dict)

    #: ``"<keyword>:<construct>"`` -> the ISO date this host was proven to
    #: refuse that class of JSON-Schema keyword in a tool schema. Host-wide
    #: rather than per model, for the same reason the tool-name ceiling is:
    #: the request validator that states it sits in front of the deployment,
    #: and a per-model row would re-pay the 400 once for every model in the
    #: catalogue.
    responses_tool_schema_keywords: dict[str, str] = field(default_factory=dict)

    #: The most tools this host's Responses surface accepts in one request,
    #: or ``None`` when it has never said. Host-wide, like the tool-name
    #: ceiling: the validator that states it sits in front of the deployment.
    responses_tools_max_count: int | None = None

    #: Where a newly learned fact is written through to, or ``None`` for a
    #: memory that persists nothing.
    sink: FactSink | None = None

    def cap_for(self, model: str) -> int | None:
        """Return the learned output-token cap for a model, if one is known."""
        return self.output_caps.get(model)

    def learn_cap(self, model: str, cap: int, *, evidence: str = "") -> int:
        """Record a stated cap, keeping the smallest ever seen, and return it.

        Monotonically narrowing: a host that states a lower maximum on a later
        request has revised its own answer downward, and a higher one does not
        contradict the number already proven to work.
        """
        previous = self.output_caps.get(model)
        cap = cap if previous is None else min(previous, cap)
        self.output_caps[model] = cap
        # Written through even when the number did not move: a re-statement is
        # the host confirming the cap today, which is what keeps it out of the
        # stale bucket.
        if self.sink is not None:
            self.sink(FACT_OUTPUT_CAP, model, cap, "", evidence)
        return cap

    def rejections_for(self, model: str) -> dict[str, str] | None:
        """Return the refused reasoning fields for a model, if any."""
        return self.rejected_reasoning_fields.get(model)

    def remember_rejection(
        self, model: str, rejected_field: str, *, evidence: str = ""
    ) -> bool:
        """Record a proven refusal; ``False`` when it was already known.

        Reached only after the stripped body was actually accepted, so the
        strip is what fixed it. A rejection is an inference, not a stated fact
        the way a cap is, and writing it before the retry succeeded would teach
        the process to stop asking for thinking on a model that was never the
        problem.
        """
        rejections = self.rejected_reasoning_fields.setdefault(model, {})
        already_known = rejected_field in rejections
        rejections[rejected_field] = date.today().isoformat()
        if self.sink is not None:
            self.sink(
                FACT_REASONING_FIELD_REJECTED, model, True, rejected_field, evidence
            )
        return not already_known

    def rejected_values_for(self, model: str) -> dict[str, set[str]] | None:
        """Return the refused values per reasoning field for a model, if any."""

        return self.rejected_effort_values.get(model)

    def remember_rejected_values(
        self, model: str, rejected_field: str, values: set[str], *, evidence: str = ""
    ) -> bool:
        """Record values this model refused; ``False`` when all were known.

        Reached on the same proof the coarse rejection is -- only once the
        rewritten body was accepted -- for the same reason: a 400 that merely
        mentioned a word is not evidence that dropping it is what fixed the
        request.

        One fact per value, and the value is in the key
        (``detail="<field>=<value>"``), so forgetting one word on the Models
        page forgets exactly that word. A list under a single key could only be
        forgotten wholesale.
        """

        if not values:
            return False
        known = self.rejected_effort_values.setdefault(model, {}).setdefault(
            rejected_field, set()
        )
        learned = {value for value in values if value not in known}
        known.update(values)
        if self.sink is not None:
            for value in sorted(values):
                self.sink(
                    FACT_EFFORT_VALUE_REJECTED,
                    model,
                    True,
                    f"{rejected_field}={value}",
                    evidence,
                )
        return bool(learned)

    def learn_responses_tool_name_limit(self, limit: int, *, evidence: str = "") -> int:
        """Record a stated tool-name ceiling, keeping the smallest, and return it.

        Monotonically narrowing for the same reason
        :meth:`learn_cap` is: a host that later states a lower ceiling has
        revised its own answer downward, and a higher one does not contradict
        the length already proven to be accepted.

        Written through even when the number did not move, so a re-statement
        counts as the host confirming it today and the row stays fresh.
        """

        previous = self.responses_tool_name_max_length
        limit = limit if previous is None else min(previous, limit)
        self.responses_tool_name_max_length = limit
        if self.sink is not None:
            self.sink(
                FACT_RESPONSES_TOOL_NAME_MAX_LENGTH,
                PROVIDER_WIDE_MODEL_ID,
                limit,
                "",
                evidence,
            )
        return limit

    def learn_responses_tools_max_count(self, limit: int, *, evidence: str = "") -> int:
        """Record a stated tools-count ceiling, keeping the smallest, and return it.

        Monotonically narrowing for the reason :meth:`learn_cap` is: a host
        that later states a lower maximum has revised its own answer downward,
        and a higher one does not contradict the count already accepted.
        Written through even when the number did not move, so a re-statement
        keeps the row fresh.
        """

        previous = self.responses_tools_max_count
        limit = limit if previous is None else min(previous, limit)
        self.responses_tools_max_count = limit
        if self.sink is not None:
            self.sink(
                FACT_RESPONSES_TOOLS_MAX_COUNT,
                PROVIDER_WIDE_MODEL_ID,
                limit,
                "",
                evidence,
            )
        return limit

    def responses_tool_choice_refused(self, model: str) -> bool:
        """Whether this model has been proven to take only ``auto``."""

        return model in self.responses_tool_choice_auto_only

    def responses_tool_choice_learned_on(self, model: str) -> str:
        """The ISO date that refusal was proven, or ``""`` if it never was."""

        return self.responses_tool_choice_auto_only.get(model, "")

    def remember_responses_tool_choice_refusal(
        self, model: str, *, evidence: str = ""
    ) -> bool:
        """Record a proven ``tool_choice`` refusal; ``False`` when already known.

        Reached only once the request without ``tool_choice`` was actually
        accepted, exactly like :meth:`remember_stream_usage_refusal`: a 400
        that merely mentioned the field is not proof that dropping it is what
        fixed the request.
        """

        already_known = model in self.responses_tool_choice_auto_only
        self.responses_tool_choice_auto_only[model] = date.today().isoformat()
        if self.sink is not None:
            self.sink(FACT_RESPONSES_TOOL_CHOICE_AUTO_ONLY, model, True, "", evidence)
        return not already_known

    def responses_tool_schema_details(self) -> tuple[str, ...]:
        """Every refused keyword class, in a stable order.

        Sorted rather than insertion-ordered because the order decides the
        order the sweeps run in, and therefore the bytes: two processes that
        learned the same two facts in different orders must still send the
        same catalogue, or the vendor's prompt-cache prefix would depend on
        which 400 arrived first.
        """

        return tuple(sorted(self.responses_tool_schema_keywords))

    def responses_tool_schema_learned_on(self, detail: str) -> str:
        """The ISO date that refusal was proven, or ``""`` if it never was."""

        return self.responses_tool_schema_keywords.get(detail, "")

    def remember_responses_tool_schema_refusal(
        self, detail: str, *, evidence: str = ""
    ) -> bool:
        """Record a proven schema-keyword refusal; ``False`` when already known.

        Reached only once the swept catalogue was actually accepted, the rule
        every inference in this file keeps: a 400 that merely named a keyword
        is not proof that removing it is what fixed the request. The host
        *stated* the keyword, which is why the row lives on the stated clock;
        what it stated is confirmed by the retry before it is written down.
        """

        already_known = detail in self.responses_tool_schema_keywords
        self.responses_tool_schema_keywords[detail] = date.today().isoformat()
        if self.sink is not None:
            self.sink(
                FACT_RESPONSES_TOOL_SCHEMA_KEYWORD,
                PROVIDER_WIDE_MODEL_ID,
                True,
                detail,
                evidence,
            )
        return not already_known

    def stream_usage_refused(self, model: str) -> bool:
        """Whether this host has been proven to reject streamed usage here."""
        return model in self.stream_usage_unsupported

    def remember_stream_usage_refusal(self, model: str, *, evidence: str = "") -> bool:
        """Record that streamed usage was refused; ``False`` when already known.

        Learned the same way a reasoning refusal is -- only once the rewritten
        body was accepted -- because a 400 that merely mentioned the option is
        not proof that removing it is what fixed the request.
        """
        already_known = model in self.stream_usage_unsupported
        self.stream_usage_unsupported.add(model)
        if self.sink is not None:
            self.sink(FACT_STREAM_USAGE_UNSUPPORTED, model, True, "", evidence)
        return not already_known
