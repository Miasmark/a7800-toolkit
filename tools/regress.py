#!/usr/bin/env python3
"""
Run a set of MAME probe jobs against a build and compare what they say with
what the last good build said.

    python tools/regress.py checks.json --var rom=build.a78 --save good.json
    python tools/regress.py checks.json --var rom=next.a78 --against good.json

A mod that changes timing-sensitive code needs the same questions asked of
every build -- does the game still finish a race, is the list still intact,
did the tick rate hold -- and the answer that matters is "same as last
time". Each job is one MAME run (a probe, usually a recording to play back,
some environment) boiled down to one line, its verdict. --save writes the
verdicts; --against compares and exits 1 if any changed.

The job file (JSON):

    {
      "vars": {"out": "build/regress", "frames": "12000"},
      "vars_cmd": "python tools/symbols.py",     optional: prints KEY=VALUE lines
      "cart": "{rom}",                           default for every job
      "jobs": [
        {"name": "health {r}", "for": {"r": ["run-02", "run-03"]},
         "script": "probes/health.lua", "playback": "{r}.inp",
         "env": {"FRAMES": "{frames}", "O": "{out}/health-{r}.csv"},
         "verdict": "python tools/health.py {out}/health-{r}.csv"},
        {"name": "wild {r}", "for": {"r": ["run-02"]},
         "script": "probes/wildfetch.lua", "playback": "{r}.inp",
         "env": {"A7800_WF_LOG": "{out}/wild-{r}.txt"},
         "verdict_file": "{out}/wild-{r}.txt"}
      ]
    }

{name} anywhere is replaced from `vars`, then `vars_cmd`'s output, then
--var (later wins), then the job's own `for` values; `for` with several keys
runs every combination. A job's verdict is the first line printed by its
`verdict` command, or the last line of `verdict_file` (`"verdict_line":
"first"` or `"all"` to change that; `"verdict_grep": REGEX` takes the first
match in it instead, group 1 if there is one), or failing both the last line
MAME printed. `stdout` saves MAME's output (lines matching `stdout_grep`, if
given) to a file before the verdict is taken -- for probes that print.

MAME is $MAME or --mame; the BIOS directory $BIOS or --bios (default
../bios); recordings are looked for in `input_dir` (default "."). Jobs run
in parallel (-j, default 4).
"""
import argparse
import concurrent.futures
import itertools
import json
import os
import re
import shlex
import subprocess
import sys


