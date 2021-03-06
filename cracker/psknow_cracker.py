#!/usr/bin/python3

import os
import sys
import inspect
import signal
import stat
import traceback


from time import sleep
from tempfile import mkstemp
from base64 import b64decode
from datetime import datetime

from config import Configuration
from process import SingleProcess, DoubleProcess
from scrambler import Scrambler
from requester import Requester
from comunicator import Comunicator


def die(condition, message):
    if condition:
        msg = "line %s in '%s': '%s'" % \
                 (inspect.currentframe().f_back.f_lineno, inspect.stack()[1][3], message)
        Configuration.dual_print(Configuration.logger.critical, msg)
        sys.exit(-1)


slow_stop_flag = False


def slow_stop(signum, _):
    global slow_stop_flag

    Configuration.logger.info("Received %s signal. Slow stopping!" % signum)
    slow_stop_flag = True


def fast_stop():
    if Cracker.crt_process is not None:
        Configuration.logger.info("Killing running process %s" % Cracker.crt_process.get_command())
        # Kill currently running process
        Cracker.crt_process.terminate()

    # Clean current varibles so all tempfiles are deleted
    Cracker.clean_variables()

    try:
        if Cracker.req is not None:
            Cracker.req.stopwork()
    except Cracker.req.ServerDown:
        pass
    Comunicator.stop()

    sys.exit(0)


def signal_handler(signum, _):
    if signum == signal.SIGINT or signum == signal.SIGTERM:
        Configuration.logger.info("Received signal %d. Exitting!" % signum)
        fast_stop()
    else:
        Configuration.logger.info("Received %s signal" % signum)


