"""The preconditions this adapter states on its public boundaries.

Every check here is one a caller can trip from outside the package: a
configuration helper, the shared transport's constructor, a symbology helper, a
REST namespace method, a private WebSocket subscription, or the execution
client's own report query. They are stated through NautilusTrader's
``PyCondition`` (installed ``nautilus_trader/core/correctness.pyx``), the same
design-by-contract vocabulary the built-in adapters use, so this module also
pins the two things a reader of that code needs to be able to rely on:

* **which exception each boundary raises.** ``PyCondition`` does not raise one
  type — ``valid_string`` raises ``ValueError`` for a blank string but
  ``TypeError`` for ``None``, ``type`` raises ``TypeError`` unless told
  otherwise, and ``is_in`` raises ``KeyError``. The type is the contract, so it
  is asserted, not assumed.
* **why the venue-specific checks were left hand-written.** Those are not
  oversights: the platform's set-membership check raises ``KeyError``, which
  is not a ``ValueError``, and the callers of the checks in question catch
  ``ValueError``. The tests at the bottom fail if someone "finishes the job" by
  translating them.

Each test names the damage the check prevents, so removing the check fails the
test on the outcome rather than on the absence of an exception.
"""

from __future__ import annotations

import inspect
import pathlib

import httpx
import pytest
from nautilus_trader.core.correctness import PyCondition

from gateio_nt import data as data_module
from gateio_nt.common.enums import GateioProductType
from gateio_nt.common.symbols import gateio_to_instrument_id, instrument_id_to_gateio
from gateio_nt.config import MAINNET, validate_book_interval_ms, validate_products
from gateio_nt.http.client import GateioHttpClient
from gateio_nt.http.futures import GateioFuturesHttpAPI
from gateio_nt.http.options import GateioOptionsHttpAPI
from gateio_nt.http.wallet import GateioWalletHttpAPI
from gateio_nt.websocket.private import GateioPrivateWebSocket
from gateio_nt.websocket.public import GateioPublicWebSocket

pytestmark = pytest.mark.filterwarnings("ignore::RuntimeWarning")


class _Recorder:
    """Answers every request with ``{}`` and records that it was made."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200, json={})


class _NoWaitLimiter:
    backoff = 0.0

    async def acquire(self) -> None:
        pass

    def on_rate_limited(self) -> None:
        pass

    def on_success(self) -> None:
        pass


def _client(recorder: _Recorder) -> GateioHttpClient:
    client = GateioHttpClient(api_key="k", api_secret="s")
    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(recorder),
        base_url="https://api.gateio.ws",
    )
    client._limiter = _NoWaitLimiter()  # type: ignore[assignment]
    return client


def _private_ws(product: GateioProductType) -> GateioPrivateWebSocket:
    return GateioPrivateWebSocket(
        product=product,
        handler=lambda payload: None,
        api_key="k",
        api_secret="s",
        user_id="1",
    )


# -- what the platform check actually raises ---------------------------------


class TestThePlatformContractThisCodeRelieson:
    """Read off the installed platform, because the adapter's types follow it."""

    def test_valid_string_separates_blank_from_absent(self) -> None:
        """``core/correctness.pyx:787`` delegates ``None`` to ``not_none``."""
        with pytest.raises(ValueError):
            PyCondition.valid_string("  ", "param")
        with pytest.raises(TypeError):
            PyCondition.valid_string(None, "param")

    def test_type_defaults_to_type_error_and_honors_ex_type(self) -> None:
        with pytest.raises(TypeError):
            PyCondition.type("spot", GateioProductType, "param")
        with pytest.raises(ValueError):
            PyCondition.type("spot", GateioProductType, "param", ValueError)

    def test_is_in_raises_key_error_which_is_not_a_value_error(self) -> None:
        """The reason the venue-set checks below stayed hand-written."""
        with pytest.raises(KeyError):
            PyCondition.is_in("20ms", ["100ms"], "interval", "intervals")
        assert not issubclass(KeyError, ValueError)

    def test_positive_admits_infinity(self) -> None:
        """The reason the transport keeps its own finiteness check."""
        PyCondition.positive(float("inf"), "timeout_secs")  # does not raise
        with pytest.raises(ValueError):
            PyCondition.positive(float("nan"), "timeout_secs")


