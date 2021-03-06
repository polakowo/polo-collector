# Grabber of trade history from Poloniex exchange
# https://github.com/polakowo/plnx-grabber
#
#   Copyright (C) 2017  https://github.com/polakowo/plnx-grabber

#   This program is free software: you can redistribute it and/or modify
#   it under the terms of the GNU General Public License as published by
#   the Free Software Foundation, either version 3 of the License, or
#   (at your option) any later version.
#
#   This program is distributed in the hope that it will be useful,
#   but WITHOUT ANY WARRANTY; without even the implied warranty of
#   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#   GNU General Public License for more details.
#
#   You should have received a copy of the GNU General Public License
#   along with this program.  If not, see <http://www.gnu.org/licenses/>.

import logging
import math
import re
from datetime import datetime, timedelta
from enum import Enum
from time import sleep
from timeit import default_timer as timer

import pandas as pd
import pymongo
import pytz
from bson.codec_options import CodecOptions
from poloniex import Poloniex

# Logger
############################################################

logger = logging.getLogger(__name__)
# No logging by default
logger.addHandler(logging.NullHandler())


# Date & time
############################################################

def parse_date(date_str, fmt='%Y-%m-%d %H:%M:%S'):
    # Parse dates coming from Poloniex
    return pytz.utc.localize(datetime.strptime(date_str, fmt))


def dt_to_str(date, fmt='%a %d/%m/%Y %H:%M:%S %Z'):
    # Format date for showing in console and logs
    return date.strftime(fmt)


def now():
    return pytz.utc.localize(datetime.utcnow())


def ago(**kwargs):
    return now() - timedelta(**kwargs)


def begin():
    return datetime(2000, 1, 1, tzinfo=pytz.utc)


class TimePeriod(Enum):
    SECOND = 1
    MINUTE = 60 * SECOND
    HOUR = 60 * MINUTE
    DAY = 24 * HOUR
    WEEK = 7 * DAY
    MONTH = 30 * DAY
    YEAR = 12 * MONTH


# Dataframes
############################################################

def df_memory(df):
    return df.memory_usage(index=True, deep=True).sum()


def df_series_info(df):
    # Returns the most valuable information on history stored in df
    # Get the order by comparing the first and last records
    from_i = -1 * (df.index[0] > df.index[-1])
    to_i = -1 * (df.index[0] < df.index[-1])
    return {
        'from_dt': df.iloc[from_i]['dt'],
        'from_id': df.index[from_i],
        'to_dt': df.iloc[to_i]['dt'],
        'to_id': df.index[to_i],
        'delta': df.iloc[to_i]['dt'] - df.iloc[from_i]['dt'],
        'count': len(df.index),
        'memory': df_memory(df)}


def verify_series_df(df):
    # Verifies the incremental nature of trade id across history
    t = timer()
    series_info = df_series_info(df)
    diff = series_info['count'] - (series_info['to_id'] - series_info['from_id'] + 1)
    if diff > 0:
        logger.warning("Dataframe - Found duplicates (%d) - %.2fs", diff, timer() - t)
    elif diff < 0:
        logger.warning("Dataframe - Found gaps (%d) - %.2fs", abs(diff), timer() - t)
    else:
        logger.debug("Dataframe - Verified - %.2fs", timer() - t)
    return diff == 0


def df_to_docs(df):
    # Convert df into shape suitable for export into MongoDB
    return df.reset_index().to_dict(orient='records')


def docs_to_df(docs, new_index=['dt']):
    # Convert docs to df
    return pd.DataFrame(list(docs)).set_index(new_index, drop=True)


# Output
############################################################

def dt_to_ts(date):
    return int(date.timestamp())


def format_td(td):
    seconds = int(abs(td).total_seconds())
    periods = [('year', 60 * 60 * 24 * 365),
               ('month', 60 * 60 * 24 * 30),
               ('day', 60 * 60 * 24),
               ('hour', 60 * 60),
               ('minute', 60),
               ('second', 1)]

    strings = []
    for period_name, period_seconds in periods:
        if seconds >= period_seconds:
            period_value, seconds = divmod(seconds, period_seconds)
            if period_value == 1:
                strings.append('%s %s' % (period_value, period_name))
            else:
                strings.append('%s %ss' % (period_value, period_name))

    return ' '.join(strings)


