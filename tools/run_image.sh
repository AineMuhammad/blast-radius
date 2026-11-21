#!/bin/sh
# Run a built image on a commit set under the scorer's flags; print CPU, then score.
# usage: tools/run_image.sh <image> <set_dir_with_commits_tests_answers> [-v]
set -e
IMAGE=$1; SET=$2; shift 2
REPO=$(cd "$(dirname "$0")/.." && pwd)
OUT=$(mktemp)
docker run --rm --platform linux/amd64 --network none --cpus 2 --memory 4096m --memory-swap 4096m \
  --pids-limit 256 --read-only --tmpfs /tmp:rw,size=64m,mode=1777 --cap-drop ALL \
  --security-opt no-new-privileges --user 65534:65534 --env HOME=/tmp --env BR_SENTINEL=${BR_SENTINEL:-0} --env BR_FAIL_KEEPS_ONE=${BR_FAIL_KEEPS_ONE:-0} \
  -v "$SET/commits:/data/commits:ro" -v "$SET/tests.json:/data/tests.json:ro" \
  "$IMAGE" python -S -c "
import resource, subprocess, sys, time
t = time.monotonic()
r = subprocess.run(['python', '-S', '/app/selector.py'], stdout=open('/tmp/o', 'w'), stderr=subprocess.DEVNULL)
u = resource.getrusage(resource.RUSAGE_CHILDREN)
print('cpu %.3f s, wall %.1f s, exit %d' % (u.ru_utime + u.ru_stime, time.monotonic() - t, r.returncode), file=sys.stderr)
sys.stdout.write(open('/tmp/o').read())" > "$OUT"
python3 "$REPO/tools/evaluate.py" "$SET" "$SET/answers.json" "$OUT" "$@"
rm -f "$OUT"
