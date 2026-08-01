"""Tests for error typing, message formatting and retry classification."""

from __future__ import annotations

import pytest

from gateio_nt.common.errors import (
    ACCOUNT_MODE_LABELS,
    WALLET_NOT_PROVISIONED_LABELS,
    GateioClientError,
    GateioError,
    GateioServerError,
    OrderValidationError,
    UnsupportedOrderError,
    WalletNotProvisionedError,
    WalletQueryRefusedError,
    error_from_response,
    should_retry,
)
from gateio_nt.http.margin import require_wallet


class TestErrorFromResponse:
    @pytest.mark.parametrize("status", [400, 401, 403, 404, 422, 429, 499])
    def test_4xx_maps_to_a_client_error(self, status):
        error = error_from_response(status, "SOME_LABEL", "bad request")
        assert isinstance(error, GateioClientError)
        assert isinstance(error, GateioError)
        assert not isinstance(error, GateioServerError)

    @pytest.mark.parametrize("status", [500, 502, 503, 504])
    def test_5xx_maps_to_a_server_error(self, status):
        error = error_from_response(status, "SERVER_ERROR", "boom")
        assert isinstance(error, GateioServerError)
        assert isinstance(error, GateioError)
        assert not isinstance(error, GateioClientError)

    def test_all_three_fields_are_preserved(self):
        error = error_from_response(400, "INVALID_PARAM_VALUE", "amount too small")
        assert error.status == 400
        assert error.label == "INVALID_PARAM_VALUE"
        assert error.message == "amount too small"

    def test_message_carries_status_label_and_text(self):
        error = error_from_response(429, "TOO_MANY_REQUESTS", "slow down")
        text = str(error)
        assert "429" in text
        assert "TOO_MANY_REQUESTS" in text
        assert "slow down" in text

    def test_errors_are_raisable_exceptions(self):
        with pytest.raises(GateioError):
            raise error_from_response(400, "LABEL", "message")


class TestShouldRetry:
    @pytest.mark.parametrize("status", [500, 502, 503, 504])
    def test_server_errors_are_retryable(self, status):
        assert should_retry(error_from_response(status, "SERVER_ERROR", "boom")) is True

    def test_rate_limiting_is_retryable(self):
        assert should_retry(error_from_response(429, "TOO_MANY_REQUESTS", "slow down")) is True

    @pytest.mark.parametrize(
        "label",
        ["TOO_MANY_REQUESTS", "SERVER_ERROR", "INTERNAL", "TIMEOUT", "REQUEST_EXPIRED"],
    )
    def test_transient_labels_are_retryable_whatever_the_status(self, label):
        assert should_retry(error_from_response(400, label, "transient")) is True

    @pytest.mark.parametrize(
        "label",
        [
            "INVALID_KEY",
            "INVALID_SIGNATURE",
            "INVALID_PARAM_VALUE",
            "BALANCE_NOT_ENOUGH",
            "ORDER_NOT_FOUND",
            "READ_ONLY",
            "IP_FORBIDDEN",
        ],
    )
    def test_permanent_client_errors_are_not_retryable(self, label):
        assert should_retry(error_from_response(400, label, "nope")) is False

    @pytest.mark.parametrize(
        "error",
        [ValueError("not a venue error"), RuntimeError("boom"), Exception("generic")],
    )
    def test_unrelated_exceptions_are_not_retryable(self, error):
        assert should_retry(error) is False

    def test_wallet_not_provisioned_is_not_retryable(self):
        assert should_retry(WalletNotProvisionedError("no futures wallet")) is False


class TestLabelSets:
    def test_wallet_not_provisioned_labels(self):
        assert "USER_NOT_FOUND" in WALLET_NOT_PROVISIONED_LABELS

    @pytest.mark.parametrize(
        "label",
        ["INVALID_UNIFIED_ACCOUNT", "UNIFIED_ACCOUNT_NOT_ACTIVATED", "FORBIDDEN"],
    )
    def test_account_mode_labels(self, label):
        assert label in ACCOUNT_MODE_LABELS

    def test_the_label_sets_do_not_overlap(self):
        assert not WALLET_NOT_PROVISIONED_LABELS & ACCOUNT_MODE_LABELS


async def _raise(error: Exception):
    raise error


