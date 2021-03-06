import pandas as pd
import numpy as np
import collections
import itertools


def fetch_archival_gtfs_realtime_data(kind='gtfs', timestamp='2014-09-17-09-31', raw=False):
    """
    Returns archived GTFS data for a particular time_assigned.

    Parameters
    ----------
    kind: {'gtfs', 'gtfs-l', 'gtfs-si'}
        Archival data is provided in these three rollups. The first one covers 1-6 and the S, the second covers the
        L, and the third, the Staten Island Railway.
    timestamp: str
        The time_assigned associated with the data rollup. The files are time stamped at 01, 06, 11, 16, 21, 26, 31, 36,
        41, 46, 51, and 56 minutes after the hour, so only these times will be valid.
    raw: bool
        Whether or not to return the raw requests object instead of the parsed GRFS-R record. Used in testing.
    """
    import requests
    from google.transit import gtfs_realtime_pb2

    response = requests.get("https://datamine-history.s3.amazonaws.com/{0}-{1}".format(kind, timestamp))

    if raw:
        return response.content
    else:
        feed = gtfs_realtime_pb2.FeedMessage()
        feed.ParseFromString(response.content)
        return feed


def _parse_gtfs_into_action_log(feed, information_time):
    """
    Parses a GTFS-Realtime feed into a single pandas.DataFrame

    Parameters
    ----------
    feed, gtfs_realtime_pb2.FeedMessage object
        The feed being processed.
    """
    return _parse_message_list_into_action_log(feed.entity, information_time)


def _parse_message_list_into_action_log(messages, information_time):
    """
    Parses a list of messages into a single pandas.DataFrame
    """
    try:
        if messages[0].vehicle.trip.trip_id == "147200_1..N02X017":
            import pdb; pdb.set_trace()
    except (KeyboardInterrupt, SystemExit):
        raise
    except:
        pass

    actions_list = []
    # action_log = pd.DataFrame(columns=['trip_id', 'route_id', 'action', 'stop_id', 'time_assigned'])

    # In the MTA case, alerts are provided at the end of the feed. Isolate those from the rest of the entries by
    # finding the breakpoint at which they appear. This is a harder process than one would expect due to the way that
    # the library is designed, hence the weirdness here.
    alert_breakpoint = None

    for i, message in enumerate(reversed(messages)):
        if _is_alert(message):
            alert_breakpoint = len(messages) - i
            break

    alerts = messages[alert_breakpoint:] if alert_breakpoint else []

    # The rest of the entries are Trip Alert and Train Station entities.
    trips_breakpoint = alert_breakpoint if alert_breakpoint else len(messages)

    for i in range(0, trips_breakpoint):
        message = messages[i]

        if _is_vehicle_update(message):
            # This is a vehicle update message.
            # Since vehicle update messages always appear after trip update messages (is this true?),
            # we won't process them separately.
            pass
        else:
            # This is a trip update message.

            # To understand what this message means, we need to read information from the vehicle update also.
            # First, we need to verify that there is a vehicle update present at all.
            if i == len(messages) - 1:
                has_associated_vehicle_update = False
            elif (alerts and i != alert_breakpoint - 1) or not alerts:
                has_associated_vehicle_update = _is_vehicle_update(messages[i + 1])
            else:
                has_associated_vehicle_update = False
            trip_in_progress = has_associated_vehicle_update

            # Pass reading the actions into a helper function.
            if trip_in_progress:
                trip_update = messages[i + 1]
                actions = parse_message_into_action_log(message, trip_update, information_time)
            else:
                actions = parse_message_into_action_log(message, None, information_time)

            actions_list.append(actions)

    return pd.concat(actions_list)