def format_bytes(num):
    for x in ['B', 'KB', 'MB', 'GB', 'TB']:
        if num < 1024.0:
            return '%3.1f %s' % (num, x)
        num /= 1024.0


def series_info_str(series_info):
    return "{ %s : %d, %s : %d, %s, %d rows, %s }" % (
        dt_to_str(series_info['from_dt']),
        series_info['from_id'],
        dt_to_str(series_info['to_dt']),
        series_info['to_id'],
        format_td(series_info['delta']),
        series_info['count'],
        format_bytes(series_info['memory']))


# MongoTS
############################################################

class MongoTS(object):
    """
    Wrapper around pymongo for dealing with trade series information
    """

    def __init__(self, db):
        # Set running MongoDB instance
        self.db = db

    def db_info(self):
        # Aggregates basic info on current state of db
        cname_series_info = {cname: self.series_info(cname) for cname in self.list_cols()}
        logger.info("Database '{0}' - {1} collections - {2:,} documents - {3}"
                    .format(self.db.name,
                            len(cname_series_info),
                            sum(series_info['count'] for series_info in cname_series_info.values()),
                            format_bytes(sum(series_info['memory'] for series_info in cname_series_info.values()))))
        # Shows detailed descriptions of each collection
        for cname, series_info in cname_series_info.items():
            logger.info("%s - %s", cname, series_info_str(series_info))

    def clear_db(self):
        # Drop all collections
        for cname in self.list_cols():
            self.drop_col(cname)

    # Collections

    def tzaware_col(self, cname):
        """
        Return timezone-aware dates by default
        """
        options = CodecOptions(tz_aware=True, tzinfo=pytz.utc)
        return self.db.get_collection(cname, codec_options=options)

    def list_cols(self):
        return self.db.collection_names()

    def create_col(self, cname):
        # Create new collection and index on timestamp field
        self.db.create_collection(cname)
        self.db[cname].create_index([('dt', pymongo.ASCENDING)], unique=False, background=True)
        logger.debug("%s - Collection - Created", cname)

    def drop_col(self, cname):
        # Delete collection entirely
        self.db[cname].drop()
        logger.debug("%s - Collection - Dropped", cname)

    def col_exists(self, cname):
        return cname in self.list_cols()

    def col_non_empty(self, cname):
        # Check whether collection exists and not empty
        return self.col_exists(cname) and self.docs_count(cname) > 0

    def col_memory(self, cname):
        # Returns size of all documents + header + index size
        return self.db.command('collstats', cname)['size'] + 16 * 100 + self.db.command('collstats', cname)[
            'totalIndexSize']

    # Series

    def series_info(self, cname):
        # Returns the most important series information
        # (start and end points, their delta, num of rows and memory taken)
        from_dict = self.from_doc(cname)
        to_dict = self.to_doc(cname)
        return {
            'from_dt': from_dict['dt'],
            'from_id': from_dict['_id'],
            'to_dt': to_dict['dt'],
            'to_id': to_dict['_id'],
            'delta': to_dict['dt'] - from_dict['dt'],
            'count': self.docs_count(cname),
            'memory': self.col_memory(cname)}

    def verify_series(self, cname):
        # Verifies the incremental nature of trade id across series
        t = timer()
        series_info = self.series_info(cname)
        diff = series_info['count'] - (series_info['to_id'] - series_info['from_id'] + 1)
        if diff > 0:
            logger.warning("%s - Collection - Found duplicates (%d) - %.2fs", cname, diff, timer() - t)
        elif diff < 0:
            logger.warning("%s - Collection - Found gaps (%d) - %.2fs", cname, abs(diff), timer() - t)
        else:
            logger.debug("%s - Collection - Verified - %.2fs", cname, timer() - t)
        return diff == 0

    def series_range(self, cname, from_dt, to_dt):
        # Get the series in the range
        return self.find_docs(cname, query={'dt': {'$gte': from_dt, '$lte': to_dt}})

    # Documents

    def docs_count(self, cname):
        # Documents count in collection
        return self.db.command('collstats', cname)['count']

    def from_doc(self, cname):
        # Return the document for the earliest point in series
        return next(self.tzaware_col(cname).find().sort([['_id', 1]]).limit(1))

    def to_doc(self, cname):
        # Return the document for the most recent point in series
        return next(self.tzaware_col(cname).find().sort([['_id', -1]]).limit(1))

    def insert_docs(self, cname, docs):
        # Convert df into list of dicts and insert into collection (fast)
        t = timer()
        result = self.db[cname].insert_many(docs)
        logger.debug("%s - Collection - Inserted %d documents - %.2fs",
                     cname, len(result.inserted_ids), timer() - t)

    def update_docs(self, cname, docs):
        # Convert df into list of dicts and only insert records not present in the collection (slow)
        t = timer()
        n_modified = 0
        n_upserted = 0
        for record in docs:
            result = self.db[cname].update_one(
                {'_id': record['_id']},
                {'$setOnInsert': record},
                upsert=True)
            if result.modified_count is not None and result.modified_count > 0:
                n_modified += result.modified_count
            if result.upserted_id is not None:
                n_upserted += 1
        logger.debug("%s - Collection - Modified %d, upserted %d documents - %.2fs",
                     cname, n_modified, n_upserted, timer() - t)

    def delete_docs(self, cname, query={}):
        # Delete documents
        t = timer()
        result = self.db[cname].delete_many(query)
        logger.debug("%s - Collection - Deleted %d documents - %.2fs",
                     cname, result.deleted_count, timer() - t)

    def find_docs(self, cname, *args):
        # Return generator for documents which match query
        return self.tzaware_col(cname).find(args)


