#!/bin/zsh
# Run the O3 DJ in the background, kept awake with caffeinate, independent of any terminal window.
#
#   ./dj.sh start [--mock]   start it (extra args go to run.py)
#   ./dj.sh stop             stop it (the speakers finish what they've already loaded, then go quiet)
#   ./dj.sh restart          stop + start; the DJ picks up its own queue where it left off
#   ./dj.sh status           is it running, and what's playing
#   ./dj.sh log              follow the log (Ctrl-C stops following, not the DJ)

cd "${0:A:h}" || exit 1
PY=.venv/bin/python
PIDFILE=data/server.pid
LOG=data/server.log
PORT=$(/usr/bin/python3 -c 'import json; print(json.load(open("config.json"))["port"])' 2>/dev/null || echo 8330)

running() { [[ -f $PIDFILE ]] && kill -0 "$(<$PIDFILE)" 2>/dev/null }

start() {
  if running; then echo "already running (pid $(<$PIDFILE)) - http://localhost:$PORT"; return 0; fi
  if lsof -nP -iTCP:$PORT -sTCP:LISTEN >/dev/null 2>&1; then
    echo "port $PORT is already in use by another process (an older DJ in a terminal tab?):"
    lsof -nP -iTCP:$PORT -sTCP:LISTEN | tail -n +2
    return 1
  fi
  [[ -x $PY ]] || { echo "no $PY - create it: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"; return 1; }
  mkdir -p data
  echo "\n=== start $(date '+%Y-%m-%d %H:%M:%S') $* ===" >> $LOG
  # New session (setsid) + nohup: survives closing this terminal or quitting the app it was started from.
  # caffeinate -is keeps the Mac awake (on power) for as long as the DJ runs.
  nohup $PY -c 'import os, sys; os.setsid(); os.execvp("caffeinate", ["caffeinate", "-is", *sys.argv[1:]])' \
    $PY run.py "$@" >> $LOG 2>&1 < /dev/null &
  echo $! > $PIDFILE
  disown
  for _ in {1..30}; do
    sleep 0.5
    running || { echo "the DJ exited during startup - last lines of $LOG:"; tail -n 20 $LOG; rm -f $PIDFILE; return 1; }
    curl -s -m 1 "http://localhost:$PORT/api/state" >/dev/null && break
  done
  echo "O3 DJ running (pid $(<$PIDFILE)) - http://localhost:$PORT"
  grep -E "found .* room|venue:|on this Wi-Fi" $LOG | tail -n 3
}

stop() {
  if ! running; then echo "not running"; rm -f $PIDFILE; return 0; fi
  local pid=$(<$PIDFILE)
  pkill -TERM -P $pid 2>/dev/null   # the python server; caffeinate exits with it
  kill -TERM $pid 2>/dev/null
  for _ in {1..20}; do kill -0 $pid 2>/dev/null || break; sleep 0.25; done
  kill -0 $pid 2>/dev/null && { pkill -KILL -P $pid; kill -KILL $pid; } 2>/dev/null
  rm -f $PIDFILE
  echo "stopped"
}

state() {
  local code
  code=$(cat <<'EOF'
import json, sys
d = json.load(sys.stdin)
now = (d.get("now") or {}).get("title") or "-"
print("  %s  %s   (dj %s, %s mode)" % (d["status"]["state"].lower(), now, "on" if d["running"] else "off", d["mode"]))
for s in d["speakers"]:
    tags = [t for t, on in (("main", s["coordinator"]), ("muted", s.get("muted"))) if on]
    vol = d["volumes"].get(s["ip"])
    level = "vol %s" % vol if vol is not None and s["in_group"] else "off"
    print("  %s %-28s %-8s %s" % ("●" if s["in_group"] else "○", s["name"], level, " ".join(tags)))
if d["health"].get("speaker_error"):
    print("  speaker error:", d["health"]["speaker_error"])
EOF
)
  curl -s -m 3 "http://localhost:$PORT/api/state" | /usr/bin/python3 -c "$code" 2>/dev/null || echo "  (not answering on port $PORT)"
}

case ${1:-status} in
  start)   shift; start "$@" ;;
  stop)    stop ;;
  restart) shift; stop; start "$@" ;;
  status)  if running; then echo "O3 DJ running (pid $(<$PIDFILE)) - http://localhost:$PORT"; state; else echo "not running"; fi ;;
  log)     tail -n 40 -f $LOG ;;
  *)       echo "usage: ./dj.sh start [--mock] | stop | restart | status | log"; exit 1 ;;
esac