def parse_message_into_action_log(message, vehicle_update, information_time):
    """
    Parses the trip update and vehicle update messages (if there is one; may be None) for a particular trip into an
    action log.

    This method is called by parse_message_list_into_action_log in a loop in order to get the complete action log.
    """
    # TODO: Simplify the overly complicated logic here.

    # To help catch errors, validate input.
    if vehicle_update is not None and not _is_vehicle_update(vehicle_update):
        raise ValueError("The vehicle update message provided is invalid.")
    if not _is_trip_update(message):
        raise ValueError("The trip update message provided is invalid.")

    # If we are passed a vehicle update, then the trip must already be in progress.
    trip_in_progress = bool(vehicle_update)

    # The base of the log entry is the same for all possible entries.
    # The entries are, in order of key: trip_id, route_id, and information_time.
    # Each line will additionally contain an action, stop_id, and time_assigned.
    base = np.array([message.trip_update.trip.trip_id, message.trip_update.trip.route_id, information_time])

    # Hash map for current status enums to current status strings.
    vehicle_status_dict = {
        0: 'INCOMING_AT',
        1: 'STOPPED_AT',
        2: 'IN_TRANSIT_TO'
    }

    if trip_in_progress:
        vehicle_status = vehicle_status_dict[vehicle_update.vehicle.current_status]
        vehicle_status_poi = vehicle_update.vehicle.stop_id
    else:
        vehicle_status = None
    n_stops = len(message.trip_update.stop_time_update)

    lines = []

    for s_i, stop_time_update in enumerate(message.trip_update.stop_time_update):

        # If we do have one, we may continue.
        # Weirdness with detecting if we have arrival/departure times.
        has_arrival_time = str(stop_time_update.arrival) != ''
        has_departure_time = str(stop_time_update.departure) != ''
        stop_time_update_poi = stop_time_update.stop_id
        if trip_in_progress:
            stop_is_next_stop = stop_time_update_poi == vehicle_status_poi

        # If the trip is not in progress, and we are at the first index, then we will have only a planned
        # departure to account for.
        if not trip_in_progress and s_i == 0:
            assert not has_arrival_time
            assert has_departure_time

            struct = np.append(base.copy(), np.array(
                ['EXPECTED_TO_DEPART_AT', stop_time_update.stop_id, stop_time_update.departure.time]
            ))
            lines.append(struct)

        # If the trip is not in progress, and we are not at the first index nor the last index, then we will
        # have both types to account for.
        elif not trip_in_progress and s_i != 0 and n_stops != s_i + 1:
            assert has_arrival_time
            assert has_departure_time

            # Arrival.
            struct = np.append(base.copy(), np.array(
                ['EXPECTED_TO_ARRIVE_AT', stop_time_update.stop_id, stop_time_update.arrival.time]
            ))
            lines.append(struct)

            # Departure.
            struct = np.append(base.copy(), np.array(
                ['EXPECTED_TO_DEPART_AT', stop_time_update.stop_id, stop_time_update.departure.time]
            ))
            lines.append(struct)

        # If we are at the last index and we do not have a vehicle update present, then we will have only an arrival to
        # account for.
        elif n_stops == s_i + 1:
            assert has_arrival_time
            try:
                assert not has_departure_time
            except AssertionError:
                # This isn't supposed to happen, because it means that the train is question is being made out as
                # though it is departing to some next station on the line when there are no other stations on the
                # line to depart to. However, this appears to occur in some cases. For example, an incidence of this
                # occurs in the 2014-09-17-09-36 GTFS-Realtime archive, where a 4 train departs from a Utica Avenue
                # end-stop.
                pass

            if len(message.trip_update.stop_time_update) != 1:  # this is the final stop, but train is elsewhere
                struct = np.append(base.copy(), np.array(
                ['EXPECTED_TO_ARRIVE_AT', stop_time_update.stop_id, stop_time_update.arrival.time]
                ))
                lines.append(struct)
            elif vehicle_status != 'STOPPED_AT':  # this is the final stop, train is en route to it
                struct = np.append(base.copy(), np.array(
                ['EXPECTED_TO_ARRIVE_AT', stop_time_update.stop_id, stop_time_update.arrival.time]
                ))
                lines.append(struct)
            else:  # this is the final stop, train is STOPPED_AT it
                struct = np.append(base.copy(), np.array(
                ['STOPPED_AT', stop_time_update.stop_id, stop_time_update.arrival.time]
                ))
                lines.append(struct)

        # If the trip is in progress the vehicle update and stop update in question are not talking about the
        # same station, and the message is not the last one in the sequence, and either only an arrival or only a
        # departure is present in the struct, then we have a forward estimate on when this train will arrive at some
        # other station further down the line (but not at the very end), but at which it *will not stop*. In other
        # words, this indicates that this train is going to skip this stop in its service!
        elif trip_in_progress and not n_stops == s_i + 1 and not has_departure_time:
            assert has_arrival_time

            struct = np.append(base.copy(), np.array(
                ['EXPECTED_TO_SKIP', stop_time_update.stop_id, stop_time_update.arrival.time]
            ))
            lines.append(struct)
        elif trip_in_progress and not n_stops == s_i + 1 and not has_arrival_time:
            assert has_departure_time

            struct = np.append(base.copy(), np.array(
                ['EXPECTED_TO_SKIP', stop_time_update.stop_id, stop_time_update.departure.time]
            ))
            lines.append(struct)

        # If we are at the last index, and we are not stopped, then we will have only an arrival to account for.
        elif n_stops == s_i + 1:
            assert has_arrival_time
            try:
                assert not has_departure_time
            except AssertionError:
                # This isn't supposed to happen, because it means that the train is question is being made out as
                # though it is departing to some next station on the line when there are no other stations on the
                # line to depart to. However, this appears to occur in some cases. For example, an incidence of this
                # occurs in the 2014-09-17-09-36 GTFS-Realtime archive, where a 4 train departs from a Utica Avenue
                # end-stop.
                pass

            if vehicle_status != 'STOPPED_AT':
                struct = np.append(base.copy(), np.array(
                ['EXPECTED_TO_ARRIVE_AT', stop_time_update.stop_id, stop_time_update.arrival.time]
                ))
            else:
                struct = np.append(base.copy(), np.array(
                ['STOPPED_AT', stop_time_update.stop_id, stop_time_update.arrival.time]
                ))
            lines.append(struct)

        # If the trip is in progress, we have an arrival time, and we have an INCOMING_AT or IN_TRANSIT_TO
        # vehicle update, and the vehicle update and stop update in question are talking about the same
        # station, then we know that we are en route to a station, but haven't arrived there yet.
        elif trip_in_progress and vehicle_status in ['INCOMING_AT', 'IN_TRANSIT_TO'] and stop_is_next_stop:
            assert has_arrival_time
            assert has_departure_time

            # Arrival.
            struct = np.append(base.copy(), np.array(
                ['EXPECTED_TO_ARRIVE_AT', stop_time_update.stop_id, stop_time_update.arrival.time]
            ))
            lines.append(struct)

            # Departure.
            struct = np.append(base.copy(), np.array(
                ['EXPECTED_TO_DEPART_AT', stop_time_update.stop_id, stop_time_update.departure.time]
            ))
            lines.append(struct)

        # If the trip is in progress, we are STOPPED_AT, we are at the first station in the line, and the vehicle
        # update and stop update in question are talking about the same station, then we are currently stopped at the
        #  first station in the line, and will only have a departure time.
        elif trip_in_progress and vehicle_status == 'STOPPED_AT' and s_i == 0 and not has_arrival_time:
            assert has_departure_time

            struct = np.append(base.copy(), np.array(
                ['STOPPED_AT', stop_time_update.stop_id, stop_time_update.arrival.time]
            ))
            lines.append(struct)

        # If the trip is in progress, we are STOPPED_AT, and the vehicle update and stop update in question are
        # talking about the same station, then that arrival time should be the time at which this train arrived at
        # this station.
        elif trip_in_progress and vehicle_status == 'STOPPED_AT' and stop_is_next_stop:
            assert has_arrival_time
            assert has_departure_time

            struct = np.append(base.copy(), np.array(
                ['STOPPED_AT', stop_time_update.stop_id, stop_time_update.arrival.time]
            ))
            lines.append(struct)

        # If the trip is in progress, the vehicle update and stop update in question are not talking about the
        # same station, and the message is not the last one in the sequence, and both an arrival and
        # departure are present in the struct, then we have a forward estimate on when this train will arrive
        # at some other station further down the line (but not at the very end).
        #
        # We actually do the same thing in this case as in the first case, but to keep the logic neat let's
        # just replicate the code.
        elif trip_in_progress and not stop_is_next_stop and not n_stops == s_i + 1 and has_departure_time:
            assert has_arrival_time

            # Arrival.
            struct = np.append(base.copy(), np.array(
                ['EXPECTED_TO_ARRIVE_AT', stop_time_update.stop_id, stop_time_update.arrival.time]
            ))
            lines.append(struct)

            # Departure.
            struct = np.append(base.copy(), np.array(
                ['EXPECTED_TO_DEPART_AT', stop_time_update.stop_id, stop_time_update.departure.time]
            ))
            lines.append(struct)

        else:
            raise ValueError

    action_log = pd.DataFrame(lines, columns=['trip_id', 'route_id', 'information_time', 'action', 'stop_id',
                                              'time_assigned'])
    return action_log


