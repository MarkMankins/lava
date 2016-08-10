#!/usr/bin/python

import argparse
import json
import lockfile
import os
import string
import subprocess32
import sys
import time

from os.path import basename, dirname, join, abspath

from lava import *

start_time = time.time()

project = None
# this is how much code we add to top of any file with main fn in it
NUM_LINES_MAIN_INSTR = 5
debugging = False

def get_suffix(fn):
    split = basename(fn).split(".")
    if len(split) == 1:
        return ""
    else:
        return "." + split[-1]

def exit_error(msg):
    print Fore.RED + msg + Fore.RESET
    sys.exit(1)

# here's how to run the built program
def run_modified_program(install_dir, input_file, timeout):
    cmd = project['command'].format(install_dir=install_dir,input_file=input_file)
    print cmd
    envv = {}
    lib_path = project['library_path'].format(install_dir=install_dir)
    envv["LD_LIBRARY_PATH"] = join(install_dir, lib_path)
    return run_cmd(cmd, install_dir, envv, timeout) # shell=True)

def confirm_bug_in_executable(install_dir):
    cmd = project['command'].format(install_dir=install_dir,input_file="foo")
    nm_cmd = ('nm {}').format(cmd.split()[0])

    (exitcode, output) = run_cmd_notimeout(nm_cmd, None, None)
    if exitcode != 0:
        exit_error("Error running: ".format(nm_cmd))
    else:
        contains_lava = lambda line: "lava_val" in line
        return len(filter(contains_lava, output)) > 0

def filter_printable(text):
    return ''.join([ '.' if c not in string.printable else c for c in text])

if __name__ == "__main__":
    update_db = False
    parser = argparse.ArgumentParser(description='Inject and test LAVA bugs.')
    parser.add_argument('project', type=argparse.FileType('r'),
            help = 'JSON project file')
    parser.add_argument('-b', '--bugid', action="store", default=-1,
            help = 'Bug id (otherwise, highest scored will be chosen)')
    parser.add_argument('-l', '--buglist', action="store", default=False,
            help = 'Inject this list of bugs')
    parser.add_argument('-k', '--knobTrigger', metavar='int', type=int, action="store", default=-1,
            help = 'Specify a knob trigger style bug, eg -k [sizeof knob offset]')
    parser.add_argument('-g', '--gdb', action="store_true", default=False,
            help = 'Switch on gdb mode which will run fuzzed input under gdb and print process mappings')

    args = parser.parse_args()
    project = json.load(args.project)
    project_file = args.project.name

    # Set up our globals now that we have a project
    db = LavaDatabase(project)

    timeout = project['timeout']

    # This is top-level directory for our LAVA stuff.
    top_dir = join(project['directory'], project['name'])
    lava_dir = dirname(dirname(abspath(sys.argv[0])))
    lava_tool = join(lava_dir, 'src_clang', 'build', 'lavaTool')

    # This should be {{directory}}/{{name}}/bugs
    bugs_top_dir = join(top_dir, 'bugs')

    try:
        os.makedirs(bugs_top_dir)
    except: pass

    # This is where we're going to do our injection. We need to make sure it's
    # not being used by another inject.py.
    bugs_parent = ""
    candidate = 0
    bugs_lock = None
    while bugs_parent == "":
        candidate_path = join(bugs_top_dir, str(candidate))
        lock = lockfile.LockFile(candidate_path)
        try:
            lock.acquire(timeout=-1)
            bugs_parent = join(candidate_path)
            bugs_lock = lock
        except lockfile.AlreadyLocked:
            print "Can\'t acquire lock on bug folder"
            bugs_parent = ""
            sys.exit(1)
            candidate += 1

    print "Using dir", bugs_parent

    # release bug lock.  who cares if another process
    # could theoretically modify this directory
    bugs_lock.release()
    # atexit.register(bugs_lock.release)
    # for sig in [signal.SIGINT, signal.SIGTERM]:
        # signal.signal(sig, lambda s, f: sys.exit(-1))

    try:
        os.mkdir(bugs_parent)
    except: pass

    if 'source_root' in project:
        source_root = project['source_root']
    else:
        tar_files = subprocess32.check_output(['tar', 'tf', project['tarfile']], stderr=sys.stderr)
        source_root = tar_files.splitlines()[0].split(os.path.sep)[0]

    queries_build = join(top_dir, source_root)
    bugs_build = join(bugs_parent, source_root)
    bugs_install = join(bugs_build, 'lava-install')
    # Make sure directories and btrace is ready for bug injection.
    def run(args, **kwargs):
        print "run(", " ".join(args), ")"
        subprocess32.check_call(args, cwd=bugs_build,
                stdout=sys.stdout, stderr=sys.stderr, **kwargs)


    if not os.path.exists(bugs_build):
        exit_error("bug_build dir: {} does not exit".format(bugs_build))
    if not os.path.exists(join(bugs_build, '.git')):
        exit_error("bug_build dir: {} does not have git repo".format(bugs_build))
    if not os.path.exists(join(bugs_build, 'btrace.log')):
        exit_error("bug_build dir: {} does not have btrace.log".format(bugs_build))

    lavadb = join(top_dir, 'lavadb')

    main_files = set(project['main_file'])

    if not os.path.exists(join(bugs_build, 'compile_commands.json')):
        exit_error("bug_build dir: {} does not have compile_commands.json".format(bugs_build))

    # Now start picking the bug and injecting
    bugs_to_inject = []
    if args.bugid != -1:
        bug_id = int(args.bugid)
        score = 0
        bugs_to_inject.append(db.session.query(Bug).filter_by(id=bug_id).one())
    elif args.buglist:
        buglist = eval(args.buglist)
        bugs_to_inject = db.session.query(Bug).filter(Bug.id.in_(buglist)).all()
        update_db = False
    else: assert False

    # exits if lava_val does not appear in executable
    if not confirm_bug_in_executable(bugs_install):
        exit_error("A lava bug does not appear to have been injected: ".format(cmd))

    for bug_index, bug in enumerate(bugs_to_inject):
         print "------------\n"
         print "SELECTED BUG {} : {}".format(bug_index, bug.id)#
         print "   (%d,%d)" % (bug.dua.id, bug.atp.id)
         print "DUA:"
         print "   ", bug.dua
         print "ATP:"
         print "   ", bug.atp
         print "max_tcn={}  max_liveness={}".format(
             bug.dua.max_liveness, bug.dua.max_tcn)

    try:
        # build succeeded -- testing
        print "------------\n"
        # first, try the original file
        print "TESTING -- ORIG INPUT"
        orig_input = join(top_dir, 'inputs', basename(bug.dua.inputfile))
        (rv, outp) = run_modified_program(bugs_install, orig_input, timeout)
        if rv != 0:
            print "***** buggy program fails on original input!"
            assert False
        else:
            print "buggy program succeeds on original input"
        print "retval = %d" % rv
        print "output:"
        lines = outp[0] + " ; " + outp[1]
