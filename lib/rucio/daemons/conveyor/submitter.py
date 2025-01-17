# Copyright 2013-2018 CERN for the benefit of the ATLAS collaboration.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# Authors:
# - Mario Lassnig <mario.lassnig@cern.ch>, 2013-2015
# - Cedric Serfon <cedric.serfon@cern.ch>, 2013-2018
# - Ralph Vigne <ralph.vigne@cern.ch>, 2013
# - Vincent Garonne <vgaronne@gmail.com>, 2014-2018
# - Martin Barisits <martin.barisits@cern.ch>, 2014-2017
# - Wen Guan <wguan.icedew@gmail.com>, 2014-2016
# - Tomas Kouba <tomas.kouba@cern.ch>, 2014
# - Joaquin Bogado <jbogado@linti.unlp.edu.ar>, 2016
# - Hannes Hansen <hannes.jakob.hansen@cern.ch>, 2018
#
# PY3K COMPATIBLE

"""
Conveyor transfer submitter is a daemon to manage non-tape file transfers.
"""

from __future__ import division

import logging
import os
import random
import socket
import sys
import threading
import time
import traceback

from collections import defaultdict
try:
    from ConfigParser import NoOptionError  # py2
except Exception:
    from configparser import NoOptionError  # py3
from six import iteritems

from rucio.common.config import config_get
from rucio.core import heartbeat, request as request_core, transfer as transfer_core
from rucio.core.monitor import record_counter, record_timer
from rucio.daemons.conveyor.common import submit_transfer, bulk_group_transfer, get_conveyor_rses, USER_ACTIVITY
from rucio.db.sqla.constants import RequestState

logging.basicConfig(stream=sys.stdout,
                    level=getattr(logging,
                                  config_get('common', 'loglevel',
                                             raise_exception=False,
                                             default='DEBUG').upper()),
                    format='%(asctime)s\t%(process)d\t%(levelname)s\t%(message)s')

graceful_stop = threading.Event()

USER_TRANSFERS = config_get('conveyor', 'user_transfers', False, None)