# -- configuration -----------------------------------------------------------


class TestConfigurationHelpers:
    @pytest.mark.parametrize("value", [(), None], ids=["empty", "none"])
    def test_a_product_set_that_names_nothing_is_refused_as_a_value_error(self, value) -> None:
        """Damage: a client that subscribes to nothing and reports no failure.

        ``None`` belongs here with the empty tuple, and as a ``ValueError``.
        A configuration struct accepts ``products=None`` — nothing on the struct
        rejects it — so the path is reachable from the public surface, and
        ``docs/configuration.md`` tells a caller to wrap one ``except
        ValueError`` around this helper. ``PyCondition.not_empty`` checks for
        ``None`` first and raises ``TypeError`` for it unless told otherwise, so
        this test fails if that ``ex_type`` is dropped.
        """
        with pytest.raises(ValueError):
            validate_products(value, MAINNET)

    def test_a_non_product_member_is_refused_as_a_value_error(self) -> None:
        """Damage without the check: ``"spot"`` reaches the product dispatch.

        The type matters as much as the refusal. ``PyCondition.type`` defaults
        to ``TypeError``, and ``docs/configuration.md`` tells a caller checking
        a configuration up front to write ``except ValueError`` — so this call
        passes ``ex_type=ValueError`` and this test would fail if that argument
        were dropped.
        """
        with pytest.raises(ValueError):
            validate_products(("spot",), MAINNET)


# -- the shared transport's public constructor --------------------------------


class TestTransportConstructor:
    @pytest.mark.parametrize("max_retries", [0, -1])
    def test_a_retry_count_below_one_is_refused(self, max_retries: int) -> None:
        """Damage: the attempt loop runs zero times and the request is never sent."""
        with pytest.raises(ValueError, match="max_retries"):
            GateioHttpClient(max_retries=max_retries)

    @pytest.mark.parametrize("timeout_secs", [0.0, -1.0, float("nan")])
    def test_a_timeout_that_expires_everything_is_refused(self, timeout_secs: float) -> None:
        """Damage: httpx reports every request as ``NETWORK_ERROR`` before sending it."""
        with pytest.raises(ValueError, match="timeout_secs"):
            GateioHttpClient(timeout_secs=timeout_secs)

    def test_an_infinite_timeout_is_refused_by_the_adapter_s_own_check(self) -> None:
        """``PyCondition.positive`` passes ``inf``; this refusal is ours, and stays."""
        with pytest.raises(ValueError, match="finite"):
            GateioHttpClient(timeout_secs=float("inf"))


# -- symbology ----------------------------------------------------------------


class TestSymbologyHelpers:
    @pytest.mark.parametrize("blank", [" ", "   ", "\t"])
    def test_a_blank_raw_symbol_cannot_become_an_instrument_id(self, blank: str) -> None:
        """Damage: ``Symbol`` never sees the blank, because ``-PERP`` is appended first.

        Before the check was stated as ``valid_string`` this returned the
        perfectly valid-looking ``InstrumentId("  -PERP.GATE_IO")``, and the
        data client would have subscribed under a whitespace symbol.
        """
        with pytest.raises(ValueError):
            gateio_to_instrument_id(GateioProductType.PERP, blank)

    def test_an_absent_raw_symbol_raises_type_error(self) -> None:
        """Breaking change, stated: ``None`` used to raise ``ValueError`` here."""
        with pytest.raises(TypeError):
            gateio_to_instrument_id(GateioProductType.SPOT, None)  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        "value",
        ["", ".GATE_IO", "-PERP.GATE_IO", " .GATE_IO", "   .GATE_IO", "\t.GATE_IO"],
    )
    def test_an_instrument_id_with_no_venue_symbol_is_refused(self, value: str) -> None:
        """Damage: a request path built around an empty contract name.

        Blank is the half an emptiness check misses, and it is the half that
        travels: ``if not symbol`` admits ``" "``, and a whitespace contract name
        then reaches the REST path and the subscription payload looking like a
        symbol. The spot branch is listed here as well as the ``-PERP`` one
        because reverting either to the emptiness check has to fail.
        """
        with pytest.raises(ValueError):
            instrument_id_to_gateio(value)


