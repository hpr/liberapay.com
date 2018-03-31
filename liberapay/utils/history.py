from calendar import monthrange
from datetime import datetime
from itertools import chain

from pando import Response
from pando.utils import utc, utcnow

from ..website import website
from .currencies import MoneyBasket
from . import group_by


def get_start_of_current_utc_day():
    """Returns a `datetime` for the start of the current day in the UTC timezone.
    """
    now = utcnow()
    return datetime(now.year, now.month, now.day, tzinfo=utc)


def month_minus_one(year, month, day):
    """Returns a `datetime` for the start of the given day in the previous month.

    >>> month_minus_one(2012, 3, 29)  # doctest: +ELLIPSIS
    datetime.datetime(2012, 2, 29, 0, 0, tzinfo=...)
    >>> month_minus_one(2012, 5, 31)  # doctest: +ELLIPSIS
    datetime.datetime(2012, 4, 30, 0, 0, tzinfo=...)
    """
    year, month = (year - 1, 12) if month == 1 else (year, month - 1)
    day = min(day, monthrange(year, month)[1])
    return datetime(year, month, day, tzinfo=utc)


def month_plus_one(year, month, day):
    """Returns a `datetime` for the start of the given day in the following month.

    >>> month_plus_one(2012, 1, 31)  # doctest: +ELLIPSIS
    datetime.datetime(2012, 2, 29, 0, 0, tzinfo=...)
    >>> month_plus_one(2013, 1, 31)  # doctest: +ELLIPSIS
    datetime.datetime(2013, 2, 28, 0, 0, tzinfo=...)
    """
    year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    day = min(day, monthrange(year, month)[1])
    return datetime(year, month, day, tzinfo=utc)


def get_end_of_period_balances(db, participant, period_end, today):
    """Get the participant's balances (`MoneyBasket`) at the end of the given period.

    `period_end` and `today` should be UTC `datetime` objects.
    """
    if period_end > today:
        return db.one("""
            SELECT basket_sum(balance)
              FROM wallets
             WHERE owner = %s
               AND is_current
        """, (participant.id,))
    if period_end < participant.join_time:
        return MoneyBasket()

    balances = db.one("""
        SELECT balances
          FROM balances_at
         WHERE participant = %s
           AND "at" = %s
    """, (participant.id, period_end))
    if balances is not None:
        return balances

    id = participant.id
    prev_period_end = month_minus_one(period_end.year, period_end.month, participant.join_time.day)
    balances = get_end_of_period_balances(db, participant, prev_period_end, today)
    balances += db.one("""
        SELECT (
                  SELECT basket_sum(ee.wallet_delta) AS a
                    FROM exchanges e
                    JOIN exchange_events ee ON ee.exchange = e.id
                   WHERE e.participant = %(id)s
                     AND ee.timestamp >= %(prev_period_end)s
                     AND ee.timestamp < %(period_end)s
               ) + (
                  SELECT basket_sum(-amount) AS a
                    FROM transfers
                   WHERE tipper = %(id)s
                     AND timestamp >= %(prev_period_end)s
                     AND timestamp < %(period_end)s
                     AND status = 'succeeded'
               ) + (
                  SELECT basket_sum(amount) AS a
                    FROM transfers
                   WHERE tippee = %(id)s
                     AND timestamp >= %(prev_period_end)s
                     AND timestamp < %(period_end)s
                     AND status = 'succeeded'
               ) AS delta
    """, locals())
    db.run("""
        INSERT INTO balances_at
                    (participant, at, balances)
             VALUES (%s, %s, %s)
        ON CONFLICT (participant, at) DO NOTHING
    """, (participant.id, period_end, balances))
    return balances


def get_ledger_events(db, participant, year=None, month=-1):
    events = iter_payday_events(db, participant, year, month)
    try:
        totals = next(events)
        if totals['kind'] != 'totals':
            # it's not what we expected, put it back
            events = chain((totals,), events)
            totals = None
    except StopIteration:
        totals, events = None, ()
    return totals, events


