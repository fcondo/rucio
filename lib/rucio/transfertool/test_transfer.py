from rucio.common.config import config_get, config_get_bool
from rucio.common.exception import TransferToolTimeout, TransferToolWrongAnswer
from rucio.core.monitor import record_counter, record_timer
from rucio.transfertool.transfertool import Transfertool
import requests
import json


class Transfertool(Transfertool):
    """
    SFTP implementation of a Rucio transfertool
    """

    def __init__(self, external_host):
        self.base_url = 'http://%s' % external_host

    def submit(self, files, job_params, timeout=None):
        response = requests.post('{0}/{1}'.format(self.base_url, 'jobs'), data=json.dumps(files))
        return response.text

    def query(self, transfer_id, details=False, timeout=None):
        response = requests.get('{0}/{1}/{2}'.format(self.base_url, 'job', transfer_id))
        return response.text

    def bulk_query(self, transfer_ids, timeout=None):
        response = requests.get('{0}/{1}/{2}'.format(self.base_url, 'job', ','.join(transfer_ids)))
        return response.text

    def cancel(self, transfer_id, timeout=None):
        requests.delete('{0}/{1}/{2}'.format(self.base_url, 'job', transfer_id))

    def update_priority(self, transfer_id, priority, timeout=None):
        pass