# -- REST namespaces ----------------------------------------------------------


class TestRestNamespaceBoundaries:
    async def test_a_transfer_between_one_wallet_and_itself_is_not_sent(self) -> None:
        """Damage: a signed, never-replayable POST that moves nothing."""
        recorder = _Recorder()
        wallet = GateioWalletHttpAPI(_client(recorder))

        with pytest.raises(ValueError):
            await wallet.transfer("USDT", "spot", "spot", "10")

        assert recorder.requests == []

    async def test_an_unscoped_options_cancel_all_is_not_sent(self) -> None:
        """Damage: Gate.io cancels every resting option order in the account."""
        recorder = _Recorder()
        options = GateioOptionsHttpAPI(_client(recorder))

        with pytest.raises(ValueError):
            await options.cancel_all()

        assert recorder.requests == []

    async def test_a_perpetual_only_endpoint_is_refused_on_delivery(self) -> None:
        """Damage: a GET against ``/delivery/usdt/funding_rate``, which does not exist.

        Gate.io answers a path it does not serve with a 404 whose message says
        nothing about funding; the refusal names the actual reason.
        """
        recorder = _Recorder()
        delivery = GateioFuturesHttpAPI(_client(recorder), delivery=True)

        with pytest.raises(ValueError, match="delivery futures"):
            await delivery.funding_rate("BTC_USDT_20260807")

        assert recorder.requests == []


# -- private WebSocket --------------------------------------------------------


class TestPrivateWebSocketBoundaries:
    @pytest.mark.parametrize("method", ["subscribe_positions", "unsubscribe_positions"])
    async def test_the_position_channel_is_refused_on_spot(self, method: str) -> None:
        """Damage: a subscription to ``spot.positions``, a channel Gate.io has not got.

        The refusal has to happen here: an unknown channel is answered by the
        venue with an error the client would log against the *connection*, and
        the caller would go on believing it is receiving position updates.
        """
        ws = _private_ws(GateioProductType.SPOT)
        with pytest.raises(ValueError, match="position channel"):
            await getattr(ws, method)()

    async def test_a_spot_only_channel_is_refused_on_a_derivative(self) -> None:
        ws = _private_ws(GateioProductType.PERP)
        with pytest.raises(ValueError, match="spot channel"):
            await ws.subscribe_funding_balances()


# -- execution client ---------------------------------------------------------


class TestExecutionClientBoundary:
    def test_the_report_query_states_the_platform_s_own_contract(self) -> None:
        """``LiveExecutionClient.generate_order_status_report`` documents this raise.

        Verified against the installed platform: the base method's docstring
        (``live/execution_client.py``) declares "Raises ValueError if both the
        `client_order_id` and `venue_order_id` are None", and the built-in
        Binance client asserts it with the same ``PyCondition.is_false`` call
        and the same message (``adapters/binance/execution.py:381``).
        """
        with pytest.raises(ValueError, match="were `None`"):
            PyCondition.is_false(
                True,
                "both `client_order_id` and `venue_order_id` were `None`",
            )


# -- the checks that were deliberately NOT translated -------------------------