def iter_payday_events(db, participant, year=None, month=-1):
    """Yields payday events for the given participant.
    """
    today = get_start_of_current_utc_day()
    year = year or today.year
    month = month or today.month

    if month == -1:
        period_start = datetime(year, 1, 1, tzinfo=utc)
        period_end = datetime(year + 1, 1, 1, tzinfo=utc)
    else:
        start_day = participant.join_time.day
        max_month_day = monthrange(year, month)[1]
        period_start = datetime(year, month, min(start_day, max_month_day), tzinfo=utc)
        period_end = month_plus_one(year, month, start_day)

    id = participant.id
    exchanges = db.all("""
        SELECT ee.timestamp, ee.status, ee.error, ee.wallet_delta
             , e.amount, e.fee, e.recorder, e.refund_ref, e.timestamp AS ctime
             , e.id AS exchange_id
          FROM exchanges e
          JOIN exchange_events ee ON ee.exchange = e.id
         WHERE e.participant = %(id)s
           AND ee.timestamp >= %(period_start)s
           AND ee.timestamp < %(period_end)s
           AND ee.status <> 'created'
           AND (ee.status = e.status OR ee.wallet_delta <> 0)
    """, locals(), back_as=dict)
    transfers = db.all("""
        SELECT t.*, p.username, (SELECT username FROM participants WHERE id = team) AS team_name
          FROM transfers t
          JOIN participants p ON p.id = tipper
         WHERE t.tippee=%(id)s
           AND t.timestamp >= %(period_start)s
           AND t.timestamp < %(period_end)s
        UNION ALL
        SELECT t.*, p.username, (SELECT username FROM participants WHERE id = team) AS team_name
          FROM transfers t
          JOIN participants p ON p.id = tippee
         WHERE t.tipper=%(id)s
           AND t.timestamp >= %(period_start)s
           AND t.timestamp < %(period_end)s
    """, locals(), back_as=dict)

    if not (exchanges or transfers):
        return

    if transfers:
        successes = [t for t in transfers if t['status'] == 'succeeded' and not t['refund_ref']]
        regular_donations = [t for t in successes if t['context'] in ('tip', 'take')]
        reimbursements = [t for t in successes if t['context'] == 'expense']
        regular_donations_by_currency = group_by(regular_donations, lambda t: t['amount'].currency)
        reimbursements_by_currency = group_by(reimbursements, lambda t: t['amount'].currency)
        yield dict(
            kind='totals',
            regular_donations=dict(
                sent=MoneyBasket(t['amount'] for t in regular_donations if t['tipper'] == id),
                received=MoneyBasket(t['amount'] for t in regular_donations if t['tippee'] == id),
                npatrons={k: len(set(t['tipper'] for t in transfers if t['tippee'] == id))
                          for k, transfers in regular_donations_by_currency.items()},
                ntippees={k: len(set(t['tippee'] for t in transfers if t['tipper'] == id))
                          for k, transfers in regular_donations_by_currency.items()},
            ),
            reimbursements=dict(
                sent=MoneyBasket(t['amount'] for t in reimbursements if t['tipper'] == id),
                received=MoneyBasket(t['amount'] for t in reimbursements if t['tippee'] == id),
                npayers={k: len(set(t['tipper'] for t in transfers if t['tippee'] == id))
                         for k, transfers in reimbursements_by_currency.items()},
                nrecipients={k: len(set(t['tippee'] for t in transfers if t['tipper'] == id))
                             for k, transfers in reimbursements_by_currency.items()},
            ),
        )
        del successes, regular_donations, reimbursements

    payday_dates = db.all("""
        SELECT ts_start::date
          FROM paydays
         WHERE ts_start IS NOT NULL
      ORDER BY ts_start ASC
    """)

    balances = get_end_of_period_balances(db, participant, period_end, today)
    prev_date = None
    get_timestamp = lambda e: e['timestamp']
    events = sorted(exchanges+transfers, key=get_timestamp, reverse=True)
    day_events, day_open = None, None  # for pyflakes
    for event in events:

        event['balances'] = balances

        event_date = event['timestamp'].date()
        if event_date != prev_date:
            if prev_date:
                day_open['wallet_deltas'] = day_open['balances'] - balances
                yield day_open
                for e in day_events:
                    yield e
                yield dict(kind='day-close', balances=balances)
            day_events = []
            day_open = dict(kind='day-open', date=event_date, balances=balances)
            if payday_dates:
                while payday_dates and payday_dates[-1] > event_date:
                    payday_dates.pop()
                payday_date = payday_dates[-1] if payday_dates else None
                if event_date == payday_date:
                    day_open['payday_number'] = len(payday_dates)
            prev_date = event_date

        if 'fee' in event:
            balances -= event['wallet_delta']
            if event['amount'] > 0:
                kind = 'payout-refund' if event['refund_ref'] else 'charge'
                event['bank_delta'] = -event['amount'] - max(event['fee'], 0)
            else:
                if event['refund_ref']:
                    kind = 'payin-refund'
                elif event['status'] == 'failed':
                    kind = 'payout-refund'
                    event['status'] = 'succeeded'
                else:
                    kind = 'credit'
                event['bank_delta'] = -event['amount'] - min(event['fee'], 0)
        else:
            kind = 'transfer'
            if event['tippee'] == id:
                event['wallet_delta'] = event['amount']
            else:
                event['wallet_delta'] = -event['amount']
            if event['status'] == 'succeeded':
                balances -= event['wallet_delta']
            if event['context'] == 'expense':
                event['invoice_url'] = participant.path('invoices/%s' % event['invoice'])
        event['kind'] = kind

        day_events.append(event)

    day_open['wallet_delta'] = day_open['balances'] - balances
    yield day_open
    for e in day_events:
        yield e
    yield dict(kind='day-close', balances=balances)