def parse_tripwise_action_logs_into_trip_log(tripwise_action_logs):
    """
    Given a list of action logs associated with a particular trip, returns the result of their merger: a single trip
    log.

    Note that this trip log is not terminated. If the action logs do not provide complete information about this
    trip's stops (for example, if the train stopped at its last stop and was subsequently removed from the record in
    the time between updates) then you will need to "finish" the trip information off yourself, using the
    `finish_trip` method. This is done for you in `parse_feeds_into_trip_logs`.
    """
    all_data = pd.concat(tripwise_action_logs)

    key_data = all_data.groupby('information_time').first().reset_index()
    current_information_time = None

    # The following bookkeeping is used to assign the *next* information time in the case of a STOPPED_AT.
    information_times = sorted(list(set(all_data['information_time'])))
    next_information_time_index = 1
    next_information_time = information_times[1] if len(information_times) > 1 else np.nan

    # To understand what went on during a trip, we only need to have a list of touched stops, the rows corresponding
    # with the first action in each observation's action sublog, and the time that has passed in between the sublog
    # entries.
    #
    # We can extract all of the stop information that we need by considering information pertaining to these entries,
    # in order.
    remaining_stops = _extract_synthetic_route_from_tripwise_action_logs(tripwise_action_logs)

    # Base is trip_id, route_id.
    base = np.array([all_data.iloc[0]['trip_id'], all_data.iloc[0]['route_id']])

    lines = []

    for ind, row in key_data.iterrows():

        previous_information_time = current_information_time if current_information_time is not None else np.nan
        current_information_time = row['information_time']

        # Do bookkeeping to keep track of the next information time for use by STOPPED_AT records.
        if current_information_time == next_information_time:
            next_information_time_index += 1
            try:
                next_information_time = information_times[next_information_time_index]
            except IndexError:  # end of the record
                next_information_time = np.nan

        current_stop = row['stop_id']

        i_del = 0
        for remaining_stop in remaining_stops:
            if remaining_stop != current_stop:
                # action, minimum_time, maximum_time, stop_id, latest_information_time
                skipped_stop = np.append(base.copy(), np.array(
                    ['STOPPED_OR_SKIPPED', previous_information_time, current_information_time,
                     remaining_stop, current_information_time]
                ))
                lines.append(skipped_stop)
                i_del += 1
            else:
                if row['action'] == 'STOPPED_AT':
                    stopped_stop = np.append(base.copy(), np.array(
                        ['STOPPED_AT', previous_information_time, next_information_time,
                         row['stop_id'], current_information_time]
                    ))
                    lines.append(stopped_stop)
                    i_del += 1
                    break
                else:
                    # We have learned nothing.
                    break
        remaining_stops = remaining_stops[i_del:]

    # Any stops left over we haven't arrived at yet.
    for remaining_stop in remaining_stops:
        future_stop = np.append(base.copy(), np.array(
            ['EN_ROUTE_TO', current_information_time, np.nan,
             remaining_stop, current_information_time]
        ))
        lines.append(future_stop)

    trip = pd.DataFrame(lines, columns=['trip_id', 'route_id', 'action', 'minimum_time', 'maximum_time', 'stop_id',
                                        'latest_information_time'])
    return trip