class TestRefusalIsTypedApartFromAbsence:
    """A missing wallet and a refused query are two different facts.

    Only the first says anything about what the ledger holds. Collapsing them
    into one type makes "absence" the default reading of a refusal at every catch
    site, and where that site is the position query the absence becomes a FLAT
    report the venue never made — the engine then squares a live book with a
    reconciliation order and an inferred fill.
    """

    @pytest.mark.parametrize("label", sorted(ACCOUNT_MODE_LABELS))
    async def test_a_refusal_carries_the_refusal_type(self, label):
        with pytest.raises(WalletQueryRefusedError) as excinfo:
            await require_wallet(_raise(GateioClientError(403, label, "nope")), "the wallet")

        assert label in str(excinfo.value)

    @pytest.mark.parametrize("label", sorted(WALLET_NOT_PROVISIONED_LABELS))
    async def test_an_unprovisioned_wallet_does_not(self, label):
        with pytest.raises(WalletNotProvisionedError) as excinfo:
            await require_wallet(_raise(GateioClientError(400, label, "nope")), "the wallet")

        assert not isinstance(excinfo.value, WalletQueryRefusedError)

    @pytest.mark.parametrize("label", sorted(ACCOUNT_MODE_LABELS))
    async def test_a_refusal_is_still_caught_as_an_unavailable_wallet(self, label):
        """The subclass is what keeps every existing handler correct.

        A caller that only reads balances is right to treat both alike: a ledger
        it cannot read keeps its previous figures whichever fact it is. Making
        the refusal a sibling type instead would turn a permission error on a
        secondary wallet into a hard start-up failure.
        """
        with pytest.raises(WalletNotProvisionedError):
            await require_wallet(_raise(GateioClientError(403, label, "nope")), "the wallet")

    async def test_an_unrelated_venue_error_passes_through_untouched(self):
        with pytest.raises(GateioClientError):
            await require_wallet(
                _raise(GateioClientError(400, "INVALID_PARAM_VALUE", "bad")),
                "the wallet",
            )

    def test_the_refusal_type_is_not_retryable_either(self):
        assert should_retry(WalletQueryRefusedError("the key may not read that ledger")) is False


class TestTheDistinctionIsReachableFromOutsideThePackage:
    """The type exists so that a *caller* can catch a refusal before an absence.

    It was introduced without being re-exported, so the only import path that
    reached it was `gateio_nt.common.errors`, which the documentation does
    not advertise as public. A distinction available to the adapter and to
    nobody else is not a distinction the package offers.
    """

    def test_both_wallet_error_types_are_exported_from_the_package_root(self):
        import gateio_nt

        for name in ("WalletNotProvisionedError", "WalletQueryRefusedError"):
            assert name in gateio_nt.__all__
            assert getattr(gateio_nt, name, None) is not None

    def test_a_caller_can_catch_the_refusal_before_the_absence(self):
        """Order matters: the refusal is a subclass, so it must be caught first."""
        from gateio_nt import WalletNotProvisionedError as Absence
        from gateio_nt import WalletQueryRefusedError as Refusal

        assert issubclass(Refusal, Absence)

        caught = None
        try:
            raise Refusal("the venue would not answer")
        except Refusal as e:
            caught = ("refusal", e)
        except Absence as e:  # pragma: no cover - the subclass clause wins
            caught = ("absence", e)

        assert caught is not None
        assert caught[0] == "refusal"


class TestAdapterLevelErrors:
    @pytest.mark.parametrize(
        "error_class",
        [
            WalletNotProvisionedError,
            WalletQueryRefusedError,
            OrderValidationError,
            UnsupportedOrderError,
        ],
    )
    def test_adapter_errors_are_plain_exceptions_not_venue_errors(self, error_class):
        """They describe local conditions, so they carry no HTTP status."""
        error = error_class("explanation")
        assert isinstance(error, Exception)
        assert not isinstance(error, GateioError)
        assert "explanation" in str(error)

    def test_unsupported_order_is_distinct_from_order_validation(self):
        """One means "the venue would reject it", the other "we cannot express it"."""
        assert not issubclass(UnsupportedOrderError, OrderValidationError)
        assert not issubclass(OrderValidationError, UnsupportedOrderError)
