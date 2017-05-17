#!/usr/bin/python
#
# ==-- process-stats-dir - summarize one or more Swift -stats-output-dirs --==#
#
# This source file is part of the Swift.org open source project
#
# Copyright (c) 2014-2017 Apple Inc. and the Swift project authors
# Licensed under Apache License v2.0 with Runtime Library Exception
#
# See https://swift.org/LICENSE.txt for license information
# See https://swift.org/CONTRIBUTORS.txt for the list of Swift project authors
#
# ==------------------------------------------------------------------------==#
#
# This file processes the contents of one or more directories generated by
# `swiftc -stats-output-dir` and emits summary data, traces etc. for analysis.

import argparse
import csv
import json
import os
import random
import re
import sys


class JobStats:

    def __init__(self, jobkind, jobid, module, start_usec, dur_usec,
                 jobargs, stats):
        self.jobkind = jobkind
        self.jobid = jobid
        self.module = module
        self.start_usec = start_usec
        self.dur_usec = dur_usec
        self.jobargs = jobargs
        self.stats = stats

    def is_driver_job(self):
        return self.jobkind == 'driver'

    def is_frontend_job(self):
        return self.jobkind == 'frontend'

    def driver_jobs_ran(self):
        assert(self.is_driver_job())
        return self.stats.get("Driver.NumDriverJobsRun", 0)

    def driver_jobs_skipped(self):
        assert(self.is_driver_job())
        return self.stats.get("Driver.NumDriverJobsSkipped", 0)

    def driver_jobs_total(self):
        assert(self.is_driver_job())
        return self.driver_jobs_ran() + self.driver_jobs_skipped()

    def merged_with(self, other):
        merged_stats = {}
        for k, v in self.stats.items() + other.stats.items():
            merged_stats[k] = v + merged_stats.get(k, 0.0)
        merged_kind = self.jobkind
        if other.jobkind != merged_kind:
            merged_kind = "<merged>"
        merged_module = self.module
        if other.module != merged_module:
            merged_module = "<merged>"
        merged_start = min(self.start_usec, other.start_usec)
        merged_end = max(self.start_usec + self.dur_usec,
                         other.start_usec + other.dur_usec)
        merged_dur = merged_end - merged_start
        return JobStats(merged_kind, random.randint(0, 1000000000),
                        merged_module, merged_start, merged_dur,
                        self.jobargs + other.jobargs, merged_stats)

    def incrementality_percentage(self):
        assert(self.is_driver_job())
        ran = self.driver_jobs_ran()
        total = self.driver_jobs_total()
        return round((float(ran) / float(total)) * 100.0, 2)

    # Return a JSON-formattable object of the form preferred by google chrome's
    # 'catapult' trace-viewer.
    def to_catapult_trace_obj(self):
        return {"name": self.module,
                "cat": self.jobkind,
                "ph": "X",              # "X" == "complete event"
                "pid": self.jobid,
                "tid": 1,
                "ts": self.start_usec,
                "dur": self.dur_usec,
                "args": self.jobargs}


# Return an array of JobStats objects
def load_stats_dir(path):
    jobstats = []
    fpat = r"^stats-(?P<start>\d+)-swift-(?P<kind>\w+)-(?P<pid>\d+).json$"
    for root, dirs, files in os.walk(path):
        for f in files:
            m = re.match(fpat, f)
            if m:
                # NB: "pid" in fpat is a random number, not unix pid.
                mg = m.groupdict()
                jobkind = mg['kind']
                jobid = int(mg['pid'])
                start_usec = int(mg['start'])

                j = json.load(open(os.path.join(root, f)))
                dur_usec = 1
                jobargs = None
                module = "module"
                patstr = (r"time\.swift-" + jobkind +
                          r"\.(?P<module>[^\.]+)(?P<filename>.*)\.wall$")
                pat = re.compile(patstr)
                stats = dict()
                for (k, v) in j.items():
                    if k.startswith("time."):
                        v = int(1000000.0 * float(v))
                    stats[k] = v
                    tm = re.match(pat, k)
                    if tm:
                        tmg = tm.groupdict()
                        dur_usec = v
                        module = tmg['module']
                        if 'filename' in tmg:
                            ff = tmg['filename']
                            if ff.startswith('.'):
                                ff = ff[1:]
                            jobargs = [ff]

                e = JobStats(jobkind=jobkind, jobid=jobid,
                             module=module, start_usec=start_usec,
                             dur_usec=dur_usec, jobargs=jobargs,
                             stats=stats)
                jobstats.append(e)
    return jobstats


# Passed args with 2-element remainder ["old", "new"], return a list of tuples
# of the form [(name, (oldstats, newstats))] where each name is a common subdir
# of each of "old" and "new", and the stats are those found in the respective
# dirs.
def load_paired_stats_dirs(args):
    assert(len(args.remainder) == 2)
    paired_stats = []
    (old, new) = args.remainder
    for p in sorted(os.listdir(old)):
        full_old = os.path.join(old, p)
        full_new = os.path.join(new, p)
        if not (os.path.exists(full_old) and os.path.isdir(full_old) and
                os.path.exists(full_new) and os.path.isdir(full_new)):
            continue
        old_stats = load_stats_dir(full_old)
        new_stats = load_stats_dir(full_new)
        if len(old_stats) == 0 or len(new_stats) == 0:
            continue
        paired_stats.append((p, (old_stats, new_stats)))
    return paired_stats