def mta_archival_time_to_unix_timestamp(mta_archival_time):
    """
    Utility function. Converts an instance of the time provided by the MTA for an archival record (which will be of
    the form 2014-09-18-09-31) into a UNIX timestamp.
    """
    import datetime

    datetime_parts = [int(datetime_part.lstrip('0')) for datetime_part in mta_archival_time.split("-")]
    return int(datetime.datetime(*datetime_parts).timestamp())


def _extract_synthetic_route_from_tripwise_action_logs(tripwise_action_logs):
    """
    Given a list of trip-wise action logs, returns the synthetic route of all of the stops that train may have
    stopped at, in the order in which those stops would have occurred.
    """
    station_lists = []
    for log in tripwise_action_logs:
        station_lists.append(list(log['stop_id'].unique()))
    return _extract_synthetic_route_from_station_lists(station_lists)


def _extract_synthetic_route_from_station_lists(station_lists):
    """
    Given a list of station lists (that is: a list of lists, where each sublist consists of the series of stations
    which a train was purported to be heading towards at any one time), returns the synthetic route of all of the
    stops that train may have stopped at, in the order in which those stops would have occurred.
    """
    ret = []
    for i in range(len(station_lists)):
        ret = _synthesize_station_lists(ret, station_lists[i])
    return ret