class Cracker:
    crt_process = None

    attack_command = None

    scrambler = None
    eta_dict = None
    crt_rule = None
    mac_ssid_job = ""

    old_eta = ""
    path_temp_file = None
    crt_workload = None

    req = None

    @staticmethod
    def get_attack_command(rule, attack_type, filename, ssid):
        generator = ""
        attack_command = "hashcat -w %d --potfile-path=%s" % \
                         (Cracker.crt_workload, Configuration.hashcat_potfile_path)
        scrambler = None

        # Append hash identification based on attack type
        if attack_type == "PMKID":
            attack_command += " -m 16800"
        elif attack_type == "WPA":
            attack_command += " -m 2500"
        else:
            die(True, "Unsupported attack type %s" % attack_type)

        # Translate rule type to command
        if rule["type"] == "generated":
            generator = rule["aux_data"]

        elif rule["type"] == "john":
            generator = "%s --min-length=8 --wordlist=%s --rules=%s --stdout" %\
                        (Configuration.john_path, rule["aux_data"]["baselist"], rule["aux_data"]["rule"])

        elif rule["type"] == "scrambler":
            scrambler = Scrambler(ssid)
            generator = "%s --min-length=8 --wordlist=%s --rules=Jumbo --stdout" %\
                        (Configuration.john_path, scrambler.get_high_value_tempfile())

        elif rule["type"] == "wordlist" or rule["type"] == "mask_hashcat" or rule["type"] == "filemask_hashcat":
            pass

        else:
            die(True, "Rules error! Unknows rule type %s" % rule["type"])

        attack_command += " " + filename

        # Append the wordlist after the cracked file
        if rule["type"] == "wordlist":
            attack_command += " " + rule["aux_data"]
            generator = ""

        if rule["type"] == "mask_hashcat" or rule["type"] == "filemask_hashcat":
            attack_command += " -a 3 " + rule["aux_data"]
        else:
            attack_command += " -a 0"

        return generator, attack_command, scrambler

    @staticmethod
    def clean_variables():
        Cracker.crt_process = None

        if Cracker.path_temp_file is not None:
            os.remove(Cracker.path_temp_file)
        Cracker.path_temp_file = None

        Cracker.attack_command = None
        Cracker.scrambler = None  # Deletes the tempfile
        Cracker.eta_dict = None
        Cracker.crt_rule = None
        Cracker.mac_ssid_job = ""

    @staticmethod
    def seconds_to_time(seconds):
        if seconds < 0:
            return "(0 secs)"
        m, s = divmod(int(seconds), 60)
        h, m = divmod(m, 60)
        d, h = divmod(h, 24)

        if d != 0:
            return "(%d days, %d hrs)" % (d, h)
        if h != 0:
            return "(%d hrs, %d mins)" % (h, m)
        if m != 0:
            return "(%d mins, %d secs)" % (m, s)
        return "(%d secs)" % s

    @staticmethod
    def update_eta():
        new_eta_dict = Cracker.crt_process.get_dict()

        if Cracker.eta_dict is None:
            is_changed = True
        else:
            is_changed = False
            for key, value in new_eta_dict.items():
                if value != Cracker.eta_dict[key]:
                    is_changed = True
                    break

        # If no changes were made no updates are necessary
        if not is_changed:
            return

        Cracker.eta_dict = new_eta_dict
        # TODO This message is wrongly displayed right around when a hashcat process stops
        eta = "Error calculating ETA"

        # TODO maksfile eta is not properly calculated because hashcat outputs eta for current queue
        # TODO each mask has it's own queue
        # TODO implement rule 5 with hashcat only
        if Cracker.crt_rule["type"] == "filemask_hashcat" or Cracker.crt_rule["wordsize"] <= 0:
            eta = "No ETA available"
        elif Cracker.eta_dict["progress"] == -1 and Cracker.eta_dict["eta"] == "":
            eta = "Calculating ETA"
        elif Cracker.eta_dict["eta"] != "" and Cracker.eta_dict["eta"] != "(0 secs)":
            eta = Cracker.eta_dict["eta"]
        elif Cracker.eta_dict["speed"] != "" and Cracker.eta_dict["progress"] != -1:
            # For rules generated at runtime with variable base dictionary length we cannot calculate ETA
            # TODO speed could be in kH - adjust for that
            speed = int(Configuration.atoi_regex.match(Cracker.eta_dict["speed"]).group())
            if speed != 0:
                if Cracker.crt_rule["wordsize"] < Cracker.eta_dict["progress"]:
                    Configuration.logger.error("Dict size (%d) seems less than current attacked (%d)" %
                                               (Cracker.crt_rule["wordsize"], Cracker.eta_dict["progress"]))

                eta_seconds = (Cracker.crt_rule["wordsize"] - Cracker.eta_dict["progress"]) / speed
                eta = Cracker.seconds_to_time(eta_seconds)
            else:
                eta = "Generating dict..."

        # Check if the eta already has the desired value in order to avoid an update
        # Usually happens when 'Cracker.crt_rule["wordsize"] <= 0'
        if Cracker.old_eta == eta:
            return

        Cracker.old_eta = eta

        try:
            Cracker.req.sendeta(eta)
        except Cracker.req.ServerDown:
            pass

    @staticmethod
    def safe_send_result(password):
        written_flag = False
        while True:
            try:
                res = Cracker.req.sendresult(password)
                die(res is True, "Sending result '%s' for job '%s' produced an error" %
                    (password, Cracker.mac_ssid_job))

                if os.path.exists(Configuration.save_result_filename):
                    os.remove(Configuration.save_result_filename)

                if res is False:
                    Configuration.logger.warning("Server cancelled last job. Requesting stopwork.")
                    Cracker.req.stopwork()

                break
            except Cracker.req.ServerDown:
                if not written_flag:
                    msg = "Trying to send result '%s' for last job but the server is unreachable" % password
                    Configuration.dual_print(Configuration.logger.warning, msg)
                    written_flag = True
                    with open(Configuration.save_result_filename, "w") as fp:
                        fp.write(password)
                sleep(10)

    @staticmethod
    def process_result():
        # Disable communicator until we start another job
        Comunicator.disable()

        # Check if process exited cleanly
        Cracker.crt_process.check_clean_exit()
        show_stdout = list(filter(None, SingleProcess(Cracker.attack_command +
                                                      " --show").split_stdout()))
        password = ""

        # Check if we cracked something!
        if len(show_stdout) != 0:
            for line in show_stdout:
                cracked_obj = Configuration.hashcat_show_regex.match(line)
                die(cracked_obj is None, "REGEX error! could not match the --show line:%s" % show_stdout)
                password = cracked_obj.group(1)

        Cracker.safe_send_result(password)

        Cracker.clean_variables()

    @staticmethod
    def is_potfile_duplicated(command):
        show_stdout = list(filter(None, SingleProcess(command + " --show").split_stdout()))

        if len(show_stdout) > 0:
            return True
        return False

    @staticmethod
    def start_cracking(work):
        Cracker.mac_ssid_job = "%s-%s" % (work["handshake"]["mac"], work["handshake"]["ssid"])
        msg = "Running '%s' with rule '%s'" % (Cracker.mac_ssid_job, work["rule"]["name"])
        Comunicator.enable(interactive=False)
        Comunicator.dual_printer(msg, Configuration.logger.info)

        _, Cracker.path_temp_file = mkstemp(prefix="psknow_crack")

        if work["handshake"]["file_type"] == "16800":
            with open(Cracker.path_temp_file, "w") as fd:
                fd.write(work["handshake"]["data"])
        else:
            with open(Cracker.path_temp_file, "wb") as fd:
                fd.write(b64decode(work["handshake"]["data"].encode("utf8")))

        # Memorize attack type - we need it to decode the output
        attack_type = work["handshake"]["handshake_type"]
        Cracker.crt_rule = work["rule"]

        attacked_file = Cracker.path_temp_file

        # Get commands needed to run hashcat
        generator_command, Cracker.attack_command, Cracker.scrambler =\
            Cracker.get_attack_command(Cracker.crt_rule, attack_type, attacked_file, work["handshake"]["ssid"])

        Configuration.logger.info("Trying rule %s on '%s-%s'" %
                                  (Cracker.crt_rule["name"], work["handshake"]["mac"], work["handshake"]["ssid"]))

        if Cracker.is_potfile_duplicated(Cracker.attack_command):
            msg = "Duplication for %s happened. It is already present in potfile!" % Cracker.mac_ssid_job
            Configuration.dual_print(Configuration.logger.critical, msg)
            fast_stop()

        if generator_command == "":
            Cracker.crt_process = SingleProcess(Cracker.attack_command)
        else:
            Cracker.crt_process = DoubleProcess(generator_command, Cracker.attack_command)

    @staticmethod
    def crack_existing_handshakes():
        # Something just finished!
        if Cracker.crt_process is not None and Cracker.crt_process.isdead():
            Cracker.process_result()

        # Process is still running - update eta
        if Cracker.crt_process is not None:
            Cracker.update_eta()
            return

        if slow_stop_flag:
            Configuration.logger.info("Slow shutdown signal received - shutting down!")
            sys.exit(0)

        # Before getting more work make sure we are up to date
        Cracker.complete_missing()

        # Nothing is running - getting more work
        try:
            work = Cracker.req.getwork()
        except Cracker.req.ServerDown:
            # TODO print something maybe
            return

        die(work is True, "An error occured while getting work!")

        # No work to be done right now
        if work is None:
            print("No work to be done, checking in 10 seconds again.")
            return

        # Redundant check
        if work is False:
            Configuration.dual_print(Configuration.logger.warning, "Capabilities out of date!")
            return

        Cracker.start_cracking(work)

    @staticmethod
    def complete_missing():
        gather_flag = False
        try:
            missings = Cracker.req.getmissing()
        except Cracker.req.ServerDown:
            return

        die(missings is True, "Server side error occurred.")

        if missings is None:
            return

        for missing in missings:
            if missing["type"] == "program":
                Configuration.dual_print(Configuration.logger.info, "Please install program '%s'" % missing["name"])
            elif missing["type"] in ["dict", "maskfile", "generator", "john-local.conf"]:
                Configuration.dual_print(Configuration.logger.info, "Downloading '%s'..." % missing["path"])

                gather_flag = True

                if "/" in missing["path"]:
                    directory, filename = missing["path"].rsplit('/', 1)

                    # Create directory if they do not exist
                    os.makedirs(directory, exist_ok=True)
                else:
                    filename = missing["path"]

                try:
                    if Cracker.req.checkfile(filename) is None and \
                            Cracker.req.getfile(filename, missing["path"]) is None:
                        Configuration.dual_print(Configuration.logger.info, "Downloaded '%s'" % missing["path"])
                        if missing["type"] == "generator":
                            os.chmod(missing["path"], stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
                except Cracker.req.ServerDown:
                    return
            else:
                Configuration.dual_print(Configuration.logger.warning, "Unknown missing type '%s'" % missing)

        if gather_flag:
            Configuration.gather_capabilities()

    @staticmethod
    def resume_work():
        if os.path.exists(Configuration.save_result_filename):
            with open(Configuration.save_result_filename) as fp:
                password = fp.read()

            Cracker.safe_send_result(password)
            return

        while True:
            try:
                Cracker.req.stopwork(suppress_stdout=True)
                break
            except Cracker.req.ServerDown:
                pass
        return

    @staticmethod
    def parse_command(cmd):
        global slow_stop_flag

        if cmd == 's':
            # TODO get hashcat status
            pass  # status
        elif cmd == 'q':
            Comunicator.printer("Stopping...", reprint=False)
            fast_stop()
        elif cmd == 'f':
            slow_stop_flag = True
            Comunicator.finished = True
            Comunicator.printer("Will finnish current job and stop. Press 'd' to cancel.")
        elif cmd == 'd':
            if Comunicator.finished:
                slow_stop_flag = False
                Comunicator.finished = False
                Comunicator.printer("Finish command cancelled. Will continue working.")
        elif Comunicator.interactive:
            if cmd == 'p':
                # TODO if finished pause might not work...
                Comunicator.paused = True
                Comunicator.printer("Pause command sent to hashcat")
                # TODO send pause command
            elif cmd == 'r':
                # TODO if process stops resume might not work
                Comunicator.paused = False
                Comunicator.printer("Resume command sent to hashcat")
                # TODO send resume command
            elif cmd == 'c':
                # TODO implement checkpoint command
                pass  # checkpoint

    @staticmethod
    def run():
        Configuration.initialize()
        Cracker.crt_workload = 4  # TODO get value from parameters, adjust from keyboard

        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)

        Cracker.req = Requester(Configuration.apikey, Comunicator.error_printer)

        Cracker.resume_work()

        Comunicator.initialize()

        Comunicator.printer("Cracker initialized", reprint=False)

        # Disable terminal echo
        os.system("stty -echo")

        try:
            last_time = None
            while True:
                now_time = datetime.now()
                if last_time is None or (now_time - last_time).total_seconds() > 10:
                    last_time = now_time
                    Cracker.crack_existing_handshakes()

                cmd = Comunicator.get_command()
                if cmd is not None:
                    Cracker.parse_command(cmd)
                sleep(0.1)
        except Exception as e:
            Configuration.dual_print(Configuration.logger.critical,
                                     "Caught unexpected exception: '%s'" % (traceback.format_exc()))
            Cracker.clean_variables()
            die(True, e)
        finally:
            # Reenable terminal echo
            os.system("stty echo")
            pass


if __name__ == "__main__":
    Cracker().run()