def write_catapult_trace(args):
    allstats = []
    for path in args.remainder:
        allstats += load_stats_dir(path)
    json.dump([s.to_catapult_trace_obj() for s in allstats], args.output)


def merge_all_jobstats(jobstats):
    m = None
    for j in jobstats:
        if m is None:
            m = j
        else:
            m = m.merged_with(j)
    return m


def show_paired_incrementality(args):
    fieldnames = ["old_pct", "old_skip",
                  "new_pct", "new_skip",
                  "delta_pct", "delta_skip",
                  "name"]
    out = csv.DictWriter(args.output, fieldnames, dialect='excel-tab')
    out.writeheader()

    for (name, (oldstats, newstats)) in load_paired_stats_dirs(args):
        olddriver = merge_all_jobstats([x for x in oldstats
                                        if x.is_driver_job()])
        newdriver = merge_all_jobstats([x for x in newstats
                                        if x.is_driver_job()])
        if olddriver is None or newdriver is None:
            continue
        oldpct = olddriver.incrementality_percentage()
        newpct = newdriver.incrementality_percentage()
        deltapct = newpct - oldpct
        oldskip = olddriver.driver_jobs_skipped()
        newskip = newdriver.driver_jobs_skipped()
        deltaskip = newskip - oldskip
        out.writerow(dict(name=name,
                          old_pct=oldpct, old_skip=oldskip,
                          new_pct=newpct, new_skip=newskip,
                          delta_pct=deltapct, delta_skip=deltaskip))


def show_incrementality(args):
    fieldnames = ["incrementality", "name"]
    out = csv.DictWriter(args.output, fieldnames, dialect='excel-tab')
    out.writeheader()

    for path in args.remainder:
        stats = load_stats_dir(path)
        for s in stats:
            if s.is_driver_job():
                pct = s.incrementality_percentage()
                out.writerow(dict(name=os.path.basename(path),
                                  incrementality=pct))


def compare_frontend_stats(args):
    assert(len(args.remainder) == 2)
    (olddir, newdir) = args.remainder

    regressions = 0
    fieldnames = ["old", "new", "delta_pct", "name"]
    out = csv.DictWriter(args.output, fieldnames, dialect='excel-tab')
    out.writeheader()

    old_stats = load_stats_dir(olddir)
    new_stats = load_stats_dir(newdir)
    old_merged = merge_all_jobstats([x for x in old_stats
                                     if x.is_frontend_job()])
    new_merged = merge_all_jobstats([x for x in new_stats
                                     if x.is_frontend_job()])
    if old_merged is None or new_merged is None:
        return regressions
    for stat_name in sorted(old_merged.stats.keys()):
        if stat_name in new_merged.stats:
            old = old_merged.stats[stat_name]
            new = new_merged.stats.get(stat_name, 0)
            if old == 0 or new == 0:
                continue
            delta = (new - old)
            delta_pct = round((float(delta) / float(old)) * 100.0, 2)
            if (stat_name.startswith("time.") and
               abs(delta) < args.delta_usec_thresh):
                continue
            if abs(delta_pct) < args.delta_pct_thresh:
                continue
            out.writerow(dict(name=stat_name, old=old, new=new,
                              delta_pct=delta_pct))
            if delta > 0:
                regressions += 1
    return regressions


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--verbose", action="store_true",
                        help="Report activity verbosely")
    parser.add_argument("--output", default="-",
                        type=argparse.FileType('wb', 0),
                        help="Write output to file")
    parser.add_argument("--paired", action="store_true",
                        help="Process two dirs-of-stats-dirs, pairwise")
    parser.add_argument("--delta-pct-thresh", type=float, default=0.01,
                        help="Percentage change required to report")
    parser.add_argument("--delta-usec-thresh", type=int, default=100000,
                        help="Absolute delta on times required to report")
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--catapult", action="store_true",
                       help="emit a 'catapult'-compatible trace of events")
    modes.add_argument("--incrementality", action="store_true",
                       help="summarize the 'incrementality' of a build")
    modes.add_argument("--compare-frontend-stats", action="store_true",
                       help="Compare frontend stats from two stats-dirs")
    parser.add_argument('remainder', nargs=argparse.REMAINDER,
                        help="stats-dirs to process")

    args = parser.parse_args()
    if len(args.remainder) == 0:
        parser.print_help()
        return 1
    if args.catapult:
        write_catapult_trace(args)
    elif args.compare_frontend_stats:
        return compare_frontend_stats(args)
    elif args.incrementality:
        if args.paired:
            show_paired_incrementality(args)
        else:
            show_incrementality(args)
    return None

sys.exit(main())