def submitter(once=False, rses=None, mock=False,
              bulk=100, group_bulk=1, group_policy='rule', source_strategy=None,
              activities=None, sleep_time=600, max_sources=4, retry_other_fts=False):
    """
    Main loop to submit a new transfer primitive to a transfertool.
    """

    try:
        scheme = config_get('conveyor', 'scheme')
    except NoOptionError:
        scheme = None
    try:
        failover_scheme = config_get('conveyor', 'failover_scheme')
    except NoOptionError:
        failover_scheme = None
    try:
        timeout = config_get('conveyor', 'submit_timeout')
        timeout = float(timeout)
    except NoOptionError:
        timeout = None

    try:
        bring_online = config_get('conveyor', 'bring_online')
    except NoOptionError:
        bring_online = 43200

    try:
        max_time_in_queue = {}
        timelife_conf = config_get('conveyor', 'max_time_in_queue')
        timelife_confs = timelife_conf.split(",")
        for conf in timelife_confs:
            act, timelife = conf.split(":")
            max_time_in_queue[act.strip()] = int(timelife.strip())
    except NoOptionError:
        max_time_in_queue = {}

    if 'default' not in max_time_in_queue:
        max_time_in_queue['default'] = 168
    logging.debug("Maximum time in queue for different activities: %s" % max_time_in_queue)

    activity_next_exe_time = defaultdict(time.time)
    executable = sys.argv[0]
    if activities:
        activities.sort()
        executable += '--activities ' + str(activities)

    hostname = socket.getfqdn()
    pid = os.getpid()
    hb_thread = threading.current_thread()
    heartbeat.sanity_check(executable=executable, hostname=hostname)
    heart_beat = heartbeat.live(executable, hostname, pid, hb_thread)
    prepend_str = 'Thread [%i/%i] : ' % (heart_beat['assign_thread'] + 1, heart_beat['nr_threads'])
    logging.info(prepend_str + 'Submitter starting with timeout %s' % (timeout))

    time.sleep(10)  # To prevent running on the same partition if all the poller restart at the same time
    heart_beat = heartbeat.live(executable, hostname, pid, hb_thread)
    prepend_str = 'Thread [%i/%i] : ' % (heart_beat['assign_thread'] + 1, heart_beat['nr_threads'])
    logging.info(prepend_str + 'Transfer submitter started')

    while not graceful_stop.is_set():

        try:
            heart_beat = heartbeat.live(executable, hostname, pid, hb_thread, older_than=3600)
            prepend_str = 'Thread [%i/%i] : ' % (heart_beat['assign_thread'] + 1, heart_beat['nr_threads'])

            if activities is None:
                activities = [None]
            if rses:
                rse_ids = [rse['id'] for rse in rses]
            else:
                rse_ids = None
            for activity in activities:
                if activity_next_exe_time[activity] > time.time():
                    graceful_stop.wait(1)
                    continue

                user_transfer = False

                if activity in USER_ACTIVITY and USER_TRANSFERS in ['cms']:
                    logging.info(prepend_str + "CMS user transfer activity")
                    user_transfer = True

                logging.info(prepend_str + 'Starting to get transfer transfers for %s' % (activity))
                start_time = time.time()
                transfers = __get_transfers(total_workers=heart_beat['nr_threads'] - 1,
                                            worker_number=heart_beat['assign_thread'],
                                            failover_schemes=failover_scheme,
                                            limit=bulk,
                                            activity=activity,
                                            rses=rse_ids,
                                            schemes=scheme,
                                            mock=mock,
                                            max_sources=max_sources,
                                            bring_online=bring_online,
                                            retry_other_fts=retry_other_fts)
                record_timer('daemons.conveyor.transfer_submitter.get_transfers.per_transfer', (time.time() - start_time) * 1000 / (len(transfers) if transfers else 1))
                record_counter('daemons.conveyor.transfer_submitter.get_transfers', len(transfers))
                record_timer('daemons.conveyor.transfer_submitter.get_transfers.transfers', len(transfers))
                logging.info(prepend_str + 'Got %s transfers for %s in %s seconds' % (len(transfers), activity, time.time() - start_time))

                # group transfers
                logging.info(prepend_str + 'Starting to group transfers for %s' % (activity))
                start_time = time.time()

                grouped_jobs = bulk_group_transfer(transfers, group_policy, group_bulk, source_strategy, max_time_in_queue)
                record_timer('daemons.conveyor.transfer_submitter.bulk_group_transfer', (time.time() - start_time) * 1000 / (len(transfers) if transfers else 1))

                logging.info(prepend_str + 'Starting to submit transfers for %s' % (activity))

                for external_host in grouped_jobs:
                    if not user_transfer:
                        for job in grouped_jobs[external_host]:
                            # submit transfers
                            submit_transfer(external_host=external_host, job=job, submitter='transfer_submitter',
                                            logging_prepend_str=prepend_str, timeout=timeout)
                    else:
                        for _, jobs in iteritems(grouped_jobs[external_host]):
                            # submit transfers
                            for job in jobs:
                                submit_transfer(external_host=external_host, job=job, submitter='transfer_submitter',
                                                logging_prepend_str=prepend_str, timeout=timeout, user_transfer_job=user_transfer)

                if len(transfers) < group_bulk:
                    logging.info(prepend_str + 'Only %s transfers for %s which is less than group bulk %s, sleep %s seconds' % (len(transfers), activity, group_bulk, sleep_time))
                    if activity_next_exe_time[activity] < time.time():
                        activity_next_exe_time[activity] = time.time() + sleep_time
        except Exception:
            logging.critical(prepend_str + '%s' % (traceback.format_exc()))

        if once:
            break

    logging.info(prepend_str + 'Graceful stop requested')

    heartbeat.die(executable, hostname, pid, hb_thread)

    logging.info(prepend_str + 'Graceful stop done')
    return


def stop(signum=None, frame=None):
    """
    Graceful exit.
    """
    graceful_stop.set()


def run(once=False, group_bulk=1, group_policy='rule',
        mock=False, rses=None, include_rses=None, exclude_rses=None, bulk=100, source_strategy=None,
        activities=None, sleep_time=600, max_sources=4, retry_other_fts=False, total_threads=1):
    """
    Starts up the conveyer threads.
    """

    if mock:
        logging.info('mock source replicas: enabled')

    working_rses = None
    if rses or include_rses or exclude_rses:
        working_rses = get_conveyor_rses(rses, include_rses, exclude_rses)
        logging.info("RSE selection: RSEs: %s, Include: %s, Exclude: %s" % (rses,
                                                                            include_rses,
                                                                            exclude_rses))
    else:
        logging.info("RSE selection: automatic")

    logging.info('starting submitter threads')
    threads = [threading.Thread(target=submitter, kwargs={'once': once,
                                                          'rses': working_rses,
                                                          'bulk': bulk,
                                                          'group_bulk': group_bulk,
                                                          'group_policy': group_policy,
                                                          'activities': activities,
                                                          'mock': mock,
                                                          'sleep_time': sleep_time,
                                                          'max_sources': max_sources,
                                                          'source_strategy': source_strategy,
                                                          'retry_other_fts': retry_other_fts}) for _ in range(0, total_threads)]

    [thread.start() for thread in threads]

    logging.info('waiting for interrupts')

    # Interruptible joins require a timeout.
    while threads:
        threads = [thread.join(timeout=3.14) for thread in threads if thread and thread.isAlive()]


