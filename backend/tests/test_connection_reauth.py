"""A re-authorization must not create a second account or re-import the history."""

import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account
from app.models.bank_connection import BankConnection
from app.models.transaction import Transaction
from app.providers.base import AccountData, TransactionData
from app.services.connection_service import sync_connection

PATCHES = (
    "app.services.connection_service.detect_transfer_pairs",
    "app.services.connection_service.stamp_primary_amount",
    "app.services.connection_service.apply_rules_to_transaction",
)


async def _connection(session: AsyncSession, user_id: uuid.UUID, last_sync) -> BankConnection:
    conn = BankConnection(
        id=uuid.uuid4(), user_id=user_id, provider="test",
        external_id=f"ext-{uuid.uuid4().hex[:8]}", institution_name="Bank",
        credentials={"token": "fake"}, status="active", last_sync_at=last_sync,
        created_at=datetime.now(timezone.utc),
    )
    session.add(conn)
    await session.commit()
    return conn


def _provider(accounts: list[AccountData], transactions: list[TransactionData]) -> AsyncMock:
    provider = AsyncMock()
    provider.refresh_credentials = AsyncMock(return_value={"token": "t"})
    provider.get_accounts = AsyncMock(return_value=accounts)
    provider.get_transactions = AsyncMock(return_value=transactions)
    return provider


def _tx(external_id: str, description: str, amount: str, day: date) -> TransactionData:
    return TransactionData(
        external_id=external_id, description=description, amount=Decimal(amount),
        date=day, type="debit", currency="EUR",
    )


async def _run(session, conn, workspace, user, provider):
    with patch("app.services.connection_service.get_provider", return_value=provider), \
         patch(PATCHES[0], new_callable=AsyncMock), \
         patch(PATCHES[1], new_callable=AsyncMock), \
         patch(PATCHES[2], new_callable=AsyncMock):
        return await sync_connection(session, conn.id, workspace.id, user.id)


async def _accounts(session, conn) -> list[Account]:
    return list(await session.scalars(select(Account).where(Account.connection_id == conn.id)))


async def _txs(session, account_id) -> list[Transaction]:
    return list(await session.scalars(
        select(Transaction).where(Transaction.account_id == account_id, Transaction.source == "sync")
    ))


@pytest.mark.asyncio
async def test_new_provider_ids_after_reauth_keep_one_account_and_no_duplicates(
    session: AsyncSession, test_user, test_workspace
):
    day = date.today() - timedelta(days=3)
    conn = await _connection(session, test_user.id, datetime.now(timezone.utc))
    first = _provider(
        [AccountData(external_id="uid-old", name="CHECKING", type="checking",
                     balance=Decimal("100"), currency="EUR")],
        [_tx("tx-old-1", "Coffee", "2.50", day), _tx("tx-old-2", "Rent", "600", day)],
    )
    await _run(session, conn, test_workspace, test_user, first)
    account = (await _accounts(session, conn))[0]
    assert len(await _txs(session, account.id)) == 2

    # Re-authorization: a new account id, and every transaction id is new.
    conn.last_sync_at = None
    await session.commit()
    second = _provider(
        [AccountData(external_id="uid-new", name="CHECKING", type="checking",
                     balance=Decimal("100"), currency="EUR")],
        [_tx("tx-new-1", "Coffee", "2.50", day), _tx("tx-new-2", "Rent", "600", day)],
    )
    await _run(session, conn, test_workspace, test_user, second)

    accounts = await _accounts(session, conn)
    assert len(accounts) == 1
    assert accounts[0].id == account.id and accounts[0].external_id == "uid-new"
    rows = await _txs(session, account.id)
    assert sorted(r.external_id for r in rows) == ["tx-new-1", "tx-new-2"]


@pytest.mark.asyncio
async def test_two_identical_same_day_transactions_stay_two(
    session: AsyncSession, test_user, test_workspace
):
    day = date.today() - timedelta(days=2)
    conn = await _connection(session, test_user.id, datetime.now(timezone.utc))
    acct = [AccountData(external_id="uid-a", name="CHECKING", type="checking",
                        balance=Decimal("0"), currency="EUR")]
    await _run(session, conn, test_workspace, test_user, _provider(
        acct, [_tx("a1", "Metro", "1.50", day), _tx("a2", "Metro", "1.50", day)]))

    conn.last_sync_at = None
    await session.commit()
    acct2 = [AccountData(external_id="uid-b", name="CHECKING", type="checking",
                         balance=Decimal("0"), currency="EUR")]
    await _run(session, conn, test_workspace, test_user, _provider(
        acct2, [_tx("b1", "Metro", "1.50", day), _tx("b2", "Metro", "1.50", day)]))

    account = (await _accounts(session, conn))[0]
    assert sorted(r.external_id for r in await _txs(session, account.id)) == ["b1", "b2"]


@pytest.mark.asyncio
async def test_a_genuinely_new_account_is_still_created(
    session: AsyncSession, test_user, test_workspace
):
    conn = await _connection(session, test_user.id, datetime.now(timezone.utc))
    await _run(session, conn, test_workspace, test_user, _provider(
        [AccountData(external_id="uid-1", name="CHECKING", type="checking",
                     balance=Decimal("1"), currency="EUR")], []))
    # Same connection now reports the old account plus a second one: nothing is re-bound.
    await _run(session, conn, test_workspace, test_user, _provider(
        [AccountData(external_id="uid-1", name="CHECKING", type="checking",
                     balance=Decimal("1"), currency="EUR"),
         AccountData(external_id="uid-2", name="CHECKING", type="checking",
                     balance=Decimal("2"), currency="EUR")], []))
    assert len(await _accounts(session, conn)) == 2


@pytest.mark.asyncio
async def test_an_unreadable_balance_does_not_zero_the_account(
    session: AsyncSession, test_user, test_workspace
):
    conn = await _connection(session, test_user.id, datetime.now(timezone.utc))
    await _run(session, conn, test_workspace, test_user, _provider(
        [AccountData(external_id="uid-1", name="CHECKING", type="checking",
                     balance=Decimal("1322.68"), currency="EUR")], []))
    await _run(session, conn, test_workspace, test_user, _provider(
        [AccountData(external_id="uid-1", name="CHECKING", type="checking",
                     balance=Decimal("0"), currency="EUR", balance_known=False)], []))
    account = (await _accounts(session, conn))[0]
    assert account.balance == Decimal("1322.68")
