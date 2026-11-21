#!/bin/sh
# Evaluate the selector on a labeled set (only the commits labeled so far).
# usage: tools/eval_set.sh <set_dir> <map_dir> <image> [-v]
set -e
SET=$1; MAP=$2; IMAGE=$3; shift 3
REPO=$(cd "$(dirname "$0")/.." && pwd)
WORK=$(mktemp -d)
mkdir -p "$WORK/commits"
python3 - "$SET" "$WORK" <<'EOF'
import json, os, shutil, sys
s, w = sys.argv[1:3]
a = json.load(open(os.path.join(s, "answers.json")))["commits"]
for c in a:
    shutil.copy(os.path.join(s, "commits", c + ".patch"), os.path.join(w, "commits"))
shutil.copy(os.path.join(s, "tests.json"), w)
json.dump({"commits": a}, open(os.path.join(w, "answers.json"), "w"))
print(len(a), "labeled commits", file=sys.stderr)
EOF
chmod -R a+rX "$WORK"
docker run --rm --read-only --tmpfs /tmp --user 65534:65534 -e HOME=/tmp \
  -e BR_PFAIL=${BR_PFAIL:-0.5} -v "$REPO/selector.py:/app/selector.py:ro" -v "$MAP:/app/map:ro" \
  -v "$WORK/commits:/data/commits:ro" -v "$WORK/tests.json:/data/tests.json:ro" \
  "$IMAGE" python -S -c "
import resource, subprocess, sys
r = subprocess.run(['python', '-S', '/app/selector.py'], stdout=open('/tmp/o', 'w'), stderr=subprocess.DEVNULL)
u = resource.getrusage(resource.RUSAGE_CHILDREN)
print('selector cpu %.3f s, exit %d' % (u.ru_utime + u.ru_stime, r.returncode), file=sys.stderr)
sys.stdout.write(open('/tmp/o').read())" > "$WORK/out.jsonl"
python3 "$REPO/tools/evaluate.py" "$WORK" "$WORK/answers.json" "$WORK/out.jsonl" "$@"
rm -rf "$WORK"