def __get_transfers(total_workers=0, worker_number=0, failover_schemes=None, limit=None, activity=None, older_than=None,
                    rses=None, schemes=None, mock=False, max_sources=4, bring_online=43200,
                    retry_other_fts=False):
    """
    Get transfers to process

    :param total_workers:    Number of total workers.
    :param worker_number:    Id of the executing worker.
    :param failover_schemes: Failover schemes.
    :param limit:            Integer of requests to retrieve.
    :param activity:         Activity to be selected.
    :param older_than:       Only select requests older than this DateTime.
    :param rses:             List of rse_id to select requests.
    :param schemes:          Schemes to process.
    :param mock:             Mock testing.
    :param max_sources:      Max sources.
    :bring_online:           Bring online timeout.
    :retry_other_fts:        Retry other fts servers if needed
    :returns:                List of transfers
    """

    transfers, reqs_no_source, reqs_scheme_mismatch, reqs_only_tape_source = transfer_core.get_transfer_requests_and_source_replicas(total_workers=total_workers,
                                                                                                                                     worker_number=worker_number,
                                                                                                                                     limit=limit,
                                                                                                                                     activity=activity,
                                                                                                                                     older_than=older_than,
                                                                                                                                     rses=rses,
                                                                                                                                     schemes=schemes,
                                                                                                                                     bring_online=bring_online,
                                                                                                                                     retry_other_fts=retry_other_fts,
                                                                                                                                     failover_schemes=failover_schemes)
    request_core.set_requests_state(reqs_no_source, RequestState.NO_SOURCES)
    request_core.set_requests_state(reqs_only_tape_source, RequestState.ONLY_TAPE_SOURCES)
    request_core.set_requests_state(reqs_scheme_mismatch, RequestState.MISMATCH_SCHEME)

    for request_id in transfers:
        sources = transfers[request_id]['sources']
        sources = __sort_ranking(sources)
        if len(sources) > max_sources:
            sources = sources[:max_sources]
        if not mock:
            transfers[request_id]['sources'] = sources
        else:
            transfers[request_id]['sources'] = __mock_sources(sources)

        # remove link_ranking in the final sources
        sources = transfers[request_id]['sources']
        transfers[request_id]['sources'] = []
        for rse, source_url, source_rse_id, ranking, link_ranking in sources:
            transfers[request_id]['sources'].append((rse, source_url, source_rse_id, ranking))

        transfers[request_id]['file_metadata']['src_rse'] = sources[0][0]
        transfers[request_id]['file_metadata']['src_rse_id'] = sources[0][2]
        logging.debug("Transfer for request(%s): %s" % (request_id, transfers[request_id]))
    return transfers


def __sort_link_ranking(sources):
    """
    Sort a list of sources based on link ranking

    :param sources:  List of sources
    :return:         Sorted list
    """

    rank_sources = {}
    ret_sources = []
    for source in sources:
        rse, source_url, source_rse_id, ranking, link_ranking = source
        if link_ranking not in rank_sources:
            rank_sources[link_ranking] = []
        rank_sources[link_ranking].append(source)
    rank_keys = list(rank_sources.keys())
    rank_keys.sort(reverse=True)
    for rank_key in rank_keys:
        sources_list = rank_sources[rank_key]
        random.shuffle(sources_list)
        ret_sources = ret_sources + sources_list
    return ret_sources


def __sort_ranking(sources):
    """
    Sort a list of sources based on ranking

    :param sources:  List of sources
    :return:         Sorted list
    """

    logging.debug("Sources before sorting: %s" % sources)
    rank_sources = {}
    ret_sources = []
    for source in sources:
        # ranking is from sources table, is the retry times
        # link_ranking is from distances table, is the link rank.
        # link_ranking should not be None(None means no link, the source will not be used).
        rse, source_url, source_rse_id, ranking, link_ranking = source
        if ranking is None:
            ranking = 0
        if ranking not in rank_sources:
            rank_sources[ranking] = []
        rank_sources[ranking].append(source)
    rank_keys = list(rank_sources.keys())
    rank_keys.sort(reverse=True)
    for rank_key in rank_keys:
        sources_list = __sort_link_ranking(rank_sources[rank_key])
        ret_sources = ret_sources + sources_list
    logging.debug("Sources after sorting: %s" % ret_sources)
    return ret_sources


def __mock_sources(sources):
    """
    Create mock sources

    :param sources:  List of sources
    :return:         List of mock sources
    """

    tmp_sources = []
    for s in sources:
        tmp_sources.append((s[0], ':'.join(['mock'] + s[1].split(':')[1:]), s[2], s[3]))
    sources = tmp_sources
    return tmp_sources
