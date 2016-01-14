# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
# Copyright (c) 2014 Mozilla Corporation
#
# Contributors:
# Michal Purzynski <mpurzynski@mozilla.com>
#
# Query the basic hardware parameters and send events to DataDog.
# The error handling has been designed so the script keeps running, and does not terminate if a single query failed.
# If that's the case, a single check will not send data and you should set your monitoring to alert you on that.
#

import time
import requests
from checks import AgentCheck
from hashlib import md5
import sys, os
import re
from subprocess import PIPE,Popen


class HPHardwareCheck(AgentCheck):

    DEFAULT_MIN_COLLECTION_INTERVAL = 900  # in seconds

    def hpacucli_query(self, query, shell):
        cmd = ""
        output = ""
        proc = ""
        safe_query = "ERROR IN COMMAND"
        r1 = re.compile("^show [\w\d=]+$")
    # FIXME
    #    r2 = re.compile("^ctrl [\w\d\s=]+ show [\w\d=]{0,10}$")
        r2 = re.compile("^ctrl")

        if r1 is not None and r2 is not None:
            if r1.search(query):
                cmd = "/sbin/hpasmcli"
                safe_query = query
                self.log.info("Sending command {0} to hpasmcli\n".format(safe_query))
            elif r2.search(query):
                cmd = "/usr/sbin/hpacucli"
                safe_query = query
                self.log.info("Sending command {0} to hpacucli\n".format(safe_query))
            else:
                self.log.error("Command {0} is not valid\n".format(query))

        try:
            if cmd is not None:
                if shell is "noshell":
                    proc = Popen(["sudo", "-u", "root", cmd], stdin=PIPE, stdout=PIPE, stderr=PIPE, shell=False)
                elif shell is "withshell":
                    proc = Popen(["sudo -u root " + cmd + " -s " + '"' + query + '"'], stdin=PIPE, stdout=PIPE, stderr=PIPE, shell=True)
        except OSError as e:
            self.log.error("Failed when starting {0}: {1}\n".format(cmd, e))
        else:
            if proc is not None:
                proc.stdin.write("\n{0}\n".format(safe_query))
                proc.stdin.write("quit\n")
                lines = proc.communicate()
                if lines is not None:
                    for line in lines:
                        if "ERROR" in line.strip():
                            self.log.error("hpasmcli returned error: {0}\n".format(line))
                        else:
                            output = lines
        return(output[0])

    def show_dimm(self, raw_status):
    #
    # hpasmcli -s "show dimm"
    #
    # DIMM Configuration
    # ------------------
    # Processor #:                     1
    # Module #:                     1
    # Status:                       Ok
    #
    # Processor #:                     1
    # Module #:                     4
    # Status:                       Ok
    #
        dimm_status = {}
        dimm_list = []
        if raw_status is not None:
            for line in raw_status.split('\n'):
                new_line = line.split()
                if new_line is not None:
                    if "Processor" in new_line and "#:" in new_line:
                        # new chunk - add the previous one to the list and create a new dict
                        dimm_list.append(dimm_status)
                        dimm_status = {}
                        dimm_status["Processor"] = new_line[2]
                    if "Module" in new_line and "#:" in new_line:
                        dimm_status["Module"] = new_line[2]
                    if "Status:" in new_line:
                        dimm_status["Status"] = new_line[1].lower()
            # add the last dict
            dimm_list.append(dimm_status)
        return(dimm_list)

    def show_smartarray_pd(self, raw_status):
    #
    # hpacucli "ctrl slot=0 pd all show"
    #
    #      physicaldrive 1I:1:3 (port 1I:box 1:bay 3, SATA, 1 TB, OK)
    #      physicaldrive 1I:1:4 (port 1I:box 1:bay 4, SATA, 1 TB, Predictive Failure)
    #
        pd_status = {}
        pd_list = []
        if raw_status is not None:
            for line in raw_status.split('\n'):
                if line is not None:
                    if "physicaldrive" in line:
                        pd_list.append(pd_status)
                        pd_status = {}
                        new_line = line.split()
                        if new_line is not None:
                            pd_status = {
                                "Drive": new_line[1],
                                "Status": new_line[9][0:2].lower(),
                                "Capacity": new_line[7] + new_line[8],
                            }
            pd_list.append(pd_status)
        return(pd_list)

    def show_smartarray_ld(self, raw_status):
    #
    # hpssacli ctrl slot=0 ld all show
    #
    # logicaldrive 1 (6.4 TB, 5): OK
    #
        ld_status = {}
        ld_list = []
        if raw_status is not None:
            for line in raw_status.split('\n'):
                if line is not None:
                    if "logicaldrive" in line:
                        ld_list.append(ld_status)
                        ld_status = {}
                        new_line = line.split()
                        if new_line is not None:
                            ld_status = {
                                "Drive": new_line[1],
                                "Status": new_line[6][0:2].lower(),
                                "Capacity": new_line[2][1:] + new_line[3],
                            }
            ld_list.append(ld_status)
        return(ld_list)

    def show_smartarray_controller(self, raw_status):
    # hpssacli ctrl all show status
    #
    # Smart Array P420i in Slot 0 (Embedded)
    #   Controller Status: OK
    #   Cache Status: OK
    #   Battery/Capacitor Status: OK
    #
        ctrl_status = {}
        ctrl_list = []
        if raw_status is not None:
            for line in raw_status.split('\n'):
                if line is not None:
                    new_line = line.split()
                    if new_line is not None:
                        if "Slot" in line:
                            ctrl_list.append(ctrl_status)
                            ctrl_status = {}
                            # Let's use the full name as a key
                            ctrl_status['Name'] = " ".join(new_line[0:]).strip()
                        if "Controller" in line and "Status" in line:
                            ctrl_status['Controller'] = new_line[2].lower()
                        if "Cache" in line:
                            ctrl_status['Cache'] = new_line[2].lower()
                        if "Battery" in line:
                            ctrl_status['Battery'] = new_line[2].lower()
            ctrl_list.append(ctrl_status)
        return(ctrl_list)

    def show_iml(self, raw_status):
    #
    # hpasmcli -s "show iml"
    #
    # Event: 75 Added: 12/21/2015 14:03
    # CAUTION: POST Messages - POST Error: 1719-A controller failure event occurred prior to this power-up.
    #
        iml_msg = {}
        iml_list = []
        if raw_status is not None:
            for line in raw_status.split('\n'):
                if len(line.strip()) > 0:
                    if "Event" in line:
                        iml_list.append(iml_msg)
                        iml_msg = {}
                        new_line = line.split()
                        iml_msg = {
                            "ID": new_line[0] + new_line[1],
                            "datetime": new_line[3] + " " + new_line[4],
                        }
                    else:
                        if len(line) > 0:
                            iml_msg["msg"] = line.strip()
            iml_list.append(iml_msg)
        return(iml_list)

    def show_psu(self, raw_status):
    #
    # hpasmcli -s "show powersupply"
    #
    # Power supply #1
    #    Present  : Yes
    #    Redundant: Yes
    #    Condition: Ok
    #    Hotplug  : Supported
    #    Power    : 75 Watts
    # Power supply #2
    #    Present  : Yes
    #    Redundant: Yes
    #    Condition: Ok
    #    Hotplug  : Supported
    #    Power    : 95 Watts
    #
        psu_status = {}
        psu_list = []
        if raw_status is not None:
            for line in raw_status.split("\n"):
                new_line = line.split()
                if new_line is not None:
                    if "supply" in new_line:
                        # new chunk - add the previous one to the list and create a new dict
                        psu_list.append(psu_status)
                        psu_status = {}
                        psu_status["PSU"] = new_line[2][1:]
                    if "Present" in new_line:
                        psu_status["Present"] = new_line[2].lower()
                    if "Redundant:" in new_line:
                        psu_status["Redundant"] = new_line[1].lower()
                    if "Condition:" in new_line:
                        psu_status["Condition"] = new_line[1].lower()
            # add the list dict
            psu_list.append(psu_status)
        return(psu_list)

    def show_fans(self, raw_status):
    # hpasmcli -s "show fans"
    #
    # #8   SYSTEM          Yes     NORMAL  33%     Yes        0        Yes           
    #
        fans_status = {}
        fans_list = []
        if raw_status is not None:
            for line in raw_status.split("\n"):
                if "SYSTEM" in line:
                    fans_list.append(fans_status)
                    new_line = line.split()
                    fans_status = {
                        "Fan": new_line[0][1:],
                        "Present": new_line[2],
                        "Speed": new_line[3].lower(),
                        "Redundant": new_line[5]
                    }

            fans_list.append(fans_status)
        return(fans_list)


    def show_server(self, raw_status):
    #
    # hpasmcli -s "show server"
    #
    # Processor: 0
    #    Status       : Ok
    #
    # Processor: 1
    #    Status       : Ok
    #

        cpu_status = {}
        cpu_list = []
        if raw_status is not None:
            for line in raw_status.split("\n"):
                new_line = line.split()
                if new_line is not None:
                    if "Processor:" in new_line and len(new_line) < 3:
                        # new chunk - add the previous one to the list and create a new dict
                        cpu_list.append(cpu_status)
                        cpu_status = {}
                        cpu_status['CPU'] = new_line[1]
                    if "Status" in new_line:
                        cpu_status['Status'] = new_line[2].lower()
            # add the list dict
            cpu_list.append(cpu_status)
        return(cpu_list)

    def error_generic_event(self, status, event_type, msg_title, msg_text):

        alert_type = "warning"

        if "ok" not in status:
            alert_type = "error"
        else:
            alert_type = "success"

        if msg_text is None:
            msg_text = " "

        out = self.event({
                "timestamp": int(time.time()),
                "event_type": event_type,
                "alert_type": alert_type,
                "msg_title": msg_title,
                "msg_text": msg_text,
        })

    def check(self, instance):

        # Check the DIMM
        try:
            out = self.hpacucli_query("show dimm", "noshell")
            dimm_status = self.show_dimm(out)

            for chunk in dimm_status:
                if len(chunk) > 1:
                    msg_title = "DIMM status for module {0}:{1}".format(str(chunk["Processor"]), str(chunk["Module"]))
                    self.error_generic_event(chunk["Status"], "DIMM status", msg_title, "")
        except Exception as e:
            self.log.error("{0}\n".format(e))

        # Check the physical disks
        try:
            out = self.hpacucli_query("ctrl slot=0 pd all show", "noshell")
            pd_status = self.show_smartarray_pd(out)

            for chunk in pd_status:
                if len(chunk) > 1:
                    msg_title = "Physical disk status for drive {0} in slot {1}".format(str(chunk['Capacity']), str(chunk['Drive']))
                    self.error_generic_event(chunk["Status"], "Physical disk status", msg_title, "")
        except Exception as e:
            self.log.error("{0}\n".format(e))

        # Check the logical disks
        try:
            out = self.hpacucli_query("ctrl slot=0 ld all show", "noshell")
            ld_status = self.show_smartarray_ld(out)

            for chunk in ld_status:
                if len(chunk) > 1:
                    event_type = "Logical disk status"
                    msg_title = "Logical disk status for drive {0} in slot {1}".format(str(chunk['Capacity']), str(chunk['Drive']))
                    self.error_generic_event(chunk['Status'], "Logical disk status", msg_title, "")
        except Exception as e:
            self.log.error("{0}\n".format(e))

        # Check the smartarray controller status
        try:
            out = self.hpacucli_query("ctrl all show status", "noshell")
            sma_status = self.show_smartarray_controller(out)

            for chunk in sma_status:
                if len(chunk) > 1:
                    if "Name" in chunk:
                        msg_text = chunk['Name']
                    if "Controller" in chunk:
                        self.error_generic_event(chunk["Controller"], "Smartarray controller board status", "Smartarray Controller status", msg_text)
                    if "Battery" in chunk:
                        self.error_generic_event(chunk["Battery"], "Smartarray Battery status", "Smartarray Battery status", msg_text)
                    if "Cache" in chunk:
                        self.error_generic_event(chunk["Cache"], "Smartarray controller battery status", "Smartarray Cache status", msg_text)
        except Exception as e:
            self.log.error("{0}\n".format(e))

        # Send the IML events and clear the hardware log
        try:
            out = self.hpacucli_query("show iml", "withshell")
