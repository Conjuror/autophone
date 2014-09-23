# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import ConfigParser
import json
import urllib
import urllib2
import urlparse
from math import sqrt
from time import sleep

from jot import jwt, jws

from adb import ADBError
from phonestatus import TestResult
from phonetest import PhoneTest

class PerfTest(PhoneTest):
    def __init__(self, phone, options, config_file=None,
                 enable_unittests=False, test_devices_repos={}):
        PhoneTest.__init__(self, phone, options,
                           config_file=config_file,
                           enable_unittests=enable_unittests,
                           test_devices_repos=test_devices_repos)
        self._result_server = None
        self._resulturl = None

    def setup_job(self):
        PhoneTest.setup_job(self)

        # [signature]
        self._signer = None
        self._jwt = {'id': '', 'key': None}
        for opt in self._jwt.keys():
            try:
                self._jwt[opt] = self.cfg.get('signature', opt)
            except (ConfigParser.NoSectionError,
                    ConfigParser.NoOptionError):
                break
        # phonedash requires both an id and a key.
        if self._jwt['id'] and self._jwt['key']:
            self._signer = jws.HmacSha(key=self._jwt['key'],
                                       key_id=self._jwt['id'])
        # [settings]
        self._iterations = self.cfg.getint('settings', 'iterations')
        try:
            self.stderrp_accept = self.cfg.getfloat('settings', 'stderrp_accept')
        except ConfigParser.NoOptionError:
            self.stderrp_accept = 0
        try:
            self.stderrp_reject = self.cfg.getfloat('settings', 'stderrp_reject')
        except ConfigParser.NoOptionError:
            self.stderrp_reject = 100
        try:
            self.stderrp_attempts = self.cfg.getint('settings', 'stderrp_attempts')
        except ConfigParser.NoOptionError:
            self.stderrp_attempts = 1
        self._resulturl = self.cfg.get('settings', 'resulturl')
        if not self._resulturl.endswith('/'):
            self._resulturl += '/'

    @property
    def result_server(self):
        if self._resulturl and not self._result_server:
            parts = urlparse.urlparse(self._resulturl)
            self._result_server = '%s://%s' % (parts.scheme, parts.netloc)
            self.loggerdeco.debug('PerfTest._result_server: %s' % self._result_server)
        return self._result_server

    def teardown_job(self):
        PhoneTest.teardown_job(self)

    def get_logcat(self):
        for attempt in range(1, self.options.phone_retry_limit+1):
            try:
                return [x.strip() for x in self.dm.get_logcat(
                    filter_specs=['*:V']
                )]
            except ADBError:
                self.loggerdeco.exception('Attempt %d get logcat throbbers' % attempt)
                if attempt == self.options.phone_retry_limit:
                    raise
                sleep(self.options.phone_retry_wait)

    def publish_results(self, starttime=0, tstrt=0, tstop=0,
                        testname='', cache_enabled=True,
                        rejected=False):
        msg = ('Cached: %s Start Time: %s Throbber Start: %s Throbber Stop: %s '
               'Total Throbber Time: %s Rejected: %s' % (
                   cache_enabled, starttime, tstrt, tstop, tstop - tstrt, rejected))
        self.loggerdeco.debug('RESULTS: %s' % msg)

        # Create JSON to send to webserver
        resultdata = {
            'phoneid': self.phone.id,
            'testname': testname,
            'starttime': starttime,
            'throbberstart': tstrt,
            'throbberstop': tstop,
            'blddate': self.build.date,
            'cached': cache_enabled,
            'rejected': rejected,
            'revision': self.build.revision,
            'productname': self.build.app_name,
            'productversion': self.build.version,
            'osver': self.phone.osver,
            'bldtype': self.build.type,
            'machineid': self.phone.machinetype
        }

        result = {'data': resultdata}
        # Upload
        if self._signer:
            encoded_result = jwt.encode(result, signer=self._signer)
            content_type = 'application/jwt'
        else:
            encoded_result = json.dumps(result)
            content_type = 'application/json; charset=utf-8'
        req = urllib2.Request(self._resulturl + 'add/', encoded_result,
                              {'Content-Type': content_type})
        try:
            f = urllib2.urlopen(req)
        except urllib2.URLError, e:
            self.loggerdeco.error('Error sending results to server: %s' % e)
            self.worker_subprocess.mailer.send(
                'Error sending %s results for phone %s, build %s' %
                (self.name, self.phone.id, self.build.id),
                'There was an error attempting to send test results'
                'to the result server %s.\n'
                '\n'
                'Test %s\n'
                'Phone %s\n'
                'Build %s\n'
                'Revision %s\n'
                'Exception: %s\n' %
                (self.result_server,
                 self.name, self.phone.id, self.build.id,
                 self.build.revision, e))
            message = 'Error sending results to server'
            self.result = TestResult.EXCEPTION
            self.message = message
            self.update_status(message=message)
        else:
            f.read()
            f.close()

    def check_results(self, testname=''):
        """Return True if there already exist unrejected results for this device,
        build and test.
        """

        # Create JSON to send to webserver
        query = {
            'phoneid': self.phone.id,
            'test': testname,
            'revision': self.build.revision,
            'product': self.build.app_name
        }

        self.loggerdeco.debug('check_results for: %s' % query)

        try:
            url = self._resulturl + 'check/?' + urllib.urlencode(query)
            f = urllib2.urlopen(url)
        except urllib2.URLError, e:
            self.loggerdeco.error(
                'check_results: %s could not check: '
                'phoneid: %s, test: %s, revision: %s, product: %s' % (
                    e,
                    query['phoneid'], query['test'],
                    query['revision'], query['product']))
            return False
        data = f.read()
        self.loggerdeco.debug('check_results: data: %s' % data)
        f.close()
        response = json.loads(data)
        return response['result']

    def get_stats(self, values):
        """Calculate and return an object containing the count, mean,
        standard deviation, standard error of the mean and percentage
        standard error of the mean of the values list."""
        r = {'count': len(values)}
        if r['count'] == 1:
            r['mean'] = values[0]
            r['stddev'] = 0
            r['stderr'] = 0
            r['stderrp'] = 0
        else:
            r['mean'] = sum(values) / float(r['count'])
            r['stddev'] = sqrt(sum([(value - r['mean'])**2 for value in values])/float(r['count']-1.5))
            r['stderr'] = r['stddev']/sqrt(r['count'])
            r['stderrp'] = 100.0*r['stderr']/float(r['mean'])
        return r

    def is_stderr_below_threshold(self, measurements, dataset, threshold):
        """Return True if all of the measurements in the dataset have
        standard errors of the mean below the threshold.

        Return False if at least one measurement is above the threshold
        or if one or more datasets have only one value.

        Return None if at least one measurement has no values.
        """

        self.loggerdeco.debug("is_stderr_below_threshold: %s" % dataset)

        for cachekey in ('uncached', 'cached'):
            for measurement in measurements:
                data = [datapoint[cachekey][measurement] - datapoint[cachekey]['starttime']
                        for datapoint in dataset
                        if datapoint and cachekey in datapoint]
                if not data:
                    return None
                stats = self.get_stats(data)
                self.loggerdeco.debug('%s %s count: %d, mean: %.2f, '
                                      'stddev: %.2f, stderr: %.2f, '
                                      'stderrp: %.2f' % (
                                          cachekey, measurement,
                                          stats['count'], stats['mean'],
                                          stats['stddev'], stats['stderr'],
                                          stats['stderrp']))
                if stats['count'] == 1 or stats['stderrp'] >= threshold:
                    return False
        return True