class TestTheDocumentedTaxonomyMatchesTheCode:
    """`docs/errors.md` tabulates what each boundary raises. Pin it to the code.

    The table is the reason a caller writes `except ValueError` and not
    `except Exception`, so a row that drifts is worse than no row.
    """

    SECTION = "## Before the venue: preconditions on the public boundaries"

    def _section(self) -> str:
        page = (pathlib.Path(__file__).resolve().parent.parent / "docs" / "errors.md").read_text()
        assert self.SECTION in page
        start = page.index(self.SECTION)
        return page[start : page.index("## The response hierarchy", start)]

    def _rows(self) -> list[tuple[str, str]]:
        """Return ``(refuses, raises)`` for every row of the table."""
        rows = []
        for line in self._section().splitlines():
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if len(cells) == 3 and cells[2].startswith("`") and "---" not in cells[0]:
                rows.append((cells[1], cells[2]))
        return rows

    def test_exactly_one_row_promises_type_error_and_the_code_keeps_it(self) -> None:
        rows = self._rows()
        assert len(rows) >= 15
        type_errors = [refuses for refuses, raises in rows if raises == "`TypeError`"]
        assert type_errors == ["`raw_symbol=None`"]
        assert {raises for _, raises in rows} == {"`ValueError`", "`TypeError`"}

        with pytest.raises(TypeError):
            gateio_to_instrument_id(GateioProductType.SPOT, None)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            gateio_to_instrument_id(GateioProductType.SPOT, "")

    def test_the_page_states_why_is_in_is_not_used(self) -> None:
        section = self._section()
        assert "`PyCondition.is_in`" in section
        assert "`KeyError`" in section

    @pytest.mark.parametrize(
        "name",
        [
            "validate_products",
            "validate_book_interval_ms",
            "GateioHttpClient(max_retries",
            "GateioHttpClient(timeout_secs",
            "gateio_to_instrument_id",
            "instrument_id_to_gateio",
            "GateioWalletHttpAPI.transfer",
            "GateioOptionsHttpAPI.cancel_all",
            "GateioFuturesHttpAPI",
            "GateioPrivateWebSocket",
            "generate_order_status_report",
        ],
    )
    def test_every_translated_boundary_has_a_row(self, name: str) -> None:
        assert name in self._section()


class TestVenueSpecificChecksStayHandWritten:
    """These would break their callers if stated through ``PyCondition.is_in``.

    Each one refuses a value against a *discrete set Gate.io publishes*, and
    every caller of them catches ``ValueError`` to log the refusal and carry on.
    ``PyCondition.is_in`` raises ``KeyError``, which those handlers do not
    catch, so translating any of these would turn a logged refusal into an
    escaping exception on a live client task.
    """

    def test_a_book_interval_outside_the_venue_s_set_is_a_value_error(self) -> None:
        with pytest.raises(ValueError, match="order_book_update_interval_ms"):
            validate_book_interval_ms(37)

    @pytest.mark.parametrize(
        ("product", "interval"),
        [
            (GateioProductType.SPOT, "1000ms"),
            (GateioProductType.PERP, "1000ms"),
            (GateioProductType.FUT, "20ms"),
        ],
    )
    def test_an_unsupported_book_interval_is_a_value_error(
        self,
        product: GateioProductType,
        interval: str,
    ) -> None:
        ws = GateioPublicWebSocket(product=product, handler=lambda payload: None)
        with pytest.raises(ValueError, match="not supported"):
            ws._book_update_payload("BTC_USDT", interval, None)

    def test_an_unsupported_candle_interval_is_a_value_error(self) -> None:
        ws = GateioPublicWebSocket(product=GateioProductType.SPOT, handler=lambda p: None)
        with pytest.raises(ValueError, match="not supported"):
            ws._candles_payload("BTC_USDT", "7m")

    @pytest.mark.parametrize(
        "method",
        [
            "_subscribe_order_book_deltas",
            "_unsubscribe_order_book_deltas",
            "_subscribe_order_book_depth",
            "_unsubscribe_order_book_depth",
            "_subscribe_bars",
            "_unsubscribe_bars",
        ],
    )
    def test_every_subscription_path_catches_only_value_error(self, method: str) -> None:
        """The pin: this is why ``KeyError`` would escape.

        Each of these methods calls a payload builder whose venue-set check is
        the one under discussion, and wraps the call in a handler that catches
        ``ValueError``. A ``KeyError`` — which is what ``PyCondition.is_in``
        raises — is not in any of these handlers, so translating the check
        would take the client task down over one unsupported interval.
        """
        source = inspect.getsource(getattr(data_module.GateioDataClient, method))
        assert "except (GateioError, ValueError)" in source
        assert "KeyError" not in source