#            print lines
        if update_db:
            db.session.add(Run(build=build, fuzzed=None, exitcode=rv,
                               output=lines, success=True))
        print "SUCCESS"
        # second, fuzz it with the magic value
        print "TESTING -- FUZZED INPUTS"
        suff = get_suffix(orig_input)
        pref = orig_input[:-len(suff)] if suff != "" else orig_input
        real_bugs = []
        for bug_index, bug in enumerate(bugs_to_inject):
            fuzzed_input = "{}-fuzzed-{}{}".format(pref, bug.id, suff)
            print bug
            print "fuzzed = [%s]" % fuzzed_input
            if args.knobTrigger != -1:
                print "Knob size: {}".format(args.knobTrigger)
                mutfile(orig_input, bug.dua.labels, fuzzed_input, bug.id, True, args.knobTrigger)
            else:
                mutfile(orig_input, bug.dua.labels, fuzzed_input, bug.id)
            print "testing with fuzzed input for {} of {} potential.  ".format(
                bug_index + 1, len(bugs_to_inject))
            print "{} real. bug {}".format(len(real_bugs), bug.id)
            (rv, outp) = run_modified_program(bugs_install, fuzzed_input, timeout)
            print "retval = %d" % rv
            # print "output: {}".format(" ;".join(output))
            if update_db:
                db.session.add(Run(build=build, fuzzed=bug, exitcode=rv,
                                output=lines, success=True))
            if rv == -11 or rv == -6:
                real_bugs.append(bug.id)
                if args.gdb:
                    gdb_py_script = join(lava_dir, "scripts/signal_analysis_gdb.py")
                    lib_path = project['library_path'].format(install_dir=bugs_install)
                    lib_prefix = "LD_LIBRARY_PATH={}".format(lib_path)
                    cmd = project['command'].format(install_dir=bugs_install,input_file=fuzzed_input)
                    # if (debug): print "cmd: " + lib_path + " " + cmd
                    gdb_cmd = " gdb -batch -silent -x {} --args {}".format(gdb_py_script, cmd)
                    full_cmd = lib_prefix + gdb_cmd
                    envv = {"LD_LIBRARY_PATH": lib_path}
                    # os.system(full_cmd)
                    (rv, out) = run_cmd(gdb_cmd, bugs_install, envv, 10000) # shell=True)
                    print "\n".join(out)
            print
        f = float(len(real_bugs)) / len(bugs_to_inject)
        print "yield {:.2f} ({} out of {}) real bugs".format(
            f, len(real_bugs), len(bugs_to_inject)
        )
        print "TESTING COMPLETE"
        if len(bugs_to_inject) > 1:
            print "list of real validated bugs:", real_bugs

        if update_db: db.session.commit()
        # NB: at the end of testing, the fuzzed input is still in place
        # if you want to try it
    except Exception as e:
        print "TESTING FAIL"
        if update_db:
            db.session.add(Run(build=build, fuzzed=None, exitcode=None,
                               output=str(e), success=False))
            db.session.commit()
        raise

    print "inject complete %.2f seconds" % (time.time() - start_time)