def substitute(value, env):
    """Fill {name} from env; an unknown name is an error, not an empty string."""
    if isinstance(value, list):
        return [substitute(v, env) for v in value]
    if isinstance(value, dict):
        return dict((k, substitute(v, env)) for k, v in value.items())
    if not isinstance(value, str):
        return value

    def one(m):
        if m.group(1) not in env:
            raise SystemExit("regress: {%s} is not defined" % m.group(1))
        return str(env[m.group(1)])
    return re.sub(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", one, value)


def expand(spec, env):
    """A job spec -> concrete jobs, one per combination of its `for`."""
    loops = spec.get("for") or {}
    keys = sorted(loops)
    out = []
    for combo in itertools.product(*(loops[k] for k in keys)) if keys else [()]:
        e = dict(env)
        e.update(zip(keys, combo))
        job = dict((k, v) for k, v in spec.items() if k != "for")
        out.append(substitute(job, e))
    return out


def run_cmd(cmd):
    p = subprocess.run(cmd if isinstance(cmd, list) else shlex.split(cmd),
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return p.returncode, p.stdout.decode("utf-8", "replace")


def mame_line(job, cfg):
    cmd = [cfg["mame"], "a7800", "-rompath", cfg["bios"], "-cart", job["cart"],
           "-skip_gameinfo", "-keyboardprovider", "none",
           "-input_directory", cfg["input_dir"]]
    if job.get("playback"):
        cmd += ["-playback", job["playback"]]
    cmd += ["-autoboot_script", job["script"], "-window", "-nomax",
            "-nothrottle", "-sound", "none", "-video", job.get("video", "none")]
    return cmd + list(job.get("mame_args", []))


def one_job(job, cfg):
    env = dict(os.environ)
    env.update(dict((k, str(v)) for k, v in job.get("env", {}).items()))
    for path in [v for k, v in job.get("env", {}).items()] + [job.get("stdout", "")]:
        d = os.path.dirname(str(path))
        if d and ("/" in str(path) or "\\" in str(path)):
            os.makedirs(d, exist_ok=True)
    p = subprocess.run(mame_line(job, cfg), env=env,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    text = p.stdout.decode("utf-8", "replace")
    if job.get("stdout"):
        pat = re.compile(job["stdout_grep"]) if job.get("stdout_grep") else None
        with open(job["stdout"], "w") as f:
            for line in text.splitlines():
                if pat is None or pat.search(line):
                    f.write(line + "\n")
    if job.get("verdict"):
        _rc, out = run_cmd(job["verdict"])
        lines = [l.strip() for l in out.splitlines() if l.strip()]
        return lines[0] if lines else "(verdict command printed nothing)"
    if job.get("verdict_file"):
        try:
            lines = [l.rstrip() for l in open(job["verdict_file"]) if l.strip()]
        except IOError:
            return "(no %s: the probe wrote nothing)" % job["verdict_file"]
        if job.get("verdict_grep"):
            m = re.search(job["verdict_grep"], "\n".join(lines))
            if not m:
                return "(no match for %s)" % job["verdict_grep"]
            return m.group(1) if m.groups() else m.group(0)
        which = job.get("verdict_line", "last")
        if not lines:
            return "(empty)"
        return {"first": lines[0], "last": lines[-1]}.get(which, " | ".join(lines))
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    return lines[-1] if lines else "(MAME printed nothing; exit %d)" % p.returncode


def main():
    ap = argparse.ArgumentParser(description=__doc__.strip().split("\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog=__doc__)
    ap.add_argument("jobs", help="the job file (JSON)")
    ap.add_argument("--var", action="append", default=[], metavar="NAME=VALUE")
    ap.add_argument("--save", metavar="FILE", help="write the verdicts as a baseline")
    ap.add_argument("--against", metavar="FILE", help="compare with a baseline")
    ap.add_argument("--only", help="run only jobs whose name contains this")
    ap.add_argument("--list", action="store_true", help="list the jobs and stop")
    ap.add_argument("--mame", default=os.environ.get("MAME", "mame"))
    ap.add_argument("--bios", default=os.environ.get("BIOS", "../bios"))
    ap.add_argument("-j", type=int, default=4, help="parallel MAME runs (default 4)")
    args = ap.parse_args()

    spec = json.load(open(args.jobs))
    env = dict(spec.get("vars", {}))
    if spec.get("vars_cmd"):
        rc, out = run_cmd(substitute(spec["vars_cmd"], env))
        if rc:
            raise SystemExit("regress: vars_cmd failed (exit %d):\n%s" % (rc, out))
        for line in out.splitlines():
            if "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    for kv in args.var:
        k, _, v = kv.partition("=")
        env[k] = v
    jobs = []
    for s in spec["jobs"]:
        s = dict(s)
        s.setdefault("cart", spec.get("cart", "{rom}"))
        jobs += expand(s, env)
    if args.only:
        jobs = [j for j in jobs if args.only in j["name"]]
    names = [j["name"] for j in jobs]
    if len(set(names)) != len(names):
        raise SystemExit("regress: job names repeat -- each needs its own")
    if args.list:
        for j in jobs:
            print(j["name"])
        return 0
    cfg = {"mame": args.mame, "bios": args.bios,
           "input_dir": spec.get("input_dir", ".")}

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.j)) as ex:
        futs = dict((j["name"], ex.submit(one_job, j, cfg)) for j in jobs)
        got = dict((n, futs[n].result()) for n in names)

    base = json.load(open(args.against)) if args.against else {}
    changed = 0
    width = max(len(n) for n in names) if names else 0
    for n in names:
        mark = ""
        if args.against:
            if n not in base:
                mark = "  NEW"
            elif base[n] != got[n]:
                mark = "  CHANGED (was: %s)" % base[n]
                changed += 1
        print("%-*s  %s%s" % (width, n, got[n], mark))
    if args.save:
        with open(args.save, "w") as f:
            json.dump(got, f, indent=1, sort_keys=True)
            f.write("\n")
        print("\nsaved %d verdicts to %s" % (len(got), args.save))
    if args.against:
        print("\n%d of %d changed" % (changed, len(names)))
        return 1 if changed else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