# Grabber
############################################################

class Grabber(object):
    """
    Poloniex only returns max of 50,000 records at a time, meaning we have to coordinate download and
    save of many chunks of data. Moreover, there is no fixed amount of records per unit of time, which
    requires a synchronization of chunks by trade id.

    For example: If we would like to go one month back in time, Poloniex could have returned us only
    the most recent week. Because Polo returns only 50,000 of the latest records (not the oldest ones),
    we can synchronize chunks only by going backwards. Otherwise, if we decided to go forwards in time,
    we couldn't know which time interval to choose to fill all records in order to synchronize with
    previous chunk.
    """

    def __init__(self, mongo_ts):
        # pymongo Wrapper
        self.mongo_ts = mongo_ts
        # Poloniex
        self.polo = Poloniex()

    def progress(self):
        """
        Shows how much history was grabbed so far in relation to overall available on Poloniex
        """
        cname_series_info = {cname: self.mongo_ts.series_info(cname) for cname in self.mongo_ts.list_cols()}
        for pair, series_info in cname_series_info.items():
            # Get latest id
            df = self.get_chunk(pair, ago(minutes=15), now())
            if df.empty:
                logger.info("%s - No information available", pair)
                continue
            max_id = df_series_info(df)['to_id']

            # Progress bar
            steps = 50
            below_rate = series_info['from_id'] / max_id
            taken_rate = (series_info['to_id'] - series_info['from_id']) / max_id
            above_rate = (max_id - series_info['to_id']) / max_id
            progress = '_' * math.floor(below_rate * steps) + \
                       'x' * (steps - math.floor(below_rate * steps) - math.floor(above_rate * steps)) + \
                       '_' * math.floor(above_rate * steps)

            logger.info("%s - 1 [ %s ] %d - %.1f/100.0%% - %s/%s",
                        pair,
                        progress,
                        series_info['to_id'],
                        taken_rate * 100,
                        format_bytes(series_info['memory']),
                        format_bytes(1 / taken_rate * series_info['memory']))

    def remote_info(self, pairs):
        """
        Detailed info on pairs listed on Poloniex
        """
        for pair in pairs:
            chart_data = Poloniex().returnChartData(pair, period=86400, start=1, end=dt_to_ts(now()))
            from_dt = chart_data[0]['date']
            to_dt = chart_data[-1]['date']

            df = self.get_chunk(pair, ago(minutes=5), now())
            if df.empty:
                logger.info("%s - No information available")
                continue
            max_id = df_series_info(df)['to_id']

            logger.info("%s - %s - %s, %s, %d trades, est. %s",
                        pair,
                        dt_to_str(from_dt, fmt='%a %d/%m/%Y'),
                        dt_to_str(to_dt, fmt='%a %d/%m/%Y'),
                        format_td(to_dt - from_dt),
                        max_id,
                        format_bytes(round(df_memory(df) * max_id / len(df.index))))

    def db_info(self):
        """
        Wrapper for mongo_ts.db_info
        """
        self.mongo_ts.db_info()

    def ticker_pairs(self):
        """
        Returns all pairs from ticker
        """
        ticker = self.polo.returnTicker()
        pairs = set(map(lambda x: str(x).upper(), ticker.keys()))
        return pairs

    def get_chunk(self, pair, from_dt, to_dt):
        """
        Returns a chunk of trade history (max 50,000 of the most recent records) of a period of time

        :param pair: pair of symbols
        :param start: date of start
        :param end: date of end
        :return: df
        """
        try:
            series = self.polo.marketTradeHist(pair, start=dt_to_ts(from_dt), end=dt_to_ts(to_dt))
            series_df = pd.DataFrame(series)
            series_df = series_df.astype({
                'date': str,
                'amount': float,
                'globalTradeID': int,
                'rate': float,
                'total': float,
                'tradeID': int,
                'type': str})
            series_df['date'] = series_df['date'].apply(lambda date_str: parse_date(date_str))
            series_df.rename(columns={'date': 'dt', 'tradeID': '_id', 'globalTradeID': 'globalid'}, inplace=True)
            series_df = series_df.set_index(['_id'], drop=True)
            return series_df
        except Exception as e:
            logger.error(e)
            return pd.DataFrame()

    def grab(self, pair, from_dt=None, from_id=None, to_dt=None, to_id=None):
        """
        Grabs trade history of a period of time for a pair of symbols.

        * Traverses history from the end date to the start date (backwards)
        * History is divided into chunks of max 50,000 records
        * Chunks are synced by id of their oldest records
        * Once received, each chunk is immediately put into MongoDB to free up RAM
        * Result includes passed dates - [from_dt, to_dt]
        * Result excludes passed ids - (from_id, to_id)
        * Ids have higher priority than dates

        The whole process looks like this:
        1) Start recording history chunk by chunk beginning from to_dt

                [ from_dt/from_id <- xxxxxxxxxxxxxxxxxxxxxxxxxx to_dt ]

            or if to_id is provided, find it first and only then start recording

                [ from_dt/from_id ___________ to_id <- <- <- <- to_dt ]

                [ from_dt/from_id <- xxxxxxxx to_id ___________ to_dt ]


        2) Each chunk is verified for consistency and inserted into MongoDB
        3) Proceed until start date or id are reached, or Poloniex returned nothing

                [ from_dt/from_id xxxxxxxxxxxxxxxxxxxxxxxxxxxxx to_dt ]
                                                |
                                                v
                                        collected history

            or if to_id is provided

                [ from_dt/from_id xxxxxxxxxxx to_id ___________ to_dt ]
                                         |
                                         v
                                 collected history

        4) Verify whole collection

        :param pair: pair of symbols
        :param from_dt: date of start point (only as approximation, program aborts if found)
        :param from_id: id of start point (has higher priority than ts, program aborts if found)
        :param to_dt: date of end point
        :param to_id: id of end point
        :return: None
        """
        if self.mongo_ts.col_non_empty(pair):
            logger.debug("%s - Collection - %s", pair, series_info_str(self.mongo_ts.series_info(pair)))
        else:
            logger.debug("%s - Collection - Empty", pair)

            # Create new collection only if none exists
            if pair not in self.mongo_ts.list_cols():
                self.mongo_ts.create_col(pair)
        logger.debug("%s - Collection - Achieving { %s%s, %s%s, %s }",
                     pair,
                     dt_to_str(from_dt),
                     ' : %d' % from_id if from_id is not None else '',
                     dt_to_str(to_dt),
                     ' : %d' % to_id if to_id is not None else '',
                     format_td(to_dt - from_dt))

        t = timer()

        # Init window params
        # ..................

        # Dates are required to build rolling windows and pass them to Poloniex
        # If start and/or end dates are empty, set the widest period possible
        if from_dt is None:
            from_dt = begin()
        if to_dt is None:
            to_dt = now()
        if to_dt <= from_dt:
            raise Exception("%s - Start date { %s } above end date { %s }" %
                            (pair, dt_to_str(from_dt), dt_to_str(to_dt)))
        if from_id is not None and to_id is not None:
            if to_id <= from_id:
                raise Exception("%s - Start id { %d } above end id { %d }" %
                                (pair, from_id, to_id))

        max_delta = timedelta(days=30)
        window = {
            # Do not fetch more than needed, pick the size smaller or equal to max_delta
            'from_dt': max(to_dt - max_delta, from_dt),
            'to_dt': to_dt,
            # Gets filled after first chunk is fetched
            'anchor_id': None
        }
        # Record only starting from to_id, or immediately if none is provided
        recording = to_id is None
        # After we recorded data, verify consistency in database
        anything_recorded = False

        # Three possibilities to escape the loop:
        #   1) empty result
        #   2) reached the start date/id
        #   3) exception
        while True:
            t2 = timer()

            # Receive and process chunk of data
            # .................................

            logger.debug("%s - Poloniex - Querying { %s, %s, %s }",
                         pair,
                         dt_to_str(window['from_dt']),
                         dt_to_str(window['to_dt']),
                         format_td(window['to_dt'] - window['from_dt']))

            df = self.get_chunk(pair, window['from_dt'], window['to_dt'])
            if df.empty:
                if anything_recorded or window['from_dt'] == from_dt:
                    # If we finished (either by reaching start or receiving no records) -> terminate
                    logger.debug("%s - Poloniex - Nothing returned - aborting", pair)
                    break
                else:
                    # If Poloniex temporary suspended trading for a pair -> look for older records
                    logger.debug("%s - Poloniex - Nothing returned - continuing", pair)
                    window['to_dt'] = window['from_dt']
                    window['from_dt'] = max(window['from_dt'] - max_delta, from_dt)
                    continue

            # If chunk contains end id (newest bound) -> start recording
            # .........................................................

            if not recording:
                # End id found
                if to_id in df.index:
                    logger.debug("%s - Poloniex - End id { %d } found", pair, to_id)
                    # Start recording
                    recording = True

                    df = df[df.index < to_id]
                    if df.empty:
                        logger.debug("%s - Poloniex - Nothing returned - aborting", pair)
                        break
                else:
                    series_info = df_series_info(df)
                    logger.debug("%s - Poloniex - End id { %d } not found in { %s : %d, %s : %d }",
                                 pair,
                                 to_id,
                                 dt_to_str(series_info['from_dt']),
                                 series_info['from_id'],
                                 dt_to_str(series_info['to_dt']),
                                 series_info['to_id'])

                    # If start reached -> terminate
                    if from_id is not None:
                        if any(df.index <= from_id):
                            logger.debug("%s - Poloniex - Start id { %d } reached - aborting", pair, from_id)
                            break
                    if any(df['dt'] <= from_dt):
                        logger.debug("%s - Poloniex - Start date { %s } reached - aborting", pair, dt_to_str(from_dt))
                        break

                    series_info = df_series_info(df)
                    window['from_dt'] = max(series_info['from_dt'] - max_delta, from_dt)
                    window['to_dt'] = series_info['from_dt']
                    continue

            if recording:

                # Synchronize with previous chunk by intersection of their ids
                # ............................................................

                if window['anchor_id'] is not None:
                    # To merge two dataframes, there must be an intersection of ids (anchor)
                    if any(df.index >= window['anchor_id']):
                        df = df[df.index < window['anchor_id']]
                        if df.empty:
                            logger.debug("%s - Poloniex - Nothing returned - aborting", pair)
                            break
                    else:
                        logger.debug("%s - Poloniex - Anchor id { %d } is missing - aborting", pair,
                                     window['anchor_id'])
                        break

                # If chunk contains start id or date (oldest record) -> finish recording
                # ....................................................................
                if from_id is not None:
                    if any(df.index <= from_id):
                        df = df[df.index > from_id]
                        if df.empty:
                            logger.debug("%s - Poloniex - Nothing returned - aborting", pair)
                        else:
                            logger.debug("%s - Poloniex - Returned %s - %.2fs",
                                         pair, series_info_str(df_series_info(df)), timer() - t2)
                            logger.debug("%s - Poloniex - Start id { %d } reached - aborting", pair, from_id)
                            if verify_series_df(df):
                                self.mongo_ts.insert_docs(pair, df_to_docs(df))
                                anything_recorded = True
                        break  # escape anyway
                # or at least the approx. date
                elif any(df['dt'] <= from_dt):
                    df = df[df['dt'] >= from_dt]
                    if df.empty:
                        logger.debug("%s - Poloniex - Nothing returned - aborting", pair)
                    else:
                        logger.debug("%s - Poloniex - Returned %s - %.2fs",
                                     pair, series_info_str(df_series_info(df)), timer() - t2)
                        logger.debug("%s - Poloniex - Start date { %s } reached - aborting", pair, dt_to_str(from_dt))
                        if verify_series_df(df):
                            self.mongo_ts.insert_docs(pair, df_to_docs(df))
                            anything_recorded = True
                    break

                # Record data
                # ...........

                # Drop rows with NaNs
                df.dropna(inplace=True)
                if df.empty:
                    logger.debug("%s - Poloniex - Nothing returned - aborting", pair)
                    break
                # Drop duplicates
                df.drop_duplicates(inplace=True)
                if df.empty:
                    logger.debug("%s - Poloniex - Nothing returned - aborting", pair)
                    break

                # If none of the start points reached, continue with execution using new window
                logger.debug("%s - Poloniex - Returned %s - %.2fs",
                             pair, series_info_str(df_series_info(df)), timer() - t2)
                # Break on last stored df if the newest chunk is broken
                if not verify_series_df(df):
                    break
                self.mongo_ts.insert_docs(pair, df_to_docs(df))
                anything_recorded = True

                # Continue with next chunk
                # ........................

                series_info = df_series_info(df)
                window['from_dt'] = max(series_info['from_dt'] - max_delta, from_dt)
                window['to_dt'] = series_info['from_dt']
                window['anchor_id'] = series_info['from_id']

        # Verify collection after recordings
        # ..................................

        if anything_recorded:
            # Generally, series verification always succeeds, because we check each df and sync them properly
            if self.mongo_ts.verify_series(pair):
                logger.debug("%s - Collection - %s - %.2fs", pair, series_info_str(self.mongo_ts.series_info(pair)),
                             timer() - t)
            else:
                raise Exception("%s - Consistency broken - fix required" % pair)
        else:
            logger.debug("%s - Nothing returned - %.2fs", pair, timer() - t)

    def one(self, pair, from_dt=None, to_dt=None, drop=False):

        """
        Grabs data for a pair based on passed params as well as history stored in the underlying collection

        Possible values of from_dt and to_dt:
        * 'oldest' means the from_dt of the collection
        * 'newest' means the to_dt of the collection

        :param pair: pair of symbols
        :param from_dt: date of the start point or command from ['oldest', 'newest']
        :param to_dt: date of the end point or command from ['oldest', 'newest']
        :param drop: delete underlying collection before insert
        :return: None
        """
        t = timer()
        logger.info("%s - ...", pair)

        # Fill dates of collection's bounds
        if self.mongo_ts.col_non_empty(pair):
            series_info = self.mongo_ts.series_info(pair)

            if isinstance(from_dt, str):
                if from_dt == 'oldest':
                    from_dt = series_info['from_dt']
                elif from_dt == 'newest':
                    from_dt = series_info['to_dt']
                else:
                    raise Exception("Unknown command '%s'" % from_dt)
            if isinstance(to_dt, str):
                if to_dt == 'oldest':
                    to_dt = series_info['from_dt']
                elif to_dt == 'newest':
                    to_dt = series_info['to_dt']
                else:
                    raise Exception("Unknown command '%s'" % to_dt)

            # Overwrite means drop completely
            if drop:
                self.mongo_ts.drop_col(pair)

        # If nothing is passed, fetch the widest tail and/or head possible
        if from_dt is None:
            from_dt = begin()
        if to_dt is None:
            to_dt = now()

        if self.mongo_ts.col_non_empty(pair):
            series_info = self.mongo_ts.series_info(pair)

            # Period must be non-zero
            if from_dt >= to_dt:
                raise Exception("%s - Start date { %s } above end date { %s }" %
                                (pair, dt_to_str(from_dt), dt_to_str(to_dt)))

            if from_dt < series_info['from_dt']:
                logger.debug("%s - Grabbing tail", pair)
                # Collect history up to the oldest record
                self.grab(pair,
                          from_dt=from_dt,
                          to_dt=series_info['from_dt'],
                          to_id=series_info['from_id'])

            if to_dt > series_info['to_dt']:
                logger.debug("%s - Grabbing head", pair)
                # Collect history from the newest record
                self.grab(pair,
                          from_dt=series_info['to_dt'],
                          to_dt=to_dt,
                          from_id=series_info['to_id'])
        else:
            # There is no newest or oldest bounds of empty collection
            if isinstance(from_dt, str) or isinstance(to_dt, str):
                raise Exception("%s - Collection empty - cannot auto-fill dates" % pair)

            logger.debug("%s - Grabbing full", pair)
            self.grab(pair,
                      from_dt=from_dt,
                      to_dt=to_dt)

        logger.info("%s - Finished - %.2fs", pair, timer() - t)

    def row(self, pairs, from_dt=None, to_dt=None, drop=False):
        """
        Grabs data for each pair in a row

        :param pairs: list of pairs or string command from ['db', 'ticker']
        :param from_dt: date of the start point or command from ['oldest', 'newest']
        :param to_dt: date of the end point or command from ['oldest', 'newest']
        :param drop: delete underlying collection before insert
        :return: None
        """
        if isinstance(pairs, str):
            # All pairs in db
            if pairs == 'db':
                pairs = self.mongo_ts.list_cols()
            # All pairs in ticker
            elif pairs == 'ticker':
                pairs = self.ticker_pairs()
            else:
                regex = re.compile(pairs)
                pairs = list(filter(regex.search, self.ticker_pairs()))
        if len(pairs) == 0:
            raise Exception("List of pairs must be non-empty")
        for pair in pairs:
            t = timer()
            self.one(pair, from_dt=from_dt, to_dt=to_dt, drop=drop)

    def ring(self, pairs, every=None):
        """
        Grabs the most recent data for a row of pairs on repeat

        Requires all pairs to be persistent in the database

        :param pairs: list of pairs or 'db' command
        :param every: pause between iterations
        :return: None
        """
        if isinstance(pairs, str):
            # All pairs in db
            if pairs == 'db':
                pairs = self.mongo_ts.list_cols()
            else:
                regex = re.compile(pairs)
                pairs = list(filter(regex.search, self.ticker_pairs()))
        if len(pairs) == 0:
            raise Exception("List of pairs must be non-empty")
        while True:
            # Collect head every time interval
            self.row(pairs, to_dt=now())
            if every is not None:
                sleep(every)