# FIXME
#            hpacucli_query("clear iml", "withshell")
            iml_status = self.show_iml(out)

            for chunk in iml_status:
                if len(chunk) > 1:
                    msg_text = "IML {0} {1} At {2}".format(chunk["ID"], chunk["msg"], chunk["datetime"])
                    self.error_generic_event("error", "IML Event", "IML Event", msg_text)
        except Exception as e:
            self.log.error("{0}\n".format(e))

        # Check the PSU
        try:
            out = self.hpacucli_query("show powersupply", "noshell")
            psu_status = self.show_psu(out)

            for chunk in psu_status:
                if len(chunk) > 1:
                    status = "error"
                    if "ok" not in chunk["Condition"]:
                        pass
                    elif "yes" not in chunk["Redundant"]:
                        pass
                    elif "yes" not in chunk["Present"]:
                        pass
                    else:
                        status = "ok"
                    msg_title = "PSU {0} status".format(str(chunk["PSU"]))
                    self.error_generic_event(status, "PSU status", msg_title, "")
        except Exception as e:
            self.log.error("{0}\n".format(e))

        # Check the fans
        try:
            out = self.hpacucli_query("show fans", "noshell")
            fans_status = self.show_fans(out)

            for chunk in fans_status:
                if len(chunk) > 1:
                    status = "error"
                    if "normal" not in chunk['Speed']:
                        pass
                    elif "Yes" not in chunk['Present']:
                        pass
                    elif "Yes" not in chunk['Redundant']:
                        pass
                    else:
                        status = "ok"
                    msg_title = 'Fan %s status' % (str(chunk['Fan']))
                    self.error_generic_event(status, "Fan status", msg_title, "")
        except Exception as e:
            self.log.error("{0}\n".format(e))

        # Check the generic server status
        try:
            out = self.hpacucli_query("show server", "noshell")
            cpu_status = self.show_server(out)

            for chunk in cpu_status:
                if len(chunk) > 1:
                    msg_title = 'CPU %s status' % (str(chunk['CPU']))
                    self.error_generic_event(chunk['Status'], "CPU status", msg_title, "")
        except Exception as e:
            self.log.error("{0}\n".format(e))

def main():
    check, instances = HPHardwareCheck.from_yaml('/path/to/conf.d/http.yaml')
    for instance in instances:
        check.check(instance)
        if check.has_events():
            print 'Events: %s' % (check.get_events())

if __name__ == "__main__":
    main()
