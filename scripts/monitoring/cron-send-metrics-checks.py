#!/usr/bin/env python
'''
  Send Cluster Metrics checks to Zagg
'''
# vim: expandtab:tabstop=4:shiftwidth=4
#
#   Copyright 2015 Red Hat Inc.
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.
#
#This is not a module, but pylint thinks it is.  This is a command.
#pylint: disable=invalid-name
#If a check throws an exception it is failed and should alert
#pylint: disable=bare-except
#pylint: disable=wrong-import-position
#pylint: disable=line-too-long
import argparse
import base64
import logging
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
import urllib2

logging.basicConfig(
    format='%(asctime)s - %(relativeCreated)6d - %(levelname)-8s - %(message)s',
)
logger = logging.getLogger()
logger.setLevel(logging.INFO)
commandDelay = 5
local_report_details_dir = '/opt/failure_reports/'
max_details_report_files = 5

import os
from datetime import datetime
import yaml


# pylint: disable=import-error
from openshift_tools.monitoring.ocutil import OCUtil
from openshift_tools.monitoring.metric_sender import MetricSender
# pylint: enable=import-error

class OpenshiftMetricsStatus(object):
    '''
        This is a check for making sure metrics is up and running
        and nodes fluentd can populate data from each node
    '''
    def __init__(self):
        ''' Initialize OpenShiftMetricsStatus class '''
        self.metric_sender = None
        self.oc = None
        self.args = None
        self.deployer_pod_name = None
        self.hawkular_pod_name = None
        self.hawkular_username = None
        self.hawkular_password = None

    def parse_args(self):
        ''' Parse arguments passed to the script '''
        parser = argparse.ArgumentParser(description='OpenShift Cluster Metrics Checker')
        parser.add_argument('-v', '--verbose', action='store_true', default=None, help='Verbose output')
        parser.add_argument('--debug', action='store_true', default=None, help='Debug?')

        self.args = parser.parse_args()

    def check_pods(self):
        ''' Check all metrics related pods '''
        pods = self.oc.get_pods()
        pod_report = {}

        for pod in pods['items']:
            if 'metrics-infra' in pod['metadata']['labels']:
                pod_name = pod['metadata']['name']

                # We do not care to monitor the deployer pod
                if pod_name.startswith('metrics-deployer'):
                    self.deployer_pod_name = pod_name
                    continue

                if pod_name.startswith('hawkular-metrics-'):
                    self.hawkular_pod_name = pod_name

                pod_pretty_name = pod['metadata']['labels']['name']
                pod_report[pod_pretty_name] = {}

                # Get the pods ready status
                pod_report[pod_pretty_name]['status'] = int(pod['status']['containerStatuses'][0]['ready'])

                # Number of times a pod has been restarted
                pod_report[pod_pretty_name]['restarts'] = pod['status']['containerStatuses'][0]['restartCount']

                # Get the time the pod was started, otherwise return 0
                try:
                    pod_start_time = pod['status']['containerStatuses'][0]['state']['running']['startedAt']
                    # oc get pods is returning this field as both
                    # date (oc <= 3.11.153) and a string (oc >= 3.11.154 with yaml.v2 update)
                    # yaml.v2 contains a modification that will output strings for dates
                    # if unmarshalled into interface{}
                    # even after yaml.v3 is released (which removes the yaml.v2 issue)
                    # we still need to support differing go yaml packages
                    if isinstance(pod_start_time, str):
                        pod_start_time = datetime.strptime(pod_start_time, "%Y-%m-%dT%H:%M:%SZ")

                        # Since we convert to seconds it is an INT but pylint still complains. Only disable here
                        # pylint: disable=E1101
                        # pylint: disable=maybe-no-member
                        pod_start_time = int(pod_start_time.strftime("%s"))
                        # pylint: enable=E1101
                        # pylint: enable=maybe-no-member
                except KeyError:
                    pod_start_time = 0

                pod_report[pod_pretty_name]['starttime'] = pod_start_time

        return pod_report

    def get_hawkular_creds(self):
        '''
            Looks up hawkular username and password in a secret.
            If the secret does not exist parse it out of the deploy log.
        '''
        # Check to see if secret for htpasswd exists
        try:
            # If so get http password from secret
            secret = self.oc.get_secrets("hawkular-htpasswd")
            # We have seen cases where username and passwork have gotten an added newline to the end
            self.hawkular_username = base64.b64decode(secret['data']['hawkular-username']).rstrip('\n')
            self.hawkular_password = base64.b64decode(secret['data']['hawkular-password']).rstrip('\n')
        except:
            self.hawkular_username = 'hawkular'
            passwd_file = "/hawkular-account/hawkular-metrics.password"
            try:
                self.hawkular_password = self.oc.run_user_cmd("rsh {} cat {}".format(self.hawkular_pod_name, passwd_file))
                self.hawkular_password = self.hawkular_password.rstrip('\n')
            except:
                passwd_file = "/client-secrets/hawkular-metrics.password"
                self.hawkular_password = self.oc.run_user_cmd("rsh {} cat {}".format(self.hawkular_pod_name, passwd_file))
                self.hawkular_password = self.hawkular_password.rstrip('\n')

            new_secret = {}
            new_secret['username'] = self.hawkular_username
            new_secret['password'] = self.hawkular_password

            directory_name = tempfile.mkdtemp()
            file_loc = "{}/hawkular-username".format(directory_name)
            temp_file = open(file_loc, 'a')
            temp_file.write(self.hawkular_username)
            temp_file.close()

            file_loc = "{}/hawkular-password".format(directory_name)
            temp_file = open(file_loc, 'a')
            temp_file.write(self.hawkular_password)
            temp_file.close()

            self.oc.run_user_cmd("secrets new hawkular-htpasswd {}".format(directory_name))
            shutil.rmtree(directory_name)

        if not self.hawkular_username or not self.hawkular_password:
            print "Failed to get hawkular username or password"
            sys.exit(1)

    def check_node_metrics(self):
        ''' Verify that fluentd on all nodes is able to talk to and populate data in hawkular '''
        result_report = {'success': 1, 'failed_nodes': []}
        # Get all nodes
        nodes = self.oc.get_nodes()
        # Get the hawkular route
        route = self.oc.get_route('hawkular-metrics')['status']['ingress'][0]['host']

        # Setup the URL headers
        auth_header = "Basic {}".format(
            base64.b64encode("{}:{}".format(self.hawkular_username, self.hawkular_password))
        )
        headers = {"Authorization": "{}".format(auth_header),
                   "Hawkular-tenant": "_system"}

        # Build url
        hawkular_url_start = "https://{}/hawkular/metrics/gauges/rate/stats?tags=nodename:".format(route)
        hawkular_url_end = ",type:node,group_id:/memory/usage&buckets=1&start=-5mn&end=-1mn"

        # Loop through nodes
        for item in nodes['items']:
            hawkular_url = "{}{}{}".format(hawkular_url_start, item['metadata']['name'], hawkular_url_end)

            # Disable SSL to work around self signed clusters
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE

            # Call hawkular for the current node
            try:
                request = urllib2.Request(hawkular_url, headers=headers)
                resp = urllib2.build_opener(urllib2.HTTPSHandler(context=ctx)).open(request)
                res = yaml.load(resp.read())
                if res[0]['empty']:
                    if self.args.verbose:
                        print "WARN - Node not reporting metrics: %s" % item['metadata']['name']
                    result_report['failed_nodes'].append({'node': item['metadata']['name'],
                                                          'labels': item['metadata']['labels'],
                                                          'reason': 'Node not reporting metrics'})
                    result_report['success'] = 0

            except urllib2.URLError as e:
                if self.args.verbose:
                    print "ERROR - Failed to query hawkular - %s" % e
                result_report['failed_nodes'].append({'node': item['metadata']['name'],
                                                      'labels': item['metadata']['labels'],
                                                      'reason': "ERROR - Failed to query hawkular - %s" % e})
                result_report['success'] = 0

        return result_report

    def report_to_zabbix(self, pods_status, node_health):
        ''' Report all of our findings to zabbix '''
        discovery_key_metrics = 'openshift.metrics.hawkular'
        item_prototype_macro_metrics = '#OSO_METRICS'
        item_prototype_key_status = 'openshift.metrics.hawkular.status'
        item_prototype_key_starttime = 'openshift.metrics.hawkular.starttime'
        item_prototype_key_restarts = 'openshift.metrics.hawkular.restarts'

        self.metric_sender.add_dynamic_metric(discovery_key_metrics,
                                              item_prototype_macro_metrics,
                                              pods_status.keys())

        for pod, data in pods_status.iteritems():
            if self.args.verbose:
                for key, val in data.items():
                    print
                    print "%s: Key[%s] Value[%s]" % (pod, key, val)

            self.metric_sender.add_metric({
                "%s[%s]" %(item_prototype_key_status, pod) : data['status'],
                "%s[%s]" %(item_prototype_key_starttime, pod) : data['starttime'],
                "%s[%s]" %(item_prototype_key_restarts, pod) : data['restarts']})

        self.metric_sender.add_metric({'openshift.metrics.nodes_reporting': node_health})
        self.metric_sender.send_metrics()

    def persist_details(self, metrics_report):
        ''' Save all failure report context into the first cassandra pod PV'''

        if not os.path.exists(local_report_details_dir):
            os.makedirs(local_report_details_dir)

        file_report_name = 'failure_context_{:%Y.%m.%d_%H-%M-%S-%f}.yaml'.format(datetime.now())

        with open(os.path.join(local_report_details_dir, file_report_name), 'w+') as fp:
            yaml.dump(metrics_report, fp, default_flow_style=False)

        # Get first cassandra pod (cassandra-1)
        pods = self.oc.get_pods()

        cassandra_1_pod = None

        for pod in pods['items']:
            if pod['metadata']['name'].startswith('hawkular-cassandra-1'):
                cassandra_1_pod = pod
                break

        if cassandra_1_pod is None:
            logger.warn("Cannot found cassandra-1 pod, the results cannot be persisted")
            return

        cassandra_main_pod_name = cassandra_1_pod['metadata']['name']

        # Find cassandra PV mount point
        remote_details_directory = None
        cassandra_volume_mounts = cassandra_1_pod['spec']['containers'][0]['volumeMounts']
        for mounts in cassandra_volume_mounts:
            if mounts['name'] == 'cassandra-data':
                remote_details_directory = mounts['mountPath']

        if remote_details_directory is None:
            logger.warn("Cannot found cassandra-1 PV, the results cannot be persisted")

        remote_details_directory = os.path.join(remote_details_directory, 'failure_reports/')

        # Make sure directory exists or create if not exists.
        try:
            self.oc.run_user_cmd('exec {} -- mkdir -p {}'.format(cassandra_main_pod_name, remote_details_directory))
        except subprocess.CalledProcessError:
            logger.error("Cannot create reports directory inside cassandra PV")

        # Trim files, this delete old files and make sure that only have certain number of files
        # this is to prevent fill up the PV.

        # First, sync with PV
        try:
            self.oc.run_user_cmd("rsync  --no-perms=true {}:{} {} ".format(cassandra_main_pod_name,
                                                                           remote_details_directory,
                                                                           local_report_details_dir))
        except subprocess.CalledProcessError:
            logger.warn("Cannot  sync cassandra-1 with local volume, probably the PV doesn't have reports.")

        report_local_files = os.listdir(local_report_details_dir)
        time_sorted_list = sorted([os.path.join(local_report_details_dir, f) for f in report_local_files],
                                  key=os.path.getmtime)

        if len(report_local_files) > max_details_report_files:
            for old_file in time_sorted_list[:-max_details_report_files]:
                os.unlink(old_file)

        try:
            self.oc.run_user_cmd("rsync --delete=true  --no-perms=true {} {}:{}".format(local_report_details_dir,
                                                                                        cassandra_main_pod_name,
                                                                                        remote_details_directory))
        except subprocess.CalledProcessError:
            logger.error("Error trying to sync local volume and cassandra-1")



    def run(self):
        ''' Main function that runs the check '''
        self.parse_args()
        self.metric_sender = MetricSender(verbose=self.args.verbose, debug=self.args.debug)

        self.oc = OCUtil(namespace='openshift-infra', config_file='/tmp/admin.kubeconfig', verbose=self.args.verbose)

        pod_report = self.check_pods()
        self.get_hawkular_creds()
        metrics_report = self.check_node_metrics()
        # if metrics_report = 0, we need this check run again
        if metrics_report['success'] == 0:
            # sleep for 5 seconds, then run the second time node check
            logger.info("The first time metrics check failed, 5 seconds later will start a second time check")
            time.sleep(commandDelay)
            logger.info("starting the second time metrics check")
            metrics_report = self.check_node_metrics()
            # persist second attempt if fails
            if metrics_report['success'] == 0:
                self.persist_details(metrics_report)
        self.report_to_zabbix(pod_report, metrics_report['success'])

if __name__ == '__main__':
    OSMS = OpenshiftMetricsStatus()
    OSMS.run()
