from flask import Flask, request, Response, jsonify
import pysftp
import os
from urlparse import urlparse
import threading
import uuid
import sqlite3
import json
import time

import logging
logger = logging.getLogger('myapp')
hdlr = logging.FileHandler('log.log')
formatter = logging.Formatter('%(asctime)s %(levelname)s %(message)s')
hdlr.setFormatter(formatter)
logger.addHandler(hdlr)
logger.setLevel(logging.DEBUG)

app = Flask(__name__)


@app.route("/jobs", methods=['POST'])
def submit_transfer():
    jobs = []
    transfer_id = str(uuid.uuid4())
    jobs = json.loads(request.data)
    for job in jobs:
        add_job_to_db(**{
            'id': str(uuid.uuid4()),
            'src': job['sources'][0][1],
            'dest': job['destinations'][0],
            'priority': 1,
            'transfer_id': transfer_id,
            'status': 'queued'
        })
    return transfer_id


@app.route("/jobs/<id>")
def stop_transfer(id):
    delete_job_from_db(id)


@app.route("/update")
def update(self):
    pass


@app.route("/job/<id>")
def info_transfer(id):
    if ',' in id:
        ids = id.split(',')
        transfers = {}
        for id in ids:
            transfers[id] = get_job_from_db(id)
        return jsonify(transfers)
    else:
        return jsonify(get_job_from_db(id))


def watch_queue():
    while True:
        transfers = get_all_jobs_from_db()
        for transfer in transfers:
            logger.error(1)
            logger.error(transfer)
            result = start_transfer(transfer)
            if result:
                update_job(transfer[2], 'status', 'done')
        time.sleep(15)


def peform_query(query):
    db = sqlite3.connect('sftp_transfers')
    logger.error(query)
    cursor = db.cursor()
    cursor_result = cursor.execute(query)
    db.commit()
    return cursor_result.fetchall()
    """
    try:
        with sqlite3.connect('sftp_transfers') as db:
            logger.error(query)
            cursor = db.cursor()
            cursor_result = cursor.execute(query)
            db.commit()
            logger.error(cursor_result.fetchall())
            return cursor_result.fetchall()
    except Exception as e:
        db.rollback()
        logger.info(e)
        return False
    """


def get_job_from_db(job_id):
    return peform_query('SELECT * FROM jobs WHERE transfer_id = "{0}"'.format(job_id))[0]


def delete_job_from_db(job_id):
    return peform_query('DELETE FROM jobs WHERE id = "{0}"'.format(job_id))


def add_job_to_db(id, src, dest, priority, transfer_id, status):
    return peform_query('INSERT INTO jobs VALUES ("{0}","{1}","{2}","{3}","{4}", "{5}")'.format(src, dest, id, priority, transfer_id, status))


def update_job(id, key, value):
    return peform_query('UPDATE jobs SET {0} = "{1}" WHERE id = "{2}"'.format(key, value, id))


def get_all_jobs_from_db():
    result = peform_query('SELECT * FROM jobs')
    logger.error(1)
    logger.error([a for a in result])
    return result


def start_transfer(job):
    logger.error('start')
    logger.error(job)
    parsed_src = urlparse(job[0])
    parsed_dest = urlparse(job[1])
    file_source_path = parsed_src.path[1:]
    file_tmp_path = 'tmp_file'
    file_dest_path = parsed_dest.path[1:]
    # todo with
    src_connection = pysftp.Connection(host=parsed_src.netloc.split(':')[0], username='user', password='password')
    dest_connection = pysftp.Connection(host=parsed_dest.netloc.split(':')[0], username='user', password='password')

    result = True
    try:
        logger.error(file_source_path)
        logger.error(file_tmp_path)
        src_connection.get(file_source_path, file_tmp_path)
        try:
            dest_connection.put(file_tmp_path, file_dest_path)
        except IOError as e:
            dest_connection.execute('mkdir -p "%s"' % '/'.join(file_dest_path.split('/')[0:-1]))
            dest_connection.put(file_tmp_path, file_dest_path)

    except Exception as e:
        logger.error(e)
        result = False
    finally:
        os.remove(file_tmp_path)
    return result


peform_query('CREATE TABLE IF NOT EXISTS jobs (src varchar(255), dest varchar(255), id varchar(255), priority varchar(255), transfer_id varchar(255), status varchar(255))')
threading.Thread(target=watch_queue).start()

if __name__ == '__main__':
    app.run(debug=True)