def _synthesize_station_lists(left, right):
    """
    Pairwise synthesis op. Submethod of the above.
    """
    # First, find the pivot.
    pivot_left = pivot_right = -1
    for j in range(len(left)):
        station_a = left[j]
        for k in range(len(right)):
            station_b = right[k]
            if station_a == station_b:
                pivot_left = j
                pivot_right = k
                break

    # If we found a pivot...
    if pivot_left != -1:
        # ...then the stations that appear before the pivot in the first list, the pivot, and the stations that
        # appear after the pivot in the second list should be the ones that are included
        return (left[:pivot_left] +
                [s for s in right[:pivot_right] if s not in left[:pivot_left]] +
                right[pivot_right:])
    # If we did not find a pivot...
    else:
        # ...then none of the stations that appear in the second list appeared in the first list. This means that the
        #  train probably cancelled those stations, but it may have stopped there in the meantime also. Add all
        # stations in the first list and all stations in the second list together.
        return left + right


def _is_vehicle_update(message):
    """Helper method that determines whether or not a message is a vehicle update."""
    return str(message.trip_update.trip.route_id) == ''


def _is_alert(message):
    """Helper method that determines whether or not a message is an alert."""
    return str(message.alert) != ''


def _is_trip_update(message):
    """Helper method that determines whether or not a message is a trip update."""
    return not _is_vehicle_update(message) and not _is_alert(message)


def _sort_feed_messages_by_trip_id(feed):
    """
    Takes a feed. Returns a hash table of non-alert messages in that feed corresponding with particular trips.

    Alerts are excluded because the way things are, it's better to leave incorporating them in downstream of when
    this method is used.
    """
    message_table = collections.defaultdict(list)
    for message in feed.entity:
        if _is_alert(message):
            continue
        elif _is_trip_update(message):
            trip_id = message.trip_update.trip.trip_id
        else:  # is_vehicle_update
            trip_id = message.vehicle.trip.trip_id
        message_table[trip_id].append(message)
    return message_table