def export_history(participant, year, mode, key, back_as='namedtuple', require_key=False):
    db = participant.db
    base_url = website.canonical_url + '/~'
    params = dict(id=participant.id, year=year, base_url=base_url)
    out = {}
    if mode == 'aggregate':
        out['given'] = lambda: db.all("""
            SELECT (%(base_url)s || t.tippee::text) AS donee_url,
                   min(p.username) AS donee_username, basket_sum(t.amount) AS amount
              FROM transfers t
              JOIN participants p ON p.id = t.tippee
             WHERE t.tipper = %(id)s
               AND extract(year from t.timestamp) = %(year)s
               AND t.status = 'succeeded'
               AND t.context IN ('tip', 'take')
               AND t.refund_ref IS NULL
          GROUP BY t.tippee
        """, params, back_as=back_as)
        out['reimbursed'] = lambda: db.all("""
            SELECT (%(base_url)s || t.tippee::text) AS recipient_url,
                   min(p.username) AS recipient_username, basket_sum(t.amount) AS amount
              FROM transfers t
              JOIN participants p ON p.id = t.tippee
             WHERE t.tipper = %(id)s
               AND extract(year from t.timestamp) = %(year)s
               AND t.status = 'succeeded'
               AND t.context = 'expense'
          GROUP BY t.tippee
        """, params, back_as=back_as)
        out['taken'] = lambda: db.all("""
            SELECT (%(base_url)s || t.team::text) AS team_url,
                   min(p.username) AS team_username, basket_sum(t.amount) AS amount
              FROM transfers t
              JOIN participants p ON p.id = t.team
             WHERE t.tippee = %(id)s
               AND t.context = 'take'
               AND extract(year from t.timestamp) = %(year)s
               AND t.status = 'succeeded'
          GROUP BY t.team
        """, params, back_as=back_as)
    else:
        out['exchanges'] = lambda: db.all("""
            SELECT timestamp, amount, fee, status, note
              FROM exchanges
             WHERE participant = %(id)s
               AND extract(year from timestamp) = %(year)s
          ORDER BY id ASC
        """, params, back_as=back_as)
        out['given'] = lambda: db.all("""
            SELECT timestamp, (%(base_url)s || t.tippee::text) AS donee_url,
                   p.username AS donee_username, t.amount, t.context
              FROM transfers t
              JOIN participants p ON p.id = t.tippee
             WHERE t.tipper = %(id)s
               AND extract(year from t.timestamp) = %(year)s
               AND t.status = 'succeeded'
          ORDER BY t.id ASC
        """, params, back_as=back_as)
        out['taken'] = lambda: db.all("""
            SELECT timestamp, (%(base_url)s || t.team::text) AS team_url,
                   p.username AS team_username, t.amount
              FROM transfers t
              JOIN participants p ON p.id = t.team
             WHERE t.tippee = %(id)s
               AND t.context = 'take'
               AND extract(year from t.timestamp) = %(year)s
               AND t.status = 'succeeded'
          ORDER BY t.id ASC
        """, params, back_as=back_as)
        out['received'] = lambda: db.all("""
            SELECT timestamp, amount, context
              FROM transfers
             WHERE tippee = %(id)s
               AND context <> 'take'
               AND extract(year from timestamp) = %(year)s
               AND status = 'succeeded'
          ORDER BY id ASC
        """, params, back_as=back_as)

    if key:
        try:
            return out[key]()
        except KeyError:
            raise Response(400, "bad key `%s`" % key)
    elif require_key:
        raise Response(400, "missing `key` parameter")
    else:
        return {k: v() for k, v in out.items()}
