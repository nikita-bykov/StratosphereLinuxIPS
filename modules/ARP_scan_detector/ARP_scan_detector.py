# Ths is a template module for you to copy and create your own slips module
# Instructions
# 1. Create a new folder on ./modules with the name of your template. Example:
#    mkdir modules/anomaly_detector
# 2. Copy this template file in that folder.
#    cp modules/template/template.py modules/anomaly_detector/anomaly_detector.py
# 3. Make it a module
#    touch modules/template/__init__.py
# 4. Change the name of the module, description and author in the variables
# 5. The file name of the python module (template.py) MUST be the same as the name of the folder (template)
# 6. The variable 'name' MUST have the public name of this module. This is used to ignore the module
# 7. The name of the class MUST be 'Module', do not change it.

# Must imports
import ipaddress

from slips_files.common.abstracts import Module
import multiprocessing
from slips_files.core.database import __database__
import configparser

# Your imports
import json
import sys
import datetime
import ipaddress


class Module(Module, multiprocessing.Process):
    # Name: short name of the module. Do not use spaces
    name = 'ARPScanDetector'
    description = 'Detect ARP scans'
    authors = ['Alya Gomaa']

    def __init__(self, outputqueue, config):
        multiprocessing.Process.__init__(self)
        # All the printing output should be sent to the outputqueue.
        # The outputqueue is connected to another process called OutputProcess
        self.outputqueue = outputqueue
        # In case you need to read the slips.conf configuration file for
        # your own configurations
        self.config = config
        # Start the DB
        __database__.start(self.config)
        self.pubsub = __database__.r.pubsub()
        self.pubsub.subscribe('new_arp')
        self.pubsub.subscribe('tw_closed')
        self.timeout = None
        self.read_configuration()
        # this dict will categorize arp requests by profileid_twid
        self.cache_arp_requests = {}

    def print(self, text, verbose=1, debug=0):
        """
        Function to use to print text using the outputqueue of slips.
        Slips then decides how, when and where to print this text by taking all the processes into account
        :param verbose:
            0 - don't print
            1 - basic operation/proof of work
            2 - log I/O operations and filenames
            3 - log database/profile/timewindow changes
        :param debug:
            0 - don't print
            1 - print exceptions
            2 - unsupported and unhandled types (cases that may cause errors)
            3 - red warnings that needs examination - developer warnings
        :param text: text to print. Can include format like 'Test {}'.format('here')
        """

        levels = f'{verbose}{debug}'
        self.outputqueue.put(f"{levels}|{self.name}|{text}")

    def read_configuration(self):
        self.home_network = []
        try:
            self.home_network.append(self.config.get('parameters', 'home_network'))
        except (configparser.NoOptionError, configparser.NoSectionError, NameError):
            # There is a conf, but there is no option, or no section or no configuration file specified
            self.home_network = ['192.168.0.0/16', '172.16.0.0/12', '10.0.0.0/8']
            # convert the ranges into network obj

        self.home_network = list(map(ipaddress.ip_network,self.home_network))


    def check_arp_scan(self, profileid, twid, daddr, uid, ts):
        # to test this module run sudo arp-scan  --localnet

        # arp flows don't have uids, the uids received are randomly generated by slips
        try:
            # cached_requests is a list, append this request to it
            # if ip x sends arp requests to 3 or more different ips within 30 seconds, then this is x doing arp scan
            # the key f'{profileid}_{twid} is used to group requests from the same saddr
            #  the dict looks something like this {profileid_twid1: {'daddr': {'uid':..,'ts' : ..'}, daddr2: {uid,ts}}

            cached_requests = self.cache_arp_requests[f'{profileid}_{twid}']
            cached_requests.update({daddr: {'uid' : uid,
                                    'ts' : ts}})
        except KeyError:
            # create the key if it doesn't exist
            self.cache_arp_requests[f'{profileid}_{twid}'] = {daddr: {'uid' : uid,
                                                                'ts' : ts}}
            return True

        # get the keys of cache_arp_requests in a list
        profileids_twids = list(cached_requests.keys())
        if len(profileids_twids) >=3:
            # check if these requests happened within 30 secs
            # get the first and the last request of the 10
            first_daddr = profileids_twids[0]
            last_daddr = profileids_twids[-1]
            starttime = cached_requests[first_daddr]['ts']
            endtime = cached_requests[last_daddr]['ts']
            # get the time of each one in secondstodo do we need mac addresses?
            starttime = datetime.datetime.fromtimestamp(starttime)
            endtime = datetime.datetime.fromtimestamp(endtime)
            # get the difference between them in seconds
            self.diff = float(str(endtime - starttime).split(':')[-1])
            if self.diff <= 30.00:
                # we are sure this is an arp scan
                confidence = 0.8
                threat_level = 60
                description = f'performing ARP scan'
                type_evidence = 'ARPScan'
                type_detection = 'ip' #srcip
                detection_info = profileid.split("_")[1]
                __database__.setEvidence(type_detection, detection_info, type_evidence,
                                     threat_level, confidence, description, ts, profileid=profileid, twid=twid, uid=uid)
                # after we set evidence, clear the dict so we can detect if it does another scan
                self.cache_arp_requests.pop(f'{profileid}_{twid}')
                return True
        return False

    def check_dstip_outside_localnet(self, profileid, twid, daddr, uid, saddr, ts):
        """ Function to setEvidence when daddr is outside the local network """

        if '0.0.0.0' in saddr or '0.0.0.0' in daddr:
            # this is the case of ARP probe, not an arp outside of local network, don't alert
            return False

        daddr_as_obj = ipaddress.IPv4Address(daddr)
        for network in self.home_network:
            if daddr_as_obj in network:
                # IP is in this local network , don't alert
                return False

        # comes here if the IP isn't in any of the local networks
        confidence = 0.8
        threat_level = 50
        description = f'sending ARP packet to a destination address outside of local network: {daddr}'
        type_evidence = 'ARPScan'
        type_detection = 'ip' #srcip
        detection_info = profileid.split("_")[1]
        __database__.setEvidence(type_detection, detection_info, type_evidence,
                             threat_level, confidence, description, ts, profileid=profileid, twid=twid, uid=uid)

    def run(self):
        # Main loop function
        while True:
            try:
                message = self.pubsub.get_message(timeout=None)
                if message and message['data'] == 'stop_process':
                    # Confirm that the module is done processing
                    __database__.publish('finished_modules', self.name)
                    return True

                if message and message['channel'] == 'new_arp' and type(message['data'])==str:
                    flow = json.loads(message['data'])
                    ts = flow['ts']
                    profileid = flow['profileid']
                    twid = flow['twid']
                    daddr = flow['daddr']
                    saddr = flow['saddr']
                    # arp flows don't have uids, the uids received are randomly generated by slips
                    uid = flow['uid']
                    self.check_arp_scan(profileid, twid, daddr, uid, ts)
                    self.check_dstip_outside_localnet(profileid, twid, daddr, uid, saddr, ts)

                # if the tw is closed, remove all its entries from the cache dict
                if message and message['channel'] == 'tw_closed' and type(message['data'])==str:
                    profileid_tw = message['data']
                    # when a tw is closed, this means that it's too old so we don't check for arp scan in this time range anymore
                    # this copy is made to avoid dictionary changed size during iteration err
                    cache_copy = self.cache_arp_requests.copy()
                    for key in cache_copy:
                        if profileid_tw in key:
                            self.cache_arp_requests.pop(key)
                            # don't break , keep looking for more keys that belong to the same tw
            except KeyboardInterrupt:
                # On KeyboardInterrupt, slips.py sends a stop_process msg to all modules, so continue to receive it
                continue
            except Exception as inst:
                exception_line = sys.exc_info()[2].tb_lineno
                self.print(f'Problem on the run() line {exception_line}', 0, 1)
                self.print(str(type(inst)), 0, 1)
                self.print(str(inst.args), 0, 1)
                self.print(str(inst), 0, 1)
                return True
