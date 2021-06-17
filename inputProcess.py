# Stratosphere Linux IPS. A machine-learning Intrusion Detection System
# Copyright (C) 2021 Sebastian Garcia

# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301, USA.
# Contact: eldraco@gmail.com, sebastian.garcia@agents.fel.cvut.cz, stratosphere@aic.fel.cvut.cz

import multiprocessing
import sys
import os
from datetime import datetime
from watchdog.observers import Observer
from filemonitor import FileEventHandler
from slips_files.core.database import __database__
import configparser
import time
import json
import traceback
import subprocess
import pwd
import platform

# Input Process
class InputProcess(multiprocessing.Process):
    """ A class process to run the process of the flows """
    def __init__(self, outputqueue, profilerqueue, input_type,
                 input_information, config, packet_filter, zeek_or_bro):
        multiprocessing.Process.__init__(self)
        self.outputqueue = outputqueue
        self.profilerqueue = profilerqueue
        self.config = config
        # Start the DB
        __database__.start(self.config)
        self.input_type = input_type
        self.given_path = input_information
        self.zeek_folder = './zeek_files'
        self.name = 'input'
        self.zeek_or_bro = zeek_or_bro
        self.read_lines_delay = 0
        # Read the configuration
        self.read_configuration()
        # If we were given something from command line, has preference
        # over the configuration file
        if packet_filter:
            self.packet_filter = "'" + packet_filter + "'"
        self.event_handler = None
        self.event_observer = None
        # number of lines read
        self.lines = 0
        self.drop_privileges()

    def drop_root_privs(self):
        """ Drop root privileges if the module doesn't need them. """

        if platform.system() != 'Linux':
            return
        try:
            # Get the uid/gid of the user that launched sudo
            sudo_uid = int(os.getenv("SUDO_UID"))
            sudo_gid = int(os.getenv("SUDO_GID"))
        except TypeError:
            # env variables are not set, you're not root
            return
        # Change the current process’s real and effective uids and gids to that user
        # -1 means value is not changed.
        os.setresgid(sudo_gid, sudo_gid, -1)
        os.setresuid(sudo_uid, sudo_uid, -1)
        return

    def read_configuration(self):
        """ Read the configuration file for what we need """
        # Get the pcap filter
        try:
            self.packet_filter = self.config.get('parameters', 'pcapfilter')
        except (configparser.NoOptionError, configparser.NoSectionError, NameError):
            # There is a conf, but there is no option, or no section or no configuration file specified
            self.packet_filter = 'ip or not ip'
        # Get tcp inactivity timeout
        try:
            self.tcp_inactivity_timeout = self.config.get('parameters', 'tcp_inactivity_timeout')
        except (configparser.NoOptionError, configparser.NoSectionError, NameError):
            # There is a conf, but there is no option, or no section or no configuration file specified
            self.tcp_inactivity_timeout = ''

    def print(self, text, verbose=1, debug=0):
        """
        Function to use to print text using the outputqueue of slips.
        Slips then decides how, when and where to print this text by taking all the prcocesses into account

        Input
         verbose: is the minimum verbosity level required for this text to be printed
         debug: is the minimum debugging level required for this text to be printed
         text: text to print. Can include format like 'Test {}'.format('here')

        If not specified, the minimum verbosity level required is 1, and the minimum debugging level is 0
        """

        # self.name = f'{Fore.YELLOW}{self.name}{Style.RESET_ALL}'
        vd_text = str(int(verbose) * 10 + int(debug))
        self.outputqueue.put(vd_text + '|' + self.name + '|[' + self.name + '] ' + str(text))

    def stop_queues(self):
        """ Stops the profiler and output queues """

        self.profilerqueue.put("stop")
        self.outputqueue.put("02|input|[In] No more input. Stopping input process. Sent {} lines ({}).".format(self.lines, datetime.now().strftime('%Y-%m-%d--%H:%M:%S')))
        self.outputqueue.close()
        self.profilerqueue.close()

    def read_nfdump_output(self) -> int:
        """
        A binary file generated by nfcapd can be read by nfdump.
        The task for this function is to send nfdump output line by line to profilerProcess.py for processing
        """

        line = {'type': 'nfdump'}
        if not self.nfdump_output:
            # The nfdump command returned nothing
            self.print("Error reading nfdump output ", 1, 3)
        else:
            lines = len(self.nfdump_output.splitlines())
            for nfdump_line in self.nfdump_output.splitlines():
                # this line is taken from stdout we need to remove whitespaces
                nfdump_line.replace(' ','')
                ts = nfdump_line.split(',')[0]
                if not ts[0].isdigit():
                    # The first letter is not digit -> not valid line.
                    # TODO: What is this valid line check?? explain
                    continue
                line['data']  = nfdump_line
                self.profilerqueue.put(line)

        return lines

    def read_zeek_files(self) -> int:
        # Get the zeek files in the folder now
        zeek_files = __database__.get_all_zeek_file()
        open_file_handlers = {}
        file_time = {}
        cache_lines = {}
        # Try to keep track of when was the last update so we stop this reading
        last_updated_file_time = datetime.now()
        lines = 0
        while True:
            # Go to all the files generated by Zeek and read them
            for filename in zeek_files:
                # Update which files we know about
                try:
                    file_handler = open_file_handlers[filename]
                    # We already opened this file
                    # self.print(f'Old File found {filename}', 0, 6)
                except KeyError:
                    # First time openning this file.
                    # Ignore the files that do not contain data. These are the zeek log files that we don't use
                    should_be_ignored = False
                    ignored_files = ['capture_loss','loaded_scripts','packet_filter', 'stats', 'weird', 'reporter']
                    for file in ignored_files:
                        if file in filename:
                            should_be_ignored = True
                            break
                    if should_be_ignored: continue
                    file_handler = open(filename + '.log', 'r')
                    open_file_handlers[filename] = file_handler
                    # self.print(f'New File found {filename}', 0, 6)

                # Only read the next line if the previous line was sent
                try:
                    _ = cache_lines[filename]
                    # We have still something to send, do not read the next line from this file
                except KeyError:
                    # We don't have any waiting line for this file, so proceed
                    zeek_line = file_handler.readline()
                    # self.print(f'Reading from file {filename}, the line {zeek_line}', 0, 6)
                    # Did the file end?
                    if not zeek_line:
                        # We reached the end of one of the files that we were reading. Wait for more data to come
                        continue

                    # Since we actually read something form any file, update the last time of read
                    last_updated_file_time = datetime.now()
                    try:
                        # Convert from json to dict
                        nline = json.loads(zeek_line)
                        line = {}
                        # All bro files have a field 'ts' with the timestamp.
                        # So we are safe here not checking the type of line
                        try:
                            timestamp = nline['ts']
                        except KeyError:
                            # In some Zeek files there may not be a ts field
                            # Like in some weird smb files
                            timestamp = 0
                        # Add the type of file to the dict so later we know how to parse it
                        line['type'] = filename
                        line['data'] = nline
                    except json.decoder.JSONDecodeError:
                        # It is not JSON format. It is tab format line.
                        nline = zeek_line
                        # Ignore comments at the beginning of the file.
                        try:
                            if nline[0] == '#':
                                continue
                        except IndexError:
                            continue
                        line = {}
                        line['type'] = filename
                        line['data'] = nline
                        # Get timestamp
                        timestamp = nline.split('\t')[0]

                    try:
                        float(timestamp)
                        file_time[filename] = timestamp
                        # self.print(f'File {filename}. TS: {timestamp}')
                        # Store the line in the cache
                        # self.print(f'Adding cache and time of {filename}')
                        cache_lines[filename] = line
                    except ValueError:
                        # this ts doesnt repr a float value, ignore it
                        pass

            ################
            # Out of the for that check each Zeek file one by one
            # self.print('Out of the for.')
            # self.print('Cached lines: {}'.format(str(cache_lines)))

            # If we don't have any cached lines to send, it may mean that new lines are not arriving. Check
            if not cache_lines:
                # Verify that we didn't have any new lines in the last 10 seconds. Seems enough for any network to have ANY traffic
                now = datetime.now()
                diff = now - last_updated_file_time
                diff = diff.seconds
                if diff >= self.bro_timeout:
                    # It has been 10 seconds without any file being updated. So stop the while
                    # Get out of the while and stop Zeek
                    break

            # Now read lines in order. The line with the smallest timestamp first
            files_sorted_by_ts = sorted(file_time, key=file_time.get)
            # self.print('Sorted times: {}'.format(str(files_sorted_by_ts)))
            try:
                # get the file that has the earliest flow
                file_with_earliest_flow = files_sorted_by_ts[0]
            except IndexError:
                # No more sorted keys. Just loop waiting for more lines
                # It may happened that we check all the files in the folder, and there is still no file for us.
                # To cover this case, just refresh the list of files
                # self.print('Getting new files...')
                # print(cache_lines)
                zeek_files = __database__.get_all_zeek_file()
                time.sleep(1)
                continue

            # to fix the problem of evidence being generated BEFORE their corresponding flows are added to our db
            # make sure we send the lines of conn.log before the lines of any other file
            for key in cache_lines:
                if 'conn' in key:
                    file_with_earliest_flow = key
                    break
            # comes here if we're done with all conn.log flows and it's time to process other files
            line_to_send = cache_lines[file_with_earliest_flow]

            # self.print('Line to send from file {}. {}'.format(file_with_earliest_flow, line_to_send))
            # SENT
            self.print("	> Sent Line: {}".format(line_to_send), 0, 3)
            self.profilerqueue.put(line_to_send)
            # Count the read lines
            lines += 1
            # Delete this line from the cache and the time list
            # self.print('Deleting cache and time of {}'.format(earliest_flow))
            del cache_lines[file_with_earliest_flow]
            del file_time[file_with_earliest_flow]

            # Get the new list of files. Since new files may have been created by Zeek while we were processing them.
            zeek_files = __database__.get_all_zeek_file()

        ################
        # Out of the while

        # We reach here after the break produced if no zeek files are being updated.
        # No more files to read. Close the files
        for file, handle in open_file_handlers.items():
            self.print('Closing file {}'.format(file), 3, 0)
            handle.close()
        return lines

    def read_zeek_folder(self):
        # This is the case that a folder full of zeek files is passed with -f. Read them all
        for file in os.listdir(self.given_path):
            # Remove .log extension and add file name to database.
            extension = file[-4:]
            if extension == '.log':
                # Add log file to database
                file_name_without_extension = file[:-4]
                __database__.add_zeek_file(self.given_path + '/' + file_name_without_extension)

        # We want to stop bro if no new line is coming.
        self.bro_timeout = 1
        lines = self.read_zeek_files()
        self.print("We read everything from the folder. No more input. Stopping input process. Sent {} lines".format(lines))
        self.stop_queues()
        return True

    def read_from_stdin(self):
        self.print('Receiving flows from the stdin.', 3, 0)
        # By default read the stdin
        sys.stdin.close()
        sys.stdin = os.fdopen(0, 'r')
        file_stream = sys.stdin
        line = {'type': 'stdin'}
        for t_line in file_stream:
            line['data'] = t_line
            self.print(f'	> Sent Line: {t_line}', 0, 3)
            self.profilerqueue.put(line)
            self.lines += 1
        self.stop_queues()
        return True

    def handle_binetflow(self):
        self.lines =0
        self.read_lines_delay = 0.02
        with open(self.given_path) as file_stream:
            line = {'type': 'argus'}
            # fake = {'type': 'argus', 'data': 'StartTime,Dur,Proto,SrcAddr,Sport,Dir,DstAddr,Dport,State,sTos,dTos,TotPkts,TotBytes,SrcBytes,SrcPkts,Label\n'}
            # self.profilerqueue.put(fake)

            # read first line to determine the type of line, tab or comma separated
            t_line = file_stream.readline()
            if '\t' in t_line:
                # this is the header line
                line['type'] = 'argus-tabs'
            line['data'] = t_line
            self.profilerqueue.put(line)
            self.lines += 1

            # go through the rst of the file
            for t_line in file_stream:
                time.sleep(self.read_lines_delay)
                line['data'] = t_line
                # argus files are either tab separated orr comma separated
                if len(t_line.strip()) != 0:
                    self.profilerqueue.put(line)
                self.lines += 1
        self.stop_queues()
        return True

    def handle_suricata(self):
        with open(self.given_path) as file_stream:
            line = {'type': 'suricata'}
            self.read_lines_delay = 0.02
            for t_line in file_stream:
                time.sleep(self.read_lines_delay)
                line['data'] = t_line
                self.print(f'	> Sent Line: {line}', 0, 3)
                if len(t_line.strip()) != 0:
                    self.profilerqueue.put(line)
                self.lines += 1
        self.stop_queues()
        return True

    def handle_zeek_log_file(self):
        try:
            file_name_without_extension = self.given_path[:self.given_path.index('.')]
        except IndexError:
            # filename doesn't have an extension, probably not a conn.log
            return False
        # Add log file to database
        __database__.add_zeek_file(file_name_without_extension)
        self.bro_timeout = 1
        self.lines = self.read_zeek_files()
        self.stop_queues()
        return True

    def handle_nfdump(self):
        command = 'nfdump -b -N -o csv -q -r ' + self.given_path
        # Execute command
        result = subprocess.run(command.split(), stdout=subprocess.PIPE)
        # Get command output
        self.nfdump_output = result.stdout.decode('utf-8')
        self.lines = self.read_nfdump_output()
        self.print("We read everything. No more input. Stopping input process. Sent {} lines".format(self.lines))
        return True

    def handle_pcap_and_interface(self) -> int:
        """ Returns the number of zeek lines read """
        # Create zeek_folder if does not exist.
        if not os.path.exists(self.zeek_folder):
            os.makedirs(self.zeek_folder)
        # Now start the observer of new files. We need the observer because Zeek does not create all the files
        # at once, but when the traffic appears. That means that we need
        # some process to tell us which files to read in real time when they appear
        # Get the file eventhandler
        # We have to set event_handler and event_observer before running zeek.
        self.event_handler = FileEventHandler(self.config)
        # Create an observer
        self.event_observer = Observer()
        # Schedule the observer with the callback on the file handler
        self.event_observer.schedule(self.event_handler, '.', recursive=True)
        # Start the observer
        self.event_observer.start()

        # This double if is horrible but we just need to change a string
        if self.input_type is 'interface':
            # Change the bro command
            bro_parameter = '-i ' + self.given_path
            # We don't want to stop bro if we read from an interface
            self.bro_timeout = 9999999999999999
        elif self.input_type is 'pcap':
            # Find if the pcap file name was absolute or relative
            if self.given_path[0] == '/':
                # If absolute, do nothing
                bro_parameter = '-r "' + self.given_path + '"'
            else:
                # If relative, add ../ since we will move into a special folder
                bro_parameter = '-r "../' + self.given_path + '"'
            # This is for stoping the input if bro does not receive any new line while reading a pcap
            self.bro_timeout = 30

        if len(os.listdir(self.zeek_folder)) > 0:
            # First clear the zeek folder of old .log files
            # The rm should not be in background because we must wait until the folder is empty
            command = "rm " + self.zeek_folder + "/*.log > /dev/null  2>&1"
            os.system(command)

        # Run zeek on the pcap or interface. The redef is to have json files
        zeek_scripts_dir = os.getcwd() + '/zeek-scripts'
        command = f'cd {self.zeek_folder}; {self.zeek_or_bro} -C {bro_parameter} {self.tcp_inactivity_timeout} local -f {self.packet_filter} {zeek_scripts_dir} 2>&1 > /dev/null &'
        self.print(f'Zeek command: {command}', 3, 0)
        # Run zeek.
        os.system(command)

        # Give Zeek some time to generate at least 1 file.
        time.sleep(3)

        lines = self.read_zeek_files()
        self.print("We read everything. No more input. Stopping input process. Sent {} lines".format(lines))

        # Stop the observer
        try:
            self.event_observer.stop()
            self.event_observer.join()
        except AttributeError:
            # In the case of nfdump, there is no observer
            pass
        return True

    def run(self):
        try:
            # Process the file that was given
            # If the type of file is 'file (-f) and the name of the file is '-' then read from stdin
            if not self.given_path or self.given_path is '-':
                self.read_from_stdin()
            elif self.input_type is 'zeek_folder':
                # is a zeek folder
                self.read_zeek_folder()
            elif self.input_type is 'zeek_log_file':
                # Is a zeek.log file
                file_name = self.given_path.split('/')[-1]
                if 'log' in file_name:
                    self.handle_zeek_log_file()
                else:
                    return False
            elif self.input_type is 'nfdump':
                # binary nfdump file
                self.handle_nfdump()
            elif self.input_type is 'binetflow' or 'binetflow-tabs' in self.input_type:
                # argus or binetflow
                self.handle_binetflow()
            elif (self.input_type is 'pcap'
                  or self.input_type is 'interface'):
                self.handle_pcap_and_interface()
            elif self.input_type is 'suricata':
                self.handle_suricata()
            else:
                # if self.input_type is 'file':
                # default value
                self.print(f'Unrecognized file type "{self.input_type}". Stopping.')
                return False

        except KeyboardInterrupt:
            self.outputqueue.put("04|input|[In] No more input. Stopping input process. Sent {} lines".format(self.lines))
            try:
                self.event_observer.stop()
                self.event_observer.join()
            except AttributeError:
                # In the case of nfdump, there is no observer
                pass
            except NameError:
                pass
            return True
        except Exception as inst:
            exception_line = sys.exc_info()[2].tb_lineno
            self.print(f"Problem with Input Process. line {exception_line}", 0, 1)
            self.print("Stopping input process. Sent {} lines".format(self.lines), 0, 1)
            self.print(type(inst), 0, 1)
            self.print(inst.args, 0, 1)
            self.print(inst, 0, 1)
            try:
                self.event_observer.stop()
                self.event_observer.join()
            except AttributeError:
                # In the case of nfdump, there is no observer
                pass
            self.print(traceback.format_exc())
            sys.exit(1)