def _finish_trip(trip_log, information_date):
    """
    Finishes a trip. We know a trip is finished when its messages stops appearing in feed files, at which time we can
    "cross out" any stations still remaining.
    """
    trip_log = (trip_log.replace('EN_ROUTE_TO', 'STOPPED_OR_SKIPPED')
                        .replace('EXPECTED_TO_SKIP', 'STOPPED_OR_SKIPPED')
                        .replace('nan', np.nan))
    trip_log['maximum_time'] = trip_log['maximum_time'].fillna(information_date)
    return trip_log


def parse_feeds_into_trip_logbook(feeds, information_dates):
    """
    Given a list of feeds and a list of information dates, returns a hash table of trip logs associated with each
    trip mentioned in those feeds.

    The ultimate method for which all of the above was developed.
    """
    message_tables = [_sort_feed_messages_by_trip_id(feed) for feed in feeds]
    trip_ids = set(itertools.chain(*[table.keys() for table in message_tables]))

    ret = dict()

    for trip_id in trip_ids:
        actions_logs = []
        trip_began = False
        trip_terminated = False
        trip_terminated_time = None

        for i, table in enumerate(message_tables):
            # Is the trip present in this table at all?
            if not table[trip_id]:
                # If the trip hasn't been planned yet, and will simply appear in a later trip update, do nothing.
                if not trip_began:
                    pass

                # If the trip has been planned already, and doesn't exist in the current table, then it must have
                # been removed. This implies that this trip terminated in the interceding time. Store this
                # information for later.
                else:
                    trip_terminated = True
                    trip_terminated_time = information_dates[i]

                continue
            else:
                trip_began = True

            action_log = _parse_message_list_into_action_log(table[trip_id], information_dates[i])
            actions_logs.append(action_log)
        trip_log = parse_tripwise_action_logs_into_trip_log(actions_logs)
        ret[trip_id] = trip_log

        # If the trip was terminated sometime in the course of these feeds, update the trip log accordingly.
        if trip_terminated:
            ret[trip_id] = _finish_trip(ret[trip_id], trip_terminated_time)

    return ret


def merge_trip_logbooks(logbooks):
    """
    Given a list of trip logbooks (as returned by `parse_feeds_into_trip_logbooks`), returns their merger.
    """
    left = dict()
    for right in logbooks:
        left = _join_logbooks(left, right)
    return left


def _join_logbooks(left, right):
    """
    Given two trip logbooks (as returned by `parse_feeds_into_trip_logbooks`), returns the merger of the two.
    """
    # Figure out what our jobs are by trip id key.
    left_keys = set(left.keys())
    right_keys = set(right.keys())

    mutual_keys = left_keys.intersection(right_keys)
    left_exclusive_keys = left_keys.difference(mutual_keys)
    right_exclusive_keys = right_keys.difference(mutual_keys)

    # Build out non-intersecting trips.
    result = dict()
    for key in left_exclusive_keys:
        result[key] = left[key]
    for key in right_exclusive_keys:
        result[key] = right[key]

    # Build out (join) intersecting trips.
    for key in mutual_keys:
        result[key] = _join_trip_logs(left[key], right[key])

    return result


# noinspection PyUnresolvedReferences
def _join_trip_logs(left, right):
    """
    Two trip logs may contain information based on action logs, and GTFS-Realtime feed updates, which are
    dis-contiguous in time. In other words, these logs reflect the same trip, but are based on different sets of
    observations.

    In such cases recovering a full(er) record requires merging these two logs together. Here we implement this
    operation.

    This method, the core of merge_trip_logbooks, is an operational necessity, as a day's worth of raw GTFS-R
    messages at minutely resolution eats up 12 GB of RAM or more.
    """
    # Order the frames so that the earlier one is on the left.
    left_start, right_start = left['latest_information_time'].min(), right['latest_information_time'].min()
    if right_start < left_start:
        left, right = right, left

    # Get the combined synthetic station list.
    stations = _extract_synthetic_route_from_station_lists([list(left['stop_id'].values),
                                                            list(right['stop_id'].values)])
    right_stations = set(right['stop_id'].values)

    # Combine the station information in last-precedent order.
    l_i = r_i = 0
    left_indices, right_indices = [], []

    for station in stations:
        if station not in right_stations:
            left_indices.append(l_i)
            l_i += 1

    # Combine records.
    join = pd.concat([left.iloc[left_indices], right]).reset_index(drop=True)

    # Declaring an ordinal categorical column in the stop_id attribute makes `pandas` handle resorting internally and,
    # hence, results in a significant speedup (over doing so ourselves).
    join['stop_id'] = pd.Categorical(join['stop_id'], stations, ordered=True)

    # Update records for stations before the first station in the right trip log that the train is EN_ROUTE_TO or
    # STOPPED_OR_SKIPPED.
    swap_station = right.iloc[0]['stop_id']
    swap_index = next(i for i, station in enumerate(stations) if station == swap_station)
    swap_space = join[:swap_index]
    where_update = swap_space[swap_space['action'] == 'EN_ROUTE_TO'].index.values

    join.loc[where_update, 'action'] = 'STOPPED_OR_SKIPPED'
    join.loc[where_update, 'maximum_time'] = right.loc[0, 'latest_information_time']
    join.loc[swap_index, 'minimum_time'] = left.loc[0, 'minimum_time']

    # Hard-case the columns to float so as to avoid weird typing issues that keep coming up.
    # TODO: Hard-fix the typing issues and simplify the tests to reflect.
    join.loc[:, ['minimum_time', 'maximum_time']] = join.loc[:, ['minimum_time', 'maximum_time']].astype(float)

    # The second trip update may on the first index contain incomplete minimum time information due to not having a
    # reference to a previous trip update included in that trip log's generative action log set. There are a number
    # of ways in which this can occur, but the end fact of the matter is that between the last entry in the first
    # trip log and the first entry in the second trip log, we may have one of three different inconsistencies:
    #
    # 1. The prior states that the train stopped at (or skipped) the last station in that log at some known time,
    #    but the minimum time of the first stop or skip in the posterior log is a NaN, due to lack of prior information.
    # 2. The prior states that the train stopped at (or skipped) the last station in that log at some known minimum
    #    time, but the posterior log first entry minimum time is even earlier.
    # 3. The prior states that the train stopped at (or skipped) the last station in that log at some known maximum
    #    time, but the posterior log first entry minimum time is even earlier.
    #
    # The lines below handle each one of these possible inconsistencies in turn.
    join.loc[:, 'minimum_time'] = join.loc[:, 'minimum_time'].fillna(method='ffill')
    join.loc[1:, 'minimum_time'] = np.maximum.accumulate(join.loc[1:, 'minimum_time'].values)

    join.loc[len(left) -1, 'minimum_time'] = np.maximum(np.nan_to_num(join.loc[len(left) - 2, 'maximum_time']),
                                                        join.loc[len(left) - 1, 'minimum_time'])

    # Again at the location of the join, we may also get an incomplete `maximum_time` entry, for the same reason. In
    # this case we will take the `maximum_time` of the following entry. However, note that we are *losing
    # information* in this case, as we could technically resolve this time to a more accurate one, given the full
    # list of information times. However, we do not have that information at this time in the processing sequence.
    # This is an unfortunate but not particularly important, all things considered, technical shortcoming of the way
    # we chose to code things.

    join.loc[:, 'maximum_time'] = join.loc[:, 'maximum_time'].fillna(method='bfill', limit=1)

    return join
